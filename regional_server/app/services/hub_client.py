"""
services/hub_client.py
----------------------
판정 결과를 중앙 허브로 통보(콜백)한다.

[허브 연동 규약]
  - 엔드포인트   : payload 의 callback_url (허브 ngrok 고정도메인 + /api/v1/callback/complete)
  - 인증         : X-API-Key (허브와 동일한 키)
  - 허용 status  : CONFIRMED, REJECTED, PENDING, CANCELED (그 외는 400)
  - 필수 키      : event_no 또는 trace_id 중 하나 이상
  - 성공 응답    : 200 {"status": "ACK", "trace_id": "..."}
  - 400/401/404  : 재시도 무의미 -> 로그만 남기고 종료
  - 5xx/타임아웃 : 허브 일시 장애 -> 재시도

콜백이 도착하지 않으면 허브 이력은 계속 'ROUTED' 에 머물고
앱 사용자는 "심사 중"만 보게 되므로, 실패 시 재시도가 중요하다.
큐(JobType.HUB_CALLBACK)를 통해 처리하므로 백오프 재시도가 자동으로 적용되고,
재시도까지 모두 소진된 건은 sweep_unsent_callbacks() 가 주기적으로 다시 집어넣는다.
"""

import json
import logging
import threading
from datetime import timedelta
from typing import Optional

import httpx

from app.config import settings
from app.core import job_queue
from app.database import session_scope, utcnow
from app.models import EventRecord, EventStatus, JobType

log = logging.getLogger("regional.hub")

_client_lock = threading.Lock()
_client: Optional[httpx.Client] = None

# 허브가 허용하는 status 값 (api/callback.py 의 ALLOWED_RESULTS 와 일치해야 함)
_ALLOWED = {"CONFIRMED", "REJECTED", "PENDING", "CANCELED"}

# 콜백 재전송 스윕 대상 상태
_SWEEP_STATES = [EventStatus.CONFIRMED, EventStatus.REJECTED]


def get_client() -> httpx.Client:
    """허브 통보용 공용 동기 HTTP 클라이언트(커넥션 풀 재사용)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=httpx.Timeout(settings.HUB_TIMEOUT_SEC, connect=10.0),
                headers=_headers(),
            )
        return _client


def close_client() -> None:
    """서버 종료 시 HTTP 클라이언트 자원을 해제한다."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _headers() -> dict:
    """허브 요청용 API Key 인증 및 우회 헤더 생성."""
    headers = {"Content-Type": "application/json"}
    if settings.API_KEY:
        headers[settings.API_KEY_HEADER] = settings.API_KEY
    # 허브가 ngrok 무료 플랜 뒤에 위치할 경우 경고 HTML 반환을 방지하는 헤더.
    # 연동 명세에서 권장하고 있다. 허브가 자체 도메인으로 옮기면 없어도 무방하다.
    headers["ngrok-skip-browser-warning"] = "true"
    return headers


def _map_status(event_status: str) -> str:
    """
    지역 서버 내부 상태값을 허브가 수신 가능한 문자열 규약으로 변환한다.

    [주의] CANCELED 는 '취소'를 뜻하므로 처리 오류에 쓰면 안 된다.
          허브가 정상 신고를 취소 처리해 버린다. 미확정 상태는 모두 PENDING 이다.
    """
    if event_status == EventStatus.CONFIRMED:
        return "CONFIRMED"
    if event_status == EventStatus.REJECTED:
        return "REJECTED"
    return "PENDING"


def send_callback(job: dict) -> bool:
    """
    판정 결과를 중앙 허브로 통보한다.
    반환값: True = 성공(작업 완료 처리), False = 실패(백오프 재시도 큐 재등록)
    """
    event_no = job["event_no"]

    with session_scope() as db:
        row = db.get(EventRecord, event_no)
        if row is None:
            log.error("[%s] 사건 기록을 찾을 수 없음 -> 콜백 작업 취소", event_no)
            return True

        mapped = _map_status(row.status)

        # 큐에 작업이 들어간 뒤 상태가 바뀐 경우(예: 확정 직후 이의제기 접수)를 방어한다.
        # 통보 대상이 아닌 상태를 그대로 보내면 허브가 COMPLETED_PENDING 을 기록한다.
        if mapped not in ("CONFIRMED", "REJECTED") and not settings.CALLBACK_ON_PENDING_MANUAL:
            log.info("[%s] 현재 상태(%s)는 허브 통보 대상이 아님 -> 콜백 생략",
                     event_no, row.status)
            return True

        payload = {
            "event_no": row.event_no,
            "trace_id": row.trace_id,
            "status": mapped,
            "reason": (row.decision_reason or "")[:500],
        }

        if settings.CALLBACK_INCLUDE_DETAIL:
            try:
                final_types = json.loads(row.violation_types or "[]")
            except (TypeError, ValueError):
                final_types = []
            # 허브 CallbackRequest.detail(Optional[Any]) 로 수신되는 확장 필드.
            payload["detail"] = {
                "violation_types": final_types,   # 지역 서버의 최종 위반 종류
                "reviewer": row.reviewer,          # AUTO / MANUAL
                "regional_status": row.status,     # PENDING_MANUAL 등 내부 상태 원본
                "region_code": row.region_code,
            }

        url = settings.HUB_CALLBACK_URL_OVERRIDE or row.callback_url

    # 허브 규약 검증
    if payload["status"] not in _ALLOWED:
        log.error("[%s] 허브가 허용하지 않는 status 값: %s", event_no, payload["status"])
        return True

    if not payload["event_no"] and not payload["trace_id"]:
        log.error("[%s] event_no/trace_id 가 모두 없어 콜백을 보낼 수 없습니다.", event_no)
        return True

    if not url:
        log.error(
            "[%s] 콜백 URL 이 없습니다. 허브 payload 의 callback_url 또는 "
            "HUB_CALLBACK_URL_OVERRIDE 를 확인하세요.", event_no,
        )
        return True

    try:
        response = get_client().post(url, json=payload)
    except Exception as exc:
        log.warning("[%s] 콜백 전송 실패 (네트워크 장애): %s", event_no, exc)
        return False  # 네트워크 오류 시 재시도 대상

    # HTTP 2xx 응답 (성공 ACK 수신)
    if 200 <= response.status_code < 300:
        with session_scope() as db:
            row = db.get(EventRecord, event_no)
            if row:
                row.callback_done = 1
                row.error_message = None
                row.updated_at = utcnow()
        log.info("[%s] 허브 통보 완료: %s (ACK)", event_no, payload["status"])
        return True

    # 400, 401, 404 등 클라이언트 요청 오류는 재시도해도 실패하므로 종료
    if response.status_code in (400, 401, 404):
        log.error("[%s] 허브 콜백 거부 HTTP %d: %s",
                  event_no, response.status_code, response.text[:300])
        with session_scope() as db:
            row = db.get(EventRecord, event_no)
            if row:
                row.error_message = (
                    f"콜백 거부 HTTP {response.status_code}: {response.text[:200]}"
                )
                row.updated_at = utcnow()
        return True

    log.warning("[%s] 허브 콜백 응답 실패 HTTP %d -> 재시도 예정", event_no, response.status_code)
    return False


def sweep_unsent_callbacks(limit: int = 50) -> int:
    """
    판정이 끝났는데 허브 통보가 안 된 건을 다시 큐에 넣는다.

    큐 재시도(3회)를 모두 소진하면 작업은 FAILED 로 끝나고 아무도 모르게 된다.
    허브가 잠깐 꺼졌다 켜지는 상황(ngrok 재시작 등)에서 이 스윕이 없으면
    매번 수동으로 복구해야 한다. 유지보수 스레드가 주기적으로 호출한다.
    """
    cutoff = utcnow() - timedelta(minutes=settings.CALLBACK_STALE_MINUTES)
    requeued = 0

    with session_scope() as db:
        rows = (
            db.query(EventRecord)
            .filter(
                EventRecord.callback_done == 0,
                EventRecord.status.in_(_SWEEP_STATES),
                EventRecord.updated_at < cutoff,
            )
            .order_by(EventRecord.updated_at.asc())
            .limit(limit)
            .all()
        )
        for row in rows:
            if job_queue.enqueue_once(db, row.event_no, JobType.HUB_CALLBACK):
                requeued += 1

    if requeued:
        log.info("미전송 콜백 %d건을 큐에 다시 넣었습니다.", requeued)
    return requeued
