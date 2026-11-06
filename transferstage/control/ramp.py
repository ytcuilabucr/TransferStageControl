"""Linear set-point ramp (the LabVIEW "Ramp" logic of ThermalControl.vi).

The program set point moves from ``original_sp`` toward ``target`` at
``rate_deg_min``; a rate of 0 is an immediate step. Targets are clamped to the
hard 300 °C limit (or a lower ``max_c``).
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from ..config import HARD_T_MAX_C

Clock = Callable[[], float]


class SetpointRamp:
    def __init__(self, initial_c: float = 22.0, *, max_c: float = HARD_T_MAX_C, clock: Clock = time.monotonic) -> None:
        if not 0 < max_c <= HARD_T_MAX_C:
            raise ValueError(f"max_c must be in (0, {HARD_T_MAX_C}] °C")
        self._lock = threading.RLock()
        self.max_c = float(max_c)
        self._clock = clock
        self.reset(initial_c)

    def _clamp(self, value: float, name: str) -> float:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
        return min(value, self.max_c)

    def reset(self, value_c: float) -> None:
        """Hold at ``value_c`` (no ramp in progress)."""
        with self._lock:
            v = self._clamp(value_c, "value_c")
            self.original_sp = v
            self.target = v
            self.rate_deg_min = 0.0
            self.ascending = True
            self._t0 = self._clock()

    def set_target(self, target_c: float, rate_deg_min: float, current_value: float | None = None) -> None:
        """Start a ramp to ``target_c``.

        ``current_value`` is the ramp's starting point; ``None`` continues from the
        current program set point (smooth retarget mid-ramp).
        """
        rate = float(rate_deg_min)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError(f"rate_deg_min must be >= 0, got {rate_deg_min!r}")
        with self._lock:
            now = self._clock()
            start = self.program_sp(now) if current_value is None else self._clamp(current_value, "current_value")
            self.original_sp = start
            self.target = self._clamp(target_c, "target_c")
            self.rate_deg_min = rate
            self.ascending = self.target >= start
            self._t0 = now

    def program_sp(self, now: float | None = None) -> float:
        """The set point the controller should use at time ``now`` (default: clock())."""
        with self._lock:
            if self.rate_deg_min == 0:
                return self.target
            t = self._clock() if now is None else now
            delta = self.rate_deg_min * max(t - self._t0, 0.0) / 60.0
            if self.ascending:
                return min(self.original_sp + delta, self.target)
            return max(self.original_sp - delta, self.target)

    @property
    def is_ramping(self) -> bool:
        with self._lock:
            return self.program_sp() != self.target

    def remaining_s(self, now: float | None = None) -> float:
        with self._lock:
            if self.rate_deg_min == 0:
                return 0.0
            return abs(self.target - self.program_sp(now)) / self.rate_deg_min * 60.0
