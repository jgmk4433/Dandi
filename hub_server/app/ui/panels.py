"""
ui/panels.py
------------
본 화면을 구성하는 네 개의 패널.

각 패널은 QGroupBox 를 상속하고 자기 영역의 위젯 생성과 갱신을 스스로 책임진다.
monitor.py 는 이 패널들을 배치하고 데이터를 나눠주기만 한다.
패널 하나를 고칠 때 다른 패널을 건드릴 필요가 없도록 분리했다.

  EndpointPanel      서버 주소 설정
  SystemStatusPanel  게이지 + 큐/검증 요약
  ErrorPanel         최근 오류
  ServerGridPanel    지역 서버 상태 카드
  HistoryTablePanel  최근 신고 이력 표
"""

from typing import Dict, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
)

from app.config import settings
from app.core.ngrok_sync import fetch_current_ngrok_url
from app.core.service_registry import registry
from app.ui import theme
from app.ui.region_utils import label as region_label, order_regions
from app.ui.widgets import ServerStatusCard, SemiCircleGauge, set_badge_cell, set_text_cell


# =============================================================================
#  주소 설정
# =============================================================================
class EndpointPanel(QGroupBox):
    """
    endpoints.ini 의 주소를 읽고 수정한다.
    지역 목록은 ini 에서 읽으므로 지역을 추가해도 코드 수정이 필요 없다.
    """

    regions_changed = Signal(list, dict)  # (정렬된 지역 코드, 번호 매핑)

    def __init__(self, parent=None):
        super().__init__("서버 주소 설정", parent)
        self._inputs: Dict[str, QLineEdit] = {}  # 지역코드 -> 입력란 매핑
        self.number_map: Dict[str, str] = {}
        self.ordered_codes: List[str] = []

        outer = QVBoxLayout(self)
        self.form = QFormLayout()
        self.public_url_input = QLineEdit()
        self.form.addRow("중앙 서버:", self.public_url_input)
        outer.addLayout(self.form)

        buttons = QHBoxLayout()
        reload_btn = QPushButton("불러오기")
        reload_btn.clicked.connect(self.load_config)
        save_btn = QPushButton("주소 저장")
        save_btn.clicked.connect(self.save_config)
        ngrok_btn = QPushButton("ngrok 자동감지")
        ngrok_btn.setObjectName("AccentBtn")
        ngrok_btn.clicked.connect(self.detect_ngrok)
        for button in (reload_btn, save_btn, ngrok_btn):
            buttons.addWidget(button)
        outer.addLayout(buttons)

    def load_config(self, show_error: bool = True) -> None:
        """ini 를 읽어 입력란을 다시 만든다."""
        try:
            regions = registry.all_endpoints()
            public_url = registry.config_loader.get_central_public_url()
        except Exception as exc:
            if show_error:
                QMessageBox.warning(self, "설정 읽기 실패", str(exc))
            return

        self.public_url_input.setText(public_url)

        # 기존 지역 입력란 제거 (첫 행의 중앙 서버 입력란은 유지)
        for widget in list(self._inputs.values()):
            self.form.removeRow(widget)
        self._inputs.clear()

        self.ordered_codes = order_regions(regions.keys())
        # 번호는 화면에서 매기지 않고 endpoints.ini 의 [REGION_NUMBERS] 를 그대로 쓴다.
        # 사건번호(PM02A1BC)와 화면 표시가 어긋나지 않게 하기 위함이다.
        self.number_map = registry.region_numbers()

        for code in self.ordered_codes:
            line = QLineEdit(regions[code])
            self.form.addRow(f"{region_label(code, self.number_map)}:", line)
            self._inputs[code] = line

        self.regions_changed.emit(self.ordered_codes, self.number_map)  # 지역 목록 변경을 다른 패널에 알림

    def save_config(self) -> None:
        try:
            registry.config_loader.set_regional_endpoints(
                {code: widget.text() for code, widget in self._inputs.items()}
            )
            registry.config_loader.set_central_public_url(self.public_url_input.text())
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))
            return
        QMessageBox.information(self, "저장 완료", "주소가 저장되었습니다. 다음 요청부터 반영됩니다.")
        self.load_config()  # 저장 직후 최신 값으로 화면 갱신

    def detect_ngrok(self) -> None:
        url = fetch_current_ngrok_url()  # 로컬 ngrok API 에서 현재 터널 주소 조회
        if url:
            self.public_url_input.setText(url)
            QMessageBox.information(self, "감지 완료", f"ngrok 주소:\n{url}\n\n'주소 저장'을 눌러 반영하세요.")
        else:
            QMessageBox.warning(self, "감지 실패", "실행 중인 ngrok 터널을 찾지 못했습니다.")


# =============================================================================
#  시스템 상태
# =============================================================================
class SystemStatusPanel(QGroupBox):
    """작업 부하율 / 디스크 사용량 게이지와 요약 텍스트."""

    def __init__(self, parent=None):
        super().__init__("시스템 상태", parent)
        layout = QVBoxLayout(self)

        gauges = QHBoxLayout()
        self.gauge_queue = SemiCircleGauge("작업 부하율", theme.OK, theme.ACCENT)
        self.gauge_disk = SemiCircleGauge("디스크 사용량", theme.WARN, theme.ERROR)
        gauges.addWidget(self.gauge_queue)
        gauges.addWidget(self.gauge_disk)
        layout.addLayout(gauges)

        self.lbl_queue = QLabel("큐 대기 0 / 처리중 0 / 완료 0 / 실패 0")
        self.lbl_verify = QLabel("검증: -\n이미지 보관: - MB")
        for label in (self.lbl_queue, self.lbl_verify):
            label.setWordWrap(True)
            label.setStyleSheet(theme.INFO_LABEL_STYLE)
            layout.addWidget(label)

        self.queue_ratio = 0.0
        self.disk_ratio = 0.0

    def update_data(self, stats: dict, disk_mb: float, yolo_state: str) -> None:
        queued = stats.get("QUEUED", 0)
        running = stats.get("RUNNING", 0)
        done = stats.get("DONE", 0)
        failed = stats.get("FAILED", 0)

        # [수정] 기존에는 running / 전체누적건수 로 계산해서
        #        완료 건수가 쌓일수록 부하율이 0 에 수렴했다.
        #        지금 처리 중이거나 대기 중인 건을 워커 수용량과 비교한다.
        capacity = max(1, settings.WORKER_COUNT)
        self.queue_ratio = min(100.0, (running + queued) / capacity * 100.0)
        self.gauge_queue.set_value(self.queue_ratio)
        self.lbl_queue.setText(
            f"큐 대기 {queued} / 처리중 {running} / 완료 {done} / 실패 {failed}\n"
            f"동시 처리 한도 {capacity}건"
        )

        self.disk_ratio = min(100.0, disk_mb / theme.DISK_GAUGE_MAX_MB * 100.0)  # 디스크 게이지 기준 용량 대비 비율
        self.gauge_disk.set_value(self.disk_ratio)
        self.lbl_verify.setText(
            f"검증: {yolo_state}\n이미지 보관: {disk_mb} MB / {theme.DISK_GAUGE_MAX_MB} MB"
        )

    def queue_text(self) -> str:
        return self.lbl_queue.text()

    def verify_text(self) -> str:
        return self.lbl_verify.text()


# =============================================================================
#  최근 오류
# =============================================================================
class ErrorPanel(QGroupBox):
    """가장 최근에 오류가 기록된 건을 보여준다."""

    def __init__(self, parent=None):
        super().__init__("최근 오류 발생 상세 로그", parent)
        from PySide6.QtWidgets import QTextEdit

        layout = QVBoxLayout(self)

        self.lbl_info = QLabel("발생 위치: - | 신고 No: -")
        self.lbl_info.setStyleSheet(f"color: {theme.ERROR}; font-weight: bold;")
        layout.addWidget(self.lbl_info)

        self.txt_error = QTextEdit()
        self.txt_error.setReadOnly(True)
        self.txt_error.setPlaceholderText("최근 발생한 오류 메시지가 여기에 표시됩니다.")
        self.txt_error.setStyleSheet(
            f"color: {theme.ERROR_TEXT}; background-color: {theme.BG_INPUT}; "
            f"border: 1px solid {theme.BORDER};"
        )
        layout.addWidget(self.txt_error)

    def update_data(self, logs: List[dict]) -> None:
        latest = next((row for row in logs if row["error_message"] or row["error_origin"]), None)  # 오류가 있는 가장 최근 건 탐색
        if latest:
            origin = latest["error_origin"] or "UNKNOWN"
            self.lbl_info.setText(f"발생 위치: {origin} | 신고 No: {latest['event_no']}")
            self.txt_error.setText(latest["error_message"] or "상세 내용 없음")
        else:
            self.lbl_info.setText("발생 위치: 정상 | 신고 No: -")
            self.txt_error.setText("최근 발생한 오류가 없습니다.")

    def info_text(self) -> str:
        return self.lbl_info.text()

    def log_text(self) -> str:
        return self.txt_error.toPlainText()


# =============================================================================
#  지역 서버 카드
# =============================================================================
class ServerGridPanel(QGroupBox):
    """지역 서버별 연결 상태(서킷 브레이커 기준)를 카드로 표시한다."""

    COLUMNS_PER_ROW = 3

    def __init__(self, parent=None):
        super().__init__("지역 서버 상태", parent)
        self.grid = QGridLayout(self)
        self.grid.setSpacing(8)
        self._cards: Dict[str, ServerStatusCard] = {}

    def rebuild(self, ordered_codes: List[str], number_map: Dict[str, str]) -> None:
        """지역 목록이 바뀌면 카드를 다시 만든다."""
        for card in self._cards.values():
            self.grid.removeWidget(card)
            card.deleteLater()  # 기존 카드 위젯 정리
        self._cards.clear()

        for index, code in enumerate(ordered_codes):
            card = ServerStatusCard(region_label(code, number_map))
            row, column = divmod(index, self.COLUMNS_PER_ROW)  # 3열 그리드로 배치
            self.grid.addWidget(card, row, column)
            self._cards[code] = card

    def update_status(self, circuit_snap: Dict[str, str]) -> None:
        """
        [수정] 기존에는 state is None(연결 안 됨/상태 미수집)도 'OK'와 같이 취급하여
              서버 연결이 안 된 상황에서도 '정상'으로 표기되는 문제가 있었다.
              이제 state가 None일 경우 '연결 없음' 에러 상태로 처리한다.
        """
        for code, card in self._cards.items():
            state = circuit_snap.get(code)
            if state == "OK":
                card.set_state("ok", "정상")
            elif state is None:
                card.set_state("error", "연결 없음")
            elif state.startswith("OPEN"):
                card.set_state("error", f"차단 {state[4:].strip()}")  # OPEN 이후 남은 시간 등 표시
            else:  # UNSTABLE (n)
                card.set_state("warn", f"불안정 {state[9:].strip()}")


# =============================================================================
#  신고 이력 표
# =============================================================================
class HistoryTablePanel(QGroupBox):
    """
    최근 신고 이력.
    내용이 바뀌었을 때만 다시 그려 스크롤과 선택을 유지한다.
    """

    # 컬럼 인덱스 (theme.COLUMNS 순서와 일치해야 함)
    COL_ID, COL_NO, COL_REGION, COL_YOLO, COL_STATUS, COL_TIME = range(6)

    def __init__(self, parent=None):
        super().__init__("최근 신고 이력", parent)
        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(len(theme.COLUMNS))
        self.table.setHorizontalHeaderLabels(theme.COLUMNS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)  # 표 직접 수정 금지(조회 전용)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)

        header = self.table.horizontalHeader()
        for column in range(len(theme.COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        header.setSectionResizeMode(self.COL_TIME, QHeaderView.Stretch)  # 마지막 열(시간)은 남는 공간 채움

        # 실제 데이터 폭에 맞춘 기본값.
        #   ID   : 000001   (6자, 접수 순번)
        #   No   : PM02A1BC (8자, PM + 지역번호 + 무작위 4자리)
        # 열 경계를 끌어 조절할 수 있고, 잘린 값은 마우스를 올리면 툴팁으로 보인다.
        for column, width in enumerate((70, 105, 60, 55, 70)):
            self.table.setColumnWidth(column, width)

        layout.addWidget(self.table)
        self._signature = None  # 직전 렌더링 데이터의 서명(변경 감지용)

    def update_rows(self, logs: List[dict], number_map: Dict[str, str]) -> None:
        signature = tuple((row["trace_id"], row["status"], row["updated_at"]) for row in logs)
        if signature == self._signature:
            return  # 데이터가 그대로면 다시 그리지 않음(스크롤/선택 유지)
        self._signature = signature

        scroll = self.table.verticalScrollBar().value()  # 갱신 전 스크롤 위치 기억

        # setRowCount(0) 이 셀 위젯까지 정리해 준다.
        # (직접 deleteLater 를 부르면 표가 다시 지울 때 이중 해제 위험이 있다)
        self.table.setRowCount(0)
        self.table.setRowCount(len(logs))

        for index, row in enumerate(logs):
            set_text_cell(self.table, index, self.COL_ID, row["trace_id"], tooltip=row["trace_id"])
            set_text_cell(self.table, index, self.COL_NO, row["event_no"], tooltip=row["event_no"])

            code = row["region_code"]
            set_text_cell(self.table, index, self.COL_REGION,
                          number_map.get(code, code), tooltip=code)

            self._set_yolo_badge(index, row["yolo_result"])
            self._set_status_badge(index, row["status"])

            set_text_cell(self.table, index, self.COL_TIME, row["created_at"])

        self.table.verticalScrollBar().setValue(scroll)  # 갱신 후 스크롤 위치 복원

    def _set_yolo_badge(self, row: int, value: str) -> None:
        """
        YOLO 판정 배지.
        표에는 한 글자만 넣고, 뜻은 마우스를 올렸을 때 한글 설명으로 보여준다.
        (문자와 설명은 theme.YOLO_BADGES 에서 관리)
        """
        text, color, description = theme.YOLO_BADGES.get(
            value, ("-", theme.BORDER, f"알 수 없는 값: {value}")
        )
        self._put_badge(row, self.COL_YOLO, text, color, value, description)

    def _set_status_badge(self, row: int, status: str) -> None:
        """처리 상태 배지. 설명은 theme.STATUS_BADGES 에서 관리."""
        text, color, description = theme.STATUS_BADGES.get(
            status, (status[:4], theme.BORDER, f"알 수 없는 상태: {status}")
        )
        self._put_badge(row, self.COL_STATUS, text, color, status, description)

    def _put_badge(self, row: int, column: int, text: str, color: str,
                   raw_value: str, description: str) -> None:
        """배지 셀 하나를 그린다. 툴팁은 '한글 설명 (원본값)' 형태."""
        # 어두운 배경(회색 계열)에는 밝은 글자, 밝은 배경에는 어두운 글자를 쓴다.
        foreground = theme.TEXT if color in (theme.BORDER, theme.TEXT_MUTED) else theme.BG_WINDOW
        set_badge_cell(
            self.table, row, column, text, color, foreground,
            tooltip=f"{text} — {description}\n(원본값: {raw_value})",
        )
