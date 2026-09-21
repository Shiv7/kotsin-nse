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

## 8. Artefact — 2026-09-21, real data, and the answer is no

**Run `bt-875d519a3777`** (`docs/backtests/`): 24 liquid NSE F&O underlyings on their cash series,
1 year of 5paisa 30m history (2025-09-22 → 2026-09-21), the inherited parameters, through this
repo's live `Fudkii` → `ExitEngine` → `CostModel` code. Pessimistic fills: next-bar open plus
slippage, stop assumed first when a bar covers both, stop fill worse than the stop price.

| | |
|---|---|
| trades | **481** over 194 days (not a small sample) |
| win rate | **17.3%** |
| avg R | **−1.40 ± 0.18** (day-clustered), **t = −7.96** |
| gross / charges / net | −29,834 / 62,220 / **−92,054** — it loses *before* costs; charges are 209% of \|gross\| |
| exits | SL-EQ 385 (80%) · TIME_STOP 74 · TARGET 21 · EOD 1 |
| profit factor / max DD | 0.40 / −91,777 |
| modelled option leg | −626,630 (a model, not a measurement — but the sign matches the old book's "OTM calls lose −5.47%/trade") |

### The falsifier in §1 fired — inverted

| grade | n | avg R | win |
|---|---|---|---|
| **A** | 345 | **−1.73** | 13.6% |
| B | 69 | −0.72 | 26.1% |
| C | 67 | −0.41 | 26.9% |

Grade A is the *worst* bucket. The grade is anti-informative, which is this strategy's own kill
condition.

### Mechanism, measured

* median confluence stop **0.23%** from entry (p25 0.14%, p75 0.35%); **89%** of stops < 0.5% away
* 1R at ₹1L size = **₹225**; round-trip charges = **0.57R**
* median MFE **+1.08R**, median MAE **−1.44R**, median hold **1 bar** — 1R is inside a single 30m
  bar's noise
* the 5 trades (1%) with a stop ≥ 1% away averaged **+0.15R**

"Nearest of ~36 pivot lines" is a noise stop, not a structural one; grade A is high-RR precisely
because the risk is tiny, so the grade selects for the stops least likely to survive.

### Exploratory variants (hypotheses, not fixes — `GradePolicy.min_stop_atr`, `stop_requires_wall`)

| variant | trades | win | avg R | t | net | charges/\|gross\| |
|---|---|---|---|---|---|---|
| baseline (inherited) | 481 | 17.3% | −1.40 | −7.96 | −92,054 | 209% |
| stop floor 1.0 ATR | 320 | 34.7% | −0.31 | −3.59 | −49,694 | 498% |
| stop must be a wall | 405 | 20.2% | −1.17 | −6.39 | −72,026 | 267% |
| 1.0 ATR floor + wall | 303 | 34.7% | −0.30 | −3.01 | −47,585 | 467% |
| stop floor 1.5 ATR | 199 | 36.7% | −0.27 | −3.02 | −42,465 | 154% |

The review committee's experiment loop (`docs/COMMITTEE.md`) reproduced the first row on
2026-09-22 without a model in the loop — hypothesis `fudkii.grade_policy.min_stop_atr = 1.0`,
graded by the backtester on the 28 cached symbols: baseline 532 trades −1.36 R (t −7.97) →
patched 364 trades **−0.34 R** (t −4.12), Δ +1.02 R, within-day permutation p = 0.0002, 13 s.
"Confirmed" there means *the change helps*, and in-sample on the same data the diagnosis came
from; it does not mean the strategy is positive.

Re-graded **out of sample** the same night, once the loop learned to hold data back
(`docs/COMMITTEE.md`): holdout 2026-06-04 → 09-21, baseline 196 trades −1.32 R → patched 146 trades
**−0.57 R**, Δ +0.75, within-day permutation p 0.0002, survives ×1.5 brokerage / ×2 slippage (Δ +0.71),
patched wins all 4 months. In sample the same change read Δ +1.20 — the gap is the optimism a
same-data grade carries. Real, robust, and still a losing strategy (t = −5.1).

**Daily bars, delivery, long-only** (`kotsin-nse backtest --decision-tf 1d --holding delivery`,
run `bt-0f1ff74a261d`): 24 trades in a year across 28 symbols, −0.78 R (t −1.55, too small to
conclude), 33 % wins, charges 148 % of gross, 15 bearish signals refused (no overnight shorts in
retail cash equity), `st_flip` the binding gate 5,589 times — the SuperTrend flip and the Bollinger
break rarely coincide on daily bars. Slower does not make it better; it makes it rare.

Widening the stop shrinks the loss but never turns it positive; gross converges on zero and
charges decide the sign. **The entry has no edge; the stop only sets how fast it is paid for.**
That is the same conclusion `kotsin-box/SESSION-PRIMER.md` reached on CAN2 ("entries, not exits").

### What this does not say

* It is measured on the **underlying**. The option leg is modelled.
* No OI in REST history, so nothing here tests OI-gated variants.
* FUKAA is not tested by this run at all — see FUKAA.md §9.

**Do not trade this with money as it stands.**

## 9. Open items

| Item | Type | Impact |
|---|---|---|
| the inherited entry has no measured edge | **falsified** | see §8 — a different entry is needed, not a different exit |
| delta is estimated, not observed | approximation | the option stop/target projection is crude; the chain does not publish a Greek on this feed |
| the ATR 14 → 7 change is uncited | unvalidated | a core parameter with no recorded reason |
| grade distribution unmeasured here | unknown | the gate counters will answer it on the first session |
