"""Safety rules and the emergency shutdown sequence.

Hard limits (300 °C, 24 V) come from code constants and apply even if a config
object somehow carries higher values.

Rules checked by :meth:`SafetyMonitor.check`:

* ``OVER_TEMP``  (trip)    temperature >= t_max_c.
* ``STALE_TEMP`` (trip)    no fresh, finite temperature reading for > stale_temp_s.
* ``PSU_DEVIATION`` (warn) PSU readback differs from the command by > tolerance for
  longer than ``deviation_hold_s`` (the readback lags a step while the supply slews).
  Not raised while the supply is current-limited, where a lower voltage is expected.
  With the output on, the expected voltage is at least the supply's ``v_min`` floor.

Trips latch until :meth:`SafetyMonitor.acknowledge`; while latched the PSU must
stay off (:meth:`SafetyMonitor.enforce` does that). Voltage commands go through
:meth:`clamp_voltage`; Z moves through :meth:`z_move_allowed`.
"""

from __future__ import annotations

import enum
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..config import HARD_T_MAX_C, HARD_V_MAX, LimitsConfig
from ..drivers.base import PowerSupply, PsuMode, PsuReading

log = logging.getLogger(__name__)

Clock = Callable[[], float]


class Severity(enum.Enum):
    WARNING = "warning"
    TRIP = "trip"


class AlarmCode(enum.Enum):
    OVER_TEMP = "over_temp"
    STALE_TEMP = "stale_temp"
    PSU_DEVIATION = "psu_deviation"


@dataclass(frozen=True)
class Alarm:
    code: AlarmCode
    severity: Severity
    message: str
    active: bool = True  # False: condition cleared, alarm only latched

    @property
    def is_trip(self) -> bool:
        return self.severity is Severity.TRIP


@dataclass(frozen=True)
class SafetySample:
    """What the thermal loop knows at one instant.

    ``temperature_time`` is the clock time of the last *successful* reading (None
    if there has never been one). ``psu_command_v`` is the voltage last sent to the
    PSU and ``psu_reading`` its latest readback (None if unavailable).
    ``psu_min_v`` is the supply's lowest programmable voltage (its output-on floor).
    """

    temperature_c: float | None
    temperature_time: float | None
    psu_command_v: float = 0.0
    psu_output_on: bool = False
    psu_reading: PsuReading | None = None
    psu_min_v: float = 0.0
    now: float | None = None


class SafetyMonitor:
    def __init__(
        self,
        limits: LimitsConfig,
        *,
        voltage_tolerance_v: float = 0.5,
        deviation_hold_s: float = 3.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self.t_max_c = min(float(limits.t_max_c), HARD_T_MAX_C)
        self.v_max = min(float(limits.v_max), HARD_V_MAX)
        self.z_min_um = float(limits.z_min_um)
        self.z_max_um = float(limits.z_max_um)
        self.stale_temp_s = float(limits.stale_temp_s)
        self.voltage_tolerance_v = float(voltage_tolerance_v)
        self.deviation_hold_s = float(deviation_hold_s)
        self._deviation_since: float | None = None
        self._started = clock()
        self._latched: dict[AlarmCode, Alarm] = {}

    # ------------------------------------------------------------------ checks

    def check(self, sample: SafetySample) -> list[Alarm]:
        """Evaluate every rule; return active alarms plus still-latched trips."""
        now = self._clock() if sample.now is None else sample.now
        active: list[Alarm] = []

        t = sample.temperature_c
        fresh = t is not None and math.isfinite(t)
        if fresh and t >= self.t_max_c:
            active.append(
                Alarm(AlarmCode.OVER_TEMP, Severity.TRIP, f"temperature {t:.1f} °C >= limit {self.t_max_c:.0f} °C")
            )

        ref = sample.temperature_time if sample.temperature_time is not None else self._started
        age = now - ref
        if age > self.stale_temp_s or (sample.temperature_time is not None and not fresh):
            what = "invalid temperature reading" if sample.temperature_time is not None and not fresh else (
                f"no temperature reading for {age:.1f} s"
            )
            active.append(Alarm(AlarmCode.STALE_TEMP, Severity.TRIP, f"{what} (limit {self.stale_temp_s:g} s)"))

        r = sample.psu_reading
        deviating = False
        if r is not None:
            expected = max(sample.psu_command_v, sample.psu_min_v) if sample.psu_output_on else 0.0
            current_limited = r.mode is PsuMode.CC and r.voltage < expected
            deviating = not current_limited and abs(r.voltage - expected) > self.voltage_tolerance_v
        with self._lock:
            if not deviating:
                self._deviation_since = None
            elif self._deviation_since is None:
                self._deviation_since = now
            persistent = deviating and now - self._deviation_since >= self.deviation_hold_s
        if persistent:
            active.append(
                Alarm(
                    AlarmCode.PSU_DEVIATION,
                    Severity.WARNING,
                    f"PSU reads {r.voltage:.2f} V, expected {expected:.2f} V",
                )
            )

        with self._lock:
            for a in active:
                if a.is_trip:
                    if a.code not in self._latched:
                        log.error("SAFETY TRIP: %s", a.message)
                    self._latched[a.code] = a
                else:
                    log.warning("safety warning: %s", a.message)
            active_codes = {a.code for a in active}
            latched_only = [
                Alarm(a.code, a.severity, a.message, active=False)
                for code, a in self._latched.items()
                if code not in active_codes
            ]
        return active + latched_only

    @property
    def tripped(self) -> bool:
        with self._lock:
            return bool(self._latched)

    @property
    def latched(self) -> list[Alarm]:
        with self._lock:
            return list(self._latched.values())

    def acknowledge(self) -> None:
        """Clear latched trips. A trip whose condition persists re-latches on the next check."""
        with self._lock:
            if self._latched:
                log.info("safety trips acknowledged: %s", [c.value for c in self._latched])
            self._latched.clear()

    def enforce(self, psu: PowerSupply) -> bool:
        """If tripped, switch the PSU output off and command 0 V. Returns True if it acted."""
        if not self.tripped:
            return False
        try:
            psu.output(False)
        except Exception:  # noqa: BLE001
            log.exception("safety: PSU output off failed")
        try:
            psu.set_voltage(0.0)
        except Exception:  # noqa: BLE001
            log.exception("safety: PSU set 0 V failed")
        return True

    # ------------------------------------------------------------------ command guards

    def clamp_voltage(self, v: float) -> float:
        """Limit a voltage command to [0, min(24 V, limits.v_max)]; non-finite -> 0 V."""
        v = float(v)
        if not math.isfinite(v):
            log.error("safety: non-finite voltage command %r replaced by 0 V", v)
            return 0.0
        return min(max(v, 0.0), self.v_max, HARD_V_MAX)

    def z_move_allowed(self, target_um: float) -> bool:
        target_um = float(target_um)
        ok = math.isfinite(target_um) and self.z_min_um <= target_um <= self.z_max_um
        if not ok:
            log.warning(
                "safety: Z move to %s µm refused (soft limits %g–%g µm)", target_um, self.z_min_um, self.z_max_um
            )
        return ok


# --------------------------------------------------------------------------- shutdown


def safe_shutdown(devices) -> list[tuple[str, BaseException]]:
    """Stop motion, kill the heater, close everything. Never raises.

    Each step runs in its own try/except so one failing device does not block the
    rest. Returns ``[(step, exception), ...]`` for the steps that failed.
    """
    failures: list[tuple[str, BaseException]] = []

    def step(name: str, fn: Callable[[], object]) -> None:
        try:
            fn()
            log.info("shutdown: %s ok", name)
        except Exception as exc:  # noqa: BLE001 - keep going no matter what
            log.exception("shutdown: %s failed", name)
            failures.append((name, exc))

    step("z stop", lambda: devices.z.stop(immediate=True))
    step("rotator stop", lambda: devices.rotator.stop(immediate=True))
    step("psu output off", lambda: devices.psu.output(False))
    step("psu 0 V", lambda: devices.psu.set_voltage(0.0))
    step("z close", devices.z.close)
    step("rotator close", devices.rotator.close)
    step("psu close", devices.psu.close)
    step("thermometer close", devices.thermometer.close)
    return failures
