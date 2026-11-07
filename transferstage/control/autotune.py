"""Relay (Åström–Hägglund) PID autotune.

Around the set point the PSU voltage is switched between ``bias + amplitude`` and
``bias - amplitude`` (clamped to 0–24 V / ``limits.v_max``) with a hysteresis band::

    T < SP - h  ->  high voltage        T > SP + h  ->  low voltage

This drives the temperature into a limit cycle. After ``discard_cycles`` start-up
cycles, ``cycles`` full periods are measured; from the mean peak-to-peak amplitude
``2a`` and mean period ``Pu`` (describing-function approximation, corrected for
the hysteresis ``h``)::

    Ku = 4 d / (π · sqrt(a² − h²))      d = (V_high − V_low) / 2

Gains (``Kc`` in V/°C, ``Ti``/``Td`` in minutes, the same form as :class:`PID`):

=====================  ==========  ===========  ==========
rule                   Kc          Ti           Td
=====================  ==========  ===========  ==========
Ziegler–Nichols PI     0.45 Ku     Pu / 1.2     0
Ziegler–Nichols PID    0.6 Ku      Pu / 2       Pu / 8
Tyreus–Luyben PI       Ku / 3.2    2.2 Pu       0
Tyreus–Luyben PID      Ku / 2.2    2.2 Pu       Pu / 6.3
=====================  ==========  ===========  ==========

Ziegler–Nichols is aggressive (expect overshoot); Tyreus–Luyben is the
conservative choice for a heater.

:class:`RelayAutotune` is pure logic: feed it ``update(now, temperature)`` once
per control period and send the returned voltage to the PSU (through
``SafetyMonitor.clamp_voltage``). It aborts by itself on timeout or when the
temperature leaves ``SP + max_deviation_c``; safety trips and user cancel are
handled by the caller (:class:`~transferstage.control.thermal.ThermalWorker`),
which calls :meth:`RelayAutotune.abort`.
"""

from __future__ import annotations

import enum
import math
import statistics
from dataclasses import dataclass, field

from ..config import HARD_V_MAX


class AutotuneState(enum.Enum):
    APPROACH = "approach"  # waiting for the first set-point crossing
    RELAY = "relay"  # oscillating, measuring
    DONE = "done"
    ABORTED = "aborted"

    @property
    def finished(self) -> bool:
        return self in (AutotuneState.DONE, AutotuneState.ABORTED)


@dataclass(frozen=True)
class AutotuneParams:
    setpoint_c: float
    amplitude_v: float = 3.0
    bias_v: float | None = None  # None: the caller substitutes the present output voltage
    hysteresis_c: float = 0.5
    cycles: int = 3  # periods measured after the discarded ones
    discard_cycles: int = 1  # start-up periods ignored
    timeout_s: float = 3600.0
    max_deviation_c: float = 30.0  # abort if T > SP + this

    def validate(self, t_max_c: float) -> None:
        def bad(msg: str) -> None:
            raise ValueError(msg)

        for name in ("setpoint_c", "amplitude_v", "hysteresis_c", "timeout_s", "max_deviation_c"):
            if not math.isfinite(float(getattr(self, name))):
                bad(f"{name} must be finite")
        if self.bias_v is not None and not (math.isfinite(self.bias_v) and self.bias_v >= 0):
            bad("bias_v must be >= 0 V")
        if not self.setpoint_c < t_max_c:
            bad(f"set point {self.setpoint_c:g} °C must be below the {t_max_c:g} °C limit")
        if self.amplitude_v <= 0:
            bad("amplitude_v must be > 0 V")
        if self.hysteresis_c < 0:
            bad("hysteresis_c must be >= 0 °C")
        if int(self.cycles) < 1 or int(self.discard_cycles) < 0:
            bad("cycles must be >= 1 and discard_cycles >= 0")
        if self.timeout_s <= 0 or self.max_deviation_c <= self.hysteresis_c:
            bad("timeout_s must be > 0 and max_deviation_c > hysteresis_c")


@dataclass(frozen=True)
class PidGains:
    kc: float
    ti_min: float
    td_min: float = 0.0


RULES = ("Ziegler–Nichols PI", "Ziegler–Nichols PID", "Tyreus–Luyben PI", "Tyreus–Luyben PID")
RECOMMENDED_RULE = "Tyreus–Luyben PI"


def gains_from_ultimate(ku: float, pu_s: float) -> dict[str, PidGains]:
    """PID gains (Ti, Td in minutes) from the ultimate gain ``ku`` (V/°C) and period ``pu_s`` (s)."""
    pu = pu_s / 60.0
    return {
        "Ziegler–Nichols PI": PidGains(0.45 * ku, pu / 1.2, 0.0),
        "Ziegler–Nichols PID": PidGains(0.6 * ku, pu / 2.0, pu / 8.0),
        "Tyreus–Luyben PI": PidGains(ku / 3.2, 2.2 * pu, 0.0),
        "Tyreus–Luyben PID": PidGains(ku / 2.2, 2.2 * pu, pu / 6.3),
    }


@dataclass(frozen=True)
class AutotuneResult:
    ku: float  # V/°C
    pu_s: float  # s
    amplitude_c: float  # oscillation half-amplitude a
    relay_d_v: float  # relay half-amplitude d
    period_spread: float  # (max - min) / mean of the measured periods; small = steady oscillation
    gains: dict[str, PidGains] = field(default_factory=dict)


@dataclass(frozen=True)
class AutotuneStatus:
    state: AutotuneState
    cycles_done: int  # measured periods completed (excludes discarded ones)
    cycles_total: int
    elapsed_s: float
    output_v: float
    message: str = ""
    result: AutotuneResult | None = None

    @property
    def fraction(self) -> float:
        if self.state is AutotuneState.DONE:
            return 1.0
        return min(self.cycles_done / max(self.cycles_total, 1), 1.0)


class RelayAutotune:
    def __init__(self, params: AutotuneParams, *, v_max: float = HARD_V_MAX, t_max_c: float = 300.0) -> None:
        if params.bias_v is None:
            raise ValueError("bias_v must be set (the worker substitutes the present output)")
        params.validate(t_max_c)
        self.params = params
        v_max = min(float(v_max), HARD_V_MAX)
        self.v_high = min(params.bias_v + params.amplitude_v, v_max)
        self.v_low = max(params.bias_v - params.amplitude_v, 0.0)
        if not self.v_high > self.v_low:
            raise ValueError(f"relay levels {self.v_low:g}–{self.v_high:g} V are empty; check bias and amplitude")
        self.d = (self.v_high - self.v_low) / 2.0
        self.state = AutotuneState.APPROACH
        self.message = ""
        self.result: AutotuneResult | None = None
        self._t0: float | None = None
        self._now = 0.0
        self._high: bool | None = None
        self._up_times: list[float] = []  # times of low -> high switches
        self._peaks: list[float] = []  # max T in each low phase
        self._troughs: list[float] = []  # min T in each high phase
        self._ext: float | None = None  # running extreme of the current phase

    # ------------------------------------------------------------------ status

    @property
    def output_v(self) -> float:
        if self.state.finished or self._high is None:
            return self.v_low if self._high is False else self.v_high
        return self.v_high if self._high else self.v_low

    @property
    def cycles_done(self) -> int:
        return max(len(self._up_times) - 1 - self.params.discard_cycles, 0)

    def status(self) -> AutotuneStatus:
        elapsed = 0.0 if self._t0 is None else self._now - self._t0
        return AutotuneStatus(
            self.state, min(self.cycles_done, self.params.cycles), self.params.cycles, elapsed,
            self.output_v, self.message, self.result,
        )  # fmt: skip

    def abort(self, reason: str) -> None:
        if not self.state.finished:
            self.state = AutotuneState.ABORTED
            self.message = reason

    # ------------------------------------------------------------------ loop

    def update(self, now: float, temperature_c: float | None) -> float:
        """Advance with a new reading; return the voltage to command. 0 V once finished."""
        if self._t0 is None:
            self._t0 = now
        self._now = now
        if self.state.finished:
            return 0.0
        p = self.params
        if now - self._t0 > p.timeout_s:
            self.abort(f"timeout after {p.timeout_s:g} s ({self.cycles_done}/{p.cycles} cycles)")
            return 0.0
        if temperature_c is None or not math.isfinite(temperature_c):
            return self.output_v  # hold the relay; the safety monitor handles stale readings
        t = float(temperature_c)
        if t > p.setpoint_c + p.max_deviation_c:
            self.abort(f"temperature {t:.1f} °C exceeded set point + {p.max_deviation_c:g} °C")
            return 0.0

        sp, h = p.setpoint_c, p.hysteresis_c
        if self._high is None:
            self._high = t < sp
        if self._high:
            self._ext = t if self._ext is None else min(self._ext, t)
            if t > sp + h:
                self._switch(now, high=False)
        else:
            self._ext = t if self._ext is None else max(self._ext, t)
            if t < sp - h:
                self._switch(now, high=True)

        if self.cycles_done >= p.cycles:
            self._finish()
            return 0.0
        return self.output_v

    def _switch(self, now: float, *, high: bool) -> None:
        if self.state is AutotuneState.APPROACH:
            self.state = AutotuneState.RELAY  # extremes before the first crossing are not oscillation
        elif self._ext is not None:
            (self._troughs if not high else self._peaks).append(self._ext)
        if high:
            self._up_times.append(now)
        self._high = high
        self._ext = None

    def _finish(self) -> None:
        p = self.params
        n = p.cycles
        periods = [b - a for a, b in zip(self._up_times[:-1], self._up_times[1:])][-n:]
        # A full period ending at up_times[k] contains one peak and one trough; take the last n of each.
        peaks, troughs = self._peaks[-n:], self._troughs[-n:]
        a = (statistics.fmean(peaks) - statistics.fmean(troughs)) / 2.0
        pu = statistics.fmean(periods)
        h = p.hysteresis_c
        if a <= 0 or pu <= 0:
            self.abort("no usable oscillation measured")
            return
        root = math.sqrt(a * a - h * h) if a > h else a
        ku = 4.0 * self.d / (math.pi * root)
        spread = (max(periods) - min(periods)) / pu
        self.result = AutotuneResult(ku, pu, a, self.d, spread, gains_from_ultimate(ku, pu))
        self.state = AutotuneState.DONE
        self.message = f"Ku = {ku:.3g} V/°C, Pu = {pu:.1f} s"
