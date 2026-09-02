"""
services/job_router.py
----------------------
작업 큐에서 꺼낸 작업을 종류에 맞는 처리기로 넘긴다.

  ENFORCE        -> 신고 처리 (검증 -> 지역 서버 전송)
  FEEDBACK_FETCH -> 판정 불일치 건의 이미지를 지역 서버에서 회수

워커 풀은 이 함수 하나만 알면 되므로, 작업 종류가 늘어나도 여기만 수정하면 된다.
"""

import logging

from app.models import JobType
from app.services.enforcement_processor import process_enforcement_job
from app.services.feedback_fetcher import fetch_and_store

log = logging.getLogger("hub.router")

HANDLERS = {
    JobType.ENFORCE: process_enforcement_job,
    JobType.FEEDBACK_FETCH: fetch_and_store,
}  # 작업 종류 -> 처리 함수 매핑


def handle_job(job: dict) -> bool:
    """반환 True = 작업 완료, False = 재시도 대상."""
    job_type = job.get("job_type") or JobType.ENFORCE
    handler = HANDLERS.get(job_type)
    if handler is None:
        log.error("[%s] 알 수 없는 작업 종류: %s", job.get("trace_id"), job_type)
        return True  # 처리 불가 -> 큐에서 제거
    return handler(job)
