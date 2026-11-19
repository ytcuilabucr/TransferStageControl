# TransferStageControl (Python)

Python port of the LabVIEW project in `reference/TransferStageControl LV2020/`. It controls:

- Thorlabs Z stage (KCube DC Servo `27270114`) and rotator (Integrated Stepper `55524924`) through Kinesis
- B&K Precision 1685B power supply (heater) over serial
- A DMM reading an RTD thermometer over VISA
- PID temperature control with setpoint ramps

See [PLAN.md](PLAN.md) for the roadmap.

> **Safety limits (hard-coded):** max temperature **300 °C**, max PSU voltage **24 V**.
> `config.yaml` may set lower limits; higher values are rejected when the config loads.

## Requirements

- Windows, **64-bit Python 3.12**
- **64-bit Thorlabs Kinesis**, installed to `C:\Program Files\Thorlabs\Kinesis`. The DLLs in
  `reference/.../Libs` are 32-bit and can't be used from 64-bit Python.
- NI-VISA (or another VISA backend) with an alias `DMM` for the multimeter
- The 1685B on `COM4` (configurable)

None of the instrument software is needed to run the app on the simulators.

## Install

1. **Python 3.12, 64-bit**, from python.org (tick "Add python.exe to PATH" or use the `py` launcher).
2. **Thorlabs Kinesis, 64-bit.** Download "Kinesis" for **64-bit Windows** from thorlabs.com
   (Software → Motion Control → Kinesis) and install it with the default path
   `C:\Program Files\Thorlabs\Kinesis`. A different path goes in `devices.kinesis_path`.
   Check with `python -m transferstage.drivers.kinesis --list`, which should list `27270114` and `55524924`.
   Close the Kinesis GUI before starting this app; only one program can own the stages.
3. **NI-VISA.** In NI MAX, give the multimeter the alias `DMM`
   (check with `python -m transferstage.drivers.dmm --alias DMM --watch`).
4. **B&K 1685B.** Install its USB-serial driver and note the COM port in Device Manager (default `COM4`).
5. **This project:**

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[dev]
```

6. Set the `devices.simulate` flags in `config.yaml` to `false` for the connected instruments,
   then work through the [hardware acceptance checklist](docs/acceptance.md) before using the app for real samples.

## Configuration

Edit `config.yaml` in the project root. Sections:

| Section   | Contents |
|-----------|----------|
| `devices` | serial numbers, PSU COM port, DMM VISA alias, Kinesis path, `simulate` flags for each device |
| `pid`     | `kc`, `ti_min`, `td_min` (NI-style, minutes), `period_ms`, `output_low`/`output_high` (V) |
| `ramp`    | `default_rate_deg_min` (0 = step change), `dial_step_c`, `set_enables_psu` (Set switches the PSU on for a set point above T_amb + 5 °C) |
| `limits`  | `t_max_c` (≤ 300), `v_max` (≤ 24), `i_max`, Z soft limits (µm), `stale_temp_s`, `idle_timeout_min` / `idle_grace_min` |
| `keys`    | action → Qt shortcut string; set these to match the TourBox Console mapping |
| `logging` | log directory and data-log interval |

Every device is simulated by default (`simulate: true`), so the app runs without hardware.
Set a flag to `false` once that instrument is connected.

```python
from transferstage.config import load_config
cfg = load_config()          # reads ./config.yaml, raises ConfigError if invalid
```

## Running

Double-click **`run.bat`**. It activates `.venv` and runs the app, passes any arguments through,
and keeps the console open with a message if the app exits with an error. Or, from a shell:

```powershell
.venv\Scripts\Activate.ps1
python -m transferstage            # or: transferstage
```

Options: `--config PATH`, `--no-datalog`, `--sim-time-scale N` (speeds up the simulated
thermal plant for demos).

### Simulation mode

With every `devices.simulate` flag `true` (the default) the app runs entirely on simulators, with no
hardware or instrument drivers needed. The flags are per device, so you can, for example, run the real stages
with a simulated heater. The simulated Z stage and rotator move at realistic speeds. The simulated heater is a
first-order thermal model (22 °C ambient), and `--sim-time-scale 20` makes it react 20× faster for demos.
The simulate toggles in Settings take effect on restart. The status bar shows which devices are
connected.

Tabs:
- **Manual**: Z Up/Down (hold to jog), speed knob + Set Speed, Cancel Motion, Safe Lock,
  Up/Down/Busy indicators, Z position slide, Z-status, time since last motion, Move Z to,
  Home Z; rotator Set Angle / Current Angle / Home; named Z + angle presets (saved in `config.yaml`).
- **Temperature**: set point + ramp rate + Set, Ramping indicator, PSU On/Off, PID gains and
  Reinitialize PID, readouts, temperature and voltage charts (rolling window, pause to zoom),
  profile editor/runner, alarm banner with Acknowledge.
- **Settings**: edit and save `config.yaml`. Key bindings apply on Save; the rest on restart.
  Limits cannot exceed 300 °C / 24 V.
- **Log**: live application log, alarm history, button to open the data-log folder.

Each session writes a CSV to `logs/YYYYMMDD_HHMMSS.csv`, plus `YYYYMMDD_HHMMSS.meta.yaml` with the
start time, version, connection errors and the full configuration used. CSV columns `sample_age_s`
(seconds since the last thermal reading; it grows if the loop stalls) and `note` come last. Type a
note in the **Note** field at the top and press **Mark** (or Enter, or the `mark` shortcut, Ctrl+F7)
to write an extra row with that note at that moment; an empty note gives `mark 1`, `mark 2`, ….
The app log is `logs/app.log` (rotating, 5 × 5 MB). It records every operator command (moves,
jogs, set points, PSU on/off, PID gains, profiles) and every mark. Closing the window (or an unhandled error) stops all motion, switches
the PSU off and closes the instruments; you are asked to confirm if the PSU is on.

### TourBox shortcuts

The defaults match the existing **TransferStageControl** preset in TourBox Console, so no
TourBox changes are needed (change the app side in Settings → Key bindings if needed):

| Action | TourBox control | Default key |
|---|---|---|
| Safe Lock on / off | Side + Top combo (set in TourBox Console) | `Ctrl+F1` |
| (nothing; status bar hint) | Side alone | `Ctrl+F12` |
| Cancel Motion | Top | `Ctrl+F6` |
| Speed Knob +1 position (applied at once) | Knob clockwise | `Ctrl+F4` |
| Speed down | Knob counter-clockwise | `Ctrl+F5` |
| Jog Z up while held | Tall | `Ctrl+F2` |
| Jog Z down while held | Short | `Ctrl+F3` |
| Event mark in the session CSV | (assign a free button) | `Ctrl+F7` |
| Setpoint +1 step (staged, not applied) | Dial clockwise (add to the preset) | `Ctrl+F8` |
| Setpoint −1 step | Dial counter-clockwise (add to the preset) | `Ctrl+F9` |
| Apply the setpoint (✔ Set) | C1 (add to the preset) | `Ctrl+F10` |
| Ramp On / Off (for the next Set) | C2 (add to the preset) | `Ctrl+F11` |

Safe Lock from the TourBox needs both buttons, as in the LabVIEW program, so a stray press of
either one never changes it. In TourBox Console the **Side + Top** combination sends `Ctrl+F1`
(`safe_lock`, toggles Safe Lock) and **Side** alone sends `Ctrl+F12` (`side_alone`: no action,
the status bar says to press Side + Top). One press may send its key twice, so a second
`Ctrl+F1` within 2 s is ignored; wait a moment before toggling again. The on-screen Safe Lock
button still toggles with one click. Every non-repeating TourBox key is written to
`logs/app.log` (`input map: key Ctrl+F1 -> safe_lock`), which shows what the TourBox sends.

The shortcuts work whenever an app window is focused. Jogs start on key press and stop on key
release; auto-repeat is ignored, and a jog stops if the app loses focus. TourBox Console does not
hold the key while a button is held: it sends an instant tap when the button goes down and another
when it comes up. A tap (released within 40 ms) therefore starts a jog that runs until the next tap
of the same key. If a tap is ever missed, Z keeps going to the soft limit: press Top (Cancel Motion)
or tap the button again. If the knob turns the speed the wrong way, swap the
`speed_up` / `speed_down` keys.

The Dial only changes the number in the setpoint box (step: Config → *Dial setpoint step*,
default 1 °C; clicks snap to whole steps, e.g. 21.94 → 22); the box starts at the running setpoint and turns amber while it differs from the running setpoint, and nothing reaches
the heater until C1 (or ✔ Set). C1 sends two taps per press, so a second Set within 1.5 s is
ignored; C2 (Ramp On/Off) is guarded the same way. Set uses the Ramp on/off and ramp rate shown in the panel. When the PSU output is off and the new set
point is more than 5 °C above the feedforward ambient (`pid.ff_t_amb_c`), Set also switches it on
(`ramp.set_enables_psu`, Config → *Set switches the PSU output on*, default on). This includes ramping
*down*: with the heater off the stage cools at roughly (T − T_amb)/tau, faster than a 10 °C/min ramp
above ~70 °C, and it would not hold the new set point. A set point near room temperature leaves the heater
off; Set never switches it on while a safety trip is latched, and leaves an output that is already on alone. If the Dial turns the setpoint the wrong way, swap the `setpoint_up` / `setpoint_down` keys.

### PID autotune

**Autotune** tab. Hold the sample near the tuning temperature with the PSU on, then
start: the PSU voltage switches between bias ± amplitude (clamped to 0 V and `limits.v_max`) around
the set point until the oscillation repeats for the set number of cycles. Leave **Bias** at
"Present output" to use the voltage that currently holds the set point. The table offers
Ziegler–Nichols and Tyreus–Luyben gains (Ti and Td in minutes). Tyreus–Luyben PI is pre-selected
because it is the conservative choice. **Apply gains** loads the selected row into the PID.

The run aborts on a safety trip, on Cancel, on PSU off, on timeout, or if T rises more than
"Max rise above SP" above the set point. After it ends, the PID holds the set point with the old gains.

## Safety

What the software enforces (GUI and scripting API alike):

- **Hard limits in code:** 300 °C and 24 V. `config.yaml` and the Config tab can only lower them, and
  `pid.output_high` can't exceed `limits.v_max`. A config above the limits is refused at start-up.
- **Over-temperature trip** at `limits.t_max_c`. The PSU output switches off and the alarm latches: the PSU can't be
  switched on again until the alarm is acknowledged.
- **Stale temperature trip:** if no valid reading arrives for `limits.stale_temp_s` (DMM unplugged, VISA error),
  the PSU switches off the same way.
- **Idle heater watchdog (GUI only):** if the PSU output is on and nobody has touched the keyboard or mouse (in any
  program on the PC) for `limits.idle_timeout_min` (20 min), an alarm window pops up on top with a countdown. If nobody
  clicks "I'm here" within `limits.idle_grace_min` (3 min), the PSU output switches off (not latched; it can be switched
  on again). The watchdog is on at every start; the checkbox next to Stop turns it off after a confirmation.
- **PSU deviation warning** when the readback differs from the command by more than 0.5 V (not while current-limited).
- **Z soft limits:** moves outside `z_min_um`–`z_max_um` are refused, and jogs stop at the limit. **Safe Lock** blocks
  every motion command except Cancel.
- **Shutdown:** closing the window, an unhandled exception, or leaving a `Stage` block stops both stages, sets the PSU to
  0 V with its output off, and closes the instruments. Each step runs even if an earlier one fails.

What it **cannot** do:

- If the process is **killed** (Task Manager, `taskkill /F`, a crash inside a driver DLL, power loss, a Windows
  update reboot), no shutdown runs. **The 1685B keeps its last voltage and output state**, so the heater
  stays on with nothing regulating it.
- The temperature limit relies on the one RTD reading. An RTD that has come loose from the heater block reads
  low, and the controller will keep heating.

**Fit an independent hardware thermal cutoff** in the heater circuit (a thermal switch or fuse on the heater
block, or a separate over-temperature controller), and don't leave the heater running unattended without it.
Test the trips with the [acceptance checklist](docs/acceptance.md) by lowering `limits.t_max_c` (e.g. to 40 °C),
never by heating to 300 °C.

## Scripting API

`transferstage.api.Stage` runs the instruments without the GUI, with the same PID and safety
monitor. Leaving the `with` block, including after an exception or Ctrl+C, runs the safe shutdown.

```python
from transferstage.api import Stage

with Stage() as stage:                 # config.yaml; session CSV in logs/
    stage.home()
    stage.move_z(5000)                 # µm, absolute; refused outside the soft limits
    stage.psu_enabled(True)
    stage.set_temperature(150, rate_deg_min=20)
    stage.wait_for_temperature(tol_c=1, stable_s=60, timeout_s=3600)
    stage.rotate(90)
    stage.mark("anneal start")         # note in the CSV `note` column and the log
    stage.sleep(600)                   # use stage.sleep, not time.sleep
    print(stage.readings())
```

`Stage(virtual_time=True)` (simulators only) runs on a virtual clock, so a long recipe finishes in
seconds. See [examples/anneal_and_rotate.py](examples/anneal_and_rotate.py):

```powershell
python examples/anneal_and_rotate.py               # simulators, virtual time
python examples/anneal_and_rotate.py --real-time   # wall clock (required with hardware)
```

## Validation

- [docs/acceptance.md](docs/acceptance.md): the hardware acceptance checklist. It covers every gui_v2 feature and the
  safety trips. Run it once on the real instruments.
- [docs/pid_comparison.md](docs/pid_comparison.md): how to compare this PID with LabVIEW `ThermalControl.vi` using the
  same gains, with pass criteria. [scripts/compare_pid.py](scripts/compare_pid.py) computes the metrics:

```powershell
python scripts/compare_pid.py metrics logs\20261106_101500.csv
python scripts/compare_pid.py compare logs\20261106_101500.csv lv_step.csv --lv-time Time --lv-temp T --lv-sp SP --lv-volt V
python scripts/compare_pid.py replay lv_step.csv --time Time --temp T --sp SP --volt V --kc 0.5 --ti 1.0
```

## Stand-alone build (optional)

A PyInstaller one-folder build runs on a PC without Python:

```powershell
pip install -e .[build]
pyinstaller transferstage.spec --noconfirm
```

This produces `dist\TransferStageControl\`. Copy the whole folder to the target PC and run
`TransferStageControl.exe`. `config.yaml` sits next to the exe, and `logs\` is created there.
The build does **not** include the instrument software. The target PC still needs **64-bit Thorlabs
Kinesis** (its DLLs are loaded at run time from `devices.kinesis_path`), NI-VISA with the `DMM` alias, and the 1685B
USB-serial driver, except for devices left in simulation.

## Tests

```powershell
pytest                 # hardware tests are skipped
pytest --hardware      # include tests that need the instruments
```
