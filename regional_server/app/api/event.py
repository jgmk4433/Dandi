"""
api/event.py
------------
사건 조회 / 이의제기 / 이미지 반환.

  GET  /api/v1/event/{event_no}          심의 내역 조회 (허브가 앱으로 중계, 타임아웃 10초)
  POST /api/v1/event/{event_no}/reassign 이의제기 -> 수동 재심의 대기열로 (타임아웃 15초)
  GET  /api/v1/event/{event_no}/image    재학습용 이미지 반환 (허브가 콜백 직후 호출)
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.core import image_store
from app.database import get_db, utcnow
from app.models import EventRecord, EventStatus
from app.schemas import EventDetail, ReassignRequest, SimpleResult
from app.security import api_key_guard

log = logging.getLogger("regional.api.event")

router = APIRouter(
    prefix="/api/v1/event",
    tags=["Event"],
    dependencies=[Depends(api_key_guard)],
)

# 앱 화면에 그대로 노출되는 문구. 내부 상태명을 시민에게 보여주지 않기 위함이다.
STATUS_TEXT = {
    EventStatus.RECEIVED: "접수 완료, 심의 대기 중입니다.",
    EventStatus.REVIEWING: "심의가 진행 중입니다.",
    EventStatus.CONFIRMED: "위반으로 확정되었습니다.",
    EventStatus.REJECTED: "위반에 해당하지 않아 종결되었습니다.",
    EventStatus.PENDING_MANUAL: "담당자 확인이 필요해 검토 중입니다.",
    EventStatus.APPEALED: "이의제기가 접수되어 재심의 중입니다.",
    EventStatus.ERROR: "처리 중 문제가 발생해 담당자가 확인하고 있습니다.",
}


def _find(db: Session, event_no: str) -> EventRecord:
    row = db.get(EventRecord, event_no)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사건을 찾을 수 없습니다.")
    return row


def _iso_utc(value) -> str | None:
    """naive UTC 를 앱이 오해하지 않도록 Z 를 붙여 반환한다."""
    if value is None:
        return None
    return value.isoformat(timespec="seconds") + "Z"


@router.get("/{event_no}", response_model=EventDetail)
def get_event(event_no: str, db: Session = Depends(get_db)):
    """
    심의 내역을 반환한다. 허브가 이 응답을 앱으로 그대로 중계하므로,
    시민에게 보여도 되는 내용만 담는다(내부 오류 메시지 등은 제외).
    """
    row = _find(db, event_no)

    try:
        violation_types = json.loads(row.violation_types or "[]")
    except (TypeError, ValueError):
        violation_types = []

    # VLM 원본 응답은 그대로 노출하지 않고 요약만 전달한다.
    summary = None
    if row.vlm_result:
        try:
            raw = json.loads(row.vlm_result)
            if isinstance(raw, dict) and "targets" in raw:
                targets = [t for t in raw.get("targets", []) if isinstance(t, dict)]
                summary = {
                    "checked": len(targets),
                    "results": [
                        {"answer": t.get("answer"), "confidence": t.get("confidence")}
                        for t in targets
                    ],
                }
            elif isinstance(raw, dict):
                summary = {
                    "answer": raw.get("helmet_missing"),
                    "confidence": raw.get("confidence"),
                }
        except (TypeError, ValueError):
            summary = None

    return EventDetail(
        event_no=row.event_no,
        trace_id=row.trace_id,
        region_code=row.region_code,
        status=row.status,
        status_text=STATUS_TEXT.get(row.status, "처리 중입니다."),
        violation_types=violation_types,
        decision_reason=row.decision_reason,
        reviewer=row.reviewer,
        reported_at=row.reported_at,
        reviewed_at=_iso_utc(row.updated_at),
        vlm_summary=summary,
    )


@router.post("/{event_no}/reassign", response_model=SimpleResult)
def reassign_event(event_no: str, payload: ReassignRequest, db: Session = Depends(get_db)):
    """
    이의제기 접수. 자동 판정을 뒤집지 않고 **수동 검수 대기열**에 올린다.

    자동 판별의 오류를 시스템 안에서 100% 막을 수는 없으므로,
    이 경로가 최종 회복 수단이 된다. 담당자가 /admin/review 화면에서 확인한다.

    [운영 시 고려] 본인 확인 절차는 이 서버(또는 앞단 행정 시스템)에서 처리해야 한다.
                  현재는 사건번호를 아는 요청을 그대로 받는다.
    """
    row = _find(db, event_no)

    # 이미지가 남아 있어야 사람이 확인할 수 있다. 없으면 접수는 받되 경고를 남긴다.
    if not row.image_path or not Path(row.image_path).is_file():
        log.warning("[%s] 이의제기 접수됐으나 원본 이미지가 없습니다. 보관기간을 확인하세요.", event_no)

    row.status = EventStatus.APPEALED
    row.appeal_reason = payload.reason[:2000]
    row.reviewer = None          # 재심의 대상이므로 이전 판정자 표시를 지운다
    row.updated_at = utcnow()
    db.commit()

    log.info("[%s] 이의제기 접수 -> 수동 검수 대기 (trace=%s)", event_no, payload.trace_id)
    return SimpleResult(status="SUCCESS", message="수동 재심의 대기열에 등록되었습니다.")


@router.get("/{event_no}/image")
def get_event_image(
    event_no: str,
    fmt: str = Query("binary", pattern="^(binary|base64)$",
                     description="binary(기본, 권장) 또는 base64"),
    db: Session = Depends(get_db),
):
    """
    원본 이미지를 반환한다. 허브가 재학습 데이터를 회수할 때 호출한다.

    [중요] 허브는 이미지를 보관하지 않으므로 이 서버가 유일한 원본 보관처다.
          허브는 **콜백 직후** 이 엔드포인트를 호출한다.
          콜백 후 이미지를 지우면 이 요청이 항상 404 가 되어
          재학습 데이터를 한 건도 모을 수 없고, 이의제기 수동 검수도 불가능해진다.
    """
    row = _find(db, event_no)
    if not row.image_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이미지가 없습니다.")

    path = Path(row.image_path)
    if not path.is_file():
        # 보관 기간 경과 등. 허브는 404 를 받으면 회수를 포기한다(정상 흐름).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이미지가 삭제되었습니다.")

    if fmt == "base64":
        return JSONResponse({"image_base64": image_store.file_to_base64(str(path))})

    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media_type, filename=path.name)
