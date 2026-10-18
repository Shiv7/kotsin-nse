# kotsin-nse — base

The **base** of an NSE/MCX trading engine on **5paisa**: market data in, bars out, orders plumbed —
and **no strategy**. One asyncio backend, one React frontend, one process, one port, one SQLite file.

This branch (`fresh-base`) is the clean starting point. Every strategy, the review committee, the
backtester and their pages were removed; what is left is the plumbing a strategy would sit on.

## What is here

| Area | What it does |
|---|---|
| Broker | 5paisa TOTP login, REST (candles, quotes, orders, positions, scrip master), the openfeed WebSocket |
| Universe | every root with a derivative, joined to its cash equity; front/next futures; a ±12% strike shortlist, rebuilt 09:20 IST |
| Market data | ticks, best-5 depth and OI from the socket; live quotes and order books held in memory |
| Bars | ticks → 1m … 30m and a live 1d bar on the session grid; REST backfill at boot; every closed 30m bar checked against the exchange's own candle |
| History | `fetch-history` fills a Parquet cache from the broker's historical endpoint |
| Archive | per-IST-day Parquet of every 1m bar and every OI frame |
| Orders | SHADOW / PAPER / LIVE_CAPPED / LIVE gateway, book-walking paper fills, the measured cost model, live executor, broker reconcile — wired, but nothing submits |
| Ops | health checks, Telegram alerts, mode and halt in the control table, kill switch |
| UI | Overview (status + controls), Chart (candles, volume, live forming bar), Options (chain, quotes, OI), Universe, System |

A strategy plugs in at `Engine._on_bar_close` (a closed bar, already checked against the exchange)
and submits an `OrderIntent` through `engine.gateway`.

## Principles

1. **One process, one port** ([ADR-0001](docs/adr/0001-single-process.md)).
2. **The broker is the source of truth.** Local state is a cache.
3. **Only `market/session.py` knows about IST.** Everything else is UTC epoch seconds
   ([ADR-0002](docs/adr/0002-utc-inside-ist-at-the-edges.md)).
4. **Config is typed and closed.** An unknown `KN_*` key fails the boot.
5. **Mode is state, not an env var.** A live mode must be *armed* with an expiry; a restart after
   it boots into PAPER.
6. **Costs are modelled before anything is believed.** At ₹33,000 a position the round trip was
   0.299% and 81% of that was flat brokerage.

The lessons behind these are in [`docs/LEARNINGS.md`](docs/LEARNINGS.md); broker quirks in
[`docs/FIVEPAISA_FACTS.md`](docs/FIVEPAISA_FACTS.md).

## Layout

```
backend/    Python 3.12 · FastAPI + asyncio · uv-managed      (kotsin_nse/)
frontend/   React 18 · Vite · TypeScript · Tailwind           (served by the backend in prod)
docs/       ARCHITECTURE · LEARNINGS · FIVEPAISA_FACTS · RUNBOOK · ADRs
deploy/     Dockerfile, docker-compose, systemd unit
```

## Quickstart

```bash
cp backend/.env.example backend/.env    # fill in 5paisa credentials — ROTATE THE OLD ONES FIRST
make setup                              # uv sync + npm install
make check                              # ruff, import contracts, tests, UI typecheck+build
make run                                # http://127.0.0.1:8500
```

Without credentials the engine still boots and serves the UI; it says so in the boot notes rather
than pretending to have a feed.

## History

```bash
cd backend
uv run kotsin-nse fetch-history --symbols RELIANCE,TCS,INFY --start 2025-09-01
```
