"""Phase 6.1: relay autotune (pure logic + ThermalWorker integration, simulators, accelerated time)."""

import math

import pytest

from transferstage.config import config_from_dict
from transferstage.control.autotune import (
    RULES,
    AutotuneParams,
    AutotuneState,
    AutotuneStatus,
    RelayAutotune,
    gains_from_ultimate,
)
from transferstage.control.thermal import ThermalWorker
from transferstage.drivers.sim import SimPowerSupply, SimThermalPlant, SimThermometer


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


PERIOD = 0.5


def _worker(delay_s=10.0, t_max_c=300.0, noise=0.05):
    clock = FakeClock()
    cfg = config_from_dict({"limits": {"t_max_c": t_max_c}, "pid": {"period_ms": int(PERIOD * 1000)}})
    psu = SimPowerSupply(current_limit_a=cfg.limits.i_max)
    plant = SimThermalPlant(psu, delay_s=delay_s, clock=clock)
    therm = SimThermometer(plant, noise_c=noise, seed=7)
    psu.connect()
    therm.connect()
    worker = ThermalWorker(psu, therm, cfg, clock=clock)
    finished: list[AutotuneStatus] = []
    worker.autotune_finished.connect(finished.append)
    return worker, psu, plant, clock, finished


def _run(worker, clock, seconds):
    for _ in range(int(round(seconds / PERIOD))):
        clock.t += PERIOD
        worker.step()


def _run_until(worker, clock, cond, max_s):
    for _ in range(int(round(max_s / PERIOD))):
        if cond():
            return True
        clock.t += PERIOD
        worker.step()
    return cond()


def _hold(worker, clock, sp, seconds=900.0):
    worker.step()
    worker.set_psu_enabled(True)
    worker.set_target(sp, 0.0)
    _run(worker, clock, seconds)


# ------------------------------------------------------------------ pure logic


def test_gain_rules():
    g = gains_from_ultimate(2.0, 60.0)  # Ku 2 V/°C, Pu 1 min
    assert g["Ziegler–Nichols PI"].kc == pytest.approx(0.9) and g["Ziegler–Nichols PI"].ti_min == pytest.approx(1 / 1.2)
    zn = g["Ziegler–Nichols PID"]
    assert (zn.kc, zn.ti_min, zn.td_min) == pytest.approx((1.2, 0.5, 0.125))
    tl = g["Tyreus–Luyben PID"]
    assert (tl.kc, tl.ti_min, tl.td_min) == pytest.approx((2 / 2.2, 2.2, 1 / 6.3))
    assert g["Tyreus–Luyben PI"].td_min == 0.0
    assert set(g) == set(RULES)


def test_relay_levels_clamped_to_voltage_limits():
    at = RelayAutotune(AutotuneParams(100.0, amplitude_v=10.0, bias_v=20.0), v_max=24.0)
    assert (at.v_low, at.v_high) == (10.0, 24.0)
    at = RelayAutotune(AutotuneParams(100.0, amplitude_v=10.0, bias_v=4.0), v_max=12.0)
    assert (at.v_low, at.v_high) == (0.0, 12.0)
    with pytest.raises(ValueError):
        RelayAutotune(AutotuneParams(100.0, amplitude_v=1.0, bias_v=30.0), v_max=24.0)  # both levels at 24 V
    with pytest.raises(ValueError):
        RelayAutotune(AutotuneParams(310.0, amplitude_v=1.0, bias_v=5.0))  # set point above 300 °C
    with pytest.raises(ValueError):
        RelayAutotune(AutotuneParams(100.0, amplitude_v=-1.0, bias_v=5.0))


def test_relay_switching_timeout_and_deviation_abort():
    at = RelayAutotune(AutotuneParams(100.0, amplitude_v=2.0, bias_v=10.0, hysteresis_c=0.5, timeout_s=100.0))
    assert at.update(0.0, 90.0) == 12.0  # below SP: high
    assert at.update(1.0, 100.3) == 12.0  # inside the band: no switch
    assert at.update(2.0, 100.6) == 8.0  # above SP + h: low
    assert at.state is AutotuneState.RELAY
    assert at.update(3.0, 99.6) == 8.0
    assert at.update(4.0, 99.4) == 12.0
    assert at.update(101.0, 100.0) == 0.0
    assert at.state is AutotuneState.ABORTED and "timeout" in at.message

    at = RelayAutotune(AutotuneParams(100.0, amplitude_v=2.0, bias_v=10.0, max_deviation_c=5.0))
    at.update(0.0, 99.0)
    assert at.update(1.0, 105.5) == 0.0
    assert at.state is AutotuneState.ABORTED and "exceeded" in at.message


def test_relay_measures_synthetic_oscillation():
    # Sine with amplitude 2 °C and 40 s period; relay switches on the band crossings.
    at = RelayAutotune(AutotuneParams(50.0, amplitude_v=1.0, bias_v=5.0, hysteresis_c=0.0, cycles=3))
    t = 0.0
    while not at.state.finished:
        at.update(t, 50.0 + 2.0 * math.sin(2 * math.pi * t / 40.0 + 0.3))
        t += 0.1
    r = at.result
    assert r is not None
    assert r.pu_s == pytest.approx(40.0, abs=0.2)
    assert r.amplitude_c == pytest.approx(2.0, abs=0.01)
    assert r.ku == pytest.approx(4 * 1.0 / (math.pi * 2.0), rel=0.01)


# ------------------------------------------------------------------ worker integration


@pytest.mark.parametrize("rule", ["Tyreus–Luyben PI", "Tyreus–Luyben PID"])
def test_autotune_gains_settle_50c_step_without_large_overshoot(rule):
    worker, psu, plant, clock, finished = _worker(delay_s=10.0)
    _hold(worker, clock, 75.0)
    samples = []
    worker.sample.connect(samples.append)
    worker.start_autotune(AutotuneParams(75.0, amplitude_v=3.0, hysteresis_c=0.3, cycles=3))  # bias = present output
    assert worker.autotune_running
    assert _run_until(worker, clock, lambda: bool(finished), 3600.0)
    st = finished[0]
    assert st.state is AutotuneState.DONE, st.message
    assert any(s.mode == "autotune" for s in samples)
    assert max(s.v_cmd for s in samples) <= 24.0
    r = st.result
    assert r.ku > 0 and r.pu_s > 0 and r.period_spread < 0.3
    # PID resumed at the autotune set point
    assert not worker.autotune_running and samples[-1].mode == "pid"
    assert samples[-1].program_sp_c == 75.0

    # Fresh plant, tuned gains, 50 °C -> 100 °C step
    g = r.gains[rule]
    w2, _, plant2, clock2, _ = _worker(delay_s=10.0)
    w2.set_gains(g.kc, g.ti_min, g.td_min)
    _hold(w2, clock2, 50.0, seconds=1500.0)
    assert plant2.temperature_c() == pytest.approx(50.0, abs=0.5)
    temps = []
    w2.sample.connect(lambda s: temps.append(plant2.temperature_c()))
    w2.set_target(100.0, 0.0)
    _run(w2, clock2, 1800.0)
    overshoot = max(temps) - 100.0
    assert overshoot < 0.10 * 50.0
    assert temps[-1] == pytest.approx(100.0, abs=0.5)  # settled
    assert all(abs(t - 100.0) < 1.0 for t in temps[-600:])  # last 5 min


def test_autotune_refused_when_psu_off_or_busy():
    worker, *_, clock, finished = _worker()
    refused = []
    worker.refused.connect(refused.append)
    worker.step()
    worker.start_autotune(AutotuneParams(80.0, bias_v=10.0))
    assert refused and "PSU" in refused[-1] and not worker.autotune_running

    worker.set_psu_enabled(True)
    worker.start_autotune(AutotuneParams(80.0))  # bias from present output, which is 0 V
    assert "bias" in refused[-1] and not worker.autotune_running

    worker.start_autotune(AutotuneParams(80.0, bias_v=10.0))
    assert worker.autotune_running
    worker.set_target(50.0, 0.0)
    assert "autotune is running" in refused[-1]
    worker.start_autotune(AutotuneParams(80.0, bias_v=10.0))
    assert "already running" in refused[-1]


def test_autotune_cancel_resumes_pid_and_psu_off_aborts():
    worker, psu, plant, clock, finished = _worker()
    _hold(worker, clock, 60.0, seconds=600.0)
    worker.start_autotune(AutotuneParams(60.0, amplitude_v=2.0))
    _run(worker, clock, 20.0)
    worker.cancel_autotune()
    assert finished[-1].state is AutotuneState.ABORTED and "cancel" in finished[-1].message
    assert not worker.autotune_running
    s = worker.step()
    assert s.mode == "pid" and s.psu_on and s.program_sp_c == 60.0

    worker.start_autotune(AutotuneParams(60.0, amplitude_v=2.0))
    _run(worker, clock, 5.0)
    worker.set_psu_enabled(False)
    assert finished[-1].state is AutotuneState.ABORTED and "PSU" in finished[-1].message
    assert not psu.is_on and psu.voltage_setpoint == 0.0


def test_autotune_aborted_by_safety_trip():
    worker, psu, plant, clock, finished = _worker(delay_s=20.0, t_max_c=66.0, noise=0.0)
    worker.set_gains(0.3, 2.0, 0.0)  # gentle hold, no overshoot
    _hold(worker, clock, 58.0, seconds=1500.0)
    assert not worker.safety.tripped
    # A big relay amplitude with a long dead time overshoots past the 66 °C trip.
    worker.start_autotune(AutotuneParams(58.0, amplitude_v=12.0, hysteresis_c=0.2, max_deviation_c=30.0))
    assert worker.autotune_running
    assert _run_until(worker, clock, lambda: bool(finished), 1800.0)
    st = finished[0]
    assert st.state is AutotuneState.ABORTED and "safety trip" in st.message
    assert worker.safety.tripped
    assert not psu.is_on and not worker.psu_enabled
    _run(worker, clock, 10.0)
    assert psu.voltage_setpoint == 0.0


def test_autotune_respects_voltage_limit():
    clock = FakeClock()
    cfg = config_from_dict({"limits": {"v_max": 15.0}, "pid": {"period_ms": 500, "output_high": 15.0}})
    psu = SimPowerSupply()
    plant = SimThermalPlant(psu, clock=clock)
    therm = SimThermometer(plant, noise_c=0.0)
    psu.connect()
    therm.connect()
    worker = ThermalWorker(psu, therm, cfg, clock=clock)
    worker.step()
    worker.set_psu_enabled(True)
    worker.start_autotune(AutotuneParams(100.0, bias_v=12.0, amplitude_v=10.0))
    volts = []
    worker.sample.connect(lambda s: volts.append(s.v_cmd))
    _run(worker, clock, 300.0)
    assert max(volts) == pytest.approx(15.0)
