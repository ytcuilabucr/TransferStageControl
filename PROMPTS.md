# Phase Prompts — TransferStageControl Python Port

One prompt per phase of [PLAN.md](PLAN.md). Paste a prompt into a new Claude Code session opened in this folder. Run them in order; each assumes the previous phases are done and committed.

Every prompt shares these rules (they are repeated inside each prompt so each stands alone):
- Read `PLAN.md` first; it is the source of truth. Do only the named phase.
- Safety limits are hard: **300 °C max temperature, 24 V max PSU voltage**.
- Finish with `pytest` passing, and commit with a message `Phase N: ...`.

---

## Phase 0 — Environment and skeleton

```
Read PLAN.md and implement Phase 0 (steps 0.2–0.4) only. Step 0.1 (installing 64-bit Thorlabs Kinesis) is my job; just check whether C:\Program Files\Thorlabs\Kinesis exists and tell me.

Context: this is a Python port of the LabVIEW project in reference/TransferStageControl LV2020/ (Thorlabs Z stage + rotator, B&K 1685B PSU, DMM RTD thermometer, PID heater control). Python 3.12 64-bit on Windows.

Do:
- pyproject.toml for package `transferstage` (src layout not required), deps: PyQt5, pyqtgraph, pyserial, pyvisa, pythonnet, pyyaml, numpy; dev extras: pytest, pytest-qt. Create .venv, install with `pip install -e .[dev]`.
- git init, .gitignore (.venv, logs/, __pycache__, *.egg-info). Do not commit the reference/ folder's binaries (DLLs, .exe, .chm, drivers) — gitignore reference/**/Libs/.
- transferstage/config.py: dataclass-based config loaded from config.yaml with sections devices (z_serial "27270114", rotator_serial "55524924", psu_port "COM4", dmm_visa "DMM", kinesis_path, per-device simulate flags, default true), pid (kc, ti_min, td_min, period_ms, output_low, output_high), ramp (default_rate_deg_min), limits (t_max_c 300, v_max 24, i_max, z_min_um, z_max_um, stale_temp_s), keys (action -> shortcut string), logging (dir, interval_s). Validation must reject or clamp anything above 300 °C / 24 V; output_high must be <= v_max.
- A default config.yaml with sensible values.
- transferstage/applog.py: logging setup to console + rotating file logs/app.log.
- README.md with setup and run instructions.
- tests/test_config.py: loads defaults; rejects t_max_c: 400 and v_max: 30; output_high > v_max rejected.

Done when pytest passes. Commit as "Phase 0: project skeleton and config".
```

---

## Phase 1 — Driver interfaces and simulators

```
Read PLAN.md and implement Phase 1 (steps 1.1–1.3) only. Phase 0 is done (package, config, logging exist).

Do:
- transferstage/drivers/base.py: abstract base classes
  - LinearStage: connect(), close(), position_um, state (enum MotorState: IDLE, MOVING, HOMING, INITIALIZING, STOPPING, ERROR, DISCONNECTED), max_velocity_um_s, velocity_um_s, move_to(um, wait=False), jog(direction: Direction.UP/DOWN, velocity_um_s), stop(immediate=True), home().
  - RotaryStage: same shape in degrees (angle_deg, move_to(deg)).
  - PowerSupply: connect(), close(), set_voltage(v), set_current(a), output(on: bool), read_output() -> PsuReading(voltage, current, mode), is_on.
  - Thermometer: connect(), close(), read_temperature() -> float (°C).
  Methods must be thread-safe (internal lock) since workers will call them from threads.
- transferstage/drivers/sim.py:
  - SimLinearStage / SimRotaryStage: position integrates velocity using time.monotonic(); move_to ramps at max velocity; states change realistically; stop works mid-move.
  - SimPowerSupply: stores setpoints, clamps to its own 0–60 V / 0–5 A range.
  - SimThermalPlant: first-order model T' = (K*V^2 - (T - T_amb)) / tau with T_amb 22 °C, tunable K and tau, optional time_scale for accelerated tests; advances on each read based on elapsed time.
  - SimThermometer: reads the plant with small Gaussian noise.
- transferstage/drivers/__init__.py: build_devices(config) -> Devices dataclass (z, rotator, psu, thermometer) using sim classes when the per-device simulate flag is true; for non-sim devices import the real driver modules lazily (they will be created in Phase 3; raise a clear NotImplementedError for now).
- Tests: stage move_to reaches target and returns to IDLE; stop mid-move; jog + stop; PSU 12 V makes plant temperature rise monotonically toward steady state (use time_scale so tests run < 2 s).

Done when pytest passes. Commit as "Phase 1: driver interfaces and simulators".
```

---

## Phase 2 — Control logic

```
Read PLAN.md and implement Phase 2 (steps 2.1–2.4) only. Phases 0–1 are done; use the simulators in transferstage/drivers/sim.py for tests.

Hard safety limits: 300 °C max temperature, 24 V max PSU voltage — enforce in code, not just config.

Do:
- transferstage/control/pid.py: PID matching NI LabVIEW PID.vi parameterization: Kc (gain), Ti (integral time, minutes), Td (derivative time, minutes). update(setpoint, pv, dt_s) -> output. Output clamped to [output_low, output_high] with output_high <= 24. Anti-windup (conditional integration or back-calculation). Derivative on measurement. reset() equals LabVIEW "reinitialize?". Ti = 0 disables integral.
- transferstage/control/ramp.py: SetpointRamp with set_target(target, rate_deg_min, current_value) storing original SP, target, direction (ascending flag), and program_sp(now) moving linearly toward target; is_ramping property; rate 0 = immediate step; target clamped to <= 300.
- transferstage/control/profile.py: TemperatureProfile of segments {ramp_to, rate_deg_min, hold_min}; load/save YAML; a runner that yields the program setpoint over time and reports current segment.
- transferstage/control/safety.py:
  - SafetyMonitor.check(sample) returning a list of Alarm objects with severity: over-temp (T >= limit) -> trip (PSU off, latched until acknowledged); stale temperature (no fresh reading for stale_temp_s) -> trip; PSU readback deviates from command by > tolerance -> warning; clamp_voltage(v) -> min(v, 24, config v_max); z_move_allowed(target_um) against soft limits.
  - safe_shutdown(devices): stop both stages, PSU set 0 V and output off, close all; each step in its own try/except with logging so one failure doesn't block the rest.
- Tests: PID P-only, integral accumulation, clamping and anti-windup, reset; closed loop on SimThermalPlant (accelerated) settles at 100 °C within ±1 °C; ramp up, down, retarget mid-ramp, completion; 3-segment profile; every safety rule; safe_shutdown continues when one device raises.

Done when pytest passes. Commit as "Phase 2: PID, ramp, profiles, safety".
```

---

## Phase 3 — Hardware drivers

```
Read PLAN.md and implement Phase 3 (steps 3.1–3.6) only. Phases 0–2 are done. Real drivers must implement the ABCs in transferstage/drivers/base.py and plug into build_devices().

Hardware facts recovered from the LabVIEW VIs:
- PSU: B&K Precision 1685B on COM4. LabVIEW sends "SOUT0" to enable output and "SOUT1" to disable, "VOLT%03d" and "CURR%03d" for setpoints. Look up the 1685B programming manual to confirm serial settings (expected 9600 8N1, CR terminator, "OK" acknowledgements), the digit scaling of VOLT/CURR (expected volts x10, amps x100 — verify, do not guess), and readback commands GETD / GETS. Put the manual reference in the module docstring.
- Thermometer: VISA alias "DMM", query "MEAS:TEMP? RTD", reply is a number in °C.
- Thorlabs via pythonnet and the 64-bit Kinesis install (config kinesis_path, default C:\Program Files\Thorlabs\Kinesis). Do NOT use the DLLs in reference/ — they are 32-bit.
  - Z stage: KCube DC Servo, serial 27270114 (KCubeDCServo in ThorLabs.MotionControl.KCube.DCServoCLI). LabVIEW used CreateDevice, GetVelocityParams().MaxVelocity, MoveTo(position, waitTimeout), MoveContinuousAtVelocity(MotorDirection, velocity), StopImmediate, Position, Status/State, CloseDevice. GUI units are µm and µm/s; Kinesis uses mm.
  - Rotator: Integrated Stepper (cage rotator), serial 55524924 (Thorlabs.MotionControl.IntegratedStepperMotorsCLI). Degrees.
  Use the standard sequence: DeviceManagerCLI.BuildDeviceList, create, Connect, WaitForSettingsInitialized, StartPolling(250), EnableDevice, LoadMotorConfiguration; reverse on close.

Do:
- transferstage/drivers/psu_1685b.py, dmm.py, kinesis.py (loader + KinesisZStage + KinesisRotator).
- Each module gets a `python -m` CLI for manual testing (psu: --port --volt --curr --on/--off --read; dmm: --alias --watch; kinesis: --list, --z-jog, --z-move, --rot-move, --home).
- Unit tests that don't need hardware: PSU command formatting/parsing using a fake serial object; DMM parsing with a fake resource; kinesis unit conversions. Mark hardware tests with @pytest.mark.hardware and skip them by default.
- scripts/smoke_test.py: interactive step-by-step hardware check with confirmations before every motion or output change. Keep PSU voltage <= 5 V during smoke tests.

Never move hardware or energize the PSU yourself; tell me the exact CLI commands to run and wait for my results. Done when unit tests pass and the CLIs are ready. Commit as "Phase 3: hardware drivers".
```

---

## Phase 4 — Worker threads and data logging

```
Read PLAN.md and implement Phase 4 (steps 4.1–4.3) only. Phases 0–3 are done. Develop and test against the simulators.

Do:
- transferstage/control/motion.py: MotionWorker (QObject moved to a QThread). Owns z and rotator. Slots: jog_start(direction), jog_stop(), move_z_to(um), set_speed(um_s), cancel_motion(), rotate_to(deg), home_z(), home_rotator(), set_safe_lock(bool). Safe lock blocks every command except cancel/stop. Polls at ~10 Hz and emits status(MotionStatus: z_um, z_state, speed, rotator_deg, rotator_state, seconds_since_last_motion, safe_lock). Respects Z soft limits via SafetyMonitor. Device exceptions become error signals, never crash the thread.
- transferstage/control/thermal.py: ThermalWorker (QThread). Loop every pid.period_ms: read T -> ramp/profile program SP -> PID -> SafetyMonitor.clamp_voltage -> psu.set_voltage -> safety checks (trip turns PSU off and emits alarm) -> emit sample(ThermalSample: t, T, target SP, program SP, V cmd, V/I readback, ramping, psu_on, alarms). Slots: set_target(sp, rate), set_psu_enabled(bool), set_gains(kc, ti, td), reset_pid(), start_profile(profile), stop_profile(), acknowledge_alarm(). PID output only applied while PSU enabled; when disabled, PID is reset.
- transferstage/datalog.py: DataLogger writing logs/YYYYMMDD_HHMMSS.csv with header; receives thermal and motion signals, writes rows at logging.interval_s; flushes regularly; closes cleanly.
- Tests with pytest-qt: motion jog/stop/safe-lock; thermal loop reaches a 100 °C target with a 10 °C/min ramp in accelerated sim time; over-temp trip turns PSU off; logger produces a CSV with expected columns.

Done when pytest passes. Commit as "Phase 4: motion/thermal workers and data logging".
```

---

## Phase 5 — GUI

```
Read PLAN.md and implement Phase 5 (steps 5.1–5.7) only. Phases 0–4 are done (workers in transferstage/control/). Build with PyQt5 + pyqtgraph and run against the simulators.

The GUI must reach feature parity with the LabVIEW gui_v2.vi, whose front panel had: Z Up/Down buttons, Speed Knob, Set Speed, Cancel Motion, Safe Lock, Up/Down/Busy indicators, Z-Position slide, Z-status (Idle/Moving/Homing/Initializing/Stopping/Terminated), "Since Last Motion", Set Angle / Current Angle for the rotator, temperature setpoint + Ramp rate (deg/min) + Set button + Ramping indicator, PSU Off/On, PID gains (Kc, Ti, Td), PID reinitialize, temperature and voltage charts, and a Settings tab with Motor SN#, Rotator SN#, PSU/DMM ports, PID period.

Do:
- transferstage/app.py entry point (`python -m transferstage`), builds devices from config, starts workers, shows MainWindow. Add a [project.scripts] entry.
- ui/main_window.py: tabs Manual, Temperature, Settings, Log; status bar with per-device connection state and an alarm indicator.
- ui/manual_tab.py: everything listed for motion above plus position presets (save/go-to named Z+angle, stored in config) and Home buttons. Up/Down are press-and-hold jogs.
- ui/temperature_tab.py: everything listed for temperature above, readouts for T / target / program SP / V / I, pyqtgraph charts (T, target, program SP on one; PSU voltage on another) with rolling window and pause; simple profile editor/runner; alarm banner with Acknowledge.
- ui/settings_tab.py: edit and save config.yaml; limits fields shown but cannot exceed 300 °C / 24 V; key bindings editor; simulate toggles (take effect on restart).
- ui/log_tab.py: live app log and alarm history; button to open the data-log folder.
- ui/input_map.py: application-wide shortcuts from config. The TourBox is configured to send keystrokes. Actions: safe_lock (Side+Top), cancel_motion (Top), speed_up / speed_down (Knob), jog_up (Tall), jog_down (Short). Jogs start on key press, stop on key release, ignore auto-repeat. Pick defaults (e.g. Ctrl+L, Esc, ], [, PageUp, PageDown) and document them so I can map the TourBox Console to them.
- closeEvent and sys.excepthook call safe_shutdown; confirm before closing if the PSU is on.
- pytest-qt smoke tests: window opens with sim devices, Set temperature starts ramping, jog key press/release moves and stops Z, safe lock blocks jog.

Run the app with simulators and take a screenshot to check the layout. Done when tests pass and the app runs. Commit as "Phase 5: GUI".
```

---

## Phase 6 — Extras

```
Read PLAN.md and implement Phase 6 only: 6.1 PID autotune and 6.2 scripting API. Skip 6.3 (embedded Kinesis WPF panels) unless I ask. Phases 0–5 are done.

Hard safety limits stay enforced: 300 °C, 24 V, plus SafetyMonitor trips during autotune.

Do:
- transferstage/control/autotune.py: relay (Åström–Hägglund) autotune. Around the current setpoint, switch PSU voltage between bias ± amplitude (user-set, clamped to 0–24 V) with hysteresis; record oscillation for N cycles; compute Ku and Pu; offer gains by Ziegler–Nichols and Tyreus–Luyben (Kc, Ti and Td in minutes, matching our PID). Abort on safety trip, user cancel, or timeout.
- Integrate into ThermalWorker (mode: autotune) and the Temperature tab: Autotune button, parameters, progress, results table, "Apply gains".
- transferstage/api.py: headless Stage context manager using build_devices + safety: move_z(um), jog for duration, rotate(deg), home(), set_temperature(target, rate_deg_min), wait_for_temperature(tol_c, stable_s, timeout_s), psu_enabled(bool), current readings, CSV logging, safe_shutdown on exit/exception.
- examples/anneal_and_rotate.py using the API against simulators.
- Tests: autotune on SimThermalPlant produces gains that settle a 50 °C step with < 10% overshoot; API example runs end-to-end in sim with accelerated time.

Done when pytest passes. Commit as "Phase 6: autotune and scripting API".
```

---

## Phase 7 — Validation and packaging

```
Read PLAN.md and implement Phase 7 (steps 7.1–7.4). Phases 0–6 are done.

Do:
- docs/acceptance.md: a hardware acceptance checklist covering every gui_v2 feature (Z jog/speed/cancel/safe lock, rotator angle, temperature ramp up and down, PSU on/off, PID gains, charts) plus safety tests: over-temperature trip (temporarily set a low limit such as 40 °C in config, not by heating to 300 °C), DMM unplugged -> PSU off within stale_temp_s, Z soft limits, app close with PSU on. Include a note that if the process is killed the PSU keeps its last setting, and recommend a hardware thermal cutoff on the heater.
- docs/pid_comparison.md: procedure to compare our PID against LabVIEW ThermalControl.vi using the same gains and setpoint, with the CSV columns to compare and pass criteria.
- run.bat: activates .venv and runs `python -m transferstage`.
- Optional PyInstaller spec (transferstage.spec) producing a one-folder build; document that the 64-bit Kinesis install is still required on the target PC.
- Final README: install (including 64-bit Kinesis), configuration, TourBox key mapping table, simulation mode, safety notes, scripting API example.
- Run the full test suite and the app in simulation mode one last time.

Don't run anything against real hardware; list the checklist steps for me to execute. Done when docs are written, tests pass, run.bat works. Commit as "Phase 7: validation docs and packaging".
```
