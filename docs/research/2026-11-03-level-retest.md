# The held retest of any wall, with the day's move — results (3 Nov 2026, 02:05–02:15 IST)

Pre-registration: `2026-11-03-prereg-level-retest.md` (written 02:02, before the run). Script and the run's summary:
`scripts/2026-11-03-level-retest/` (`level_retest.py`, `summarize.py`, `dev-summary.txt`, `dev.log`; the 90 MB of
per-trade parquet stays out of the repo). Development window 2024-01-01 → 2025-09-30, 5m, 212 names, 433 days; the
holdout untouched. Net = gross − 15 bps.

## Verdict
**The generalized retest lands where every written-down entry has landed: +4.2 bps gross, −10.8 net, 56 % wins,
day-t −12.9, on 98,259 first-flips-with-the-move.** Both years the same (+4.0 / +4.4 gross). Every wall type +3.3 to
+5.6 gross: CPR +3.5, pivots +3.8, PDH/PDL +3.4, weekly +5.6, opening range +3.3, round numbers +4.4; confluence
+4.1 vs single wall +4.6. Waiting for the pullback adds **+0.8 bps** over buying the break bar's close (control +3.4
gross, 53 % wins): the retest makes the entry a little cleaner, not profitable. Shorts +5.5, longs +2.5 (the window
drifted down). Gap-and-go longs (gap ≥ +2 %, with the move): +3.1 gross on 3,492, 62 % wins and small. Gap-down
shorts: +23.2 gross on 665, day-t −1.5 (a few days carry it; not a result). Wider stops look better (risk > 120 bps:
+7.8 gross) because a wide stop is a trend day that already moved — the fee is the same, the risk doubles.
**Shipped anyway (owner, 02:10: "no i want it to ship properly and check it tomorrow"; 02:46 restart, `strategy/level_retest.py`, `KN_STRATEGY_SEQUENCE=level`, round walls on, no ADR cap, with a shadow option leg per trade).** The book is to be read against +4.2 gross / −10.8 net; Monday replayed through the runner: 224 trades, +18.1 bps, +₹10,944 at ₹1L. The owner's concept is not wrong as a description of how a trend day
looks; as a rule it has no expectancy after cost, exactly like the CPR version (rows 15–17).

## The dev-window tables (from `dev-summary.txt`)
```
retest rows 1,570,414: held 1,470,551 (94 %), confirmed within 6 bars and before 14:30 650,728
stock-days with a confirmed flip 86,682; symbols 212; days 433

## Primary: with the day's move, the first confirmed flip per stock-day-side
| set | n | gross bps | net bps | win | median risk bps | day-t (net) |
|---|---|---|---|---|---|---|
| primary | 98,259 | +4.2 | -10.8 | 56.0 % | 71 | -12.85 |
| primary 2024 | 54,346 | +4.0 | -11.0 | 55.9 % | 72 | -10.16 |
| primary 2025 | 43,913 | +4.4 | -10.6 | 56.3 % | 70 | -7.90 |
| primary short | 54,166 | +5.5 | -9.5 | 56.9 % | 70 | -10.16 |
| primary long | 44,093 | +2.5 | -12.5 | 54.9 % | 73 | -15.15 |
| against the move (first per stock-day-side) | 78,655 | +2.5 | -12.5 | 51.8 % | 69 | -17.45 |
| every confirmed flip | 650,728 | +3.1 | -11.9 | 54.1 % | 71 | -23.70 |

## Primary by wall type
| set | n | gross bps | net bps | win | median risk bps | day-t (net) |
|---|---|---|---|---|---|---|
| cpr | 5,908 | +3.5 | -11.5 | 58.8 % | 70 | -7.57 |
| or | 10,346 | +3.3 | -11.7 | 54.5 % | 61 | -12.32 |
| pd | 7,370 | +3.4 | -11.6 | 55.9 % | 69 | -8.56 |
| pivot | 13,677 | +3.8 | -11.2 | 56.9 % | 68 | -11.04 |
| round | 55,659 | +4.4 | -10.6 | 55.9 % | 75 | -11.15 |
| weekly | 5,299 | +5.6 | -9.4 | 55.2 % | 69 | -6.70 |
| confluence (another wall within tol) | 82,704 | +4.1 | -10.9 | 55.8 % | 71 | -13.42 |
| single wall | 15,555 | +4.6 | -10.4 | 57.3 % | 73 | -9.27 |

## Primary by entry clock
| set | n | gross bps | net bps | win | median risk bps | day-t (net) |
|---|---|---|---|---|---|---|
| ≤10:15 | 53,811 | +3.9 | -11.1 | 57.3 % | 81 | -11.11 |
| 10:20–11:15 | 20,585 | +5.8 | -9.2 | 55.2 % | 65 | -9.52 |
| 11:20–12:15 | 10,404 | +3.3 | -11.7 | 54.1 % | 59 | -11.74 |
| 12:20–13:15 | 6,919 | +4.6 | -10.4 | 54.5 % | 58 | -10.54 |
| 13:20–14:30 | 6,540 | +2.3 | -12.7 | 52.9 % | 58 | -13.59 |

## Gap-and-go (gap ≥ +2 %, longs with the move) and its mirror
| set | n | gross bps | net bps | win | median risk bps | day-t (net) |
|---|---|---|---|---|---|---|
| gap ≥ +2 %, long | 3,492 | +3.1 | -11.9 | 62.3 % | 73 | -8.96 |
| gap ≤ −2 %, short | 665 | +23.2 | +8.2 | 61.1 % | 103 | -1.48 |
| |gap| < 1 % | 77,077 | +3.6 | -11.4 | 56.0 % | 69 | -13.75 |

## Exits (primary)
{'trail': 39.9, 'stop': 36.5, 'close': 20.5, 'target': 3.2}
first wall: {'ROUND': 33.2, 'S1': 13.4, 'R1': 10.0, 'PDL': 7.7, 'WP': 7.4, 'R2': 6.2, 'S2': 6.2, 'PDH': 4.6}
median first-wall distance bps 52.6 median bars held 15.0

## Control: enter at the break bar's close (no retest), first break per stock-day-side with the move
| set | n | gross bps | net bps | win | median risk bps | day-t (net) |
|---|---|---|---|---|---|---|
| control, with the move | 129,276 | +3.4 | -11.6 | 53.4 % | 68 | -14.09 |
| control 2024 | 72,109 | +2.9 | -12.1 | 53.4 % | 69 | -11.34 |
| control 2025 | 57,167 | +3.9 | -11.1 | 53.5 % | 67 | -8.48 |
| control cpr | 10,095 | +3.7 | -11.3 | 56.8 % | 69 | -7.70 |
| control or | 9,957 | +0.8 | -14.2 | 51.4 % | 57 | -13.01 |
| control pd | 10,243 | +3.4 | -11.6 | 52.9 % | 67 | -8.23 |
| control pivot | 18,725 | +4.6 | -10.4 | 54.6 % | 67 | -9.94 |
| control round | 73,728 | +3.3 | -11.7 | 53.1 % | 71 | -12.84 |
| control weekly | 6,528 | +3.8 | -11.2 | 52.5 % | 65 | -7.39 |

## Primary by risk (stop distance)
| set | n | gross bps | net bps | win | median risk bps | day-t (net) |
|---|---|---|---|---|---|---|
| risk ≤30 | 1,296 | +0.9 | -14.1 | 48.6 % | 27 | -8.92 |
| risk 30–50 | 17,634 | +2.1 | -12.9 | 52.6 % | 43 | -21.34 |
| risk 50–80 | 40,130 | +3.1 | -11.9 | 55.1 % | 64 | -17.27 |
| risk 80–120 | 25,941 | +5.5 | -9.5 | 58.0 % | 95 | -9.95 |
| risk >120 | 13,258 | +7.8 | -7.2 | 60.3 % | 147 | -5.68 |

## Per-year, long vs short (primary)
| set | n | gross bps | net bps | win | median risk bps | day-t (net) |
|---|---|---|---|---|---|---|
| 2024 short | 30,227 | +5.6 | -9.4 | 56.8 % | 71 | -7.36 |
| 2024 long | 24,119 | +1.9 | -13.1 | 54.6 % | 74 | -11.64 |
| 2025 short | 23,939 | +5.4 | -9.6 | 57.1 % | 69 | -7.00 |
| 2025 long | 19,974 | +3.3 | -11.7 | 55.3 % | 72 | -9.70 |
```

## The same rule on last week's live bars (demonstration, not evidence)
`archive_trades.parquet` (scratch): 27, 29, 30 Oct and 2 Nov (28 Oct lost to sleep). Primary set (with the move, first
flip per stock-day-side): 968 trades, +5.2 gross, −9.8 net, 56 % wins — the dev window's number. By day: Tue 27 −1.4,
Thu 29 +0.5, Fri 30 +3.8, **Mon 2 Nov +20.3 gross on 216 (64 % wins)** — the broad up day (31 names > +3 % from the
open, 3 below −3 %) lifted every long; longs +13.2 vs shorts −5.8 across the four days. By wall: CPR +18.9 (51),
pivots +15.0 (130), weekly +13.6 (44), round +3.0 (538), opening range −1.3 (137), PDH/PDL +1.0 (68) — four days.

**RELIANCE, Mon 2 Nov** (Friday close 1164.9, open 1235.0 = +6.0 %, 3.5 % above R3, close 1274.6 = +9.4 %; the future
1286 at 15:29). The owner's wall was the opening-range high 1237.3: broken at 09:35 (close 1240.5). The rule's first
pullback was the very next bar (09:40, low 1238.0, held), whose high 1244.0 was not cleared within six bars (09:45–
10:10 closed 1234.8–1239.5), so no entry; the deeper pullback at 10:00 closed 1234.8, below the wall's break line, and
the re-break at 10:10 was followed by a 10:15 close of 1235.5, 0.7 under the hold line — failed. What did fire were two
round-number flips: long at 10:50 at 1257.6 (wall R1250 held) and 13:55 at 1255.6, each exited at the **only wall
above, the next round number** (+19.1 and +35.0 bps, "target"), plus a short at 12:50 (−43.3, stop): +10.8 bps net
of three trades on a day the stock made +320 bps from the open. The entry found the pullback; the exit sold the first
round number because a stock above every pivot has no other wall. (Six other trail rules were tested on the CPR
retests, row 16, all 2–4 gross; a trend-day exit for a stock with no walls above was not part of this pre-registration.)

## H2, the board (description only — the ten-session rule holds)
Mon 2 Nov, `archive/fno`: 132 of 224 roots showed the future's state turn short covering → long build-up in session,
112 of them by 10:30 (most in the first ten minutes, when the labels flip on the first OI frames). From the turn's
minute to the close: mean +0.49 %, 62 % positive; with the puts in short build-up at that minute (60 names): +0.40 %,
58 %. The names without a turn closed +0.77 % from the open on average, 65 % positive — on this day the turn was the
market's drift, not a selection. RELIANCE's turn came at 09:22 (then +4.9 % to the close); TCS (09:52) and CDSL
(09:38), the day's two shadow shorts, turned too and then fell 2.1 % and 2.3 %. One day; the turn needs a robust
definition (sustained ≥ 15 min, OI up ≥ 0.5 % from the open) before the day-10 pre-registration.

## The option leg on the archive week (3 Nov 02:50–03:04 IST, `scripts/2026-11-03-level-retest/option_leg_replay.py`, log `option-leg-week.txt`)
Owner (02:25): "we want get trigger on equity and buy its option … backtest on week or today how much option data we have."
Data: the feed's tick archive holds every subscribed F&O contract's frames (ltp, bid, ask) for 14 days since 19 Oct; the
full sessions with bars, ticks and the board are 27, 29, 30 Oct and 2 Nov (28 Oct lost to sleep; 19–26 Oct partial). On
2 Nov 3,049 F&O contracts ticked in session, median 199 frames a contract, 985 with one a minute, bid and ask on every
frame — enough to price an ATM leg at the signal's minute (entry quote age median 13–26 s, 5–16 % of entries priced off a
stale last trade).
Method: each day replayed through the runner in level mode (the book that runs from 3 Nov), then one lot of the nearest
ATM option of the nearest expiry after the day (the 23 Nov series on all four days), bought at the first ask at or after
the entry, sold at the last bid at or before the stock's exit, charges from the cost model.

| day | stock trades | stock mean bps / net ₹1L | option legs | option mean % | win | sum net ₹ (one lot each) | premium ₹ |
|---|---|---|---|---|---|---|---|
| Tue 27 Oct | 275 | 0.0 / −35,960 | 268 | −2.3 | 32 % | −180,570 | 5.9 M |
| Thu 29 Oct | 239 | +4.4 / −19,690 | 237 | −0.9 | 37 % | −67,011 | 4.8 M |
| Fri 30 Oct | 249 | +4.1 / −21,868 | 246 | −1.1 | 39 % | −75,657 | 4.7 M |
| Mon 2 Nov | 228 | +21.4 / +18,529 | 226 | +2.4 | 48 % | +60,288 | 4.2 M |
| pooled | 991 | ≈ −6 net | 977 | −0.6 (median −1.6) | 39 % | −262,949 | 19.7 M (−1.3 % of premium) |

Longs +0.5 % (567 legs), shorts −2.0 % (410). By the stock's exit: stops −11.2 % on 345 legs, trails +5.1 % on 423,
close +3.6 % on 189, target +24.9 % on 20. **Realised leverage is asymmetric: the option moves 10.3 % per 1 % of the stock
on winners and 17.6 % on losers** (median 14.4) — a stop is fast (the spread and the ask-in/bid-out round trip are paid
in full), a trail is slow (theta and the bid). The option leg loses on the same trades the stock leg loses on, times the
leverage, plus the spread; it turns a −6 bps book into −1.3 % of premium a trade. Monday, the broad up day, was the only
positive day for both legs. Nothing here changes the rule; the live leg records the same thing with real quote ages from
3 Nov, and the owner's confirmation (the board's SC → LB turn) is the open question for the day-10 pre-registration.

## What made Tue 27 Oct lose and Mon 2 Nov win (3 Nov 03:05–03:10, `scripts/2026-11-03-level-retest/attribution.py`, tables in `attribution-week.txt`)
Owner: "find out what make us loss and what make us profit act as quant developer". Four replayed days, 991 trades, 977
option legs; every split below is a description on four days (day-clustered), not a test.
**The two days were the same NIFTY (+0.45 % / +0.46 % open to close) and a different breadth path.** 27 Oct: 78 % of the
universe above its open at 09:30, 77 % at 12:15, 60 % at 14:15 — a broad morning that faded; 133 entries by 09:45 (mostly
longs on the opening range and round numbers) were trailed out at a fraction of their excursion or stopped (108 stops, 39 %);
entries after 12:15 −11.3 on 43; the 94 shorts −6.0; PDH/PDL walls −22 on 40 (expiry day, the board blind). 2 Nov: 68–75 %
above the open all day and 68 % at 15:10 — the breadth held, so the same early longs trailed to +50 and closed at +45
(52 closes, 10 targets at +196); even the 79 shorts made +13.8 (the few real decliners); wide stops (> 120 bps, the
gap-and-go names) +57.5. **The rule's P&L is breadth persistence; the entry chooses the stock, the market chooses the day.**
**Across the four days, the structure that repeats:** 35 % stops at −67 bps, 43 % trails at +44, 19 % closes at +45, 2 %
targets — ≈ +7 bps gross before the 13–15 bps round trip, the dev window's number. Only 24 % of stops had been +0.5R before
stopping (4 % +1R): stops are trades that never worked, not give-backs. Trails keep a third of their excursion (median
give-back 44 bps, 66 % of the MFE): the wall-to-wall trail is loose, and row 16 says tighter trails cut the winners as much.
Wider stops do better (> 120 bps +30.5 on 73; the dev window +7.8 gross / −7.2 net): a wide retest bar is a stock already
moving. The first wall within 0.5R wins 69 % (+11.3), beyond 2R wins 42 % (+2.3): mechanical, the near wall locks the trail.
**Pooled splits that flip by day (not edges yet):** the future's state with the trade at entry (LB for a long / SB for a
short) +11.9 on 524 vs +1.6 on 467 pooled — but +1.9 vs −0.6 on 27 Oct and +15.4 vs +27.7 on 2 Nov (reversed); the board
score with the trade > 2: +11.0 on 713 vs −1 to −13 on 268, the same inconsistency; a stock in the day's top-20 movers
+11.2 vs +2.8 here, the *worst* subset on the dev window (row 15); breadth the trade's way at entry 50–70 % +25 to +30
(266, win 70 %) vs 40–50 % −31.6 (103) vs > 70 % +5.6 (372): non-monotonic. Delta, queue imbalance, NIFTY's own move: nothing.
**The option leg's cost:** option % ≈ −1.5 + 14.7 × stock % (per day −1.3 to −2.0 intercept, 12–17× slope); the spread at
entry is 1.2–1.3 % of premium (p75 1.7 %), a flat stock trade costs −1.1 % of premium; 11 % of entries were priced off a
stale last trade. The stock has to make ≈ +10 bps gross to pay the option's round trip — the same hurdle as cash. Options
multiply the book, they do not change its sign.
**Next, pre-registered before running (not tonight, on battery):** on the dev window's 98,259 any-wall trades, (a) a breadth
-at-entry filter (the universe's share above its open, in the trade's direction, 50–70 %), (b) the tighter trails of row 16
on this population, (c) risk > 80 bps only. The board and flow readings wait for ten sessions (9 Nov).

**03:34 — the cap.** The owner's sizing: a ₹1L budget, at most two trades a day. `KN_STRATEGY_MAX_TRADES_PER_DAY=2`: the first two confirmed flips of the day are booked, the rest recorded with the cap as the reason. The evening scoreboard (`scripts/2026-11-03-level-retest/picker_scoreboard.py`) simulates every recorded signal's trade on the day's bars and scores the pickers; the learning curve is that table, day by day, against a random pick and the best possible two.

## H3 — can a ranker pick two a day? (3 Nov 03:50, `scripts/2026-11-03-level-retest/ranker.py`, log `ranker-dev.txt`)
Owner, on the hindsight best-two row: "can we do this anyway from algo". Pre-registered in the addendum before running.
A gradient-boosted classifier of "net > 0" on what is known at the entry (side, wall type, confluence, stop distance,
first-wall distance and room, entry bar, gap, tolerance), fit on one year of the 98,259 dev-window first flips, picking
the two highest-scored flips each day in the other year:

| fit → pick | days | top two a day, mean net | random two a day | top two − random (day-t) | top decile by score | all trades |
|---|---|---|---|---|---|---|
| 2024 → 2025 | 187 | −24.1 bps/day (day-t −2.4) | −20.7 | −3.4 (−0.4) | −6.9 on 4,392 | −10.6 a trade |
| 2025 → 2024 | 246 | −26.1 bps/day (day-t −3.2) | −21.6 | −4.5 (−0.6) | −11.3 on 5,432 | −11.0 a trade |

**Fails the bar in both directions: the model's two are no better than two at random, and both lose.** What the model
learns to like is what the descriptive splits showed — a wide stop (median 111–120 bps vs 70), a near first wall (0.56R
vs 0.77), an early bar (5–6 vs 11), round-number walls — and it does not pick on new days. The hindsight best-two over
the four live days (PGEL, GVT&D, OBEROIRLTY, BANKBARODA, INOXWIND, DATAPATTNS, NBCC, POLICYBZR: +221 to +396 bps)
share the same look — stop median 104 bps vs 67, room 0.54R vs 0.76, move from the open 0.59 ADR vs 0.31, 57 % already
in the day's top-20 movers — and four of the eight were held to the close: they are the trend days, which nothing at the
entry predicts (row 15). The one feature the dev window cannot test: 7 of the 8 had the future's state with the trade
(long build-up under a long, short build-up under a short) against 53 % of the rest; that is the owner's confirmation
and the day-10 pre-registration. The gap between first-two and hindsight-two (≈ ₹6,000 a day at ₹1L) is not reachable
with price features; the evening scoreboard measures whether the board closes any of it.

## H4 — the price action at the entry (3 Nov 04:00, `scripts/2026-11-03-level-retest/features.py`, log `features-dev.txt`)
Owner: "why we missing any signal use data and find real insight". Thirteen features built from the dev cache's 5m bars for
all 98,259 first flips: the stock's move from the open (ADR), its rank among every stock's move at that bar, the breadth
at that bar, NIFTY's move, the day's range so far, the close's place in the range, the break bar's volume and range, the
retest bar's volume and range, the bars break → retest and retest → confirmation. One-way splits, net bps, both years:

**The retest's own anatomy says nothing** (98k instances): the break bar's volume (high volume is *worse*, −13 vs −10), its
range, the retest bar's volume and range, how fast the pullback came, how fast the confirmation came, where the close sits
in the day's range, the day's range so far — every bucket −9 to −13, no order, no year agreement. The folklore of the
"breakout on volume" and the "clean retest" has no information in it.

**The market around the trade does, monotonically and in both years:**
| breadth moving the trade's way at the entry bar | n | net | 2024 | 2025 |
|---|---|---|---|---|
| ≤ 30 % | 6,007 | −19.6 | −17.5 | −21.4 |
| 30–40 % | 9,000 | −15.5 | −16.6 | −14.2 |
| 40–50 % | 17,193 | −14.5 | −13.8 | −15.8 |
| 50–60 % | 21,674 | −12.9 | −13.2 | −12.6 |
| 60–70 % | 18,558 | −7.8 | −6.6 | −9.3 |
| > 70 % | 25,827 | **−5.1** | −6.1 | −4.2 |
NIFTY's own move the trade's way is the same shape (< −0.5 % → −17.1; > +0.5 % → −8.2, both years). This is the first
conditioning variable in the whole project that is ordered, stable across years and large (a 15 bps spread on tens of
thousands of instances). It is still negative net in its best bucket (+10 gross against the 15 bps round trip).
**The leaders lose:** the stocks ranked in the top 10 % of the universe's move at the entry bar make −14.0 (both years),
the middle −8 to −9; a stock already 1.5 ADR from the open −15.7. The hindsight best-two of the live week were all leaders
— the exceptions in the worst group, which is why no picker finds them in advance.
**The ranker with everything (H4):** 2024 → 2025 the top two a day −17.9 vs random −20.7 (+2.8, day-t 0.3), the top decile
by score −0.8 against −10.6 for all (the model finds the breadth, not enough of it); 2025 → 2024 the top two −31.8 vs
−21.6. Fails the bar both ways. Picking the two highest-breadth flips of a day is the *worst* single-feature picker
(−67 to −71 bps/day): the most extreme breadth sits on the 09:30 flips of a one-way open, which reverse.
**The insight, plainly:** the money in this family is in the day, not in the trade. The entry contributes +4 gross
whatever its shape; whether the day's breadth and the index keep going the trade's way moves the result by 15 bps, and the
best stocks to be in are not the leaders but the middle of the pack on a broad day. A two-a-day book should therefore
(a) refuse flips against the breadth (< 50 % the trade's way), (b) avoid the top-decile leaders, and (c) accept that the
remaining expectancy is about −5 net until something not in the price (the board's state) is shown to add to it. (a) and
(b) go into the evening scoreboard as pickers from tonight; a pre-registered test of (a)+(b) as a live filter follows.
