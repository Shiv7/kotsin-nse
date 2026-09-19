# Architecture

## Shape

```
                                5paisa
  ┌─ WebSocket (openfeed) ──────────────┐  ┌─ REST (Openapi) ─────────────────────┐
  │ MarketFeedV3     ticks              │  │ V2/historical      OHLCV backfill     │
  │ MarketDepthService  20-level book   │  │ V1/MarketFeed      snapshot quotes    │
  │ GetScripInfoForFuture  open interest│  │ V1/PlaceOrderRequest  orders          │
  └──────────────────┬──────────────────┘  │ V2/OrderStatus     fills              │
                     ▼                     │ V2/NetPositionNetWise  reconciliation │
  venue/fivepaisa/   auth (TOTP → JWT) ────│ ScripMaster/segment  the catalogue    │
                     ▼                     └────────────────────┬──────────────────┘
  instrument/    scrip master → Instrument (code, symbol, lot, tick, multiplier,   │
                 expiry, strike) — the ONLY place a symbol becomes a scrip code    │
                     ▼                                                             │
  bars/          ticks → 1m → 5m/15m/30m on the SESSION grid (09:15 NSE, 09:00 MCX)│
                 ⊕ OI from the front future ⊕ session VWAP → UnifiedBar ◀──────────┘
                     ▼
  bars/pivots    daily+weekly+monthly pivots → weighted zones → stop, T1–T4, grade
                     ▼
  strategy/      pure: FUDKII.on_bar(ctx, bar) → Outcome; FUKAA.on_signal(base)
                 gates declare on_missing; every rejection names its binding gate
                     ▼
  instrument/    OTM strike anchored on confluence T1 (MCX → front future)
                     ▼
  risk/          the only owner of sizing and exits: cost model, wallets, sizing
                 from the stop, exposure by underlying, exits, breakers
                     ▼
  exec/          gateway: SHADOW | PAPER (fills vs the live 20-level book)
                 | LIVE_CAPPED | LIVE · halt · idempotency · caps · breaker · reconcile
                     ▼
  ledger/        SQLite: control, wallets, signals, rejections, orders, positions,
                 trades, wallet snapshots, events, health
  api/           REST → frontend/ (mounted on the same port in prod)
  ops/           Telegram alerts (rate-limited), health with consecutive-failure gating
```

All of it is **one asyncio process**, connected by bounded in-process queues (`bus.py`).

## Infra decisions

| Concern | Choice | Why | Revisit when |
|---|---|---|---|
| Message transport | `asyncio.Queue` behind a typed bus | one producer, one consumer, one process | a second *process* needs the same stream ([ADR-0001](adr/0001-single-process.md)) |
| Time | UTC epoch seconds internally, IST only in `market/session.py` and the UI | 106 classes in the old stack hard-coded `Asia/Kolkata` and none of their maths was portable ([ADR-0002](adr/0002-utc-inside-ist-at-the-edges.md)) |
| Database | SQLite (WAL) via SQLAlchemy async | single writer, ACID for orders, the UI reads concurrently | a second writer → Postgres is a URL change |
| Hot state | in memory, persisted on every change | positions, mode and the halt flag are rows, not Redis keys ([ADR-0003](adr/0003-positions-are-persisted.md)) |
| Broker | 5paisa only, behind `venue/base.py` | the seam exists so nothing outside `venue/` names a broker |
| UI serving | the backend mounts `frontend/dist` | one port, one unit, one thing to restart |
| Language | Python 3.12 + asyncio | the research and LLM tooling lives here ([ADR-0004](adr/0004-python-not-java.md)) |

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | typed, closed settings; unknown `KN_*` keys fail at boot; `None` means a cap is OFF |
| `market/session.py` | the only IST-aware module: sessions, bar grids, force-flat, the holiday calendar |
| `bus.py` | typed topics, bounded queues, drop-oldest for market data, block for orders |
| `venue/fivepaisa/auth.py` | TOTP → RequestToken → AccessToken, one login per 30 s window, public-IP detection |
| `venue/fivepaisa/rest.py` | historical candles, snapshot quotes, orders, positions, option chain, scrip master |
| `venue/fivepaisa/ws.py` | the openfeed socket; byte-exact control frames; reconnect + resubscribe |
| `instrument/catalogue.py` | the scrip master, parsed daily; the only symbol ↔ scrip-code mapping |
| `instrument/select.py` | OTM strike anchored on T1, liquidity checks, level → premium projection |
| `bars/indicators.py` | Wilder ATR, population-σ Bollinger, SuperTrend with band lock, the two surge forms |
| `bars/pivots.py` | classic pivots (Kite R3 convention), zone clustering, the confluence ladder and grade |
| `bars/aggregator.py` | ticks → bars on the session grid; volume from the cumulative delta; partial tagging |
| `strategy/fudkii.py` · `fukaa.py` | the two books |
| `strategy/conviction.py` | the S1–S6 matrix with its per-exchange thresholds |
| `risk/costs.py` | the measured NSE/MCX charge model — used by paper, live and research alike |
| `risk/sizing.py` · `exits.py` · `exposure.py` · `wallet.py` | one owner per rule |
| `exec/gateway.py` · `paper.py` · `live.py` · `reconcile.py` | modes, book-walking fills, real orders, truth |
| `ledger/db.py` | SQLite schema and reads, including the binding-gate histogram |
| `api/routes.py` | REST; computes nothing, reads engine state and ledger rows |

Boundaries are enforced by `import-linter` (`backend/pyproject.toml`), not by convention:

* `strategy` may not import `venue`, `exec`, `ledger`, `api`, `ops`, `feed` or `instrument`;
* `exec` may not import `strategy`;
* `venue` is a leaf;
* `strategy`, `risk`, `bars` and `exec` may not import `zoneinfo` directly.

## The decision path, in order

1. A tick arrives; the aggregator buckets it by **trade time** into the 30m bucket anchored on the
   session open, and closes the previous bucket if this tick starts a new one.
2. On a closed 30m bar, `Fudkii.on_bar` computes BB(20, 2) and SuperTrend(7, 3) over the trailing
   window, scores the flip and the band break, and — if both fire — asks the confluence engine for
   a stop, targets and a grade. Grade `F` is a recorded rejection, not a silent drop.
3. `Fukaa.on_signal` takes that signal and applies the volume bar, the conviction matrix, the RR
   floor and the OI floor. A signal that fails only on volume is **parked** for 35 minutes and
   promoted if the next bar delivers.
4. `instrument.select` picks the OTM strike nearest the T1 anchor that is actually quotable, and
   projects the underlying's stop and targets onto the premium via an estimated delta.
5. `risk.sizing` sizes from the option stop, clamps to the position budget and lot granularity, and
   **declines** if the round-trip charge would eat more than 35% of the move to T1.
6. `risk.exposure` checks the aggregate across both books, bucketed by underlying.
7. `exec.gateway` runs halt → idempotency → caps → place, and writes an order row whatever happens.
8. Every 1 s, `risk.exits` evaluates each open position: option stop, underlying stop, hard floor,
   target ladder, trail, then the time stop and force-flat as backstops.

## What is deliberately absent

| Not here | Why |
|---|---|
| CAN1 / CAN2 | equity books; a different thesis and a different cost regime |
| FUDKII-RT | the geometry router; several of its thresholds were fitted on n = 1 or n = 2 |
| FUDKOI | one threshold comparison whose entire ranking apparatus was inert |
| RETEST | every threshold hardcoded; its configured and read key sets did not overlap at all |
| QUANT | coherent, but a separate book with its own scoring service |
| BB-squeeze family | 2, 4 and 47 lifetime signals respectively — seven conjunctive gates |
| PIVOTBOSS | never fired in its life: a symbol was passed into a numeric-code lookup |
| PIVOT_CONFLUENCE | deliberately suspended 2026-04-02 |
| MERE / MICROALPHA | deleted upstream; MICROALPHA never emitted a signal |
| Kafka / Redis / Mongo | one process does not need a broker between its own functions |
