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

**Per-name implied vol (the stock's own VIX).** The option ladder's merge tolerance is
`k(name) × ATR30(parent) × δ / premium` (`market/iv.py`), where `k(name)` bands today's ATM implied
vol of the front expiry against the name's **own** median IV (≥ 10 sessions; India VIX until then).
Not IV/realised — a single stock's IV always sits above its realised. History is seeded from the
option candles the legs fetch and replaced by live once-a-minute points, persisted under
`data/iv/<SYMBOL>.json`. The equity zones stay on the India VIX band. `/api/chain/{symbol}.stockIv`
shows the IV, the median, the band and the `k` in force.
