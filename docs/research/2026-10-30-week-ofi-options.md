# The week's order flow, options board and shadow trades — results (2026-10-30, evening)

Pre-registration: `2026-10-30-prereg-week-ofi-options.md` (written first). Scripts and logs:
`scripts/2026-10-30-week/` (`t1_micro_ic.json`, `t1_placebo.log`, `t2.log`, `t3.log`, `t4.log`).
Data: the engine's own archives for 27, 29 and 30 Oct 2026 (28 Oct lost to the laptop's sleep).

## T1 — does the recorded order flow predict the next minutes? **Yes, by 1–3 bps. Not tradable.**

Cross-sectional rank IC per minute (≈ 200 stocks a minute, ≈ 330 minutes a day), `kotsin-nse micro-ic`
on 27–30 Oct, placebo = the reading shuffled within its minute (`t1_placebo.py`, three full days).

| reading (last minutes) | 5 min IC | 30 min IC | days with the sign | pooled t (5 / 30) | placebo max |t| | top − bottom quintile, 30 min |
|---|---|---|---|---|---|---|
| **weighted book imbalance** `wimb` | +0.015 | +0.022 | 3 / 3 | 9.9 / 6.7 | 1.9 | **+2.2 bps** |
| **top-5 imbalance** | +0.014 | +0.021 | 3 / 3 | 9.8 / 13.8 | 1.0 | +2.2 bps |
| microprice `micro_bps` | +0.010 | +0.020 | 3 / 3 | 6.5 / 2.5 | 1.9 | +3.4 bps |
| queue imbalance `qi` | +0.008 | +0.019 | 3 / 3 | 3.7 / 2.0 | 1.2 | +2.8 bps |
| OFI / depth (trade flow) | **−0.017** | −0.011 | 3 / 3 (negative) | −4.2 / −4.9 | 1.7 | −1.1 bps |
| deep OFI / depth | −0.018 | −0.013 | 3 / 3 (negative) | −3.2 / −7.2 | — | −1.3 bps |
| delta share (buy − sell) | −0.028 | −0.009 | 3 / 3 (negative) | −3.3 / −0.9 | 1.8 | −2.7 bps |

Two real, consistent microstructure effects, both passing the pre-registered bar (|t| ≥ 3, same sign every
day, placebo |t| < 2):

1. **The book leads.** Where the resting bid side is heavier than the ask (weighted imbalance, top-5
   imbalance, microprice above mid), the stock drifts up over the next 5–30 minutes relative to the others.
2. **The tape reverts.** Where the last minutes' *trades* were aggressive buys (positive delta, positive
   OFI), the stock drifts *down* relative to the others over the next 5–30 minutes. Aggression is paid for.

The size: the top quintile beats the bottom quintile by **about 2 bps over 30 minutes** (3.4 at best, for
the microprice). A round trip at ₹1L costs 13 bps, at ₹25k 41. These readings are a tie-breaker and a
timing aid (enter with the book, not into it), not a trade. The 19–20 Oct one-day run said the same with
less data.

## T2 — does the morning board say anything about the rest of the day? **Nothing passes. Two leans.**

218 roots × 3 days, the stock's return from the mark to 15:14 demeaned across stocks per day (`t2.log`).
Bar: same sign on all three days and pooled |t| ≥ 2.5.

| reading at the mark | n | demeaned return to the close | per day | t | verdict |
|---|---|---|---|---|---|
| futures in **long build-up at 12:00** | 169 | **+14.1 bps** | +29 / +11 / +15 | 2.14 | lean, below the bar |
| futures OI change since the open, top quintile at 12:00 | 134 | +18.7 | +13 / +28 / +15 | 2.28 (q5 − q1 +28, t 2.1) | lean, below the bar |
| puts in long build-up at 12:00 | 179 | +12.0 | +20 / +14 / +8 | 1.76 | lean |
| puts in short covering at 12:00 | 55 | −26.9 | +89 / −26 / −39 | −2.56 | sign flips: noise |
| everything at 10:15 | — | — | signs flip | < 1.8 | noise |
| PCR change, IV change (quintiles) | — | — | non-monotonic | < 1.7 | noise |
| board bias (Strong Bull/Bear) | — | ±4 | signs flip | < 0.7 | noise |

The one coherent lean: **OI still being added to the future by noon, with price up (a long build-up), carries
into the close by ~15 bps** on all three days. It agrees in sign with the September daily study (a long
build-up day is not to be shorted). It is three days; the bar says no. Re-test when ten sessions exist.

## T3 — the week's 14 live shadow trades against the readings (descriptive, n = 14)

`t3.log` has the full table. What it shows, with the caveat that fourteen trades decide nothing:

- **With the board, 29 Oct:** the five longs taken with the future in an intraday long build-up, calls in
  long build-up and a "Strong Bull" board averaged **+95 bps** (TATAPOWER, ETERNAL, JSWSTEEL, TIINDIA;
  SHREECEM stopped). The 27 Oct long with the same board (CROMPTON, calls LB, Strong Bull) lost −56.
- **Against the board, 30 Oct:** both shorts were taken with the future in a short build-up (with) but
  **into the tape**: ADANIPOWER's last 15 minutes before the entry had +184k delta (buy/sell 2.2) — the
  short was sold into aggressive buying and stopped in one bar. LUPIN's delta was −5.7k (neutral).
- **The previous session's official build-up:** ADANIPOWER was shorted after a *long build-up* day
  (OI +1.2 %, price +4.2 %) — the September gate's exact case (shorts after a long build-up, −0.15 R);
  the 29 Oct longs came after *long-unwinding* days (OI −4 to −27 %) and averaged +77 (4 trades). This is
  what the daily study said: no prediction alone, a gate against trading against yesterday's build-up.
- NIFTY's position at the entry: winners 0.66, losers 0.51 — and T4 below says it is nothing.

Every signal now carries these readings (`context`, `bhav`), so this table writes itself each evening.

## T4 — the shorts at NIFTY's low (dev window, 3,100 base-rule instances). **Fails.**

| split | n | net | win | t (by day) | 2024 | 2025 |
|---|---|---|---|---|---|---|
| all | 3,100 | −11.8 | 40 % | −7.6 | −11.6 | −12.0 |
| kept (with / neutral) | 1,642 | −11.5 | 40 % | −5.1 | −10.2 | −13.1 |
| dropped (short with NIFTY ≤ 0.2 of its range, long ≥ 0.8) | 1,458 | −12.2 | 39 % | −6.4 | −13.3 | −10.9 |

Kept − all = +0.3 bps (bar ≥ 3); the years disagree. The shorts taken with NIFTY in the bottom fifth of
its range lose −13.0 (n 832) against −12.2 in the next fifth and +1.6 in the middle fifth — no gradient
worth a rule. This week's five stopped shorts at NIFTY's low are five.

## What is actual and correct, in one place

1. The book's resting imbalance predicts the next half hour; the tape's aggression predicts a reversal.
   Both are consistent every day and survive the placebo. Both are worth ~2 bps: **use them for timing an
   entry the strategy already wants, never as a reason to enter.**
2. Nothing on the morning F&O board predicts the rest of the day on three days of data. The noon long
   build-up lean (+15 bps to the close, every day) is the one thing worth re-testing at ten sessions.
3. The daily official build-up remains what September found: a gate, not a signal. This week's one short
   after a long-build-up day was the week's worst trade.
4. NIFTY's position in its range at the entry is not a filter (dev window).
5. The strategy itself is unchanged: 14 live trades, +36.7 bps a trade gross this week (29 Oct carried it);
   −10 net on history.
