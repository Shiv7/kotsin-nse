# Shadow day 3 (2026-10-29) read trade by trade, and loop 2 (committed 22:24 IST, 9c352d5)

Loop 1 live (no round walls, 0.6 ADR cap, continuous H/L). NIFTY +1.05 % (open 22694 flat, first hour 22657–22799,
close 22937 just under R2 22938). Seven signals, all long; six taken, one skipped by the cap. Dossiers in
`scripts/2026-10-29-trade-read/out29.txt` (built from the day's archived bars by `dossier29.py`).

| trade | entry | nearest level | path | exit | gross | net ₹ at ₹25k |
|---|---|---|---|---|---|---|
| ETERNAL | 10:50 at 331.05, stop 327.74 (100 bps) | PDH 331.75 (0.21R) | 25 min of chop (MAE −47), then 339.35 at 13:55 | close 337.85, trailed | +205 bps, 2.1R | +407 |
| TIINDIA | 14:10 at 2140.90, stop 2118.33 (105 bps) | PDH 2146 (0.23R) | straight up to 2177 at the close | close | +169 bps, 1.6R | +294 |
| TATAPOWER | 10:25 at 339.30, stop 337.57 (51 bps) | R1 339.73 (0.25R) | low 337.65 at 10:50 — 8 paise from the stop — then 343.75 at 13:00 | trail 342.84 at 13:05 (stock closed 344.55) | +104 bps, 2.0R | +155 |
| JSWSTEEL | 10:45 at 1249.70, stop 1243.17 (52 bps) | PDH 1256.20 (1.0R) | low 1244.10 at 11:00 — 0.9 points from the stop — then 1255.60 at 14:25, 60 paise under the wall, never tagged | close 1255.40 | +46 bps, 0.9R | +11 |
| LT | 14:50 at 3841.60, stop 3829.48 (31 bps) | R1 3879.33 (3.1R) | 35 minutes left; range 3835–3842 | close 3837.10 | −12 bps | −130 |
| SHREECEM | 10:25 at 23255, stop 23142 (48 bps) | PDH 23265 (0.09R), R1 0.6R | through PDH by 15 points at 10:30, faded for an hour | stop 11:35 | −48 bps, −1R | −215 |
| AMBUJACEM (skipped) | 14:35 at 360.30 | cap: moved 0.80 ADR | fell to 354.70 by the close without hitting the stop | — | would be −155 bps | +390 avoided |

## Why the losses

* **LT (−₹130):** ₹27 of it is the trade (−12 bps), ₹103 is charges. The entry came at 14:50 for a wall 98 bps away
  with seven bars left in the session; the rule has no clock. The stock had gapped +3.7 % the previous day and was
  resting at the previous day's levels.
* **SHREECEM (−₹215):** the entry close 23255 sat 4 bps under PDH 23265, 60 bps above the retest low, on the third
  up bar after the retest; R1 was 0.6R away. It poked 15 points through PDH and bled to the stop. The stock lagged
  NIFTY from the open (−0.45 % relative) and closed red on a +1 % index day. On the development window, though,
  entries with the nearest level < 0.3R are the *better* half (−8.8 vs −11.6 net); this loss is not a rule.
* **JSWSTEEL (+₹11 from +46 bps):** gross ₹114 at ₹25,000, charges ₹103. At ₹1,00,000 the same trade nets ₹326.
  Charges are ₹103 / ₹112 / ₹130 / ₹165 a round trip at ₹25k / ₹50k / ₹1L / ₹2L (41 / 22 / 13 / 8 bps); today's
  six trades net ₹540 / ₹1,647 / ₹3,860 / ₹8,287 at those sizes. The research's 15 bps assumes about ₹1L.

Also seen: TATAPOWER and JSWSTEEL both missed the stop by one or two ticks before paying (MAE −49 of 51 bps, −45 of
52). ETERNAL gave back 150 bps from its high under the trail (fixed 1:2 would have booked 337.67).

## Loop 2 candidates on the development window (`2026-10-29-prereg-loop2.md`; loop-1 baseline n 2,497, −10.6 net, t −5.34)

| candidate | kept | dropped / changed |
|---|---|---|
| cutoff 14:00 | n 2,238, −10.2, t −4.91 | n 259, −14.1, win 33 % |
| **cutoff 14:30** | n 2,332, −10.2, t −4.97 | n 165, **−17.4, win 26 %, 63 % close exits** |
| cutoff 15:00 | n 2,421, −10.3, t −5.16 | n 76, −22.4, win 14 %, 92 % close exits |
| skip room < 0.15R | n 2,078, −10.5 | n 419, −11.4 |
| skip room < 0.3R | n 1,652, −11.6 (worse) | n 845, −8.8 |
| skip room < 0.5R | n 1,153, −12.5 (worse) | n 1,344, −9.1 |
| stop at retest − 1.5 tol | n 2,497, −9.9, t −4.87 | median risk 63 → 94 bps |
| stop at retest − 2.0 tol | n 2,497, −9.9, t −4.53; 2024 −8.8, 2025 −11.2 | median risk 63 → 125 bps; 2025 worse than the baseline (−9.7) |
| cutoff 14:30 + stop 2.0 | n 2,332, −9.5, t −4.22; 2024 −8.3, 2025 −10.8 | |

By year the baseline is 2024 −11.5 / 2025 −9.7; the cutoff improves both (−11.1 / −9.2); the wider stop helps 2024
and hurts 2025.

## Decision (committed 22:24 IST)

Nothing reaches the pre-registered bar (≥ 3 bps net and ≥ 0.3 t): the cutoff is +0.4 bps / +0.37 t, the wider stop
+0.7 / +0.81 but inconsistent across years, the room skip negative. **Nothing ships by the rule.** Recorded for the
owner's call: the 14:30 cutoff is hygiene rather than edge — it removes the 7 % of entries that cannot reach their
wall before the close (win 26 %, 63 % close exits, LT today), improves both years, and costs nothing else; it would
ship as a setting (`KN_STRATEGY_ENTRY_CUTOFF=14:30`, signals after it recorded as skipped) if the owner wants it.
The stop width and the room filter stay as they are. The intraday rule is still −10 net on history after loop 2; the
charges at ₹25k (41 bps) are the larger lever than any of these.

## Shipped (22:38 IST, 657567b, owner's call; engine restarted 22:41)

`KN_STRATEGY_ENTRY_CUTOFF=14:30` (`Settings.strategy_entry_cutoff`; a signal whose entry bar is labelled at or after
it is recorded with `why`, not taken; `""` switches it off; the snapshot carries `entry_cutoff`) and
`KN_STRATEGY_NOTIONAL_INR=100000` so the shadow book's net is read at the research's cost level (13 bps a round trip
at ₹1L against 41 at ₹25k). Both are below the pre-registered bar and are recorded as hygiene and reporting, not
edge. Day 4 runs them.
