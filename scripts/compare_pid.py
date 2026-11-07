"""Compare temperature-control runs (Python app vs LabVIEW ThermalControl.vi).

Procedure and pass criteria: docs/pid_comparison.md.

Sub-commands
------------
``metrics CSV``
    Step-response metrics of one run: rise time, overshoot, settling time,
    steady-state error / noise, steady-state and peak voltage.

``compare PY_CSV LV_CSV``
    Metrics of both runs side by side, with the differences checked against the
    pass criteria. Exit code 0 = pass, 1 = fail.

``replay CSV --kc --ti --td``
    Feed the recorded temperature and set point of a run (normally the LabVIEW
    one) through our PID and compare its output with the recorded voltage,
    sample by sample. This removes plant/day-to-day differences and tests the
    controller algorithm alone.

Columns are chosen with ``--time/--temp/--sp/--volt`` (``--lv-*`` for the LabVIEW
file in ``compare``). The defaults are the Python session-CSV columns. The time
column may be seconds (any origin) or an ISO date-time.

Examples::

    python scripts/compare_pid.py metrics logs/20261106_101500.csv
    python scripts/compare_pid.py compare logs/20261106_101500.csv lv_run.csv ^
        --lv-time "Time (s)" --lv-temp "T" --lv-sp "SP" --lv-volt "V"
    python scripts/compare_pid.py replay lv_run.csv --time "Time (s)" --temp T --sp SP --volt V ^
        --kc 0.5 --ti 1.0 --td 0
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transferstage.control.pid import PID  # noqa: E402

PY_COLS = {"time": "elapsed_s", "temp": "temperature_c", "sp": "program_sp_c", "volt": "v_cmd"}

# Pass criteria (see docs/pid_comparison.md).
CRITERIA = {
    "overshoot_c": 1.0,  # |Δ| °C
    "rise_time_s": 0.20,  # relative
    "settling_time_s": 0.25,  # relative
    "ss_mean_error_c": 0.3,  # |Δ| °C
    "ss_std_c": 0.2,  # |Δ| °C
    "ss_mean_v": 0.5,  # |Δ| V
}


@dataclass
class Run:
    t: list[float]
    temp: list[float]
    sp: list[float]
    volt: list[float]


@dataclass
class Metrics:
    t_start_c: float
    sp_final_c: float
    rise_time_s: float  # 10 % -> 90 % of the step
    overshoot_c: float  # max T beyond the final SP, in the step direction (>= 0)
    settling_time_s: float  # from the SP change until T stays within ±band of SP
    ss_mean_error_c: float  # mean(T - SP) over the steady-state window
    ss_std_c: float
    ss_mean_v: float
    peak_v: float


def _parse_time(values: list[str]) -> list[float]:
    try:
        return [float(v) for v in values]
    except ValueError:
        pass
    try:
        ts = [datetime.fromisoformat(v.strip()) for v in values]
    except ValueError as exc:
        raise SystemExit(f"time column is neither seconds nor ISO date-time: {exc}") from exc
    return [(x - ts[0]).total_seconds() for x in ts]


def load_run(path: str | Path, time: str, temp: str, sp: str, volt: str, delimiter: str | None = None) -> Run:
    text = Path(path).read_text(encoding="utf-8-sig")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
    if not rows:
        raise SystemExit(f"{path}: no data rows")
    for col in (time, temp, sp, volt):
        if col not in rows[0]:
            raise SystemExit(f"{path}: no column {col!r}; columns are {list(rows[0])}")
    rows = [r for r in rows if all((r[c] or "").strip() for c in (time, temp, sp, volt))]
    t = _parse_time([r[time] for r in rows])
    t0 = t[0]
    return Run(
        t=[x - t0 for x in t],
        temp=[float(r[temp]) for r in rows],
        sp=[float(r[sp]) for r in rows],
        volt=[float(r[volt]) for r in rows],
    )


def compute_metrics(run: Run, band_c: float = 1.0, ss_window_s: float = 300.0) -> Metrics:
    """Metrics of the step from the first set-point change to the final set point.

    Pass the run trimmed to one step (ramp or step change followed by a hold).
    """
    n = len(run.t)
    if n < 3:
        raise ValueError("need at least 3 samples")
    sp_final = run.sp[-1]
    i0 = next((i for i in range(n) if abs(run.sp[i] - run.sp[0]) > 1e-9), 0)
    t_sp = run.t[i0]
    t_start = run.temp[i0]
    step = sp_final - t_start
    sign = 1.0 if step >= 0 else -1.0

    def first_cross(frac: float) -> float:
        level = t_start + frac * step
        for i in range(i0, n):
            if sign * (run.temp[i] - level) >= 0:
                return run.t[i]
        return math.nan

    rise = first_cross(0.9) - first_cross(0.1) if abs(step) > 0 else math.nan
    overshoot = max(0.0, max(sign * (x - sp_final) for x in run.temp[i0:]))

    settled_at = math.nan
    for i in range(n - 1, i0 - 1, -1):
        if abs(run.temp[i] - run.sp[i]) > band_c:
            settled_at = run.t[i + 1] - t_sp if i + 1 < n else math.nan
            break
    else:
        settled_at = 0.0

    t_end = run.t[-1]
    ss = [i for i in range(n) if run.t[i] >= t_end - ss_window_s]
    err = [run.temp[i] - run.sp[i] for i in ss]
    mean_err = sum(err) / len(err)
    std = math.sqrt(sum((e - mean_err) ** 2 for e in err) / len(err))
    return Metrics(
        t_start_c=t_start,
        sp_final_c=sp_final,
        rise_time_s=rise,
        overshoot_c=overshoot,
        settling_time_s=settled_at,
        ss_mean_error_c=mean_err,
        ss_std_c=std,
        ss_mean_v=sum(run.volt[i] for i in ss) / len(ss),
        peak_v=max(run.volt[i0:]),
    )


def check(py: Metrics, lv: Metrics) -> list[tuple[str, float, float, str, bool]]:
    """(name, python, labview, criterion text, passed) for each criterion."""
    out = []
    for name, tol in CRITERIA.items():
        a, b = getattr(py, name), getattr(lv, name)
        if name.endswith("_time_s"):
            ok = math.isfinite(a) and math.isfinite(b) and abs(a - b) <= tol * max(abs(b), 1e-9)
            text = f"within ±{tol:.0%}"
        else:
            ok = math.isfinite(a) and math.isfinite(b) and abs(a - b) <= tol
            text = f"|Δ| ≤ {tol:g}"
        out.append((name, a, b, text, ok))
    return out


def replay(run: Run, kc: float, ti_min: float, td_min: float, low: float = 0.0, high: float = 24.0) -> list[float]:
    """Our PID's output for the recorded T/SP sequence.

    The integrator starts so that the first output equals the first recorded
    voltage (bumpless start, as if the run began mid-control).
    """
    pid = PID(kc, ti_min, td_min, output_low=low, output_high=high)
    p0 = kc * (run.sp[0] - run.temp[0])
    pid.reset(integral=min(max(run.volt[0] - p0, low), high) if ti_min > 0 else 0.0)
    out = [run.volt[0]]
    for i in range(1, len(run.t)):
        out.append(pid.update(run.sp[i], run.temp[i], run.t[i] - run.t[i - 1]))
    return out


def _trim(run: Run, start: float | None, end: float | None) -> Run:
    keep = [i for i, t in enumerate(run.t) if (start is None or t >= start) and (end is None or t <= end)]
    if not keep:
        raise SystemExit("no samples left after --start/--end")
    t0 = run.t[keep[0]]
    return Run([run.t[i] - t0 for i in keep], [run.temp[i] for i in keep], [run.sp[i] for i in keep], [run.volt[i] for i in keep])


def _print_metrics(m: Metrics) -> None:
    for f in fields(m):
        print(f"  {f.name:18s} {getattr(m, f.name):10.3f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cols(p: argparse.ArgumentParser, prefix: str = "") -> None:
        for key, default in PY_COLS.items():
            p.add_argument(f"--{prefix}{key}", default=default, help=f"column name (default {default})")

    def window(p: argparse.ArgumentParser) -> None:
        p.add_argument("--start", type=float, help="ignore samples before this time (s from file start)")
        p.add_argument("--end", type=float, help="ignore samples after this time (s from file start)")
        p.add_argument("--band", type=float, default=1.0, help="settling band, °C (default 1)")
        p.add_argument("--ss-window", type=float, default=300.0, help="steady-state window at the end, s (default 300)")

    p = sub.add_parser("metrics", help="step-response metrics of one run")
    p.add_argument("csv")
    cols(p)
    window(p)

    p = sub.add_parser("compare", help="compare a Python run with a LabVIEW run")
    p.add_argument("py_csv")
    p.add_argument("lv_csv")
    cols(p)
    cols(p, "lv-")
    window(p)

    p = sub.add_parser("replay", help="replay recorded T/SP through our PID")
    p.add_argument("csv")
    cols(p)
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--kc", type=float, required=True)
    p.add_argument("--ti", type=float, required=True, help="minutes")
    p.add_argument("--td", type=float, default=0.0, help="minutes")
    p.add_argument("--low", type=float, default=0.0)
    p.add_argument("--high", type=float, default=24.0)
    p.add_argument("--tol", type=float, default=0.1, help="pass if RMS difference ≤ this, V (default 0.1)")

    a = ap.parse_args(argv)

    if a.cmd == "metrics":
        run = _trim(load_run(a.csv, a.time, a.temp, a.sp, a.volt), a.start, a.end)
        print(f"{a.csv}:")
        _print_metrics(compute_metrics(run, a.band, a.ss_window))
        return 0

    if a.cmd == "compare":
        py = compute_metrics(_trim(load_run(a.py_csv, a.time, a.temp, a.sp, a.volt), a.start, a.end), a.band, a.ss_window)
        lv_run = load_run(a.lv_csv, a.lv_time, a.lv_temp, a.lv_sp, a.lv_volt)
        lv = compute_metrics(_trim(lv_run, a.start, a.end), a.band, a.ss_window)
        print(f"{'metric':18s} {'Python':>10s} {'LabVIEW':>10s}  criterion")
        for f in fields(py):
            print(f"{f.name:18s} {getattr(py, f.name):10.3f} {getattr(lv, f.name):10.3f}")
        print()
        results = check(py, lv)
        for name, x, y, text, ok in results:
            print(f"{'PASS' if ok else 'FAIL'}  {name:18s} Δ = {x - y:+.3f}  ({text})")
        passed = all(r[-1] for r in results)
        print("\nOVERALL:", "PASS" if passed else "FAIL")
        return 0 if passed else 1

    run = _trim(load_run(a.csv, a.time, a.temp, a.sp, a.volt), a.start, a.end)
    ours = replay(run, a.kc, a.ti, a.td, a.low, a.high)
    diff = [o - v for o, v in zip(ours, run.volt)]
    rms = math.sqrt(sum(d * d for d in diff) / len(diff))
    worst = max(range(len(diff)), key=lambda i: abs(diff[i]))
    print(f"samples {len(diff)}, RMS difference {rms:.4f} V, max {diff[worst]:+.4f} V at t = {run.t[worst]:.1f} s")
    ok = rms <= a.tol
    print("PASS" if ok else "FAIL", f"(RMS ≤ {a.tol:g} V)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
