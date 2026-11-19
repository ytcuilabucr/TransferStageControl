"""Application configuration: dataclasses loaded from ``config.yaml``.

The hard safety limits (``HARD_T_MAX_C`` and ``HARD_V_MAX``) are code constants.
A config file may set lower limits, but anything above them is rejected with
:class:`ConfigError`. Nothing in the config (or the GUI) can raise them.
"""

from __future__ import annotations

import dataclasses
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Hard safety limits — not user-overridable.
HARD_T_MAX_C = 300.0
HARD_V_MAX = 24.0

if getattr(sys, "frozen", False):  # PyInstaller build: config.yaml and logs/ sit next to the .exe
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigError(ValueError):
    """Raised when a configuration value is missing, unknown, or unsafe."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _finite(value: float, name: str) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        f"{name} must be a finite number, got {value!r}",
    )
    return float(value)


# --------------------------------------------------------------------------- sections


@dataclass
class SimulateFlags:
    z: bool = True
    rotator: bool = True
    psu: bool = True
    dmm: bool = True

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            _require(isinstance(getattr(self, f.name), bool), f"devices.simulate.{f.name} must be true/false")


@dataclass
class DevicesConfig:
    z_serial: str = "27270114"
    rotator_serial: str = "55524924"
    psu_port: str = "COM4"
    dmm_visa: str = "DMM"
    kinesis_path: str = r"C:\Program Files\Thorlabs\Kinesis"
    simulate: SimulateFlags = field(default_factory=SimulateFlags)

    def __post_init__(self) -> None:
        # YAML may give serials as ints if unquoted; normalise to str.
        self.z_serial = str(self.z_serial)
        self.rotator_serial = str(self.rotator_serial)
        for name in ("z_serial", "rotator_serial", "psu_port", "dmm_visa", "kinesis_path"):
            _require(bool(str(getattr(self, name)).strip()), f"devices.{name} must not be empty")


@dataclass
class PidConfig:
    kc: float = 0.5
    ti_min: float = 1.0
    td_min: float = 0.0
    period_ms: int = 500
    output_low: float = 0.0
    output_high: float = 24.0
    ff_k_c_per_v2: float = 0.0  # feedforward heater model: steady-state rise per V², °C/V² (0 = off)
    ff_t_amb_c: float = 22.0  # feedforward ambient temperature, °C
    ff_tau_s: float = 0.0  # feedforward thermal time constant, s (adds the ramp term; 0 = hold voltage only)

    def __post_init__(self) -> None:
        self.kc = _finite(self.kc, "pid.kc")
        self.ff_k_c_per_v2 = _finite(self.ff_k_c_per_v2, "pid.ff_k_c_per_v2")
        self.ff_t_amb_c = _finite(self.ff_t_amb_c, "pid.ff_t_amb_c")
        self.ff_tau_s = _finite(self.ff_tau_s, "pid.ff_tau_s")
        _require(self.ff_tau_s >= 0, "pid.ff_tau_s must be >= 0 s")
        _require(self.ff_k_c_per_v2 >= 0, "pid.ff_k_c_per_v2 must be >= 0 (0 disables feedforward)")
        self.ti_min = _finite(self.ti_min, "pid.ti_min")
        self.td_min = _finite(self.td_min, "pid.td_min")
        self.output_low = _finite(self.output_low, "pid.output_low")
        self.output_high = _finite(self.output_high, "pid.output_high")
        _require(self.ti_min >= 0, "pid.ti_min must be >= 0 (0 disables integral)")
        _require(self.td_min >= 0, "pid.td_min must be >= 0")
        _require(
            isinstance(self.period_ms, int) and not isinstance(self.period_ms, bool) and self.period_ms > 0,
            "pid.period_ms must be a positive integer",
        )
        _require(self.output_low >= 0, "pid.output_low must be >= 0 V")
        _require(self.output_low < self.output_high, "pid.output_low must be < pid.output_high")
        _require(self.output_high <= HARD_V_MAX, f"pid.output_high must be <= {HARD_V_MAX} V")


@dataclass
class RampConfig:
    default_rate_deg_min: float = 10.0
    dial_step_c: float = 1.0  # setpoint change per TourBox Dial click (setpoint_up / setpoint_down)
    set_enables_psu: bool = True  # Set (GUI / TourBox C1) also switches the PSU on (set point > T_amb + 5 °C)

    def __post_init__(self) -> None:
        self.default_rate_deg_min = _finite(self.default_rate_deg_min, "ramp.default_rate_deg_min")
        _require(self.default_rate_deg_min >= 0, "ramp.default_rate_deg_min must be >= 0 (0 = step)")
        self.dial_step_c = _finite(self.dial_step_c, "ramp.dial_step_c")
        _require(0 < self.dial_step_c <= 100, "ramp.dial_step_c must be in (0, 100] °C")
        _require(isinstance(self.set_enables_psu, bool), "ramp.set_enables_psu must be true/false")


def _default_speed_steps() -> list[float]:
    return [1.0, 10.0, 20.0, 50.0, 200.0]


@dataclass
class MotionConfig:
    # Z speeds (µm/s) selected by the 5-position Speed Knob (TourBox knob), positions 0..4.
    speed_steps_um_s: list[float] = field(default_factory=_default_speed_steps)

    def __post_init__(self) -> None:
        _require(isinstance(self.speed_steps_um_s, list), "motion.speed_steps_um_s must be a list")
        _require(len(self.speed_steps_um_s) == 5, "motion.speed_steps_um_s must have exactly 5 values")
        self.speed_steps_um_s = [
            _finite(v, f"motion.speed_steps_um_s[{i}]") for i, v in enumerate(self.speed_steps_um_s)
        ]
        _require(all(v > 0 for v in self.speed_steps_um_s), "motion.speed_steps_um_s values must be > 0")


@dataclass
class LimitsConfig:
    t_max_c: float = HARD_T_MAX_C
    v_max: float = HARD_V_MAX
    i_max: float = 3.0
    z_min_um: float = 0.0
    z_max_um: float = 25000.0
    stale_temp_s: float = 5.0
    idle_timeout_min: float = 20.0  # heater on and no keyboard/mouse input this long -> idle alarm
    idle_grace_min: float = 3.0  # idle alarm unanswered this long -> heater off

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            setattr(self, f.name, _finite(getattr(self, f.name), f"limits.{f.name}"))
        _require(0 < self.t_max_c <= HARD_T_MAX_C, f"limits.t_max_c must be in (0, {HARD_T_MAX_C}] °C, got {self.t_max_c}")
        _require(0 < self.v_max <= HARD_V_MAX, f"limits.v_max must be in (0, {HARD_V_MAX}] V, got {self.v_max}")
        _require(self.i_max > 0, "limits.i_max must be > 0 A")
        _require(self.z_min_um < self.z_max_um, "limits.z_min_um must be < limits.z_max_um")
        _require(self.stale_temp_s > 0, "limits.stale_temp_s must be > 0 s")
        _require(self.idle_timeout_min > 0, "limits.idle_timeout_min must be > 0 min")
        _require(self.idle_grace_min > 0, "limits.idle_grace_min must be > 0 min")


def _default_keys() -> dict[str, str]:
    # Match these to the TourBox Console mapping.
    return {
        "safe_lock": "Ctrl+F1",  # TourBox Side + Top combo (Side alone sends side_alone)
        "cancel_motion": "Ctrl+F6",
        "speed_up": "Ctrl+F4",
        "speed_down": "Ctrl+F5",
        "jog_up": "Ctrl+F2",
        "jog_down": "Ctrl+F3",
        "mark": "Ctrl+F7",  # event mark in the session CSV (not in the TourBox preset; assign a button if wanted)
        "setpoint_up": "Ctrl+F8",  # TourBox Dial clockwise: setpoint +1 step (staged, not applied)
        "setpoint_down": "Ctrl+F9",  # TourBox Dial counter-clockwise: setpoint -1 step
        "setpoint_set": "Ctrl+F10",  # TourBox C1: apply the staged setpoint
        "ramp_toggle": "Ctrl+F11",  # TourBox C2: Ramp On / Off
        "side_alone": "Ctrl+F12",  # TourBox Side alone: no action, only a "press Side + Top" hint
    }


@dataclass
class LoggingConfig:
    dir: str = "logs"
    interval_s: float = 1.0

    def __post_init__(self) -> None:
        _require(bool(str(self.dir).strip()), "logging.dir must not be empty")
        self.interval_s = _finite(self.interval_s, "logging.interval_s")
        _require(self.interval_s > 0, "logging.interval_s must be > 0")


def _presets(value: Any) -> list[dict[str, Any]]:
    """Validate ``presets``: a list of {name, z_um, angle_deg} (named Z + angle positions)."""
    if value is None:
        return []
    _require(isinstance(value, list), "presets must be a list of {name, z_um, angle_deg}")
    out: list[dict[str, Any]] = []
    for i, p in enumerate(value):
        _require(isinstance(p, dict), f"presets[{i}] must be a mapping")
        unknown = set(p) - {"name", "z_um", "angle_deg"}
        _require(not unknown, f"unknown key(s) in presets[{i}]: {sorted(unknown)}")
        name = str(p.get("name", "")).strip()
        _require(bool(name), f"presets[{i}].name must not be empty")
        out.append(
            {
                "name": name,
                "z_um": _finite(p.get("z_um"), f"presets[{i}].z_um"),
                "angle_deg": _finite(p.get("angle_deg"), f"presets[{i}].angle_deg"),
            }
        )
    names = [p["name"] for p in out]
    _require(len(names) == len(set(names)), "presets: names must be unique")
    return out


@dataclass
class Config:
    devices: DevicesConfig = field(default_factory=DevicesConfig)
    pid: PidConfig = field(default_factory=PidConfig)
    ramp: RampConfig = field(default_factory=RampConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    keys: dict[str, str] = field(default_factory=_default_keys)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    presets: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.presets = _presets(self.presets)
        _require(
            self.pid.output_high <= self.limits.v_max,
            f"pid.output_high ({self.pid.output_high} V) must be <= limits.v_max ({self.limits.v_max} V)",
        )
        _require(isinstance(self.keys, dict), "keys must be a mapping of action -> shortcut")
        for action, shortcut in self.keys.items():
            _require(isinstance(action, str) and isinstance(shortcut, str), f"keys.{action} must be a string")
        shortcuts = [s for s in self.keys.values() if s]
        _require(len(shortcuts) == len(set(shortcuts)), "keys: the same shortcut is bound to more than one action")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- load / save


def _build(cls: type, data: Any, path: str) -> Any:
    """Construct dataclass ``cls`` from ``data``, rejecting unknown keys."""
    if data is None:
        data = {}
    _require(isinstance(data, dict), f"{path or 'config'} must be a mapping")
    names = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(names)
    _require(not unknown, f"unknown key(s) in {path or 'config'}: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        f = names[key]
        sub = f"{path}.{key}" if path else key
        factory = f.default_factory
        if isinstance(factory, type) and dataclasses.is_dataclass(factory):
            kwargs[key] = _build(factory, value, sub)
        elif key == "keys" and cls is Config:
            merged = _default_keys()
            _require(isinstance(value, dict), "keys must be a mapping of action -> shortcut")
            merged.update(value or {})
            kwargs[key] = merged
        else:
            kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{path or 'config'}: {exc}") from exc


def config_from_dict(data: dict[str, Any] | None) -> Config:
    return _build(Config, data, "")


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate config. A missing file at the default path yields defaults."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.exists():
        if path is None:
            return Config()
        raise ConfigError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    return config_from_dict(data)


def save_config(config: Config, path: str | Path | None = None) -> Path:
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config.to_dict(), fh, sort_keys=False, allow_unicode=True)
    return p
