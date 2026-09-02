"""
ui/dialogs.py
-------------
'+' 버튼으로 여는 확대 보기 창들.

각 창은 스스로 데이터를 읽지 않고, 부모 화면이 넘겨주는 값을 표시만 한다
(데이터 수집 경로를 한 곳으로 유지하기 위함).
"""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from app.ui import theme
from app.ui.widgets import SemiCircleGauge


class _BaseDialog(QDialog):
    """공통 스타일과 닫기 버튼을 가진 기본 창."""

    def __init__(self, title: str, width: int, height: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(width, height)
        self.setStyleSheet(theme.DASHBOARD_STYLE)
        self.layout_main = QVBoxLayout(self)
        self.layout_main.setSpacing(12)

    def add_close_button(self, extra_buttons=()) -> None:
        row = QHBoxLayout()
        for button in extra_buttons:
            row.addWidget(button)  # 닫기 버튼 앞에 추가 버튼(예: 복사) 배치
        row.addStretch(1)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.close)
        row.addWidget(close_btn)
        self.layout_main.addLayout(row)


class SystemStatusDialog(_BaseDialog):
    """시스템 상태 확대 보기."""

    def __init__(self, parent=None):
        super().__init__("시스템 상태 - 상세 모니터링", 700, 480, parent)

        gauges = QHBoxLayout()
        self.gauge_queue = SemiCircleGauge("작업 부하율", theme.OK, theme.ACCENT)
        self.gauge_disk = SemiCircleGauge("디스크 사용량", theme.WARN, theme.ERROR)
        for gauge in (self.gauge_queue, self.gauge_disk):
            gauge.setMinimumSize(220, 180)  # 확대 창이므로 본 화면보다 크게 표시
            gauges.addWidget(gauge)
        self.layout_main.addLayout(gauges)

        self.txt_detail = QTextEdit()
        self.txt_detail.setReadOnly(True)
        self.txt_detail.setFont(QFont("Consolas", 11))
        self.layout_main.addWidget(self.txt_detail)

        self.add_close_button()

    def update_info(self, queue_ratio: float, disk_ratio: float,
                    queue_text: str, verify_text: str) -> None:
        self.gauge_queue.set_value(queue_ratio)
        self.gauge_disk.set_value(disk_ratio)
        self.txt_detail.setText(
            f"=== 작업 큐 현황 ===\n{queue_text}\n\n=== 리소스 / 검증 모듈 ===\n{verify_text}"
        )


class ErrorDetailDialog(_BaseDialog):
    """최근 오류 확대 보기."""

    def __init__(self, parent=None):
        super().__init__("최근 발생 오류 - 상세 로그", 800, 550, parent)

        self.lbl_info = QLabel("발생 위치: - | 신고 No: -")
        self.lbl_info.setStyleSheet(
            f"color: {theme.ERROR}; font-weight: bold; font-size: 14px;"
        )
        self.layout_main.addWidget(self.lbl_info)

        self.txt_error = QTextEdit()
        self.txt_error.setReadOnly(True)
        self.txt_error.setFont(QFont("Consolas", 10))
        self.txt_error.setStyleSheet(
            f"color: {theme.ERROR_TEXT}; background-color: {theme.BG_INPUT}; "
            f"border: 1px solid {theme.ERROR};"
        )
        self.layout_main.addWidget(self.txt_error)

        copy_btn = QPushButton("로그 복사")
        copy_btn.clicked.connect(self._copy)
        self.add_close_button([copy_btn])

    def update_info(self, info_text: str, log_text: str) -> None:
        self.lbl_info.setText(info_text)
        self.txt_error.setText(log_text)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.txt_error.toPlainText())  # 오류 로그 전체를 클립보드로 복사
        QMessageBox.information(self, "알림", "오류 로그를 클립보드에 복사했습니다.")


class HistoryTableDialog(_BaseDialog):
    """신고 이력 확대 보기. 본 화면의 표 내용을 복사해 넓게 보여준다."""

    def __init__(self, parent=None):
        super().__init__("최근 신고 이력 - 전체 테이블", 1100, 650, parent)

        self.table = QTableWidget()
        self.table.setColumnCount(len(theme.COLUMNS))
        self.table.setHorizontalHeaderLabels(theme.COLUMNS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)

        header = self.table.horizontalHeader()
        for column in range(len(theme.COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        header.setSectionResizeMode(len(theme.COLUMNS) - 1, QHeaderView.Stretch)
        for column, width in enumerate((150, 210, 80, 70, 90)):
            self.table.setColumnWidth(column, width)  # 확대 창 기준 넓힌 열 폭

        self.layout_main.addWidget(self.table)
        self.add_close_button()

    def copy_from(self, source: QTableWidget) -> None:
        """본 화면 표의 내용을 그대로 옮겨 담는다(배지는 라벨째 복제)."""
        self.table.setRowCount(0)
        self.table.setRowCount(source.rowCount())

        for row in range(source.rowCount()):
            for column in range(source.columnCount()):
                item = source.item(row, column)
                if item is not None:
                    copied = QTableWidgetItem(item.text())
                    copied.setTextAlignment(Qt.AlignCenter)
                    copied.setToolTip(item.toolTip())
                    self.table.setItem(row, column, copied)
                    continue  # 일반 텍스트 셀 복제 후 다음 열로

                widget = source.cellWidget(row, column)
                if isinstance(widget, QLabel):
                    label = QLabel(widget.text())
                    label.setAlignment(Qt.AlignCenter)
                    label.setStyleSheet(widget.styleSheet())  # 배지 색상 스타일까지 그대로 복제
                    label.setToolTip(widget.toolTip())
                    self.table.setCellWidget(row, column, label)
