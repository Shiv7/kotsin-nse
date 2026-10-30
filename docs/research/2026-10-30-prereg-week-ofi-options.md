# Pre-registration — the week's order flow, options board and the shadow trades (2026-10-30, evening)

Owner's ask (30 Oct, 20:30 IST): "whole week we have OFI option whole data perform weekly integrated analysis
and find actual and correct patterns". Written before any test ran.

## Data

Recorded by the engine itself (`backend/data-live/archive/`), full sessions **27, 29 and 30 Oct 2026** (28 Oct
was lost to the laptop's sleep; 26 Oct is half a session and is excluded):

| archive | grain | rows a day | what |
|---|---|---|---|
| `flow` | per minute × code (779 codes: underlyings, futures) | ~235k | buy/sell volume by the quote rule, delta, cumulative delta, OFI (top), OFI (deep), queue imbalance `qi`, weighted imbalance `wimb`, microprice `micro_bps`, spread, bid/ask depth |
| `fno` | per minute × root (218) | ~81k | the F&O board: futures state (LB/SB/SC/LU) and OI change, calls' and puts' states, PCR and its change, ATM IV and the previous day's, straddle, call/put walls, score, bias |
| `bars` | 1m × underlying (224) | ~79k | the tick-built bars (`live`), REST-installed (`rest`) |
| `oi` | per OI update × code | ~300k | futures and options OI as the feed sent it |
| strategy files | per day | 6 + 6 + 2 trades | the shadow book (29 Oct's and 30 Oct's two files) |

The **official** daily OI (NSE bhavcopy, `oi_daily/`, 697 sessions) was already studied in September
(HANDOFF §3 rows 6–8): the four build-up states, the 16 and the 64 states predict nothing on their own;
as a gate, longs after a short build-up / Bear day lose −0.20 R and shorts after a long build-up / Bull day
−0.15 R. That is **not re-run** here; T5 checks the week's trades against it.

## Tests

**T1 — does the recorded order flow predict the next minutes? (micro)** For every underlying and every
5-minute mark from 09:45 to 15:00, the last 5 and last 15 minutes of each reading (delta, OFI top, OFI deep,
`qi`, `wimb`, `micro_bps`) against the next 5 / 15 / 30-minute log return of the 1m close, **demeaned per
(day, minute) across stocks** (the market factor out). Metric: Spearman rank IC per cross-section, mean and
t over the ~190 cross-sections (3 days × ~63 marks), and per day; the top-minus-bottom-quintile return in
bps. Placebo: the reading shuffled within each cross-section.
*Counts as a pattern only if:* mean IC's |t| ≥ 3 over the cross-sections **and** the same sign on all three
days **and** the placebo |t| < 2. *Counts as tradable only if* top-minus-bottom ≥ 13 bps at the horizon
(the ₹1L round-trip cost). A known microstructure IC of a few hundredths at 5 minutes is a pattern, not a trade.

**T2 — does the morning F&O board say anything about the rest of the day?** For every root with a board
row at 10:15 and at 12:00: the futures state (LB/SB/SC/LU), the change of PCR since the open, the change of
ATM IV versus the previous day, the calls' and the puts' states → the stock's demeaned return from that
minute to 15:15 (1m close). Metric: mean demeaned return by state with its SE (n ≈ 3 × 218 stock-days per
mark); IV change and PCR change as quintiles.
*Counts as a pattern only if:* the same sign on all three days **and** pooled |t| ≥ 2.5 **and** the sign
agrees with the September daily study (a morning long build-up that then falls would contradict it and is
reported as noise). Three days are three draws of the market factor; the demeaning removes the level, not the
regime, so a result is "this week's", not "a rule".

**T3 — the week's 14 live shadow trades against the readings** (27 Oct 6, 29 Oct 6, 30 Oct 2; 28 Oct's nine
are a replay with no recording). For each entry minute: the board row (futures state, PCR, IV change), the
last 15 minutes of flow (delta, OFI, `qi`), NIFTY's position in its day range so far, the previous session's
official build-up. **Descriptive only** (n = 14); nothing is decided from it. It is the table the evening read
will keep producing now that every signal carries `context` and `bhav`.

**T4 — the shorts at NIFTY's low (loop-3 candidate, dev window).** This week's five stopped shorts (28 Oct
three, 30 Oct two) were all taken with NIFTY in the bottom of its range so far. On the development window
(2024-01-01 → 2025-09-30; the holdout is spent) the 7,138 base-rule instances get NIFTY's position in its
day range at the entry bar (`(close − low) / (high − low)` of NIFTY's 5m bars up to that bar). Pre-registered
split: **against** = short with position ≤ 0.2 or long with position ≥ 0.8; **with** = the rest.
*Decision bar (same as loop 2):* drop *against* only if that slice is ≤ −10 bps net on ≥ 100 instances, the
kept set improves by ≥ 3 bps net and ≥ 0.3 t, and both years move the same way. Otherwise nothing ships
and the week's five are five.

Net = gross − 15 bps. t by cross-section (T1) or by day (T2, T4). Everything is reported, including the
failures. Scripts in `docs/research/scripts/2026-10-30-week/`.
