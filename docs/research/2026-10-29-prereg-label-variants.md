# Pre-registration: the entry and skip rules read out of the 50 chart labels

Written 2026-10-29 01:10 IST, after the two October days were looked at (below) and **before** the development-window
result was read. Owner's ask (29 Oct, 01:00): "that claude learn from my 20 can we take those 30 run and improve our algo
and lets test for today and yesterday".

## Source

The 50 calibration charts' labels (`scripts/2026-10-28-chart-labels/labels-2026-10-29.json`): 20 by the owner, 30 by a
Claude.ai chat grading "as Shiv would". What the reasons say, as rules a machine can apply:

1. **Enter when price leaves the zone, not on the pullback after.** "candle cross CPR should our entry", "take trade when
   price leave pivot", "after 3 times rejected price now able to cross so it will be our breakout trade", "entry when red
   closes back below BC". → `brk_bar`: entry at the close of the first bar through the CPR edge (the sequence's through
   bar), stop that bar's far side ∓ tol; `brk_cpr`: same entry, stop the far CPR edge ∓ tol ("SL below BC").
2. **Rejections first.** "3 candles showing rejection", "after 2 rejection again price start rising". → the count of
   distinct visits to the edge (touch within a tolerance, no close through, re-armed a tolerance away) between the open
   test and the through bar; precondition `rej_n ≥ 2`.
3. **Room.** "these risk worth", every chat skip with "0.5R to S1". → first wall ≥ 1R from the entry.
4. **Trend already gone.** "momentum came in first few candles", "price reached S1 by 09:55". → wall 1 touched between
   the through bar and the entry → skip.
5. **No big entry bar.** "the 10:10 candle already ran 0.8", "buying its close needs a 1.5% stop". → entry bar range ≤ 2 tol.
6. **A real impulse before the retest** ("a pause after an impulse, not inside it"). → excursion beyond the edge before
   the retest ≥ 2 tol; and the retest ≥ 2 bars after the through bar.
7. **Time.** "15:25 — market is closing", "square off by 15:15". → entry bar at or before 14:30.
8. **Fill at the retest bar's extreme, not the next close** (chat c20/c29/c36). → `stop_in`: a stop order at the retest
   bar's high/low, filled in the first later bar that crosses it (assumed filled at the level: optimistic).
9. **The IB break** ("IBH (15m first candle) or 1st 5 minute candle if low break take entry", four times in the owner's
   20). → first 5m close beyond the first-15-minute high/low from the fourth bar on, stop the other IB edge ∓ tol, every
   symbol-day (not only sequence days).

All exits `wall_then_trail` to the session's last bar (the live rule). Net = gross − 15 bps. Instances: every symbol-day
of the 5m cache (214 stocks, indices out) in the development window 2024-01-01 → 2025-09-30 where the baseline sequence
confirmed; the baseline is re-scored by the same harness so the comparison is like for like.

## Decision rule (before reading)

- A variant **replaces** the live entry only if its net mean is positive **and** t by day ≥ 2.5 (about 15 comparisons
  here, so 2.0 is not enough) **and** it beats the baseline on the same instances.
- A skip filter is **kept** only if the kept set's net mean beats the baseline by ≥ 5 bps with t by day ≥ 2.5 on the
  kept set, and the excluded set is worse than the kept set.
- IB15 is a separate strategy: ship to shadow only if net > 0 with t ≥ 2.5 over all symbol-days.
- Anything that passes goes to SHADOW beside the current rule (tagged, not replacing it) from 2026-10-29; the holdout
  is spent, so forward shadow is the only out-of-sample left.
- Expected (from the 3,101-instance result and every filter tried on 2026-10-27): the entry variants shift the
  distribution by a few bps and no filter clears the bar.

## The two October days (looked at first; 11 instances, so a picture, not evidence)

27 Oct (feed 1m archive built into 5m) and 28 Oct (exchange 5m from the cache), official previous close:
base (live entry) +5.8 gross / −9.2 net on 11; `brk_bar` +24.2 / +9.2; `brk_cpr` +30.8 / +15.8; `stop_in` +29.4 / +14.4;
the through-bar entry beat the base on 9 of 11. Filters: `rej_n ≥ 2` kept 8 at +20.7 gross, excluded 3 at −34.0;
impulse ≥ 2 tol kept 4 at +26.9. IB15: 413 symbol-days, +7.3 gross / −7.7 net; long +21.8, short −26.7 (two up-days).

## Run log

- 01:12 IST: harness `scripts/2026-10-29-label-variants/variants.py` on the development window, output pending.
- 01:05 IST, first pass (wrong, kept for the record): the through-bar entries were scored only on the 3,101 sides that
  later confirmed — `brk_bar` +25.4 gross / +10.4 net, t 5.7; `brk_cpr` +27.2 / +12.2, t 7.0. That is look-ahead: at the
  through bar nobody knows the retest will hold and the confirmation will come; the subset is the one where the trade
  survived to the confirmation bar, so the entry sits on the confirmation bar's gain by construction. Caught before it
  was reported. The same trap sat in `stop_in` (scored on confirmed fills only: +16.4 gross).
- 01:10 IST, honest pass — every entry scored on every bar where it is known (7,138 sides reached the through bar;
  6,409 held retests; 4,061 stop-order fills), 429 days:

  | entry / rule | n | gross bps | net bps | t by day |
  |---|---|---|---|---|
  | base: live entry on the confirmation bar | 3,101 | +3.2 | −11.8 | −7.6 |
  | through-bar close, stop at the bar | 7,137 | +3.1 | −11.9 | −10.9 |
  | through-bar close, stop at the far CPR edge | 7,137 | +3.9 | −11.1 | −9.0 |
  | through bar, rejections ≥ 2 | 3,793 | +4.3 | −10.7 | −7.1 |
  | through bar, rejections ≥ 2, room ≥ 1R, before 14:30, bar ≤ 2 tol | 1,037 | +5.7 | −9.3 | −2.2 |
  | stop order at the retest extreme, every fill | 4,060 | +5.3 | −9.7 | −7.6 |
  | IB15 break, every symbol-day | 82,599 | +4.4 | −10.6 | −9.2 |
  | IB15 break, room ≥ 1R, before 14:30 | 6,101 | +6.4 | −8.6 | −3.5 |

  Skip filters on the live entry (kept set): room ≥ 1R −6.0 net (753); before 14:30 −11.0; wall 1 untouched −11.7;
  bar ≤ 2 tol −11.0; impulse ≥ 2 tol −13.7; retest ≥ 2 bars after −12.7; rejections ≥ 2 −10.2 (1,730); all five
  together +0.1 net on 103. Nothing clears the bar; nothing beats the baseline by 5 bps with t ≥ 2.5.
  Where the through-bar entry's money is: the 619 sides that never came back to the edge made +61.9 gross (the runaway
  days), the 3,307 that came back and held lost −27.0, the 110 that failed −49.0, the 3,102 that confirmed +25.5. At the
  through bar these are one population: +3.1. The owner's eye may separate them; the arithmetic in his words does not.
- October 27+28 on the honest scoring: through-bar entry on all 25 sides −2.9 gross / −17.9 net; stop-order fills 13 at
  +24.6 / +9.6; base 11 at +5.8 / −9.2. Two days, a picture.
- **Verdict:** no variant and no filter replaces the live entry; nothing ships. The live rule stays the only strategy
  in SHADOW. The one thing in the labels this harness cannot encode — the owner's visual judgment of a proper double
  bottom and a mature retest — is what the chart-label gate tests once his 50 are in.
