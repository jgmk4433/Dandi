"""
api/callback.py
---------------
지역 서버가 심의 결과를 알려줄 때 호출하는 콜백.

  POST /api/v1/callback/complete
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core import image_store, job_queue
from app.database import get_db, utcnow
from app.models import CentralEventLog
from app.schemas import CallbackRequest, CallbackResult
from app.security import api_key_guard

log = logging.getLogger("hub.api.callback")

router = APIRouter(
    prefix="/api/v1/callback",
    tags=["Callback"],
    dependencies=[Depends(api_key_guard)],
)

# 지역 서버가 보낼 수 있는 심의 결과 값
ALLOWED_RESULTS = {
    "CONFIRMED",   # 위반 확정 (과태료 처리)
    "REJECTED",    # 위반 아님 (허브/앱 단계 오탐)
    "PENDING",     # 심의 보류
    "CANCELED",    # 취소
}


def _mismatch_kind(hub_result: str, regional_result: str):
    """허브 판정과 지역 최종 판정을 비교해 재학습 대상인지 판단한다."""
    if hub_result == "VIOLATION" and regional_result == "REJECTED":
        return "FALSE_POSITIVE", "허브가 위반 확정했으나 지역 서버 심의 결과 위반 아님"
    if hub_result == "VLM_REQUIRED" and regional_result == "CONFIRMED":
        return "HELMET_MISREAD", "헬멧으로 검출했으나 실제로는 미착용(모자 등)"
    return None, ""  # 두 조건 모두 아니면 판정 일치 -> 재학습 대상 아님


@router.post("/complete", response_model=CallbackResult)
async def regional_complete_callback(payload: CallbackRequest, db: Session = Depends(get_db)):
    """지역 서버 심의 완료 통보를 받아 이력을 갱신한다."""
    result = payload.status.upper()
    if result not in ALLOWED_RESULTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"허용되지 않는 status 값입니다: {payload.status}",
        )

    if not payload.event_no and not payload.trace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="event_no 또는 trace_id 중 하나는 필수입니다.",
        )

    if payload.event_no:
        row = db.query(CentralEventLog).filter(CentralEventLog.event_no == payload.event_no).first()
    else:
        row = db.get(CentralEventLog, payload.trace_id)

    if row is None:
        # 존재하지 않는 건이면 404 로 명확히 알려 지역 서버가 재시도하도록 한다.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="해당 사건을 찾을 수 없습니다.")

    row.status = f"COMPLETED_{result}"
    
    # 지역 서버가 최종 결정한 위반 종류 정보가 전달된 경우 허브 DB에도 반영
    if hasattr(payload, "detail") and isinstance(payload.detail, dict) and "violation_types" in payload.detail:
        row.violation_types = json.dumps(payload.detail["violation_types"], ensure_ascii=False)

    row.updated_at = utcnow()

    # 판정이 어긋난 건만 재학습 데이터로 회수한다.
    kind, reason = _mismatch_kind(row.yolo_result, result)
    if kind and settings.FEEDBACK_ON_MISMATCH and row.event_no:
        job_queue.enqueue_feedback_fetch(
            db,
            trace_id=row.trace_id,
            region_code=row.region_code,
            context={
                "event_no": row.event_no,
                "kind": kind,
                "reason": reason,
                "hub_result": row.yolo_result,
                "regional_result": result,
                "violation_types": row.violation_types,
            },
        )
        log.info("[%s] 판정 불일치(%s) -> 이미지 회수 작업 등록", row.trace_id, kind)

    # 남아 있는 이미지가 있으면 정리한다(정상 흐름에서는 이미 삭제된 상태).
    image_store.delete(row.image_path)
    row.image_path = None

    db.commit()
    log.info("[%s] 심의 완료 통보: %s", row.trace_id, result)
    return CallbackResult(status="ACK", trace_id=row.trace_id)
