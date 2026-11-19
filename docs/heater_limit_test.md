# Heater limit test: 260 °C and 290 °C

Find out how hot the transfer stage can safely run with the Thorlabs **HT15W** cartridge heater,
first up to its rated 260 °C (Phase A), then a short excursion to 290 °C (Phase B).

| | |
|---|---|
| Date | |
| Operator | |
| Git commit (`git rev-parse --short HEAD`) | |
| Phase A session CSV (`logs/YYYYMMDD_HHMMSS.csv`) | |
| Phase B session CSV | |

Profiles: [`profiles/limit_test_A_260C.yaml`](../profiles/limit_test_A_260C.yaml),
[`profiles/limit_test_B_290C.yaml`](../profiles/limit_test_B_290C.yaml) (load them with **Load…** on the
Profile tab).

---

## 1. What the spec sheet allows

From Thorlabs spec sheet 14035-S01 Rev C (HT15W):

| Item | Value | What it means here |
|---|---|---|
| Power | 15 W at 24 V nominal (12.5–18 W) | The app's 24 V / 0.8 A limits already fit the heater |
| Resistance | 27.6–41.1 Ω; can rise up to 8 % in the first hour of use | Track V/I; more than ~8 % drift means the heater is degrading |
| **Max block temperature** | **260 °C**, installed in a Ø0.125" bore in a metal block | Rated ceiling |
| **Lead exits** | PTFE end caps, "should never exceed 260 °C" | The real limit: the steel sheath can run hotter, the caps cannot |
| Release agents / moisture | Warm to 120 °C before full power to restore insulation | Phase A starts with a 120 °C hold |

> **290 °C is about 30 °C above the rating.** Above 260 °C, PTFE starts to break down: it slowly
> loses strength, and its fumes become a health risk (polymer fume fever), getting worse toward
> its 327 °C melting point. Phase B is a one-off, short excursion, not a normal operating point.

### Expected drive (feedforward model in `config.yaml`: 0.618 °C/V², ambient 37.7 °C)

| Target | Predicted V | Power at ~30 Ω |
|---|---|---|
| 200 °C | 16.2 V | ~8.7 W |
| 260 °C | 19.0 V | ~12 W |
| 290 °C | 20.2 V | ~13.6 W |
| 24 V (max) | levels off near 394 °C | ~19 W |

The model was fitted at lower temperatures. Radiation losses grow quickly when hot, so expect
the real voltages to come out higher. Finding out how much higher is one of the goals of this test.

---

## 2. Before you start

- [ ] **Know where the RTD is.** The app only sees the DMM RTD. If it sits at the sample, away from
      the heater, the heater block and lead exits may be **hotter** than the reading.
- [ ] **Fit a second sensor at the heater lead exit** (where the PTFE caps are). A thermocouple taped or
      clamped there and read on a handheld meter is enough. This reading decides go/no-go, not the RTD.
- [ ] Everything else on the stage can take 260–290 °C: wire insulation, thermal paste or epoxy,
      adhesives, stamps (PDMS or other polymers), slide holder, nearby plastic. Take off anything not needed.
- [ ] Cold heater resistance measured with a DMM: ________ Ω
- [ ] Ventilation: fume hood or extraction if available. Nothing flammable near the stage.
- [ ] Someone stays with the stage the whole time. Safe-lock (**Ctrl+F1**) and the PSU's front-panel
      output switch are within reach.
- [ ] Z stage parked clear of the sample.
- [ ] Logging is on (1 s interval). Press **Ctrl+F7** (mark) at the start of each hold.

### Software limits

Change these on the **Config tab** while the app is running (it rewrites `config.yaml`).

| Phase | `limits.t_max_c` | Why |
|---|---|---|
| A | **275 °C** | Trips 15 °C above the rated top without getting in the way of normal overshoot |
| B | **300 °C** | Same as the hard limit in the code (`HARD_T_MAX_C`); only 10 °C above target, so keep overshoot small |
| After the test | back to your normal value | |

`limits.stale_temp_s` (5 s) and `limits.v_max` / `i_max` (24 V / 0.8 A) stay as they are.

---

## 3. Stop rules (both phases)

Press **Ctrl+F1** (safe-lock) right away if any of these happen:

- Lead-exit temperature reaches **250 °C** (10 °C below the PTFE rating). This applies even in Phase B,
  when the RTD reads 290 °C.
- Any smell, smoke, or discolouration on the leads or caps.
- Heater resistance (V/I) moves by more than 8 % from the cold value.
- The PSU is current-limited (CC mode), or the PID sits at 24 V and the temperature stops rising.
- The RTD reading jumps, drops out, or disagrees with the lead-exit thermocouple in a way that doesn't make sense.

---

## 4. Phase A: up to 260 °C (within spec)

Profile `limit_test_A_260C`. Takes about 95 min, plus cool-down.

| Segment | Ramp to | Rate | Hold |
|---|---|---|---|
| 1 | 120 °C | 10 °C/min | 10 min (dry-out) |
| 2 | 200 °C | 10 °C/min | 10 min |
| 3 | 230 °C | 5 °C/min | 10 min |
| 4 | 250 °C | 3 °C/min | 10 min |
| 5 | 260 °C | 2 °C/min | 20 min |
| 6 | 25 °C | step | heater off, passive cool-down |

The ramps slow down near the top to keep PID overshoot small. The thermal time constant is ~265 s,
so a 10 min hold gives a reasonably settled reading.

Write down values at the **end** of each hold (V and I are also in the CSV):

| Hold | RTD T (°C) | Lead exit T (°C) | PSU V | PSU I (A) | R = V/I (Ω) | PID at 24 V or CC? | Overshoot (°C) | Notes (smell, colour) |
|---|---|---|---|---|---|---|---|---|
| 120 °C | | | | | | | | |
| 200 °C | | | | | | | | |
| 230 °C | | | | | | | | |
| 250 °C | | | | | | | | |
| 260 °C | | | | | | | | |

**Phase A passes if:** no stop rule fired, the lead exit stayed **≤ 230 °C** at the 260 °C hold, and the
PID output stayed clearly below 24 V (headroom left).

- [ ] Phase A passed. If not, **do not run Phase B**.

---

## 5. Phase B: short excursion to 290 °C (outside spec)

Profile `limit_test_B_290C`. Set `limits.t_max_c = 300` first. Planned to keep **time above 260 °C as
short as possible**.

| Segment | Ramp to | Rate | Hold |
|---|---|---|---|
| 1 | 250 °C | 10 °C/min | 5 min (go/no-go check below spec) |
| 2 | 290 °C | 15 °C/min | 1 min |
| 3 | 25 °C | step | heater off immediately |

**Time above 260 °C: about 3.5 min.**
- Ramp 260 → 290 °C: 2 min.
- Hold: 1 min. Set `hold_min: 0` in the profile to only touch 290 °C, which brings the total to about 2.5 min.
- Passive cool-down 290 → 260 °C: about 35 s (τ ≈ 265 s, ambient ≈ 38 °C). A fan on the stage would shorten this.

**Why 15 °C/min and not faster**
- **Power headroom:** at 24 V the model levels off near 394 °C, so at ~275 °C the fastest possible climb
  is about 27 °C/min, and less if radiation losses are higher than modelled. At 15 °C/min the PID keeps
  up instead of falling behind and then overshooting.
- **Trip margin:** the 300 °C trip is only 10 °C above target. If the PID overshoots, the trip at the peak
  switches everything off. Try 20 °C/min only if the Phase A ramps showed overshoot under ~2 °C.

**Go/no-go at the 250 °C hold.** Let the ramp to 290 °C start only if:
- [ ] lead exit is ≤ 230 °C
- [ ] PID output is clearly below 24 V, and the PSU is not current-limited

Otherwise press **Stop profile** and step the set point down.

**During and after the excursion**
- The lead caps heat up later than the block, so a short spike helps. It also means their temperature
  peaks **after** the RTD peak. Keep watching the lead-exit thermocouple for 1–2 min after the step down.
- If the lead exit reaches 250 °C at any point, press Ctrl+F1 right away.

| Point | RTD T (°C) | Lead exit T (°C) | PSU V | PSU I (A) | R = V/I (Ω) | Notes |
|---|---|---|---|---|---|---|
| End of 250 °C hold | | | | | | |
| Peak (290 °C) | | | | | | |
| End of 290 °C hold | | | | | | |
| Lead-exit peak after step down | — | | — | — | — | time after step: |
| RTD overshoot above 290 °C | | | | | | |
| Total time above 260 °C (from CSV) | | | | | | |

---

## 6. Afterwards

- [ ] Cooled to room temperature. Cold resistance again: ________ Ω (change: ____ %)
- [ ] Leads and PTFE caps inspected (colour, cracks, softening): ________
- [ ] `limits.t_max_c` set back to the normal value on the Config tab.
- [ ] From the CSVs, plot temperature against V² at each hold and refit `ff_k_c_per_v2` / `ff_t_amb_c`
      for high temperatures. The coefficient is likely to drop when hot.
- [ ] Check overshoot and settling at 250–260 °C. The PID gains were tuned around 80 °C
      ([pid_comparison.md](pid_comparison.md)); retune if the high-temperature response is poor.

### Recommendation to take forward

- Routine use: keep the stage at or below about **250 °C** (10 °C under the rating).
- If 290 °C is needed routinely, change the hardware rather than the software: a heater with
  high-temperature lead insulation (fibreglass or mica instead of PTFE), or a mount that puts the lead
  exits outside the hot zone.
