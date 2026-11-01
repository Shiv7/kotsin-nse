# The order-flow checklist — results (2026-11-01, Sunday evening)

Pre-registration: `2026-11-01-prereg-orderflow-checklist.md` (committed 97a43f4 before any run). Scripts and
logs: `scripts/2026-11-01-orderflow/` (`t5_t6_cvd_absorption.py` → `t5_t6.log`, `t7_footprint.py` → `t7.log`,
`t5_control_reversal.py` → `t5_control.log`). Data: the engine's archives for 27, 29 and 30 Oct 2026 —
~79k stock-minutes a day for T5/T6, ~1.75M cash-name ticks a day → ~15.8k stock × 5-minute bars for T7.
Metric: cross-sectional Spearman IC per minute (or per 5-minute bar), t over the cross-sections, placebo =
the reading shuffled within its cross-section, top-minus-bottom quintile forward return in bps.

## T5 — cumulative delta. **Fails.** The "divergence" is the price's own 30-minute reversal.

| reading | horizon | IC 27 / 29 / 30 Oct (t) | pooled t | placebo max \|t\| | q5 − q1 bps |
|---|---|---|---|---|---|
| `cvd_30` (CVD slope, 30 min) | 5 min | −0.004 (−1.0) / +0.002 (0.2) / **−0.019 (−4.7)** | −2.7 | 1.3 | +0.2 |
| | 30 min | +0.010 (2.3) / +0.015 (1.1) / −0.016 (−3.4) | −0.1 | 1.9 | 0.0 |
| `div` (rank CVD − rank price) | 5 min | +0.009 (1.8) / +0.035 (3.6) / +0.020 (4.5) | 5.6 | **3.0** | +0.5 |
| | 15 min | +0.010 (1.9) / +0.042 (3.6) / +0.016 (3.2) | 4.9 | 0.9 | +0.6 |
| | 30 min | +0.003 (0.6) / +0.052 (4.1) / +0.012 (2.8) | 4.0 | 0.6 | −1.8 |
| control: −`ret_30` alone | 5 min | +0.010 (1.9) / +0.042 (3.2) / +0.040 (7.8) | **7.4** | — | — |
| control: CVD rank with the price rank partialled out | 5 min | +0.005 (1.1) / +0.019 (1.8) / −0.005 (−1.1) | 1.0 | — | — |
| | 30 min | +0.010 (2.1) / +0.041 (3.1) / −0.006 (−1.5) | 2.3 | — | — |

- The CVD's slope on its own flips sign across the days and on Friday ran strongly *against* the claim
  (t −4.7 at 5 minutes): a stock whose last 30 minutes were net-bought drifted down relative to the others.
- `div` looked like a pass (3 / 3 days, pooled t 4–5.6), but the post-hoc decomposition (not pre-registered;
  a control, not a result) shows why: the 30-minute price change alone, sign flipped, has a *larger* IC (t 7.4).
  Partial the price rank out of the CVD rank and what is left is +0.003 at 5 minutes (t 1.0), sign flipping on
  30 Oct. **Cumulative delta says nothing the price did not already say.**
- The plain divergence cases (CVD and price in opposite quartiles, n 6,623): +0.17 / +0.26 / +0.15 bps in
  the CVD's direction at 5 / 15 / 30 minutes, 2 of 3 days, t ≤ 2.2.

## T6 — absorption. **Fails; the sign is the other way.**

| what | n (3 days) | forward return in the "absorbed" direction, bps, 5 / 15 / 30 min | days positive (5 min) |
|---|---|---|---|
| absorbed: strong one-sided volume, price did not move | 5,364 | **−0.56 / −0.44 / −0.35** | 0 of 3 |
| strong one-sided volume, price moved | 13,294 | +0.53 / +0.23 / +0.26 | 2 of 3 |

`absorb` IC at 5 minutes −0.004 / −0.008 / −0.007 (t −1.0 / −1.5 / −1.6), quintile −1.4 bps. The minutes where
one side took the volume and the price stood still were followed by a drift *with* the aggressors, not
against them. Whatever "absorption" is on a footprint chart, on this feed's minute data the trapped side is
not there, and the effect, with either sign, is under a basis point.

## T7 — footprint diagonal imbalance (300 %). **Fails.**

- **94 % of 5-minute bars contain at least one 300 % diagonal** (14,774 of 15,805 on 27 Oct, the same on
  the other days). At that threshold, on a snapshot feed, an "imbalance" is the normal state of a bar.
  Stacked imbalances (≥ 3 consecutive levels one way) occur in 4–5 % of bars each way.
- `net_imb` (buy − sell imbalances): IC +0.009 / +0.017 / −0.005 at 5 minutes (2 of 3 days, max t 2.1,
  placebo t 2.2 on 30 Oct); −0.018 / +0.005 / +0.007 at 30 minutes. Quintile spread +0.8 / +1.0 / +1.2 bps.
- `stacked`: IC ≈ 0 at every horizon. Stacked-up bars against stacked-down bars, pooled: 5 min +0.13 vs
  +0.85 bps, 15 min +1.35 vs +1.35, 30 min +1.80 vs +2.56 — no difference, and the bars with no stack are the
  same. Each day's number is the day's drift (29 Oct: everything +7 bps at 30 minutes; 30 Oct: everything −3).
- The bar's own delta share at the 5-minute grain: −0.001 / −0.004 / −0.009, 1 of 3 days — T1's reversion in
  sign, not significant at this grain.

Caveat, stated once: the ticks are 5paisa's snapshot stream (every quote change; ~1.75M a day across 224
cash names; receive-minus-exchange lag median 0.9 s), not an exchange trade print, so the footprint books
each snapshot's new volume at its last price by the quote rule. A true trade feed would be sharper. The
estimated delta from the same source does carry T1's consistent effects, so the data is not empty; it is the
concept that does not show.

## Side finding (post-hoc; a candidate for a pre-registered test, not a result)

Cross-sectional 30-minute price reversal: −`ret_30` → next 5 minutes IC +0.028 (pooled t 7.4; by day 1.9 /
3.2 / 7.8), fading to +0.012 at 30 minutes and negative on 27 Oct there. `div`'s top-minus-bottom quintile
was 0.5 bps, so even this is pennies against the 13 bps round trip. Not to be used; it goes on the list.

## The checklist, closed

| item (the comment's order) | recorded | verdict |
|---|---|---|
| 1–2. footprint imbalance 300 % | derivable from `ticks` | **fails** — 94 % of bars have one; stacked bars do nothing |
| 3. delta | `flow.delta` | **reverts** ~1–3 bps over 5–30 min (30 Oct T1) — a timing aid, not an entry |
| 4. cumulative delta | `flow.cum_delta` | **fails** — the slope flips sign; the "divergence" is the 30-min price reversal |
| 5. DOM | 5 levels a side (`depth`, `flow.wimb/qi/micro_bps`) | **leads** ~2 bps over 5–30 min (30 Oct T1) — a timing aid |
| 6. absorption | derivable | **fails** — the sign is the other way, under a basis point |
| 7. tape | `ticks` | the delta's source |

Nothing from the checklist enters the strategy. The two readings that do something (the book leads, the
tape reverts) were already in the engine's `context` on every signal, as timing. The ₹83 L P&L in the
screenshot is a picture of a screen; it is not evidence of any of this.
