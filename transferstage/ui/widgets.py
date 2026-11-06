"""Small shared widgets: LED indicator and numeric spin-box factory."""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDoubleSpinBox, QLabel, QWidget

GREEN = "#2ecc40"
RED = "#e74c3c"
AMBER = "#f39c12"
BLUE = "#3498db"
OFF = "#555555"


class Led(QLabel):
    """Round indicator with a text label, like a LabVIEW boolean indicator."""

    def __init__(self, text: str = "", on_color: str = GREEN, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._on_color = on_color
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
        c = (color or self._on_color) if on else OFF
        self.setText(f'<span style="color:{c}; font-size:16px;">&#9679;</span> {self._text}')


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
