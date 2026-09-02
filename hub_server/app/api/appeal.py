"""
api/appeal.py
-------------
이의제기 관련 API.

  GET  /api/v1/appeal/inquire/{event_no}  사건번호로 지역 서버 심의 내역 조회
  POST /api/v1/appeal/submit              이의제기 접수 -> 지역 서버 재배정 요청
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core.service_registry import registry
from app.database import get_db, utcnow
from app.models import CentralEventLog, EventStatus
from app.schemas import AppealRequest, AppealResult
from app.security import api_key_guard

log = logging.getLogger("hub.api.appeal")

router = APIRouter(
    prefix="/api/v1/appeal",
    tags=["Appeal"],
    dependencies=[Depends(api_key_guard)],
)


def _find_log(db: Session, event_no: str) -> CentralEventLog:
    row = db.query(CentralEventLog).filter(CentralEventLog.event_no == event_no).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사건번호를 찾을 수 없습니다.")
    return row


def _record_error(db: Session, row: CentralEventLog, origin: str, message: str) -> None:
    """실패 원인을 이력에 남긴다(모니터 UI 에서 추적 가능하도록)."""
    row.error_origin = origin
    row.error_message = message[:2000]  # 과도하게 긴 메시지는 잘라서 저장
    row.updated_at = utcnow()
    db.commit()


def _get_headers() -> dict:
    """지역 서버 인증을 위한 API Key 및 ngrok 우회 헤더"""
    headers = {"ngrok-skip-browser-warning": "true"}
    if settings.API_KEY:
        headers[settings.API_KEY_HEADER] = settings.API_KEY
    return headers


@router.get("/inquire/{event_no}")
async def inquire_event(event_no: str, request: Request, db: Session = Depends(get_db)):
    """사건번호에 해당하는 지역 서버의 상세 심의 내역을 그대로 중계한다."""
    row = _find_log(db, event_no)
    client = request.app.state.http_client  # lifespan 에서 만든 공용 비동기 클라이언트 재사용

    try:
        endpoint = registry.get_endpoint(row.region_code)
    except (ValueError, FileNotFoundError) as exc:
        _record_error(db, row, "REGION_NOT_CONFIGURED", str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        response = await client.get(
            f"{endpoint}/api/v1/event/{event_no}",
            headers=_get_headers(),
            timeout=10.0,
        )
    except Exception as exc:
        _record_error(db, row, "REGIONAL_INQUIRE_NETWORK_FAIL", f"{type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"지역 서버 연결 실패: {exc}",
        ) from exc

    if not (200 <= response.status_code < 300):
        _record_error(
            db, row, "REGIONAL_INQUIRE_HTTP_ERROR",
            f"HTTP {response.status_code}: {response.text[:500]}",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"지역 서버 오류 응답 (HTTP {response.status_code})",
        )

    return response.json()  # 지역 서버 응답을 그대로 앱에 중계


@router.post("/submit", response_model=AppealResult)
async def submit_appeal(payload: AppealRequest, request: Request, db: Session = Depends(get_db)):
    """이의제기 사유를 지역 서버에 전달해 수동 재심의를 요청한다."""
    row = _find_log(db, payload.event_no)
    client = request.app.state.http_client

    try:
        endpoint = registry.get_endpoint(row.region_code)
    except (ValueError, FileNotFoundError) as exc:
        _record_error(db, row, "REGION_NOT_CONFIGURED", str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        response = await client.post(
            f"{endpoint}/api/v1/event/{payload.event_no}/reassign",
            json={"reason": payload.appeal_reason, "trace_id": row.trace_id},
            headers=_get_headers(),
            timeout=15.0,
        )
    except Exception as exc:
        _record_error(db, row, "REGIONAL_REASSIGN_NETWORK_FAIL", f"{type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"지역 서버 연결 실패: {exc}",
        ) from exc

    if not (200 <= response.status_code < 300):
        _record_error(
            db, row, "REGIONAL_REASSIGN_HTTP_ERROR",
            f"HTTP {response.status_code}: {response.text[:500]}",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"지역 서버가 재배정 요청을 거부했습니다 (HTTP {response.status_code})",
        )

    row.status = EventStatus.APPEALED
    row.error_origin = None
    row.error_message = None
    row.updated_at = utcnow()
    db.commit()

    log.info("[%s] 이의제기 접수 -> 지역 서버 재배정", payload.event_no)
    return AppealResult(status="SUCCESS", message="지역 서버에 수동 재심의를 요청했습니다.")
