import csv
import importlib.util
import sys
from pathlib import Path

import pytest

from transferstage.control.pid import PID

_spec = importlib.util.spec_from_file_location(
    "compare_pid", Path(__file__).resolve().parent.parent / "scripts" / "compare_pid.py"
)
compare_pid = importlib.util.module_from_spec(_spec)
sys.modules["compare_pid"] = compare_pid
_spec.loader.exec_module(compare_pid)


def _simulate(path: Path, kc=0.8, ti=0.5, td=0.0, sp=80.0, dt=0.5, n=4000, header=None):
    """Closed loop of our PID on a first-order plant; write a session-style CSV."""
    pid = PID(kc, ti, td, output_low=0.0, output_high=24.0)
    T, k, tau, amb = 22.0, 0.25, 60.0, 22.0
    header = header or ["elapsed_s", "temperature_c", "program_sp_c", "v_cmd"]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        v = 0.0
        for i in range(n):
            s = 22.0 if i < 10 else sp
            v = pid.update(s, T, dt) if i else 0.0
            w.writerow([f"{i * dt:.3f}", f"{T:.4f}", f"{s:.3f}", f"{v:.5f}"])
            T += dt * (k * v * v - (T - amb)) / tau


def test_metrics_and_self_compare(tmp_path):
    f = tmp_path / "run.csv"
    _simulate(f)
    m = compare_pid.compute_metrics(compare_pid.load_run(f, **compare_pid.PY_COLS))
    assert m.sp_final_c == pytest.approx(80.0)
    assert m.rise_time_s > 0
    assert abs(m.ss_mean_error_c) < 0.2
    assert compare_pid.main(["compare", str(f), str(f)]) == 0


def test_compare_detects_difference(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _simulate(a, kc=0.8, ti=0.5)
    _simulate(b, kc=3.0, ti=0.1)  # much more aggressive -> overshoot differs
    assert compare_pid.main(["compare", str(a), str(b)]) == 1


def test_replay_reproduces_own_output(tmp_path):
    f = tmp_path / "lv.csv"
    _simulate(f, kc=0.8, ti=0.5, td=0.05, header=["Time", "T", "SP", "V"])
    args = ["replay", str(f), "--time", "Time", "--temp", "T", "--sp", "SP", "--volt", "V"]
    assert compare_pid.main(args + ["--kc", "0.8", "--ti", "0.5", "--td", "0.05", "--start", "1"]) == 0
    assert compare_pid.main(args + ["--kc", "2.0", "--ti", "0.5", "--td", "0.05", "--start", "1"]) == 1
