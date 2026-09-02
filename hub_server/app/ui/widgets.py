"""
ui/widgets.py
-------------
재사용 위젯과 표 셀 헬퍼.

  SemiCircleGauge   : 반원형 게이지
  ServerStatusCard  : 지역 서버 상태 카드 (정상/불안정/차단 3단계)
  attach_corner_button : 그룹박스 우측 상단에 '+' 확대 버튼 부착
  set_text_cell / set_badge_cell : 표 셀 채우기
"""

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme


class SemiCircleGauge(QWidget):
    """0~100% 를 반원 호로 표시하는 게이지."""

    def __init__(self, title: str, start_color: str = theme.OK,
                 end_color: str = theme.ACCENT, parent=None):
        super().__init__(parent)
        self.title = title
        self.value = 0.0
        self.start_color = QColor(start_color)
        self.end_color = QColor(end_color)
        self.setMinimumSize(130, 110)

    def set_value(self, value: float) -> None:
        self.value = max(0.0, min(100.0, float(value)))  # 0~100 범위로 고정
        self.update()  # 다시 그리기 요청

    def paintEvent(self, event):  # noqa: N802 (Qt 규약)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width, height = self.width(), self.height()
        size = max(min(width - 20, (height - 35) * 2), 70)  # 위젯 크기에 맞춰 게이지 지름 계산
        rect = QRectF((width - size) / 2, 10, size, size)
        stroke = 9

        # 배경 호
        painter.setPen(QPen(QColor(theme.BORDER), stroke, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rect, 0, 180 * 16)

        # 값 호 (그라데이션)
        gradient = QConicalGradient(rect.center().x(), rect.center().y(), 180)
        gradient.setColorAt(0.0, self.start_color)
        gradient.setColorAt(0.5, self.end_color)
        painter.setPen(QPen(QBrush(gradient), stroke, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rect, 180 * 16, int(-180 * (self.value / 100.0) * 16))  # 값 비율만큼 호 그리기

        center_y = rect.top() + size / 2

        painter.setPen(QColor(theme.TEXT_STRONG))
        painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
        painter.drawText(QRectF(0, center_y - 20, width, 20), Qt.AlignCenter, f"{self.value:.1f}%")

        painter.setPen(QColor(theme.TEXT_INFO))
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(QRectF(0, center_y + 4, width, 18), Qt.AlignCenter, self.title)


class ServerStatusCard(QFrame):
    """
    지역 서버 1대의 상태 카드.
    서킷 브레이커 상태에 맞춰 정상/불안정/차단 3단계를 색으로 구분한다.
    """

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            ServerStatusCard {{
                background-color: {theme.BG_INPUT};
                border: 1px solid {theme.BORDER};
                border-radius: 6px;
            }}
            ServerStatusCard:hover {{ border-color: {theme.ACCENT}; }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        self.dot = QLabel("●")  # 상태를 색으로 표시하는 점
        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet(f"font-weight: bold; color: {theme.TEXT_STRONG};")
        self.lbl_sub = QLabel("대기 중")
        self.lbl_sub.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 10px;")

        info = QVBoxLayout()
        info.setSpacing(2)
        info.addWidget(self.lbl_name)
        info.addWidget(self.lbl_sub)

        layout.addWidget(self.dot)
        layout.addLayout(info)
        layout.addStretch(1)
        self.set_state("ok", "정상")  # 초기 상태

    def set_state(self, level: str, text: str) -> None:
        """level: ok / warn / error"""
        color = {"ok": theme.OK, "warn": theme.WARN, "error": theme.ERROR}.get(level, theme.OK)
        self.dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        self.lbl_sub.setText(text)


class _CornerButtonPlacer(QObject):
    """
    그룹박스 크기가 바뀔 때 '+' 버튼을 우측 상단에 다시 붙이는 이벤트 필터.

    [수정] 기존 코드는 box.resizeEvent 에 함수를 대입해 가로채려 했지만,
          PySide6 는 가상 함수를 클래스 단위로 해석하므로 인스턴스 속성 대입은
          호출되지 않을 수 있다. 이벤트 필터가 확실한 방법이다.
    """

    def __init__(self, box: QGroupBox, button: QPushButton):
        super().__init__(box)
        self.box = box
        self.button = button
        box.installEventFilter(self)  # 그룹박스의 이벤트를 가로채기 위해 필터 등록
        self._place()

    def _place(self) -> None:
        self.button.move(self.box.width() - 26, 6)  # 우측 상단에 버튼 위치시킴
        self.button.raise_()  # 다른 위젯 위로 보이게 함

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.box and event.type() == QEvent.Resize:
            self._place()  # 크기 변경 시 버튼 위치 재조정
        return False


def attach_corner_button(box: QGroupBox, tooltip: str, on_click: Callable) -> QPushButton:
    """그룹박스 우측 상단(제목 라인)에 '+' 확대 버튼을 붙인다."""
    button = QPushButton("+", box)
    button.setObjectName("PlusBadgeBtn")
    button.setToolTip(tooltip)
    button.setCursor(Qt.PointingHandCursor)
    button.clicked.connect(on_click)
    _CornerButtonPlacer(box, button)  # 크기 변화에 따른 위치 재배치 등록
    return button


def set_text_cell(table: QTableWidget, row: int, col: int, text: str,
                  tooltip: Optional[str] = None, color: Optional[str] = None) -> None:
    """일반 텍스트 셀. 열 폭보다 긴 값은 툴팁으로 전체를 볼 수 있게 한다."""
    item = QTableWidgetItem(str(text))
    item.setTextAlignment(Qt.AlignCenter)
    if tooltip:
        item.setToolTip(tooltip)
    if color:
        item.setForeground(QBrush(QColor(color)))
    table.setItem(row, col, item)


def set_badge_cell(table: QTableWidget, row: int, col: int, text: str,
                   bg_hex: str, fg_hex: str = theme.BG_WINDOW,
                   tooltip: Optional[str] = None) -> None:
    """색 배지 셀(YOLO/Status 요약 표시용)."""
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    if tooltip:
        label.setToolTip(tooltip)
    label.setStyleSheet(f"""
        QLabel {{
            background-color: {bg_hex};
            color: {fg_hex};
            border-radius: 3px;
            font-weight: bold;
            padding: 1px 4px;
        }}
    """)
    table.setCellWidget(row, col, label)  # 텍스트 대신 라벨 위젯을 셀에 삽입(배경색 적용 위함)
