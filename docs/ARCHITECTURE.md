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
  (strategy)     nothing here yet — a strategy reads closed bars and submits orders
                     ▼
  exec/          gateway: SHADOW | PAPER (fills vs the live book)
                 | LIVE_CAPPED | LIVE · halt · idempotency · caps · breaker · reconcile
                 risk/costs: the measured charge model
                     ▼
  ledger/        SQLite: control, orders, positions, trades, events, health
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
| `feed/` | the market-data seam: generic channels (quotes, depth, OI), normalised messages stamped with their source, `build_source` picks the source from `KN_DATA_SOURCE`; `feed/fivepaisa.py` adapts the 5paisa socket; `feed/rate.py` measures how often each source really updates each kind of contract |
| `scan/` | the minute board: strength (one-sided move from the open ÷ 50-session average range), ranks, picks, breadth, sectors, VWAP side, OI build-up; `DayReplay` rebuilds any cached day minute by minute with the same code |
| `structure/` | 5-minute price structure: swings, breaks of structure by a close, order blocks with a life cycle, fair value gaps, liquidity, sweeps, Z/S1/R1/R2 |
| `profile/` | volume profile (POC, value area, HVN/LVN) and market profile (TPO letters, first hour, single prints, day type) from 1m bars |
| `orderflow/` | per-minute estimated order flow from quote snapshots (quote rule on each snapshot's new volume), cumulative delta, top-5 book imbalance, the future's OI |
| `market/fo_bhavcopy.py` · `research/oi_daily.py` | NSE's daily F&O bhavcopy: the official OI every OI change is measured from, and the daily futures build-up per stock |
| `research/obtest.py` · `playbooks.py` · `mtf.py` | backtests on the cache: the order-block playbook (1m), sweep / value-area strategies (1m), and 1h/4h sweeps timed on 15m/5m (5m cache, 2.5 years), tagged with the previous session's OI build-up |
| `instrument/catalogue.py` | the scrip master, parsed daily; the only symbol ↔ scrip-code mapping |
| `instrument/quote.py` | the last price, bid and ask seen for a contract |
| `bars/aggregator.py` | ticks → bars on the session grid, **inside the session only**; volume from the cumulative delta; day-extreme recovery; partial tagging; a live 1d bar |
| `bars/verify.py` | REST reconciliation: the exchange's candle installed over the live build on every closed 30m bar; running fidelity metric |
| `instrument/universe.py` | the scripFinder universe model: roots with derivatives → equity join → front/next future → ±12% strike shortlist, rebuilt at 09:20 IST |
| `risk/costs.py` | the measured NSE/MCX charge model — used by paper and live alike |
| `exec/gateway.py` · `paper.py` · `live.py` · `reconcile.py` | modes, book-walking fills, real orders, truth |
| `ledger/db.py` | SQLite: control row, orders, positions, trades, events, health, the call journal |
| `journal.py` | the owner's call journal: a discretionary call logged as it is seen (Chart page), scored by the engine from the 1m bars that follow — fill, stop before target, R at 15/30/60 min and at 15:25; the Journal page reads it by kind, side and tag |
| `api/routes.py` | REST; computes nothing, reads engine state and ledger rows |
| `research/history.py` | Parquet cache of broker history, merged on overlap |

Boundaries are enforced by `import-linter` (`backend/pyproject.toml`), not by convention:

* `venue` is a leaf;
* `risk`, `bars` and `exec` may not import `zoneinfo` directly.

## The bar path, in order

1. A tick arrives; the aggregator buckets it by **trade time** into buckets anchored on the session
   open (ticks outside the session are counted and dropped), and closes the previous bucket if
   this tick starts a new one.
2. A closed bar is published on the bus; 1m bars go to the archive.
3. A closed 30m bar is handed to the reconciler **off the tick path**: it fetches the exchange's
   own candle for that bucket (bounded wait, default 12 s) and installs it over the live build.
   A bucket the socket joined mid-way that REST cannot confirm is logged as a fragment.

There is no decision step: this is where a strategy would read the bar and submit through the
gateway (halt → idempotency → caps → place, an order row written whatever happens).

## Live archive (`ops/archive.py`)

Per-IST-day Parquet of every 1m bar, every OI frame and (`KN_ARCHIVE_TICKS`) every quote frame of
the underlyings and their futures, with its source. The only record of what the socket delivered —
the REST history has no OI and no ticks.

## Adding a market-data source

Orders always go to 5paisa; market data may come from a vendor (Accelpix and GFDL are in review).
A source is a class satisfying `feed.base.MarketDataSource`: it maps an `Instrument` to its own
ticker on subscribe, turns its frames into `TickMsg` / `DepthMsg` / `OIMsg` keyed by the 5paisa
scrip code, stamps `source`, and declares its `Capabilities` honestly (snapshots or every trade,
depth levels, whether the trade side is known). Register it in `feed/__init__.py` and in the
`data_source` literal in `config.py`. Before trusting it, run a session and read `/api/feed`: the
rate meter shows its real update rate next to 5paisa's. Flushed every 5
minutes and at shutdown, in a worker thread.

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
