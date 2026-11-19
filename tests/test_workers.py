"""Phase 4: motion/thermal workers and the data logger (pytest-qt, simulators only)."""

import csv
import time
from datetime import datetime

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from transferstage.config import Config, config_from_dict
from transferstage.control.motion import MotionStatus, MotionWorker
from transferstage.control.profile import Segment, TemperatureProfile
from transferstage.control.safety import AlarmCode, SafetyMonitor
from transferstage.control.thermal import ThermalSample, ThermalWorker
from transferstage.control.threads import start_worker, stop_worker
from transferstage.datalog import COLUMNS, DataLogger
from transferstage.drivers.base import DeviceError, MotorState
from transferstage.drivers.sim import (
    SimLinearStage,
    SimPowerSupply,
    SimRotaryStage,
    SimThermalPlant,
    SimThermometer,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ===================================================================== motion


def _motion(clock=None, **limits):
    clock = clock or FakeClock()
    z = SimLinearStage(velocity_um_s=500.0, position_um=1000.0, stop_decel_s=0.0, clock=clock)
    rot = SimRotaryStage(clock=clock)
    z.connect()
    rot.connect()
    cfg = config_from_dict({"limits": {"z_min_um": 0.0, "z_max_um": 5000.0, **limits}})
    safety = SafetyMonitor(cfg.limits, clock=clock)
    worker = MotionWorker(z, rot, safety, clock=clock)
    statuses: list[MotionStatus] = []
    worker.status.connect(statuses.append)
    return worker, z, rot, clock, statuses


def test_motion_jog_and_stop():
    worker, z, _, clock, statuses = _motion()
    worker.jog_start(1)
    clock.advance(1.0)
    worker.poll()
    st = statuses[-1]
    assert st.z_state is MotorState.MOVING
    assert st.jog_direction == 1
    assert st.z_um == pytest.approx(1500.0)
    assert st.seconds_since_last_motion == 0.0

    worker.jog_stop()
    clock.advance(2.0)
    worker.poll()
    st = statuses[-1]
    assert st.z_state is MotorState.IDLE
    assert st.jog_direction == 0
    assert st.z_um == pytest.approx(1500.0)
    assert st.seconds_since_last_motion == pytest.approx(2.0)

    worker.jog_start(-1)
    clock.advance(0.5)
    worker.poll()
    assert statuses[-1].z_um == pytest.approx(1250.0)
    worker.cancel_motion()
    assert z.state is MotorState.IDLE


def test_motion_jog_stops_at_soft_limit():
    worker, z, _, clock, statuses = _motion()
    refused = []
    worker.refused.connect(refused.append)
    worker.jog_start(1)
    clock.advance(100.0)  # far past the 5000 µm soft limit at 500 µm/s
    worker.poll()
    assert statuses[-1].z_um == pytest.approx(5000.0)
    assert statuses[-1].z_state is MotorState.IDLE
    assert statuses[-1].jog_direction == 0
    worker.jog_start(1)  # already at the limit
    assert refused and "soft limit" in refused[-1]


def test_motion_move_respects_soft_limits():
    worker, z, _, clock, _ = _motion()
    refused = []
    worker.refused.connect(refused.append)
    worker.move_z_to(6000.0)
    assert refused and z.state is MotorState.IDLE
    worker.move_z_to(2000.0)
    clock.advance(10.0)
    assert z.position_um == pytest.approx(2000.0)


def test_motion_set_speed_clamps_and_applies_to_jog():
    worker, z, _, clock, statuses = _motion()
    worker.set_speed(1e9)
    assert z.velocity_um_s == z.max_velocity_um_s
    worker.set_speed(100.0)
    worker.jog_start(1)
    clock.advance(1.0)
    worker.set_speed(200.0)  # re-issues the jog at the new speed
    clock.advance(1.0)
    assert z.position_um == pytest.approx(1300.0)
    worker.jog_stop()


def test_motion_slow_approach_caps_downward_speed():
    worker, z, _, clock, statuses = _motion()
    refused = []
    worker.refused.connect(refused.append)
    worker.set_speed(500.0)
    worker.set_slow_approach(True)
    assert statuses[-1].slow_approach

    worker.jog_start(-1)  # down: capped at 25 µm/s
    clock.advance(2.0)
    assert z.position_um == pytest.approx(950.0)
    worker.set_speed(200.0)  # changing the speed mid-jog does not lift the cap
    clock.advance(2.0)
    assert z.position_um == pytest.approx(900.0)
    worker.jog_stop()

    worker.jog_start(1)  # up: full requested speed
    clock.advance(1.0)
    assert z.position_um == pytest.approx(1100.0)
    worker.jog_stop()

    worker.move_z_to(1000.0)  # move to a lower position: capped too
    clock.advance(1.0)
    assert z.position_um == pytest.approx(1075.0)
    worker.set_slow_approach(False)  # lifting the cap re-issues the move at full speed
    clock.advance(0.25)
    assert z.position_um == pytest.approx(1025.0)
    clock.advance(1.0)
    worker.poll()
    assert z.position_um == pytest.approx(1000.0)
    assert z.velocity_um_s == pytest.approx(200.0)

    worker.jog_start(-1)  # turning it on mid-jog slows the running jog down
    clock.advance(1.0)
    worker.set_slow_approach(True)
    clock.advance(1.0)
    assert z.position_um == pytest.approx(775.0)
    worker.jog_stop()

    worker.home_z()
    assert refused and "slow approach" in refused[-1]


def test_motion_safe_lock_blocks_everything_but_stop():
    worker, z, rot, clock, statuses = _motion()
    refused = []
    worker.refused.connect(refused.append)
    worker.jog_start(1)
    worker.set_safe_lock(True)
    assert statuses[-1].safe_lock is True

    for cmd in (
        lambda: worker.jog_start(1),
        lambda: worker.move_z_to(100.0),
        lambda: worker.set_speed(100.0),
        lambda: worker.rotate_to(10.0),
        lambda: worker.home_z(),
        lambda: worker.home_rotator(),
    ):
        cmd()
    assert len(refused) == 6
    assert all("safe lock" in r for r in refused)
    assert rot.state is MotorState.IDLE

    # Motion already running is not stopped by the lock, but stop/cancel still work.
    assert z.state is MotorState.MOVING
    worker.jog_stop()
    assert z.state is MotorState.IDLE
    worker.set_safe_lock(False)
    worker.rotate_to(10.0)
    assert rot.state is MotorState.MOVING
    worker.cancel_motion()
    assert rot.state is MotorState.IDLE


def test_rotate_to_limited_to_plus_minus_90():
    worker, _, rot, _, _ = _motion()
    refused = []
    worker.refused.connect(refused.append)
    worker.rotate_to(120.0)
    worker.rotate_to(-90.5)
    assert len(refused) == 2 and rot.state is MotorState.IDLE
    worker.rotate_to(-90.0)
    assert rot.state is MotorState.MOVING


def test_motion_device_errors_become_signals():
    worker, z, _, clock, statuses = _motion()
    errors = []
    worker.error.connect(errors.append)
    z.fault = DeviceError("cable unplugged")
    worker.jog_start(1)
    worker.move_z_to(100.0)
    assert len([e for e in errors if "cable unplugged" in e]) == 2
    worker.poll()
    assert statuses[-1].z_state is MotorState.ERROR
    z.fault = None
    worker.poll()
    assert statuses[-1].z_state is MotorState.IDLE

    class BrokenStage:
        def __getattr__(self, name):
            raise DeviceError("USB gone")

    real = worker.rotator
    worker.rotator = BrokenStage()
    worker.poll()
    worker.poll()  # same failure again: not re-reported
    polls = [e for e in errors if e.startswith("poll failed")]
    assert len(polls) == 1 and "USB gone" in polls[0]
    st = statuses[-1]
    assert st.rotator_state is MotorState.ERROR and st.rotator_deg is None
    assert st.z_um == pytest.approx(1000.0)
    worker.rotator = real
    worker.poll()
    assert statuses[-1].rotator_state is MotorState.IDLE


def test_motion_worker_in_thread(qtbot):
    worker, z2, _, _, _ = _motion(clock=time.monotonic)  # real time in the threaded test

    class Gui(QObject):
        jog = pyqtSignal(int)
        stop = pyqtSignal()

    gui = Gui()
    gui.jog.connect(worker.jog_start)
    gui.stop.connect(worker.jog_stop)
    thread = start_worker(worker, "motion")
    try:
        with qtbot.waitSignal(worker.status, timeout=2000):
            pass
        gui.jog.emit(1)
        qtbot.waitUntil(lambda: z2.state is MotorState.MOVING, timeout=2000)
        with qtbot.waitSignal(
            worker.status, timeout=2000, check_params_cb=lambda s: s.jog_direction == 1 and s.z_um > 1000.0
        ):
            pass
        gui.stop.emit()
        with qtbot.waitSignal(
            worker.status, timeout=2000, check_params_cb=lambda s: s.z_state is MotorState.IDLE and s.jog_direction == 0
        ):
            pass
    finally:
        assert stop_worker(worker, thread)


# ===================================================================== thermal


def _thermal(t_max_c=300.0, t_initial_c=None, noise=0.05, **pid):
    clock = FakeClock()
    cfg = config_from_dict({"limits": {"t_max_c": t_max_c}, "pid": {"period_ms": 500, **pid}})
    psu = SimPowerSupply(current_limit_a=cfg.limits.i_max)
    plant = SimThermalPlant(psu, t_initial_c=t_initial_c, clock=clock)
    therm = SimThermometer(plant, noise_c=noise, seed=1)
    psu.connect()
    therm.connect()
    worker = ThermalWorker(psu, therm, cfg, clock=clock)
    samples: list[ThermalSample] = []
    worker.sample.connect(samples.append)
    return worker, psu, plant, therm, clock, samples


def _run(worker, clock, seconds, period_s=0.5):
    for _ in range(int(round(seconds / period_s))):
        clock.advance(period_s)
        worker.step()


def test_thermal_ramp_reaches_100c_in_sim_time():
    worker, psu, plant, _, clock, samples = _thermal()
    worker.step()  # first reading initialises the idle set point at room temperature
    assert samples[-1].program_sp_c == pytest.approx(22.0, abs=0.5)

    worker.set_psu_enabled(True)
    worker.set_target(100.0, 10.0)
    _run(worker, clock, 240.0)
    s = samples[-1]
    assert s.ramping and s.psu_on
    assert s.target_sp_c == 100.0
    assert s.program_sp_c == pytest.approx(22.0 + 40.0, abs=1.0)  # 10 °C/min for 4 min
    assert s.temperature_c == pytest.approx(s.program_sp_c, abs=5.0)  # tracks the ramp

    _run(worker, clock, 15 * 60.0)  # ramp ends at ~7.8 min, then settle
    s = samples[-1]
    assert not s.ramping
    assert s.program_sp_c == 100.0
    assert plant.temperature_c() == pytest.approx(100.0, abs=0.5)
    assert 0 < s.v_cmd <= 24.0
    assert s.v_read == pytest.approx(s.v_cmd)
    assert not s.alarms
    assert max(x.temperature_c for x in samples) < 105.0  # no large overshoot


def test_thermal_pid_only_applied_while_psu_enabled():
    worker, psu, plant, _, clock, samples = _thermal()
    worker.set_target(80.0, 0.0)
    _run(worker, clock, 30.0)
    assert samples[-1].v_cmd == 0.0 and not psu.is_on
    assert psu.voltage_setpoint == 0.0

    worker.set_psu_enabled(True)
    _run(worker, clock, 5.0)
    assert samples[-1].v_cmd > 0 and psu.is_on

    worker.set_psu_enabled(False)
    assert not psu.is_on and psu.voltage_setpoint == 0.0
    assert worker.pid.integral == 0.0
    worker.step()
    assert samples[-1].v_cmd == 0.0


def test_thermal_over_temperature_trip_turns_psu_off():
    worker, psu, plant, _, clock, samples = _thermal(t_max_c=50.0, t_initial_c=45.0, noise=0.0)
    alarms = []
    worker.alarm.connect(alarms.append)
    worker.set_psu_enabled(True)
    worker.set_target(49.0, 0.0)
    worker.step()
    assert psu.is_on

    plant._temp = 60.0  # something heats the stage beyond the limit
    clock.advance(0.5)
    s = worker.step()
    assert s.tripped and not s.psu_on
    assert not psu.is_on and psu.voltage_setpoint == 0.0
    assert not worker.psu_enabled
    assert [a.code for a in alarms] == [AlarmCode.OVER_TEMP]

    # Latched: cannot re-enable until acknowledged (and the condition has cleared).
    refused = []
    worker.refused.connect(refused.append)
    plant._temp = 30.0
    _run(worker, clock, 2.0)
    worker.set_psu_enabled(True)
    assert refused and not psu.is_on
    assert samples[-1].tripped  # still latched (inactive)
    worker.acknowledge_alarm()
    worker.set_psu_enabled(True)
    assert psu.is_on
    s = worker.step()
    assert not s.alarms and s.psu_on


def test_thermal_target_above_limit_refused():
    worker, *_ = _thermal(t_max_c=150.0)
    refused = []
    worker.refused.connect(refused.append)
    worker.set_target(200.0, 10.0)
    assert refused
    worker.start_profile(TemperatureProfile([Segment(200.0, 10.0)]))
    assert len(refused) == 2 and not worker.profile_running


def test_thermal_stale_reading_trips():
    worker, psu, _, therm, clock, samples = _thermal()
    errors, alarms = [], []
    worker.error.connect(errors.append)
    worker.alarm.connect(alarms.append)
    worker.set_psu_enabled(True)
    worker.set_target(60.0, 0.0)
    _run(worker, clock, 2.0)
    therm.fault = DeviceError("GPIB timeout")
    _run(worker, clock, 4.0)
    assert psu.is_on  # within stale_temp_s the last command is held
    assert len(errors) == 1
    _run(worker, clock, 2.0)
    assert not psu.is_on
    assert AlarmCode.STALE_TEMP in [a.code for a in alarms]


def test_thermal_profile_runs_and_finishes():
    worker, psu, plant, _, clock, samples = _thermal()
    finished = []
    worker.profile_finished.connect(lambda: finished.append(True))
    worker.step()
    worker.set_psu_enabled(True)
    worker.start_profile(TemperatureProfile([Segment(60.0, 20.0, 1.0), Segment(40.0, 0.0, 0.5)]))
    _run(worker, clock, 60.0)
    s = samples[-1]
    assert s.profile_segment == 0 and s.profile_phase == "ramp"
    assert s.program_sp_c == pytest.approx(42.0, abs=0.5)
    _run(worker, clock, 6 * 60.0)
    assert finished and not worker.profile_running
    assert samples[-1].program_sp_c == 40.0
    assert plant.temperature_c() == pytest.approx(40.0, abs=1.0)


def test_thermal_worker_in_thread(qtbot):
    cfg = config_from_dict({"pid": {"period_ms": 20}})
    psu = SimPowerSupply()
    plant = SimThermalPlant(psu, time_scale=50.0)
    therm = SimThermometer(plant, seed=1)
    psu.connect()
    therm.connect()
    worker = ThermalWorker(psu, therm, cfg)

    class Gui(QObject):
        enable = pyqtSignal(bool)
        target = pyqtSignal(float, float)

    gui = Gui()
    gui.enable.connect(worker.set_psu_enabled)
    gui.target.connect(worker.set_target)
    thread = start_worker(worker, "thermal")
    try:
        with qtbot.waitSignal(worker.sample, timeout=2000):
            pass
        gui.enable.emit(True)
        gui.target.emit(50.0, 0.0)
        with qtbot.waitSignal(worker.sample, timeout=2000, check_params_cb=lambda s: s.psu_on and s.v_cmd > 0):
            pass
    finally:
        assert stop_worker(worker, thread)
    assert not psu.is_on  # stopping the worker switches the heater off


# ===================================================================== data logger


def test_datalogger_writes_csv(qtbot, tmp_path):
    logger = DataLogger(tmp_path / "logs", 0.05, now=lambda: datetime(2026, 11, 6, 14, 30, 5))
    path = logger.start()
    assert path.name == "20261106_143005.csv"
    second = DataLogger(tmp_path / "logs", 0.05, now=lambda: datetime(2026, 11, 6, 14, 30, 5))
    assert second.start().name == "20261106_143005_1.csv"  # same second: no overwrite
    second.close()

    worker, psu, plant, _, clock, _ = _thermal()
    worker.sample.connect(logger.on_thermal)
    worker.set_psu_enabled(True)
    worker.set_target(50.0, 10.0)
    worker.step()
    m_worker, *_ = _motion()
    m_worker.status.connect(logger.on_motion)
    m_worker.poll()

    qtbot.waitUntil(lambda: logger.rows_written >= 3, timeout=3000)
    logger.close()
    logger.close()  # idempotent
    assert not logger.is_open

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with path.open(newline="", encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == COLUMNS
    assert len(rows) >= 3
    r = rows[-1]
    assert float(r["temperature_c"]) == pytest.approx(22.0, abs=1.0)
    assert float(r["target_sp_c"]) == 50.0
    assert r["psu_on"] == "1" and r["ramping"] == "1"
    assert float(r["z_um"]) == pytest.approx(1000.0)
    assert r["z_state"] == "Idle" and r["rotator_state"] == "Idle"
    assert r["safe_lock"] == "0" and r["alarms"] == ""
    assert r["timestamp"].startswith("2026-11-06T14:30:05")


def test_datalogger_from_config_relative_dir():
    logger = DataLogger.from_config(Config().logging)
    assert logger.directory.is_absolute() and logger.directory.name == "logs"
    assert logger.interval_s == 1.0


def test_datalogger_mark_sample_age_and_metadata(tmp_path):
    t = [100.0]
    logger = DataLogger(tmp_path / "logs", 1.0, clock=lambda: t[0], now=lambda: datetime(2026, 11, 6, 15, 0, 0))
    path = logger.open({"app_version": "x", "config": Config().to_dict()})
    assert logger.meta_path == path.with_suffix(".meta.yaml")
    import yaml

    meta = yaml.safe_load(logger.meta_path.read_text(encoding="utf-8"))
    assert meta["data_file"] == path.name and meta["config"]["logging"]["interval_s"] == 1.0

    worker, *_ = _thermal()
    sample = worker.step()
    logger.write_row()  # no thermal sample yet: empty age
    logger.on_thermal(sample)
    t[0] = 101.0
    logger.write_row()
    t[0] = 105.5  # thermal loop stalled for 5.5 s
    logger.write_row()
    assert logger.mark("  stamp\ncontact ") == "stamp contact"
    assert logger.mark() == "mark 2"
    logger.close()
    assert logger.mark("after close") == "after close"  # log closed: not written, must not raise

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["sample_age_s"] for r in rows] == ["", "1.000", "5.500", "5.500", "5.500"]
    assert [r["note"] for r in rows] == ["", "", "", "stamp contact", "mark 2"]
    assert list(rows[0].keys())[-2:] == ["sample_age_s", "note"]