import pytest

from transferstage.config import Config, LimitsConfig
from transferstage.control.safety import AlarmCode, SafetyMonitor, SafetySample, Severity, safe_shutdown
from transferstage.drivers import MotorState, PsuMode, PsuReading, build_devices
from transferstage.drivers.base import Direction


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _mon(**limits) -> tuple[SafetyMonitor, FakeClock]:
    clock = FakeClock()
    return SafetyMonitor(LimitsConfig(**limits), clock=clock), clock


def _sample(clock, t=50.0, age=0.0, **kw) -> SafetySample:
    return SafetySample(temperature_c=t, temperature_time=clock.t - age, **kw)


def codes(alarms):
    return {a.code for a in alarms}


def test_normal_conditions_no_alarms():
    mon, clock = _mon()
    assert mon.check(_sample(clock)) == []
    assert not mon.tripped


def test_over_temp_trips_and_latches_until_ack():
    mon, clock = _mon()
    alarms = mon.check(_sample(clock, t=300.0))
    assert codes(alarms) == {AlarmCode.OVER_TEMP}
    assert alarms[0].severity is Severity.TRIP and mon.tripped
    # Condition clears but trip stays latched (reported as inactive).
    alarms = mon.check(_sample(clock, t=50.0))
    assert codes(alarms) == {AlarmCode.OVER_TEMP} and not alarms[0].active
    assert mon.tripped
    mon.acknowledge()
    assert not mon.tripped
    assert mon.check(_sample(clock, t=50.0)) == []


def test_over_temp_uses_lower_config_limit():
    mon, clock = _mon(t_max_c=150)
    assert codes(mon.check(_sample(clock, t=149.9))) == set()
    assert codes(mon.check(_sample(clock, t=150))) == {AlarmCode.OVER_TEMP}


def test_hard_limit_applies_even_if_config_bypassed():
    limits = LimitsConfig()
    object.__setattr__(limits, "t_max_c", 999)  # simulate a corrupted config object
    object.__setattr__(limits, "v_max", 60)
    mon = SafetyMonitor(limits)
    assert mon.t_max_c == 300 and mon.v_max == 24
    assert mon.clamp_voltage(50) == 24


def test_stale_temperature_trips():
    mon, clock = _mon(stale_temp_s=5)
    assert mon.check(_sample(clock, age=4.9)) == []
    alarms = mon.check(_sample(clock, age=5.1))
    assert codes(alarms) == {AlarmCode.STALE_TEMP} and alarms[0].is_trip


def test_never_read_temperature_trips_after_stale_time():
    mon, clock = _mon(stale_temp_s=5)
    s = SafetySample(temperature_c=None, temperature_time=None)
    assert mon.check(s) == []  # grace period after start
    clock.t += 6
    assert codes(mon.check(s)) == {AlarmCode.STALE_TEMP}


def test_nan_temperature_trips():
    mon, clock = _mon()
    assert codes(mon.check(_sample(clock, t=float("nan")))) == {AlarmCode.STALE_TEMP}


def test_psu_deviation_is_warning():
    mon, clock = _mon()
    ok = PsuReading(12.1, 1.5, PsuMode.CV)
    assert mon.check(_sample(clock, psu_command_v=12, psu_output_on=True, psu_reading=ok)) == []
    bad = PsuReading(10.0, 1.2, PsuMode.CV)
    alarms = mon.check(_sample(clock, psu_command_v=12, psu_output_on=True, psu_reading=bad))
    assert codes(alarms) == {AlarmCode.PSU_DEVIATION}
    assert alarms[0].severity is Severity.WARNING and not mon.tripped
    # Output should be off but PSU reports voltage.
    live = PsuReading(5.0, 0.6, PsuMode.CV)
    assert codes(mon.check(_sample(clock, psu_command_v=5, psu_output_on=False, psu_reading=live))) == {
        AlarmCode.PSU_DEVIATION
    }
    # Current-limited: lower voltage is expected, no warning.
    cc = PsuReading(8.0, 1.0, PsuMode.CC)
    assert mon.check(_sample(clock, psu_command_v=12, psu_output_on=True, psu_reading=cc)) == []


def test_clamp_voltage():
    mon, _ = _mon(v_max=20)
    assert mon.clamp_voltage(12) == 12
    assert mon.clamp_voltage(30) == 20
    assert mon.clamp_voltage(-1) == 0
    assert mon.clamp_voltage(float("nan")) == 0
    mon24, _ = _mon()
    assert mon24.clamp_voltage(1e6) == 24


def test_z_soft_limits():
    mon, _ = _mon(z_min_um=1000, z_max_um=20000)
    assert mon.z_move_allowed(1000) and mon.z_move_allowed(20000) and mon.z_move_allowed(5000)
    assert not mon.z_move_allowed(999)
    assert not mon.z_move_allowed(20001)
    assert not mon.z_move_allowed(float("nan"))


def _devices():
    devs = build_devices(Config(), time_scale=1)
    assert devs.connect_all() == {}
    return devs


def test_enforce_turns_psu_off_when_tripped():
    devs = _devices()
    mon = SafetyMonitor(Config().limits)
    devs.psu.set_voltage(12)
    devs.psu.output(True)
    assert mon.enforce(devs.psu) is False and devs.psu.is_on
    mon.check(SafetySample(temperature_c=305, temperature_time=None))
    assert mon.enforce(devs.psu) is True
    assert not devs.psu.is_on and devs.psu.voltage_setpoint == 0


def test_safe_shutdown_all_ok():
    devs = _devices()
    devs.z.jog(Direction.UP)
    devs.psu.set_voltage(12)
    devs.psu.output(True)
    assert safe_shutdown(devs) == []
    assert not devs.psu.is_on and devs.psu.voltage_setpoint == 0
    for _, d in devs.all():
        assert not d.is_connected


def test_safe_shutdown_continues_when_a_device_raises():
    devs = _devices()
    devs.rotator.jog(Direction.UP)
    devs.psu.set_voltage(12)
    devs.psu.output(True)
    devs.z.fault = OSError("Z cable unplugged")
    failures = safe_shutdown(devs)
    failed = {name for name, _ in failures}
    assert failed == {"z stop"}
    assert devs.rotator.state is MotorState.DISCONNECTED
    assert not devs.psu.is_on and devs.psu.voltage_setpoint == 0
    assert not devs.thermometer.is_connected


def test_safe_shutdown_survives_psu_failure():
    devs = _devices()
    devs.psu.set_voltage(12)
    devs.psu.output(True)
    devs.psu.fault = OSError("COM4 gone")
    failures = safe_shutdown(devs)
    assert {"psu output off", "psu 0 V"} <= {n for n, _ in failures}
    assert not devs.z.is_connected and not devs.rotator.is_connected and not devs.thermometer.is_connected
