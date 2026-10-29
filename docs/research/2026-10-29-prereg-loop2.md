# Pre-registration: loop 2 of the intraday CPR retest (2026-10-29, 23:05 IST, written before the runs)

## From the day-3 read (six trades, `scripts/2026-10-29-trade-read/dossier29 → out29.txt`)

* **LT** entered at 14:50 with 35 minutes to the close and the first wall 98 bps (3.1R) away: nowhere to go, −12 bps
  gross, −₹130 after ₹103 of charges. The sequence has no clock.
* **SHREECEM** entered at 23255 with PDH 23265 four bps above (0.09R) and R1 at 0.6R; through PDH by 15 points, then
  faded to the stop (−1R). The entry bar was the third up bar after the retest, 60 bps above the retest low.
* **TATAPOWER** and **JSWSTEEL** both came within one or two ticks of the stop (MAE −49 of 51 bps risk; −45 of 52)
  before working. The stop at the retest bar's low minus a tenth of the ADR is probed.
* **JSWSTEEL** +46 bps gross = ₹114 at ₹25,000, minus ₹103 of charges = ₹11. Not a rule problem: a size problem.

## Candidates, scored on the dev window (2024-01 → 2025-09) under the shipped loop-1 rules (no round walls, cap 0.6 ADR)

1. **Entry cutoff**: no new entries from 14:00 / 14:30 / 15:00 (a signal after the cutoff is recorded, not taken).
2. **Room to the nearest real level**: skip when the nearest level beyond the entry is < 0.15 / 0.3 / 0.5 R away
   (loop 1 tested ≥ 1R and did not ship it; this is the other end).
3. **Stop width**: the retest bar's low/high minus 1.5× and 2× the tolerance (same walls, same trail).

## Decision, fixed before the run

A candidate ships only if, on the dev window, it improves net per trade by ≥ 3 bps **and** t by ≥ 0.3 against the
loop-1 baseline, and the trades it removes or changes are clearly negative on their own (net ≤ −10). The by-time and
by-room tables are reported whole; the thresholds above are the only ones considered. Day 3 is one day and chooses
nothing. The notional question (₹25,000 → ₹1,00,000 in shadow so net reads at the research's 15 bps) is a reporting
choice for the owner, not a rule change.
