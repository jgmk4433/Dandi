"""
services/review_processor.py
----------------------------
핵심 판별 로직. 워커 스레드가 실행한다.

처리 순서
  1) 허브가 이미 위반을 확정한 건(hub_verified) -> VLM 없이 CONFIRMED
  2) crop_targets 가 있으면 -> 좌표대로 크롭 -> 목적별 VLM 판별
  3) crop_targets 가 없으면(허브 YOLO 학습 전) -> 원본 전체를 VLM 이 판단
  4) 판정 결과를 저장하고 허브 콜백 작업을 큐에 넣는다

판정 원칙
  - 확신이 없으면(unknown 또는 신뢰도 미달) 자동 확정하지 않고 PENDING_MANUAL 로 둔다.
    헬멧 착용자를 위반 처리하는 오단속은 놓친 위반보다 비용이 크다.
  - 탑승자가 여럿이면 한 명이라도 미착용이면 위반이다.

[반드시 지킬 것] 어떤 경로로 끝나든 사건은 종결 상태에 도달해야 한다.
  VLM 이 계속 실패한다고 REVIEWING 상태로 방치하면 허브 이력은 영원히 ROUTED 에 머물고
  앱 사용자는 영원히 "심사 중"을 본다. 마지막 시도에서는 반드시 PENDING_MANUAL 로 종결한다.
"""

import json
import logging
from typing import List, Optional

from app.config import settings
from app.core import image_store, job_queue
from app.database import session_scope, utcnow
from app.models import EventRecord, EventStatus, JobType
from app.services import vlm_client
from app.services.vlm_client import VlmError

log = logging.getLogger("regional.review")

VIOLATION_HELMET_NO = "HELMET_NO"
VIOLATION_MULTI_RIDER = "MULTI_RIDER"

# 허브에 통보하는 상태. PENDING_MANUAL 은 설정에 따라 PENDING 으로 알린다.
_CALLBACK_STATES = (EventStatus.CONFIRMED, EventStatus.REJECTED)


def _update(event_no: str, **fields) -> None:
    with session_scope() as db:
        row = db.get(EventRecord, event_no)
        if row is None:
            log.error("[%s] 사건 기록을 찾을 수 없습니다.", event_no)
            return
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utcnow()


def _pack_vlm_result(result: Optional[dict]) -> Optional[str]:
    """
    VLM 응답을 DB 컬럼에 안전하게 담는다.
    단순 문자열 자르기를 하면 JSON 이 깨져 조회 화면에서 파싱에 실패하므로,
    길면 내용을 줄여 유효한 JSON 을 유지한다.
    """
    if not result:
        return None

    packed = json.dumps(result, ensure_ascii=False)
    if len(packed) <= 4000:
        return packed

    # 1차: reason 제거
    trimmed = json.loads(packed)
    if isinstance(trimmed.get("targets"), list):
        for item in trimmed["targets"]:
            if isinstance(item, dict):
                item.pop("reason", None)
    trimmed.pop("reason", None)

    packed = json.dumps(trimmed, ensure_ascii=False)
    if len(packed) <= 4000:
        return packed

    # 2차: 개수만 남긴다
    return json.dumps({"truncated": True, "checked": len(trimmed.get("targets", []) or [])},
                      ensure_ascii=False)


def _finalize(event_no: str, status: str, violation_types: List[str],
              reason: str, vlm_result: Optional[dict], reviewer: str = "AUTO") -> None:
    """판정을 확정하고 허브 콜백 작업을 등록한다."""
    with session_scope() as db:
        row = db.get(EventRecord, event_no)
        if row is None:
            return
        row.status = status
        row.violation_types = json.dumps(violation_types, ensure_ascii=False)
        row.decision_reason = reason[:1000]
        row.reviewer = reviewer
        row.vlm_result = _pack_vlm_result(vlm_result)
        row.callback_done = 0
        row.updated_at = utcnow()

        notify = status in _CALLBACK_STATES or (
            status == EventStatus.PENDING_MANUAL and settings.CALLBACK_ON_PENDING_MANUAL
        )
        if notify:
            job_queue.enqueue_once(db, event_no, JobType.HUB_CALLBACK)

    log.info("[%s] 판정: %s / %s / %s", event_no, status,
             ",".join(violation_types) or "-", reason[:80])


def _terminal_or_retry(event_no: str, job: dict, detail: str,
                       vlm_result: Optional[dict] = None) -> bool:
    """
    재시도 여지가 있으면 False(재시도), 마지막 시도면 수동 검수로 종결하고 True.
    이 함수가 없으면 VLM 이 계속 실패할 때 사건이 REVIEWING 인 채로 영원히 남는다.
    """
    if job.get("is_last_attempt") or job.get("attempts", 0) >= settings.MAX_RETRY:
        _finalize(
            event_no, EventStatus.PENDING_MANUAL, [],
            f"자동 판별 실패({detail[:120]}) -> 수동 검수 필요", vlm_result,
        )
        return True
    return False


def process_vlm_review(job: dict) -> bool:
    """작업 1건 처리. True = 완료, False = 재시도."""
    event_no = job["event_no"]

    with session_scope() as db:
        row = db.get(EventRecord, event_no)
        if row is None:
            log.error("[%s] 사건 없음 -> 작업 폐기", event_no)
            return True

        # 이미 사람이 결론을 낸 건은 자동 판별로 덮어쓰지 않는다.
        if row.status in (EventStatus.CONFIRMED, EventStatus.REJECTED) and row.reviewer == "MANUAL":
            log.info("[%s] 수동 검수 완료 건 -> 자동 판별 생략", event_no)
            return True

        try:
            hub_types = json.loads(row.hub_violation_types or "[]")
        except (TypeError, ValueError):
            hub_types = []
        try:
            crop_targets = json.loads(row.crop_targets or "[]")
        except (TypeError, ValueError):
            crop_targets = []

        context = {
            "image_path": row.image_path,
            "hub_verified": bool(row.hub_verified),
            "requires_vlm": bool(row.requires_vlm),
            "hub_violation_types": hub_types,
            "crop_targets": crop_targets,
        }

    # ---------- 1. 허브가 이미 확정한 건 ----------
    # 맨머리 검출 또는 2인 이상 탑승. VLM 판별 없이 그대로 확정한다.
    if context["hub_verified"]:
        types = context["hub_violation_types"] or [VIOLATION_HELMET_NO]
        _finalize(
            event_no, EventStatus.CONFIRMED, types,
            "허브 YOLO 판정으로 위반 확정 (VLM 판별 불필요)", None,
        )
        return True

    _update(event_no, status=EventStatus.REVIEWING)

    image_path = context["image_path"]
    if not image_path:
        _finalize(event_no, EventStatus.PENDING_MANUAL, [],
                  "이미지 경로가 없어 자동 판별 불가", None)
        return True

    try:
        image = image_store.load(image_path)
    except Exception as exc:
        # 파일 문제는 재시도해도 동일하다. 사건을 미아로 두지 않고 수동 검수로 넘긴다.
        log.error("[%s] 이미지 열기 실패: %s", event_no, exc)
        _update(event_no, error_message=f"이미지 열기 실패: {exc}"[:2000])
        _finalize(event_no, EventStatus.PENDING_MANUAL, [],
                  "원본 이미지를 열 수 없어 자동 판별 불가", None)
        return True

    targets = context["crop_targets"]

    # ---------- 2. 크롭 좌표가 있는 경우 ----------
    if targets:
        return _review_with_crops(
            event_no, job, image, image_path, targets, context["hub_violation_types"]
        )

    # ---------- 3. 좌표가 없는 경우 (허브 YOLO 학습 전 — 현재 상태) ----------
    return _review_full_image(event_no, job, image_path)


def _review_with_crops(event_no: str, job: dict, image, image_path: str,
                       targets: List[dict], hub_types: List[str]) -> bool:
    """
    허브가 준 좌표로 크롭해 목적별로 판별한다.
    한 건이라도 '미착용'으로 확인되면 위반 확정이다.

    [허브 동작 참고] violation_rules.evaluate() 는 VLM_REQUIRED 건에도
    violation_types=["HELMET_NO"] 를 담아 보낸다(VLM 이 착용으로 판정하면 지역이 취소).
    MULTI_RIDER 가 함께 성립한 건은 허브에서 이미 CONFIRMED 로 확정되므로
    이 경로에는 오지 않지만, 설정 변경에 대비해 허브 판정을 승계한다.
    """
    # 가림 상황 판단을 돕기 위해 원본 전체를 보조 이미지로 함께 제시한다.
    context_b64 = image_store.file_to_base64(image_path) if settings.FALLBACK_TO_FULL_IMAGE else None

    results: List[dict] = []
    violation = False
    uncertain = False

    for target in targets:
        purpose = vlm_client.resolve_purpose(target.get("purpose"))
        bbox = target.get("padded_bbox") or target.get("bbox")
        low_res = bool(target.get("low_resolution"))

        # low_resolution 건은 영역을 넓혀서 어깨선까지 보이게 한 뒤 업스케일한다.
        # (연동 명세 권고: 확대보다 넓은 크롭이 헬멧/모자 구분에 효과적)
        cropped = image_store.crop_region(
            image, bbox,
            upscale=low_res,
            expand_ratio=settings.LOW_RES_EXPAND_RATIO if low_res else 0.0,
        )
        if cropped is None:
            uncertain = True
            results.append({"roi_id": target.get("roi_id"), "error": "크롭 실패", "bbox": bbox})
            continue

        try:
            result = vlm_client.verify_crop(purpose, image_store.to_base64(cropped), context_b64)
        except VlmError as exc:
            log.warning("[%s] VLM 호출 실패: %s", event_no, exc)
            summary = {"targets": results, "error": str(exc)[:200]}
            return _terminal_or_retry(event_no, job, str(exc), summary)

        result["roi_id"] = target.get("roi_id")
        result["low_resolution"] = low_res
        results.append(result)

        answer = result["answer"]
        low_confidence = result["confidence"] < settings.VLM_MIN_CONFIDENCE

        if answer == "unknown" or low_confidence:
            uncertain = True
        elif purpose in ("HELMET_VERIFY", "HEADWEAR_UNKNOWN"):
            # yes = 안전모 착용 -> 위반 아님 / no = 미착용 -> 위반
            if answer == "no":
                violation = True
        elif purpose == "BARE_HEAD_VERIFY":
            # yes = 미착용이 맞음 -> 위반 / no = 실제로는 착용 -> 위반 아님
            if answer == "yes":
                violation = True

    summary = {"targets": results}

    # 판정 우선순위: 위반 확인 > 불확실 > 위반 아님
    if violation:
        final_types = [VIOLATION_HELMET_NO]
        if VIOLATION_MULTI_RIDER in (hub_types or []):
            final_types.append(VIOLATION_MULTI_RIDER)
        _finalize(event_no, EventStatus.CONFIRMED, final_types,
                  "VLM 판별 결과 안전모 미착용 확인", summary)
    elif uncertain:
        # 확신이 없으면 자동 확정하지 않는다. 사람이 본다.
        _finalize(event_no, EventStatus.PENDING_MANUAL, [],
                  "가림/화질로 자동 판별 불가 -> 수동 검수 필요", summary)
    else:
        _finalize(event_no, EventStatus.REJECTED, [],
                  "VLM 판별 결과 안전모 착용 확인 (허브 검출 오탐)", summary)
    return True


def _review_full_image(event_no: str, job: dict, image_path: str) -> bool:
    """
    허브 YOLO 학습 전 단계 — 현재 모든 건이 이 경로로 들어온다.
    좌표가 없으므로 원본 전체를 보고 탑승 여부부터 헬멧까지 모두 판단한다.
    허브의 DISCARD 필터도 돌지 않은 상태이므로 자전거·오토바이 배제까지 여기서 한다.
    """
    try:
        result = vlm_client.assess_full_image(image_store.file_to_base64(image_path))
    except VlmError as exc:
        log.warning("[%s] VLM 전체 판별 실패: %s", event_no, exc)
        return _terminal_or_retry(event_no, job, str(exc))

    riding = result["riding"]
    helmet_missing = result["helmet_missing"]
    rider_count = result["rider_count"]
    low_confidence = result["confidence"] < settings.VLM_MIN_CONFIDENCE

    if riding == "no":
        _finalize(event_no, EventStatus.REJECTED, [],
                  "전동킥보드 탑승자가 확인되지 않음", result)
        return True

    if riding == "unknown" or low_confidence or helmet_missing == "unknown":
        _finalize(event_no, EventStatus.PENDING_MANUAL, [],
                  "자동 판별 불가 -> 수동 검수 필요", result)
        return True

    violation_types = []
    if helmet_missing == "yes":
        violation_types.append(VIOLATION_HELMET_NO)
    if rider_count >= 2:
        violation_types.append(VIOLATION_MULTI_RIDER)

    if violation_types:
        _finalize(event_no, EventStatus.CONFIRMED, violation_types,
                  f"VLM 전체 판별: 탑승 {rider_count}명, {result['reason']}", result)
    else:
        _finalize(event_no, EventStatus.REJECTED, [],
                  f"VLM 전체 판별: 위반 사항 없음 ({result['reason']})", result)
    return True
