"""Log tab: live application log, alarm history, and a link to the data-log folder."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..applog import LOG_FORMAT
from ..control.safety import Alarm

MAX_LOG_LINES = 5000


class _Bridge(QObject):
    record = pyqtSignal(int, str)


class QtLogHandler(logging.Handler):
    """Logging handler that forwards formatted records to the GUI thread via a queued signal."""

    def __init__(self) -> None:
        super().__init__()
        self.bridge = _Bridge()
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.record.emit(record.levelno, self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)


class LogTab(QWidget):
    def __init__(self, log_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log_dir = Path(log_dir)
        self.alarm_count = 0

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Level:"))
        self.level_combo = QComboBox()
        for name in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.level_combo.addItem(name, getattr(logging, name))
        self.level_combo.setCurrentText("INFO")
        bar.addWidget(self.level_combo)
        clear = QPushButton("Clear")
        bar.addWidget(clear)
        bar.addStretch(1)
        self.open_btn = QPushButton("Open data-log folder")
        self.open_btn.clicked.connect(self.open_folder)
        bar.addWidget(self.open_btn)
        root.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(MAX_LOG_LINES)
        self.text.setFont(QFont("Consolas", 9))
        clear.clicked.connect(self.text.clear)
        split.addWidget(self.text)

        alarms = QWidget()
        av = QVBoxLayout(alarms)
        av.setContentsMargins(0, 0, 0, 0)
        av.addWidget(QLabel("Alarm history"))
        self.alarm_table = QTableWidget(0, 4)
        self.alarm_table.setHorizontalHeaderLabels(["Time", "Severity", "Code", "Message"])
        self.alarm_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.alarm_table.setEditTriggers(QTableWidget.NoEditTriggers)
        av.addWidget(self.alarm_table)
        split.addWidget(alarms)
        split.setSizes([500, 200])
        root.addWidget(split, 1)

        self.handler = QtLogHandler()
        self.handler.bridge.record.connect(self.append)
        logging.getLogger().addHandler(self.handler)

    def detach(self) -> None:
        logging.getLogger().removeHandler(self.handler)

    def append(self, level: int, line: str) -> None:
        if level < self.level_combo.currentData():
            return
        if level >= logging.ERROR:
            line = f'<span style="color:#e74c3c;">{_esc(line)}</span>'
        elif level >= logging.WARNING:
            line = f'<span style="color:#f39c12;">{_esc(line)}</span>'
        else:
            line = _esc(line)
        self.text.appendHtml(line)

    def add_alarm(self, alarm: Alarm) -> None:
        r = self.alarm_table.rowCount()
        self.alarm_table.insertRow(r)
        for col, val in enumerate(
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), alarm.severity.value, alarm.code.value, alarm.message)
        ):
            self.alarm_table.setItem(r, col, QTableWidgetItem(val))
        self.alarm_table.scrollToBottom()
        self.alarm_count += 1

    def open_folder(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_dir)))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
