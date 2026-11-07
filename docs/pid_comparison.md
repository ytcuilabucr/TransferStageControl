# PID comparison: Python vs LabVIEW ThermalControl.vi

**Goal:** show that, with the same gains and set point, the Python controller
(`transferstage/control/pid.py`) controls the heater the same way as the LabVIEW `ThermalControl.vi`
(NI `PID.vi`). Do this after the hardware acceptance checklist ([acceptance.md](acceptance.md))
has passed.

There are two tests:

- **A. Replay (algorithm test):** feed the temperature and set point recorded by LabVIEW into our PID
  and compare its output with the voltage LabVIEW actually commanded, sample by sample. Plant
  differences (sample position, room temperature) don't matter here, so this is the stricter test of the
  algorithm itself.
- **B. Closed-loop runs (behaviour test):** run the same step on the same hardware with each program
  and compare the step responses.

Both use [scripts/compare_pid.py](../scripts/compare_pid.py).

## What is expected to match, and what isn't

Both controllers use the NI parameterization `u = Kc·(e + (1/Ti)∫e dt + Td·d(−PV)/dt)` with Ti and Td
in **minutes**. Small differences are expected from:

- **Loop timing.** LabVIEW `PID.vi` measures dt itself, and the loop period has jitter on both sides. Use the same nominal period.
- **Anti-windup.** NI and our conditional integration differ only while the output is saturated (0 V or `output_high`).
- **The first sample after (re)initialization.** Both skip or limit the derivative differently.
- **The PSU's 0.1 V setpoint resolution**, which applies to both programs equally.

So compare with Td = 0 first (PI). That's also what is normally used on this heater.

## Fixed conditions (both programs)

| Setting | Value |
|---|---|
| Kc, Ti, Td | Gains currently used in LabVIEW, e.g. Kc = ____, Ti = ____ min, Td = 0 |
| PID period | Same as LabVIEW's loop (Python: `pid.period_ms`) |
| Output range | 0 – `output_high` V, same in both (≤ 24 V) |
| PSU current limit | Same `i_max` |
| Sample / holder | Same sample, same mounting, same Z position |
| Start temperature | Within ±1 °C of each other (let it cool to room temperature between runs) |
| Test step | From room temperature to **80 °C, ramp rate 0** (pure step), then hold **20 min** |
| Optional ramp test | Room temperature → 80 °C at **10 °C/min**, hold 20 min |

## Recording LabVIEW data

The Python app logs automatically to `logs/YYYYMMDD_HHMMSS.csv`. For LabVIEW, log (or export from the
chart, right-click → Export → Excel) at **every loop iteration** if possible:

| Quantity | Suggested LabVIEW column | Python CSV column |
|---|---|---|
| Time, s (or ISO date-time) | `Time` | `elapsed_s` (or `timestamp`) |
| Measured temperature, °C | `T` | `temperature_c` |
| Set point given to `PID.vi` (the ramped / program SP) | `SP` | `program_sp_c` |
| Final set point | – | `target_sp_c` |
| PID output = voltage commanded to the PSU, V | `V` | `v_cmd` |
| PSU voltage / current readback (if logged) | `V_read`, `I_read` | `v_read`, `i_read` |

Save as CSV (comma, semicolon or tab delimited all work).

**For test A**, the LabVIEW log must be at the PID loop rate, and must contain the **set point actually fed to
PID.vi** (after the ramp), not only the final target.

## Test A: replay

1. Make the LabVIEW run (the step above) with the logging described.
2. Replay it through our PID with the same gains and output range:

```powershell
python scripts/compare_pid.py replay lv_step.csv --time Time --temp T --sp SP --volt V --kc 0.5 --ti 1.0 --td 0 --high 24
```

3. The script prints the RMS and maximum difference between our output and LabVIEW's.

Use `--start` / `--end` (seconds) to skip a section, for example the first seconds before the PSU was switched on.
The replay starts with the integrator preset so that its first output equals LabVIEW's first output.

**Pass:** RMS difference ≤ **0.1 V** (the script's default `--tol`). Individual samples may differ by more
while the output is saturated at 0 V or `output_high` (anti-windup differences). These should die
out within about Ti after the output leaves saturation.

If it fails with a constant offset, check the units of Ti/Td (minutes vs seconds) and the output range. If it fails with a scale
error, check whether LabVIEW's PID input was in °C. If it fails only during saturation, that's the expected anti-windup
difference: check the closed-loop test instead.

## Test B: closed-loop runs

1. **LabVIEW run.** Close the Python app. Start `ThermalControl.vi` with the fixed gains, PSU on, apply the step
   to 80 °C, and hold 20 min while logging. Turn the PSU off and let it cool to room temperature.
2. **Python run.** Close LabVIEW. In the Python app, Settings tab: enter the same Kc/Ti/Td, PSU On, set point 80, ramp 0,
   **Set**. Hold 20 min, then PSU Off. Note the session CSV name.
3. Trim both files to the step (from just before the set-point change to the end of the hold) with
   `--start/--end` if needed, and compare:

```powershell
python scripts/compare_pid.py compare logs\20261106_101500.csv lv_step.csv --lv-time Time --lv-temp T --lv-sp SP --lv-volt V
```

4. Overlay the curves (T and V vs time, both runs) in Origin/Excel, and attach the plot to the acceptance sheet.
5. Optionally repeat with the 10 °C/min ramp.

The script computes, for each run:

| Metric | Definition |
|---|---|
| `rise_time_s` | Time from 10 % to 90 % of the step (start T → final SP) |
| `overshoot_c` | Largest excursion of T beyond the final SP, in the step direction |
| `settling_time_s` | From the SP change until T stays within ±1 °C of the SP (`--band`) |
| `ss_mean_error_c`, `ss_std_c` | Mean and standard deviation of T − SP over the last 5 min (`--ss-window`) |
| `ss_mean_v` | Mean PSU command over the last 5 min |
| `peak_v` | Largest PSU command after the SP change |

**Pass criteria** (Python vs LabVIEW). The script checks these and prints PASS/FAIL:

| Metric | Criterion |
|---|---|
| Overshoot | \|Δ\| ≤ 1.0 °C |
| Rise time | within ±20 % |
| Settling time (±1 °C) | within ±25 % |
| Steady-state mean error | \|Δ\| ≤ 0.3 °C (and, checked by eye, each run's own \|error\| ≤ 0.5 °C) |
| Steady-state noise (std) | \|Δ\| ≤ 0.2 °C |
| Steady-state voltage | \|Δ\| ≤ 0.5 V |

Also check by eye: no oscillation in the Python run that is absent in the LabVIEW run, and the output never exceeds `output_high`.

If test A passes but test B fails, the difference is in the loop: the period/jitter, the timing of
the temperature reading, or the PSU command path. It's not the PID algorithm. Compare `elapsed_s` spacing and
`v_read` against `v_cmd`.

## Record

| | LabVIEW | Python | Δ | Pass |
|---|---|---|---|---|
| Date / file | | | | |
| Kc / Ti / Td | | | | |
| Start T (°C) | | | | |
| Rise time (s) | | | | |
| Overshoot (°C) | | | | |
| Settling time (s) | | | | |
| SS mean error (°C) | | | | |
| SS std (°C) | | | | |
| SS voltage (V) | | | | |
| Replay RMS (V) | – | | | |
