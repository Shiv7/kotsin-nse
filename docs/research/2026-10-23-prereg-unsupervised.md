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
