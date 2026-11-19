"""Thorlabs Kinesis stages through pythonnet (.NET CLI assemblies).

* :class:`KinesisZStage` — KCube DC Servo (``KCubeDCServo``, e.g. serial 27270114).
  Kinesis works in mm and mm/s; this driver exposes µm and µm/s.
* :class:`KinesisRotator` — Integrated Stepper cage rotator (``CageRotator``, e.g.
  serial 55524924), in degrees and deg/s.

The assemblies are loaded from the **64-bit** Kinesis install (``kinesis_path``,
default ``C:\\Program Files\\Thorlabs\\Kinesis``). The DLLs in ``reference/`` are 32-bit
and cannot be loaded into 64-bit Python.

Connect sequence: ``DeviceManagerCLI.BuildDeviceList`` -> ``Create…(serial)`` ->
``Connect`` -> ``WaitForSettingsInitialized`` -> ``StartPolling(250)`` ->
``EnableDevice`` -> ``LoadMotorConfiguration``. Close: stop if moving ->
``StopPolling`` -> ``Disconnect(True)``. The drive is *not* disabled on close, so a
DC-servo Z stage keeps holding its position under load.

Moves are started with ``waitTimeout=0`` (return immediately); ``wait=True`` polls
the status without holding the device lock so ``stop()`` always gets through.
Jogs use ``SetVelocityParams`` + ``MoveContinuous`` (``MoveContinuousAtVelocity``
takes device units, not mm/s).

Manual test CLI (serials and path default to config.yaml)::

    python -m transferstage.drivers.kinesis --list
    python -m transferstage.drivers.kinesis --status
    python -m transferstage.drivers.kinesis --z-jog 100        # jog +100 µm, then stop
    python -m transferstage.drivers.kinesis --z-move 12500     # absolute, µm
    python -m transferstage.drivers.kinesis --rot-move 10      # absolute, degrees
    python -m transferstage.drivers.kinesis --home z           # or rot
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import struct
import sys
import threading
import time
from typing import Any

from .base import DeviceError, Direction, LinearStage, MotorState, NotConnectedError, RotaryStage

log = logging.getLogger(__name__)

DEFAULT_KINESIS_PATH = r"C:\Program Files\Thorlabs\Kinesis"
ASSEMBLIES = (
    "Thorlabs.MotionControl.DeviceManagerCLI",
    "Thorlabs.MotionControl.GenericMotorCLI",
    "ThorLabs.MotionControl.KCube.DCServoCLI",
    "Thorlabs.MotionControl.IntegratedStepperMotorsCLI",
)
POLL_MS = 250
SETTINGS_TIMEOUT_MS = 10_000
# After a move/home command the polled status can lag by up to one polling period;
# report the commanded state until the device confirms it (or this window expires).
PENDING_WINDOW_S = 3 * POLL_MS / 1000

UM_PER_MM = 1000.0


def um_to_mm(um: float) -> float:
    return float(um) / UM_PER_MM


def mm_to_um(mm: float) -> float:
    return float(mm) * UM_PER_MM


# --------------------------------------------------------------------------- loader


class KinesisApi:
    """The loaded .NET namespace, wrapped so the stage classes can be tested with a fake."""

    def __init__(self, path: str) -> None:
        if struct.calcsize("P") != 8:
            raise DeviceError("Kinesis requires 64-bit Python")
        missing = [a for a in ASSEMBLIES if not os.path.isfile(os.path.join(path, a + ".dll"))]
        if missing:
            raise DeviceError(f"Kinesis assemblies not found in {path!r}: {missing} (install 64-bit Kinesis)")
        try:
            import clr  # pythonnet
        except ImportError as exc:
            raise DeviceError("pythonnet is not installed (pip install pythonnet)") from exc
        if path not in sys.path:
            sys.path.append(path)  # dependent native DLLs are resolved from here
        try:
            for a in ASSEMBLIES:
                clr.AddReference(os.path.join(path, a + ".dll"))
            import System
            from Thorlabs.MotionControl.DeviceManagerCLI import DeviceConfiguration, DeviceManagerCLI
            from Thorlabs.MotionControl.GenericMotorCLI import MotorDirection
            from Thorlabs.MotionControl.IntegratedStepperMotorsCLI import CageRotator
            from Thorlabs.MotionControl.KCube.DCServoCLI import KCubeDCServo
        except Exception as exc:  # noqa: BLE001 - FileLoadException, BadImageFormatException, ...
            raise DeviceError(f"cannot load Kinesis assemblies from {path!r}: {exc}") from exc
        self.path = path
        self._decimal = System.Decimal
        self.DeviceManagerCLI = DeviceManagerCLI
        self.forward = MotorDirection.Forward
        self.backward = MotorDirection.Backward
        self.use_device_settings = DeviceConfiguration.DeviceSettingsUseOptionType.UseDeviceSettings
        self._KCubeDCServo = KCubeDCServo
        self._CageRotator = CageRotator

    def to_decimal(self, x: float) -> Any:
        return self._decimal(float(x))

    def from_decimal(self, d: Any) -> float:
        return float(self._decimal.ToDouble(d))

    def device_list(self) -> list[str]:
        self.DeviceManagerCLI.BuildDeviceList()
        return [str(s) for s in self.DeviceManagerCLI.GetDeviceList()]

    def create_dc_servo(self, serial: str) -> Any:
        return self._KCubeDCServo.CreateKCubeDCServo(serial)

    def create_cage_rotator(self, serial: str) -> Any:
        return self._CageRotator.CreateCageRotator(serial)


_api_cache: dict[str, KinesisApi] = {}
_api_lock = threading.Lock()


def load_kinesis(path: str = DEFAULT_KINESIS_PATH) -> KinesisApi:
    """Load the Kinesis assemblies once per path and return the wrapper."""
    with _api_lock:
        if path not in _api_cache:
            _api_cache[path] = KinesisApi(path)
        return _api_cache[path]


def list_devices(path: str = DEFAULT_KINESIS_PATH) -> list[str]:
    return load_kinesis(path).device_list()


# --------------------------------------------------------------------------- shared axis


_STATE_NAMES = {
    "Idle": MotorState.IDLE,
    "Moving": MotorState.MOVING,
    "Homing": MotorState.HOMING,
    "Initializing": MotorState.INITIALIZING,
    "Stopping": MotorState.STOPPING,
    "Terminated": MotorState.ERROR,
}


class _KinesisAxis:
    """Implementation shared by both stages. ``scale`` = user units per Kinesis unit."""

    scale: float = 1.0
    unit: str = ""
    enforce_travel: bool = True
    position_tol: float = 0.0  # user units; "target reached" tolerance for the pending window

    _lock: threading.RLock

    def __init__(self, serial: str, kinesis_path: str = DEFAULT_KINESIS_PATH, *, api: Any = None) -> None:
        self.serial = str(serial)
        self.kinesis_path = kinesis_path
        self._api = api
        self._dev: Any = None
        self._velocity = 0.0  # user units / s
        self._max_velocity = 0.0
        self._accel: Any = None  # Kinesis Decimal, kept as read from the device
        self._travel: tuple[float, float] | None = None
        self._pending: tuple[MotorState, float, float | None] | None = None  # (state, t_issued, target)

    # ------------------------------------------------------------------ helpers

    def _create(self, api: Any, serial: str) -> Any:
        raise NotImplementedError

    def _require(self) -> Any:
        if self._dev is None:
            raise NotConnectedError(f"{type(self).__name__} {self.serial} is not connected")
        return self._dev

    def _to_dev(self, x: float) -> Any:
        return self._api.to_decimal(x / self.scale)

    def _from_dev(self, d: Any) -> float:
        return self._api.from_decimal(d) * self.scale

    def _call(self, what: str, fn, *args):
        try:
            return fn(*args)
        except DeviceError:
            raise
        except Exception as exc:  # noqa: BLE001 - .NET exceptions surface as System.Exception
            raise DeviceError(f"{type(self).__name__} {self.serial}: {what} failed: {exc}") from exc

    # ------------------------------------------------------------------ Device

    def connect(self) -> None:
        with self._lock:
            if self._dev is not None:
                return
            if self._api is None:
                self._api = load_kinesis(self.kinesis_path)
            api = self._api
            serials = self._call("BuildDeviceList", api.device_list)
            if self.serial not in serials:
                raise DeviceError(f"Kinesis device {self.serial} not found (found: {serials or 'none'})")
            dev = self._call("create", self._create, api, self.serial)
            try:
                self._call("Connect", dev.Connect, self.serial)
                if not dev.IsSettingsInitialized():
                    self._call("WaitForSettingsInitialized", dev.WaitForSettingsInitialized, SETTINGS_TIMEOUT_MS)
                self._call("StartPolling", dev.StartPolling, POLL_MS)
                time.sleep(2 * POLL_MS / 1000)
                self._call("EnableDevice", dev.EnableDevice)
                time.sleep(2 * POLL_MS / 1000)
                self._call(
                    "LoadMotorConfiguration", dev.LoadMotorConfiguration, self.serial, api.use_device_settings
                )
                self._dev = dev
                self._read_parameters()
            except Exception:
                self._dev = None
                self._release(dev)
                raise
            log.info(
                "%s %s connected: pos %.4g %s, v %.4g/%.4g %s/s, travel %s",
                type(self).__name__, self.serial, self._position(), self.unit,
                self._velocity, self._max_velocity, self.unit, self._travel,
            )

    def _read_parameters(self) -> None:
        dev = self._dev
        vp = self._call("GetVelocityParams", dev.GetVelocityParams)
        self._accel = vp.Acceleration
        self._velocity = self._from_dev(vp.MaxVelocity)
        vmax = 0.0
        lo = hi = 0.0
        try:
            lim = dev.AdvancedMotorLimits
            vmax = self._from_dev(lim.VelocityMaximum)
            lo, hi = self._from_dev(lim.LengthMinimum), self._from_dev(lim.LengthMaximum)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s %s: AdvancedMotorLimits unavailable: %s", type(self).__name__, self.serial, exc)
        self._max_velocity = vmax if vmax > 0 else self._velocity
        self._travel = (lo, hi) if self.enforce_travel and hi > lo else None

    @staticmethod
    def _release(dev: Any) -> None:
        for name, args in (("StopPolling", ()), ("Disconnect", (True,))):
            try:
                getattr(dev, name)(*args)
            except Exception as exc:  # noqa: BLE001 - close must not raise
                log.warning("Kinesis %s: %s", name, exc)

    def close(self) -> None:
        with self._lock:
            dev, self._dev = self._dev, None
            self._pending = None
            if dev is None:
                return
            try:
                if self._raw_state(dev).is_busy:
                    dev.StopImmediate()
            except Exception as exc:  # noqa: BLE001
                log.warning("Kinesis stop on close: %s", exc)
            self._release(dev)

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._dev is not None

    # ------------------------------------------------------------------ state

    def _position(self) -> float:
        return self._from_dev(self._call("Position", lambda: self._require().Position))

    def _raw_state(self, dev: Any) -> MotorState:
        st = dev.Status
        if getattr(st, "IsError", False):
            return MotorState.ERROR
        if getattr(st, "IsHoming", False):
            return MotorState.HOMING
        if getattr(st, "IsInMotion", False) or getattr(st, "IsMoving", False) or getattr(st, "IsJogging", False):
            return MotorState.MOVING
        return _STATE_NAMES.get(str(dev.State), MotorState.IDLE)

    def _state(self) -> MotorState:
        with self._lock:
            if self._dev is None:
                return MotorState.DISCONNECTED
            try:
                raw = self._raw_state(self._dev)
            except Exception as exc:  # noqa: BLE001
                log.warning("Kinesis status read failed: %s", exc)
                return MotorState.ERROR
            if self._pending is not None:
                state, t0, target = self._pending
                if raw.is_busy or raw is MotorState.ERROR:
                    self._pending = None  # device has caught up
                elif time.monotonic() - t0 < PENDING_WINDOW_S and not (
                    target is not None and abs(self._position() - target) <= self.position_tol
                ):
                    return state
                else:
                    self._pending = None
            return raw

    def _wait(self, timeout_s: float | None) -> None:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            state = self._state()  # takes and releases the lock
            if state is MotorState.ERROR:
                raise DeviceError(f"{type(self).__name__} {self.serial}: device reported an error")
            if state is MotorState.DISCONNECTED:
                raise NotConnectedError(f"{type(self).__name__} {self.serial} disconnected during motion")
            if not state.is_busy:
                return
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"{type(self).__name__}: motion did not finish within {timeout_s} s")
            time.sleep(0.05)

    # ------------------------------------------------------------------ motion

    def _clamp_velocity(self, v: float | None) -> float:
        v = self._velocity if v is None else min(abs(float(v)), self._max_velocity)
        if not (math.isfinite(v) and v > 0):
            raise ValueError(f"velocity must be a positive number, got {v!r}")
        return v

    def _apply_velocity(self, v: float) -> None:
        dev = self._require()
        self._call("SetVelocityParams", dev.SetVelocityParams, self._to_dev(v), self._accel)

    def _set_velocity(self, v: float) -> None:
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
            raise ValueError(f"velocity must be a positive number, got {v!r}")
        with self._lock:
            self._require()
            v = min(float(v), self._max_velocity)
            self._apply_velocity(v)
            self._velocity = v

    def _move_to(self, target: float, wait: bool, timeout_s: float | None) -> None:
        target = float(target)
        if not math.isfinite(target):
            raise ValueError(f"target must be finite, got {target!r}")
        with self._lock:
            dev = self._require()
            if self._travel is not None and not self._travel[0] <= target <= self._travel[1]:
                raise ValueError(f"target {target} {self.unit} outside travel range {self._travel}")
            self._apply_velocity(self._velocity)
            self._call("MoveTo", dev.MoveTo, self._to_dev(target), 0)
            self._pending = (MotorState.MOVING, time.monotonic(), target)
        if wait:
            self._wait(timeout_s)

    def _jog(self, direction: Direction, velocity: float | None) -> None:
        with self._lock:
            dev = self._require()
            v = self._clamp_velocity(velocity)
            self._apply_velocity(v)
            md = self._api.forward if Direction(direction) is Direction.UP else self._api.backward
            self._call("MoveContinuous", dev.MoveContinuous, md)
            self._pending = (MotorState.MOVING, time.monotonic(), None)

    def _home(self, wait: bool, timeout_s: float | None) -> None:
        with self._lock:
            dev = self._require()
            self._call("Home", dev.Home, 0)
            self._pending = (MotorState.HOMING, time.monotonic(), None)
        if wait:
            self._wait(timeout_s)

    def stop(self, immediate: bool = True) -> None:
        with self._lock:
            dev = self._require()
            self._pending = None
            if immediate:
                self._call("StopImmediate", dev.StopImmediate)
            else:
                self._call("Stop", dev.Stop, 0)


# --------------------------------------------------------------------------- public classes


def wrap_deg(deg: float) -> float:
    """Map any angle to the controller's [0, 360) range (-30 -> 330)."""
    w = float(deg) % 360.0
    return 0.0 if w >= 360.0 else w  # guard -1e-17 % 360 == 360.0


def signed_deg(deg: float) -> float:
    """Map a [0, 360) controller angle to (-180, 180] (330 -> -30)."""
    w = wrap_deg(deg)
    return w - 360.0 if w > 180.0 else w


class KinesisZStage(_KinesisAxis, LinearStage):
    """KCube DC Servo Z stage; µm and µm/s."""

    scale = UM_PER_MM
    unit = "µm"
    enforce_travel = True
    position_tol = 0.5

    def __init__(self, serial: str, kinesis_path: str = DEFAULT_KINESIS_PATH, *, api: Any = None) -> None:
        LinearStage.__init__(self)
        _KinesisAxis.__init__(self, serial, kinesis_path, api=api)

    def _create(self, api: Any, serial: str) -> Any:
        return api.create_dc_servo(serial)

    @property
    def position_um(self) -> float:
        with self._lock:
            return self._position()

    @property
    def state(self) -> MotorState:
        return self._state()

    @property
    def max_velocity_um_s(self) -> float:
        with self._lock:
            return self._max_velocity

    @property
    def velocity_um_s(self) -> float:
        with self._lock:
            return self._velocity

    def set_velocity(self, um_s: float) -> None:
        self._set_velocity(um_s)

    def move_to(self, um: float, wait: bool = False, timeout_s: float | None = None) -> None:
        self._move_to(um, wait, timeout_s)

    def jog(self, direction: Direction, velocity_um_s: float | None = None) -> None:
        self._jog(direction, velocity_um_s)

    def home(self, wait: bool = False, timeout_s: float | None = None) -> None:
        self._home(wait, timeout_s)


class KinesisRotator(_KinesisAxis, RotaryStage):
    """Integrated Stepper cage rotator; degrees and deg/s (continuous rotation, no travel check).

    The controller only accepts absolute targets in [0, 360), so ``move_to`` wraps the
    requested angle (e.g. -30° is sent as 330°). ``angle_deg`` reports the signed angle
    in (-180, 180] so a -30° move reads back as -30°.
    """

    scale = 1.0
    unit = "°"
    enforce_travel = False
    position_tol = 0.01

    def __init__(self, serial: str, kinesis_path: str = DEFAULT_KINESIS_PATH, *, api: Any = None) -> None:
        RotaryStage.__init__(self)
        _KinesisAxis.__init__(self, serial, kinesis_path, api=api)

    def _create(self, api: Any, serial: str) -> Any:
        return api.create_cage_rotator(serial)

    @property
    def angle_deg(self) -> float:
        with self._lock:
            return signed_deg(self._position())

    @property
    def state(self) -> MotorState:
        return self._state()

    @property
    def max_velocity_deg_s(self) -> float:
        with self._lock:
            return self._max_velocity

    @property
    def velocity_deg_s(self) -> float:
        with self._lock:
            return self._velocity

    def set_velocity(self, deg_s: float) -> None:
        self._set_velocity(deg_s)

    def move_to(self, deg: float, wait: bool = False, timeout_s: float | None = None) -> None:
        self._move_to(wrap_deg(deg), wait, timeout_s)

    def jog(self, direction: Direction, velocity_deg_s: float | None = None) -> None:
        self._jog(direction, velocity_deg_s)

    def home(self, wait: bool = False, timeout_s: float | None = None) -> None:
        self._home(wait, timeout_s)


# --------------------------------------------------------------------------- CLI

Z_JOG_MAX_UM = 5000.0


def jog_distance(stage: LinearStage, distance_um: float, timeout_s: float | None = None) -> float:
    """Jog ``stage`` by about ``distance_um`` (continuous move, then profiled stop). Returns the final position."""
    start = stage.position_um
    direction = Direction.UP if distance_um > 0 else Direction.DOWN
    v = stage.velocity_um_s
    if timeout_s is None:
        timeout_s = abs(distance_um) / v * 2 + 5
    deadline = time.monotonic() + timeout_s
    stage.jog(direction)
    try:
        while abs(stage.position_um - start) < abs(distance_um):
            if time.monotonic() > deadline:
                raise TimeoutError(f"jog did not cover {distance_um} µm within {timeout_s:.1f} s")
            if stage.state is MotorState.ERROR:
                raise DeviceError("stage reported an error during the jog")
            time.sleep(0.02)
        stage.stop(immediate=False)
    except BaseException:
        stage.stop(immediate=True)
        raise
    t_end = time.monotonic() + 5
    while stage.is_busy and time.monotonic() < t_end:
        time.sleep(0.05)
    return stage.position_um


def _status(name: str, stage: Any) -> str:
    if isinstance(stage, LinearStage):
        return (f"{name}: pos {stage.position_um:.2f} µm, state {stage.state.value}, "
                f"v {stage.velocity_um_s:.1f}/{stage.max_velocity_um_s:.1f} µm/s")
    return (f"{name}: angle {stage.angle_deg:.3f}°, state {stage.state.value}, "
            f"v {stage.velocity_deg_s:.2f}/{stage.max_velocity_deg_s:.2f} °/s")


def main(argv: list[str] | None = None) -> int:
    from ..config import load_config

    cfg = load_config().devices
    p = argparse.ArgumentParser(prog="python -m transferstage.drivers.kinesis", description="Manual test of the Kinesis stages.")
    p.add_argument("--path", default=cfg.kinesis_path, help="64-bit Kinesis install folder")
    p.add_argument("--z-serial", default=cfg.z_serial)
    p.add_argument("--rot-serial", default=cfg.rotator_serial)
    p.add_argument("--list", action="store_true", help="list connected Kinesis serial numbers")
    p.add_argument("--status", action="store_true", help="connect and print status of both stages")
    p.add_argument("--z-speed", type=float, metavar="UM_S", help="set Z velocity before moving")
    p.add_argument("--z-jog", type=float, metavar="UM", help=f"jog Z by this distance (signed, |d| <= {Z_JOG_MAX_UM:g})")
    p.add_argument("--z-move", type=float, metavar="UM", help="move Z to this absolute position")
    p.add_argument("--rot-speed", type=float, metavar="DEG_S", help="set rotator velocity before moving")
    p.add_argument("--rot-move", type=float, metavar="DEG", help="move rotator to this absolute angle")
    p.add_argument("--home", choices=["z", "rot"], action="append", help="home a stage (repeatable)")
    p.add_argument("--timeout", type=float, default=120.0, help="move/home timeout, s")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if args.z_jog is not None and not 0 < abs(args.z_jog) <= Z_JOG_MAX_UM:
        p.error(f"--z-jog must be non-zero and at most {Z_JOG_MAX_UM:g} µm")
    homes = set(args.home or [])
    want_z = args.status or args.z_jog is not None or args.z_move is not None or args.z_speed is not None or "z" in homes
    want_rot = args.status or args.rot_move is not None or args.rot_speed is not None or "rot" in homes
    if not (args.list or want_z or want_rot):
        args.list = True

    try:
        if args.list:
            serials = list_devices(args.path)
            print("Kinesis devices:", ", ".join(serials) or "none")
            for s, name in ((args.z_serial, "Z stage"), (args.rot_serial, "rotator")):
                print(f"  {name} {s}: {'found' if s in serials else 'NOT FOUND'}")

        stages: list[tuple[str, Any]] = []
        try:
            if want_z:
                z = KinesisZStage(args.z_serial, args.path)
                z.connect()
                stages.append(("Z", z))
                print(_status("Z", z))
                if "z" in homes:
                    print("homing Z ...")
                    z.home(wait=True, timeout_s=args.timeout)
                if args.z_speed is not None:
                    z.set_velocity(args.z_speed)
                if args.z_jog is not None:
                    print(f"jogging Z {args.z_jog:+g} µm at {z.velocity_um_s:.1f} µm/s ...")
                    jog_distance(z, args.z_jog)
                if args.z_move is not None:
                    print(f"moving Z to {args.z_move:g} µm ...")
                    z.move_to(args.z_move, wait=True, timeout_s=args.timeout)
                print(_status("Z", z))
            if want_rot:
                r = KinesisRotator(args.rot_serial, args.path)
                r.connect()
                stages.append(("rotator", r))
                print(_status("rotator", r))
                if "rot" in homes:
                    print("homing rotator ...")
                    r.home(wait=True, timeout_s=args.timeout)
                if args.rot_speed is not None:
                    r.set_velocity(args.rot_speed)
                if args.rot_move is not None:
                    print(f"moving rotator to {args.rot_move:g}° ...")
                    r.move_to(args.rot_move, wait=True, timeout_s=args.timeout)
                print(_status("rotator", r))
        except KeyboardInterrupt:
            for _, s in stages:
                s.stop(immediate=True)
            print("interrupted: stopped")
            return 1
        finally:
            for _, s in stages:
                s.close()
    except (DeviceError, ValueError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
