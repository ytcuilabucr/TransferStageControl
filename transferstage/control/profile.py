"""Multi-segment temperature profiles: ramp to a temperature, then hold.

YAML format::

    name: anneal
    segments:
      - {ramp_to: 100, rate_deg_min: 10, hold_min: 5}
      - {ramp_to: 200, rate_deg_min: 5,  hold_min: 30}
      - {ramp_to: 25,  rate_deg_min: 0,  hold_min: 0}   # rate 0 = step

The program set point is a pure function of elapsed time, so a run can be
replayed exactly in simulated time.
"""

from __future__ import annotations

import enum
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import HARD_T_MAX_C

Clock = Callable[[], float]


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    ramp_to: float
    rate_deg_min: float
    hold_min: float = 0.0

    def __post_init__(self) -> None:
        for name in ("ramp_to", "rate_deg_min", "hold_min"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ProfileError(f"segment {name} must be a finite number, got {v!r}")
            object.__setattr__(self, name, float(v))
        if self.ramp_to > HARD_T_MAX_C:
            raise ProfileError(f"segment ramp_to {self.ramp_to} °C exceeds the {HARD_T_MAX_C} °C limit")
        if self.rate_deg_min < 0:
            raise ProfileError("segment rate_deg_min must be >= 0 (0 = step)")
        if self.hold_min < 0:
            raise ProfileError("segment hold_min must be >= 0")


@dataclass
class TemperatureProfile:
    segments: list[Segment] = field(default_factory=list)
    name: str = ""

    def __post_init__(self) -> None:
        if not self.segments:
            raise ProfileError("a profile needs at least one segment")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "segments": [
                {"ramp_to": s.ramp_to, "rate_deg_min": s.rate_deg_min, "hold_min": s.hold_min} for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> TemperatureProfile:
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            raise ProfileError("profile must be a mapping with a 'segments' list")
        unknown = set(data) - {"name", "segments"}
        if unknown:
            raise ProfileError(f"unknown profile key(s): {sorted(unknown)}")
        segs = []
        for i, s in enumerate(data["segments"]):
            if not isinstance(s, dict):
                raise ProfileError(f"segment {i} must be a mapping")
            extra = set(s) - {"ramp_to", "rate_deg_min", "hold_min"}
            if extra or "ramp_to" not in s or "rate_deg_min" not in s:
                raise ProfileError(f"segment {i} needs ramp_to and rate_deg_min (optional hold_min); got {sorted(s)}")
            segs.append(Segment(s["ramp_to"], s["rate_deg_min"], s.get("hold_min", 0.0)))
        return cls(segs, str(data.get("name") or ""))

    @classmethod
    def load(cls, path: str | Path) -> TemperatureProfile:
        with Path(path).open("r", encoding="utf-8") as fh:
            try:
                return cls.from_dict(yaml.safe_load(fh))
            except yaml.YAMLError as exc:
                raise ProfileError(f"invalid YAML in {path}: {exc}") from exc

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        with p.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self.to_dict(), fh, sort_keys=False, allow_unicode=True)
        return p


class Phase(enum.Enum):
    RAMP = "ramp"
    HOLD = "hold"
    DONE = "done"


@dataclass(frozen=True)
class ProfileStatus:
    program_sp: float
    segment: int  # index of the current segment (last one when DONE)
    phase: Phase
    elapsed_s: float
    remaining_s: float

    @property
    def done(self) -> bool:
        return self.phase is Phase.DONE


class ProfileRunner:
    """Runs a :class:`TemperatureProfile` starting from ``start_c`` at ``start()``."""

    def __init__(self, profile: TemperatureProfile, start_c: float, *, clock: Clock = time.monotonic) -> None:
        self.profile = profile
        self._clock = clock
        self._t0: float | None = None
        # Precompute (start value, ramp seconds, hold seconds) per segment.
        self._plan: list[tuple[float, float, float]] = []
        value = min(float(start_c), HARD_T_MAX_C)
        for s in profile.segments:
            ramp_s = 0.0 if s.rate_deg_min == 0 else abs(s.ramp_to - value) / s.rate_deg_min * 60.0
            self._plan.append((value, ramp_s, s.hold_min * 60.0))
            value = s.ramp_to
        self.total_s = sum(r + h for _, r, h in self._plan)

    def start(self, now: float | None = None) -> None:
        self._t0 = self._clock() if now is None else now

    @property
    def started(self) -> bool:
        return self._t0 is not None

    def status_at(self, elapsed_s: float) -> ProfileStatus:
        """Status ``elapsed_s`` seconds after the start."""
        t = max(float(elapsed_s), 0.0)
        remaining = max(self.total_s - t, 0.0)
        seg_t0 = 0.0
        for i, (seg, (v0, ramp_s, hold_s)) in enumerate(zip(self.profile.segments, self._plan)):
            local = t - seg_t0
            if local < ramp_s:
                sp = v0 + (seg.ramp_to - v0) * (local / ramp_s)
                return ProfileStatus(sp, i, Phase.RAMP, t, remaining)
            if local < ramp_s + hold_s:
                return ProfileStatus(seg.ramp_to, i, Phase.HOLD, t, remaining)
            seg_t0 += ramp_s + hold_s
        last = len(self.profile.segments) - 1
        return ProfileStatus(self.profile.segments[last].ramp_to, last, Phase.DONE, t, 0.0)

    def status(self, now: float | None = None) -> ProfileStatus:
        if self._t0 is None:
            raise RuntimeError("ProfileRunner.start() has not been called")
        t = self._clock() if now is None else now
        return self.status_at(t - self._t0)

    def iter_setpoints(self, step_s: float) -> Iterator[ProfileStatus]:
        """Yield the status every ``step_s`` of profile time, ending with the DONE status."""
        if step_s <= 0:
            raise ValueError("step_s must be > 0")
        n = 0
        while True:
            st = self.status_at(n * step_s)
            yield st
            if st.done:
                return
            n += 1
