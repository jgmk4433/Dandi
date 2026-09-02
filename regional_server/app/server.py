"""
server.py
---------
FastAPI 애플리케이션 구성.

lifespan(수명주기)에서 함께 관리하는 것
  - DB 테이블 생성
  - 워커 스레드 풀 시작/종료 (VLM 판별 + 허브 콜백)
  - 유지보수 스레드
      * 미전송 콜백 재등록 (기본 10분 주기)
      * 보관 기간 지난 이미지 정리 (하루 1회)
      * 오래된 작업 기록 정리 (하루 1회)
  - VLM/보안 설정 점검
"""

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin, enforce, event, system
from app.config import settings
from app.core import image_store
from app.core import job_queue
from app.core.job_queue import JobWorkerPool
from app.database import init_db
from app.services import hub_client
from app.services.hub_client import close_client as close_hub_client
from app.services.job_router import handle_job
from app.services.vlm_client import close_client as close_vlm_client
from app.services.vlm_client import health as vlm_health

log = logging.getLogger("regional.server")

_DAILY_SEC = 86400


class MaintenanceThread(threading.Thread):
    """주기적인 정리·복구 작업을 담당한다."""

    def __init__(self, interval_sec: int | None = None):
        super().__init__(name="maintenance", daemon=True)
        self.interval = interval_sec or settings.MAINTENANCE_INTERVAL_SEC
        self._stop = threading.Event()

    def run(self) -> None:
        last_daily = 0.0
        while not self._stop.is_set():
            # ---- 매 주기: 허브에 통보되지 않은 판정 재등록 ----
            try:
                hub_client.sweep_unsent_callbacks()
            except Exception as exc:
                log.warning("콜백 스윕 실패: %s", exc)

            # ---- 하루 1회: 보관기간/작업기록 정리 ----
            now = time.monotonic()
            if now - last_daily >= _DAILY_SEC or last_daily == 0.0:
                last_daily = now
                try:
                    removed = image_store.cleanup_expired()
                    if removed:
                        log.info("보관기간 만료 이미지 %d건 삭제", removed)
                except Exception as exc:
                    log.warning("이미지 정리 실패: %s", exc)
                try:
                    job_queue.cleanup_old_jobs()
                except Exception as exc:
                    log.warning("작업 기록 정리 실패: %s", exc)

            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def _startup_checks() -> None:
    """기동 시 놓치기 쉬운 설정을 점검한다."""
    if not settings.API_KEY:
        log.error(
            "경고: API_KEY 가 설정되지 않았습니다. "
            "허브 요청 검증과 콜백 인증이 모두 비활성 상태입니다."
        )

    status = vlm_health()
    if not status.get("ok"):
        log.error(
            "Ollama 에 연결할 수 없습니다(%s). VLM 판별 작업은 재시도 후 "
            "수동 검수로 넘어갑니다. `ollama serve` 실행 여부를 확인하세요.",
            status.get("detail"),
        )
    elif not status.get("model_installed"):
        same = status.get("same_family") or []
        log.warning(
            "모델 %s 가 설치되어 있지 않습니다. `ollama pull %s` 를 먼저 실행하세요. "
            "(같은 계열 설치본: %s)",
            settings.VLM_MODEL, settings.VLM_MODEL, ", ".join(same) or "없음",
        )
    else:
        log.info("VLM 준비 완료: %s", settings.VLM_MODEL)

    if settings.WORKER_COUNT > 1:
        log.warning(
            "WORKER_COUNT=%d 입니다. Ollama 인스턴스가 1개면 실제 병렬 추론이 되지 않고 "
            "VRAM 부족이 발생할 수 있습니다. OLLAMA_NUM_PARALLEL 설정을 함께 확인하세요.",
            settings.WORKER_COUNT,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------- 시작 ----------
    init_db()
    # 동기 HTTP 점검이라 이벤트 루프를 막지 않도록 스레드에서 실행한다.
    await asyncio.to_thread(_startup_checks)

    app.state.worker_pool = JobWorkerPool(handle_job)
    app.state.worker_pool.start()

    app.state.maintenance = MaintenanceThread()
    app.state.maintenance.start()

    log.info("%s (%s) 준비 완료 - 포트 %d",
             settings.PROJECT_NAME, settings.REGION_CODE, settings.PORT)
    if settings.API_KEY:
        log.info("수동 검수 화면: http://localhost:%d/admin/review?key=<API_KEY>", settings.PORT)
    else:
        log.info("수동 검수 화면: http://localhost:%d/admin/review", settings.PORT)
    yield

    # ---------- 종료 ----------
    app.state.maintenance.stop()
    app.state.worker_pool.stop()
    close_vlm_client()
    close_hub_client()
    log.info("서버 종료")


def create_app() -> FastAPI:
    application = FastAPI(
        title=f"{settings.PROJECT_NAME} ({settings.REGION_CODE})",
        version="1.1.0",
        description="킥보드 위반 지역 서버 — 허브 수신 / VLM 최종 판별 / 수동 검수",
        lifespan=lifespan,
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_DOCS else None,
        openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
    )

    application.include_router(system.router)
    application.include_router(enforce.router)
    application.include_router(event.router)
    application.include_router(admin.router)
    return application


app = create_app()
