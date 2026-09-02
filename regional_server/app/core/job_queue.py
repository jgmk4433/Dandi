"""
core/job_queue.py
-----------------
Redis/Celery 없이 동작하는 내장 작업 큐. 허브와 동일한 구조다.

VLM 추론은 수십 초가 걸릴 수 있으므로 HTTP 요청 안에서 처리하면
허브가 타임아웃(30초)으로 판단해 같은 건을 재전송한다.
따라서 수신은 즉시 응답하고 실제 판별은 이 큐를 통해 백그라운드에서 처리한다.
"""

import json
import logging
import threading
import time
from datetime import timedelta
from typing import Callable, Dict, Optional

from app.config import settings
from app.database import SessionLocal, session_scope, utcnow
from app.models import JobState, JobType, ReviewJob

log = logging.getLogger("regional.queue")

_claim_lock = threading.Lock()

# 아직 처리되지 않은 상태들
_OPEN_STATES = (JobState.QUEUED, JobState.RUNNING)


def enqueue(db, event_no: str, job_type: str = JobType.VLM_REVIEW,
            context: Optional[dict] = None, delay_sec: int = 0) -> None:
    """
    작업을 큐에 등록한다. commit 은 호출측이 한다
    (수신 기록과 작업을 한 트랜잭션으로 묶기 위함).
    """
    db.add(
        ReviewJob(
            event_no=event_no,
            job_type=job_type,
            context=json.dumps(context or {}, ensure_ascii=False),
            state=JobState.QUEUED,
            next_attempt_at=utcnow() + timedelta(seconds=delay_sec),
        )
    )


def enqueue_once(db, event_no: str, job_type: str,
                 context: Optional[dict] = None, delay_sec: int = 0) -> bool:
    """
    같은 사건·같은 종류의 미처리 작업이 없을 때만 등록한다.
    콜백 재전송 스윕이 같은 건을 중복으로 쌓는 것을 막는다.
    반환: True = 새로 등록함
    """
    exists = (
        db.query(ReviewJob.id)
        .filter(
            ReviewJob.event_no == event_no,
            ReviewJob.job_type == job_type,
            ReviewJob.state.in_(_OPEN_STATES),
        )
        .first()
    )
    if exists is not None:
        return False  # 이미 처리 대기 중인 동일 작업이 있으므로 중복 등록하지 않음

    enqueue(db, event_no, job_type, context, delay_sec)
    return True


def requeue_stale_jobs() -> int:
    """서버 시작 시 호출. 비정상 종료로 RUNNING 에 남은 작업을 되살린다."""
    with session_scope() as db:
        stale = db.query(ReviewJob).filter(ReviewJob.state == JobState.RUNNING).all()
        for job in stale:
            job.state = JobState.QUEUED
            job.next_attempt_at = utcnow()
            # 재기동으로 인한 되살림은 시도 횟수에서 되돌려 준다.
            job.attempts = max(0, (job.attempts or 1) - 1)
        count = len(stale)
    if count:
        log.warning("중단된 작업 %d건을 큐에 다시 넣었습니다.", count)
    return count


def cleanup_old_jobs() -> int:
    """오래된 완료 기록을 지운다. review_jobs 테이블 무한 증가 방지."""
    cutoff = utcnow() - timedelta(days=settings.JOB_RETENTION_DAYS)
    with session_scope() as db:
        removed = (
            db.query(ReviewJob)
            .filter(ReviewJob.state == JobState.DONE, ReviewJob.updated_at < cutoff)
            .delete(synchronize_session=False)
        )
    if removed:
        log.info("완료된 작업 기록 %d건 정리", removed)
    return int(removed or 0)


def _claim_next_job() -> Optional[dict]:
    """작업 1건을 RUNNING 으로 선점하고 필요한 값만 dict 로 복사해 반환한다."""
    with _claim_lock:
        db = SessionLocal()
        try:
            job = (
                db.query(ReviewJob)
                .filter(
                    ReviewJob.state == JobState.QUEUED,
                    ReviewJob.next_attempt_at <= utcnow(),
                )
                .order_by(ReviewJob.id.asc())
                .first()
            )
            if job is None:
                return None

            job.state = JobState.RUNNING
            job.attempts = (job.attempts or 0) + 1
            job.updated_at = utcnow()
            db.commit()

            try:
                context = json.loads(job.context) if job.context else {}
            except (TypeError, ValueError):
                context = {}

            return {
                "id": job.id,
                "event_no": job.event_no,
                "job_type": job.job_type or JobType.VLM_REVIEW,
                "attempts": job.attempts,
                "max_attempts": settings.MAX_RETRY,
                "is_last_attempt": job.attempts >= settings.MAX_RETRY,
                "context": context,
            }
        finally:
            db.close()


def _finish_job(job_id: int, done: bool, error: Optional[str] = None) -> None:
    """완료 처리하거나, 재시도 한도 내면 백오프 후 다시 큐에 넣는다."""
    with session_scope() as db:
        job = db.get(ReviewJob, job_id)
        if job is None:
            return

        if done:
            job.state = JobState.DONE
            job.last_error = None
            return

        job.last_error = (error or "")[:2000]
        if job.attempts < settings.MAX_RETRY:
            delay = settings.RETRY_BACKOFF_SEC * job.attempts
            job.state = JobState.QUEUED
            job.next_attempt_at = utcnow() + timedelta(seconds=delay)
            log.warning("[%s] 실패, %d초 후 재시도 (%d/%d)",
                        job.event_no, delay, job.attempts, settings.MAX_RETRY)
        else:
            job.state = JobState.FAILED
            log.error("[%s] 재시도 %d회 모두 실패", job.event_no, job.attempts)


def queue_stats() -> Dict[str, int]:
    from sqlalchemy import func

    stats = {JobState.QUEUED: 0, JobState.RUNNING: 0, JobState.DONE: 0, JobState.FAILED: 0}
    db = SessionLocal()
    try:
        for state, count in db.query(ReviewJob.state, func.count()).group_by(ReviewJob.state).all():
            stats[state] = count
    finally:
        db.close()
    return stats


class JobWorkerPool:
    """작업을 꺼내 handler 로 넘기는 워커 스레드 묶음."""

    def __init__(self, handler: Callable[[dict], bool], worker_count: Optional[int] = None):
        self.handler = handler
        self.worker_count = worker_count or settings.WORKER_COUNT
        self._stop_event = threading.Event()
        self._threads: list = []

    def start(self) -> None:
        requeue_stale_jobs()
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._loop, name=f"review-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("워커 %d개 시작", self.worker_count)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = _claim_next_job()
            except Exception as exc:
                log.exception("작업 선점 실패: %s", exc)
                time.sleep(1.0)
                continue

            if job is None:
                self._stop_event.wait(0.5)
                continue

            try:
                success = bool(self.handler(job))
                _finish_job(job["id"], done=success,
                            error=None if success else "handler returned False")
            except Exception as exc:
                log.exception("[%s] 작업 처리 중 예외", job.get("event_no"))
                _finish_job(job["id"], done=False, error=f"{type(exc).__name__}: {exc}")
