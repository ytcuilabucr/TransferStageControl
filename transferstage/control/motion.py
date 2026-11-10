"""Motion worker: owns the Z stage and the rotator, runs on its own QThread.

Usage (GUI side)::

    worker = MotionWorker(devices.z, devices.rotator, SafetyMonitor(cfg.limits))
    thread = start_worker(worker, "motion")       # see control/threads.py
    some_button_pressed.connect(lambda: worker_jog_start.emit(1))   # queued signal -> slot
    ...
    stop_worker(worker, thread)

Every command is a slot, so connect GUI signals to the slots and Qt queues the
call into the worker thread. Device exceptions are caught and reported via
:attr:`MotionWorker.error`; they never escape a slot (an exception escaping a
PyQt slot aborts the process).

Safe lock: while engaged every command is refused except ``jog_stop``,
``cancel_motion`` and ``set_safe_lock`` itself. Engaging it does not stop motion
already in progress.

Z soft limits (``limits.z_min_um``/``z_max_um``): ``move_z_to`` refuses targets
outside them, and a jog is executed as a move to the soft limit in the jog
direction, so it stops exactly at the limit even between polls. Homing goes to
the hardware home position regardless of the soft limits.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot

from ..drivers.base import Direction, LinearStage, MotorState, RotaryStage
from .safety import SafetyMonitor

log = logging.getLogger(__name__)

Clock = Callable[[], float]


@dataclass(frozen=True)
class MotionStatus:
    z_um: float | None
    z_state: MotorState
    speed_um_s: float | None
    rotator_deg: float | None
    rotator_state: MotorState
    seconds_since_last_motion: float
    safe_lock: bool
    jog_direction: int = 0  # +1 up, -1 down, 0 not jogging

    @property
    def busy(self) -> bool:
        return self.z_state.is_busy or self.rotator_state.is_busy


class MotionWorker(QObject):
    status = pyqtSignal(object)  # MotionStatus
    error = pyqtSignal(str)  # device failure
    refused = pyqtSignal(str)  # command rejected (safe lock, soft limit, bad value)
    connection = pyqtSignal(object)  # {"z"/"rotator": (ok, detail)} from check_connection

    def __init__(
        self,
        z: LinearStage,
        rotator: RotaryStage,
        safety: SafetyMonitor,
        *,
        poll_ms: int = 100,
        clock: Clock = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.z = z
        self.rotator = rotator
        self.safety = safety
        self.poll_ms = int(poll_ms)
        self._clock = clock
        self._timer: QTimer | None = None
        self._safe_lock = False
        self._jog_dir = 0
        self._last_motion = clock()
        self._poll_error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    @pyqtSlot()
    def start(self) -> None:
        """Start polling. Call in the worker thread (``start_worker`` does)."""
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setTimerType(Qt.PreciseTimer)
            self._timer.timeout.connect(self.poll)
        self._timer.start(self.poll_ms)
        self.poll()

    @pyqtSlot()
    def stop(self) -> None:
        """Stop polling and stop both axes."""
        if self._timer is not None:
            self._timer.stop()
        self._jog_dir = 0
        self._call("stop Z", self.z.stop, True)
        self._call("stop rotator", self.rotator.stop, True)

    # ------------------------------------------------------------------ helpers

    @property
    def safe_lock(self) -> bool:
        return self._safe_lock

    def _call(self, what: str, fn: Callable, *args) -> bool:
        try:
            fn(*args)
            return True
        except Exception as exc:  # noqa: BLE001 - never let a device error escape a slot
            log.error("motion: %s failed: %s", what, exc)
            self.error.emit(f"{what} failed: {exc}")
            return False

    def _locked(self, what: str) -> bool:
        if self._safe_lock:
            log.info("motion: %s refused: safe lock is on", what)
            self.refused.emit(f"{what}: safe lock is on")
            return True
        return False

    # ------------------------------------------------------------------ commands

    @pyqtSlot(int)
    def jog_start(self, direction: int) -> None:
        if self._locked("jog"):
            return
        try:
            d = Direction(1 if direction > 0 else -1)
        except Exception:  # noqa: BLE001
            self.refused.emit(f"jog: bad direction {direction!r}")
            return
        target = self.safety.z_max_um if d is Direction.UP else self.safety.z_min_um
        try:
            pos = self.z.position_um
        except Exception as exc:  # noqa: BLE001
            log.error("motion: jog failed: %s", exc)
            self.error.emit(f"jog failed: {exc}")
            return
        if (pos >= target and d is Direction.UP) or (pos <= target and d is Direction.DOWN):
            log.info("motion: jog %s refused at %g µm: soft limit %g µm", d.name.lower(), pos, target)
            self.refused.emit(f"jog {d.name.lower()}: at Z soft limit ({target:g} µm)")
            return
        if self._call("jog", self.z.move_to, target):
            log.info("motion: jog %s from %g µm toward %g µm", d.name.lower(), pos, target)
            self._jog_dir = int(d)
            self._last_motion = self._clock()

    @pyqtSlot()
    def jog_stop(self) -> None:
        log.info("motion: jog stop")
        self._jog_dir = 0
        self._call("stop Z", self.z.stop, True)

    @pyqtSlot(float)
    def move_z_to(self, um: float) -> None:
        if self._locked("move Z"):
            return
        if not self.safety.z_move_allowed(um):
            self.refused.emit(
                f"move Z to {um:g} µm: outside soft limits {self.safety.z_min_um:g}–{self.safety.z_max_um:g} µm"
            )
            return
        self._jog_dir = 0
        if self._call("move Z", self.z.move_to, float(um)):
            self._last_motion = self._clock()

    @pyqtSlot(float)
    def set_speed(self, um_s: float) -> None:
        if self._locked("set speed"):
            return
        um_s = float(um_s)
        if not (math.isfinite(um_s) and um_s > 0):
            self.refused.emit(f"set speed: {um_s!r} µm/s is not a positive number")
            return
        try:
            um_s = min(um_s, self.z.max_velocity_um_s)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"set speed failed: {exc}")
            return
        if self._call("set speed", self.z.set_velocity, um_s) and self._jog_dir:
            # Re-issue the jog so the new speed takes effect immediately.
            target = self.safety.z_max_um if self._jog_dir > 0 else self.safety.z_min_um
            self._call("jog", self.z.move_to, target)

    @pyqtSlot()
    def cancel_motion(self) -> None:
        """Stop both axes immediately. Always allowed."""
        self._jog_dir = 0
        self._call("stop Z", self.z.stop, True)
        self._call("stop rotator", self.rotator.stop, True)

    @pyqtSlot(float)
    def rotate_to(self, deg: float) -> None:
        if self._locked("rotate"):
            return
        deg = float(deg)
        if not math.isfinite(deg):
            self.refused.emit(f"rotate: {deg!r} is not a finite angle")
            return
        if self._call("rotate", self.rotator.move_to, deg):
            self._last_motion = self._clock()

    @pyqtSlot()
    def home_z(self) -> None:
        if self._locked("home Z"):
            return
        self._jog_dir = 0
        if self._call("home Z", self.z.home):
            self._last_motion = self._clock()

    @pyqtSlot()
    def home_rotator(self) -> None:
        if self._locked("home rotator"):
            return
        if self._call("home rotator", self.rotator.home):
            self._last_motion = self._clock()

    @pyqtSlot(bool)
    def set_safe_lock(self, on: bool) -> None:
        self._safe_lock = bool(on)
        log.info("motion: safe lock %s", "on" if on else "off")
        self.poll()

    # ------------------------------------------------------------------ connection check

    @pyqtSlot()
    def check_connection(self) -> None:
        """Talk to both stages and reconnect any that is not connected, does not answer or reports an error.

        Emits ``connection`` with ``{"z": (ok, detail), "rotator": (ok, detail)}``. Allowed under
        safe lock: it never moves anything. A reconnect blocks this thread for a few seconds
        (Kinesis Connect + settings load); polling resumes afterwards.
        """
        results = {
            "z": self._check_axis("Z", self.z, lambda: f"{self.z.position_um / 1000.0:.4f} mm"),
            "rotator": self._check_axis("rotator", self.rotator, lambda: f"{self.rotator.angle_deg:.2f}°"),
        }
        self.connection.emit(results)
        self.poll()

    def _check_axis(self, label: str, dev, where: Callable[[], str]) -> tuple[bool, str]:
        def probe() -> tuple[bool, str]:
            """Raise if the stage does not answer or reports an error (both get a reconnect)."""
            state = dev.state
            pos = where()  # raises if the device does not answer
            if state is MotorState.ERROR:
                raise RuntimeError(f"stage reports an error state (at {pos})")
            if state is MotorState.DISCONNECTED:
                raise RuntimeError("not connected")
            return True, f"responding: {state.value}, {pos}"

        if dev.is_connected:
            try:
                return probe()
            except Exception as exc:  # noqa: BLE001
                log.warning("connection check: %s does not answer (%s), reconnecting", label, exc)
                try:
                    dev.close()
                except Exception as close_exc:  # noqa: BLE001
                    log.warning("connection check: closing %s failed: %s", label, close_exc)
            action = "reconnected"
        else:
            action = "connected"
        try:
            dev.connect()
        except Exception as exc:  # noqa: BLE001
            log.error("connection check: %s: %s", label, exc)
            return False, f"not connected: {exc}"
        try:
            _, detail = probe()
        except Exception as exc:  # noqa: BLE001
            log.error("connection check: %s %s, but %s", label, action, exc)
            return False, f"{action}, but {exc}"
        log.info("connection check: %s %s", label, action)
        return True, f"{action}; {detail}"

    # ------------------------------------------------------------------ polling

    def _read(self, fn: Callable[[], object], errors: list[str], what: str):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{what}: {exc}")
            return None

    @pyqtSlot()
    def poll(self) -> None:
        errors: list[str] = []
        z_um = self._read(lambda: self.z.position_um, errors, "Z position")
        z_state = self._read(lambda: self.z.state, errors, "Z state") or MotorState.ERROR
        speed = self._read(lambda: self.z.velocity_um_s, errors, "Z speed")
        r_deg = self._read(lambda: self.rotator.angle_deg, errors, "rotator angle")
        r_state = self._read(lambda: self.rotator.state, errors, "rotator state") or MotorState.ERROR

        now = self._clock()
        if z_state.is_busy or r_state.is_busy:
            self._last_motion = now
        if self._jog_dir and not z_state.is_busy:
            self._jog_dir = 0  # jog ended at the soft limit (or was stopped elsewhere)

        msg = "; ".join(errors) or None
        if msg != self._poll_error:
            self._poll_error = msg
            if msg:
                log.error("motion poll: %s", msg)
                self.error.emit(f"poll failed: {msg}")

        self.status.emit(
            MotionStatus(
                z_um=z_um,
                z_state=z_state,
                speed_um_s=speed,
                rotator_deg=r_deg,
                rotator_state=r_state,
                seconds_since_last_motion=max(now - self._last_motion, 0.0),
                safe_lock=self._safe_lock,
                jog_direction=self._jog_dir,
            )
        )
