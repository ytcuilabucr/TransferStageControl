"""Simulated devices for development and tests (no hardware needed).

* :class:`SimLinearStage` / :class:`SimRotaryStage` — position is computed from
  velocity and elapsed ``clock()`` time each time it is read, so no background
  thread is needed. Moves run at the set velocity; ``stop(immediate=False)``
  decelerates linearly over ``stop_decel_s``.
* :class:`SimPowerSupply` — stores set points (clamped to 0–60 V / 0–5 A, the
  1685B range) and models a resistive load so that the current limit (CC mode)
  works.
* :class:`SimThermalPlant` — first-order heater model with optional dead time::

      dT/dt = (K * V**2 - (T - T_amb)) / tau

  It integrates exactly over piecewise-constant input and advances on each read.
  ``time_scale`` multiplies elapsed wall-clock time for accelerated tests.
* :class:`SimThermometer` — reads the plant with Gaussian noise.

Every simulator has a ``fault`` attribute: set it to an exception instance and
every command raises it (and stages report :attr:`MotorState.ERROR`), which lets
tests exercise error handling. Set it back to ``None`` to clear.
"""

from __future__ import annotations

import bisect
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .base import (
    Direction,
    LinearStage,
    MotorState,
    NotConnectedError,
    PowerSupply,
    PsuMode,
    PsuReading,
    RotaryStage,
    Thermometer,
)

Clock = Callable[[], float]


class _FaultMixin:
    fault: BaseException | None = None

    def _check_fault(self) -> None:
        if self.fault is not None:
            raise self.fault


# --------------------------------------------------------------------------- motion


@dataclass
class _Segment:
    state: MotorState  # MOVING, HOMING or STOPPING
    p0: float
    t0: float
    v: float  # signed velocity at t0
    target: float | None = None  # None = jog (runs until stopped or a limit)
    decel_s: float = 0.0  # STOPPING only


class _SimAxis:
    """Unit-agnostic single-axis motion engine. Not locked; the owning stage locks."""

    def __init__(
        self,
        *,
        max_velocity: float,
        velocity: float,
        position: float,
        lower: float,
        upper: float,
        home_position: float,
        stop_decel_s: float,
        clock: Clock,
    ) -> None:
        if not lower < upper:
            raise ValueError("lower travel limit must be < upper")
        if max_velocity <= 0:
            raise ValueError("max_velocity must be > 0")
        self.max_velocity = float(max_velocity)
        self.velocity = min(float(velocity), self.max_velocity)
        self.pos = float(position)
        self.lower = float(lower)
        self.upper = float(upper)
        self.home_position = float(home_position)
        self.stop_decel_s = float(stop_decel_s)
        self.clock = clock
        self.seg: _Segment | None = None
        self.homed = False

    @property
    def state(self) -> MotorState:
        self.update()
        return self.seg.state if self.seg else MotorState.IDLE

    def update(self, now: float | None = None) -> None:
        seg = self.seg
        if seg is None:
            return
        now = self.clock() if now is None else now
        dt = max(0.0, now - seg.t0)
        done = False
        if seg.state is MotorState.STOPPING:
            T = seg.decel_s
            if dt >= T:
                pos, done = seg.p0 + seg.v * T / 2.0, True
            else:
                pos = seg.p0 + seg.v * dt - seg.v * dt * dt / (2.0 * T)
        else:
            pos = seg.p0 + seg.v * dt
            if seg.target is not None and (pos - seg.target) * seg.v >= 0:
                pos, done = seg.target, True
        if (pos <= self.lower and seg.v < 0) or (pos >= self.upper and seg.v > 0):
            pos, done = min(max(pos, self.lower), self.upper), True
        self.pos = pos
        if done:
            if seg.state is MotorState.HOMING and pos == seg.target:
                self.homed = True
            self.seg = None

    def _current_velocity(self, now: float) -> float:
        seg = self.seg
        if seg is None:
            return 0.0
        if seg.state is MotorState.STOPPING:
            return seg.v * max(0.0, 1.0 - (now - seg.t0) / seg.decel_s)
        return seg.v

    def set_velocity(self, v: float) -> None:
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
            raise ValueError(f"velocity must be a positive number, got {v!r}")
        self.velocity = min(float(v), self.max_velocity)

    def start_move(self, target: float, state: MotorState = MotorState.MOVING) -> None:
        if not (math.isfinite(target) and self.lower <= target <= self.upper):
            raise ValueError(f"target {target} outside travel range [{self.lower}, {self.upper}]")
        now = self.clock()
        self.update(now)
        if target == self.pos:
            self.seg = None
            if state is MotorState.HOMING:
                self.homed = True
            return
        v = math.copysign(self.velocity, target - self.pos)
        self.seg = _Segment(state, self.pos, now, v, target=target)

    def start_jog(self, direction: Direction, velocity: float | None) -> None:
        v = self.velocity if velocity is None else min(abs(float(velocity)), self.max_velocity)
        if not v > 0:
            raise ValueError(f"jog velocity must be > 0, got {velocity!r}")
        now = self.clock()
        self.update(now)
        self.seg = _Segment(MotorState.MOVING, self.pos, now, int(Direction(direction)) * v)

    def stop(self, immediate: bool) -> None:
        now = self.clock()
        self.update(now)
        if self.seg is None:
            return
        if immediate or self.stop_decel_s <= 0:
            self.seg = None
        elif self.seg.state is not MotorState.STOPPING:
            self.seg = _Segment(
                MotorState.STOPPING, self.pos, now, self._current_velocity(now), decel_s=self.stop_decel_s
            )


class _SimStageBase(_FaultMixin):
    """Shared implementation; subclasses expose it under unit-specific names."""

    _axis: _SimAxis
    _connected: bool

    def _require(self) -> None:
        self._check_fault()
        if not self._connected:
            raise NotConnectedError(f"{type(self).__name__} is not connected")

    def _state(self) -> MotorState:
        with self._lock:
            if self.fault is not None:
                return MotorState.ERROR
            if not self._connected:
                return MotorState.DISCONNECTED
            return self._axis.state

    def _wait(self, timeout_s: float | None) -> None:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            with self._lock:
                self._check_fault()
                if not self._axis.state.is_busy:
                    return
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"{type(self).__name__}: motion did not finish within {timeout_s} s")
            time.sleep(0.005)

    def connect(self) -> None:
        with self._lock:
            self._check_fault()
            self._connected = True

    def close(self) -> None:
        with self._lock:
            if self._connected:
                self._axis.stop(immediate=True)
            self._connected = False

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def is_homed(self) -> bool:
        with self._lock:
            return self._axis.homed

    def _move_to(self, target: float, wait: bool, timeout_s: float | None) -> None:
        with self._lock:
            self._require()
            self._axis.start_move(float(target))
        if wait:
            self._wait(timeout_s)

    def _home(self, wait: bool, timeout_s: float | None) -> None:
        with self._lock:
            self._require()
            self._axis.start_move(self._axis.home_position, MotorState.HOMING)
        if wait:
            self._wait(timeout_s)

    def _jog(self, direction: Direction, velocity: float | None) -> None:
        with self._lock:
            self._require()
            self._axis.start_jog(direction, velocity)

    def _set_velocity(self, v: float) -> None:
        with self._lock:
            self._require()
            self._axis.set_velocity(v)

    def stop(self, immediate: bool = True) -> None:
        with self._lock:
            self._require()
            self._axis.stop(immediate)


class SimLinearStage(_SimStageBase, LinearStage):
    """Simulated Z stage. Defaults resemble a Z825B on a KCube DC Servo (25 mm, 2.6 mm/s)."""

    def __init__(
        self,
        *,
        max_velocity_um_s: float = 2600.0,
        velocity_um_s: float = 500.0,
        position_um: float = 0.0,
        min_um: float = 0.0,
        max_um: float = 25000.0,
        stop_decel_s: float = 0.1,
        clock: Clock = time.monotonic,
    ) -> None:
        LinearStage.__init__(self)
        self._connected = False
        self._axis = _SimAxis(
            max_velocity=max_velocity_um_s,
            velocity=velocity_um_s,
            position=position_um,
            lower=min_um,
            upper=max_um,
            home_position=min_um,
            stop_decel_s=stop_decel_s,
            clock=clock,
        )

    @property
    def position_um(self) -> float:
        with self._lock:
            self._axis.update()
            return self._axis.pos

    @property
    def state(self) -> MotorState:
        return self._state()

    @property
    def max_velocity_um_s(self) -> float:
        with self._lock:
            return self._axis.max_velocity

    @property
    def velocity_um_s(self) -> float:
        with self._lock:
            return self._axis.velocity

    def set_velocity(self, um_s: float) -> None:
        self._set_velocity(um_s)

    def move_to(self, um: float, wait: bool = False, timeout_s: float | None = None) -> None:
        self._move_to(um, wait, timeout_s)

    def jog(self, direction: Direction, velocity_um_s: float | None = None) -> None:
        self._jog(direction, velocity_um_s)

    def home(self, wait: bool = False, timeout_s: float | None = None) -> None:
        self._home(wait, timeout_s)


class SimRotaryStage(_SimStageBase, RotaryStage):
    """Simulated cage rotator. Unlimited travel by default; homes to 0°."""

    def __init__(
        self,
        *,
        max_velocity_deg_s: float = 10.0,
        velocity_deg_s: float = 10.0,
        angle_deg: float = 0.0,
        min_deg: float = -1e6,
        max_deg: float = 1e6,
        stop_decel_s: float = 0.1,
        clock: Clock = time.monotonic,
    ) -> None:
        RotaryStage.__init__(self)
        self._connected = False
        self._axis = _SimAxis(
            max_velocity=max_velocity_deg_s,
            velocity=velocity_deg_s,
            position=angle_deg,
            lower=min_deg,
            upper=max_deg,
            home_position=0.0,
            stop_decel_s=stop_decel_s,
            clock=clock,
        )

    @property
    def angle_deg(self) -> float:
        with self._lock:
            self._axis.update()
            return self._axis.pos

    @property
    def state(self) -> MotorState:
        return self._state()

    @property
    def max_velocity_deg_s(self) -> float:
        with self._lock:
            return self._axis.max_velocity

    @property
    def velocity_deg_s(self) -> float:
        with self._lock:
            return self._axis.velocity

    def set_velocity(self, deg_s: float) -> None:
        self._set_velocity(deg_s)

    def move_to(self, deg: float, wait: bool = False, timeout_s: float | None = None) -> None:
        self._move_to(deg, wait, timeout_s)

    def jog(self, direction: Direction, velocity_deg_s: float | None = None) -> None:
        self._jog(direction, velocity_deg_s)

    def home(self, wait: bool = False, timeout_s: float | None = None) -> None:
        self._home(wait, timeout_s)


# --------------------------------------------------------------------------- power supply


class SimPowerSupply(_FaultMixin, PowerSupply):
    """Simulated B&K 1685B (0–60 V, 0–5 A) driving a resistive load of ``load_ohm``.

    ``load_ohm=None`` models an open circuit (no current flows).
    """

    V_RANGE = (0.0, 60.0)
    I_RANGE = (0.0, 5.0)

    def __init__(self, *, load_ohm: float | None = 8.0, current_limit_a: float = 5.0) -> None:
        super().__init__()
        if load_ohm is not None and load_ohm <= 0:
            raise ValueError("load_ohm must be > 0 or None")
        self.load_ohm = load_ohm
        self._connected = False
        self._v_set = 0.0
        self._i_set = self._clamp(current_limit_a, self.I_RANGE)
        self._on = False
        self._listeners: list[Callable[[], None]] = []

    @staticmethod
    def _clamp(x: float, rng: tuple[float, float]) -> float:
        x = float(x)
        if not math.isfinite(x):
            raise ValueError(f"set point must be finite, got {x!r}")
        return min(max(x, rng[0]), rng[1])

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register ``callback()``, called (without the PSU lock held) after every output change."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _require(self) -> None:
        self._check_fault()
        if not self._connected:
            raise NotConnectedError("SimPowerSupply is not connected")

    def connect(self) -> None:
        with self._lock:
            self._check_fault()
            self._connected = True

    def close(self) -> None:
        with self._lock:
            self._connected = False

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def set_voltage(self, v: float) -> None:
        with self._lock:
            self._require()
            self._v_set = self._clamp(v, self.V_RANGE)
        self._notify()

    def set_current(self, a: float) -> None:
        with self._lock:
            self._require()
            self._i_set = self._clamp(a, self.I_RANGE)
        self._notify()

    def output(self, on: bool) -> None:
        with self._lock:
            self._require()
            self._on = bool(on)
        self._notify()

    @property
    def is_on(self) -> bool:
        with self._lock:
            return self._on

    @property
    def voltage_setpoint(self) -> float:
        with self._lock:
            return self._v_set

    @property
    def current_setpoint(self) -> float:
        with self._lock:
            return self._i_set

    def _actual(self) -> PsuReading:
        if not self._on:
            return PsuReading(0.0, 0.0, PsuMode.OFF)
        if self.load_ohm is None:
            return PsuReading(self._v_set, 0.0, PsuMode.CV)
        if self._v_set / self.load_ohm > self._i_set:
            return PsuReading(self._i_set * self.load_ohm, self._i_set, PsuMode.CC)
        return PsuReading(self._v_set, self._v_set / self.load_ohm, PsuMode.CV)

    def output_voltage(self) -> float:
        """Voltage actually across the load (0 when off). Used by the thermal plant; no fault check."""
        with self._lock:
            return self._actual().voltage

    def read_output(self) -> PsuReading:
        with self._lock:
            self._require()
            return self._actual()


# --------------------------------------------------------------------------- thermal


class SimThermalPlant:
    """First-order heater + sample model, optionally with dead time.

    ``k`` is the steady-state rise per V² (°C/V²), ``tau_s`` the time constant in
    (simulated) seconds. With the defaults, 12 V gives ~108 °C and 24 V ~368 °C at
    steady state. The input is ``source``: a :class:`SimPowerSupply` (coupled via
    its listener so set-point changes are integrated exactly), any callable
    returning volts, or ``None`` (always 0 V).
    """

    def __init__(
        self,
        source: SimPowerSupply | Callable[[], float] | None = None,
        *,
        k: float = 0.6,
        tau_s: float = 120.0,
        t_amb_c: float = 22.0,
        t_initial_c: float | None = None,
        delay_s: float = 0.0,
        time_scale: float = 1.0,
        clock: Clock = time.monotonic,
    ) -> None:
        if tau_s <= 0 or time_scale <= 0 or delay_s < 0 or k < 0:
            raise ValueError("require tau_s > 0, time_scale > 0, delay_s >= 0, k >= 0")
        self._lock = threading.RLock()
        self.k = float(k)
        self.tau_s = float(tau_s)
        self.t_amb_c = float(t_amb_c)
        self.delay_s = float(delay_s)
        self.time_scale = float(time_scale)
        self._clock = clock
        self._coupled = isinstance(source, SimPowerSupply)
        if isinstance(source, SimPowerSupply):
            self._source: Callable[[], float] = source.output_voltage
            source.add_listener(self._on_input_change)
        elif source is None:
            self._source = lambda: 0.0
        else:
            self._source = source
        self._wall0 = clock()
        self._sim_t = 0.0
        self._temp = self.t_amb_c if t_initial_c is None else float(t_initial_c)
        # Input history as parallel lists of (sim time, volts); piecewise constant.
        self._hist_t: list[float] = [-math.inf]
        self._hist_v: list[float] = [float(self._source())]

    def steady_state_c(self, volts: float) -> float:
        return self.t_amb_c + self.k * volts * volts

    @property
    def sim_time_s(self) -> float:
        with self._lock:
            self._advance()
            return self._sim_t

    def temperature_c(self) -> float:
        """Advance the model to now and return the true (noise-free) temperature."""
        with self._lock:
            self._advance()
            return self._temp

    def _input_at(self, s: float) -> float:
        i = bisect.bisect_right(self._hist_t, s) - 1
        return self._hist_v[max(i, 0)]

    def _advance(self) -> None:
        s1 = (self._clock() - self._wall0) * self.time_scale
        s0 = self._sim_t
        if s1 <= s0:
            return
        d = self.delay_s
        # Breakpoints where the delayed input changes inside (s0, s1).
        points = [t + d for t in self._hist_t if s0 < t + d < s1]
        t = s0
        for bp in points + [s1]:
            v = self._input_at(t - d)
            t_ss = self.steady_state_c(v)
            self._temp = t_ss + (self._temp - t_ss) * math.exp(-(bp - t) / self.tau_s)
            t = bp
        self._sim_t = s1
        if not self._coupled:
            # Plain callable source: sample it now; it is assumed constant until the next read.
            self._record(s1, float(self._source()))
        # Drop history no longer needed (keep the last entry at or before s1 - d).
        keep = max(bisect.bisect_right(self._hist_t, s1 - d) - 1, 0)
        if keep:
            del self._hist_t[:keep]
            del self._hist_v[:keep]

    def _record(self, s: float, v: float) -> None:
        if v == self._hist_v[-1]:
            return
        if s <= self._hist_t[-1]:
            self._hist_v[-1] = v
        else:
            self._hist_t.append(s)
            self._hist_v.append(v)

    def _on_input_change(self) -> None:
        with self._lock:
            self._advance()
            self._record(self._sim_t, float(self._source()))


class SimThermometer(_FaultMixin, Thermometer):
    """Reads :class:`SimThermalPlant` with Gaussian noise of ``noise_c`` (1 σ, °C)."""

    def __init__(self, plant: SimThermalPlant, *, noise_c: float = 0.05, seed: int | None = None) -> None:
        super().__init__()
        self.plant = plant
        self.noise_c = float(noise_c)
        self._rng = random.Random(seed)
        self._connected = False

    def connect(self) -> None:
        with self._lock:
            self._check_fault()
            self._connected = True

    def close(self) -> None:
        with self._lock:
            self._connected = False

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def read_temperature(self) -> float:
        with self._lock:
            self._check_fault()
            if not self._connected:
                raise NotConnectedError("SimThermometer is not connected")
            t = self.plant.temperature_c()
            return t + (self._rng.gauss(0.0, self.noise_c) if self.noise_c > 0 else 0.0)
