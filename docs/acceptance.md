# Hardware acceptance checklist

Run this once on the real instruments before using the Python app in place of the LabVIEW
`gui_v2.vi`. Work through the sections in order. Tick each box, and note the measured
value in the **Result** column where there is one. If any step fails, stop and record what you saw
(and the matching lines from `logs/app.log`).

| | |
|---|---|
| Date | |
| Operator | |
| Git commit (`git rev-parse --short HEAD`) | |
| Session CSV (`logs/YYYYMMDD_HHMMSS.csv`) | |

> **Safety**
> - Keep a hand near the PSU's front-panel output switch the whole time a heater test runs.
> - Never test the over-temperature trip by heating toward 300 °C. Lower `limits.t_max_c` instead (section 6.1).
> - Work with the stage clear of the sample, and with Z soft limits set to a range that is safe for the
>   mounted hardware, until section 2 has passed.

---

## 0. Preparation

- [ ] 64-bit Thorlabs Kinesis is installed at `C:\Program Files\Thorlabs\Kinesis` (or `devices.kinesis_path` points to it).
- [ ] Kinesis (the Thorlabs GUI) and LabVIEW are **closed**. Only one program can own the stages, the COM port and the VISA session.
- [ ] NI MAX shows the VISA alias `DMM`, and `*IDN?` answers.
- [ ] The B&K 1685B is on `COM4` (Device Manager) with its output **off**.
- [ ] The heater has an independent hardware thermal cutoff (see section 8).
- [ ] Back up `config.yaml`, then set `devices.simulate` to `false` for `z`, `rotator`, `psu` and `dmm`.
- [ ] Set `limits.z_min_um` / `z_max_um` to a range that is safe for the current mechanical setup.
- [ ] `pytest` passes (hardware tests skipped).
- [ ] The Phase 3 driver checks passed: `python scripts/smoke_test.py`, or the driver CLIs:

```powershell
python -m transferstage.drivers.kinesis --list
python -m transferstage.drivers.dmm --alias DMM --watch
python -m transferstage.drivers.psu_1685b --port COM4 --read
```

## 1. Start-up

| # | Step | Expected | Result |
|---|---|---|---|
| 1.1 | Run `run.bat` | Window opens with no "Device connection" warning | ☐ |
| 1.2 | Status bar | Z, rotator, PSU and DMM are all shown as connected; alarm indicator clear | ☐ |
| 1.3 | Temperature tab | T readout matches the DMM front panel (±0.2 °C) and updates every PID period | ☐ |
| 1.4 | Temperature tab | PSU shows **Off**, V ≈ 0, I ≈ 0 | ☐ |
| 1.5 | Manual tab | Z position matches Kinesis / the controller display; Z-status **Idle** | ☐ |
| 1.6 | Manual tab | Current Angle matches the rotator's real position | ☐ |
| 1.7 | Settings tab | Motor SN# `27270114`, Rotator SN# `55524924`, PSU `COM4`, DMM `DMM`, PID period as in `config.yaml` | ☐ |
| 1.8 | `logs/` | A new `YYYYMMDD_HHMMSS.csv` was created and grows once per `logging.interval_s` | ☐ |

## 2. Z stage (Manual tab)

| # | Step | Expected | Result |
|---|---|---|---|
| 2.1 | **Home Z** | Z-status **Homing**, then **Idle**; position ≈ 0 µm | ☐ |
| 2.2 | Speed knob to a low value (e.g. 50 µm/s), **Set Speed** | Speed readout shows the new value | ☐ |
| 2.3 | Hold **Up** for ~2 s, release | Z rises while held, **Up** and **Busy** lit, Z-status **Moving**; stops on release; distance ≈ speed × time | ☐ |
| 2.4 | Hold **Down** for ~2 s, release | Same, downward, **Down** lit | ☐ |
| 2.5 | Note **Since Last Motion** | Resets to 0 on each move, then counts up | ☐ |
| 2.6 | Change speed, jog again | New speed is used | ☐ |
| 2.7 | **Move Z to** a target within the soft limits | Moves there and stops; position slide follows | ☐ |
| 2.8 | Start a long move, press **Cancel Motion** | Stops immediately (Z-status **Stopping** → **Idle**) | ☐ |
| 2.9 | Turn on **Safe Lock**, then try Up, Down, Move Z to, Set Speed, Home | Every command is refused (message in the Log tab); Z does not move | ☐ |
| 2.10 | With Safe Lock on, start nothing; press **Cancel Motion** | Cancel still works (it is never blocked) | ☐ |
| 2.11 | Turn Safe Lock off | Commands work again | ☐ |

## 3. TourBox / keyboard shortcuts

Use the TourBox with the **TransferStageControl** preset active in TourBox Console, with the app window focused.

| # | Control | Default key | Expected | Result |
|---|---|---|---|---|
| 3.1 | Tall (hold) | `Ctrl+F2` | Jogs Z up while held, stops on release | ☐ |
| 3.2 | Short (hold) | `Ctrl+F3` | Jogs Z down while held, stops on release | ☐ |
| 3.3 | Knob clockwise | `Ctrl+F4` | Speed increases by 5 % of max per click | ☐ |
| 3.4 | Knob counter-clockwise | `Ctrl+F5` | Speed decreases | ☐ |
| 3.5 | Top | `Ctrl+F6` | Cancel Motion | ☐ |
| 3.6 | Side / Side + Top | `Ctrl+F1` | Toggles Safe Lock | ☐ |
| 3.7 | Hold Tall, then click another window (Alt+Tab) | | Jog stops when the app loses focus | ☐ |

## 4. Rotator (Manual tab)

| # | Step | Expected | Result |
|---|---|---|---|
| 4.1 | **Home** rotator | Homes; Current Angle ≈ 0° | ☐ |
| 4.2 | **Set Angle** 90° | Rotates to 90°; Current Angle within ±0.1° | ☐ |
| 4.3 | **Set Angle** 0° | Returns to 0° | ☐ |
| 4.4 | Save a preset (Z + angle), move elsewhere, go to the preset | Both axes return to the saved values; the preset is still there after a restart | ☐ |

## 5. Temperature control (Temperature tab)

Use moderate temperatures (≤ 100 °C) for this section.

| # | Step | Expected | Result |
|---|---|---|---|
| 5.1 | Enter PID gains (Kc, Ti, Td) from the LabVIEW setup and apply | Values held; Log tab records the change | ☐ |
| 5.2 | **PSU On** | PSU front panel shows output on; V starts at 0 | ☐ |
| 5.3 | Set point 60 °C, ramp 10 °C/min, **Set** | **Ramping** lit; program SP rises at 10 °C/min; V increases | ☐ |
| 5.4 | Wait for the ramp to finish | Ramping goes off when program SP reaches 60; T settles within ±1 °C | ☐ |
| 5.5 | V readback vs command | PSU V readback ≈ V cmd (no PSU-deviation warning while not current-limited) | ☐ |
| 5.6 | Set point 40 °C, ramp 5 °C/min, **Set** | Program SP falls at 5 °C/min; V drops; T follows down | ☐ |
| 5.7 | Set point 50 °C, ramp **0** | Program SP steps straight to 50 °C (no ramp) | ☐ |
| 5.8 | **Reinitialize PID** while holding | Integral cleared; no large output jump beyond the P term | ☐ |
| 5.9 | **PSU Off** | Output off on the front panel, V cmd 0; T starts falling | ☐ |
| 5.10 | Charts: T/target/program SP and PSU voltage | All traces plot live; **Pause** freezes the view for zooming; un-pause resumes | ☐ |
| 5.11 | Profile: 3 segments (e.g. 50 °C @ 10/min hold 2 min → 70 °C @ 5/min hold 2 min → 40 °C @ 10/min hold 0) and run | Segments run in order; the current segment is shown; **Stop** ends the profile | ☐ |
| 5.12 | (optional) **Autotune** around 60 °C with a small amplitude (e.g. ±2 V) | Oscillation, then a gains table; Cancel aborts with PSU still controlled | ☐ |

## 6. Safety tests

### 6.1 Over-temperature trip

Don't heat toward 300 °C. Lower the limit temporarily instead:

1. [ ] Close the app. In `config.yaml` set `limits.t_max_c: 40` (pick a value ~10 °C above the room/sample temperature).
2. [ ] Start the app. PSU On. Set point 60 °C, ramp 10 °C/min.
3. [ ] When T reaches 40 °C: **over-temperature alarm**, PSU output **off** (front panel), V cmd 0, alarm banner shown. Recorded T at trip: ______ °C.
4. [ ] Press **PSU On** while the alarm is latched: refused, the output stays off.
5. [ ] Let T fall below 40 °C, press **Acknowledge**: the banner clears and PSU On works again.
6. [ ] The alarm appears in the Log tab's alarm history and in the CSV `alarms` column.
7. [ ] **Restore `limits.t_max_c`** to its normal value and restart the app.

### 6.2 Stale temperature (DMM lost)

1. [ ] PSU On, holding ~50 °C.
2. [ ] Unplug the DMM's USB/GPIB cable (or switch the DMM off). Start a stopwatch.
3. [ ] Within `limits.stale_temp_s` (default 5 s) plus one PID period: **stale-temperature alarm**, PSU output **off**. Measured time: ______ s.
4. [ ] Reconnect the DMM. Restart the app if the readings don't come back (the VISA session may need to reopen), then Acknowledge.

### 6.3 Z soft limits

1. [ ] **Move Z to** a value above `limits.z_max_um`: refused (message in the Log tab), no motion.
2. [ ] Same below `limits.z_min_um`: refused.
3. [ ] Jog up towards `z_max_um` at a low speed: the stage stops **at** the limit, not past it.
4. [ ] Jog down towards `z_min_um`: stops at the limit.

### 6.4 Voltage cap

1. [ ] Set `pid.output_high` in Settings above `limits.v_max`, or above 24: it cannot be saved.
2. [ ] With a large set-point step (e.g. 20 → 80 °C, ramp 0), V cmd never exceeds `pid.output_high` (≤ 24 V). Peak V seen: ______ V.

### 6.5 Closing the app with the PSU on

1. [ ] PSU On, holding ~50 °C, Z moving slowly.
2. [ ] Close the window: a confirmation appears ("The PSU output is ON…"). Choose **No**: the app keeps running.
3. [ ] Close again, choose **Yes**: Z stops, PSU output off and 0 V (check the front panel), the app exits. `logs/app.log` ends with "shutting down" and no failed shutdown steps.

### 6.6 Config validation

1. [ ] Set `limits.t_max_c: 400` in `config.yaml` and start: the app refuses to start with a config error. Restore.
2. [ ] Same with `limits.v_max: 30`. Restore.

## 7. Session data

- [ ] Open the session CSV in Excel/Origin. The columns are: `timestamp, elapsed_s, temperature_c, target_sp_c,
  program_sp_c, v_cmd, v_read, i_read, psu_on, ramping, profile_segment, profile_phase, mode, z_um,
  z_state, speed_um_s, rotator_deg, rotator_state, safe_lock, alarms`.
- [ ] Rows are at `logging.interval_s`, with no gaps longer than a few intervals.
- [ ] The ramps, the trip and the motion from the tests above are visible in the data.

## 8. Known limitation: process killed

The safe shutdown runs when the window is closed, and when an unhandled Python exception happens.
It **cannot** run if the process is killed: Task Manager "End task", `taskkill /F`, a Python crash
inside a native DLL, a power cut, or Windows restarting for updates.
In that case **the 1685B keeps its last voltage setting and output state**. The heater stays on with
nothing regulating it, and no over-temperature or stale-reading check runs.

To confirm this (optional, at a low voltage only):

1. [ ] Set `limits.v_max` low, e.g. 3 V. PSU On, set point a little above room temperature so V cmd is a few volts.
2. [ ] Kill `python.exe` in Task Manager.
3. [ ] The PSU front panel still shows output on at the last voltage. Switch it off by hand.

**Recommendation:** fit a hardware over-temperature cutoff in series with the heater supply
(a bimetal thermal switch or thermal fuse on the heater block, rated below the maximum safe temperature
of the sample and stage, or a stand-alone over-temperature controller with its own sensor).
It must be independent of this PC and software. Also don't leave the system heating unattended
until this cutoff is in place.

## Sign-off

| Section | Pass / Fail | Notes |
|---|---|---|
| 1 Start-up | | |
| 2 Z stage | | |
| 3 Shortcuts | | |
| 4 Rotator | | |
| 5 Temperature | | |
| 6 Safety | | |
| 7 Data | | |
| PID comparison ([pid_comparison.md](pid_comparison.md)) | | |
