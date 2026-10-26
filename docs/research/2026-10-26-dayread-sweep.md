# The day read on every stock-day: what repeats (sweep of 2026-10-26)

Owner's ask, after the NIFTY read of 2026-10-26 matched his narrative point by point: "go deep, use
the same theory on other stocks, we have so much data, tell me common findings". This is a
**descriptive sweep, not a pre-registered test**: `analysis/dayread.py` (pivots and the CPR tested,
held, broken; swings as they become known; breaks of structure; legs) run on every F&O stock-day in
the development window, with the futures and option OI per leg on the three live archive days.
Anything that looks like an edge here is a hypothesis to pre-register, and the holdout is spent:
the only test left is forward, in SHADOW.

Scripts: `research/dayread_sweep.py` (the sweep, per-symbol parquet) and `research/dayread_stats.py`
(the tables). `kotsin-nse dayread-sweep OUT_DIR` then `kotsin-nse dayread-sweep OUT_DIR --stats`.

## Data

| Part | Source | Window | Size |
|---|---|---|---|
| A, price only | 5m history cache, 09:15–15:30, previous day from the 1d cache | **2024-01-01 → 2025-09-30** (the holdout 2025-10-01 → 2026-10-16 is spent and untouched) | 90,452 stock-days, 216 names (212 stocks, 4 indices), 33 events and 14 legs a day |
| B, with OI | the engine's archive: 1m bars, `archive/oi` through the scrip master (front future + the tracked expiry's strikes) | 2026-10-19 (217 names), 10-23 (52), 10-26 (52) | 321 stock-days, 4,054 legs |

Forward returns are measured from the close of the bar on which the event became known (a level
test, break or break of structure is known at that bar's close; a swing label is stamped when it
becomes known, two bars later). "Excess" = minus the unconditional forward return from the same time
bucket. t-statistics are clustered by calendar day. Costs: ~15 bps a round trip (charges ≈ 9 bps on
equity intraday + the engine's 5 bps slippage a fill).

## Common findings

**1. The opening half hour sets one of the day's extremes on 79 % of days.** The day's high is in
the first 30 minutes on 45.9 % of days, the low on 38.8 %, both (an inside day) on 6.1 %, neither on
21.4 %. The last half hour is the second cluster (low 12.7 %, high 8.6 %). The day's high is in the
first hour 53 % of the time, the low 47 %. A random walk puts an extreme in the first 8 % of the
session about 18 % of the time, so this is the shape of the session, not chance.

**2. A level that held is not a wall.** After a touch that closed on the right side, the level gave
way (a close beyond it by half a tolerance) within 30 minutes about a quarter of the time, within the
hour about a third, and by the close about half. The CPR band is the stickiest. The first hour is the
worst time to trust a hold.

| Level (support) | n | gave way in 30 min | in 60 min | by the close | in 30 min, first hour / later |
|---|---|---|---|---|---|
| CPR | 73,283 | 12.4 % | 19.4 % | 39.5 % | 17.4 / 6.1 |
| PDC | 81,320 | 25.4 % | 35.8 % | 56.5 % | 32.1 / 17.4 |
| PDL | 63,036 | 24.7 % | 35.0 % | 55.1 % | 31.7 / 18.1 |
| S1 | 62,672 | 23.1 % | 33.8 % | 54.2 % | 29.8 / 17.5 |
| S2 | 28,815 | 23.0 % | 33.5 % | 52.1 % | 30.6 / 19.3 |

Resistance mirrors it (CPR 11.3 / 17.6 / 35.9 %, R1 23.4 / 32.5 / 51.1 %, PDH 24.2 / 33.4 / 51.6 %).
Breaks are no better: 33–47 % of closes beyond a level are back through it within 30 minutes
(S1 42 %, S2 47 %, R1 45 %; the CPR 33 % down, 41 % up), 41–55 % within the hour.

**3. The hold itself predicts nothing.** The excess return to the close after a hold, a rejection or
a break is within ±4 bps at every level (pooled; the day-weighted mean within ±13 bps), with hit
rates 45–55 %. Still holding 30 minutes later: the same. With or against the 20-day trend: the same.

**4. Five-minute structure is noise.** 14 legs a day (10 to 18); the median leg is retraced 89 % by
the next one; 42.5 % of legs are retraced fully (the "structure" fails). 15m: 79 % median retrace,
37 % fail; 30m: 66 %, 31 %. The largest leg of a day is 59 % of the ADR (median). A clean HH + HL
structure known by 10:15 (46 % of days) is followed by a higher close 47.7 % of the time — the same
as a mixed structure (47.4 %). Known by 11:15 (18.5 % of days): 47.5 %. A break of structure
continues to the close 45–52 % of the time at every hour; the day's first BOS (median 10:30) agrees
with the close-versus-open sign 65 % of the time because it *is* that move so far, and adds +4 bps
after it.

**5. The owner's NIFTY sequence, on everyone.** Open test held under the CPR by 09:45 → break of
structure within the hour → close through the CPR → the first return to the CPR band. Bar-level,
both sides:

| Stage | Bullish | Bearish |
|---|---|---|
| A: open test held under (over) the CPR by 09:45 | 31,556 stock-days (35 %) | 33,603 (37 %) |
| B: + BOS within the hour | 5,830 (18 % of A) | 7,203 (21 %) |
| C: + close through the CPR (median 10:30) | 3,286 (56 % of B) | 4,019 (56 %) |
| D: first return to the CPR band | held 2,928, failed 48, never returned 310 | held 3,628, failed 64, never 327 |
| the held retest still holding 30 min later | 91.3 % | 91.6 % |
| from the held retest to the close, excess | **+3.8 bps, hit 49.5 %**, MFE +90 / MAE −83 | **+4.6 bps, hit 53.6 %**, MFE +92 / MAE −83 |

The retest holds — on the bar 98 %, half an hour later 91 % — and the day from there is a coin
flip with a symmetric excursion. Above/below the 20- and 50-day average, the previous day's
direction, the gap, with or against the trend: every slice within ±11 bps, no |t| above 2.1.
Retests at 11:15–13:15 run +7 to +16 bps on 200–400 cases, not significant. The 2026-10-26 NIFTY
day was a true instance of a common pattern whose outcome, on average, is not predicted by the
pattern. (Consistent with HANDOFF §3.4 rows 10, 11 and 11c, which tested the owner's rules as trades.)

**6. "Outer pivot held while the stock is alone" was a hindsight artifact.** The first pass showed
S1/S2 holds worth +30 to +57 bps when few stocks tested S1/S2 *that day* — but the day's count is
known only at the close (few tests all day = the market did not fall later). With the count known at
the time (stocks seen so far) the raw return to the close is −1 to −8 bps in every bucket; "at most
15 seen and |Nifty| < 0.5 % so far": raw −3.4 bps, −18 after costs, hit 48.5 %, daily t −3.1.
Against the *same day's* cross-section a held S1/S2 does outperform (+13 bps, t 12 on broad
sell-off days) — a relative statement, two legs of cost, not a trade.

**7. Gaps fade, and the window's drift was down.** Close above the open: 58.8 % after a gap below
−1 % (+40 bps open→close), 43.2 % after a gap above +1 % (−19 bps), 43.6 % on flat opens (−19 bps).
30 % of days were trend days (close in the top or bottom 15 % of the range). 82 % of days break the
first-hour range; the close is beyond it 55 % (up break) / 57.5 % (down break) of the time.

**8. OI on the three archive days (description, not a test).** Over a 5m leg the front future's OI
is flat (< 0.25 %) on 65–76 % of legs; the share with a real change rises through the day — up-legs:
flat 76 % at the open → 40 % in the last hour (short covering 8 % → 34 %, long build-up 16 % →
26 %); down-legs: flat 68 % → 36 % (long unwinding 12 % → 33 %). Positioning is a last-hour
phenomenon on these days, not a morning one. Up-legs with an OI change (LB or SC) were retraced 82 %
(median) by the next leg against 124 % for flat-OI legs — but they are also bigger legs (0.55 % vs
0.34 %). Day-level: 2026-10-19 was a short build-up day (98 of 217 names), 10-23 long unwinding,
10-26 short covering (34 of 52) — the week before the October expiry. Three days: re-run at 15+.

## What this means for the method

- The vocabulary (pivots, CPR, structure, legs, who positioned) **describes** a session correctly and
  `dayread` now produces that description for any symbol and day. It does not **predict** the rest of
  the session on the price path alone, at any level, in any context tried, over 90,452 stock-days.
- What is left to test is not another price-path rule: it is whether the OI/strike story per leg
  (Part B) separates the retests that run from the ones that fail. That needs ≥ 15 archive days and a
  pre-registration first, then a forward SHADOW test (the holdout is spent).
- Useful as structure for the day: the first half hour is a day extreme 4 days in 5; a hold is good
  for about an hour, half the time; 5m legs are retraced almost fully, 30m legs two thirds.

## 9. The retest as a trade to the next wall (owner's ask, the same night)

"Proper retest, then price moves, then it reaches the next wall — that gives the direction too." Tested on
every held retest of the owner's sequence above (6,409: 2,856 bullish, 3,553 bearish; indices excluded), on the
5m bars. Entry A = the close of the retest bar; entry B = the first 5m close beyond the retest bar's high (low)
within 6 bars ("it starts growing"; 49 % of retests give it). Stop L = the retest bar's low (high) minus one
tolerance; stop P = the central pivot (the owner's "TC to pivot" risk). Target = the nearest wall beyond the entry
by at least one tolerance: daily pivots R1/R2/R3 (S1/S2/S3), PDH (PDL), the previous week's pivots WP/WR1/WR2
(WS1/WS2), and the next round number (a step of ~0.5–0.7 % of price). Stop and target in the same bar count as
the stop. Costs 15 bps a round trip. t by day.

| Entry | Stop | n | target first | stop first | close | wall touched by the close | median risk / reward bps | gross bps | net bps | gross R | net R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A retest close | L | 6,371 | 43.5 % | 49.4 % | 7.0 % | 58.4 % | 39 / 47 | +2.3 | −12.7 | +0.06 | −0.35 |
| B first close beyond | L | 3,101 | 52.5 % | 34.0 % | 13.5 % | 58.5 % | 65 / 46 | +2.0 | −13.0 | +0.03 | −0.21 |
| B | P | 3,101 | 51.8 % | 33.7 % | 14.5 % | 58.5 % | 67 / 46 | +1.0 | −14.0 | +0.02 | −0.23 |
| B, pivot walls only | P | 3,101 | 47.0 % | 35.7 % | 17.2 % | 53.7 % | 67 / 55 | +0.8 | −14.2 | +0.01 | −0.23 |

By the wall that was the target (entry B, stop L): round number 755 cases, hit 63 %, reward 36 bps, +3.7 gross;
PDH/PDL 767, 52 %, +3; S1/R1 990, 49 %, −1; S2/R2 356, 40 %, +2; weekly pivot 112, 61 %, +9. By the setup's
reward:risk: under 1 → 58 % reach the target, +0.8 gross; 1–2 → 36 %, +6.3; 2–3 → 14 %, −5.4. By hour of the
retest: −6 to +6 bps gross everywhere; 2024 +2.6, 2025 +1.3. Bullish and bearish the same.

The next wall is reached 58 % of the time — because it is closer than the stop (median reward 46 bps against a
65 bps risk). A driftless path reaches the nearer side first risk ÷ (risk + reward) = 59 % of the time; the
observed 52.5 % plus the 13.5 % that were still open at the close is that number. When the wall is 2–3 R away
only 14 % get there. The hit rate is set by the distances, not by the sequence: the sequence gives the direction
of the move so far, not of the move to come. Expectancy +0.01 to +0.06 R before costs, −0.2 to −0.35 R after —
the same as HANDOFF §3.4 rows 11 and 11c from a different construction. Script: scratch `retest_wall.py`
(method fully specified above).

## 10. Dynamic management and entry filters on the same retests (owner: "our entry, stop and target are not dynamic")

Same instances, entry = the first 5m close beyond the retest bar's extreme within 6 bars (3,101), initial stop = the
retest bar's extreme minus a tolerance (median risk 65 bps), walls as in §9 (median nearest wall 46 bps away).
Eleven exit rules, then entry filters known at the entry bar. Costs 15 bps.

| Exit rule | win % | avg win / loss bps | gross bps | net bps | net R | exits |
|---|---|---|---|---|---|---|
| fixed: next wall | 58.2 | +48 / −61 | +2.0 | −13.0 | −0.21 | wall 52, stop 34, close 14 |
| fixed 1:2 | 43.6 | +87 / −59 | +4.4 | −10.6 | −0.18 | stop 43, close 38, target 19 |
| fixed 1:1 | 51.2 | +63 / −61 | +2.3 | −12.7 | −0.21 | |
| ATR stop (1×), target 2 ATR | 34.4 | +60 / −30 | +0.8 | −14.2 | −0.23 | stop 64 |
| trail under each new 5m swing low | 39.0 | +78 / −45 | +2.7 | −12.3 | −0.20 | stop 56, close 23, trail 21 |
| break-even at 1R, target 2R | 57.8 | +47 / −60 | +2.1 | −12.9 | −0.21 | |
| half off at 1R, rest trailed | 45.1 | +62 / −47 | +1.9 | −13.1 | −0.21 | |
| time stop (12 bars, < 0.5R) | 52.6 | +39 / −43 | 0.0 | −15.0 | −0.24 | |
| chandelier 1.5 ATR | 38.1 | +48 / −27 | +1.6 | −13.4 | −0.21 | |
| **next wall, then trail under it to the following wall** | 58.2 | +50 / −61 | +3.2 | −11.8 | −0.19 | trail 45, stop 34, close 21, following wall 0 |
| fade: sell the confirmation, target the retest low, stop the wall | 55.5 | +38 / −47 | +0.1 | −14.9 | −0.24 | |

Every rule lands between 0 and +4.4 bps gross: the exit changes the shape (win rate against payoff), never the mean
— the optional-stopping theorem on a driftless path. The fade at +0.1 says the path after the confirmation has no
drift either way. Entry filters on the next-wall rule: rejection bar (close in the top/bottom third) +3.4 gross
(1,182); quiet pullback (retest volume ≤ 0.7× the day's average) +3.5 (1,645); confirmation on volume ≥ 1.5× −1.8
(782); all three +3.7 (76); before 11:15 +1.8, after +2.3; narrow / medium / wide CPR +1.6 / +2.8 / +1.4; gap with
the trade +3.0, against +2.0. Nothing above +4 gross, nothing positive net, every day-clustered t negative.

**Where the owner's trades come from.** Split by what the day turned out to be (hindsight): 29.9 % of these retests
fell on a trend day in the trade's direction (close in the top/bottom 15 % of the range) — there the next-wall rule
makes +27 bps gross with 77 % wins, wall-then-trail +37, fixed 1:2 +56 (+41 net), trail-swings +42. The other 70 %:
−7 to −15 gross on ordinary days (66 %) and −36 to −62 on trend days against (4 %). The sequence does not raise the
odds of a trend day: 29.8 % of all stock-days are trend days, 29.9 % of the retest days are trend days the trade's
way. The pattern is remembered on the days that trended; it did not predict them. The algorithm's missing piece is a
trend-day predictor known by 10:50, and the price path has not supplied one (the day-type study, these filters).
Break-even at these distances: a 72 % wall-hit rate; a driftless path gives 59 %; observed 52.5 % + 13.5 % open at
the close.

## 11. "Find each stock which is trending and use this strategy" (owner, 2026-10-27 00:30)

Method (scratch `retest_trend.py`, the same 3,101 entry-B instances as §10, dev window): 33 definitions of "trending",
every one known at the confirmation bar — daily (close vs DMA20/DMA50, EMA 9/21/50 stack, 5- and 20-day return, 20-day
return minus NIFTY's, ADX14 > 20/25 with DI aligned, yesterday's close in its range ≥ 0.7, yesterday's return, close vs the
weekly pivot), the owner's stack (SuperTrend(10, 3) on 15m and on 1H bars, built from the 5m history), intraday at the bar
(move from the open ≥ 0.3 / 0.5 ADR, close in the top fifth of the range so far, range so far ≥ 0.5 ADR, NIFTY's own move
≥ 0.2 %, the stock's move minus NIFTY's ≥ 0.3 %, the opening hour's range broken), and the cross-section (the aligned
move / ADR ranked across all stocks at that bar: top half, top 20 %, top 10 %, top 5 %), plus combinations. For each: the
share of entries landing on a trend day the trade's way (base 29.9 %), and the four rules' gross / net / day-t.

Result: nothing. Trend-day share 26–36 % across every filter; the ones that raise it (move ≥ 0.5 ADR 36 %, top-10 % movers
33 %, top-10 % with NIFTY aligned 41 % on n 128) lose more, because what has moved has less left: the top 20 % of the day's
movers −3.2 gross / −18 net (n 1,174), the top 10 % −6.1 / −21, the top 5 % −11 / −26; the *not*-top-20 % +5.3 / −9.7. The
best subsets are the owner's daily stack with the 1H SuperTrend (n 480: next-wall +4.3 gross / −10.7 net, 1:2 +10.2 / −4.8,
day-t −0.4 to −3.0) and "not moving relative to NIFTY" (n 710: +4.6 / −10.4). The 15m SuperTrend aligned is *worse* than
not aligned (+1.1 vs +3.7 gross). Every filter, both years, negative net.

Plain momentum on every stock-day (signed move / ADR at a bar, by decile → the rest of the day): the top decile continues
by +12 to +15 bps raw at 09:45 / 10:15 / 11:15 / 12:15 and the bottom decile reverses by +2 to +10 (both sides drift up with
the market); against the cross-section the top decile is +4 to +7 and the bottom +6 to +8 — the extremes both mean-revert
relative to the crowd. Continuation in the move's direction for the top decile: −1.0 bps/day-mean at 09:45, +1.1 at 10:15,
+4.6 at 11:15, +6.6 at 12:15 (t 1.8; 2024 +14.5, 2025 −3.8). Trend-day share rises with the move (23 → 34 % for the top
decile from 09:45 to 12:15) but so does what is already spent.

The owner's decision the same night: run the sequence in SHADOW from 2026-10-27, every confirmed retest, each tagged with
these filters, and read the day against this note in the evening. The live detector (`strategy/cpr_retest.sequence`)
replayed on twelve stocks' history reproduced the sweep's sequence rows on 10,304 symbol-day-sides with zero differences and
found each confirmation bar at its own prefix (168 / 168).
