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
