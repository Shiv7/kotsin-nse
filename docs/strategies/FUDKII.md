---
strategy_key:   FUDKII
display_name:   FUDKII
owner:          backend/kotsin_nse/strategy/fudkii.py
segments:       NSE (cash-signalled, option-traded) · MCX (future-traded)
instrument:     OPTION (OTM, anchored on confluence T1) · MCX → front-month FUTURE
timeframe:      30m
status:         BUILT — never traded
doc_verified:   2026-09-20
---

# FUDKII

## 1. Thesis

On a 30-minute chart, a SuperTrend flip landing on the same bar as a close outside the Bollinger
band marks a regime change with momentum behind it: trend reversal **and** volatility expansion at
once. The trade is expressed as an OTM option chosen so it is roughly at the money by the
confluence T1.

**Falsifier.** If signals graded A/B stop outperforming C, the geometry that produces the grade has
stopped carrying information and the book is paying option premium for noise.

## 2. Inputs

| Input | Source | Missing → |
|---|---|---|
| 30m OHLCV | ticks bucketed by trade time on the session grid; REST backfill at boot | `history` gate FAIL_CLOSED |
| MTF pivot zones | daily / weekly / monthly OHLC from the daily series, clustered | no zones → grade `F` → recorded rejection |
| ATR(14) on the decision frame | `bars/indicators.atr` (Wilder) | no signal; indicators-not-warm rejection |
| Session phase | `market/session` | — |

## 3. Indicators

### SuperTrend(7, 3)

ATR is **Wilder's RMA**, seeded with a simple mean of the first `p` true ranges. Bands ratchet: the
upper may only fall while the trend is down, the lower may only rise while it is up.

The whole series is recomputed from the trailing window on every evaluation. The Java original
persisted `SuperTrendState` to Mongo and then recomputed from 200 bars anyway, precisely so a
restart could not change a signal; a pure function makes that free.

> The original's class javadoc said `SuperTrend(10,3)`. The code ran **7**. Both period and
> multiplier are arguments here, and a test asserts that changing them changes the output.

### Bollinger(20, 2.0)

Population σ (divide by `n`), from closes. That is what `BBSuperTrendCalculator` computed, and
matching it matters more than being statistically fastidious because the live thresholds were
fitted against it.

### Trigger score

| Component | Points | Condition |
|---|---|---|
| `ST_FLIP` | +50 | the SuperTrend direction changed on this bar |
| `BB_BREAK` | +50 | close outside the band **on the side the trend points** |

`require_both = true` ⇒ threshold **100**. A flip alone or a break alone never fires.

### Confluence (`bars/pivots.py`)

Daily (weight 4.0), weekly (3.2) and monthly (2.0) pivots — standard levels plus Fibonacci at half
weight; Camarilla levels are populated for display and carry **zero** weight (disabled 2026-04-13).
Levels within 0.25% of each other merge into a zone whose strength is the sum of its members'
weights. A zone is a **wall** at strength ≥ 5.2 — i.e. one daily level is not a wall; a daily plus
a weekly is.

* **Stop** = the nearest zone behind the close, tick-rounded, strength-agnostic. With nothing
  behind, it falls back to 1 ATR and the note says so — the old engine produced a stop of zero
  there, which the executor read as "no stop".
* **Targets** = the next walls ahead, with an outward round-number snap **capped at 20% of the
  original distance**, so a cosmetic rounding can never change the reward:risk of the trade.
* **Grade** from RR, the strength of the first wall ("fortress") and the room to the next one.

## 4. Parameter register

Every value below is read by the code. A test changes one and asserts the output changes.

| Parameter | Value | Note |
|---|---|---|
| `tf` | `30m` | boundaries at :15 / :45 IST on NSE, :00 / :30 on MCX |
| `bb_period` / `bb_mult` | 20 / 2.0 | population σ |
| `st_atr_period` / `st_mult` | 7 / 3.0 | Wilder ATR; **7, not the javadoc's 10** |
| `require_both` | `true` | ⇒ score threshold 100 |
| `flip_max_bars_ago` | 0 | see §6 |
| `warm_bars` | 50 | hard minimum is `max(bb, atr) + 1 = 21` |
| `room_atr_period` | 14 | for the confluence room ratio, not for SuperTrend |
| `grade_floor` | `true` | `F` is blocked and **recorded** |
| `eod_strong_only` / `eod_min_fortress` | `true` / 10.0 | the last bar of the session |
| zone tolerance | 0.25% | `bars/pivots.ZONE_TOLERANCE_PCT` |
| wall minimum strength | 5.2 | live value from the old stack |
| `rr_hard_floor` / A / B / C | 1.0 / 2.5 / 1.8 / 1.2 | `GradePolicy` |

## 5. Entry conditions

| # | Condition | Type |
|---|---|---|
| 1 | ≥ 21 bars of history (50 for a clean ATR warm-up) | hard, FAIL_CLOSED |
| 2 | SuperTrend flipped on this bar | +50 |
| 3 | close outside the band on the trend's side | +50 |
| 4 | score ≥ 100 | hard |
| 5 | confluence grade ≠ `F` | hard (publish gate) |
| 6 | on the last bar of the session, fortress ≥ 10 | restriction |

Everything that fails is written to the `rejections` table with the gate that killed it.

## 6. Deliberate differences from the original

| Change | Why |
|---|---|
| `bb_period` / `st_atr_period` are read | they were bound into fields that appeared only in a startup log line while the calculator used hardcoded constants; the NSE default coincidentally matched, so the config *looked* correct |
| the 10-minute flip debounce is gone | it compensated for SuperTrend state that could be lost or raced across restarts. A pure recomputation cannot disagree with itself, so a flip either happened on this bar or it did not. The knob remains as `flip_max_bars_ago`, defaulted to the strict reading |
| `fudkii.router.enabled` / `.shadow.only` are absent | they read like a two-level kill switch, were populated by Spring, and were referenced **exactly once each: their own declaration**. They gated a router later scoped to a different book |
| grade `F` is a recorded rejection | ~61% of graded signals were `F` in the live log and nothing counted them, so nobody could say whether the RR floor was mis-set |
| the target snap is capped | an uncapped snap could move a target 9.5% when it was 0.5% away, turning a 1:1 trade into a fictional 10:1 one |
| the stop falls back to 1 ATR, loudly | the old engine emitted a stop of zero when no zone sat behind the close |
| option enrichment does not block publication | the old enricher fetched a live LTP inline, took 3–23 s on a cache miss, and logged "price may be stale at publish time" past 10 s |

## 7. Exits

Owned entirely by `risk/exits.py` — see that module. Option stop, underlying stop, hard floor,
T1–T4 ladder (40/30/20/10), breakeven after T1, peak trail armed at +3% with 40% giveback, time
stop, segment force-flat. The stop only ever tightens.

## 8. Artefact

**None.** No backtest has been run against this implementation. The parameters are inherited from
a live book that itself carried no located backtest artefact for its BB/ST periods — the ATR period
was changed 14 → 7 "per user request" with no citation in the code.

Before this trades real money it needs: a backtest over ≥ 1 year on ≥ 50 symbols, a within-day
permutation test, ≥ 300 out-of-sample trades, and a net edge after the cost model in
`risk/costs.py`. The cost model says the bar is roughly **0.3% per round trip at ₹33,000 and 0.1%
at ₹1.3 lakh** — which is why `risk/limits.py` defaults to the larger size.

## 9. Open items

| Item | Type | Impact |
|---|---|---|
| no backtest artefact | unvalidated | every parameter is inherited, not justified |
| delta is estimated, not observed | approximation | the option stop/target projection is crude; the chain does not publish a Greek on this feed |
| the ATR 14 → 7 change is uncited | unvalidated | a core parameter with no recorded reason |
| grade distribution unmeasured here | unknown | the gate counters will answer it on the first session |
