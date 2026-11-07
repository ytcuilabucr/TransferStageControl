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
==============  ===================  =======================================================

These defaults match the "TransferStageControl" preset in TourBox Console
(the keys the LabVIEW program used). Ctrl+F keys do not collide with typing in
number fields. Knob direction (Ctrl+F4 vs Ctrl+F5) is inferred from the preset
layout; swap speed_up/speed_down if it turns out reversed.

Implementation: an event filter on the QApplication sees every key event while
any window of the app is active. A jog starts on the key press and stops on the
release of the same key; auto-repeat presses/releases are ignored. The release
is matched on the key alone, because the TourBox may release the modifiers
first. If the app loses focus during a jog, the jog is stopped. Matched keys are
consumed so they do not also reach the focused widget. Nothing fires while a
key-sequence editor (Config tab) has focus.
"""

from __future__ import annotations

import logging

from PyQt5.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt5.QtGui import QKeyEvent, QKeySequence
from PyQt5.QtWidgets import QApplication, QKeySequenceEdit

log = logging.getLogger(__name__)

ACTIONS = ("safe_lock", "cancel_motion", "speed_up", "speed_down", "jog_up", "jog_down")
JOG_ACTIONS = {"jog_up": 1, "jog_down": -1}
REPEATABLE = {"speed_up", "speed_down"}  # knob turns may arrive as auto-repeat

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
    action = pyqtSignal(str)  # safe_lock, cancel_motion, speed_up, speed_down
    jog_start = pyqtSignal(int)
    jog_stop = pyqtSignal()

    def __init__(self, bindings: dict[str, str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._combos: dict[int, str] = {}
        self._jog_key: int | None = None
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
                if not ev.isAutoRepeat():
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
            if self._jog_key is not None and self._jog_key != ev.key():
                self._stop_jog()
            self._jog_key = ev.key()
            self.jog_start.emit(JOG_ACTIONS[action])
        else:
            self.action.emit(action)
        return True
