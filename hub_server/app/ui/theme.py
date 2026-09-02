"""
ui/theme.py
-----------
화면 전체에서 쓰는 색/스타일/상수 정의.

색상이나 여백을 바꾸고 싶으면 이 파일만 수정하면 된다.
다른 UI 파일은 여기서 값을 가져다 쓴다.
"""

# ---------------------------------------------------------------------------
#  색상 팔레트 (Teal / Charcoal)
#  코드 곳곳에 색상 문자열을 흩어놓지 않기 위해 이름으로 모아둔다.
# ---------------------------------------------------------------------------
BG_WINDOW = "#1a1d24"
BG_PANEL = "#21252f"
BG_INPUT = "#14171d"
BORDER = "#2b303c"

ACCENT = "#00b4d8"
ACCENT_LIGHT = "#48cae4"
ACCENT_DARK = "#03045e"

TEXT = "#c5c6c7"
TEXT_STRONG = "#edf2f4"
TEXT_MUTED = "#6c757d"
TEXT_INFO = "#90e0ef"

OK = "#00f5d4"        # 정상 / 완료
WARN = "#ffb703"      # 진행 중 / 불안정
ERROR = "#ff4d6d"     # 실패 / 차단
ERROR_TEXT = "#f38ba8"

# ---------------------------------------------------------------------------
#  표시 설정
# ---------------------------------------------------------------------------
# 이력 표 컬럼. 순서를 바꾸면 panels.HistoryTablePanel 의 열 번호도 함께 맞춰야 한다.
COLUMNS = ["ID", "No", "Region", "YOLO", "Status", "Created (KST)"]

# 이력 표에서 한 번에 보여줄 최대 행 수
HISTORY_LIMIT = 100

# 디스크 게이지가 100% 가 되는 기준 용량(MB).
# 실제 디스크 크기를 재지 않고 표시용 기준만 잡은 값이므로 필요에 맞게 조정한다.
DISK_GAUGE_MAX_MB = 2048

# 지역 표시 순서. 여기에 없는 지역은 뒤에 가나다순으로 붙는다.
# (번호는 endpoints.ini 의 [REGION_NUMBERS] 에서 읽는다 — 순서와 번호는 별개다)
REGION_PRIORITY = ["SEOUL", "DAEGU", "BUSAN"]

# ---------------------------------------------------------------------------
#  배지 정의 : (표시 문자, 배경색, 마우스를 올렸을 때 나오는 한글 설명)
#  표에는 한 글자만 넣고, 뜻은 툴팁으로 보여준다.
#  문구를 바꾸고 싶으면 이 표만 수정하면 된다.
# ---------------------------------------------------------------------------
YOLO_BADGES = {
    "VIOLATION":    ("V", ERROR,      "위반 확정 — 맨머리 검출 또는 2인 이상 탑승 (VLM 판별 불필요)"),
    "VLM_REQUIRED": ("M", WARN,       "판별 요청 — 헬멧으로 검출됨. 지역 서버 VLM 이 최종 확인"),
    "PASSED":       ("P", OK,         "단속 대상 아님 — 허브 재검증 결과 위반 없음 (폐기)"),
    "UNVERIFIED":   ("U", TEXT_MUTED, "미검증 — 허브 YOLO 모델 학습 중이라 판정하지 않음"),
    "PENDING":      ("·", BORDER,     "접수됨 — 아직 판정 전"),
    "ERROR":        ("E", ERROR,      "오류 — 검증 중 문제가 발생함"),
}

STATUS_BADGES = {
    "RECEIVED":             ("RUN",  WARN,       "접수 완료 — 처리 대기 중"),
    "PROCESSING":           ("RUN",  WARN,       "허브에서 검증 중"),
    "PENDING":              ("RUN",  WARN,       "위반 판정 완료 — 지역 서버 전송 준비"),
    "RETRYING":             ("RUN",  WARN,       "전송 실패 — 잠시 후 재시도"),
    "ROUTED":               ("SENT", ACCENT,     "지역 서버 전달 완료 — 심사 진행 중"),
    "DISCARDED":            ("SKIP", TEXT_MUTED, "단속 대상 아님 — 전송하지 않고 폐기"),
    "DLQ_FAILED":           ("FAIL", ERROR,      "전송 실패 — 재시도 모두 소진, 수동 확인 필요"),
    "ERROR":                ("FAIL", ERROR,      "처리 중 오류 발생"),
    "APPEALED":             ("APL",  ACCENT,     "이의제기 접수 — 지역 서버 재심의 요청됨"),
    "COMPLETED_CONFIRMED":  ("OK",   OK,         "심의 완료 — 위반 확정"),
    "COMPLETED_REJECTED":   ("OK",   TEXT_MUTED, "심의 완료 — 위반 아님"),
    "COMPLETED_PENDING":    ("HOLD", WARN,       "심의 보류 — 지역 서버에서 판단 유보"),
    "COMPLETED_CANCELED":   ("CNCL", TEXT_MUTED, "취소됨"),
}


DASHBOARD_STYLE = f"""
QMainWindow, QDialog {{
    background-color: {BG_WINDOW};
}}
QWidget {{
    color: {TEXT};
    font-family: "Segoe UI", "Pretendard", "Malgun Gothic", sans-serif;
    font-size: 12px;
}}

/* 상단 헤더 */
#HeaderFrame {{
    background-color: {ACCENT};
    border-bottom: 2px solid #0096c7;
}}
#HeaderTitle {{
    color: {ACCENT_DARK};
    font-size: 18px;
    font-weight: bold;
}}
#HeaderSub {{
    color: #023e8a;
    font-size: 11px;
}}

/* 카드 / 박스 */
QGroupBox {{
    font-weight: bold;
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 15px;
    background-color: {BG_PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 5px;
    color: {ACCENT_LIGHT};
}}

/* 입력 */
QLineEdit, QTextEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 8px;
    color: {TEXT_STRONG};
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}

/* 버튼 */
QPushButton {{
    background-color: {BORDER};
    border: 1px solid #3a4050;
    border-radius: 4px;
    padding: 5px 12px;
    font-weight: bold;
    color: {TEXT_STRONG};
}}
QPushButton:hover {{
    background-color: #3a4050;
    border-color: {ACCENT_LIGHT};
}}
QPushButton#AccentBtn {{
    background-color: {ACCENT};
    color: {ACCENT_DARK};
    border: none;
}}
QPushButton#AccentBtn:hover {{
    background-color: {ACCENT_LIGHT};
}}

/* 그룹박스 우측 상단 '+' 확대 버튼 */
QPushButton#PlusBadgeBtn {{
    background-color: transparent;
    border: none;
    color: {ACCENT_LIGHT};
    font-size: 16px;
    font-weight: bold;
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
    padding: 0px;
    margin: 0px;
}}
QPushButton#PlusBadgeBtn:hover {{
    color: {ACCENT};
}}

/* 표 */
QTableWidget {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    gridline-color: {BG_PANEL};
    outline: none;
    alternate-background-color: {BG_WINDOW};
}}
QTableWidget::item {{
    padding: 4px;
}}
QHeaderView::section {{
    background-color: {BG_WINDOW};
    color: {TEXT_INFO};
    padding: 6px;
    font-weight: bold;
    border: none;
    border-bottom: 2px solid {ACCENT};
}}
"""

# 정보 라벨(모노스페이스 박스) 공통 스타일
INFO_LABEL_STYLE = (
    f"background-color: {BG_INPUT}; border-radius: 4px; padding: 5px; "
    "font-family: monospace; font-size: 11px;"
)
