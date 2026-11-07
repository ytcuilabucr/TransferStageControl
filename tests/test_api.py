"""Phase 6.2: headless scripting API (simulators; virtual time unless noted)."""

import csv
import importlib.util
from pathlib import Path

import pytest

from transferstage.api import SafetyTripError, Stage, StageError
from transferstage.config import config_from_dict
from transferstage.control.autotune import AutotuneParams
from transferstage.datalog import COLUMNS

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, **limits):
    return config_from_dict(
        {
            "limits": {"z_min_um": 0.0, "z_max_um": 20000.0, **limits},
            "logging": {"dir": str(tmp_path / "logs"), "interval_s": 1.0},
        }
    )


def _load_example():
    spec = importlib.util.spec_from_file_location("anneal_and_rotate", ROOT / "examples" / "anneal_and_rotate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_example_runs_end_to_end_in_virtual_time(tmp_path):
    example = _load_example()
    lines = []
    readings = example.run(_cfg(tmp_path), virtual_time=True, out=lines.append)
    at_height, annealed, *held, jogged, cooled = readings
    assert at_height.z_um == pytest.approx(5000.0)
    assert annealed.temperature_c == pytest.approx(150.0, abs=1.0)
    assert [r.rotator_deg for r in held] == pytest.approx([0.0, 30.0, 60.0, 90.0])
    assert all(abs(r.temperature_c - 150.0) < 1.5 and r.psu_on for r in held)
    assert jogged.z_um == pytest.approx(5400.0, abs=1.0)  # 2 s at 200 µm/s
    assert cooled.temperature_c < 50.0 and not cooled.psu_on
    assert cooled.time_s > 15 * 60  # simulated minutes ran in (much) less wall time

    logs = list((tmp_path / "logs").glob("*.csv"))
    assert len(logs) == 1 and str(logs[0]) in lines[0]
    with logs[0].open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == COLUMNS
    assert len(rows) > 15 * 60  # one row per simulated second
    assert max(float(r["temperature_c"]) for r in rows if r["temperature_c"]) < 155.0
    assert {r["mode"] for r in rows} == {"pid"}
    assert rows[-1]["psu_on"] == "0"


def test_stage_shuts_down_on_exit_and_on_exception(tmp_path):
    with pytest.raises(ZeroDivisionError):
        with Stage(_cfg(tmp_path), virtual_time=True, log=False) as stage:
            stage.psu_enabled(True)
            stage.set_temperature(80.0, 0.0)
            stage.sleep(30.0)
            assert stage.devices.psu.is_on and stage.readings().v_cmd > 0
            1 / 0
    d = stage.devices
    assert not d.psu.is_on and d.psu.voltage_setpoint == 0.0
    assert not any(dev.is_connected for _, dev in d.all())
    with pytest.raises(StageError, match="closed"):
        stage.readings()
    stage.close()  # idempotent


def test_stage_refusals_raise(tmp_path):
    with Stage(_cfg(tmp_path, t_max_c=200.0), virtual_time=True, log=False) as stage:
        with pytest.raises(StageError, match="soft limits"):
            stage.move_z(25000.0)
        with pytest.raises(StageError, match="limit"):
            stage.set_temperature(250.0)
        with pytest.raises(StageError, match="kc"):
            stage.set_pid(float("nan"), 1.0)
        stage.move_z(19990.0)
        stage.jog_z(+1, 60.0)  # stops at the 20000 µm soft limit
        assert stage.z_um == pytest.approx(20000.0)
        with pytest.raises(StageError, match="soft limit"):
            stage.jog_z(+1, 1.0)
        assert stage.move_z(-500.0, relative=True) == pytest.approx(19500.0)
        assert stage.rotate(45.0) == pytest.approx(45.0)
        assert stage.rotate(-15.0, relative=True) == pytest.approx(30.0)


def test_safety_trip_raises_and_psu_stays_off(tmp_path):
    with Stage(_cfg(tmp_path), virtual_time=True, log=False) as stage:
        stage.psu_enabled(True)
        stage.set_temperature(100.0, 0.0)
        stage.sleep(60.0)
        stage.devices.thermometer.fault = OSError("DMM unplugged")
        with pytest.raises(SafetyTripError, match="no temperature reading"):
            stage.wait_for_temperature(tol_c=0.2, stable_s=600.0, timeout_s=3600.0)
        assert stage.readings().time_s < 60.0 + 10.0  # stale limit is 5 s
        assert not stage.devices.psu.is_on
        stage.devices.thermometer.fault = None
        with pytest.raises(SafetyTripError):
            stage.psu_enabled(True)  # still latched


def test_wait_for_temperature_timeout(tmp_path):
    with Stage(_cfg(tmp_path), virtual_time=True, log=False) as stage:
        stage.set_temperature(100.0, 0.0)  # PSU off: never gets there
        with pytest.raises(TimeoutError):
            stage.wait_for_temperature(timeout_s=120.0)
        assert stage.readings().time_s == pytest.approx(120.0, abs=2.0)


def test_api_autotune(tmp_path):
    with Stage(_cfg(tmp_path), virtual_time=True, log=False) as stage:
        stage.psu_enabled(True)
        stage.set_temperature(80.0, 0.0)
        stage.wait_for_temperature(tol_c=0.5, stable_s=60.0, timeout_s=3600.0)
        result = stage.autotune(AutotuneParams(80.0, amplitude_v=3.0, hysteresis_c=0.3))
        assert result.ku > 0 and result.pu_s > 0
        g = result.gains["Tyreus–Luyben PI"]
        stage.set_pid(g.kc, g.ti_min, g.td_min)
        r = stage.readings()
        assert r.mode == "pid" and r.target_c == 80.0


def test_virtual_time_needs_simulators(tmp_path):
    cfg = config_from_dict({"devices": {"simulate": {"psu": False}}})
    with pytest.raises(StageError, match="simulated"):
        Stage(cfg, virtual_time=True)


def test_real_time_mode_runs_loop_in_background(tmp_path):
    cfg = config_from_dict({"pid": {"period_ms": 50}, "logging": {"dir": str(tmp_path / "logs"), "interval_s": 0.1}})
    with Stage(cfg) as stage:
        t0 = stage.readings().time_s
        stage.psu_enabled(True)
        stage.set_temperature(60.0, 0.0)
        stage.sleep(0.5)
        r = stage.readings()
        assert r.time_s - t0 >= 0.5 and r.psu_on and r.v_cmd > 0
        path = stage.log_path
    assert not stage._thread.is_alive()
    with path.open(newline="", encoding="utf-8") as fh:
        assert len(list(csv.reader(fh))) >= 4
