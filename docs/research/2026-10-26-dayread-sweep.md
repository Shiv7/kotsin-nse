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
