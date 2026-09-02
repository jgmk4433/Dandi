"""
config.py
---------
지역 서버 설정. .env 파일이나 환경변수로 덮어쓸 수 있다.

[역할] 중앙 허브에서 넘어온 단속 건을 받아
       1) 원본 이미지를 장기 보관하고
       2) 허브가 준 바운딩 박스 좌표로 크롭해 VLM(Ollama)에 최종 판별을 맡기고
       3) 결과를 허브로 콜백하며
       4) 이의제기 건은 수동 검수 대기열에 올린다.
"""

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- 기본 ----------
    PROJECT_NAME: str = "Regional Server"
    REGION_CODE: str = "DAEGU"          # 이 서버가 담당하는 지역
    HOST: str = "0.0.0.0"
    PORT: int = 8001

    # ---------- 저장소 ----------
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'data' / 'regional.db'}"
    IMAGE_DIR: Path = BASE_DIR / "data" / "images"

    # 이미지 보관 기간(일).
    # [중요] 이의제기 수동 검수와 허브의 재학습 이미지 회수가 모두 여기에 걸려 있다.
    #        허브는 콜백 직후 GET /image 를 호출하므로 짧게 잡으면 재학습 데이터가 0건이 된다.
    IMAGE_RETENTION_DAYS: int = 180

    # 처리 완료된 작업 기록 보관 기간(일). review_jobs 테이블 무한 증가 방지.
    JOB_RETENTION_DAYS: int = 7

    # ---------- 인증 ----------
    # 허브와 공유하는 값. 허브 요청 검증과 콜백 발신에 모두 사용한다.
    API_KEY: str = ""
    API_KEY_HEADER: str = "X-API-Key"
    ENABLE_DOCS: bool = True

    # 수동 검수 화면은 브라우저로 여는데 브라우저는 커스텀 헤더를 붙일 수 없다.
    # /admin/review?key=<API_KEY> 로 한 번 열면 이 이름의 쿠키를 심어 이후 요청을 통과시킨다.
    ADMIN_COOKIE_NAME: str = "regional_admin_key"
    ADMIN_COOKIE_MAX_AGE: int = 86400

    # ---------- 처리 ----------
    # [주의] Ollama 인스턴스가 1개이므로 워커를 늘려도 실제 병렬 추론은 되지 않는다.
    #        VRAM 여유를 확인하고 OLLAMA_NUM_PARALLEL 을 함께 올린 뒤에만 2 이상으로.
    WORKER_COUNT: int = 1
    MAX_RETRY: int = 3
    RETRY_BACKOFF_SEC: int = 20

    # ---------- 유지보수 스레드 ----------
    MAINTENANCE_INTERVAL_SEC: int = 600     # 콜백 미전송 스윕 주기
    CALLBACK_STALE_MINUTES: int = 10        # 이 시간 넘게 미전송이면 콜백을 다시 큐에 넣는다

    # ---------- VLM (Ollama) ----------
    OLLAMA_URL: str = "http://localhost:11434"
    VLM_MODEL: str = "qwen3-vl:7b"
    VLM_TIMEOUT_SEC: float = 120.0      # 로컬 추론은 수십 초가 걸릴 수 있다
    VLM_TEMPERATURE: float = 0.0        # 판별 작업이므로 무작위성 제거
    # 모델을 메모리에 유지하는 시간. 기본(5분)이면 유휴 후 첫 건이 로딩에 수십 초를 더 쓴다.
    VLM_KEEP_ALIVE: str = "30m"
    # 판정 신뢰도가 이 값 미만이면 자동 확정하지 않고 수동 검수로 넘긴다
    VLM_MIN_CONFIDENCE: float = 0.60

    # ---------- 이미지 수신 ----------
    # 허브 상한(MAX_UPLOAD_MB=8)과 대칭이 되도록 잡는다. base64 는 원본보다 약 33% 크다.
    MAX_IMAGE_BYTES: int = 12 * 1024 * 1024
    MIN_IMAGE_BYTES: int = 1024

    # ---------- 크롭 처리 ----------
    # 허브가 보내는 padded_bbox 를 그대로 사용한다(헬멧 박스 기준 40% 여유 + 경계 클램핑 완료).
    UPSCALE_MIN_EDGE: int = 448
    # low_resolution=true(크롭 최소변 48px 미만)인 건은 업스케일 전에 영역을 더 넓힌다.
    # 연동 명세 권고: 확대보다 어깨선이 함께 보이는 넓은 크롭이 헬멧/모자 구분에 효과적이다.
    LOW_RES_EXPAND_RATIO: float = 0.3
    # 크롭 판별이 어려울 때 원본 전체를 함께 제시할지
    FALLBACK_TO_FULL_IMAGE: bool = True

    # ---------- 허브 콜백 ----------
    # 허브가 payload 로 callback_url 을 보내주므로 보통 비워둔다.
    # 값이 있으면 payload 값보다 우선한다(허브 주소가 바뀐 경우 대비).
    HUB_CALLBACK_URL_OVERRIDE: str = ""
    HUB_TIMEOUT_SEC: float = 30.0

    # 수동 검수 대기(PENDING_MANUAL)를 허브에 PENDING 으로 알릴지.
    #
    # [False 로 확정한 이유 — 허브 api/callback.py 확인 결과]
    #   허브는 수신한 결과값을 그대로 접두사에 붙여 `COMPLETED_{status}` 로 기록한다.
    #   PENDING 을 보내면 COMPLETED_PENDING 이 되는데 이 값은 허브 EventStatus 에
    #   정의되어 있지 않고, "COMPLETED_" 접두사 때문에 앱과 모니터 UI 에는
    #   심의가 끝난 것처럼 보인다(실제로는 사람이 아직 안 봤다).
    #   허브에 ROUTED 를 만료시키는 로직이 없으므로, 통보하지 않고 ROUTED 로
    #   두는 것이 "지역 서버 심의 중"이라는 의미에 정확히 맞는다.
    #
    #   허브가 COMPLETED_PENDING 또는 별도 중간 상태를 처리하도록 바뀌면 true 로 켠다.
    CALLBACK_ON_PENDING_MANUAL: bool = False

    # 콜백 detail 필드에 최종 위반 종류/검수자를 함께 담을지.
    # 허브 CallbackRequest 에 detail(Optional[Any]) 이 이미 선언되어 있어 안전하다.
    # 허브가 row.violation_types 를 콜백으로 갱신하지 않으므로(callback.py 확인),
    # 이 값을 넣어두면 허브가 한 줄 추가로 앱에 최종 위반 종류를 노출할 수 있다.
    CALLBACK_INCLUDE_DETAIL: bool = True


settings = Settings()

for _directory in (settings.IMAGE_DIR, BASE_DIR / "data"):
    Path(_directory).mkdir(parents=True, exist_ok=True)
