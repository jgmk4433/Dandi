"""
api/enforce.py
--------------
중앙 허브가 단속 건을 보내는 엔드포인트.

  POST /api/v1/enforce

[중요] 여기서 VLM 판별을 직접 수행하면 안 된다.
      로컬 VLM 추론은 수십 초가 걸리는데 허브 타임아웃은 30초다.
      응답이 늦으면 허브가 실패로 판단해 같은 건을 재전송하고,
      3회 연속 실패하면 서킷 브레이커가 작동해 이 지역으로의 전송이 30초간 끊긴다.
      -> 이미지 저장만 하고 즉시 202 를 반환한 뒤, 판별은 큐에서 처리한다.

[멱등성] 같은 event_no 가 다시 들어오면 새로 만들지 않고 200 을 반환한다.
        (허브 재시도 중 이 서버는 정상 저장했는데 응답만 유실된 경우)
        조회-삽입 사이의 경쟁까지 막기 위해 IntegrityError 도 성공으로 처리한다.

[동기 함수인 이유] async def 로 두면 base64 디코딩과 디스크 쓰기가 이벤트 루프를 막아
                  동시 수신이 직렬화되고 /health 응답까지 늦어진다.
                  일반 def 로 두면 FastAPI 가 스레드풀에서 실행한다.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core import image_store, job_queue
from app.core.image_store import ImageDataError
from app.database import get_db
from app.models import EventRecord, EventStatus, JobType
from app.schemas import DEFAULT_PURPOSE, KNOWN_PURPOSES, EnforceAccepted, EnforceRequest
from app.security import api_key_guard

log = logging.getLogger("regional.api.enforce")

router = APIRouter(
    prefix="/api/v1",
    tags=["Enforcement"],
    dependencies=[Depends(api_key_guard)],
)


def _normalize_targets(payload: EnforceRequest) -> list:
    """
    크롭 목표를 dict 로 펼치고 purpose 를 정규화한다.
    허브가 새로운 purpose 를 추가해도 판별이 통째로 실패하지 않도록
    미지원 값은 기본값으로 대체한다(경고 로그는 남긴다).
    """
    normalized = []
    for target in payload.targets():
        data = target.model_dump()
        purpose = str(data.get("purpose") or DEFAULT_PURPOSE).strip().upper()
        if purpose not in KNOWN_PURPOSES:
            log.warning("[%s] 미지원 purpose=%s -> %s 로 대체",
                        payload.event_no, purpose, DEFAULT_PURPOSE)
            purpose = DEFAULT_PURPOSE
        data["purpose"] = purpose
        normalized.append(data)
    return normalized


@router.post("/enforce", response_model=EnforceAccepted, status_code=status.HTTP_202_ACCEPTED)
def receive_enforcement(
    payload: EnforceRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    """허브가 보낸 단속 건을 접수하고 판별 작업을 큐에 넣는다."""

    # 1) 멱등 처리 — 이미 접수된 사건이면 200 OK 로 즉시 응답
    if db.get(EventRecord, payload.event_no) is not None:
        log.info("[%s] 이미 접수된 사건 (중복 수신)", payload.event_no)
        response.status_code = status.HTTP_200_OK
        return EnforceAccepted(event_no=payload.event_no, message="Already received.")

    # 2) 지역 코드 확인 — 잘못 라우팅된 건을 조용히 삼키지 않는다
    if payload.region_code and payload.region_code.upper() != settings.REGION_CODE.upper():
        log.warning(
            "[%s] 지역 코드 불일치: 수신 %s / 이 서버 %s — 허브 endpoints.ini 를 확인하세요",
            payload.event_no, payload.region_code, settings.REGION_CODE,
        )

    # 3) 이미지 저장 (지역 서버가 원본을 장기 보관한다)
    try:
        image_path, size = image_store.save_base64(payload.event_no, payload.image_base64)
    except ImageDataError as exc:
        # 데이터 자체가 잘못됨 -> 재시도해도 동일하다. 허브 로그에 원인이 남도록 400.
        log.error("[%s] 이미지 데이터 오류: %s", payload.event_no, exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"이미지 데이터 오류: {exc}") from exc
    except OSError as exc:
        # 디스크 가득참·권한 등 일시적일 수 있는 문제 -> 허브 재시도를 유도하도록 500.
        log.exception("[%s] 이미지 저장 실패(환경 문제)", payload.event_no)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"이미지 저장 실패: {exc}"
        ) from exc

    # 허브가 알려준 해상도와 실제 이미지가 다르면 좌표가 어긋난다는 뜻이므로 경고한다.
    if size and payload.image.width and (size[0], size[1]) != (payload.image.width, payload.image.height):
        log.warning(
            "[%s] 해상도 불일치: 허브 %sx%s / 실제 %sx%s — 좌표 해석에 주의",
            payload.event_no, payload.image.width, payload.image.height, size[0], size[1],
        )

    targets = _normalize_targets(payload)

    # 4) 사건 기록 + 판별 작업을 한 트랜잭션으로 생성
    try:
        db.add(
            EventRecord(
                event_no=payload.event_no,
                trace_id=payload.trace_id,
                region_code=payload.region_code or settings.REGION_CODE,
                reported_at=payload.timestamp,
                hub_verified=1 if payload.hub_verified else 0,
                requires_vlm=1 if payload.requires_vlm else 0,
                hub_violation_types=json.dumps(payload.violation_types, ensure_ascii=False),
                crop_targets=json.dumps(targets, ensure_ascii=False),
                image_width=size[0] if size else payload.image.width,
                image_height=size[1] if size else payload.image.height,
                image_path=image_path,
                status=EventStatus.RECEIVED,
                callback_url=payload.callback_url,
            )
        )
        job_queue.enqueue(db, payload.event_no, JobType.VLM_REVIEW)
        db.commit()
    except IntegrityError:
        # 허브가 동시에 두 번 보낸 경우. 1) 의 조회와 삽입 사이의 경쟁.
        db.rollback()
        image_store.delete(image_path)
        log.info("[%s] 동시 중복 수신 -> 기존 기록 유지", payload.event_no)
        response.status_code = status.HTTP_200_OK
        return EnforceAccepted(event_no=payload.event_no, message="Already received.")
    except Exception as exc:
        db.rollback()
        image_store.delete(image_path)
        log.exception("[%s] 접수 실패", payload.event_no)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"접수 처리 실패: {exc}"
        ) from exc

    log.info("[%s] 접수 완료 (trace=%s, 허브확정=%s, VLM필요=%s, 크롭 %d건)",
             payload.event_no, payload.trace_id, payload.hub_verified,
             payload.requires_vlm, len(targets))
    return EnforceAccepted(event_no=payload.event_no)
