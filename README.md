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

The GUI arrives in Phase 5. Until then, the package can be imported and tested:

```powershell
.venv\Scripts\Activate.ps1
pytest
```

The app writes its log to `logs/app.log` (rotating, 5 × 5 MB).
