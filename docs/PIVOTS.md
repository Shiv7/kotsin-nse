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
Fib: P ± 0.382 / 0.618 / 1.000 × (H − L)        Camarilla: C ± 1.1(H − L) / 12, /6, /4, /2
```

`P, R1/S1, R2/S2, CPR, Fibonacci, Camarilla` are identical across every platform. Only the outer
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
