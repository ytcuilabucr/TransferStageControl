"""Interactive hardware smoke test (PLAN.md steps 3.1–3.5).

Runs each device check in turn and asks before every motion or output change.
Answer ``y`` to do the step, ``n`` to skip it, ``q`` to quit (everything is made
safe on exit: PSU output off, stages stopped and released).

The PSU is kept at <= 5 V with a 0.5 A current limit throughout.

    .venv\\Scripts\\python scripts\\smoke_test.py            # all devices
    .venv\\Scripts\\python scripts\\smoke_test.py psu dmm    # a subset: psu dmm list z rot
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transferstage.config import load_config  # noqa: E402
from transferstage.drivers.base import DeviceError, Direction  # noqa: E402

PSU_TEST_V = 5.0
PSU_TEST_I = 0.5
assert PSU_TEST_V <= 5.0

STEPS = ("psu", "dmm", "list", "z", "rot")


class Quit(Exception):
    pass


def ask(prompt: str) -> bool:
    while True:
        ans = input(f"  ? {prompt} [y/n/q] ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            print("    skipped")
            return False
        if ans == "q":
            raise Quit


def header(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def result(ok: bool, msg: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")


def check_psu(cfg) -> None:
    from transferstage.drivers.psu_1685b import Psu1685B

    header(f"3.1 PSU 1685B on {cfg.devices.psu_port}")
    psu = Psu1685B(cfg.devices.psu_port)  # connect() turns the output OFF
    try:
        psu.connect()
        print(f"  connected; GMAX {psu.v_max} V / {psu.i_max} A; output forced OFF")
        print(f"  set points: {psu.read_setpoints()}  readback: {psu.read_output()}")
        if ask(f"program {PSU_TEST_V} V / {PSU_TEST_I} A (output stays OFF)?"):
            psu.set_current(PSU_TEST_I)
            psu.set_voltage(PSU_TEST_V)
            v, i = psu.read_setpoints()
            result(abs(v - PSU_TEST_V) < 0.05 and abs(i - PSU_TEST_I) < 0.05, f"GETS -> {v} V, {i} A")
            if ask(f"turn the output ON at {PSU_TEST_V} V for 3 s?"):
                psu.output(True)
                try:
                    time.sleep(1.5)
                    r = psu.read_output()
                    print(f"  GETD: {r}")
                    result(abs(r.voltage - PSU_TEST_V) < 0.2 or r.mode.value == "CC",
                           "readback near set point (or CC if the load limits it)")
                    print("  -> compare with the front panel now")
                    time.sleep(1.5)
                finally:
                    psu.output(False)
                    print("  output OFF")
                time.sleep(0.5)
                print(f"  after OFF: {psu.read_output()}")
            psu.set_voltage(0.0)
    finally:
        psu.close()


def check_dmm(cfg) -> None:
    from transferstage.drivers.dmm import DmmThermometer

    header(f"3.2 DMM thermometer, VISA alias {cfg.devices.dmm_visa!r}")
    with DmmThermometer(cfg.devices.dmm_visa) as dmm:
        for _ in range(5):
            print(f"  {dmm.read_temperature():8.3f} °C")
            time.sleep(1.0)
    print("  -> compare with the DMM front panel")


def check_list(cfg) -> None:
    from transferstage.drivers.kinesis import list_devices

    header("3.3 Kinesis device list")
    serials = list_devices(cfg.devices.kinesis_path)
    print("  found:", ", ".join(serials) or "none")
    result(cfg.devices.z_serial in serials, f"Z stage {cfg.devices.z_serial}")
    result(cfg.devices.rotator_serial in serials, f"rotator {cfg.devices.rotator_serial}")


def check_z(cfg) -> None:
    from transferstage.drivers.kinesis import KinesisZStage, jog_distance

    header(f"3.4 Z stage {cfg.devices.z_serial}")
    z = KinesisZStage(cfg.devices.z_serial, cfg.devices.kinesis_path)
    try:
        z.connect()
        print(f"  pos {z.position_um:.2f} µm, state {z.state.value}, "
              f"v {z.velocity_um_s:.1f} / max {z.max_velocity_um_s:.1f} µm/s")
        start = z.position_um
        if ask("jog UP 100 µm and stop? (check clearance above the sample)"):
            end = jog_distance(z, 100.0)
            result(80 <= end - start <= 200, f"moved {end - start:+.1f} µm, state {z.state.value}")
            print(f"  -> confirm the stage moved UP (Direction.{Direction.UP.name} = Kinesis Forward)")
            if ask(f"move back to {start:.1f} µm?"):
                z.move_to(start, wait=True, timeout_s=60)
                result(abs(z.position_um - start) < 2, f"back at {z.position_um:.2f} µm")
        if ask("home Z? (moves to the home limit; check clearance)"):
            z.home(wait=True, timeout_s=180)
            result(True, f"homed, pos {z.position_um:.2f} µm")
    finally:
        z.close()


def check_rot(cfg) -> None:
    from transferstage.drivers.kinesis import KinesisRotator

    header(f"3.5 Rotator {cfg.devices.rotator_serial}")
    r = KinesisRotator(cfg.devices.rotator_serial, cfg.devices.kinesis_path)
    try:
        r.connect()
        print(f"  angle {r.angle_deg:.3f}°, state {r.state.value}, v {r.velocity_deg_s:.2f} °/s")
        if ask("home the rotator?"):
            r.home(wait=True, timeout_s=180)
            result(True, f"homed, angle {r.angle_deg:.3f}°")
        if ask("move to 10°?"):
            r.move_to(10.0, wait=True, timeout_s=60)
            result(abs(r.angle_deg - 10.0) < 0.1, f"angle {r.angle_deg:.3f}°")
            if ask("move back to 0°?"):
                r.move_to(0.0, wait=True, timeout_s=60)
                result(abs(r.angle_deg) < 0.1, f"angle {r.angle_deg:.3f}°")
    finally:
        r.close()


CHECKS = {"psu": check_psu, "dmm": check_dmm, "list": check_list, "z": check_z, "rot": check_rot}


def main(argv: list[str]) -> int:
    steps = argv or list(STEPS)
    unknown = [s for s in steps if s not in CHECKS]
    if unknown:
        print(f"unknown step(s) {unknown}; choose from {list(STEPS)}")
        return 2
    cfg = load_config()
    print("Hardware smoke test. Nothing moves or switches on without a 'y'.")
    failures = 0
    try:
        for step in steps:
            try:
                CHECKS[step](cfg)
            except (DeviceError, TimeoutError, ValueError, OSError) as exc:
                failures += 1
                print(f"  [FAIL] {step}: {exc}")
    except (Quit, KeyboardInterrupt):
        print("\nquit (devices released)")
        return 1
    print(f"\ndone, {failures} step(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
