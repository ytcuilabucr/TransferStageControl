"""Headless scripting API.

::

    from transferstage.api import Stage

    with Stage() as stage:                       # config.yaml, devices connected, CSV log started
        stage.home()
        stage.move_z(5000)                       # µm, absolute (inside the Z soft limits)
        stage.psu_enabled(True)
        stage.set_temperature(150, rate_deg_min=20)
        stage.wait_for_temperature(tol_c=1, stable_s=60, timeout_s=3600)
        stage.rotate(90)
        stage.sleep(600)                          # anneal
        stage.psu_enabled(False)
    # leaving the block (normally or by an exception / Ctrl+C) runs safe_shutdown:
    # motion stopped, PSU off and 0 V, every device closed

The temperature loop is the same :class:`ThermalWorker` the GUI uses (PID, ramp,
autotune, :class:`SafetyMonitor` with the 300 °C / 24 V hard limits). In normal
mode it runs on a background thread every ``pid.period_ms``.

``virtual_time=True`` (simulators only) replaces the wall clock with a virtual
clock: nothing runs in the background, and every wait (:meth:`Stage.sleep`,
move waits, :meth:`Stage.wait_for_temperature`) advances the clock and steps the
loop, so an hour-long anneal finishes in seconds. Scripts must wait with
``stage.sleep()``, not ``time.sleep()``, to work in both modes.

Errors: refused commands raise :class:`StageError`; a safety trip raises
:class:`SafetyTripError` from the next wait (the PSU has already been switched off
by then).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config, load_config
from .control.autotune import AutotuneParams, AutotuneResult, AutotuneState
from .control.motion import MotionStatus
from .control.safety import SafetyMonitor, safe_shutdown
from .control.thermal import ThermalSample, ThermalWorker
from .datalog import DataLogger, run_metadata
from .drivers import Devices, build_devices
from .drivers.base import Direction, MotorState

log = logging.getLogger(__name__)

__all__ = ["Readings", "SafetyTripError", "Stage", "StageError", "VirtualClock"]


class StageError(RuntimeError):
    """A command was refused or a device failed."""


class SafetyTripError(StageError):
    """A safety trip (over-temperature, stale reading) latched; the PSU is off."""


class VirtualClock:
    """Manually advanced clock for ``virtual_time`` runs."""

    def __init__(self, t0: float = 0.0) -> None:
        self.t = float(t0)

    def __call__(self) -> float:
        return self.t


@dataclass(frozen=True)
class Readings:
    time_s: float  # seconds since the stage was opened (stage clock)
    temperature_c: float | None
    target_c: float
    program_sp_c: float
    v_cmd: float
    v_read: float | None
    i_read: float | None
    psu_on: bool
    ramping: bool
    mode: str  # "pid" or "autotune"
    z_um: float | None
    rotator_deg: float | None
    alarms: tuple[str, ...]


_BUSY = (MotorState.MOVING, MotorState.HOMING, MotorState.STOPPING, MotorState.INITIALIZING)


class Stage:
    """Context manager owning the devices, the temperature loop and the CSV log.

    ``config``: a :class:`Config`, a path to a YAML file, or None for ``config.yaml``.
    ``log``: write a session CSV to ``logging.dir`` (one row per ``logging.interval_s``).
    """

    def __init__(
        self,
        config: Config | str | Path | None = None,
        *,
        virtual_time: bool = False,
        log: bool = True,
        poll_s: float = 0.05,
    ) -> None:
        self.config = config if isinstance(config, Config) else load_config(config)
        self.virtual_time = bool(virtual_time)
        if self.virtual_time:
            sim = self.config.devices.simulate
            if not (sim.z and sim.rotator and sim.psu and sim.dmm):
                raise StageError("virtual_time needs every device simulated (devices.simulate.* = true)")
        self.clock: Callable[[], float] = VirtualClock(1000.0) if self.virtual_time else time.monotonic
        self.poll_s = float(poll_s)
        self._want_log = bool(log)
        self._lock = threading.RLock()
        self._refusals: list[str] = []
        self._alarms: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop_error: BaseException | None = None
        self._last: ThermalSample | None = None
        self._next_tick = 0.0
        self._next_log = 0.0
        self._opened = False
        self._closed = False
        self.devices: Devices | None = None
        self.safety: SafetyMonitor | None = None
        self.thermal: ThermalWorker | None = None
        self.datalog: DataLogger | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> Stage:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            log.error("stage: leaving on %s: %s", exc_type.__name__, exc)
        self.close()
        return False

    def open(self) -> Stage:
        """Create and connect the devices, start the temperature loop and the log."""
        if self._opened:
            return self
        self._opened = True
        cfg = self.config
        self.devices = build_devices(cfg, clock=self.clock)
        errors = self.devices.connect_all()
        if errors:
            safe_shutdown(self.devices)
            self._closed = True
            raise StageError("could not connect: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        try:
            self.safety = SafetyMonitor(cfg.limits, clock=self.clock)
            self.thermal = ThermalWorker(
                self.devices.psu, self.devices.thermometer, cfg, safety=self.safety, clock=self.clock
            )
            # Plain-callable receivers are called directly in the emitting thread (no Qt event loop needed).
            self.thermal.refused.connect(self._refusals.append)
            self.thermal.error.connect(lambda m: log.error("stage: %s", m))
            self.thermal.alarm.connect(lambda a: self._alarms.append(a.message))
            if self._want_log:
                self.datalog = DataLogger.from_config(cfg.logging, clock=self.clock)
                self.datalog.open(run_metadata(cfg, source="api", virtual_time=self.virtual_time))
            self.period_s = cfg.pid.period_ms / 1000.0
            self._t0 = self.clock()
            self._next_log = self._t0
            self._tick()
            self._next_tick = self._t0 + self.period_s
            if not self.virtual_time:
                self._thread = threading.Thread(target=self._run, name="stage-thermal", daemon=True)
                self._thread.start()
        except BaseException:
            self.close()
            raise
        log.info("stage opened (%s)", "virtual time" if self.virtual_time else "real time")
        return self

    def close(self) -> None:
        """Stop the loop, close the log, then ``safe_shutdown`` the devices. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        with self._lock:
            if self.thermal is not None:
                try:
                    self.thermal.stop()  # cancels autotune, PSU off, 0 V
                except Exception:  # noqa: BLE001
                    log.exception("stage: stopping the thermal loop failed")
            if self.datalog is not None:
                try:
                    if self._last is not None:
                        self._log_row(force=True)
                except Exception:  # noqa: BLE001
                    log.exception("stage: final log row failed")
                self.datalog.close()
        if self.devices is not None:
            failures = safe_shutdown(self.devices)
            if failures:
                log.error("stage: shutdown steps failed: %s", [n for n, _ in failures])
        log.info("stage closed")

    @property
    def log_path(self) -> Path | None:
        return None if self.datalog is None else self.datalog.path

    def mark(self, text: str = "") -> str:
        """Write an event note (e.g. ``"stamp contact"``) to the session CSV ``note`` column and the log."""
        self._check_open()
        if self.datalog is None:
            note = " ".join(str(text).split()) or "mark"
            log.info("MARK: %s (no data log)", note)
            return note
        with self._lock:
            self.datalog.on_motion(self._motion_status())
            return self.datalog.mark(text)

    # ------------------------------------------------------------------ loop

    def _tick(self) -> None:
        with self._lock:
            self._last = self.thermal.step()
            if self.datalog is not None:
                self.datalog.on_thermal(self._last)  # every step, so sample_age_s and marks see the latest
            self._log_row()

    def _log_row(self, force: bool = False) -> None:
        if self.datalog is None:
            return
        now = self.clock()
        if not force and now < self._next_log:
            return
        self._next_log = now + self.datalog.interval_s
        self.datalog.on_motion(self._motion_status())
        self.datalog.write_row()

    def _motion_status(self) -> MotionStatus:
        d = self.devices

        def get(fn, default=None):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return default

        return MotionStatus(
            z_um=get(lambda: d.z.position_um),
            z_state=get(lambda: d.z.state, MotorState.ERROR),
            speed_um_s=get(lambda: d.z.velocity_um_s),
            rotator_deg=get(lambda: d.rotator.angle_deg),
            rotator_state=get(lambda: d.rotator.state, MotorState.ERROR),
            seconds_since_last_motion=0.0,
            safe_lock=False,
        )

    def _run(self) -> None:
        nxt = time.monotonic()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - make the heater safe and report on the next wait
                log.exception("stage: temperature loop failed")
                self._loop_error = exc
                with self._lock:
                    try:
                        self.thermal.set_psu_enabled(False)
                    except Exception:  # noqa: BLE001
                        pass
                return
            nxt += self.period_s
            self._stop.wait(max(nxt - time.monotonic(), 0.0))

    def _check_open(self) -> None:
        if self._closed:
            raise StageError("the stage is closed")
        if not self._opened:
            raise StageError("the stage is not open (use 'with Stage() as stage:' or stage.open())")

    def _check(self) -> None:
        """Open, loop alive, no latched trip (checked before and after every wait)."""
        self._check_open()
        if self._loop_error is not None:
            raise StageError(f"temperature loop stopped: {self._loop_error}")
        if self.safety.tripped:
            raise SafetyTripError("; ".join(a.message for a in self.safety.latched))

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` (virtual time: advance the clock and run the loop). Raises on a safety trip."""
        self._check()
        seconds = max(float(seconds), 0.0)
        if self.virtual_time:
            clock: VirtualClock = self.clock  # type: ignore[assignment]
            end = clock.t + seconds
            while self._next_tick <= end:
                clock.t = max(clock.t, self._next_tick)
                self._next_tick += self.period_s
                self._tick()
            clock.t = end
        else:
            end = time.monotonic() + seconds
            while (left := end - time.monotonic()) > 0:
                time.sleep(min(left, 0.2))
                if self.safety.tripped or self._loop_error is not None:
                    break
        self._check()

    def wait_for(self, condition: Callable[[Readings], bool], timeout_s: float | None = None, poll_s: float = 1.0) -> Readings:
        """Wait until ``condition(readings)`` is true. Raises TimeoutError / SafetyTripError."""
        start = self.clock()
        while True:
            r = self.readings()
            if condition(r):
                return r
            if timeout_s is not None and self.clock() - start >= timeout_s:
                raise TimeoutError(f"condition not met within {timeout_s:g} s")
            self.sleep(poll_s)

    def _call(self, fn: Callable, *args) -> None:
        self._check_open()
        with self._lock:
            n = len(self._refusals)
            fn(*args)
            if len(self._refusals) > n:
                raise StageError(self._refusals[-1])

    # ------------------------------------------------------------------ motion

    def _wait_idle(self, axis, what: str, timeout_s: float | None) -> None:
        start = self.clock()
        while True:
            try:
                state = axis.state
            except Exception as exc:  # noqa: BLE001
                raise StageError(f"{what}: {exc}") from exc
            if state is MotorState.ERROR:
                raise StageError(f"{what}: motor error")
            if state not in _BUSY:
                return
            if timeout_s is not None and self.clock() - start > timeout_s:
                self._device("stop", axis.stop, True)
                raise TimeoutError(f"{what} did not finish within {timeout_s:g} s")
            self.sleep(self.poll_s)

    def _device(self, what: str, fn: Callable, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            raise StageError(f"{what} failed: {exc}") from exc

    def set_z_speed(self, um_s: float) -> None:
        self._check()
        z = self.devices.z
        v = float(um_s)
        if not (math.isfinite(v) and v > 0):
            raise StageError(f"Z speed must be > 0 µm/s, got {um_s!r}")
        self._device("set Z speed", z.set_velocity, min(v, z.max_velocity_um_s))

    def move_z(self, um: float, *, relative: bool = False, wait: bool = True, timeout_s: float | None = None) -> float:
        """Move Z to ``um`` (or by ``um`` if ``relative``); refused outside the soft limits. Returns the position."""
        self._check()
        z = self.devices.z
        target = float(um) + (self._device("read Z", lambda: z.position_um) if relative else 0.0)
        if not self.safety.z_move_allowed(target):
            raise StageError(
                f"move Z to {target:g} µm: outside soft limits {self.safety.z_min_um:g}–{self.safety.z_max_um:g} µm"
            )
        self._device("move Z", z.move_to, target)
        if wait:
            self._wait_idle(z, "move Z", timeout_s)
        return self._device("read Z", lambda: z.position_um)

    def jog_z(self, direction: int, duration_s: float, *, speed_um_s: float | None = None) -> float:
        """Jog Z up (+1) or down (-1) for ``duration_s``; stops early at the soft limit. Returns the position."""
        self._check()
        d = Direction.UP if direction > 0 else Direction.DOWN
        if speed_um_s is not None:
            self.set_z_speed(speed_um_s)
        z = self.devices.z
        limit = self.safety.z_max_um if d is Direction.UP else self.safety.z_min_um
        pos = self._device("read Z", lambda: z.position_um)
        if (d is Direction.UP and pos >= limit) or (d is Direction.DOWN and pos <= limit):
            raise StageError(f"jog {d.name.lower()}: at Z soft limit ({limit:g} µm)")
        self._device("jog Z", z.move_to, limit)  # a jog is a move toward the soft limit, stopped after duration_s
        try:
            end = self.clock() + float(duration_s)
            while self.clock() < end and self._device("read Z", lambda: z.state) in _BUSY:
                self.sleep(min(self.poll_s, end - self.clock()))
        finally:
            try:
                z.stop(True)
            except Exception:  # noqa: BLE001
                log.exception("stage: stop Z after jog failed")
        self._wait_idle(z, "jog Z", 10.0)
        return self._device("read Z", lambda: z.position_um)

    def rotate(self, deg: float, *, relative: bool = False, wait: bool = True, timeout_s: float | None = None) -> float:
        """Rotate to ``deg`` (or by ``deg`` if ``relative``). Returns the angle."""
        self._check()
        r = self.devices.rotator
        target = float(deg) + (self._device("read rotator", lambda: r.angle_deg) if relative else 0.0)
        if not math.isfinite(target):
            raise StageError(f"rotate: invalid angle {deg!r}")
        self._device("rotate", r.move_to, target)
        if wait:
            self._wait_idle(r, "rotate", timeout_s)
        return self._device("read rotator", lambda: r.angle_deg)

    def home(self, *, z: bool = True, rotator: bool = True, wait: bool = True, timeout_s: float | None = None) -> None:
        self._check()
        if z:
            self._device("home Z", self.devices.z.home)
        if rotator:
            self._device("home rotator", self.devices.rotator.home)
        if wait:
            if z:
                self._wait_idle(self.devices.z, "home Z", timeout_s)
            if rotator:
                self._wait_idle(self.devices.rotator, "home rotator", timeout_s)

    def stop_motion(self) -> None:
        for name, dev in (("Z", self.devices.z), ("rotator", self.devices.rotator)):
            self._device(f"stop {name}", dev.stop, True)

    # ------------------------------------------------------------------ temperature

    def psu_enabled(self, on: bool) -> None:
        """Switch the heater output on (PID active) or off (0 V). Refused while a trip is latched."""
        if on and self.safety is not None and self.safety.tripped:
            raise SafetyTripError("PSU stays off until the trip is acknowledged: " + "; ".join(a.message for a in self.safety.latched))
        self._call(self.thermal.set_psu_enabled, bool(on))
        if on and not self.thermal.psu_enabled:
            raise StageError("PSU could not be enabled (see the log)")

    @property
    def psu_on(self) -> bool:
        return bool(self.thermal is not None and self.thermal.psu_enabled)

    def set_temperature(self, target_c: float, rate_deg_min: float | None = None) -> None:
        """Ramp the set point to ``target_c`` at ``rate_deg_min`` (None: config default, 0: step)."""
        rate = self.config.ramp.default_rate_deg_min if rate_deg_min is None else rate_deg_min
        self._call(self.thermal.set_target, float(target_c), float(rate))

    def set_pid(self, kc: float, ti_min: float, td_min: float = 0.0) -> None:
        self._call(self.thermal.set_gains, float(kc), float(ti_min), float(td_min))

    def wait_for_temperature(
        self, tol_c: float = 1.0, stable_s: float = 30.0, timeout_s: float | None = None, *, poll_s: float = 1.0
    ) -> Readings:
        """Wait until the ramp is done and |T − target| <= ``tol_c`` continuously for ``stable_s``."""
        start = self.clock()
        since: float | None = None
        while True:
            r = self.readings()
            ok = (
                not r.ramping
                and r.temperature_c is not None
                and abs(r.temperature_c - r.target_c) <= tol_c
            )
            now = self.clock()
            if ok:
                since = now if since is None else since
                if now - since >= stable_s:
                    return r
            else:
                since = None
            if timeout_s is not None and now - start >= timeout_s:
                raise TimeoutError(
                    f"temperature not within ±{tol_c:g} °C of {r.target_c:g} °C for {stable_s:g} s "
                    f"after {timeout_s:g} s (now {r.temperature_c})"
                )
            self.sleep(poll_s)

    def autotune(self, params: AutotuneParams, *, poll_s: float = 1.0) -> AutotuneResult:
        """Run a relay autotune (PSU must be on). Returns the result; gains are not applied."""
        done: list = []
        on_done = done.append
        self.thermal.autotune_finished.connect(on_done)
        try:
            self._call(self.thermal.start_autotune, params)
            if not self.thermal.autotune_running:
                raise StageError("autotune did not start")
            while not done:
                self.sleep(poll_s)
        except BaseException:
            with self._lock:
                self.thermal.cancel_autotune()
            raise
        finally:
            self.thermal.autotune_finished.disconnect(on_done)
        st = done[0]
        if st.state is not AutotuneState.DONE or st.result is None:
            raise StageError(f"autotune aborted: {st.message}")
        return st.result

    def acknowledge_alarm(self) -> None:
        self._call(self.thermal.acknowledge_alarm)

    # ------------------------------------------------------------------ readings

    def readings(self) -> Readings:
        self._check_open()
        with self._lock:
            s = self._last
        m = self._motion_status()
        return Readings(
            time_s=self.clock() - self._t0,
            temperature_c=s.temperature_c,
            target_c=s.target_sp_c,
            program_sp_c=s.program_sp_c,
            v_cmd=s.v_cmd,
            v_read=s.v_read,
            i_read=s.i_read,
            psu_on=s.psu_on,
            ramping=s.ramping,
            mode=s.mode,
            z_um=m.z_um,
            rotator_deg=m.rotator_deg,
            alarms=tuple(a.message for a in s.alarms),
        )

    @property
    def temperature_c(self) -> float | None:
        return self.readings().temperature_c

    @property
    def z_um(self) -> float | None:
        return self.readings().z_um

    @property
    def rotator_deg(self) -> float | None:
        return self.readings().rotator_deg
