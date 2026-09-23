# Pivots — the frozen contract

Frozen 2026-09-23 after auditing the levels against NSE bhavcopy and the old stack. Everything
below is pinned by `tests/test_pivot_freeze.py` (formula and period selection, on RELIANCE's real
September sessions) and `tests/test_daily.py` (the data plane). Change any of it and a test fails
first.

## 1. Formula

Classic (Kite) convention — `bars/pivots.py::classic_pivots`:

```
P  = (H + L + C) / 3          R1 = 2P − L        S1 = 2P − H
R2 = P + (H − L)              S2 = P − (H − L)
R3 = P + 2(H − L)             S3 = P − 2(H − L)
R4 = P + 3(H − L)             S4 = P − 3(H − L)
BC = (H + L) / 2              TC = 2P − BC       (sorted: TC is always the upper edge)
```

**Classic only.** Fibonacci and Camarilla levels are still computed (`classic_pivots`) for display,
but neither enters the confluence engine: `pivot_points` emits only the eleven classic levels, and
every stop, target, wall, option-ladder rung and ratchet step is downstream of it (Camarilla off
since 2026-04-13, Fibonacci off since 2026-09-23).

`P, R1/S1, R2/S2, CPR` are identical across every platform. Only the outer
rungs differ from the floor-trader form (`R3 = H + 2(P − L)`); the dashboard, Kite and this engine
all use the Classic form. `kotsin-nse pivots SYMBOL --for DATE` prints both so a number from anywhere
can be matched.

## 2. Inputs — the only accepted source

**The broker's own daily candle** (`V2/historical/…/1d`, `BarSource.REST`). It equals NSE bhavcopy
to the paisa. Never a bar rolled up from intraday candles (5paisa's stop at 15:15, so the close and
any late-session high/low are missing) and never a bar the aggregator built from ticks (its close is
the last print, not the exchange's closing price). `bars/daily.py::is_official` is the gate.

| timeframe | source period | selection |
|---|---|---|
| daily | the previous **trading** session | last REST bar dated strictly before today (`previous_session`) |
| weekly | the previous completed Mon–Fri | `periods.weekly` (ISO week), running week excluded by calendar |
| monthly | the previous completed calendar month | `periods.monthly`, running month excluded by calendar |
| future | daily | same rule, front contract |
| options | daily (+ weekly in the report) | same rule, 8 OTM strikes, thin-bar and zero-range guards (`instrument/legs.py`) |

Levels are fixed for the whole session. Nothing recomputes them from ticks. Holidays: `data/holidays.txt`
via `TradingCalendar`; when the file is incomplete the market is the calendar (see §4).

## 3. When they are fetched

| moment | what happens |
|---|---|
| boot, any time of day | daily cache (`data/daily/*.json`) seeds the store instantly → `_backfill` refetches every name's `1d` from REST (REST wins over cache) → zones compute lazily → leg ladders load in the background |
| day roll (IST date changes) | daily series refetched, zone cache cleared, leg ladders reloaded for the new day |
| `daily_refresh_hm` (08:30, 15:45 IST) | daily series refetched: after the NSE close so the official close lands the same evening, before the MCX open so an overnight-persistent process starts on official candles |
| every `pivot_repair_interval_s` (120 s) | `bars/daily.py::audit` runs; anything **missing / unofficial / stale / short** is refetched (bounded per pass); legs that failed are retried; a stale set is refetched once per expected session, then left alone |

Every successful REST refresh rewrites the cache atomically, so the file on disk is always the
last known official series.

## 4. Corner cases, and what the engine does

| case | behaviour |
|---|---|
| mid-session restart | REST refetch → identical levels (idempotent; verified 2026-09-23 09:33). The live-built partial bar of the running day is excluded by `previous_session` and can never overwrite a REST bar (`store.py` PARTIAL guard). |
| broker historical API down at boot | cache seeds the previous session's official candles; zones and ladders are real immediately; repair loop replaces them when REST answers |
| one name's backfill fails | recorded in `_daily_failed`; the repair loop retries it every interval; `zones_for` returns no zones for it rather than stale ones, and **does not cache the failure** |
| process stays up overnight | day roll + 08:30 refresh replace yesterday's tick-built daily bar with the official candle before any pivot is computed |
| leg fetch fails (rate limit, blip) | 3 attempts with backoff inside `LegPivotLoader._one`; still-missing legs retried by the repair loop; guard refusals (thin / zero-range) are counted separately and not retried |
| holiday not in `data/holidays.txt` | every name's latest session predates the calendar's expectation → `holiday_suspected`; reported in health, no refetch storm |
| holiday in the file, market actually open | `expected_prev` is wrong-early; names look "fresh" — the audit cannot detect this. Keep the file right. |
| machine clock not IST | all "today" decisions use `market.session.ist_today()`; a UTC box gets the same answers |
| 15:15 cutoff on intraday REST bars | affects backfilled 30m/1m bars only, never the daily series; live ticks cover 15:15–15:30 |

## 5. Observability

Health check `pivots_ready`: `ok` when every underlying that has any candles at the broker holds an
official previous-session bar with ≥ 25 daily bars, no daily fetch is in the failed state, and no leg
is in the failed state after retries. A listed contract with no candles at all (COTTON, KAPAS, the
MCX indices) is `dormant`: reported in `detail`, asked once per session, never a fault. `detail` carries the audit summary
(`N/M names on the official previous session; k missing; …`) and the leg counts. `/api/leg-pivots`
reports `loaded / failed / refused`.

## 6. How FUDKII-RT-X exits use the ladders (operator's design, 2026-09-23)

`RT_X_LIMITS.own_ladder = True`, `reproject_stop_s = 10`, `peak_giveback_pct = 3`, `sustain_s = 75`:

- **Ladder:** the contract's **own** daily + weekly classic pivots (`LegPivotLoader`, weekly from the
  same candles, ≥ 3 sessions), merged (`mtf_rungs`, 2 % tolerance) and sorted; **T1–T4 are the rungs
  above the entry premium**. A rung the contract gapped over is not a target. Nothing is
  delta-projected onto the option.
- **Equity trigger:** if the underlying reaches its own T1 first, the option's price at that instant
  *is* T1 and the higher own rungs follow it.
- **Touch** of Tn → one lot out (the last rung takes the rest); the hard SL steps to T(n−1)
  (breakeven for T1). **Sustained** — 75 s continuously at/above Tn *and* a 1-minute close at/above it
  since the touch — → the hard SL steps to Tn; for T1 that arms the give-back.
- **One rising stop:** `ratchet_sl = max(stepped rung SL, peak − 3 % [spread-floored])`, never lowered;
  trading through it ends the trade at once. Before the T1 touch the option-side stop is the equity
  stop through **live** delta (re-projected every 10 s), with the 75 s sustain and the 9 % hard floor.
- No bar time-stop; NSE positions flatten at 15:20 IST, MCX at 23:20.

**Three RT books off the same fill** (`risk/limits.py`, since 2026-09-23 evening). Every FUDKII
NSE fill is mirrored into RT-X, RT-N and RT-Y, each on its own wallet; only the exit differs.
The give-back band is read live (`ExitEngine._band_level`), never folded into the stepped SL, so
the rung SL and the band keep their own exit rules:

| | RT-X | RT-N | RT-Y |
|---|---|---|---|
| ladder | own daily+weekly rungs above entry | own daily R1–R4 above entry | own daily+weekly rungs ≥ 0.5 × expected daily move above entry |
| arming | T1 touch pays a lot; T1 sustained (75 s + 1m close) arms | equity T1 touch, or a 1m close over own R1, pays a lot and arms at once | as X, on the higher first rung |
| stepped SL | breakeven at T1 touch → T1 on sustain → T2 … | breakeven at arming → rung on sustain | one rung behind (`sl_lag`): breakeven until T2 touches, then T1 … |
| band | peak − 3 % | peak − 2 % | peak − max(10 %, 0.25 × expected daily move) |
| band exit | one read through | three consecutive reads | 75 s continuous breach |
| rung SL exit | one read through | one read through | 75 s continuous breach (`post_arm_sustain`) |

Expected daily move (`market/iv.py::expected_move_frac`) = spot × IV(name) × √(1/252) × δ / premium:
the option's own volatility unit, 60–200 % of premium on 2026-09-23 — which is why a 2–3 % band
scratched every runner in the replays.

**Entry gate — RT-X and RT-Y only** (`RiskLimits.dried_volume_v = 0.85`): the mirror is skipped
when the trigger 30m bar *and* the one before it are both under 0.85 × the T-2…T-7 volume baseline
(floor 1000, `bars/indicators.py::dried_volume` — the reference RT router's R1) on the underlying
**or** on its front future (`Engine._volume_surges`; the future's candles come from the broker at
twin time, a leg without data is absent, never dried). RT-N takes every fill as the control. The skip
is written on the ENTRY alert card (`card.skipped`).

**No premium floor for the FUDKII family** (operator, 2026-09-23 evening): the parent, the RT
twins and the CT fades select without the shared ₹5 floor (`Engine.selection_policy_for`); FUKAA
keeps it. On the cheap contracts this admits the δ-projected option stop is a tick or two, so the
stop is floored at `MIN_STOP_TICKS` = 8 ticks below the premium at entry (`floored_option_stop`)
and in the RT engines' re-projection (`RiskLimits.min_stop_ticks`) — the setting the replay of the
23-Sep sub-₹5 rejections was run with (RT-X −31.6k, RT-N −23.3k, RT-Y +13.0k gross on ten
contracts; the operator chose to trade them).

**Every book is independent** (operator, 2026-09-23 evening): the position counts and the money a
book's entry is checked against are its own (`risk/exposure.py`, `total_capital` = the book's
wallet). A parent fill spawns RT-X/RT-N/RT-Y (and CT-X/CT-Y on a COUNTER); none of them count
against the parent's caps, nor the parent against theirs. Before this the parent's
`max_positions_all_books=6` counted the twins, so the parent stopped entering after two fills and
a 09:45 burst was a lottery over which two names got the slots. `GradePolicy.min_stop_atr_filter`
(a filter, off by default; the evidence is on the field) can grade a signal whose stop is inside one
bar's noise as F.

**Counter-trend books — CT-X and CT-Y** (`strategy/counter.py`, 2026-09-23 evening; the reference
stack's wall-strength COUNTER, live there since 2026-09-11). On every FUDKII trigger the wall ahead
of the close is scored: every daily/weekly/monthly classic level inside the trigger candle on the
signal's side or within 0.5 × ATR30m past its extreme, weighted 1d 4.0 / 1wk 3.2 / 1mo 2.0 × nearness
rank (1.0 / 0.8 / 0.6 / 0.4), clustered within 0.25 × ATR30m. A genuine ST flip into a wall ≥ 5.2 is
**COUNTER**: the fade is entered by CT-X — the opposite OTM from the same selector, stop and targets
from `compute_confluence` for the flipped direction, sized under RT-X's limits, exits under RT-X's
policy — and mirrored into CT-Y (RT-Y's exits). Anything else is IN_TREND and CT-X/CT-Y stay flat.
The route and the wall are stamped on the ENTRY card (`card.route`); a COUNTER with no wall on the
flipped side is recorded as `COUNTER_NO_PLAN`. The in-trend mirrors (RT-X/N/Y) trade regardless of
the route — the fade is beside them, not instead of them.

**Per-name implied vol (the stock's own VIX).** The option ladder's merge tolerance is
`k(name) × ATR30(parent) × δ / premium` (`market/iv.py`), where `k(name)` bands today's ATM implied
vol of the front expiry against the name's **own** median IV (≥ 10 sessions; India VIX until then).
Not IV/realised — a single stock's IV always sits above its realised. History is seeded from the
option candles the legs fetch and replaced by live once-a-minute points, persisted under
`data/iv/<SYMBOL>.json`. The equity zones stay on the India VIX band. `/api/chain/{symbol}.stockIv`
shows the IV, the median, the band and the `k` in force.
