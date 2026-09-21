---
strategy_key:   FUKAA
display_name:   FUKAA
owner:          backend/kotsin_nse/strategy/fukaa.py
derives_from:   FUDKII
segments:       NSE · MCX
instrument:     the same contract FUDKII resolved
timeframe:      30m
status:         BUILT — never traded
doc_verified:   2026-09-20
---

# FUKAA

## 1. Thesis

A SuperTrend flip with a Bollinger break is a real event, but base FUDKII takes it whether or not
anyone participated. FUKAA requires the 30-minute volume to run a multiple of its recent baseline
on either the trigger bar or the one before it: volume that large is hard to fake and marks genuine
commitment to the break. It also allows the confirmation to arrive **one bar late** — a signal that
fires without volume is kept `WATCHING` for 35 minutes and promoted if the next bar delivers.

**Falsifier.** If volume-confirmed FUDKII signals stop outperforming unconfirmed ones, the filter
is only reducing sample size.

## 2. Derived, not duplicated

FUKAA consumes a FUDKII `Signal` and never recomputes Bollinger or SuperTrend. The old service
published both books from inside one 6,732-line trigger class, which is how a comment describing
their interaction survived two months after the interaction was deleted.

## 3. Volume surge

```
baseline = mean(volume[T-2 … T-(N+1)])   with N = avg_bars = 6, floored at 1000
surge_T   = volume[T]   / baseline
surge_T-1 = volume[T-1] / baseline
pass      = max(surge_T, surge_T-1) ≥ multiplier(exchange)
```

Two properties that are easy to get wrong and were:

* **T-1 is excluded from its own baseline**, so neither candidate bar can inflate the average it is
  judged against;
* **both surges share one baseline.** "The higher of the two" only means something if both are over
  the same denominator. A test pins this.

No cap on the surge — extreme surges *are* the signal. Insufficient history **fails closed**.

## 4. Volume multipliers — the dead-config story

| Exchange | Here | Old stack | |
|---|---|---|---|
| NSE (`N`) | **4.0×** | 4.0× | intended and effective |
| MCX (`M`) | **2.0×** | **1.0×** | ⚠ see below |
| CDS (`C`) | 2.0× | 2.0× | |

`fukaa.trigger.volume.multiplier=4.0` was set in `application.properties` and **read by nothing**.
The code read three *other* keys — `.nse`, `.mcx`, `.currency` — none of which were set, so all
three came from their code defaults. NSE's default coincidentally equalled the configured value, so
the config looked correct. MCX's default was **1.0**, meaning a bar at its own average volume
passed a gate called "volume confirmation".

The Mongo audit proved it rather than inferring it: `fukaa_audit` stamped the multiplier used on
every candidate, `ex=M volumeMultiplier=[1]`, and **11 of 16 MCX passes would have failed a 4.0×
bar** (`SILVERM 1.54×`, `GOLDGUINEA 1.54×`, `GOLDTEN 1.71×`, `NATGASMINI 1.90×`, `GOLDM 2.02×`).
The measured passing surge across the book averaged 7.47× with a maximum of 38.34×.

> **The MCX 2.0 here is a decision, not a recovered value.** 1.0 was an accident and is not a gate;
> 4.0 is NSE's number and MCX volume is structurally thinner. 2.0 is a placeholder that **needs
> recalibration** against MCX history before this book trades a commodity. It is listed as an open
> item below rather than buried in a default.

## 5. Conviction matrix (`strategy/conviction.py`)

Five factors, 0–100: OI change, OI buildup, volume surge, price-change ÷ ATR, and reward:risk.
Thresholds are per exchange and were fitted separately:

| Threshold | NSE | MCX | CDS |
|---|---|---|---|
| volume STRONG / BASE / GATE | 3.0 / 1.5 / 0.9 | 2.0 / 1.0 / 0.9 | 2.5 / 1.5 / 0.9 |
| OI VERY_HIGH / HIGH | 300 / 150 | 200 / 100 | 200 / 100 |
| Δprice ÷ ATR STRETCHED / EXTENDED | 0.8 / 1.2 | 1.2 / 2.0 | 1.0 / 1.5 |
| reference-OI floor | 5.0 | 8.0 | 3.0 |

Two notes carried over verbatim because they are load-bearing:

* the matrix's volume gate is **0.9×**, which is *not* FUKAA's 4× entry filter — different bars,
  different purposes, and conflating them is an easy mistake;
* `OI_HIGH` on NSE is **150**, the same number FUDKOI used as its threshold. One calibration point,
  two consumers.

Momentum scores **lower** when a move is already extended: it has spent most of the range the
target needs. Tiers S1–S4 trade; S5–S6 skip. An unknown exchange code falls back to the **strictest**
table and says so — FUDKOI's `default -> false` dropped anything that was not N/M/C with no log line.

## 6. Parameter register

| Parameter | Value | Note |
|---|---|---|
| `volume_multiplier_nse` / `_mcx` / `_cds` | 4.0 / 2.0 / 2.0 | all three read; MCX needs recalibration |
| `avg_bars` | 6 | baseline `T-2 … T-7` |
| `avg_volume_floor` | 1000 | a dead scrip's 3-share average must not manufacture a 400× surge |
| `watching_ttl_minutes` | 35 | T+1 promotion window |
| `composite_min` | 60.0 | conviction floor |
| `rr_floor` | 0.5 | |
| `require_ref_oi` | `true` | |OI change %| ≥ the per-exchange floor |
| `tier_floor` | S4 | S5/S6 skip |
| `top_n` / `max_same_direction` | **`None` (OFF)** | were `999` sentinels disabling a stage named "Top-N selection" |

## 7. Entry conditions

Inherited: FUDKII fired — flip + break, score ≥ 100, grade ≠ `F`.

| # | Gate | On missing |
|---|---|---|
| 1 | ≥ `avg_bars + 2` bars | FAIL_CLOSED |
| 2 | volume surge ≥ multiplier on T or T-1 | FAIL_CLOSED — or **park for T+1** |
| 3 | composite ≥ 60 | FAIL_CLOSED |
| 4 | RR ≥ 0.5 | FAIL_CLOSED |
| 5 | |OI change %| ≥ the exchange floor | FAIL_CLOSED |
| 6 | conviction tier S1–S4 | FAIL_CLOSED |

Note gate 5: **cash equity has no OI**, so the value comes from the front-month future of the same
underlying, resolved from today's scrip master. Reading it off the cash segment is what pinned one
strategy's OI term at exactly zero for its entire life.

## 8. Removed

| Removed | Why |
|---|---|
| the bare `volume.multiplier` key | read by nothing; see §4 |
| `top.n = 999`, `max.same.direction = 999` | sentinels disabling the stage they named |
| the cross-strategy dedup comment | described behaviour excised 2026-06-24. Both books co-trade deliberately; `risk/exposure.py` is the guard that makes that survivable |

## 9. Artefact

**Structurally untestable in the current backtester.** The 1-year run in FUDKII.md §8 shows FUKAA
with 0 trades — but that is not evidence about FUKAA: REST history carries no open interest, so
`ref_oi` (FAIL_CLOSED) never opens and the composite is missing its OI score. Testing FUKAA needs
an OI history (the live archive will accumulate one) and then its own run. Until then: **no
artefact, and the base signal it filters has been falsified (FUDKII.md §8).**

**Inherited caveat.** Same position as FUDKII, plus one specific debt: the MCX multiplier needs fitting before
a commodity trade. The honest baseline is the measured distribution — average passing surge 7.47×,
maximum 38.34×, with a thin tail at 1.5–2.0× that only existed because the gate was 1.0.

## 10. Open items

| Item | Type | Impact |
|---|---|---|
| MCX multiplier is a placeholder | **unvalidated** | 2.0 chosen by judgement; fit it on MCX history |
| `ref_oi` definition | unverified | implemented as \|OI change %\|; the original quantity was not traced |
| no backtest artefact | unvalidated | inherited parameters |
| T+1 promotion is untested live | unvalidated | the logic is unit-tested; the edge is not measured |
