"""
services/enforcement_processor.py
---------------------------------
워커 스레드가 실행하는 핵심 처리 로직.

처리 순서
  1) 이력을 PROCESSING 으로 갱신
  2) 허브 재검증 (YOLO 5클래스) -> 세 갈래로 갈린다
       DISCARD      : 킥보드 미검출 / 탑승자 없음 / 자전거·오토바이
                      -> 지역 서버로 보내지 않고 폐기 (이미지 삭제)
       CONFIRMED    : bare head 탑승자 또는 2인 이상 탑승 -> 위반 확정
                      -> 전송하되 crop_targets 없음 (VLM 2차 판별 불필요)
       VLM_REQUIRED : 헬멧 착용으로 보이는 탑승자
                      -> 전송 + 헬멧 박스 좌표. 지역 서버가 원본을 크롭해
                         VLM 에게 "헬멧인가 모자인가"를 묻는다
     모델이 아직 없으면(학습 중) VERIFY_MODE 에 따라
       auto   -> UNVERIFIED 로 표시하고 판정 없이 전송 (requires_vlm=true).
                 앱은 person+escooter 공존만 확인하므로 위반 판정은 지역 서버가 전담한다.
       strict -> ERROR 로 기록하고 전송 보류
       off    -> 검증 없이 전송

[이미지 보관 정책]
  허브는 전국 요청을 받는 중계 지점이므로 원본을 쌓아두지 않는다.
  전송 성공(ROUTED) 또는 폐기(DISCARD) 즉시 삭제한다.
  재학습 데이터가 필요한 경우(허브 판정과 지역 서버 최종 판정 불일치)에만
  콜백 수신 후 지역 서버에 이미지를 되돌려 요청한다 -> services/feedback_fetcher.py
  3) 사건번호(event_no) 발급 — 재시도 시 기존 번호 재사용(중복 발급 방지)
  4) 지역 서버 전송 (base64 변환은 이 순간에만)
  5) 성공 -> ROUTED(+이미지 삭제) / 실패 -> RETRYING 또는 DLQ_FAILED

앱 판정과 허브 판정이 다르면 verify_detail 에 mismatch 로 남겨
학습 데이터 개선(오탐 수집)에 활용한다.
"""

import json
import logging
from typing import Optional

from app.config import settings
from app.core import image_store
from app.core.trace import TraceManager
from app.database import session_scope, utcnow
from app.models import CentralEventLog, EventStatus
from app.services.regional_client import RegionalServerClient
from app.services.yolo_service import get_yolo_service, verification_enabled

log = logging.getLogger("hub.processor")

# 재시도해도 결과가 바뀌지 않는 오류 (즉시 DLQ 처리)
_NON_RETRYABLE = {"REGION_NOT_CONFIGURED"}


def _update_log(trace_id: str, **fields) -> None:
    """이력 테이블의 특정 행을 부분 갱신하는 헬퍼."""
    with session_scope() as db:
        row = db.get(CentralEventLog, trace_id)
        if row is None:
            log.error("[%s] 이력 행을 찾을 수 없습니다.", trace_id)
            return
        for key, value in fields.items():
            setattr(row, key, value)  # 전달된 필드만 선택적으로 갱신
        row.updated_at = utcnow()


def _load_context(trace_id: str) -> Optional[dict]:
    """처리에 필요한 값만 DB 에서 읽어온다."""
    with session_scope() as db:
        row = db.get(CentralEventLog, trace_id)
        if row is None:
            return None
        has_size = bool(row.image_width and row.image_height)
        return {
            "image_size": (row.image_width, row.image_height) if has_size else None,
        }


def process_enforcement_job(job: dict) -> bool:
    """작업 1건 처리. 반환값 True = 큐에서 완료, False = 재시도 대상."""
    trace_id: str = job["trace_id"]
    region_code: str = job["region_code"]
    image_path: str = job["image_path"]
    attempts: int = job["attempts"]

    context = _load_context(trace_id)
    if context is None:
        log.error("[%s] 이력 행 없음 -> 작업 폐기", trace_id)
        return True

    _update_log(trace_id, status=EventStatus.PROCESSING, error_origin=None, error_message=None)

    service = get_yolo_service()
    verdict = None

    # ---------- 1. 허브 재검증 ----------
    if verification_enabled():
        verdict = service.detect(image_path, image_size=context["image_size"])

        if verdict is None:
            # 모델을 쓸 수 없는 상태 (가중치 없음 / ultralytics 미설치 / 추론 실패)
            if (settings.VERIFY_MODE or "auto").lower() == "strict":
                _update_log(
                    trace_id,
                    yolo_result="ERROR",
                    status=EventStatus.ERROR,
                    error_origin="YOLO_UNAVAILABLE",
                    error_message=service.describe_state(),
                )
                log.error("[%s] strict 모드 검증 불가 -> 전송 보류", trace_id)
                return True
            log.warning("[%s] 검증 불가 -> 앱 판정으로 진행", trace_id)

        elif not verdict.should_forward:
            # DISCARD: 단속 대상이 아니므로 지역 서버로 보내지 않고 이미지도 지운다.
            _update_log(
                trace_id,
                yolo_result="PASSED",
                status=EventStatus.DISCARDED,
                violation_types=json.dumps([], ensure_ascii=False),
                verify_detail=json.dumps(verdict.detail, ensure_ascii=False)[:4000],
                image_path=None,
            )
            image_store.delete(image_path)
            log.info("[%s] 단속 대상 아님 -> 폐기 (%s)",
                     trace_id, verdict.detail.get("reason", "-"))
            return True

    # ---------- 2. 전송 내용 결정 ----------
    if verdict is not None:
        violation_types = verdict.violation_types
        crop_targets = verdict.crop_targets
        # CONFIRMED = 위반 확정(VLM 불필요) / VLM_REQUIRED = 헬멧 2차 판별 필요
        yolo_result = "VIOLATION" if verdict.is_violation else "VLM_REQUIRED"
        requires_vlm = not verdict.is_violation
        detail = dict(verdict.detail)
        detail["decision"] = verdict.decision
    else:
        # 모델 학습 중: 허브는 판정하지 않는다.
        # 앱도 위반을 판정하지 않으므로 위반 종류는 비워서 보내고,
        # 판별은 전적으로 지역 서버 VLM 에 맡긴다.
        violation_types = []
        crop_targets = []
        yolo_result = "UNVERIFIED"
        requires_vlm = True
        detail = {"verified": False, "reason": service.describe_state()}

    # ---------- 3. 사건번호 발급 (재시도 시 재사용) ----------
    with session_scope() as db:
        row = db.get(CentralEventLog, trace_id)
        if row is None:
            return True
        if not row.event_no:
            # db 를 넘기면 중복 사건번호가 나오지 않도록 확인한다
            row.event_no = TraceManager.generate_event_no(region_code, db)
        event_no = row.event_no
        row.yolo_result = yolo_result
        row.status = EventStatus.PENDING
        row.violation_types = json.dumps(violation_types, ensure_ascii=False)
        row.verify_detail = json.dumps(detail, ensure_ascii=False)[:4000]
        row.updated_at = utcnow()

    # ---------- 4. 지역 서버 전송 ----------
    try:
        image_base64 = image_store.read_as_base64(image_path)
    except OSError as exc:
        _update_log(
            trace_id,
            status=EventStatus.ERROR,
            error_origin="IMAGE_READ_FAIL",
            error_message=str(exc),
        )
        return True  # 파일이 없으면 재시도해도 무의미

    # 이미지는 저장된 바이트 그대로 보낸다(재인코딩 없음).
    # 좌표는 이 이미지 픽셀 기준 절대값이므로 지역 서버는 EXIF 회전을 적용하면 안 된다.
    payload = {
        "trace_id": trace_id,
        "event_no": event_no,
        "region_code": region_code,
        "timestamp": job.get("reported_at"),

        # 판정 결과
        "violation_types": violation_types,
        "hub_verified": yolo_result == "VIOLATION",   # True = 허브에서 위반 확정
        "requires_vlm": requires_vlm,                 # True = 크롭 후 VLM 2차 판별 필요

        # 원본 이미지 (크롭하지 않음)
        "image_base64": image_base64,
        "image": {
            "width": context["image_size"][0] if context["image_size"] else None,
            "height": context["image_size"][1] if context["image_size"] else None,
            "coord_system": "absolute_xyxy_topleft",
            "apply_exif_rotation": False,
        },

        # 지역 서버가 크롭할 좌표. purpose 로 무엇을 판별할지 지정한다.
        #   HELMET_VERIFY    : 이 착용물이 헬멧인지 모자인지
        #   HEADWEAR_UNKNOWN : 머리 박스 미검출 -> 사람 박스에서 착용 여부 확인
        "crop_targets": crop_targets,
        "crop_rois": crop_targets,  # 이전 스펙 호환용 별칭

        "callback_url": _callback_url(),
    }

    success, error_origin, error_message = RegionalServerClient.forward_enforcement_data(
        region_code, payload
    )
    del payload, image_base64  # 대용량 문자열 즉시 해제

    # ---------- 5. 결과 반영 ----------
    if success:
        # 전송이 끝났으므로 허브에는 원본을 남기지 않는다(저장 자원 절약).
        # 나중에 재학습 데이터가 필요하면 지역 서버에서 되돌려 받는다.
        _update_log(trace_id, status=EventStatus.ROUTED, image_path=None,
                    error_origin=None, error_message=None)
        image_store.delete(image_path)
        log.info("[%s] 전송 성공 %s / %s / %s / VLM=%s(크롭 %d건)",
                 trace_id, region_code, event_no,
                 ",".join(violation_types) or "-", requires_vlm, len(crop_targets))
        return True

    give_up = error_origin in _NON_RETRYABLE or attempts >= settings.MAX_RETRY  # 재시도 한도 초과 또는 재시도 무의미 판단
    _update_log(
        trace_id,
        status=EventStatus.DLQ_FAILED if give_up else EventStatus.RETRYING,
        error_origin=error_origin,
        error_message=error_message,
    )
    log.warning("[%s] 전송 실패(%s): %s", trace_id, error_origin, error_message)
    return give_up


def _callback_url() -> str:
    """지역 서버가 호출할 허브 콜백 주소 (endpoints.ini 의 public_url 기준)."""
    from app.core.service_registry import registry

    try:
        base = registry.config_loader.get_central_public_url()
    except Exception:
        base = ""
    if not base:
        base = f"http://localhost:{settings.PORT}"
    if "localhost" in base or "127.0.0.1" in base:
        # 지역 서버는 인터넷 너머에 있으므로 로컬 주소로는 콜백을 받을 수 없다.
        log.warning("콜백 주소가 로컬 주소입니다(%s). ngrok 공개 주소를 설정하세요.", base)
    return f"{base}/api/v1/callback/complete"
