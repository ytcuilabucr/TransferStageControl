"""PID controller with the NI LabVIEW ``PID.vi`` parameterization.

    u = FF(SP, dSP/dt)  +  Kc * ( e  +  (1/Ti) ∫ e dt  +  Td * d(-PV)/dt )        Ti, Td in minutes

* Integral: trapezoidal, stored in output units (so gain changes are bumpless).
  ``Ti = 0`` disables the integral action (and clears the integrator).
* Derivative on measurement (no kick on set-point steps); skipped on the first
  update after :meth:`PID.reset`.
* Output clamped to ``[output_low, output_high]``; ``output_high`` may never exceed
  the hard 24 V limit. Anti-windup by conditional integration: the integrator is
  frozen while the output is saturated and the error would drive it further in,
  and feedforward + integrator is kept inside the output range.
* Feedforward (optional, :class:`HeaterFeedforward`): the voltage the heater model
  ``tau * dT/dt = k * V**2 - (T - T_amb)`` needs to follow the set point, i.e. the
  hold voltage plus the extra (or reduced) power a ramp needs. The integrator then
  only holds the model error, so it does not have to wind up during a ramp or
  rebuild from zero after the output sat at 0 V on a cool-down. ``dSP/dt`` is taken
  from successive set points (the ramp generator's program SP).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

from ..config import HARD_V_MAX, PidConfig


@dataclass(frozen=True)
class PidTerms:
    """Breakdown of the last update (for logging / GUI)."""

    p: float
    i: float
    d: float
    output: float
    saturated: bool
    ff: float = 0.0


def _finite(x: float, name: str) -> float:
    x = float(x)
    if not math.isfinite(x):
        raise ValueError(f"{name} must be finite, got {x!r}")
    return x


@dataclass(frozen=True)
class HeaterFeedforward:
    """Heater voltage ``V = sqrt((SP + tau_s * dSP/dt - t_amb_c) / k_c_per_v2)``; 0 when that is negative.

    ``k_c_per_v2`` is the steady-state rise per V² and ``tau_s`` the thermal time
    constant (0 = hold voltage only, no ramp term).
    """

    k_c_per_v2: float
    t_amb_c: float = 22.0
    tau_s: float = 0.0

    def __post_init__(self) -> None:
        if not (math.isfinite(self.k_c_per_v2) and self.k_c_per_v2 > 0):
            raise ValueError("feedforward needs a finite k_c_per_v2 > 0")
        if not (math.isfinite(self.t_amb_c) and math.isfinite(self.tau_s) and self.tau_s >= 0):
            raise ValueError("feedforward needs a finite t_amb_c and a finite tau_s >= 0")

    def __call__(self, setpoint_c: float, rate_c_per_s: float = 0.0) -> float:
        target = setpoint_c + self.tau_s * rate_c_per_s - self.t_amb_c
        return math.sqrt(max(target, 0.0) / self.k_c_per_v2)


class PID:
    def __init__(
        self,
        kc: float,
        ti_min: float,
        td_min: float = 0.0,
        *,
        output_low: float = 0.0,
        output_high: float = HARD_V_MAX,
        feedforward: HeaterFeedforward | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.feedforward = feedforward
        self.set_tunings(kc, ti_min, td_min)
        self.set_output_limits(output_low, output_high)
        self.reset()

    @classmethod
    def from_config(cls, cfg: PidConfig) -> PID:
        ff = None
        if cfg.ff_k_c_per_v2 > 0:
            ff = HeaterFeedforward(cfg.ff_k_c_per_v2, cfg.ff_t_amb_c, cfg.ff_tau_s)
        return cls(
            cfg.kc, cfg.ti_min, cfg.td_min, output_low=cfg.output_low, output_high=cfg.output_high, feedforward=ff
        )

    def feedforward_v(self, setpoint: float, rate_c_per_s: float = 0.0) -> float:
        """Feedforward output (0 without a model), clamped to the output range."""
        if self.feedforward is None:
            return 0.0
        return min(max(self.feedforward(setpoint, rate_c_per_s), self.output_low), self.output_high)

    # ------------------------------------------------------------------ settings

    def set_tunings(self, kc: float, ti_min: float, td_min: float = 0.0) -> None:
        kc = _finite(kc, "kc")
        ti_min = _finite(ti_min, "ti_min")
        td_min = _finite(td_min, "td_min")
        if ti_min < 0 or td_min < 0:
            raise ValueError("ti_min and td_min must be >= 0")
        with self._lock:
            self.kc, self.ti_min, self.td_min = kc, ti_min, td_min
            if ti_min == 0:
                self._integral = 0.0

    def set_output_limits(self, low: float, high: float) -> None:
        low = _finite(low, "output_low")
        high = _finite(high, "output_high")
        if high > HARD_V_MAX:
            raise ValueError(f"output_high {high} V exceeds the hard limit of {HARD_V_MAX} V")
        if not low < high:
            raise ValueError("output_low must be < output_high")
        with self._lock:
            self.output_low, self.output_high = low, high
            if hasattr(self, "_integral"):
                floor = low - high if self.feedforward is not None else low
                self._integral = min(max(self._integral, floor), high)

    # ------------------------------------------------------------------ operation

    def reset(self, integral: float = 0.0) -> None:
        """Clear the controller state (LabVIEW "reinitialize?"). ``integral`` presets the I term.

        With feedforward the I term is a correction around it and may be negative.
        """
        with self._lock:
            lo, hi = self.output_low, self.output_high
            floor = lo - hi if self.feedforward is not None else lo
            self._integral = min(max(_finite(integral, "integral"), floor), hi)
            if self.ti_min == 0:
                self._integral = 0.0
            self._prev_error: float | None = None
            self._prev_pv: float | None = None
            self._prev_sp: float | None = None
            self.output = self.output_low
            self.last_terms = PidTerms(0.0, self._integral, 0.0, self.output, False)

    @property
    def integral(self) -> float:
        with self._lock:
            return self._integral

    def update(self, setpoint: float, pv: float, dt_s: float) -> float:
        """Advance the controller by ``dt_s`` seconds and return the clamped output."""
        setpoint = _finite(setpoint, "setpoint")
        pv = _finite(pv, "pv")
        dt_s = _finite(dt_s, "dt_s")
        if dt_s <= 0:
            raise ValueError(f"dt_s must be > 0, got {dt_s}")
        dt_min = dt_s / 60.0

        with self._lock:
            lo, hi = self.output_low, self.output_high
            error = setpoint - pv
            p = self.kc * error

            d = 0.0
            if self.td_min > 0 and self._prev_pv is not None:
                d = -self.kc * self.td_min * (pv - self._prev_pv) / dt_min

            rate = 0.0 if self._prev_sp is None else (setpoint - self._prev_sp) / dt_s
            ff = self.feedforward_v(setpoint, rate)
            i = self._integral
            if self.ti_min > 0:
                e_avg = error if self._prev_error is None else 0.5 * (error + self._prev_error)
                candidate = i + self.kc / self.ti_min * e_avg * dt_min
                u = ff + p + candidate + d
                winding_up = u > hi and candidate > i
                winding_down = u < lo and candidate < i
                if not (winding_up or winding_down):
                    i = min(max(candidate, lo - ff), hi - ff)
            self._integral = i

            raw = ff + p + i + d
            out = min(max(raw, lo), hi)
            self._prev_error = error
            self._prev_pv = pv
            self._prev_sp = setpoint
            self.output = out
            self.last_terms = PidTerms(p, i, d, out, out != raw, ff)
            return out
