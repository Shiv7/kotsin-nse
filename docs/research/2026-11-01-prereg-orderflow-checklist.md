# Pre-registration — the order-flow checklist (2026-11-01, Sunday evening)

Owner's ask (1 Nov, 18:1x IST): a screenshot of an Instagram comment listing seven order-flow readings —
footprint imbalance ("buyer or seller ke beech 300 % se zyada ka difference"), delta, cumulative delta,
DOM, absorption ("kaun phasa"), tape — "can we check this, did we try this". The P&L in the screenshot
(₹83.4 L realised, Oct 2026) is a picture; it is not evidence of anything and is not used.
Written before any test ran.

## What is already answered (30 Oct, `2026-10-30-week-ofi-options.md` T1)

| item | recorded? | tested? | result |
|---|---|---|---|
| 3. delta | yes, per minute by the quote rule (`flow.delta`) | yes | **reverts**: IC −0.017 (5 min) / −0.011 (30 min), 3/3 days, placebo clean; −1 to −3 bps |
| 4. cumulative delta | yes (`flow.cum_delta`) | the slope no, the divergence no | this doc, T5 |
| 5. DOM | 5 levels a side for ~570 codes (`depth`, `flow.wimb/qi/micro_bps/imbalance`); 5paisa's 20-level gateway refuses this account | yes | **leads**: IC +0.015 (5 min) / +0.022 (30 min), 3/3 days; +2.2 bps top − bottom quintile |
| 6. absorption | derivable (delta against the price move) | no | this doc, T6 |
| 7. tape | every snapshot (`ticks`: ltp, last/total qty, bid, ask) | as the delta's source | — |
| 1–2. footprint imbalance 300 % | derivable from `ticks` (volume by price by side) | no | this doc, T7 |

## Data

The engine's own archives, full sessions **27, 29 and 30 Oct 2026** (28 Oct lost to sleep; 26 Oct a half
session, excluded as in T1). `flow` + `bars` for T5/T6; `ticks` + `bars` for T7. Forward returns from the
1m close grid, as `micro_ic.day_frame` builds them (a reading is known at the end of its minute; the
return runs from that minute's close).

## Tests

**T5 — cumulative delta.** Per stock and minute: `cvd_30` = Σ delta over the last 30 minutes ÷ Σ volume
(the CVD's slope, normalised); `ret_30` = the 30-minute price change; `div` = rank(cvd_30) − rank(ret_30)
within the minute's cross-section (price lagging its CVD: positive when the CVD is stronger than the price).
The checklist's claim is that price follows the CVD: *IC(cvd_30) > 0 and IC(div) > 0* on the next
5 / 15 / 30 minutes. Also the plain divergence cases (cvd_30 and ret_30 of opposite sign, both beyond the
cross-section's quartiles): mean forward return in the CVD's direction, bps, t.

**T6 — absorption.** Per stock and minute: *strong* = minute volume ≥ the stock's trailing-60-minute 80th
percentile **and** |delta share| ≥ 0.5 (one side took the minute); *quiet* = |1-minute return| ≤ the stock's
trailing-60-minute median. `absorb` = −sign(delta) when strong ∧ quiet, else 0 (aggressors hit a wall: the
other side absorbed them, so price should go the other way). Claim: *IC(absorb) > 0*, and the absorbed
minutes revert **more** than the strong-but-moving minutes (T1 already says aggression reverts; absorption
has to beat that, not just repeat it): mean forward return × (−sign delta), bps, for strong ∧ quiet versus
strong ∧ not quiet, with t.

**T7 — footprint diagonal imbalance.** From the ticks: each snapshot's new volume (Δ total quantity) at its
last price, side by the quote rule (at or above the ask = buy, at or below the bid = sell, between = dropped).
Per 5-minute bar and price level: buy volume, sell volume. A *buy imbalance* at price p: buy(p) ≥ 3 ×
sell(next level below) with that sell > 0 (the 300 %); a *sell imbalance* at p: sell(p) ≥ 3 × buy(next level
above). Readings per bar: `net_imb` = buy imbalances − sell imbalances; `stacked` = +1 / −1 when ≥ 3
consecutive levels are imbalanced one way, else 0. The checklist's claim is continuation: *IC > 0* on the
next 5 / 15 / 30 minutes from the bar's close, and stacked +1 bars outperform stacked −1 bars.

**Metric and bar (T1's).** Spearman rank IC per cross-section (minute or 5-minute bar, ≥ 30 stocks), mean
per day, t over the cross-sections; *counts as a pattern only if* pooled |t| ≥ 3 **and** the same sign on
all three days **and** the placebo (the reading shuffled within its cross-section) |t| < 2. *Counts as
tradable only if* the top-minus-bottom quintile forward return ≥ 13 bps at the horizon (the ₹1L round trip).

**Decision.** A pass at a few bps is, like T1, a timing aid for an entry the strategy already wants — nothing
is added to the rule from three days; it goes on the ten-session re-test list. A fail closes the checklist
item. The sign is reported as found, including a sign opposite to the checklist's claim.

Scripts and logs: `scripts/2026-11-01-orderflow/`.
