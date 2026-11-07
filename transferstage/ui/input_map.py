"""Application-wide keyboard shortcuts (the TourBox is configured to send keystrokes).

Actions and default keys (``keys:`` in config.yaml; they match the TourBox Console preset):

==============  ===================  =======================================================
action          TourBox control      default key
==============  ===================  =======================================================
safe_lock       Side (+ Top)         Ctrl+F1         toggle Safe Lock
cancel_motion   Top                  Ctrl+F6         stop Z and rotator immediately
speed_up        Knob clockwise       Ctrl+F4         Speed Knob +1 position (motion.speed_steps_um_s)
speed_down      Knob counter-cw      Ctrl+F5         Speed Knob −1 position
jog_up          Tall (hold)          Ctrl+F2         jog Z up while held
jog_down        Short (hold)         Ctrl+F3         jog Z down while held
mark            (unassigned)         Ctrl+F7         write an event mark to the session CSV
==============  ===================  =======================================================

These defaults match the "TransferStageControl" preset in TourBox Console
(the keys the LabVIEW program used). Ctrl+F keys do not collide with typing in
number fields. Knob direction (Ctrl+F4 vs Ctrl+F5) is inferred from the preset
layout; swap speed_up/speed_down if it turns out reversed.

Implementation: an event filter on the QApplication sees every key event while
any window of the app is active. A jog starts on the key press and stops on the
release of the same key; auto-repeat presses/releases are ignored. The release
is matched on the key alone, because the TourBox may release the modifiers
first.

Tap pairs: TourBox Console does not hold the key down while a button is held.
It sends one instant tap (press + release within a few ms) when the button goes
down and another tap when it comes up. (The LabVIEW program worked because a
key-navigation shortcut toggles a LabVIEW switch button.) So a release that comes
less than ``TAP_MS`` after its press does not stop the jog; the jog keeps going
until the next press of the same key (the button-up tap). A real held key
(keyboard) still stops on release. If the app loses focus during a jog, the jog
is stopped. Matched keys are
consumed so they do not also reach the focused widget. Nothing fires while a
key-sequence editor (Config tab) has focus.
"""

from __future__ import annotations

import logging
import time

from PyQt5.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt5.QtGui import QKeyEvent, QKeySequence
from PyQt5.QtWidgets import QApplication, QKeySequenceEdit

log = logging.getLogger(__name__)

ACTIONS = ("safe_lock", "cancel_motion", "speed_up", "speed_down", "jog_up", "jog_down", "mark")
JOG_ACTIONS = {"jog_up": 1, "jog_down": -1}
REPEATABLE = {"speed_up", "speed_down"}  # knob turns may arrive as auto-repeat
TAP_MS = 40.0  # a jog key released sooner than this was a TourBox tap, not a held key

_MOD_MASK = int(Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)


def parse_shortcut(text: str) -> int | None:
    """Return the combined key|modifiers int of a single-chord shortcut, or None if empty/invalid."""
    text = (text or "").strip()
    if not text:
        return None
    seq = QKeySequence(text, QKeySequence.PortableText)
    if seq.count() != 1 or seq[0] == 0 or seq[0] == Qt.Key_unknown:
        return None
    return int(seq[0])


def event_combo(ev: QKeyEvent) -> int:
    return int(ev.key()) | (int(ev.modifiers()) & _MOD_MASK)


class InputMap(QObject):
    action = pyqtSignal(str)  # safe_lock, cancel_motion, speed_up, speed_down, mark
    jog_start = pyqtSignal(int)
    jog_stop = pyqtSignal()

    def __init__(self, bindings: dict[str, str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._combos: dict[int, str] = {}
        self._jog_key: int | None = None
        self._jog_t0 = 0.0
        self._tap_jog = False  # jog started by a TourBox tap; the next tap of the same key stops it
        self._clock = time.monotonic
        self.set_bindings(bindings)

    # ------------------------------------------------------------------ bindings

    def set_bindings(self, bindings: dict[str, str]) -> None:
        combos: dict[int, str] = {}
        for action in ACTIONS:
            text = bindings.get(action, "")
            combo = parse_shortcut(text)
            if combo is None:
                if text:
                    log.warning("input map: cannot parse shortcut %r for %s", text, action)
                continue
            combos[combo] = action
        self._combos = combos
        log.info("input map: %s", {a: bindings.get(a, "") for a in ACTIONS})

    def action_for(self, ev: QKeyEvent) -> str | None:
        return self._combos.get(event_combo(ev))

    @property
    def jogging(self) -> bool:
        return self._jog_key is not None

    def install(self, app: QApplication | None = None) -> None:
        (app or QApplication.instance()).installEventFilter(self)

    def uninstall(self, app: QApplication | None = None) -> None:
        app = app or QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    # ------------------------------------------------------------------ events

    def _stop_jog(self) -> None:
        self._tap_jog = False
        if self._jog_key is not None:
            self._jog_key = None
            self.jog_stop.emit()

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802 - Qt API
        et = ev.type()
        if et == QEvent.ApplicationDeactivate:
            self._stop_jog()
            return False
        if et not in (QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride):
            return False
        if isinstance(QApplication.focusWidget(), QKeySequenceEdit):
            return False  # let the key-binding editor capture keys

        if et == QEvent.KeyRelease:
            if self._jog_key is not None and ev.key() == self._jog_key:
                if not ev.isAutoRepeat() and not self._tap_jog:
                    held_ms = (self._clock() - self._jog_t0) * 1000.0
                    name = QKeySequence(ev.key()).toString()
                    if held_ms < TAP_MS:
                        self._tap_jog = True  # TourBox button-down tap: keep jogging until the button-up tap
                        log.info("input map: jog key %s tapped (%.0f ms); jogging until the next tap", name, held_ms)
                    else:
                        log.info("input map: jog key %s released after %.0f ms", name, held_ms)
                        self._stop_jog()
                return True
            return self.action_for(ev) is not None

        action = self.action_for(ev)
        if action is None:
            return False
        if et == QEvent.ShortcutOverride:
            ev.accept()  # claim the key so no widget shortcut steals it; handled on KeyPress
            return True

        # KeyPress
        if ev.isAutoRepeat() and action not in REPEATABLE:
            return True
        if action in JOG_ACTIONS:
            if self._tap_jog and self._jog_key == ev.key():
                log.info("input map: jog key %s tapped again; stopping", QKeySequence(event_combo(ev)).toString())
                self._stop_jog()  # TourBox button-up tap; its release then falls through harmlessly
                return True
            if self._jog_key is not None and self._jog_key != ev.key():
                self._stop_jog()
            self._jog_key = ev.key()
            self._jog_t0 = self._clock()
            log.info("input map: jog key %s pressed (%s)", QKeySequence(event_combo(ev)).toString(), action)
            self.jog_start.emit(JOG_ACTIONS[action])
        else:
            self.action.emit(action)
        return True
