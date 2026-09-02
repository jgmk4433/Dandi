"""
main.py
-------
실행 진입점.

  python main.py            서버 실행
  python main.py --check    설정/Ollama 연결만 점검하고 종료

사전 준비
  1) Ollama 설치 후 실행         : ollama serve
  2) 모델 내려받기               : ollama pull qwen3-vl:7b
  3) .env 에 API_KEY 지정 (허브와 동일한 값)
"""

import argparse
import logging
import sys

import uvicorn

from app.config import settings


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_check() -> int:
    """서버를 띄우지 않고 환경만 확인한다."""
    from app.services.vlm_client import health

    print(f"지역 코드      : {settings.REGION_CODE}")
    print(f"DB             : {settings.DATABASE_URL}")
    print(f"이미지 보관    : {settings.IMAGE_DIR} ({settings.IMAGE_RETENTION_DAYS}일)")
    print(f"API Key        : {'설정됨' if settings.API_KEY else '미설정 (경고)'}")
    print(f"워커 수        : {settings.WORKER_COUNT}")

    status = health()  # Ollama 연결 및 모델 설치 상태 점검
    if not status.get("ok"):
        print(f"Ollama         : 연결 실패 ({status.get('detail')})")
        print("                 `ollama serve` 실행 여부를 확인하세요.")
        return 1

    print("Ollama         : 연결 성공")
    if status.get("model_installed"):
        print(f"모델 {settings.VLM_MODEL:14}: 설치됨")
    else:
        same = ", ".join(status.get("same_family", [])) or "없음"
        print(f"모델 {settings.VLM_MODEL:14}: 미설치 -> `ollama pull {settings.VLM_MODEL}` 필요")
        print(f"                 같은 계열 설치본: {same}")
    print(f"설치된 모델    : {', '.join(status.get('available', [])) or '없음'}")

    admin_url = f"http://localhost:{settings.PORT}/admin/review"
    if settings.API_KEY:
        admin_url += "?key=<API_KEY>"
    print(f"수동 검수 화면 : {admin_url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="지역 서버 실행")
    parser.add_argument("--check", action="store_true", help="설정/Ollama 점검만 수행")
    args = parser.parse_args()

    _setup_logging()

    if args.check:
        return run_check()

    # init_db 는 server.py 의 lifespan 에서 호출한다(중복 호출 제거).
    # exe 로 묶을 가능성을 고려해 앱 객체를 직접 넘긴다(문자열 임포트 회피).
    from app.server import app as fastapi_app

    uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host=settings.HOST,
            port=settings.PORT,
            log_level="info",
            timeout_keep_alive=30,
        )
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
