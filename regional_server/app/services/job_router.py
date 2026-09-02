"""
services/job_router.py
----------------------
큐에서 꺼낸 작업을 종류에 맞는 처리기로 넘긴다.

  VLM_REVIEW   -> 크롭/전체 이미지 VLM 판별
  HUB_CALLBACK -> 판정 결과를 허브로 통보

작업 종류가 늘어나면 HANDLERS 에만 추가하면 된다.
"""

import logging

from app.models import JobType
from app.services.hub_client import send_callback
from app.services.review_processor import process_vlm_review

log = logging.getLogger("regional.router")

HANDLERS = {
    JobType.VLM_REVIEW: process_vlm_review,
    JobType.HUB_CALLBACK: send_callback,
}  # 작업 종류 -> 처리 함수 매핑


def handle_job(job: dict) -> bool:
    job_type = job.get("job_type") or JobType.VLM_REVIEW
    handler = HANDLERS.get(job_type)
    if handler is None:
        log.error("[%s] 알 수 없는 작업 종류: %s", job.get("event_no"), job_type)
        return True
    return handler(job)
