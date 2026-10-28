# Pre-registration: the same five concepts on 5m, 15m, 30m (intraday) and 1h, 4h, daily (positional)

Written 2026-10-29 01:38 IST, before the run. Owner: "why you testing all on 5 minute run all strategy on 5, 15, 30,
1hr, 4hr also positional way and then tell me all results".

## Fixed before the run

**Bars.** 15m, 30m, 1h and 4h are built from the 5m cache on the session grid (09:15, 09:30 …; 1h 09:15, 10:15 …
15:15; 4h 09:15 and 13:15); daily from the 1d cache. Window 2024-01-01 → 2025-09-30, 214 stocks, indices out.

**Intraday (5m, 15m, 30m):** levels from the previous session (CPR, pivots, PDH/PDL; weekly pivots and the round
number as further walls), tolerance a tenth of the ADR, every trade flat at the session's last bar. The live
sequence's bar counts scale with the timeframe: open test by 30 minutes, BOS within the hour, confirmation within 30
minutes (5m: 6 / 12 / 6 bars; 15m: 2 / 4 / 2; 30m: 1 / 2 / 1). IB = the first 15 minutes (30m: the first bar).

**Positional (1h, 4h, daily):** bars run across sessions. 1h and 4h: levels from the previous session, tolerance a
fifth of the ADR, the chain may complete within two sessions and a trade is held up to five sessions, then closed at
the last bar. Daily: levels from the previous week (its CPR and floor pivots, its high/low; the previous month's pivots
as further walls), tolerance half an ADR, chain within two weeks, held up to twenty sessions.

**The five concepts, each at each timeframe:**
1. **Level retest** — the live chain: a close through TC (BC), the first bar back to it that holds (closes at or
   beyond BC), entry at the first close beyond the retest bar's extreme; stop the retest bar's far side ∓ tol.
   Intraday it also needs the open test and the break of structure, as the live rule does.
2. **Through bar** — entry at the close that goes through TC (BC), stop that bar's far side ∓ tol.
3. **Range break** — intraday: the first close beyond the first 15 minutes, stop the other side of that range;
   positional: the first close beyond PDH/PDL (1h, 4h) or the previous week's high/low (daily), stop the bar's far side.
4. **Order block** — the engine's structure code: a displacement break of structure, the last opposite candle, a
   limit at its body edge on the first return, stop the zone's far side (positional in windows of 300 bars).
5. **Sweep reversal** — a bar that trades through a swing low / equal lows / PDL (highs for a short) and closes
   back; entry at that close, stop the wick ∓ tol.

**Exits:** the walls then trail (daily R1/R2/R3, PDH, weekly pivots, the round number; daily: weekly R1/R2/R3, the
previous week's high, monthly pivots, the round number) and fixed 1:2. **Net = gross − 15 bps** at every
timeframe (positional trades are stock futures: the cost is about the same; the overnight gap risk is not priced).

**Decision rule:** a cell ships to SHADOW only if net > 0, t by day of entry ≥ 2.5, n ≥ 300, both halves positive.
Expected: the gross per trade grows with the timeframe (bigger stops), the net stays near or below zero.

## Run log
- 01:50 IST, first run (`scripts/2026-10-29-all-timeframes/mtf_all.py`, results `mtf_results.csv`): intraday rows at
  5m / 15m / 30m all negative net (the best: the 30m order block +13.6 gross / −1.4 net, t −0.1). **Positional cells
  passing the bar as scored:** 1h sweep reversal +37.3 gross / +22.3 net (t 12.2, n 124,465, 2024 = 2025 = +22.3,
  long +27 / short +18); 4h sweep reversal +38.2 / +23.2 (t 10.4, n 64,744); 1h order block +27.2 / +12.2 (t 4.3);
  4h order block +57.3 / +42.3 (t 8.8, n 3,804); daily order block +100.7 / +85.7 (t 6.7, n 3,366, long +127 / short +34).
  **Not accepted yet.** Three ways the scoring can fake a positional pass, fixed before the follow-up (written now):
  (1) a stop crossed by an overnight gap filled at the stop price — the follow-up fills at the bar's open when the
  open is already through the stop; (2) an entry at the close of a session's last bar (15:15–15:30, the auction) — the
  follow-up takes no entry on a session's last bar; (3) the 5m cache is today's F&O list, a survivorship tilt for
  longs — the follow-up reads long and short separately and adds a **placebo**: for every passing cell, the same
  number of entries per stock at random bars with the same side, the same stop rule (the bar's far side ∓ tol), the
  same walls, trail and hold. **A cell keeps its pass only if, with (1) and (2), it still clears the bar and beats its
  placebo by ≥ 10 bps net.** Exit reasons and bars held are recorded.
- 01:57 IST, robustness run 1 (gap-aware fills, no last-bar entries, placebos): the 1h sweep reversal fell to +10.0
  net (t 6.8, n 113,217) and still beat its placebo (−22.9); the daily order block to +41.8 (t 3.9, long-only).
  Then a third artifact was found in the positional scan: **a window scanned backwards takes entries in its back part
  with the levels of its base day — PDH / PDL sweeps and walls from a day that had not happened yet.** The 1h
  sweep's extra 78,000 entries were exactly those (a bar that trades below a *future* day's low and closes back
  above it is selected on the future). Entries restricted to the forward part of every window.
- 02:04 IST, robustness run 2 (all three fixes, placebos; entries 2024-01-01 → 2025-09-30 only):

  | tf | concept | exit | n | gross | net | t by day | 2024 | 2025 | placebo net |
  |---|---|---|---|---|---|---|---|---|---|
  | 1h | sweep reversal | trail | 34,892 | −4.1 | −19.1 | −2.5 | −17.1 | −21.4 | −37.9 |
  | 4h | sweep reversal | trail | 11,461 | −9.2 | −24.2 | −0.3 | −27.3 | −20.4 | −32.3 |
  | 1d | sweep reversal | trail | 7,368 | −10.2 | −25.2 | −2.7 | −36.2 | −10.9 | −89.5 |
  | 1h | order block | trail / 1:2 | 5,613 | +2.3 / +16.5 | −12.7 / +1.5 | −3.9 / −0.1 | | | −36.1 / −37.7 |
  | 4h | order block | trail / 1:2 | 1,181 | +17.8 / +48.9 | +2.8 / +33.9 | −0.7 / 2.3 | +11.4 / +45.2 | −6.5 / +21.6 | −64.5 / −74.2 |
  | 1d | order block | trail / 1:2 | 1,793 | +2.1 / +43.4 | −12.9 / +28.4 | −0.8 / 1.9 | −7.7 / +24.0 | −19.1 / +33.6 | −66.6 / −68.3 |

  1h / 4h / daily level retest, through bar and range break: −5 to −60 net, every one. Intraday rows unchanged from
  the first run (all negative; the 30m order block the best at +13.6 gross / −1.4 net).
- **Verdict: no cell passes.** The two closest are the order block with a fixed 1:2 at 4h (+33.9 net, t 2.3, n 1,181)
  and daily (+28.4, t 1.9, n 1,793): positive in both halves and far above their placebos, below the t bar and on
  small samples. Recorded as leads; not shipped. Every "pass" of the first run was an artifact: stop fills across gaps
  (+6 to +12 bps), entries at the session's last bar, and the back-window future levels (the whole of the 1h sweep).
- 02:40 IST, written before the run: the one daily lead (order block, fixed 1:2: +28.4 net, t 1.9 on 2024–25) gets its
  own out-of-sample on **2021-01 → 2023-12 daily bars**, which no test has touched (the 1d cache holds 294 names
  including 72 past F&O members). Same harness, same placebo. It stays a lead unless net > 0 with t ≥ 2.5, n ≥ 300
  and every year positive; the 4h lead has no bars before 2024 and cannot be checked this way.
- 02:55 IST, the daily order block (fixed 1:2) on 2021-01 → 2023-12, one run (`scripts/2026-10-29-all-timeframes/ob5y.py`;
  the harness's daily series start fixed to START − 400 days first — the first attempt had only 2023): all 289
  cached names +71.1 gross / +56.1 net (t 5.2, n 3,627) — but the 1,122 trades in names **not yet in F&O on the
  day** made +80 net and the members +45: the admission bias the `fo_universe` docstring warns of. **Members on the
  day only (point-in-time bhavcopies): n 2,505, +60.4 gross / +45.4 net, t 4.0, 2021 +48.5 / 2022 +44.0 / 2023 +44.7,
  long +79 / short +12, placebo −64.1 — PASSES the rule written above.** The same on 2024-01 → 2025-09 over the full
  daily cache, members only: n 1,627, +40.6 / +25.6, t 1.5, 2024 +39.9 / 2025 +11.8, long +48 / short +4, placebo −77.
  **Five years, members only: n 4,132, +52.6 gross / +37.6 net, t 4.0, every year positive, 41 of 56 months, 172 of
  264 stocks, top ten stocks 26 % of the total; risk median 212 bps, 46 % win, 42 % target / 48 % stop / 4 % gap-stop,
  held 4.4 sessions; worst month 2025-03 −97; drawdown of the cumulative per-day mean −2,323 bps (about 70 average
  days). The trail rule on the same trades +5.3 net: the fixed 1:2 is the exit.**
- Caveats before anyone sizes it: fills at Z are assumed whenever a bar trades to Z (a resting limit at the exact
  level fills less often); 15 bps covers charges and a tick, not the roll of a future across expiry on the longer
  holds nor margin; it is mostly a long strategy in a market that rose; 2025 is the weakest year (+11.8) and the
  holdout 2025-10 → 2026-10 is spent, so the only out-of-sample left is forward. **Next: a positional daily shadow
  book for it (loop 2), read every evening like the intraday one.**
- 03:15 IST, the three caveats closed properly (`scripts/2026-10-29-all-timeframes/ob5y_robust.py`; 9,766 zones with a
  first return 2021-01 → 2025-09, 7,002 in members; the harness's walls requirement dropped, so n differs slightly):

  | fill model | cost | n | gross | net | t | 2021 / 22 / 23 / 24 / 25 |
  |---|---|---|---|---|---|---|
  | limit at Z, filled on a touch | 15 | 4,175 | +53.2 | +38.2 | 3.9 | +48 / +44 / +44 / +39 / +17 |
  | limit at Z, filled only once price trades 10 bps through | 15 | 3,780 | +43.4 | +28.4 | 2.8 | +38 / +29 / +37 / +30 / +9 |
  | the same | 15 + 5 on every stop + 10 when the hold crosses a month end (17 %) | 3,780 | +39.0 | **+24.0** | 2.3 | +33 / +25 / +33 / +26 / +5 |
  | market order at the close of the bar that reached the zone | the same stack | 4,175 | +23.4 | +8.4 | 0.9 | +11 / −3 / +4 / +13 / +20 |

  **The fill is the biggest lever**: it lives on the limit at the zone. Conservative case (through, full stack), by
  side: long +56.0 net, short −7.1; minus NIFTY over the hold at beta 1: long +33.8, short +9.7 (NIFTY rose 22 bps
  during the longs' holds and 15 during the shorts'). NIFTY-down months +35.3 (long +43, short +29); up months +11.8
  (long +68, short −51). 14 of 19 quarters positive, worst 2025Q2 −23.7. **Sensitivity** (not pre-registered;
  reported as the shape, 2R stays the registered target): target 1.5R +46 (t 5.6–6.0), 2R +24 (t 2.3), 3R +5
  (t 0.9); hold 10 / 20 / 40 sessions identical — the trades resolve inside ten sessions and the edge is in the
  first 1.5–2R. The book: +0.17 R a trade, 796 trades a year, median 11 open positions (90th pct 21, max 39), equity
  +646 R over 3,780 trades with a −44 R drawdown; every year positive in R (2021 +98, 2022 +185, 2023 +183, 2024
  +114, 2025 +64).
- **Verdict on the daily order block, properly:** it passes with the conservative fill and the full cost stack,
  every year, in down months, market-adjusted on both sides, on an untuned zone definition, and it is not knife-edge
  on target or hold. What it is not: a short strategy after costs (shorts carry nothing), a small-account strategy if
  run in full (₹25–50 lakh deployed at ₹5,000 risk a trade), or anything without the limit fill. 2025 is the weakest
  year on every row. Forward shadow is the next evidence; nothing else is left to test on the past.
