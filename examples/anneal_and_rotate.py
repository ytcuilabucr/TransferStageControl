"""Example: anneal a sample while stepping the rotator, using the scripting API.

    python examples/anneal_and_rotate.py              # simulators, virtual time (finishes in seconds)
    python examples/anneal_and_rotate.py --real-time  # run at wall-clock speed (needed for hardware)
    python examples/anneal_and_rotate.py --real-time --config my_config.yaml

Sequence: home both axes -> Z to the work height -> heat to the anneal temperature
and wait until it is stable -> rotate in steps, holding at each angle -> jog Z up
briefly -> heater off and passive cool-down below the safe temperature.
Leaving the ``with`` block (also on an error or Ctrl+C) runs ``safe_shutdown``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from transferstage.api import Readings, Stage
from transferstage.config import Config


def fmt(r: Readings) -> str:
    t = "—" if r.temperature_c is None else f"{r.temperature_c:6.1f}"
    return f"t={r.time_s / 60:6.1f} min  T={t} °C  SP={r.program_sp_c:6.1f}  V={r.v_cmd:5.2f}  Z={r.z_um:8.1f} µm  θ={r.rotator_deg:6.1f}°"


def run(
    config: Config | str | Path | None = None,
    *,
    virtual_time: bool = True,
    anneal_c: float = 150.0,
    rate_deg_min: float = 20.0,
    z_work_um: float = 5000.0,
    angles: tuple[float, ...] = (0.0, 30.0, 60.0, 90.0),
    hold_s: float = 120.0,
    safe_c: float = 50.0,
    out=print,
) -> list[Readings]:
    """Run the sequence; return one :class:`Readings` per stage of the recipe."""
    log: list[Readings] = []

    def note(msg: str, r: Readings) -> None:
        log.append(r)
        out(f"{msg:<22s} {fmt(r)}")

    with Stage(config, virtual_time=virtual_time) as stage:
        out(f"logging to {stage.log_path}")
        stage.home()
        stage.move_z(z_work_um)
        note("at work height", stage.readings())

        stage.psu_enabled(True)
        stage.set_temperature(anneal_c, rate_deg_min)
        r = stage.wait_for_temperature(tol_c=1.0, stable_s=60.0, timeout_s=3600.0)
        note("anneal temperature", r)

        for a in angles:
            stage.rotate(a)
            stage.sleep(hold_s)
            note(f"held at {a:g}°", stage.readings())

        stage.jog_z(+1, 2.0, speed_um_s=200.0)
        note("jogged up", stage.readings())

        stage.psu_enabled(False)
        r = stage.wait_for(lambda r: r.temperature_c is not None and r.temperature_c < safe_c, timeout_s=4 * 3600.0, poll_s=5.0)
        note("cooled", r)
    return log


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=None, help="config file (default: config.yaml)")
    p.add_argument("--real-time", action="store_true", help="wall-clock time (required with real hardware)")
    p.add_argument("--anneal", type=float, default=150.0, help="anneal temperature, °C")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    run(args.config, virtual_time=not args.real_time, anneal_c=args.anneal)
    return 0


if __name__ == "__main__":
    sys.exit(main())
