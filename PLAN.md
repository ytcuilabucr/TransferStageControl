# TransferStageControl — Python Port Plan

Port of the LabVIEW 2020 project in `reference/TransferStageControl LV2020/` to Python, with feature parity plus extras.

## Decisions

| Topic | Decision |
|---|---|
| GUI | PyQt5 + pyqtgraph |
| Thorlabs | pythonnet + **64-bit Kinesis** (`C:\Program Files\Thorlabs\Kinesis`); the DLLs in `reference/.../Libs` are 32-bit and unusable from 64-bit Python |
| PSU | B&K 1685B via pyserial, COM4 |
| Thermometer | DMM via PyVISA, alias `DMM`, `MEAS:TEMP? RTD` |
| TourBox | Configured to send keyboard shortcuts → handled as Qt shortcuts, remappable in config |
| Kinesis WPF panels | Optional, last phase |
| Safety limits | **Max temperature 300 °C**, **max PSU voltage 24 V** (hard limits, not user-overridable from GUI) |
| Hardware IDs | Z stage KCube DC Servo `27270114`; rotator Integrated Stepper `55524924` |

Each step lists **Done when** — the check that closes it.

---

## Phase 0 — Environment

**0.1 Install 64-bit Kinesis** (user action) from thorlabs.com. Verify with Kinesis app that both devices appear.
Done when: `C:\Program Files\Thorlabs\Kinesis\Thorlabs.MotionControl.DeviceManagerCLI.dll` exists and both serials show in Kinesis.

**0.2 Project skeleton**
- `pyproject.toml` (package `transferstage`, Python ≥3.12), deps: `PyQt5`, `pyqtgraph`, `pyserial`, `pyvisa`, `pythonnet`, `pyyaml`, `numpy`; dev deps: `pytest`, `pytest-qt`.
- Virtual env `.venv`, `git init`, `.gitignore` (exclude `.venv`, `logs/`, `reference/Libs` binaries optional).
- `README.md` with setup/run instructions.
Done when: `pip install -e .[dev]` succeeds and `pytest` runs (0 tests).

**0.3 Config system** — `transferstage/config.py` + `config.yaml`
- Sections: `devices` (serials, COM port, VISA alias, `simulate: true/false` per device), `pid` (Kc, Ti, Td, period_ms, output_low/high), `ramp` (default deg/min), `limits` (T_max 300, V_max 24, I_max, Z soft min/max, stale_temp_s), `keys` (action → shortcut), `logging` (dir, interval).
- Load with dataclasses + validation; clamp limits so config cannot exceed 300 °C / 24 V.
Done when: unit test loads default config and rejects `T_max: 400`.

**0.4 App logging** — Python `logging` to console + rotating file `logs/app.log`.

---

## Phase 1 — Driver interfaces and simulators (no hardware)

**1.1 Interfaces** — `drivers/base.py` (ABCs)
- `LinearStage`: `connect()`, `close()`, `position_um`, `state` (enum: Idle, Moving, Homing, Initializing, Stopping, Error), `max_velocity_um_s`, `move_to(um, wait=False)`, `jog(direction, velocity)`, `stop(immediate=True)`, `home()`.
- `RotaryStage`: same with degrees.
- `PowerSupply`: `connect/close`, `set_voltage(V)`, `set_current(A)`, `output(on: bool)`, `read_output() -> (V, A, mode)`.
- `Thermometer`: `connect/close`, `read_temperature() -> float`.
Done when: interfaces importable and documented.

**1.2 Simulators** — `drivers/sim.py`
- `SimStage` / `SimRotator`: integrates position from velocity on a timer, honours stop, reports states.
- `SimThermalPlant`: first-order + delay model, `T' = (K·V² − (T − T_amb)) / τ`, coupled to `SimPowerSupply`; `SimThermometer` reads it with noise.
Done when: a test sets PSU 12 V and sees simulated T rise monotonically toward steady state.

**1.3 Driver factory** — `drivers/__init__.py: build_devices(config)` returns real or sim per `simulate` flag.

---

## Phase 2 — Control logic (pure Python, fully unit-tested)

**2.1 PID** — `control/pid.py`
- NI-compatible parameterization: Kc, Ti (min), Td (min); `update(setpoint, pv, dt) -> output`.
- Output clamp `[output_low, output_high]` (high ≤ 24 V), integrator anti-windup, derivative on measurement, `reset()` (= LabVIEW "reinitialize?").
Done when: tests for P-only, integral accumulation, clamping/anti-windup, reset; closed-loop test on `SimThermalPlant` settles within ±1 °C.

**2.2 Setpoint ramp** — `control/ramp.py`
- State: original SP, target SP, rate (deg/min), direction (ascending flag), `program_sp(t)`; `is_ramping`.
- Rate 0 → step change. Target clamped to ≤ 300 °C.
Done when: tests for up-ramp, down-ramp, retarget mid-ramp, completion flag.

**2.3 Temperature profiles (extra)** — `control/profile.py`
- List of segments `{ramp_to, rate, hold_min}`; load/save YAML.
Done when: test runs a 3-segment profile in simulated time.

**2.4 Safety rules** — `control/safety.py`
- Checks: T ≥ 300 °C → PSU off + latch alarm; temperature reading stale > `stale_temp_s` → PSU off; PSU readback voltage deviates from command → warning; command voltage > 24 V → clamp; Z outside soft limits → refuse move.
- `safe_shutdown(devices)`: stop motors, output off, set 0 V, close all — each step isolated with try/except.
Done when: tests cover every rule with sim devices.

---

## Phase 3 — Hardware drivers

**3.1 PSU 1685B** — `drivers/psu_1685b.py`
- Serial 9600 8N1, `\r` terminator; commands `SOUT0` (on) / `SOUT1` (off), `VOLTxxx`, `CURRxxx`, `GETD` (readback), `GETS` (setpoints); parse `OK` acks.
- Verify against the 1685B manual: digit scaling for VOLT/CURR (expected V×10, A×100 — confirm), and that SOUT0 = on.
- CLI: `python -m transferstage.drivers.psu_1685b --port COM4 --volt 5 --on`.
Done when: CLI sets 5.0 V on hardware and `GETD` reads it back.

**3.2 DMM thermometer** — `drivers/dmm.py`
- PyVISA open alias `DMM`, query `MEAS:TEMP? RTD`, parse float, timeout handling.
- CLI prints temperature once per second.
Done when: reading matches the DMM front panel.

**3.3 Kinesis loader** — `drivers/kinesis.py`
- `clr.AddReference` on DeviceManagerCLI, GenericMotorCLI, KCube.DCServoCLI, IntegratedStepperMotorsCLI from the 64-bit install path (configurable).
- `DeviceManagerCLI.BuildDeviceList()`; list serials.
Done when: CLI `--list` prints both serials.

**3.4 Z stage (KCube DC Servo)**
- `KCubeDCServo.CreateKCubeDCServo(serial)`, `Connect`, `WaitForSettingsInitialized`, `StartPolling(250)`, `EnableDevice`, `LoadMotorConfiguration`.
- Map: `MoveTo(Decimal, timeout)`, `MoveContinuousAtVelocity(MotorDirection, ...)`/`SetVelocityParams` + `MoveContinuous`, `StopImmediate`, `Position`, `Status`, `GetVelocityParams().MaxVelocity`, `Home`.
- Unit conversion mm ↔ µm (Kinesis uses real units mm).
Done when: CLI jogs up 100 µm, stops, reports position and state.

**3.5 Rotator (Integrated Stepper)** — same pattern with `CageRotator`/`IntegratedStepperMotor`; `MoveTo(angle)`, `Home`.
Done when: CLI moves to 10°, back to 0°.

**3.6 Hardware smoke test script** — `scripts/smoke_test.py` runs 3.1–3.5 in sequence with confirmations.

---

## Phase 4 — Worker threads

**4.1 Motion worker** — `control/motion.py` (QThread / QObject)
- Owns both stages; command queue (move_to, jog, stop, home, set_speed); polls at ~10 Hz and emits `status(pos, state, speed, since_last_motion)`.
- "Safe lock" flag blocks all motion commands except stop.
Done when: pytest-qt test with sim stages: jog → stop → status signals correct.

**4.2 Thermal worker** — `control/thermal.py`
- Loop each `period_ms`: read T → ramp/profile SP → PID → clamp → PSU set_voltage → safety checks → emit `sample(t, T, SP, V, I)`.
- Commands: set target + rate, enable/disable PSU, update gains, reset PID, start/stop profile.
Done when: sim closed loop reaches 100 °C via 10 °C/min ramp in accelerated sim time.

**4.3 Data logger** — `logging/datalog.py`: CSV per session in `logs/YYYYMMDD_HHMMSS.csv` (time, T, SP, program SP, V, I, Z, angle, states, alarms).

---

## Phase 5 — GUI (feature parity with gui_v2)

**5.1 Main window shell** — `ui/main_window.py`: tabs **Manual**, **Temperature**, **Settings**, **Log**; status bar (connection state per device, alarm indicator); device connect on start (= LabVIEW "Initialization").

**5.2 Manual (TourBox) tab**
- Z: Up/Down buttons (press-and-hold jog), speed knob (QDial, range 0..MaxVelocity µm/s), Set Speed, Cancel Motion, Safe Lock toggle, Up/Down/Busy indicators, Z position slide + numeric, Z-status text, "since last motion".
- Rotator: Set Angle + button, Current Angle.
- Presets list (extra): save/go-to named Z/angle positions; Home buttons.
Done when: all controls work against sim devices.

**5.3 Temperature tab**
- Setpoint, ramp rate (deg/min), Set button, Ramping indicator, PSU On/Off, PID gains (Kc/Ti/Td), PID reset, current T / SP / program SP / V readouts.
- pyqtgraph charts: temperature (T, SP, program SP) and PSU voltage vs time, with rolling window + pause/zoom.
- Profile editor/runner (extra).
Done when: sim closed loop displayed live; Set triggers ramp.

**5.4 Settings tab** — serials, COM port, VISA alias, PID period, limits (display; max capped at 300 °C/24 V), key bindings, simulate toggles; Save writes `config.yaml`.

**5.5 Log tab** — live app log + alarm history; button to open data-log folder.

**5.6 Keyboard / TourBox shortcuts** — `ui/input_map.py`
- Default actions: `safe_lock` (Side+Top), `cancel_motion` (Top), `speed_up`/`speed_down` (Knob), `jog_up` (Tall, press/release = start/stop), `jog_down` (Short).
- Keys configured to match the TourBox Console mapping; application-wide shortcuts; jog uses key press/release events with auto-repeat ignored.
Done when: TourBox buttons drive the GUI with the window focused.

**5.7 Shutdown** — `closeEvent` and unhandled-exception hook call `safe_shutdown`; confirmation dialog if PSU is on.

---

## Phase 6 — Extras

**6.1 PID autotune** — relay (Åström–Hägglund) test in `control/autotune.py`: oscillate PSU between two voltages around SP, measure Ku/Pu, compute PID gains (Ziegler–Nichols / Tyreus–Luyben); GUI button with progress and "apply gains". Respects 300 °C / 24 V limits.
Done when: autotune on sim plant yields gains that settle without large overshoot.

**6.2 Scripting API** — `transferstage/api.py`: `Stage` context manager exposing `move_z`, `rotate`, `set_temperature(target, rate)`, `wait_for_temperature(tol, stable_s)`, `log`. Example script in `examples/`.

**6.3 Optional: embedded Kinesis panels** — host `KCubeDCServoControl` / `CageRotatorControl` WPF controls via a separate window (WPF interop through pythonnet), behind a config flag.

---

## Phase 7 — Validation and packaging

**7.1 Hardware acceptance test** (checklist in `docs/acceptance.md`): every gui_v2 feature exercised on real hardware; over-temperature trip tested by temporarily lowering the limit; DMM unplug → PSU off within `stale_temp_s`; app kill → (document that PSU stays at last setting; advise hardware thermal fuse).
**7.2 Compare PID behaviour** against LabVIEW ThermalControl with the same gains.
**7.3 Packaging** — `run.bat` launcher (activates venv, starts app); optional PyInstaller build.
**7.4 Docs** — README: install, config, key mapping, safety notes.

---

## Order of work

0 → 1 → 2 can be done fully without hardware. 3 needs the instruments connected (3.3–3.5 need Kinesis 64-bit). 4–5 develop against simulators, then switch `simulate: false`. 6 and 7 last.
