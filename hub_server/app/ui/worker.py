"""
ui/worker.py
------------
백그라운드 데이터 수집 스레드.

DB 조회와 디스크 용량 계산을 UI 스레드에서 하면 2초마다 화면이 잠깐씩 멈춘다.
QThread 로 옮기고 결과만 시그널로 전달한다.

[주의] 여기서 만든 값은 그대로 UI 로 넘어가므로, ORM 객체가 아니라
      순수 dict/문자열로 변환해서 보낸다(세션이 닫힌 뒤 접근하면 오류).
"""

import logging
from typing import List

from PySide6.QtCore import QObject, Signal, Slot

from app.core import image_store, job_queue
from app.core.circuit_breaker import circuit_breaker
from app.core.trace import to_kst_string
from app.database import SessionLocal
from app.models import CentralEventLog
from app.services.yolo_service import get_yolo_service
from app.ui.theme import HISTORY_LIMIT

log = logging.getLogger("hub.ui.worker")


class MonitorFetchWorker(QObject):
    """UI 타이머가 호출하면 데이터를 모아 data_fetched 로 넘긴다."""

    data_fetched = Signal(dict)
    error_occurred = Signal(str)

    @Slot()
    def fetch(self) -> None:
        try:
            payload = {
                "stats": job_queue.queue_stats(),               # 큐 상태(대기/처리중/완료/실패) 집계
                "disk_mb": image_store.disk_usage_mb(),          # 이미지 저장소 용량
                "yolo_state": get_yolo_service().describe_state(),  # YOLO 모델 로드 상태 설명
                "circuit_snap": circuit_breaker.snapshot(),      # 지역 서버별 서킷 브레이커 상태
                "logs": self._fetch_logs(),                      # 최근 신고 이력
            }
            self.data_fetched.emit(payload)
        except Exception as exc:
            log.warning("모니터 데이터 수집 실패: %s", exc)
            self.error_occurred.emit(str(exc))

    @staticmethod
    def _fetch_logs() -> List[dict]:
        """최근 이력을 dict 목록으로 변환한다."""
        db = SessionLocal()
        try:
            rows = (
                db.query(CentralEventLog)
                .order_by(CentralEventLog.created_at.desc())
                .limit(HISTORY_LIMIT)
                .all()
            )
            return [
                {
                    "trace_id": str(row.trace_id),
                    "event_no": str(row.event_no or "-"),
                    "region_code": str(row.region_code or "-"),
                    "yolo_result": str(row.yolo_result or "-"),
                    "status": str(row.status or "-"),
                    "created_at": to_kst_string(row.created_at),  # KST 문자열로 변환
                    "error_origin": row.error_origin,
                    "error_message": row.error_message,
                    "updated_at": str(row.updated_at) if row.updated_at else "",
                }
                for row in rows
            ]  # ORM 객체 대신 순수 dict 로 변환(세션 종료 후에도 안전하게 사용)
        finally:
            db.close()
