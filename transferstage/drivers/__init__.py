"""Device drivers and the factory that picks real or simulated devices.

Real drivers are imported lazily so that the simulators work on machines without
pyserial/pyvisa hardware or the Kinesis install. Phase 3 must provide:

* ``drivers/psu_1685b.py``  -> ``Psu1685B(port: str)``
* ``drivers/dmm.py``        -> ``DmmThermometer(visa_alias: str)``
* ``drivers/kinesis.py``    -> ``KinesisZStage(serial: str, kinesis_path: str)`` and
  ``KinesisRotator(serial: str, kinesis_path: str)``
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

from ..config import Config
from .base import (
    Device,
    DeviceError,
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
from .sim import SimLinearStage, SimPowerSupply, SimRotaryStage, SimThermalPlant, SimThermometer

__all__ = [
    "Device",
    "DeviceError",
    "Devices",
    "Direction",
    "LinearStage",
    "MotorState",
    "NotConnectedError",
    "PowerSupply",
    "PsuMode",
    "PsuReading",
    "RotaryStage",
    "Thermometer",
    "build_devices",
]

log = logging.getLogger(__name__)


@dataclass
class Devices:
    z: LinearStage
    rotator: RotaryStage
    psu: PowerSupply
    thermometer: Thermometer
    plant: SimThermalPlant | None = None  # set when the thermometer is simulated

    def all(self) -> list[tuple[str, Device]]:
        return [("z", self.z), ("rotator", self.rotator), ("psu", self.psu), ("thermometer", self.thermometer)]

    def connect_all(self) -> dict[str, Exception]:
        """Connect every device; return {name: error} for the ones that failed (others stay connected)."""
        errors: dict[str, Exception] = {}
        for name, dev in self.all():
            try:
                dev.connect()
            except Exception as exc:  # noqa: BLE001 - report every failure, keep going
                log.error("connect %s failed: %s", name, exc)
                errors[name] = exc
        return errors


def _load_real(module: str, cls_name: str, what: str, flag: str):
    try:
        mod = importlib.import_module(f".{module}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.endswith(f"drivers.{module}"):
            raise NotImplementedError(
                f"The real {what} driver (transferstage/drivers/{module}.py) is not implemented yet "
                f"(Phase 3). Set devices.simulate.{flag}: true in config.yaml."
            ) from exc
        raise
    try:
        return getattr(mod, cls_name)
    except AttributeError as exc:
        raise NotImplementedError(f"transferstage.drivers.{module} does not define {cls_name}") from exc


def build_devices(config: Config, *, time_scale: float = 1.0) -> Devices:
    """Create (but do not connect) the four devices described by ``config``.

    Each device is simulated when its ``devices.simulate.<name>`` flag is true.
    ``time_scale`` speeds up the simulated thermal plant (for tests/demos).
    """
    dev = config.devices
    sim = dev.simulate

    if sim.z:
        z: LinearStage = SimLinearStage()  # hardware travel 0–25 mm; soft limits are enforced by SafetyMonitor
    else:
        z = _load_real("kinesis", "KinesisZStage", "Z stage (Kinesis)", "z")(dev.z_serial, dev.kinesis_path)

    if sim.rotator:
        rotator: RotaryStage = SimRotaryStage()
    else:
        rotator = _load_real("kinesis", "KinesisRotator", "rotator (Kinesis)", "rotator")(
            dev.rotator_serial, dev.kinesis_path
        )

    if sim.psu:
        psu: PowerSupply = SimPowerSupply(current_limit_a=min(config.limits.i_max, SimPowerSupply.I_RANGE[1]))
    else:
        psu = _load_real("psu_1685b", "Psu1685B", "B&K 1685B PSU", "psu")(dev.psu_port)

    plant = None
    if sim.dmm:
        if isinstance(psu, SimPowerSupply):
            plant = SimThermalPlant(psu, time_scale=time_scale)
        else:
            log.warning("simulated thermometer with a real PSU: the simulated plant is not heated")
            plant = SimThermalPlant(None, time_scale=time_scale)
        thermometer: Thermometer = SimThermometer(plant)
    else:
        thermometer = _load_real("dmm", "DmmThermometer", "DMM thermometer", "dmm")(dev.dmm_visa)

    return Devices(z=z, rotator=rotator, psu=psu, thermometer=thermometer, plant=plant)
