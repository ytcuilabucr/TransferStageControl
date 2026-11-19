"""Heater feedforward in :class:`PID`."""

import math

import pytest

from transferstage.config import ConfigError, PidConfig, config_from_dict
from transferstage.control.pid import PID, HeaterFeedforward
from transferstage.control.ramp import SetpointRamp
from transferstage.drivers.sim import SimPowerSupply, SimThermalPlant, SimThermometer

TL_KC, TL_TI = 3.634, 0.7272  # Tyreus–Luyben PI from the 2026-11-09 autotune of the real stage


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_heater_feedforward_model():
    ff = HeaterFeedforward(0.6, 22.0)
    assert ff(100.0) == pytest.approx(math.sqrt(78 / 0.6))
    assert ff(22.0) == 0.0
    assert ff(10.0) == 0.0  # below ambient: no heat
    ramp = HeaterFeedforward(0.6, 22.0, tau_s=120.0)
    assert ramp(100.0) == pytest.approx(ff(100.0))  # no ramp: the hold voltage
    assert ramp(100.0, 10 / 60) == pytest.approx(math.sqrt((78 + 20) / 0.6))  # heating needs more
    assert ramp(100.0, -10 / 60) == pytest.approx(math.sqrt((78 - 20) / 0.6))  # cooling needs less
    with pytest.raises(ValueError):
        HeaterFeedforward(0.0)
    with pytest.raises(ValueError):
        HeaterFeedforward(0.6, tau_s=-1.0)


def test_feedforward_adds_to_output_and_integral_is_the_correction():
    ff = HeaterFeedforward(0.6, 22.0)
    pid = PID(kc=2.0, ti_min=1.0, feedforward=ff)
    v_ff = ff(100.0)
    assert pid.update(100.0, 100.0, 0.5) == pytest.approx(v_ff)  # at the set point: feedforward alone
    assert pid.last_terms.ff == pytest.approx(v_ff)
    for _ in range(20):
        pid.update(100.0, 101.0, 0.5)
    assert pid.integral < 0  # too hot: the correction goes below the feedforward
    assert pid.output == pytest.approx(v_ff + pid.last_terms.p + pid.integral)


def test_ramp_term_uses_setpoint_rate():
    ff = HeaterFeedforward(0.6, 22.0, tau_s=120.0)
    pid = PID(kc=0.0, ti_min=0.0, feedforward=ff)
    pid.update(100.0, 100.0, 0.5)
    out = pid.update(100.0 + 0.5 * 10 / 60, 100.0, 0.5)  # set point rising at 10 °C/min
    assert out == pytest.approx(ff(100.0 + 0.5 * 10 / 60, 10 / 60))
    pid.reset()
    assert pid.update(100.0, 100.0, 0.5) == pytest.approx(ff(100.0))  # no rate on the first update


def test_feedforward_output_and_integral_stay_in_range():
    pid = PID(kc=5.0, ti_min=0.1, output_low=0.0, output_high=24.0, feedforward=HeaterFeedforward(0.6, 22.0))
    for _ in range(200):
        assert 0.0 <= pid.update(100.0, 300.0, 0.5) <= 24.0  # far too hot
    assert pid.integral >= -pid.feedforward_v(100.0) - 1e-9


def test_reset_preset_can_be_negative_only_with_feedforward():
    plain = PID(kc=1.0, ti_min=1.0)
    plain.reset(integral=-3.0)
    assert plain.integral == 0.0
    with_ff = PID(kc=1.0, ti_min=1.0, feedforward=HeaterFeedforward(0.6))
    with_ff.reset(integral=-3.0)
    assert with_ff.integral == -3.0


def test_config_fields_and_from_config():
    cfg = config_from_dict({"pid": {"ff_k_c_per_v2": 0.5, "ff_t_amb_c": 21.0, "ff_tau_s": 300.0}})
    assert PID.from_config(cfg.pid).feedforward == HeaterFeedforward(0.5, 21.0, 300.0)
    assert PID.from_config(PidConfig()).feedforward is None
    for bad in ({"ff_k_c_per_v2": -0.1}, {"ff_tau_s": -1.0}):
        with pytest.raises(ConfigError):
            config_from_dict({"pid": bad})


def _ramp_run(pid: PID, start_c: float, target_c: float, *, k=0.8, tau_s=360.0, delay_s=5.0) -> list[float]:
    """Plant like the real stage, held at ``start_c``, then ramped at 10 °C/min to ``target_c``."""
    clock = FakeClock()
    psu = SimPowerSupply()
    psu.connect()
    plant = SimThermalPlant(psu, k=k, tau_s=tau_s, delay_s=delay_s, t_initial_c=start_c, clock=clock)
    th = SimThermometer(plant, noise_c=0.0, seed=1)
    th.connect()
    psu.output(True)
    ramp = SetpointRamp(start_c, clock=clock)
    ramp.set_target(target_c, 10.0, current_value=start_c)
    hold = math.sqrt(max(start_c - 22.0, 0.0) / k)
    pid.reset(integral=hold - pid.feedforward_v(start_c))  # as if it had been holding start_c
    dt, history = 0.5, []
    for _ in range(int(25 * 60 / dt)):
        psu.set_voltage(max(pid.update(ramp.program_sp(), th.read_temperature(), dt), 1.0))  # 1685B floor
        history.append(plant.temperature_c())
        clock.t += dt
    return history


def _excursion(history, start, target):
    return max(history) - target if target > start else target - min(history)


@pytest.mark.parametrize(("start", "target"), [(25.0, 100.0), (100.0, 60.0), (60.0, 140.0)])
def test_ramp_feedforward_reduces_overshoot(start, target):
    plain = _ramp_run(PID(TL_KC, TL_TI), start, target)
    ff = _ramp_run(PID(TL_KC, TL_TI, feedforward=HeaterFeedforward(0.8, 22.0, 360.0)), start, target)
    assert _excursion(ff, start, target) < 0.8 * _excursion(plain, start, target)
    assert abs(ff[-1] - target) < 0.1


def test_ramp_feedforward_tolerates_model_error():
    """k 20 % high and tau 30 % low still beats no feedforward on the cool-down."""
    plain = _ramp_run(PID(TL_KC, TL_TI), 100.0, 60.0)
    off = _ramp_run(PID(TL_KC, TL_TI, feedforward=HeaterFeedforward(0.96, 22.0, 250.0)), 100.0, 60.0)
    assert _excursion(off, 100.0, 60.0) < _excursion(plain, 100.0, 60.0)
    assert abs(off[-1] - 60.0) < 0.1
