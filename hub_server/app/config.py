"""
config.py
---------
서버 전체 설정을 한 곳에서 관리한다.
.env 파일이나 환경변수로 값을 덮어쓸 수 있고, 없으면 아래 기본값을 사용한다.

[운영 전제]
  - 클라우드/유료 서비스 없이 로컬 데스크톱 PC 1대에서 전부 구동한다.
    (DB=SQLite 파일, 큐=DB 테이블, 이미지=로컬 디스크)
  - 앱이 최대 변 800px 로 리사이즈해서 올리므로 1장당 100~400KB 수준이다.
  - 동시 요청은 시험/시연 촬영 때만 몰리고 평시에는 산발적이다.
    -> 워커 수와 업로드 상한을 작게 잡아 데스크톱 자원을 과점유하지 않는다.
    -> 단, 코드 구조는 상용 전환을 전제로 큐/재시도/차단기/인증을 모두 유지한다.
"""

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 프로젝트 루트 경로.
# - 소스 실행: main.py 가 있는 폴더
# - exe 실행(PyInstaller): exe 파일이 있는 폴더
#   (exe 내부 임시 폴더에 데이터를 쓰면 종료 시 사라지므로 반드시 분기해야 한다)
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # .env 파일을 읽고, 정의되지 않은 키가 있어도 무시한다.
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- 기본 정보 ----------
    PROJECT_NAME: str = "Central Hub Server"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ---------- 저장소 (전부 로컬 파일) ----------
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'data' / 'central_hub.db'}"
    IMAGE_DIR: Path = BASE_DIR / "data" / "images"          # 업로드 원본 보관
    FEEDBACK_DIR: Path = BASE_DIR / "data" / "dataset_feedback"  # 오탐 재학습 수집
    MODEL_DIR: Path = BASE_DIR / "models"                   # YOLO 가중치/라벨 설정
    ENDPOINTS_FILE: Path = BASE_DIR / "endpoints.ini"

    # ---------- 업로드 제한 ----------
    # 앱이 800px 로 줄여 보내므로 8MB면 충분히 여유롭다. 초과 시 413.
    MAX_UPLOAD_MB: int = 8
    # 허용 최대 변 길이(px). 앱 리사이즈가 빠진 원본이 올라오면 경고/차단 판단에 쓴다.
    # 800px 기준 + 여유. MAX_IMAGE_EDGE 초과 시 REJECT_OVERSIZED_IMAGE 에 따라 동작.
    MAX_IMAGE_EDGE: int = 1600
    REJECT_OVERSIZED_IMAGE: bool = False  # False: 경고만 기록하고 계속 처리
    # 이미지 보관 기간(일).
    # 허브는 지역 서버 전송이 끝나면 이미지를 바로 지운다(전국 트래픽을 받는 서버가
    # 원본을 들고 있으면 저장 자원 낭비). 이 값은 전송에 끝내 실패한 건(DLQ)이나
    # 정리되지 않고 남은 파일을 걷어내는 backstop 용도다.
    IMAGE_RETENTION_DAYS: int = 7

    # =====================================================================
    #  외부 공개 주소 구성
    # ---------------------------------------------------------------------
    #  허브는 로컬 PC 에서 구동되고, 앱과 지역 서버는 인터넷을 통해 접속한다.
    #  여기서 정하는 주소는 지역 서버가 심의 결과를 되돌려 보낼 콜백 주소가 되므로
    #  값이 틀리면 콜백이 도착하지 않는다.
    #
    #  [현재 구성] ngrok 고정 도메인
    #    서버 시작 시 ngrok 로컬 API(127.0.0.1:4040)에서 터널 주소를 확인해
    #    endpoints.ini 의 public_url 과 일치하는지 점검/보정한다.
    # =====================================================================
    NGROK_AUTO_SYNC: bool = True

    # ---------------------------------------------------------------------
    #  [전환 방법] 허브도 자체 고정 도메인(유료)으로 옮길 경우
    #    1) 위 NGROK_AUTO_SYNC 를 False 로 바꾸거나 .env 에 NGROK_AUTO_SYNC=false 지정
    #    2) endpoints.ini 의 public_url 에 자체 도메인을 입력
    #         [CENTRAL_HUB]
    #         public_url = https://hub.example.com
    #    3) app/core/ngrok_sync.py 모듈과 모니터 UI 의 'ngrok 주소 자동 감지'
    #       버튼은 사용하지 않아도 되므로 그대로 두거나 제거
    #    코드 수정은 필요 없다. 아래 한 줄만 바꾸면 감지 로직이 비활성된다.
    # ---------------------------------------------------------------------
    # NGROK_AUTO_SYNC: bool = False

    # ---------- 인증 (외부 공개 서버이므로 필수) ----------
    #  앱과 지역 서버가 같은 값을 헤더로 보내야 한다.
    #  키 생성:  python -c "import secrets; print(secrets.token_urlsafe(32))"
    #  값은 .env 에만 두고 소스에 직접 적지 않는다(.gitignore 에 .env 포함).
    API_KEY: str = ""
    API_KEY_HEADER: str = "X-API-Key"

    # CORS 허용 출처. 모바일 앱과 지역 서버는 CORS 대상이 아니므로 기본값은 비움.
    # 웹 관리 화면을 붙일 때만 해당 도메인을 명시적으로 넣는다.
    CORS_ORIGINS: list[str] = []

    # API 문서(/docs, /redoc, /openapi.json) 공개 여부.
    # 인터넷에 노출되는 서버이므로 시연이 끝나면 false 로 두는 것을 권장한다.
    ENABLE_DOCS: bool = True

    # ---------- 작업 처리(내장 큐) ----------
    # 데스크톱 1대 기준. 시연 촬영 중 순간적으로 몰려도 2개면 충분하고,
    # 값을 올리면 그대로 병렬도가 올라간다(상용 전환 시 조정 지점).
    WORKER_COUNT: int = 2
    MAX_RETRY: int = 3
    RETRY_BACKOFF_SEC: int = 15
    REGIONAL_TIMEOUT_SEC: float = 30.0

    # ---------- YOLO 재검증 ----------
    # VERIFY_MODE
    #   "auto"   : 모델 가중치 파일이 있으면 검증, 없으면 앱 판정을 그대로 통과(UNVERIFIED)
    #   "strict" : 반드시 검증. 모델이 없으면 오류로 처리(상용 운영 권장값)
    #   "off"    : 검증하지 않음(앱 판정 전적 신뢰)
    # 학습이 끝나면 models/ 에 가중치를 넣기만 하면 auto 모드에서 자동 활성화된다.
    VERIFY_MODE: str = "auto"
    YOLO_MODEL_PATH: str = "models/escooter.pt"   # 학습 완료된 가중치를 이 경로에 배치
    YOLO_LABELS_FILE: str = "models/labels.json"  # 클래스명 -> 역할 매핑 설정
    # 앱 전송 해상도이자 YOLO 학습 해상도. 학습 설정과 반드시 일치해야 하므로 임의로 바꾸지 않는다.
    YOLO_IMG_SIZE: int = 800
    YOLO_CONF_THRESHOLD: float = 0.35
    YOLO_MIN_BOX_RATIO: float = 0.002  # 이미지 면적 대비 이보다 작은 박스는 노이즈로 무시
    YOLO_DEVICE: str = "cpu"           # GPU 있으면 "0" (로컬 PC 기준 기본 cpu)

    # ---------- 위반 판정 규칙 (5클래스 기준) ----------
    # 사람 박스와 킥보드 박스가 이만큼 겹치면 탑승으로 본다
    RIDE_OVERLAP_THRESHOLD: float = 0.10
    # 같은 킥보드의 탑승자 박스끼리 이만큼 겹치면 2인 이상 밀착 탑승으로 본다
    MULTI_RIDER_OVERLAP_THRESHOLD: float = 0.05

    # ---------- 헬멧 검출 신뢰 판단 (VLM 재확인 대상 선별) ----------
    #  bare head 클래스가 '맨머리 + 모자'를 함께 학습하므로, helmet 검출은
    #  그 자체로 "헬멧 착용"을 뜻한다. 따라서 확실한 helmet 건은 단속 대상이 아니며
    #  지역 서버로 보내지 않는다.
    #  문제는 동승자·장애물에 가려 부분만 보일 때 발생하는 오검출이다.
    #  아래 조건 중 하나라도 걸리면 '불확실'로 보고 그 건만 VLM 재확인을 요청한다.
    #
    #  이 신뢰 임계값은 학습이 끝난 뒤 실제 검출 분포를 보고 조정해야 한다.
    #  (오검출이 0.8 부근에 몰린다면 0.85~0.90 사이에서 정한다)
    HELMET_TRUST_CONF: float = 0.85
    # 헬멧 박스가 다른 사람/머리 박스와 이만큼 겹치면 가림으로 판단
    OCCLUSION_OVERLAP_THRESHOLD: float = 0.05
    # 박스가 이미지 가장자리에 이 픽셀 이내로 붙어 있으면 잘린 것으로 판단
    EDGE_MARGIN_PX: int = 2

    # ---------- VLM 크롭 좌표 ----------
    # 허브는 크롭하지 않고 좌표만 보낸다. 지역 서버가 padded_bbox 로 잘라 VLM 에 넣는다.
    # 헬멧 박스는 딱 맞게 자르면 챙 모양/실루엣이 잘려 모자와 구분이 어려우므로 여유를 크게 준다.
    HELMET_CROP_PAD_RATIO: float = 0.40
    PERSON_CROP_PAD_RATIO: float = 0.10
    # 크롭 결과가 이 픽셀보다 작으면 low_resolution=true 로 표시한다.
    # 전송 해상도(800px)는 학습 설정에 묶여 고정이므로 화질 보정은 지역 서버가 담당한다.
    # 이 값은 "지역 서버가 업스케일하거나 더 넓은 영역으로 판단해야 하는 건"을 알리는 신호다.
    CROP_MIN_EDGE_PX: int = 48
    # 머리 박스를 못 찾은 탑승자를 지역 서버로 넘길지(True) 폐기할지(False)
    FORWARD_WHEN_HEAD_UNDETECTED: bool = True

    # bare head(미착용) 판정도 VLM 으로 재확인할지.
    #   False(기본) : YOLO 의 bare head 검출을 신뢰해 즉시 위반 확정 -> VLM 호출 없음
    #   True        : 미착용 건도 VLM 확인을 거친다.
    #                 가려짐 때문에 helmet 을 bare head 로 잘못 볼 가능성까지 차단하지만
    #                 (오단속 방지) VLM 호출량이 늘어난다.
    VERIFY_BARE_HEAD_WITH_VLM: bool = False

    # ---------- 오탐 재학습 데이터 회수 ----------
    # 허브 판정과 지역 서버 최종 판정이 어긋난 건에 한해,
    # 지역 서버에 이미지를 되돌려 요청해서 재학습 데이터로 보관한다.
    # (허브는 평소 이미지를 보관하지 않으므로 필요한 것만 사후에 가져온다)
    FEEDBACK_ON_MISMATCH: bool = True
    # 지역 서버 이미지 조회 경로 템플릿 ({event_no} 치환)
    REGIONAL_IMAGE_PATH: str = "/api/v1/event/{event_no}/image"


settings = Settings()  # 앱 전역에서 재사용하는 설정 싱글턴

# 필요한 폴더를 미리 생성
for _directory in (settings.IMAGE_DIR, settings.FEEDBACK_DIR, settings.MODEL_DIR, BASE_DIR / "data"):
    Path(_directory).mkdir(parents=True, exist_ok=True)


# endpoints.ini 가 없으면 기본값으로 만들어 둔다.
# (exe 를 새 PC 에 복사했을 때 설정 파일이 없어 곧바로 오류나는 것을 막는다.
#  주소는 실행 후 모니터 UI 에서 수정하면 된다)
_DEFAULT_ENDPOINTS = """[CENTRAL_HUB]
# 지역 서버가 콜백으로 호출할 허브 자신의 공개 주소
public_url = http://localhost:8000

[REGIONAL_SERVERS]
DAEGU = http://localhost:8001
SEOUL = http://localhost:8002
BUSAN = http://localhost:8003
"""

if not Path(settings.ENDPOINTS_FILE).exists():
    Path(settings.ENDPOINTS_FILE).write_text(_DEFAULT_ENDPOINTS, encoding="utf-8")  # 최초 실행 시 기본 설정 파일 생성
