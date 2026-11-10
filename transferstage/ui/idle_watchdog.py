"""Idle-heater watchdog: switch the heater off if nobody is at the PC.

While the heater (PSU output) is on, the watchdog measures how long nobody has
used the PC. Any keyboard or mouse input counts, in any program (Windows
``GetLastInputInfo``), so working in the camera software keeps it quiet. TourBox
buttons send keystrokes and count too. Input inside this app is also tracked
directly, which is the fallback if the system call is unavailable.

* idle >= ``timeout_s`` -> :class:`IdleAlarmDialog` pops up on top of every
  window, beeps and counts down ``grace_s``.
* "I'm here" in the dialog -> back to normal; the idle time restarts.
  Other input during the countdown does not cancel it: someone has to answer.
* countdown reaches 0 -> heater off (``heater_off`` signal), the dialog stays up
  saying so. This is not a latched trip; the heater can be switched on again.
* heater switched off some other way, or watchdog disabled -> the dialog closes.

The idle time also restarts when the heater is switched on, so the first
alarm comes ``timeout_s`` after that at the earliest.
"""

from __future__ import annotations

import ctypes
import enum
import logging
import sys
import time
from collections.abc import Callable

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

CHECK_MS = 1000
BEEP_EVERY_S = 5.0
RETRY_OFF_S = 5.0  # heater still reported on this long after the shut-off -> send the off command again

_INPUT_EVENTS = {
    QEvent.KeyPress,
    QEvent.MouseButtonPress,
    QEvent.MouseMove,
    QEvent.Wheel,
    QEvent.TouchBegin,
}


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def system_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input anywhere on this PC (None if unknown)."""
    if sys.platform != "win32":
        return None
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount()
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0  # 32-bit tick counts wrap every 49.7 days
    except Exception:  # noqa: BLE001
        return None


class WatchState(enum.Enum):
    OFF = "off"  # heater off or watchdog disabled
    WATCHING = "watching"
    WARNING = "warning"  # alarm up, counting down
    SHUT_OFF = "shut_off"  # heater was switched off by the watchdog


class IdleWatchdog(QObject):
    warning_started = pyqtSignal(float)  # grace seconds
    countdown = pyqtSignal(float)  # seconds left, every check while warning
    warning_cleared = pyqtSignal(str)  # reason
    heater_off = pyqtSignal()  # the watchdog switched the heater off

    def __init__(
        self,
        is_heater_on: Callable[[], bool],
        *,
        timeout_s: float,
        grace_s: float,
        system_idle: Callable[[], float | None] = system_idle_seconds,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._heater_on = is_heater_on
        self._system_idle = system_idle
        self._clock = clock
        self.timeout_s = float(timeout_s)
        self.grace_s = float(grace_s)
        self._enabled = True
        self.state = WatchState.OFF
        now = clock()
        self._last_app_input = now
        self._watch_since = now  # heater on / alarm answered
        self._deadline = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check)

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._timer.start(CHECK_MS)

    def stop(self) -> None:
        self._timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        if bool(on) == self._enabled:
            return
        self._enabled = bool(on)
        log.warning("idle watchdog %s", "enabled" if on else "DISABLED")
        if on:
            self._watch_since = self._clock()
        self._to_off("watchdog disabled" if not on else "watchdog enabled")
        self.check()

    def set_times(self, timeout_s: float, grace_s: float) -> None:
        self.timeout_s = float(timeout_s)
        self.grace_s = float(grace_s)

    def acknowledge(self) -> None:
        """Someone answered the alarm: restart the idle time."""
        self._watch_since = self._clock()
        if self.state is WatchState.WARNING:
            log.info("idle watchdog: alarm answered, heater stays on")
            self.state = WatchState.WATCHING
            self.warning_cleared.emit("answered")

    # ------------------------------------------------------------------ idle time

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802 - Qt API
        if ev.type() in _INPUT_EVENTS:
            self._last_app_input = self._clock()
        return False

    def idle_seconds(self) -> float:
        """Time since the last input (any program) or since the heater went on / the alarm was answered."""
        now = self._clock()
        idle = now - max(self._last_app_input, self._watch_since)
        sys_idle = self._system_idle()
        if sys_idle is not None:
            idle = min(idle, sys_idle)
        return max(idle, 0.0)

    # ------------------------------------------------------------------ loop

    def _to_off(self, reason: str) -> None:
        was_warning = self.state is WatchState.WARNING
        self.state = WatchState.OFF
        if was_warning:
            self.warning_cleared.emit(reason)

    def check(self) -> None:
        heater = bool(self._heater_on())
        if not self._enabled:
            return
        if self.state is WatchState.SHUT_OFF:
            if not heater:  # off confirmed; switching it on again starts a new watch
                self.state = WatchState.OFF
            elif self._clock() - self._deadline >= RETRY_OFF_S:  # still on: ask again
                self._deadline = self._clock()
                log.error("idle watchdog: heater still on after the shut-off, switching it off again")
                self.heater_off.emit()
            return
        if not heater:
            if self.state is not WatchState.OFF:
                self._to_off("heater off")
            return
        if self.state is WatchState.OFF:
            self._watch_since = self._clock()
            self.state = WatchState.WATCHING
            return
        if self.state is WatchState.WATCHING:
            idle = self.idle_seconds()
            if idle >= self.timeout_s:
                self.state = WatchState.WARNING
                self._deadline = self._clock() + self.grace_s
                log.warning("idle watchdog: heater on and no input for %.0f min, alarm raised", idle / 60)
                self.warning_started.emit(self.grace_s)
            return
        # WARNING
        left = self._deadline - self._clock()
        if left > 0:
            self.countdown.emit(left)
            return
        log.error("idle watchdog: alarm not answered within %.0f s, switching the heater off", self.grace_s)
        self.state = WatchState.SHUT_OFF
        self._deadline = self._clock()
        self.heater_off.emit()


class IdleAlarmDialog(QDialog):
    """Stays-on-top alarm with a countdown. Non-modal so the GUI keeps updating."""

    answered = pyqtSignal()  # "I'm here, keep heating"
    off_requested = pyqtSignal()  # "Turn heater off now"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Heater idle alarm")
        self.setWindowFlags(
            (self.windowFlags() | Qt.WindowStaysOnTopHint | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
            & ~Qt.WindowCloseButtonHint
            & ~Qt.WindowContextHelpButtonHint
        )
        self.setMinimumWidth(460)
        self.setStyleSheet("QDialog { background:#fff3cd; }")
        v = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setStyleSheet("font-size:14pt; font-weight:bold; color:#b00000;")
        self.title.setWordWrap(True)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("font-size:11pt;")
        self.clock_label = QLabel()
        self.clock_label.setAlignment(Qt.AlignCenter)
        self.clock_label.setStyleSheet("font-size:28pt; font-weight:bold; font-family:Consolas;")
        v.addWidget(self.title)
        v.addWidget(self.detail)
        v.addWidget(self.clock_label)
        h = QHBoxLayout()
        self.here_btn = QPushButton("I'm here, keep heating")
        self.here_btn.setMinimumHeight(40)
        self.here_btn.setStyleSheet("font-weight:bold; font-size:11pt;")
        self.here_btn.clicked.connect(self._on_here)
        self.off_btn = QPushButton("Turn heater off now")
        self.off_btn.setMinimumHeight(40)
        self.off_btn.clicked.connect(self._on_off)
        self.ok_btn = QPushButton("OK")
        self.ok_btn.setMinimumHeight(40)
        self.ok_btn.clicked.connect(self._on_here)
        h.addWidget(self.here_btn, 1)
        h.addWidget(self.off_btn)
        h.addWidget(self.ok_btn, 1)
        v.addLayout(h)
        self._last_beep = 0.0

    def show_warning(self, idle_min: float, grace_s: float) -> None:
        self.title.setText("Heater is ON and nobody has used the PC")
        self.detail.setText(
            f"No keyboard or mouse input for {idle_min:.0f} min. The heater will be switched off "
            "when the countdown ends unless someone answers."
        )
        self.here_btn.setVisible(True)
        self.off_btn.setVisible(True)
        self.ok_btn.setVisible(False)
        self._last_beep = 0.0
        self.set_remaining(grace_s)
        self.show()
        self.raise_()
        self.activateWindow()
        if self.parent() is not None:
            QApplication.alert(self.parent().window(), 0)  # flash the taskbar until the window is activated

    def set_remaining(self, seconds: float) -> None:
        s = max(0, int(seconds + 0.999))
        self.clock_label.setText(f"{s // 60}:{s % 60:02d}")
        now = time.monotonic()
        if now - self._last_beep >= BEEP_EVERY_S:
            self._last_beep = now
            QApplication.beep()

    def show_shut_off(self) -> None:
        self.title.setText("Heater switched OFF (nobody answered)")
        self.detail.setText(
            f"The idle alarm was not answered, so the PSU output was switched off at {time.strftime('%H:%M:%S')}."
        )
        self.clock_label.setText("OFF")
        self.here_btn.setVisible(False)
        self.off_btn.setVisible(False)
        self.ok_btn.setVisible(True)
        self.show()
        self.raise_()

    def _on_here(self) -> None:
        self.hide()
        self.answered.emit()

    def _on_off(self) -> None:
        self.hide()
        self.off_requested.emit()

    def reject(self) -> None:  # Esc does not dismiss the alarm
        pass
