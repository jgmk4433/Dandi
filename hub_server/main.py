"""
main.py
-------
실행 진입점.

  python main.py            서버 + 모니터 UI 함께 실행 (기본)
  python main.py --no-ui    서버만 실행 (화면 없는 PC/원격 배포용)
  python main.py --ui-only  UI만 실행 (DB 내용만 확인)

[개선점]
  - 기존 launcher 는 init_db() 를 두 번 호출하고, uvicorn.run() 을 데몬 스레드로 띄워
    UI 를 닫아도 워커/서버가 정상 종료되지 않았다.
    -> uvicorn.Server 객체를 직접 제어해 UI 종료 시 서버도 안전하게 내린다.
  - 서버 로그를 error 레벨로 감춰 문제 파악이 어려웠다 -> info 레벨로 노출.
"""

import argparse
import logging
import sys
import threading
import time

import uvicorn

from app.config import settings
from app.database import init_db


def _setup_logging() -> None:
    """콘솔에 시간/레벨/모듈이 함께 보이도록 로그 형식을 지정한다."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_server() -> uvicorn.Server:
    # exe 로 묶으면 "app.server:app" 같은 문자열 임포트가 실패할 수 있으므로
    # 앱 객체를 직접 넘긴다. (reload 를 쓰지 않으므로 문자열일 필요가 없다)
    from app.server import app as fastapi_app  # 사용 시점에만 임포트(지연 로딩)

    config = uvicorn.Config(
        fastapi_app,
        host=settings.HOST,
        port=settings.PORT,
        log_level="info",
        # 대용량 업로드가 많으므로 keep-alive 를 넉넉히 준다.
        timeout_keep_alive=30,
    )
    return uvicorn.Server(config)  # run() 대신 제어 가능한 Server 객체로 반환


def run_server_only() -> None:
    _build_server().run()  # UI 없이 서버만 블로킹 실행


def run_with_ui() -> int:
    # PySide6 는 UI 모드에서만 임포트한다(헤드리스 서버에서 불필요한 의존성 회피).
    from PySide6.QtWidgets import QApplication

    from app.ui.monitor import CentralHubMonitorUI

    server = _build_server()
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)  # 서버를 별도 스레드에서 구동
    thread.start()

    # 서버가 포트를 열 때까지 잠깐 기다린다(UI 가 먼저 DB 를 읽어도 문제없게).
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    qt_app = QApplication(sys.argv)  # Qt 애플리케이션 생성
    window = CentralHubMonitorUI()  # 모니터 UI 창 생성
    window.show()
    exit_code = qt_app.exec()  # UI 이벤트 루프(창 닫힐 때까지 블로킹)

    # UI 를 닫으면 서버에 종료 신호를 보내고 lifespan 정리(워커 종료)를 기다린다.
    server.should_exit = True
    thread.join(timeout=15)  # 서버 스레드 종료 대기(최대 15초)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="중앙 허브 서버 실행")
    parser.add_argument("--no-ui", action="store_true", help="모니터 UI 없이 서버만 실행")
    parser.add_argument("--ui-only", action="store_true", help="서버 없이 UI만 실행")
    args = parser.parse_args()

    _setup_logging()
    init_db()  # 테이블 준비 (서버 lifespan 에서도 안전하게 재호출됨)

    if args.ui_only:
        from app.ui.monitor import run_ui_standalone

        return run_ui_standalone()  # DB 조회용 UI만 단독 실행

    if args.no_ui:
        run_server_only()  # 서버만 실행
        return 0

    return run_with_ui()  # 기본 모드: 서버 + UI 함께 실행


if __name__ == "__main__":
    sys.exit(main())  # main() 반환값을 프로세스 종료 코드로 사용