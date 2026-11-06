import pytest

from transferstage.control.profile import Phase, ProfileError, ProfileRunner, Segment, TemperatureProfile
from transferstage.control.ramp import SetpointRamp
from transferstage.control.pid import PID
from transferstage.drivers.sim import SimPowerSupply, SimThermalPlant


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ----------------------------------------------------------------- ramp


def test_ramp_up_and_completion():
    clock = FakeClock()
    r = SetpointRamp(25, clock=clock)
    assert not r.is_ramping
    r.set_target(85, 10)  # 60 °C at 10 °C/min = 6 min
    assert r.ascending and r.original_sp == 25 and r.is_ramping
    clock.advance(60)
    assert r.program_sp() == pytest.approx(35)
    assert r.remaining_s() == pytest.approx(300)
    clock.advance(300)
    assert r.program_sp() == pytest.approx(85)
    assert not r.is_ramping
    clock.advance(600)
    assert r.program_sp() == 85  # never overshoots


def test_ramp_down():
    clock = FakeClock()
    r = SetpointRamp(clock=clock)
    r.set_target(50, 5, current_value=200)
    assert not r.ascending
    clock.advance(120)
    assert r.program_sp() == pytest.approx(190)
    clock.advance(10_000)
    assert r.program_sp() == 50 and not r.is_ramping


def test_retarget_mid_ramp_continues_from_program_sp():
    clock = FakeClock()
    r = SetpointRamp(20, clock=clock)
    r.set_target(120, 20)
    clock.advance(90)  # 30 °C up -> 50
    r.set_target(30, 10)  # reverse
    assert r.original_sp == pytest.approx(50) and not r.ascending
    clock.advance(60)
    assert r.program_sp() == pytest.approx(40)
    clock.advance(60)
    assert r.program_sp() == pytest.approx(30) and not r.is_ramping


def test_rate_zero_is_step_and_target_clamped():
    clock = FakeClock()
    r = SetpointRamp(20, clock=clock)
    r.set_target(150, 0)
    assert r.program_sp() == 150 and not r.is_ramping
    r.set_target(500, 10)
    assert r.target == 300
    r2 = SetpointRamp(20, max_c=250, clock=clock)
    r2.set_target(280, 0)
    assert r2.program_sp() == 250
    with pytest.raises(ValueError):
        SetpointRamp(max_c=400)
    with pytest.raises(ValueError):
        r.set_target(100, -1)


# ----------------------------------------------------------------- profile


def _three_segments() -> TemperatureProfile:
    return TemperatureProfile(
        [
            Segment(100, 10, 2),  # 25->100: 7.5 min ramp, 2 min hold
            Segment(150, 0, 1),  # step to 150, 1 min hold
            Segment(50, 20, 0),  # 150->50: 5 min ramp
        ],
        name="test",
    )


def test_three_segment_profile_in_simulated_time():
    clock = FakeClock()
    runner = ProfileRunner(_three_segments(), start_c=25, clock=clock)
    assert runner.total_s == pytest.approx((7.5 + 2 + 1 + 5) * 60)
    runner.start()

    def at(t):
        clock.t = t
        return runner.status()

    s = at(0)
    assert (s.segment, s.phase, s.program_sp) == (0, Phase.RAMP, 25)
    assert at(225).program_sp == pytest.approx(62.5)
    s = at(500)
    assert (s.segment, s.phase, s.program_sp) == (0, Phase.HOLD, 100)
    s = at(9.5 * 60 + 1)
    assert (s.segment, s.phase, s.program_sp) == (1, Phase.HOLD, 150)
    s = at(10.5 * 60 + 150)
    assert (s.segment, s.phase) == (2, Phase.RAMP) and s.program_sp == pytest.approx(100)
    s = at(10_000)
    assert s.done and s.program_sp == 50 and s.segment == 2 and s.remaining_s == 0

    sps = [st.program_sp for st in runner.iter_setpoints(30)]
    assert sps[0] == 25 and sps[-1] == 50
    assert max(sps) == 150


def test_profile_drives_simulated_heater():
    """Closed loop follows the profile's first ramp+hold within a few degrees."""
    clock = FakeClock()
    psu = SimPowerSupply()
    psu.connect()
    plant = SimThermalPlant(psu, clock=clock)
    pid = PID(0.5, 1.0)
    profile = TemperatureProfile([Segment(80, 10, 10)])
    runner = ProfileRunner(profile, start_c=22, clock=clock)
    runner.start()
    psu.output(True)
    dt = 0.5
    while not runner.status().done:
        sp = runner.status().program_sp
        psu.set_voltage(pid.update(sp, plant.temperature_c(), dt))
        clock.advance(dt)
    assert plant.temperature_c() == pytest.approx(80, abs=1)


def test_profile_yaml_roundtrip(tmp_path):
    p = _three_segments()
    path = p.save(tmp_path / "prof.yaml")
    q = TemperatureProfile.load(path)
    assert q == p


@pytest.mark.parametrize(
    "data",
    [
        {"segments": []},
        {"segments": [{"ramp_to": 301, "rate_deg_min": 1}]},
        {"segments": [{"ramp_to": 100, "rate_deg_min": -1}]},
        {"segments": [{"ramp_to": 100, "rate_deg_min": 1, "hold_min": -2}]},
        {"segments": [{"ramp_to": 100}]},
        {"segments": [{"ramp_to": 100, "rate_deg_min": 1, "bogus": 1}]},
        {"segments": [{"ramp_to": 100, "rate_deg_min": 1}], "extra": 1},
    ],
)
def test_profile_validation(data):
    with pytest.raises(ProfileError):
        TemperatureProfile.from_dict(data)
