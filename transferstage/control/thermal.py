"""Thermal worker: the closed temperature loop, run on its own QThread.

Every ``pid.period_ms`` :meth:`ThermalWorker.step` does::

    read T -> program set point (ramp or profile) -> PID -> SafetyMonitor.clamp_voltage
           -> psu.set_voltage -> PSU readback -> safety checks -> emit sample

* The PID output is applied only while the PSU is enabled; disabling the PSU
  switches the output off, commands 0 V and resets the PID.
* A safety trip (over-temperature, stale reading) switches the PSU off, disables
  it and emits :attr:`alarm`. While a trip is latched the PSU cannot be
  re-enabled; :meth:`acknowledge_alarm` clears the latch.
* A failed temperature read keeps the last voltage command; if no fresh reading
  arrives for ``limits.stale_temp_s`` the STALE_TEMP trip switches the PSU off.
* Autotune mode (:meth:`ThermalWorker.start_autotune`): a :class:`RelayAutotune`
  replaces the PID output (still through ``clamp_voltage`` and the safety checks).
  It ends on success, timeout, cancel, PSU off or a safety trip and emits
  :attr:`autotune_finished`. Unless the PSU went off, PID control then resumes at
  the autotune set point, its integrator preset to the relay bias (bumpless).
  The gains are not changed automatically; apply them with :meth:`set_gains`.

``clock`` is the time base for the ramp, profile, PID dt and safety checks. For
accelerated tests pass the simulator's clock and drive :meth:`step` directly.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot

from ..config import Config
from ..drivers.base import PowerSupply, PsuReading, Thermometer
from .autotune import AutotuneParams, AutotuneState, RelayAutotune
from .pid import PID
from .profile import ProfileRunner, TemperatureProfile
from .ramp import SetpointRamp
from .safety import Alarm, AlarmCode, SafetyMonitor, SafetySample

log = logging.getLogger(__name__)

Clock = Callable[[], float]


@dataclass(frozen=True)
class ThermalSample:
    t: float  # seconds since the worker was created (worker clock)
    temperature_c: float | None  # None if the read failed
    target_sp_c: float  # final target (ramp target or current profile segment target)
    program_sp_c: float  # set point used by the PID this period
    v_cmd: float  # voltage commanded this period (0 when PSU disabled)
    v_read: float | None
    i_read: float | None
    ramping: bool
    psu_on: bool
    alarms: tuple[Alarm, ...] = field(default_factory=tuple)
    profile_segment: int | None = None
    profile_phase: str | None = None
    mode: str = "pid"  # "pid" or "autotune"

    @property
    def tripped(self) -> bool:
        return any(a.is_trip for a in self.alarms)


class ThermalWorker(QObject):
    sample = pyqtSignal(object)  # ThermalSample
    alarm = pyqtSignal(object)  # Alarm, emitted when an alarm becomes active
    error = pyqtSignal(str)  # device failure
    refused = pyqtSignal(str)  # command rejected
    profile_finished = pyqtSignal()
    autotune_progress = pyqtSignal(object)  # AutotuneStatus, every period while autotune runs
    autotune_finished = pyqtSignal(object)  # AutotuneStatus (state DONE with .result, or ABORTED)

    def __init__(
        self,
        psu: PowerSupply,
        thermometer: Thermometer,
        config: Config,
        *,
        safety: SafetyMonitor | None = None,
        clock: Clock = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.psu = psu
        self.thermometer = thermometer
        self.config = config
        self.period_ms = config.pid.period_ms
        self._clock = clock
        self.safety = safety if safety is not None else SafetyMonitor(config.limits, clock=clock)
        self.pid = PID.from_config(config.pid)
        self.ramp = SetpointRamp(22.0, max_c=self.safety.t_max_c, clock=clock)
        self._t0 = clock()
        self._timer: QTimer | None = None
        self._psu_enabled = False
        self._v_cmd = 0.0
        self._last_step: float | None = None
        self._temp: float | None = None
        self._temp_time: float | None = None
        self._have_reading = False
        self._runner: ProfileRunner | None = None
        self._autotune: RelayAutotune | None = None
        self._active_alarms: set[AlarmCode] = set()
        self._errors: dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle

    @pyqtSlot()
    def start(self) -> None:
        """Start the control loop. Call in the worker thread (``start_worker`` does)."""
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setTimerType(Qt.PreciseTimer)
            self._timer.timeout.connect(self.step)
        self._timer.start(self.period_ms)

    @pyqtSlot()
    def stop(self) -> None:
        """Stop the loop and switch the heater off."""
        if self._timer is not None:
            self._timer.stop()
        self._disable_psu()

    # ------------------------------------------------------------------ helpers

    @property
    def psu_enabled(self) -> bool:
        return self._psu_enabled

    @property
    def profile_running(self) -> bool:
        return self._runner is not None

    @property
    def autotune_running(self) -> bool:
        return self._autotune is not None

    def _report(self, key: str, exc: BaseException | None) -> None:
        """Emit ``error`` when a failure starts or its message changes; stay quiet while it repeats."""
        if exc is None:
            self._errors.pop(key, None)
            return
        msg = f"{key} failed: {exc}"
        if self._errors.get(key) != msg:
            self._errors[key] = msg
            log.error("thermal: %s", msg)
            self.error.emit(msg)

    def _disable_psu(self) -> None:
        self._end_autotune("PSU switched off", resume_pid=False)
        self._psu_enabled = False
        self._v_cmd = 0.0
        self.pid.reset()
        for what, fn in (("PSU output off", lambda: self.psu.output(False)), ("PSU 0 V", lambda: self.psu.set_voltage(0.0))):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                log.error("thermal: %s failed: %s", what, exc)
                self.error.emit(f"{what} failed: {exc}")

    def _start_point(self) -> float:
        """Where a new ramp/profile starts: the measured temperature if known, else the program SP."""
        if self._temp is not None:
            return min(self._temp, self.safety.t_max_c)
        return self.ramp.program_sp()

    # ------------------------------------------------------------------ commands

    @pyqtSlot(float, float)
    def set_target(self, sp_c: float, rate_deg_min: float) -> None:
        """Ramp to ``sp_c`` at ``rate_deg_min`` (0 = step). Cancels a running profile."""
        sp_c, rate = float(sp_c), float(rate_deg_min)
        if not math.isfinite(sp_c) or not math.isfinite(rate) or rate < 0:
            self.refused.emit(f"set target: invalid set point {sp_c!r} / rate {rate!r}")
            return
        if sp_c > self.safety.t_max_c:
            self.refused.emit(f"set target: {sp_c:g} °C is above the {self.safety.t_max_c:g} °C limit")
            return
        if self._autotune is not None:
            self.refused.emit("set target: autotune is running; cancel it first")
            return
        if self._runner is not None:
            self._runner = None
            log.info("thermal: profile cancelled by a manual target")
        # Retargeting during an active ramp continues from the program SP (no jump);
        # otherwise start from the measured temperature.
        start = None if (self._psu_enabled and self.ramp.is_ramping) else self._start_point()
        self.ramp.set_target(sp_c, rate, current_value=start)

    @pyqtSlot(bool)
    def set_psu_enabled(self, on: bool) -> None:
        if not on:
            self._disable_psu()
            return
        if self.safety.tripped:
            self.refused.emit("enable PSU: a safety trip is latched; acknowledge the alarm first")
            return
        try:
            self.psu.set_current(self.config.limits.i_max)
            self.psu.set_voltage(0.0)
            self.psu.output(True)
        except Exception as exc:  # noqa: BLE001
            log.error("thermal: enable PSU failed: %s", exc)
            self.error.emit(f"enable PSU failed: {exc}")
            self._disable_psu()
            return
        self.pid.reset()
        self._v_cmd = 0.0
        self._last_step = None
        self._psu_enabled = True

    @pyqtSlot(float, float, float)
    def set_gains(self, kc: float, ti_min: float, td_min: float) -> None:
        try:
            self.pid.set_tunings(kc, ti_min, td_min)
        except ValueError as exc:
            self.refused.emit(f"set gains: {exc}")

    @pyqtSlot()
    def reset_pid(self) -> None:
        self.pid.reset()

    @pyqtSlot(object)
    def start_profile(self, profile: TemperatureProfile) -> None:
        if not isinstance(profile, TemperatureProfile):
            self.refused.emit("start profile: not a TemperatureProfile")
            return
        if self._autotune is not None:
            self.refused.emit("start profile: autotune is running; cancel it first")
            return
        too_hot = [s.ramp_to for s in profile.segments if s.ramp_to > self.safety.t_max_c]
        if too_hot:
            self.refused.emit(f"start profile: segment target {max(too_hot):g} °C is above the {self.safety.t_max_c:g} °C limit")
            return
        runner = ProfileRunner(profile, self._start_point(), clock=self._clock)
        runner.start()
        self._runner = runner
        log.info("thermal: profile %r started (%.0f s)", profile.name, runner.total_s)

    @pyqtSlot()
    def stop_profile(self) -> None:
        """Stop the profile and hold the current program set point."""
        if self._runner is None:
            return
        sp = self._runner.status().program_sp
        self._runner = None
        self.ramp.reset(sp)

    @pyqtSlot(object)
    def start_autotune(self, params: AutotuneParams) -> None:
        """Start a relay autotune around ``params.setpoint_c``. Needs the PSU on and a reading."""
        if not isinstance(params, AutotuneParams):
            self.refused.emit("autotune: not AutotuneParams")
            return
        if self._autotune is not None:
            self.refused.emit("autotune: already running")
            return
        if not self._psu_enabled or self.safety.tripped:
            self.refused.emit("autotune: switch the PSU on first (and acknowledge any trip)")
            return
        if self._runner is not None:
            self.refused.emit("autotune: a profile is running; stop it first")
            return
        if self._temp is None:
            self.refused.emit("autotune: no temperature reading yet")
            return
        if params.bias_v is None:
            if self._v_cmd <= 0:
                self.refused.emit("autotune: the present output is 0 V; enter a bias voltage")
                return
            params = dataclasses.replace(params, bias_v=round(self._v_cmd, 3))
        try:
            self._autotune = RelayAutotune(params, v_max=self.safety.v_max, t_max_c=self.safety.t_max_c)
        except ValueError as exc:
            self.refused.emit(f"autotune: {exc}")
            return
        at = self._autotune
        log.info(
            "thermal: autotune started at %.1f °C, relay %.2f/%.2f V, h=%.2f °C, %d cycles",
            params.setpoint_c, at.v_low, at.v_high, params.hysteresis_c, params.cycles,
        )  # fmt: skip

    @pyqtSlot()
    def cancel_autotune(self) -> None:
        self._end_autotune("cancelled by user")

    def _end_autotune(self, reason: str | None, *, resume_pid: bool = True) -> None:
        """Finish the running autotune (abort with ``reason`` if it is still running)."""
        at = self._autotune
        if at is None:
            return
        if reason is not None:
            at.abort(reason)
        self._autotune = None
        st = at.status()
        if st.state is AutotuneState.DONE:
            log.info("thermal: autotune done: %s", st.message)
        else:
            log.warning("thermal: autotune aborted: %s", st.message)
        if resume_pid:
            self.ramp.reset(at.params.setpoint_c)
            # Bumpless hand-back: the I term is what the feedforward does not already supply.
            self.pid.reset(integral=at.params.bias_v - self.pid.feedforward_v(at.params.setpoint_c))
        self.autotune_finished.emit(st)

    @pyqtSlot()
    def acknowledge_alarm(self) -> None:
        self.safety.acknowledge()
        self._active_alarms = {c for c in self._active_alarms if c is AlarmCode.PSU_DEVIATION}

    # ------------------------------------------------------------------ loop

    @pyqtSlot()
    def step(self) -> ThermalSample:
        """Run one control period and emit (and return) its sample."""
        now = self._clock()

        # 1. temperature
        temp: float | None = None
        try:
            temp = float(self.thermometer.read_temperature())
            self._report("read temperature", None)
        except Exception as exc:  # noqa: BLE001
            self._report("read temperature", exc)
        if temp is not None and math.isfinite(temp):
            self._temp, self._temp_time = temp, now
            if not self._have_reading:
                self._have_reading = True
                if not self._psu_enabled and self._runner is None and not self.ramp.is_ramping:
                    self.ramp.reset(min(temp, self.safety.t_max_c))  # idle set point starts at room temperature
        elif temp is not None:
            self._temp_time = now  # a non-finite value is a "reading" that safety rejects as invalid

        # 2. program set point
        profile_segment = profile_phase = None
        if self._runner is not None:
            st = self._runner.status(now)
            program_sp = st.program_sp
            target_sp = self._runner.profile.segments[st.segment].ramp_to
            profile_segment, profile_phase = st.segment, st.phase.value
            ramping = st.phase.value == "ramp"
            if st.done:
                self._runner = None
                self.ramp.reset(program_sp)
                log.info("thermal: profile finished")
                self.profile_finished.emit()
        elif self._autotune is not None:
            program_sp = target_sp = self._autotune.params.setpoint_c
            ramping = False
        else:
            program_sp = self.ramp.program_sp(now)
            target_sp = self.ramp.target
            ramping = program_sp != target_sp

        # 3. PID -> clamp -> PSU
        dt = self.period_ms / 1000.0 if self._last_step is None else now - self._last_step
        self._last_step = now
        mode = "pid"
        if self._autotune is not None and self._psu_enabled and not self.safety.tripped:
            at = self._autotune
            v = self.safety.clamp_voltage(at.update(now, temp))
            if at.state.finished:
                self._end_autotune(None)  # done, timeout or deviation abort: the PID takes over below
                program_sp = target_sp = self.ramp.program_sp(now)
            else:
                mode = "autotune"
                try:
                    self.psu.set_voltage(v)
                    self._v_cmd = v
                    self._report("set voltage", None)
                except Exception as exc:  # noqa: BLE001
                    self._report("set voltage", exc)
                self.autotune_progress.emit(at.status())
        if mode == "pid" and self._psu_enabled and not self.safety.tripped:
            if temp is not None and math.isfinite(temp) and dt > 0:
                v = self.safety.clamp_voltage(self.pid.update(program_sp, temp, dt))
                try:
                    self.psu.set_voltage(v)
                    self._v_cmd = v
                    self._report("set voltage", None)
                except Exception as exc:  # noqa: BLE001
                    self._report("set voltage", exc)

        # 4. readback
        reading: PsuReading | None = None
        try:
            reading = self.psu.read_output()
            self._report("read PSU", None)
        except Exception as exc:  # noqa: BLE001
            self._report("read PSU", exc)
        try:
            psu_on = bool(self.psu.is_on)
        except Exception:  # noqa: BLE001
            psu_on = self._psu_enabled

        # 5. safety
        alarms = self.safety.check(
            SafetySample(
                # A failed read reports the last good value; its age drives STALE_TEMP.
                temperature_c=temp if temp is not None else self._temp,
                temperature_time=self._temp_time,
                psu_command_v=self._v_cmd,
                psu_output_on=psu_on,
                psu_reading=reading,
                psu_min_v=self.psu.v_min,
                now=now,
            )
        )
        if self.safety.tripped and (self._psu_enabled or psu_on):
            if self._autotune is not None:
                trip = "; ".join(a.message for a in alarms if a.is_trip)
                self._end_autotune(f"safety trip: {trip}", resume_pid=False)
                mode = "pid"
            self.safety.enforce(self.psu)
            self._psu_enabled = False
            self._v_cmd = 0.0
            self.pid.reset()
            try:
                psu_on = bool(self.psu.is_on)
            except Exception:  # noqa: BLE001
                psu_on = False

        active_now = {a.code for a in alarms if a.active}
        for a in alarms:
            if a.active and a.code not in self._active_alarms:
                self.alarm.emit(a)
        self._active_alarms = active_now | {a.code for a in alarms if a.is_trip}

        s = ThermalSample(
            t=now - self._t0,
            temperature_c=temp,
            target_sp_c=target_sp,
            program_sp_c=program_sp,
            v_cmd=self._v_cmd if self._psu_enabled else 0.0,
            v_read=None if reading is None else reading.voltage,
            i_read=None if reading is None else reading.current,
            ramping=ramping,
            psu_on=psu_on,
            alarms=tuple(alarms),
            profile_segment=profile_segment,
            profile_phase=profile_phase,
            mode=mode,
        )
        self.sample.emit(s)
        return s
