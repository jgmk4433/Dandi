"""
server.py
---------
FastAPI 애플리케이션 구성.

lifespan(수명주기)에서 다음을 함께 관리한다.
  - DB 테이블 생성
  - 워커 스레드 풀 시작/종료 (Celery 대체)
  - 유지보수 스레드 시작/종료 (오래된 이미지 삭제)
  - 공용 httpx AsyncClient 생성/종료 (요청마다 새로 만들지 않기 위함)
"""

import logging
import threading
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import appeal, callback, enforcement, system
from app.config import settings
from app.core import image_store
from app.core.job_queue import JobWorkerPool
from app.core.ngrok_sync import sync_public_url
from app.database import init_db
from app.services.job_router import handle_job
from app.services.regional_client import close_sync_client

log = logging.getLogger("hub.server")


class MaintenanceThread(threading.Thread):
    """1시간마다 보관 기간이 지난 이미지를 삭제하는 백그라운드 스레드."""

    def __init__(self, interval_sec: int = 3600):
        super().__init__(name="maintenance", daemon=True)
        self.interval = interval_sec
        self._stop = threading.Event()  # 종료 신호용 이벤트

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                removed = image_store.cleanup_expired()  # 만료 이미지 삭제 실행
                if removed:
                    log.info("보관기간 만료 이미지 %d건 삭제", removed)
            except Exception as exc:
                log.warning("이미지 정리 실패: %s", exc)  # 실패해도 스레드는 계속 유지
            self._stop.wait(self.interval)  # 다음 주기까지 대기(종료 시 즉시 깨어남)

    def stop(self) -> None:
        self._stop.set()  # 대기 중인 run() 루프를 깨워 종료


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------- 시작 ----------
    init_db()  # DB 테이블 생성/초기화

    # 외부 공개 주소 확인. 지역 서버 콜백 주소로 사용되므로 먼저 맞춰둔다.
    sync_public_url()
    _check_security()  # 보안 설정 점검(경고만 출력, 기동은 막지 않음)

    # 이의제기/조회용 비동기 HTTP 클라이언트 (커넥션 재사용)
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.REGIONAL_TIMEOUT_SEC, connect=10.0),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        headers={settings.API_KEY_HEADER: settings.API_KEY} if settings.API_KEY else None,
    )

    # 백그라운드 처리 워커 시작
    app.state.worker_pool = JobWorkerPool(handle_job)  # Celery 대신 사용하는 자체 워커 풀
    app.state.worker_pool.start()

    app.state.maintenance = MaintenanceThread()  # 이미지 정리용 백그라운드 스레드
    app.state.maintenance.start()

    log.info("%s 준비 완료 (포트 %d)", settings.PROJECT_NAME, settings.PORT)
    yield  # 이 지점부터 서버가 실제 요청을 처리

    # ---------- 종료 ----------
    app.state.maintenance.stop()  # 유지보수 스레드 종료
    app.state.worker_pool.stop()  # 워커 풀 종료(진행 중 작업 정리 대기)
    await app.state.http_client.aclose()  # 비동기 HTTP 클라이언트 정리
    close_sync_client()  # 동기 HTTP 클라이언트 정리
    log.info("서버 종료")


def _check_security() -> None:
    """
    외부에 공개되는 서버이므로 시작 시 보안 설정을 점검한다.
    치명적인 설정 누락은 눈에 띄게 경고한다(기동은 막지 않는다).
    """
    if not settings.API_KEY:
        log.error(
            "=" * 62 + "\n"
            "  경고: API_KEY 가 설정되지 않았습니다.\n"
            "  이 서버는 인터넷에 공개되므로 누구나 신고를 등록하거나\n"
            "  심의 결과 콜백을 위조할 수 있는 상태입니다.\n"
            "  .env 에 API_KEY 를 지정하세요.\n"
            '    python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            + "=" * 62
        )  # API_KEY 미설정 시 강한 경고 로그
    if settings.ENABLE_DOCS:
        log.warning("API 문서(/docs)가 공개되어 있습니다. 시연 후 ENABLE_DOCS=false 권장.")


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.PROJECT_NAME,
        version="1.0.0",
        description="킥보드 위반 신고 중앙 허브 서버 (앱 <-> 허브 <-> 지역 서버)",
        lifespan=lifespan,
        # 인터넷에 노출되는 서버이므로 문서 공개를 설정으로 제어한다.
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_DOCS else None,
        openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
    )

    # 앱(모바일)과 지역 서버는 CORS 대상이 아니다.
    # 웹 관리 화면을 붙일 계획이 없으므로 허용 출처를 열어둘 이유가 없다.
    # 필요해지면 allow_origins 에 해당 도메인만 명시적으로 추가할 것.
    if settings.CORS_ORIGINS:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        )  # 설정된 출처에 한해 CORS 허용

    application.include_router(system.router)  # 헬스체크 등 시스템 라우터
    application.include_router(enforcement.router)  # 신고 접수/상태조회 라우터
    application.include_router(appeal.router)  # 이의제기 라우터
    application.include_router(callback.router)  # 지역 서버 콜백 라우터
    return application


app = create_app()  # ASGI 앱 인스턴스 (uvicorn 이 참조)