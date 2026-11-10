"""Abstract device interfaces shared by the real drivers and the simulators.

Units used throughout the GUI and control code:

* Linear (Z) stage: micrometres (µm) and µm/s. Real drivers convert to/from the
  units the vendor library uses (Kinesis uses mm).
* Rotary stage: degrees and deg/s.
* Power supply: volts and amps.
* Thermometer: degrees Celsius.

Thread safety: every device owns a re-entrant lock (``self._lock``). Each
implementation must hold it for the duration of every public method/property so
that worker threads (motion, thermal, GUI) can share a device object. Blocking
waits (``wait=True``) must *not* hold the lock while sleeping, so that another
thread can still call ``stop()``.
"""

from __future__ import annotations

import enum
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass


class DeviceError(RuntimeError):
    """A device reported an error or could not complete a command."""


class NotConnectedError(DeviceError):
    """A command was issued to a device that is not connected."""


class MotorState(enum.Enum):
    """Motor state, mirroring the Z-status enum of the LabVIEW GUI."""

    IDLE = "Idle"
    MOVING = "Moving"
    HOMING = "Homing"
    INITIALIZING = "Initializing"
    STOPPING = "Stopping"
    ERROR = "Error"
    DISCONNECTED = "Disconnected"

    @property
    def is_busy(self) -> bool:
        return self in (MotorState.MOVING, MotorState.HOMING, MotorState.INITIALIZING, MotorState.STOPPING)


class Direction(enum.IntEnum):
    """Jog direction. ``UP`` is increasing position (Z up / rotator forward)."""

    UP = 1
    DOWN = -1


class PsuMode(enum.Enum):
    CV = "CV"  # constant voltage
    CC = "CC"  # constant current (current limit reached)
    OFF = "OFF"  # output disabled


@dataclass(frozen=True)
class PsuReading:
    """Measured PSU output."""

    voltage: float
    current: float
    mode: PsuMode


class Device(ABC):
    """Common base: a lock, connect/close, and context-manager support."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @abstractmethod
    def connect(self) -> None:
        """Open the connection and initialise the device. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release the device. Idempotent; must not raise if already closed."""

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class LinearStage(Device):
    """A motorised linear axis (the Z stage). Positions in µm, velocities in µm/s."""

    @property
    @abstractmethod
    def position_um(self) -> float: ...

    @property
    @abstractmethod
    def state(self) -> MotorState: ...

    @property
    @abstractmethod
    def max_velocity_um_s(self) -> float:
        """Upper bound for :meth:`set_velocity` (from the device's velocity parameters)."""

    @property
    @abstractmethod
    def velocity_um_s(self) -> float:
        """Velocity used for moves and default jogs (the GUI "Set Speed" value)."""

    @abstractmethod
    def set_velocity(self, um_s: float) -> None:
        """Set the move/jog velocity; values are clamped to ``(0, max_velocity_um_s]``."""

    @abstractmethod
    def move_to(self, um: float, wait: bool = False, timeout_s: float | None = None) -> None:
        """Absolute move. With ``wait=True`` block until the move ends (or ``TimeoutError``)."""

    @abstractmethod
    def jog(self, direction: Direction, velocity_um_s: float | None = None) -> None:
        """Move continuously in ``direction`` until :meth:`stop` (or a travel limit)."""

    @abstractmethod
    def stop(self, immediate: bool = True) -> None:
        """Stop motion. ``immediate=False`` requests a profiled (decelerated) stop."""

    @abstractmethod
    def home(self, wait: bool = False, timeout_s: float | None = None) -> None: ...

    @property
    def is_busy(self) -> bool:
        return self.state.is_busy


class RotaryStage(Device):
    """A motorised rotation axis (the cage rotator). Angles in degrees, velocities in deg/s."""

    @property
    @abstractmethod
    def angle_deg(self) -> float: ...

    @property
    @abstractmethod
    def state(self) -> MotorState: ...

    @property
    @abstractmethod
    def max_velocity_deg_s(self) -> float: ...

    @property
    @abstractmethod
    def velocity_deg_s(self) -> float: ...

    @abstractmethod
    def set_velocity(self, deg_s: float) -> None: ...

    @abstractmethod
    def move_to(self, deg: float, wait: bool = False, timeout_s: float | None = None) -> None: ...

    @abstractmethod
    def jog(self, direction: Direction, velocity_deg_s: float | None = None) -> None: ...

    @abstractmethod
    def stop(self, immediate: bool = True) -> None: ...

    @abstractmethod
    def home(self, wait: bool = False, timeout_s: float | None = None) -> None: ...

    @property
    def is_busy(self) -> bool:
        return self.state.is_busy


class PowerSupply(Device):
    """A programmable DC supply driving the heater.

    Drivers do not enforce the application's 24 V safety limit; that is the job of
    the control layer (``control/safety.py``). They only reject values outside the
    instrument's own range.
    """

    #: Lowest voltage the supply can be programmed to; lower set points are sent as this.
    v_min: float = 0.0

    @abstractmethod
    def set_voltage(self, v: float) -> None: ...

    @abstractmethod
    def set_current(self, a: float) -> None:
        """Set the current limit."""

    @abstractmethod
    def output(self, on: bool) -> None:
        """Enable or disable the output."""

    @abstractmethod
    def read_output(self) -> PsuReading: ...

    @property
    @abstractmethod
    def is_on(self) -> bool: ...


class Thermometer(Device):
    @abstractmethod
    def read_temperature(self) -> float:
        """Return the current temperature in °C. Raises :class:`DeviceError` on failure."""
