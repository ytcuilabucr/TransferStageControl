import pytest

from transferstage.config import Config
from transferstage.control.pid import PID
from transferstage.drivers.sim import SimPowerSupply, SimThermalPlant, SimThermometer


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_p_only():
    pid = PID(kc=2.0, ti_min=0, output_low=0, output_high=24)
    assert pid.update(100, 95, 1.0) == pytest.approx(10.0)
    assert pid.update(100, 99, 1.0) == pytest.approx(2.0)
    assert pid.integral == 0
    assert pid.update(100, 105, 1.0) == 0.0  # negative clamped to output_low


def test_integral_accumulates_ni_units():
    # Kc=1, Ti=1 min: constant error 1 for 60 s adds Kc/Ti * 1 * 1 min = 1 V of integral.
    pid = PID(kc=1.0, ti_min=1.0, output_high=24)
    for _ in range(60):
        out = pid.update(11, 10, 1.0)
    assert pid.integral == pytest.approx(1.0)
    assert out == pytest.approx(1.0 + 1.0)
    # Halving Ti doubles the rate.
    pid2 = PID(kc=1.0, ti_min=0.5)
    for _ in range(60):
        pid2.update(11, 10, 1.0)
    assert pid2.integral == pytest.approx(2.0)


def test_derivative_on_measurement():
    pid = PID(kc=1.0, ti_min=0, td_min=1.0, output_low=-24, output_high=24)
    pid.update(50, 20, 60.0)  # first call: no derivative
    assert pid.last_terms.d == 0
    pid.update(80, 20, 60.0)  # set-point step: no derivative kick
    assert pid.last_terms.d == 0
    pid.update(80, 21, 60.0)  # PV rises 1 °C in 1 min -> D = -Kc*Td*1 = -1
    assert pid.last_terms.d == pytest.approx(-1.0)


def test_clamp_and_anti_windup():
    pid = PID(kc=1.0, ti_min=0.1, output_high=10)
    for _ in range(600):  # big error for 10 min: would wind up enormously without anti-windup
        out = pid.update(300, 20, 1.0)
        assert 0 <= out <= 10
    assert out == 10 and pid.last_terms.saturated
    assert pid.integral <= 10
    # Error reverses: output must leave saturation immediately, not after unwinding.
    out = pid.update(20, 25, 1.0)
    assert out < 10


def test_output_high_cannot_exceed_24v():
    with pytest.raises(ValueError):
        PID(1, 1, output_high=30)
    pid = PID(1, 1)
    with pytest.raises(ValueError):
        pid.set_output_limits(0, 24.1)
    for _ in range(100):
        assert pid.update(1000, 0, 10) <= 24


def test_reset_and_disable_integral():
    pid = PID(kc=1.0, ti_min=1.0)
    for _ in range(30):
        pid.update(15, 10, 1.0)
    assert pid.integral > 0
    pid.reset()
    assert pid.integral == 0 and pid.output == 0
    assert pid.update(15, 10, 1.0) == pytest.approx(5 + 5 / 60)  # fresh start, rectangular first step
    pid.set_tunings(1.0, 0.0)
    assert pid.integral == 0
    assert pid.update(15, 10, 1.0) == pytest.approx(5)


def test_rejects_bad_inputs():
    pid = PID(1, 1)
    with pytest.raises(ValueError):
        pid.update(10, float("nan"), 1)
    with pytest.raises(ValueError):
        pid.update(10, 5, 0)
    with pytest.raises(ValueError):
        PID(1, -1)


def test_closed_loop_settles_at_100c():
    """Default config gains on the simulated heater, 1 s wall = 600 s simulated (fake clock)."""
    clock = FakeClock()
    psu = SimPowerSupply()
    psu.connect()
    plant = SimThermalPlant(psu, k=0.6, tau_s=120, delay_s=2, clock=clock)
    th = SimThermometer(plant, noise_c=0.05, seed=3)
    th.connect()
    pid = PID.from_config(Config().pid)
    psu.output(True)

    dt = 0.5
    history = []
    for _ in range(int(1800 / dt)):  # 30 simulated minutes
        t = th.read_temperature()
        psu.set_voltage(pid.update(100.0, t, dt))
        history.append(plant.temperature_c())
        clock.advance(dt)

    tail = history[-int(300 / dt):]  # last 5 minutes
    assert max(tail) <= 101 and min(tail) >= 99
    assert max(history) < 110  # no wild overshoot
