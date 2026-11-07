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

## Setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[dev]
```

## Configuration

Edit `config.yaml` in the project root. Sections:

| Section   | Contents |
|-----------|----------|
| `devices` | serial numbers, PSU COM port, DMM VISA alias, Kinesis path, `simulate` flags for each device |
| `pid`     | `kc`, `ti_min`, `td_min` (NI-style, minutes), `period_ms`, `output_low`/`output_high` (V) |
| `ramp`    | `default_rate_deg_min` (0 = step change) |
| `limits`  | `t_max_c` (≤ 300), `v_max` (≤ 24), `i_max`, Z soft limits (µm), `stale_temp_s` |
| `keys`    | action → Qt shortcut string; set these to match the TourBox Console mapping |
| `logging` | log directory and data-log interval |

Every device is simulated by default (`simulate: true`), so the app runs without hardware.
Set a flag to `false` once that instrument is connected.

```python
from transferstage.config import load_config
cfg = load_config()          # reads ./config.yaml, raises ConfigError if invalid
```

## Running

```powershell
.venv\Scripts\Activate.ps1
python -m transferstage            # or: transferstage
```

Options: `--config PATH`, `--no-datalog`, `--sim-time-scale N` (speeds up the simulated
thermal plant for demos). With every `devices.simulate` flag true (the default) the app runs
entirely on simulators.

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

Each session writes a CSV to `logs/YYYYMMDD_HHMMSS.csv`; the app log is `logs/app.log`
(rotating, 5 × 5 MB). Closing the window (or an unhandled error) stops all motion, switches
the PSU off and closes the instruments; you are asked to confirm if the PSU is on.

### TourBox shortcuts

The defaults match the existing **TransferStageControl** preset in TourBox Console, so no
TourBox changes are needed (change the app side in Settings → Key bindings if needed):

| Action | TourBox control | Default key |
|---|---|---|
| Safe Lock (toggle) | Side, or Side + Top | `Ctrl+F1` |
| Cancel Motion | Top | `Ctrl+F6` |
| Speed up (+5 % of max, applied at once) | Knob clockwise | `Ctrl+F4` |
| Speed down | Knob counter-clockwise | `Ctrl+F5` |
| Jog Z up while held | Tall | `Ctrl+F2` |
| Jog Z down while held | Short | `Ctrl+F3` |

The shortcuts work whenever an app window is focused. Jogs start on key press and stop on key
release; auto-repeat is ignored, and a jog stops if the app loses focus. If the knob turns the speed the wrong way, swap the
`speed_up` / `speed_down` keys.

### PID autotune

Temperature tab → **Autotune**. Hold the sample near the tuning temperature with the PSU on, then
start: the PSU voltage switches between bias ± amplitude (clamped to 0 V and `limits.v_max`) around
the set point until the oscillation repeats for the set number of cycles. Leave **Bias** at
"Present output" to use the voltage that currently holds the set point. The table offers
Ziegler–Nichols and Tyreus–Luyben gains (Ti and Td in minutes). Tyreus–Luyben PI is pre-selected
because it is the conservative choice. **Apply gains** loads the selected row into the PID.

The run aborts on a safety trip, on Cancel, on PSU off, on timeout, or if T rises more than
"Max rise above SP" above the set point. After it ends, the PID holds the set point with the old gains.

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
    stage.sleep(600)                   # use stage.sleep, not time.sleep
    print(stage.readings())
```

`Stage(virtual_time=True)` (simulators only) runs on a virtual clock, so a long recipe finishes in
seconds. See [examples/anneal_and_rotate.py](examples/anneal_and_rotate.py):

```powershell
python examples/anneal_and_rotate.py               # simulators, virtual time
python examples/anneal_and_rotate.py --real-time   # wall clock (required with hardware)
```

## Tests

```powershell
pytest                 # hardware tests are skipped
pytest --hardware      # include tests that need the instruments
```
