# Pre-registration: option signals, regimes, day types (written 2026-10-23, before any result)

Owner's go: "lets do it". Three studies, decided here before the data is looked at. Anything that
passes goes to the holdout **once**, on the owner's word. Anything that fails is recorded and dropped,
not re-tuned.

**Windows.** Daily studies: 2021-04-01 → 2025-09-30. Intraday (1m): 2024-04-01 → 2025-09-30.
**Holdout 2025-10-01 → 2026-10-16 stays untouched.** Universe: stocks with stock futures on the day
(`market/fo_universe.py`). Costs: a stock future round trip = charges (`risk/costs.py`) + 5 bp
slippage per fill.

**Multiple testing.** 10 + 3 + 1 tests below → a result counts only at **t ≥ 3.0** (by week), with the
sign stated here, ≥ 60 % of months the right way, and the same sign in both halves of the window
(daily: 2021-04 → 2023-06 / 2023-07 → 2025-09; intraday: 2024-04 → 2024-12 / 2025-01 → 2025-09).

## 1. Option signals → the stock's next 1 and 5 sessions (`research/optsignals.py`)
From NSE's F&O bhavcopy, per stock and session (known after the close of D): the near-month
options (≥ 5 days to expiry, else the next month), the future of the same expiry as F.

| # | Signal | Source idea | Predicted sign |
|---|---|---|---|
| H1 | ATM call IV − ATM put IV (same strike) | Cremers & Weinbaum 2010 | + |
| H2 | OTM put IV (strike ≈ 0.95 F) − ATM call IV (skew) | Xing, Zhang & Zhao 2010 | − |
| H3 | Option volume vs its own 60-session median ÷ stock volume vs its own (abnormal O/S) | Johnson & So 2012 | − |
| H4 | OTM put OI added − OTM call OI added, ÷ total option OI | put build-up = informed bears | − |
| H5 | Isolation-forest anomaly score over the z-scored features (unsupervised) | unusual activity | + on the **absolute** move vs the ATM-implied move (direction-free) |

Measure: per session, the rank correlation (IC) between the signal and the return from D+1's open to
the close of D+1 / D+5; mean IC by week, t over weeks; top-minus-bottom quintile in bps against the
cost of a round trip per rebalance. H5: abs(return) / ATM-implied move over the same days, top
anomaly decile vs the rest.

## 2. Market regimes (`research/regimes.py`)
Market-wide per session: breadth (% of F&O stocks up, % above their 20-day average), dispersion
(cross-sectional σ of returns), mean |return|, NIFTY future's return and 20-day realised vol, NIFTY
ATM IV (bhavcopy OPTIDX), the aggregate change in stock-futures OI, NIFTY PCR (OI). Gaussian mixture,
k = 4, **fitted only on sessions before** the one labelled (refit each quarter, from 2021-04 + 1 year
of warm-up, so labels start 2022-04).

| # | Test | Pass |
|---|---|---|
| R1 | NIFTY next-session realised move ÷ ATM-implied move, by regime | a regime ≤ 0.8 (sellers' regime) or ≥ 1.2 (buyers'), both halves |

*Correction (2026-10-23, before any result was seen): for a fairly priced normal move E|r| = σ·√(2/π) ≈ 0.80σ, so R1's ratio is divided by √(2/π) — 1.0 = fair — and the 0.8 / 1.2 thresholds apply to that.*
| R2 | The intraday rules already tested (`confirm-*`, `mtf-*`, `dbcpr-*`): average R by the regime known at the open | a regime with average R > 0 after costs at t ≥ 3 |
| R3 | Next-session 1-day cross-sectional reversal (IC of −return D on return D+1) by regime | regime-dependent sign, both halves |

## 3. Day types (`research/daytype.py`)
Per stock-day from 1m bars. **Types** (unsupervised): k-means, k = 5, on the full-day shape (open→close
/ ADR, close location in the range, times of the high and low, VWAP crossings, range / ADR, path
efficiency), fitted on 2024-04 → 2024-09 only, labels applied after. **Prediction at 11:00**: gradient
boosting on what is known by 11:00 (gap / ATR, 09:15–11:00 return and range / ADR, location in the
morning range, VWAP side and crossings, volume vs the 20-day same-time average, position against the
daily CPR and weekly levels, the previous 1–3 sessions, the market's same readings), walk-forward:
trained on every month before, tested on the next.

| # | Test | Pass |
|---|---|---|
| D1 | Trade 11:00 → close in the predicted direction when the model's probability ≥ 0.6, a stock future | net bps after costs > 0, t ≥ 3 by week, ≥ 60 % months, both halves; and better than the two baselines (always follow the morning move; always fade it) |

Recorded whatever the outcome: in `docs/HANDOFF.md` §3.4, with run ids.

## 4. The power of each tool on its own (`research/power.py`) — added 2026-10-23, before any result

Owner's ask: "test each indicator power — pivot, pivot cluster, fibo, SMC, liquidity sweep, double
bottom, reversal — low and high timeframe; IBH role". Not strategies: each tool's raw prediction.

**Outcome (every event).** From the close of the event bar, a race: does price go X in the tool's
direction before X against it? (X = 0.25 ADR on 5m bars, rest of the session; X = 1 ADR on daily
bars, 10 sessions.) A coin scores 50 %; a bar touching both counts ½. Also the signed move after 30
and 60 minutes (5m) / 5 sessions (daily), in ADR.

**Control.** Every level event has a placebo: the same level shifted by 0.15–0.35 ADR (random side,
fixed seed) on the same stock-day, read by the same rules. Fibonacci ratios (38.2 / 50 / 61.8 / 78.6)
are matched by non-Fibonacci ratios (30 / 45 / 56 / 70). Patterns (sweep, order block, double bottom,
reversal candles) are compared with 50 % and with their opposite reading (a sweep vs a breakout that
closes beyond).

**Events.** Low timeframe (5m from the 1m cache, 2024-04 → 2025-09): first touch of each daily pivot
(P, TC, BC, R1, S1, R2, S2, PDH, PDL), weekly (WP, WR1, WS1, PWH, PWL) and monthly (MP, MR1, MS1, PMH,
PML) level from either side; clusters (levels within 0.1 ADR, weight M3/W2/D1); Fibonacci of the
previous day's range; IBH / IBL (09:15–10:15) touches and breaks; liquidity sweeps of PDH/PDL and IBH/IBL;
SMC order blocks (the last opposite 5m candle before a ≥ 1.5 ATR displacement) on their first retest;
5m double bottoms/tops at the neckline break; hammer / shooting star / engulfing. High timeframe
(daily cache 2021-04 → 2025-09): weekly and monthly pivots, Fibonacci of the 60-session swing, sweeps
of the 20-session high/low, daily double bottoms/tops.

**Counts as power:** real − placebo (or − 50 %) with t ≥ 3 by week, the same sign in both halves.
**IBH role** is descriptive: how often the IB breaks up / down / both / neither, what follows a break
or a failed break, and how the IB's size and the CPR's width change that.

## 5. Hedged NIFTY selling in the calm regime (`research/condor.py`) — 2026-10-23, before any result

Follows R1 (passed: NIFTY's next-day move 0.77× what its IV priced in regime 3, the calm uptrend).
**One primary rule, judged once here and once on the holdout.**

**Trade (primary).** At the close of session D, when D's regime (`regimes.py`, fitted on the past) is
the calm uptrend: sell a NIFTY **iron butterfly** on the nearest weekly expiry with ≥ 1 day to go —
short the ATM call and put (the strike nearest F, F from put-call parity at the strike nearest the
future), long the call and put **2 one-day σ** away (σ_d = ATM IV × F / √252, rounded to the nearest
listed strike, at least one strike). Close all four legs at the close of D + 1 (the expiry's final
settlement if D + 1 is the expiry). Prices: the bhavcopy close (settle on expiry day).

**Costs.** Every leg, both ways: 1 index point of spread per transaction + the engine's charges
(`risk/costs.py`, an option leg, NIFTY's lot). Sensitivity, not selection: 2 points.

**Window.** 2022-04 → 2025-09 (regime labels start 2022-04). Holdout 2025-10-01 → 2026-10-16 untouched.

**Pass.** Net points per trade > 0 with t(by week) ≥ 2.0; ≥ 60 % of months positive; both halves
(2022-04 → 2023-12 / 2024-01 → 2025-09) positive; profit factor ≥ 1.2; still positive at 2 points of
spread. Reported, not judged: the same trade on every day and in each other regime; the worst day, the
worst month and the largest drawdown, in points and in ₹ for one lot.

### 5b. Result of §5 and one follow-up variant (written before the variant was run)

§5 failed (`condor-4ea7986b13`): +5.4 points a trade gross in the calm regime (65 % wins, PF 1.46), but
12.1 points of costs — eight legs crossed every day — leave −6.7. Costs, not the edge, killed it.

**Variant B — hold to expiry.** At the close of a calm-regime session D whose nearest weekly expiry is
2–7 calendar days away, and with no position open: the same iron butterfly on that expiry, the wings 2σ
of the move **to expiry** away (σ_d × √(business days to expiry)), held to the expiry's final settlement
(intrinsic, no exit spread; charges on the in-the-money legs' settlement). One position at a time.
Costs: 1 point of spread per leg on entry; charges at 1 lot and at 10 lots (brokerage is per order).
**Pass:** the same as §5 (net > 0, t by week ≥ 2, ≥ 60 % months, both halves, PF ≥ 1.2, still positive at
2 points), at **10 lots**; 1 lot reported. This is the last variant in the exploration window; the
survivor of §5 / §5b, if any, goes to the holdout once.

## 6. Two follow-ups (written 2026-10-24, before any result)

Both chase the cost problem that stopped §1 and §5: the information is real, the daily turnover killed it.

### 6a. A combined option-chain score, traded weekly (`research/chainscore.py`)
From the §1 panel (158 k stock-sessions, known after each close): H1 call−put IV spread, H2 put skew,
H3 abnormal option/stock volume, H4 OTM put build, each as a per-session cross-sectional rank (0–1).
**Score** = the weighted sum with weights fitted **walk-forward**: for each month, a ridge regression of
the next-5-session return on the four ranks over the previous 12 months (no look-ahead; the first
test month is 2022-04). The **primary rule**: every Friday's close (the last session of the week), go
long the top quintile and short the bottom quintile of the score, equal-weighted, as stock futures;
hold to the next Friday's close; a stock already on the same side is kept, so costs (the §1 round
trip, 14.6 bps) are charged only on names that enter or leave. Reported, not judged: the same with
daily rebalancing, the long and the short legs separately, by year.

**Pass**: weekly long-short return > 0 after costs with t(by week) ≥ 2.0, ≥ 55 % of weeks positive,
both halves (2022-04 → 2023-12 / 2024-01 → 2025-09) positive, and a Sharpe (weekly, annualised)
≥ 1.0. One run. A pass goes to the holdout once, on the owner's word.

### 6b. Overnight vs intraday (`research/overnight.py`)
Daily cache 2021-04-01 → 2025-09-30, NIFTY, BANKNIFTY and every F&O stock (point-in-time members).
Decompose each session: **overnight** = open / previous close − 1, **intraday** = close / open − 1.
Report means, t by week, both halves, by year, and for the stocks the cross-sectional average.
**Primary rule** (index only): long NIFTY futures from each close to the next open (one lot; costs = the
future's charges + 5 bp slippage per fill, round trip). **Pass**: net > 0, t(by week) ≥ 2.0, ≥ 55 % of
weeks positive, both halves positive. Reported, not judged: the same on BANKNIFTY; intraday short
(open → close) as the mirror; the overnight return by regime (`_regimes.parquet`); by weekday.

**Results (2026-10-24):** §6a fails — +11.8 bps/week gross (t 1.4), −9.3 net at 72 % weekly turnover (`chainscore-0a17df1e73`).
§6b fails as a trade — NIFTY overnight +9.1 bps gross (t 6.0) vs 12.8 bps costs → −3.7 net (`overnight-23c20be4e0`) — but the
decomposition is the strongest fact found: stocks +16.4 bps overnight (t 10.8), −8.4 intraday (t −3.3), 96 % / 91 % of names.

## 6c. Overnight only, names picked by the chain (`research/nightpick.py`) — 2026-10-24, before any result

Combines the two real sources: the overnight premium (§6b: stocks +16.4 bps a night) and the chain's
one-day information (§1). **Timing caveat, stated up front:** the chain readings are the bhavcopy's
close-of-D values; NSE publishes the file after the close, so in the backtest they stand in for what
the engine's live chain shows at 15:25 (it computes the same readings live). A 5-minute approximation.

**Primary rule.** Each session D: a ridge score from the four chain ranks (as §6a) with weights fitted
walk-forward on the **overnight return** (D's close → D+1's open) over the 12 months before D's month;
**long the top quintile, equal-weighted stock futures, from D's close to D+1's open**; costs 14.6 bps
per name per night (a full round trip). **Pass**: net > 0, t(by week) ≥ 2.0, ≥ 55 % of weeks positive,
both halves (2022-04 → 2023-12 / 2024-01 → 2025-09) positive, Sharpe ≥ 1.0.

Reported, not judged: every member's overnight (the baseline the pick must beat), the bottom quintile,
top − bottom; by year; by the regime at D's close — **post-hoc** (§6b already showed relief-rally and
calm nights are the big ones), so a regime filter is a candidate for a later pre-registration, not a
result here; a 10-lot NIFTY future on the same nights as a cheaper carrier.

**§6c result (2026-10-24):** passes as registered (`nightpick-e7c35211e4`: +7.5 bps net, t 3.5, both halves, Sharpe 1.85). The
execution check with tradable prices: entry at the 15:25 bar holds (+8.6 net); exit at the 09:16 close −3.2, at 09:20 −5.0 — the
edge is the opening print's overshoot, which futures cannot sell and cash cannot afford (delivery STT). Not taken to the holdout.

## 7. Post-results drift (`research/events.py`) — 2026-10-25, before any result

**Why this lane.** Every edge found so far is 5–25 bps against a 10–15 bps round trip. A quarterly
result moves a stock by hundreds of bps and, in every market studied, the move continues for days
(post-earnings-announcement drift). Costs are paid once against a multi-day move — the only lane where
the cost wall does not bind. It needs one new input: *when* each company reported.

**Data.** NSE's corporate-filings API: the financial-results filings with their **broadcast timestamp**
(`kotsin-nse fetch-results`, kept under `data-live/events/`), 2021-01 → 2026-10 (the holdout part is
downloaded, not read). The daily cache, NIFTY, the point-in-time F&O membership. Window 2021-04-01 →
2025-09-30. A stock-quarter without a broadcast timestamp is dropped and counted (the board-meeting
calendar is kept as a cross-check, not used to fill the gap: it has no time of day).

**Definitions.**
- **Event session E** = the first session whose close follows the broadcast: a broadcast before 15:30
  IST on a session day is that day (flagged `in_hours` if after 09:15), otherwise the next session.
- **Reaction** r_E = close(E) / close(E−1) − 1 minus NIFTY's same return. **z** = r_E / σ, σ the standard
  deviation of the stock's market-adjusted daily returns over the 60 sessions ending E−1. The reaction
  stands in for the earnings surprise (there is no consensus-estimate data here).
- **Drift_k** = the market-adjusted return from close(E) to close(E+k), k ∈ {1, 2, 3, 5, 10, 20}.

**Primary rule.** |z| ≥ 2: long the stock future if z > 0, short if z < 0, from close(E) to close(E+5),
one unit per event, equal-weighted; costs 14.6 bps per round trip (the §1 figure). **Pass**: mean net
per event > 0, t ≥ 2.0 by week (events cluster in result seasons, so the unit is the week of E, the
per-event nets summed), ≥ 55 % of weeks with an event positive, both halves (2021-04 → 2023-06 /
2023-07 → 2025-09) positive. One run, one rule.

**Reported, not judged:** k = 1, 2, 3, 10, 20; the long and the short leg apart; |z| bins 1–2, 2–3, > 3;
in-hours vs after-hours broadcasts; the run-up E−5 → E−1 (does the market lean the right way before);
the mirror (fade the reaction); the realised |r_E| against the move the ATM IV at E−1 priced
(`_optsignals.parquet`: iv × √(1/252)) — a sellers' question for a later pre-registration; by the regime
at E (post-hoc, as in §6c). Before any holdout: the execution check at the 15:25 bar of E on the
5m cache (2024-01 → 2025-09), as §6c taught.

**§7 results (2026-10-25) — three runs, disclosed.** Run 1 (`events-6fe672b38a`) and run 2 (`events-95ed4d530a`,
after adding NSE's "Annual" filings, which changed nothing) used the daily cache as it stood: the stocks in F&O
*today*, membership point-in-time. 2,423 events, 780 with |z| ≥ 2: **+25.9 bps net, t 1.53, 62.7 % of weeks,
halves +26.8 / +25.2 — fails on t.** The coverage cross-check then showed 29 % of members' result dates had no
event, and 875 of those 1,000 were stocks that have since left F&O and had no daily data at all (survivorship
in the data, not the membership). Daily history was fetched for the 72 that still trade (HDFC, MINDTREE, IDFC,
GSPL, PEL are gone) and the study re-run with the rule untouched. **Run 3 (`events-f1042b6f6f`): 3,205 events,
1,014 selected — +35.3 bps net per event, t 2.36 by week, 63.3 % of weeks positive, halves +35.6 / +35.1 —
PASSES as registered** (gross +49.9, t 3.35; market-adjusted +42.4, t 3.15, 70 % of weeks; median +53.7;
hit rate 55.5 %). Reported: by hold, net 1 d +23.9 (t 2.9), 3 d +35.8 (t 3.1), 10 d +29.7 (t 1.4), 20 d +74.4
(t 2.8, halves +34 / +108); market-adjusted drift 31 → 42 → 92 bps from 1 to 20 sessions; |z| 1–2 / 2–3 / > 3
at 5 d: 13 / 38 / 46 bps, at 20 d: 17 / 72 / 107; the mirror (fade) −64.6 (t −4.3); long leg +30 (t 1.2),
short +40 (t 1.5); in-hours broadcasts +83.9 (t 2.7, n 220) vs after-hours +21.9 (t 1.4, n 794); run-up
+14 bps the right way; realised ÷ implied results move: median 0.90, 46 % above; by regime at E (post-hoc):
drift-down +65, calm +42, relief rally −36, fear −64. Missing by construction: the four insurers (results filed
under another NSE category, 64 dates) and ~160 postponed-meeting dates. **Not taken to the holdout.** First the
15:25 execution check (selection and entry on the 15:25 bar, 5m cache, 2024-01 → 2025-09), then the owner's word.

**§7 execution check (2026-10-25, `kotsin-nse events --exec-check events-f1042b6f6f`; 5m cache, 2024-01 → 2025-09, so only
today's F&O stocks — 970 events, 326 selected).** Selecting on the 15:25 bar gives the same picks as the close (z correlation
0.991, same sign 96 %) and entering there costs nothing (every hold a little better). **But on this recent, survivors-only slice the
registered 5-session hold is flat: +1.8 bps net (daily rule, t 0.06, median +31) / +7.6 traded at 15:25 (t 0.3).** 1 session: +14 /
+18 (t 1.1 / 1.7); 20 sessions: **+68 / +94 (t 1.45 / 2.5, 59 / 67 % of weeks)**. So the full-sample pass of the 5-day rule rests on
2021–2023 and on the stocks that later left F&O; the 20-session hold is the only horizon positive in every cut, and it is not
the registered rule. **Decision: the 5-day rule does not go to the holdout.** Next, if the owner agrees: §7b — the same selection
held 20 sessions, charged a roll (a second round trip) whenever the hold crosses a monthly expiry, with the pass criteria of §7;
one run.

### 7b. The same selection held 20 sessions, as a book (`research/events.py study_book`) — 2026-10-25, before any result

**Why, and what this is not.** The 20-session horizon was *chosen after looking* at §7's by-hold table and at the
execution check (the only horizon positive in every cut). So this is a second look at the same 2021–2025 data, not a
fresh hypothesis; the holdout is the only fresh data and it is not touched here. The pass bar is unchanged. If §7b
fails, the lane closes. If it passes, the owner decides whether the holdout is spent on it.

**Rule (the candidate strategy, end to end).**
- **Selection exactly as §7:** event session E from the broadcast time; z = market-adjusted reaction / σ (60 sessions
  ending E−1); |z| ≥ 2; point-in-time F&O member on E; |raw move| ≤ 50 %; a second filing within 3 sessions is the
  same event. Side = sign(z). Window 2021-04-01 ≤ E ≤ 2025-09-30 − 20 sessions.
- **Hold:** close(E) → close(E+20). Stock future, one unit of notional per name, no stop, no target, no early exit.
- **Costs:** 14.62 bps per round trip (CostModel on a ₹5 lakh future + 10 bps slippage), **plus one more round trip
  for every monthly stock-futures expiry strictly inside (E, E+20)** — the roll. Expiry = the last Thursday of the
  month (the previous session if a holiday); from September 2025 the last Tuesday (NSE's move). Entering on an
  expiry day or exiting on one needs no roll.
- **The book:** at most **20 open positions**. At the close of E, positions whose E+20 is today exit first; then the
  day's candidates are admitted **in descending |z|** while fewer than 20 are open. Turned-away candidates are
  counted and reported, never traded.

**Primary statistic and pass criteria (as §7):** the *admitted* events' net bps per event, grouped by the ISO week
of E — mean > 0, t ≥ 2.0 by week, ≥ 55 % of weeks positive, both halves positive (2021-04-01 → 2023-06-30 and
2023-07-01 → 2025-09-30, by E).

**Reported beside it (not pass criteria, written down now so none is a post-hoc rescue):**
1. The same without the cap (every selected event), with cap 10 and cap 40; the share of candidates turned away.
2. Gross, market-adjusted; the long and short legs; in-hours vs after-hours; |z| 2–3 vs > 3; by year.
3. **Carry sensitivity.** The study uses spot closes as the future's proxy. A stock future's premium decays into
   spot by roughly 40–60 bps over 20 sessions: the long leg forgoes it, the short leg earns it. Reported: net with
   −50 bps on every long and +50 on every short ("carry-adjusted"). If the carry-adjusted long leg is negative that
   is written down as a fact about the long leg, not used to drop it from the rule.
4. **The book as a P&L:** per session, the sum over open positions of side × the stock's daily return (one notional
   unit each); weekly Sharpe, the worst peak-to-trough drawdown and the longest drawdown, the average and the peak
   number of open positions, the share of sessions with an empty book.
5. By regime at E (post-hoc, as in §7).
6. The execution reference is §7's 15:25 check at hold 20 (+93.9 bps net, t 2.5, 2024-01 → 2025-09, survivors,
   no cap, no roll). It is not re-run; a reader compares after subtracting one roll (14.6 bps).

**One run** (`kotsin-nse events --book`), 2021-04-01 → 2025-09-30, cap 20. The result is appended here whatever it
says. A second run happens only for a bug in the code, with the bug named.

**§7b result (2026-10-25, one run, `kotsin-nse events --book`, `events7b-e7e5c86d8c`; 3,205 member events, 1,014 candidates, 466
admitted to the 20-position book; 57 expiries, 0.87 rolls per trade on average). Primary — admitted events, net per event:
+89.5 bps, median +104, t 2.95 by week, 62 % of 100 weeks positive, halves +121 / +60, hit rate 55.6 %. PASSES as registered.**
Gross +117 (t 3.8); market-adjusted +118 (t 3.8, 66 % of weeks); carry-adjusted +94.6 (t 3.1). By year +205 / +97 / **+0.4** / +142 /
**+6** (2025 to September): two flat years in four and a half. The cap binds: 54 % of candidates are turned away, the book is at 20
on 231 of 1,114 sessions and empty on 25 %; mean 8.4 open. Uncapped +61.5 (t 2.3, halves +21 / +96), cap 10 +127 (t 2.8), cap 40
+58 (t 2.2) — |z| priority is doing real work (|z| > 3: +110, t 2.4; 2–3: +57, t 1.3). **The legs are not alike at 20 sessions:
long +209 net (t 3.4, carry-adjusted +159, t 2.6) — short −7.7 (t −0.15, halves +22 / −39).** Market-adjusted the short leg is
+100 (t 2.1): the shorts beat the market but NIFTY rose 80 bps on average over their holds and took it all. Written down as a
fact about the legs, not a change to the rule (a hedged short leg would be a new registration). In-hours +61 (t 0.8, n 111, 2025
−319) vs after-hours +98 (t 2.7) — the §7 ordering reversed at this horizon, on few events. The book: +35.6 bps-units a session,
weekly Sharpe 0.97, 4.5 % a year on the full 20-unit notional (about 20–25 % on futures margin, before financing), worst drawdown
−9,068 bps-units (0.45 of the cap notional), longest drawdown 317 sessions (Nov 2022 → early 2024). By regime at E (post-hoc):
drift-down +109, calm +78, relief −4, fear −63. **Decision: the rule passed its one pre-registered run. Whether the holdout
(2025-10-01 → 2026-10-16) is spent on it is the owner's call; the facts he should weigh are the flat 2023 and 2025, the
long-only nature of the absolute return and the 15-month drawdown.**
