"""Small shared widgets in the style of the LabVIEW front panel (gui_v2.vi)."""

from __future__ import annotations

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

GREEN = "#2ecc40"
RED = "#e74c3c"
AMBER = "#f39c12"
BLUE = "#3498db"
OFF = "#555555"

LED_GREEN_OFF = "#1f5d1f"  # LabVIEW's dark-green "off" LED

# Light-gray LabVIEW panel look. Applied to the main window so dialogs keep the system style.
PANEL_STYLE = """
QMainWindow, QWidget#panel { background: #e6e6e6; }
QLabel#indicator {
    background: #d0d0d0; border: 2px inset #a0a0a0; border-radius: 3px;
    padding: 2px 6px; color: #111; font-family: Consolas; font-size: 11pt;
}
QLabel#section { font-size: 13pt; font-weight: bold; color: #111; }
QFrame#rule { background: #707070; max-height: 3px; min-height: 3px; }
QFrame#card { background: white; border: 1px solid #a0a0a0; }
QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox { background: white; }
QPushButton { padding: 4px 10px; }
"""


class Led(QLabel):
    """Round indicator with a text label, like a LabVIEW boolean indicator."""

    def __init__(
        self, text: str = "", on_color: str = GREEN, parent: QWidget | None = None, *, off_color: str = OFF
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._on_color = on_color
        self._off_color = off_color
        self._on = False
        self.setTextFormat(Qt.RichText)
        self.set_on(False)

    @property
    def is_on(self) -> bool:
        return self._on

    def set_text(self, text: str) -> None:
        self._text = text

    def set_on(self, on: bool, color: str | None = None) -> None:
        self._on = bool(on)
        self._color = color
        c = (color or self._on_color) if on else self._off_color
        self.setText(f'<span style="color:{c}; font-size:18px;">&#9679;</span> {self._text}')


def indicator(text: str = "—", min_width: int = 90) -> QLabel:
    """Gray, sunken, read-only numeric display (LabVIEW numeric indicator)."""
    lab = QLabel(text)
    lab.setObjectName("indicator")
    lab.setMinimumWidth(min_width)
    lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return lab


def section(title: str, *, extra: QWidget | None = None) -> QWidget:
    """Section heading with a dark rule above it, as on the LabVIEW panel."""
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 6, 0, 2)
    v.setSpacing(2)
    rule = QFrame()
    rule.setObjectName("rule")
    v.addWidget(rule)
    h = QHBoxLayout()
    lab = QLabel(title)
    lab.setObjectName("section")
    h.addWidget(lab)
    if extra is not None:
        h.addWidget(extra)
    h.addStretch(1)
    v.addLayout(h)
    return w


class VerticalGauge(QWidget):
    """Vertical slide indicator with a numeric scale (LabVIEW "vertical fill slide")."""

    def __init__(self, lo: float, hi: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lo, self.hi = float(lo), float(hi)
        self.value: float | None = None
        self.setMinimumSize(70, 200)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def set_range(self, lo: float, hi: float) -> None:
        self.lo, self.hi = float(lo), float(hi)
        self.update()

    def set_value(self, v: float | None) -> None:
        self.value = v
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        top, bottom = 12, h - 12
        track = QRectF(w - 26, top - 4, 14, bottom - top + 8)
        p.setPen(QPen(QColor("#909090"), 1))
        p.setBrush(QColor("#c8c8c8"))
        p.drawRoundedRect(QRectF(w - 32, 2, 26, h - 4), 4, 4)
        p.setBrush(QColor("#707070"))
        p.drawRoundedRect(track, 3, 3)

        span = self.hi - self.lo or 1.0

        def y_of(v: float) -> float:
            return bottom - (v - self.lo) / span * (bottom - top)

        # scale: 5 major ticks
        f = QFont()
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QColor("#111"))
        for i in range(6):
            v = self.lo + span * i / 5
            y = y_of(v)
            p.drawLine(int(w - 38), int(y), int(w - 33), int(y))
            p.drawText(QRectF(0, y - 8, w - 40, 16), Qt.AlignRight | Qt.AlignVCenter, f"{v:g}")

        if self.value is not None:
            v = min(max(self.value, self.lo), self.hi)
            y = y_of(v)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e8a400"))
            p.drawRect(QRectF(track.left() + 2, y, track.width() - 4, bottom + 2 - y))
            p.setBrush(QColor("#f5f5f5"))
            p.setPen(QPen(QColor("#606060"), 1))
            p.drawRect(QRectF(track.left() - 2, y - 4, track.width() + 4, 8))
        p.end()


def spin(
    lo: float, hi: float, value: float = 0.0, *, decimals: int = 2, step: float = 1.0, suffix: str = "", width: int = 0
) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setKeyboardTracking(False)
    if suffix:
        s.setSuffix(suffix)
    if width:
        s.setMinimumWidth(width)
    return s


def fmt(x: float | None, digits: int = 2, unit: str = "") -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}{(' ' + unit) if unit else ''}"
