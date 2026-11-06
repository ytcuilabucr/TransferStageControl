import statistics
import time

import pytest

from transferstage.config import Config, config_from_dict
from transferstage.drivers import Devices, NotConnectedError, PsuMode, build_devices
from transferstage.drivers.sim import (
    SimLinearStage,
    SimPowerSupply,
    SimRotaryStage,
    SimThermalPlant,
    SimThermometer,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _psu(**kw) -> SimPowerSupply:
    psu = SimPowerSupply(**kw)
    psu.connect()
    return psu


# ----------------------------------------------------------------- PSU


def test_psu_clamps_to_instrument_range():
    psu = _psu()
    psu.set_voltage(100)
    assert psu.voltage_setpoint == 60.0
    psu.set_voltage(-3)
    assert psu.voltage_setpoint == 0.0
    psu.set_current(9)
    assert psu.current_setpoint == 5.0
    with pytest.raises(ValueError):
        psu.set_voltage(float("nan"))


def test_psu_readback_cv_cc_off():
    psu = _psu(load_ohm=8.0)
    psu.set_voltage(12)
    psu.set_current(5)
    r = psu.read_output()
    assert r.mode is PsuMode.OFF and r.voltage == 0 and r.current == 0
    psu.output(True)
    assert psu.is_on
    r = psu.read_output()
    assert r.mode is PsuMode.CV
    assert r.voltage == pytest.approx(12) and r.current == pytest.approx(1.5)
    psu.set_current(1.0)  # 12 V / 8 Ω = 1.5 A > 1 A limit -> CC
    r = psu.read_output()
    assert r.mode is PsuMode.CC
    assert r.voltage == pytest.approx(8) and r.current == pytest.approx(1.0)


def test_psu_requires_connection():
    psu = SimPowerSupply()
    with pytest.raises(NotConnectedError):
        psu.set_voltage(1)


# ----------------------------------------------------------------- plant


def test_12v_temperature_rises_monotonically_to_steady_state():
    """Real time, accelerated: 1 s wall clock = 600 s simulated = 5 tau."""
    psu = _psu()
    plant = SimThermalPlant(psu, k=0.6, tau_s=120, time_scale=600)
    t_ss = plant.steady_state_c(12)
    assert t_ss == pytest.approx(22 + 0.6 * 144)

    psu.set_voltage(12)
    psu.output(True)
    temps = []
    t_end = time.monotonic() + 1.2
    while time.monotonic() < t_end:
        temps.append(plant.temperature_c())
        time.sleep(0.01)

    assert len(temps) > 20
    assert all(b >= a for a, b in zip(temps, temps[1:])), "temperature must not decrease"
    assert temps[0] < 40
    assert temps[-1] <= t_ss
    assert temps[-1] == pytest.approx(t_ss, abs=0.02 * (t_ss - 22))


def test_plant_matches_first_order_solution():
    clock = FakeClock()
    psu = _psu()
    plant = SimThermalPlant(psu, k=0.5, tau_s=100, clock=clock)
    psu.set_voltage(10)
    psu.output(True)
    clock.advance(100)  # one tau
    expected = 22 + 50 * (1 - 2.718281828 ** -1)
    assert plant.temperature_c() == pytest.approx(expected, rel=1e-6)


def test_input_change_between_reads_is_integrated_exactly():
    clock = FakeClock()
    psu = _psu()
    plant = SimThermalPlant(psu, k=0.5, tau_s=100, clock=clock)
    psu.set_voltage(10)
    psu.output(True)
    clock.advance(50)
    psu.output(False)  # no read in between: plant must still integrate the heating
    clock.advance(50)
    heated = 22 + 50 * (1 - 2.718281828 ** -0.5)
    expected = 22 + (heated - 22) * 2.718281828 ** -0.5
    assert plant.temperature_c() == pytest.approx(expected, rel=1e-6)


def test_dead_time_delays_response():
    clock = FakeClock()
    psu = _psu()
    plant = SimThermalPlant(psu, k=0.5, tau_s=100, delay_s=20, clock=clock)
    psu.set_voltage(10)
    psu.output(True)
    clock.advance(19)
    assert plant.temperature_c() == pytest.approx(22)
    clock.advance(21)  # 20 s of effective heating
    assert plant.temperature_c() == pytest.approx(22 + 50 * (1 - 2.718281828 ** -0.2), rel=1e-6)


def test_time_scale_and_cooling():
    clock = FakeClock()
    plant = SimThermalPlant(None, tau_s=60, t_initial_c=200, time_scale=10, clock=clock)
    clock.advance(6)  # 60 s simulated
    assert plant.sim_time_s == pytest.approx(60)
    assert plant.temperature_c() == pytest.approx(22 + 178 * 2.718281828 ** -1, rel=1e-6)


def test_callable_source():
    clock = FakeClock()
    volts = {"v": 10.0}
    plant = SimThermalPlant(lambda: volts["v"], k=0.5, tau_s=100, clock=clock)
    clock.advance(1000)
    assert plant.temperature_c() == pytest.approx(72, abs=0.01)


# ----------------------------------------------------------------- thermometer


def test_thermometer_noise():
    clock = FakeClock()
    plant = SimThermalPlant(None, t_initial_c=22, clock=clock)
    th = SimThermometer(plant, noise_c=0.1, seed=1)
    with pytest.raises(NotConnectedError):
        th.read_temperature()
    th.connect()
    xs = [th.read_temperature() for _ in range(2000)]
    assert statistics.mean(xs) == pytest.approx(22, abs=0.02)
    assert statistics.stdev(xs) == pytest.approx(0.1, rel=0.1)


def test_thermometer_fault():
    th = SimThermometer(SimThermalPlant(None))
    th.connect()
    th.fault = OSError("DMM unplugged")
    with pytest.raises(OSError):
        th.read_temperature()


# ----------------------------------------------------------------- factory


def test_build_devices_all_simulated():
    devs = build_devices(Config(), time_scale=50)
    assert isinstance(devs, Devices)
    assert isinstance(devs.z, SimLinearStage)
    assert isinstance(devs.rotator, SimRotaryStage)
    assert isinstance(devs.psu, SimPowerSupply)
    assert isinstance(devs.thermometer, SimThermometer)
    assert devs.plant is not None and devs.plant.time_scale == 50
    assert devs.connect_all() == {}
    # PSU and thermometer are coupled through the plant.
    devs.psu.set_voltage(12)
    devs.psu.output(True)
    time.sleep(0.2)
    assert devs.thermometer.read_temperature() > 25


@pytest.mark.parametrize("flag", ["z", "rotator", "psu", "dmm"])
def test_build_devices_real_driver_not_yet_implemented(flag):
    cfg = config_from_dict({"devices": {"simulate": {flag: False}}})
    with pytest.raises(NotImplementedError, match=f"simulate.{flag}"):
        build_devices(cfg)
