"""
ui/monitor.py
-------------
실시간 모니터 본 화면.

화면 구성
  상단  헤더(제목 + KST 시계)
  좌측  주소 설정 / 시스템 상태 / 최근 오류
  우측  지역 서버 카드 / 최근 신고 이력 표
  각 박스 우측 상단 '+' 버튼으로 확대 창을 연다.

이 파일은 **조립과 데이터 분배만** 담당한다. 실제 위젯과 갱신 로직은 나뉘어 있다.
  theme.py         색/스타일/표시 상수
  widgets.py       재사용 위젯(게이지, 카드, 표 셀)
  panels.py        본 화면 패널 5종
  dialogs.py       '+' 확대 창 3종
  worker.py        백그라운드 데이터 수집 스레드
  region_utils.py  지역 표시 순서/번호 규칙
"""

import sys

from PySide6.QtCore import QDateTime, QMetaObject, Qt, QThread, QTimer, Slot
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from app.config import settings
from app.ui import theme
from app.ui.dialogs import ErrorDetailDialog, HistoryTableDialog, SystemStatusDialog
from app.ui.panels import (
    EndpointPanel,
    ErrorPanel,
    HistoryTablePanel,
    ServerGridPanel,
    SystemStatusPanel,
)
from app.ui.widgets import attach_corner_button
from app.ui.worker import MonitorFetchWorker

REFRESH_INTERVAL_MS = 2000   # 데이터 갱신 주기
CLOCK_INTERVAL_MS = 1000     # 시계 갱신 주기


class CentralHubMonitorUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{settings.PROJECT_NAME} - 실시간 모니터")
        self.resize(1350, 780)
        self.setStyleSheet(theme.DASHBOARD_STYLE)

        self._build_layout()
        self._build_dialogs()
        self._start_worker()
        self._start_timers()

        # 지역 목록을 읽어 카드까지 한 번에 구성한다.
        self.panel_endpoint.load_config(show_error=False)
        self.refresh()

    # ------------------------------------------------------------------
    #  화면 구성
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.setSpacing(12)

        self.panel_endpoint = EndpointPanel()
        self.panel_status = SystemStatusPanel()
        self.panel_error = ErrorPanel()
        self.panel_servers = ServerGridPanel()
        self.panel_history = HistoryTablePanel()

        # 지역 목록이 바뀌면 카드도 함께 다시 만든다.
        self.panel_endpoint.regions_changed.connect(self.panel_servers.rebuild)

        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(self.panel_endpoint)
        left.addWidget(self.panel_status)
        left.addWidget(self.panel_error, stretch=1)
        body_layout.addLayout(left, stretch=35)  # 좌측 35 : 우측 65 비율 배분

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self.panel_servers)
        right.addWidget(self.panel_history, stretch=1)
        body_layout.addLayout(right, stretch=65)

        root.addWidget(body)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("HeaderFrame")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 8, 16, 8)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(f"★ {settings.PROJECT_NAME.upper()} MONITOR")
        title.setObjectName("HeaderTitle")
        subtitle = QLabel("Central Hub & Regional Processing Node System")
        subtitle.setObjectName("HeaderSub")
        titles.addWidget(title)
        titles.addWidget(subtitle)

        layout.addLayout(titles)
        layout.addStretch(1)

        self.lbl_clock = QLabel()
        self.lbl_clock.setStyleSheet(
            f"color: {theme.ACCENT_DARK}; font-weight: bold; font-size: 13px;"
        )
        layout.addWidget(self.lbl_clock)
        return header

    def _build_dialogs(self) -> None:
        """
        확대 창은 처음에 한 번만 만들어 재사용한다.
        (열 때마다 새로 만들면 닫힌 창이 부모의 자식으로 계속 쌓인다)
        """
        self.dlg_status = SystemStatusDialog(self)
        self.dlg_error = ErrorDetailDialog(self)
        self.dlg_history = HistoryTableDialog(self)

        attach_corner_button(self.panel_status, "시스템 상태 확대", self._open_status_dialog)
        attach_corner_button(self.panel_error, "오류 로그 확대", self._open_error_dialog)
        attach_corner_button(self.panel_history, "신고 이력 확대", self._open_history_dialog)

    # ------------------------------------------------------------------
    #  백그라운드 수집
    # ------------------------------------------------------------------
    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = MonitorFetchWorker()
        self._worker.moveToThread(self._thread)  # 워커 객체를 별도 스레드로 이동
        self._worker.data_fetched.connect(self._on_data_fetched)
        self._worker.error_occurred.connect(self._on_fetch_error)
        self._thread.start()

    def _start_timers(self) -> None:
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_INTERVAL_MS)

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(CLOCK_INTERVAL_MS)
        self._update_clock()

    def refresh(self) -> None:
        """워커 스레드에 수집을 요청한다(비동기)."""
        QMetaObject.invokeMethod(self._worker, "fetch", Qt.QueuedConnection)  # 다른 스레드의 슬롯을 안전하게 호출

    def _update_clock(self) -> None:
        now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
        self.lbl_clock.setText(f"KST {now}")

    # ------------------------------------------------------------------
    #  수집 결과 반영
    # ------------------------------------------------------------------
    @Slot(dict)
    def _on_data_fetched(self, data: dict) -> None:
        try:
            self.panel_status.update_data(data["stats"], data["disk_mb"], data["yolo_state"])
            self.panel_error.update_data(data["logs"])
            self.panel_servers.update_status(data["circuit_snap"])
            self.panel_history.update_rows(data["logs"], self.panel_endpoint.number_map)

            # 열려 있는 확대 창도 함께 갱신한다.
            if self.dlg_status.isVisible():
                self._sync_status_dialog()
            if self.dlg_error.isVisible():
                self._sync_error_dialog()
            if self.dlg_history.isVisible():
                self.dlg_history.copy_from(self.panel_history.table)
        except Exception as exc:
            self.statusBar().showMessage(f"표시 처리 오류: {exc}", 3000)

    @Slot(str)
    def _on_fetch_error(self, message: str) -> None:
        self.statusBar().showMessage(f"갱신 오류: {message}", 3000)

    # ------------------------------------------------------------------
    #  확대 창
    # ------------------------------------------------------------------
    def _sync_status_dialog(self) -> None:
        self.dlg_status.update_info(
            self.panel_status.queue_ratio,
            self.panel_status.disk_ratio,
            self.panel_status.queue_text(),
            self.panel_status.verify_text(),
        )

    def _sync_error_dialog(self) -> None:
        self.dlg_error.update_info(self.panel_error.info_text(), self.panel_error.log_text())

    def _open_status_dialog(self) -> None:
        self._sync_status_dialog()  # 열기 전 최신 값으로 동기화
        self.dlg_status.show()
        self.dlg_status.raise_()

    def _open_error_dialog(self) -> None:
        self._sync_error_dialog()
        self.dlg_error.show()
        self.dlg_error.raise_()

    def _open_history_dialog(self) -> None:
        self.dlg_history.copy_from(self.panel_history.table)
        self.dlg_history.show()
        self.dlg_history.raise_()

    # ------------------------------------------------------------------
    #  종료
    # ------------------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802
        self.timer.stop()
        self.clock_timer.stop()
        for dialog in (self.dlg_status, self.dlg_error, self.dlg_history):
            dialog.close()
        self._thread.quit()
        # 수집이 진행 중일 수 있으므로 잠시 기다렸다가 종료한다.
        self._thread.wait(3000)
        super().closeEvent(event)


def run_ui_standalone() -> int:
    """UI만 단독 실행(서버 없이 DB 내용만 확인)."""
    qt_app = QApplication(sys.argv)
    window = CentralHubMonitorUI()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(run_ui_standalone())
