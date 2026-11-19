"""Kinesis driver logic with a fake .NET API (no pythonnet or hardware needed)."""

import pytest

from transferstage.config import Config, config_from_dict
from transferstage.drivers import DeviceError, Direction, MotorState, NotConnectedError, build_devices
from transferstage.drivers import kinesis
from transferstage.drivers.kinesis import KinesisRotator, KinesisZStage, mm_to_um, um_to_mm


def test_unit_conversions():
    assert um_to_mm(100) == 0.1
    assert mm_to_um(0.1) == pytest.approx(100)
    assert mm_to_um(um_to_mm(12345.6)) == pytest.approx(12345.6)
    assert um_to_mm(25000) == 25.0


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeDevice:
    def __init__(self, serial, *, vel=2.0, vmax=2.6, lo=0.0, hi=25.0, pos=1.0):
        self.serial = serial
        self.calls = []
        self.Position = pos
        self.State = "Idle"
        self.Status = Obj(IsError=False, IsHoming=False, IsInMotion=False, IsMoving=False, IsJogging=False)
        self._vel = Obj(MaxVelocity=vel, Acceleration=1.5)
        self.AdvancedMotorLimits = Obj(VelocityMaximum=vmax, LengthMinimum=lo, LengthMaximum=hi)

    def __getattr__(self, name):  # record any other method call
        def method(*args):
            self.calls.append((name, args))
        return method

    def IsSettingsInitialized(self):
        return False

    def GetVelocityParams(self):
        return self._vel

    def SetVelocityParams(self, v, a):
        self.calls.append(("SetVelocityParams", (v, a)))
        self._vel = Obj(MaxVelocity=v, Acceleration=a)

    def set_busy(self, homing=False):
        self.Status.IsInMotion = True
        self.Status.IsHoming = homing


class FakeApi:
    forward, backward = "Forward", "Backward"
    use_device_settings = "UseDeviceSettings"

    def __init__(self, serials=("27270114", "55524924"), **dev_kw):
        self.serials = list(serials)
        self.dev_kw = dev_kw
        self.devices = []

    def to_decimal(self, x):
        return float(x)

    def from_decimal(self, d):
        return float(d)

    def device_list(self):
        return self.serials

    def create_dc_servo(self, serial):
        self.devices.append(FakeDevice(serial, **self.dev_kw))
        return self.devices[-1]

    create_cage_rotator = create_dc_servo


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(kinesis.time, "sleep", lambda s: None)


def connected_z(**kw):
    api = FakeApi(**kw)
    z = KinesisZStage("27270114", api=api)
    z.connect()
    return z, api.devices[0]


def test_connect_sequence_and_units():
    z, dev = connected_z()
    names = [c[0] for c in dev.calls]
    assert names == ["Connect", "WaitForSettingsInitialized", "StartPolling", "EnableDevice", "LoadMotorConfiguration"]
    assert dev.calls[2] == ("StartPolling", (250,))
    assert dev.calls[4] == ("LoadMotorConfiguration", ("27270114", "UseDeviceSettings"))
    assert z.position_um == pytest.approx(1000)
    assert z.velocity_um_s == pytest.approx(2000)
    assert z.max_velocity_um_s == pytest.approx(2600)
    assert z.state is MotorState.IDLE


def test_missing_serial():
    z = KinesisZStage("999", api=FakeApi())
    with pytest.raises(DeviceError, match="999 not found"):
        z.connect()
    assert z.state is MotorState.DISCONNECTED


def test_move_to_sends_mm_and_reports_pending_motion():
    z, dev = connected_z()
    z.move_to(12500)
    assert ("MoveTo", (12.5, 0)) in dev.calls
    assert z.state is MotorState.MOVING  # status not yet updated by polling: pending window
    dev.set_busy()
    assert z.state is MotorState.MOVING
    dev.Status.IsInMotion = False
    dev.Position = 12.5
    assert z.state is MotorState.IDLE


def test_move_to_wait(monkeypatch):
    z, dev = connected_z()
    ticks = iter([True, True, False])

    def state_seq():
        busy = next(ticks, False)
        dev.Status.IsInMotion = busy
        if not busy:
            dev.Position = 3.0
        return original()

    original = z._state
    monkeypatch.setattr(z, "_state", state_seq)
    z.move_to(3000, wait=True, timeout_s=5)
    assert z.position_um == pytest.approx(3000)


def test_move_outside_travel_rejected():
    z, dev = connected_z()
    with pytest.raises(ValueError):
        z.move_to(26000)
    with pytest.raises(ValueError):
        z.move_to(-1)
    assert not any(c[0] == "MoveTo" for c in dev.calls)


def test_set_velocity_converts_and_clamps():
    z, dev = connected_z()
    z.set_velocity(500)
    assert dev.calls[-1] == ("SetVelocityParams", (0.5, 1.5))
    assert z.velocity_um_s == 500
    z.set_velocity(10_000)
    assert z.velocity_um_s == pytest.approx(2600)
    with pytest.raises(ValueError):
        z.set_velocity(0)


def test_jog_and_stop():
    z, dev = connected_z()
    z.jog(Direction.UP, 100)
    assert dev.calls[-2] == ("SetVelocityParams", (0.1, 1.5))
    assert dev.calls[-1] == ("MoveContinuous", ("Forward",))
    z.jog(Direction.DOWN)
    assert dev.calls[-1] == ("MoveContinuous", ("Backward",))
    assert dev.calls[-2] == ("SetVelocityParams", (2.0, 1.5))
    z.stop()
    assert dev.calls[-1] == ("StopImmediate", ())
    z.stop(immediate=False)
    assert dev.calls[-1] == ("Stop", (0,))


def test_state_mapping():
    z, dev = connected_z()
    dev.set_busy(homing=True)
    assert z.state is MotorState.HOMING
    dev.Status.IsHoming = dev.Status.IsInMotion = False
    dev.State = "Stopping"
    assert z.state is MotorState.STOPPING
    dev.State = "Idle"
    dev.Status.IsError = True
    assert z.state is MotorState.ERROR


def test_close_stops_if_moving_and_releases():
    z, dev = connected_z()
    dev.set_busy()
    z.close()
    names = [c[0] for c in dev.calls]
    assert names[-3:] == ["StopImmediate", "StopPolling", "Disconnect"]
    assert "DisableDevice" not in names
    assert z.state is MotorState.DISCONNECTED
    with pytest.raises(NotConnectedError):
        z.move_to(0)
    z.close()


def test_rotator_degrees_no_travel_check():
    api = FakeApi(vel=10.0, vmax=10.0, lo=0.0, hi=360.0, pos=0.0)
    r = KinesisRotator("55524924", api=api)
    r.connect()
    dev = api.devices[0]
    r.move_to(400.0)
    assert ("MoveTo", (40.0, 0)) in dev.calls
    r.move_to(-30.0)
    assert ("MoveTo", (330.0, 0)) in dev.calls

    assert r.velocity_deg_s == 10.0
    r.home()
    assert dev.calls[-1] == ("Home", (0,))
    assert r.state is MotorState.HOMING


def test_rotator_angle_helpers():
    from transferstage.drivers.kinesis import signed_deg, wrap_deg

    assert wrap_deg(-30.0) == 330.0 and wrap_deg(-90.0) == 270.0 and wrap_deg(90.0) == 90.0
    assert wrap_deg(360.0) == 0.0 and wrap_deg(-1e-17) == 0.0
    assert signed_deg(330.0) == -30.0 and signed_deg(270.0) == -90.0 and signed_deg(180.0) == 180.0


def test_build_devices_uses_kinesis():
    cfg = config_from_dict({"devices": {"simulate": {"z": False, "rotator": False}}})
    devs = build_devices(cfg)
    assert isinstance(devs.z, KinesisZStage) and devs.z.serial == "27270114"
    assert isinstance(devs.rotator, KinesisRotator) and devs.rotator.serial == "55524924"


@pytest.mark.hardware
def test_hardware_list_devices():
    cfg = Config().devices
    serials = kinesis.list_devices(cfg.kinesis_path)
    assert cfg.z_serial in serials and cfg.rotator_serial in serials
