# kotsin-nse

Personal NSE/MCX trading engine on **5paisa**. One asyncio backend, one React frontend.
No Kafka, no Redis, no Mongo — one process, one port, one SQLite file.

Two books, both 30-minute:

| Key | What it is |
|---|---|
| **FUDKII** | a SuperTrend flip that coincides with a Bollinger break, expressed as an OTM option |
| **FUKAA** | the same trigger, admitted only when volume confirms participation |

Everything else from the old stack — CAN1, CAN2, FUDKII-RT, FUDKOI, RETEST, QUANT, the BB-squeeze
family, PIVOTBOSS, PIVOT_CONFLUENCE, MERE, MICROALPHA — is deliberately **not here**. What is here
is what those books were built from: the confluence engine, the conviction matrix, the cost model,
the order gateway, and the seventeen failure patterns catalogued while documenting them.

## Principles

1. **One process, one port.** feed → bars → strategies → risk → gateway → ledger → API in a single
   `asyncio` process ([ADR-0001](docs/adr/0001-single-process.md)).
2. **The broker is the source of truth.** Positions reconcile against 5paisa on boot and on a
   timer; local state is a cache, and a mismatch freezes entries.
3. **Strategies are pure.** `on_bar(ctx, bar) -> Outcome` — no I/O, no clock, no broker. Enforced
   by `import-linter`, not by convention.
4. **Only `market/session.py` knows about IST.** Everything else is UTC epoch seconds. The old
   stack hard-coded `Asia/Kolkata` in 61 classes in one service, which is why none of its maths was
   portable ([ADR-0002](docs/adr/0002-utc-inside-ist-at-the-edges.md)).
5. **Config is typed and closed.** An unknown key fails the boot, not the trade.
6. **Mode is state, not an env var.** `SHADOW / PAPER / LIVE_CAPPED / LIVE` live in the control
   table; a live mode must be *armed* with an expiry and a restart after it boots into PAPER.
7. **Every candidate is recorded, not only the winners** — with the gate that killed it.
8. **Costs are modelled before the strategy is believed.** At ₹33,000 a position the round trip was
   0.299% and 81% of that was flat brokerage. Sizing declines a trade whose charges eat its target.

## Layout

```
backend/    Python 3.12 · FastAPI + asyncio · uv-managed      (kotsin_nse/)
frontend/   React 18 · Vite · TypeScript · Tailwind           (served by the backend in prod)
docs/       ARCHITECTURE · LEARNINGS · FIVEPAISA_FACTS · RUNBOOK · ADRs · strategy docs
deploy/     Dockerfile, docker-compose, systemd unit
```

## Quickstart

```bash
cp backend/.env.example backend/.env    # fill in 5paisa credentials — ROTATE THE OLD ONES FIRST
make setup                              # uv sync + npm install
make check                              # ruff, import contracts, 139 tests, UI typecheck+build
make run                                # http://127.0.0.1:8500
```

The engine boots in **SHADOW**. To take paper trades:

```bash
curl -X POST localhost:8500/api/control/mode -H 'Content-Type: application/json' -d '{"mode":"PAPER"}'
```

Live requires an explicit arming window and is capped separately — see
[`docs/RUNBOOK.md`](docs/RUNBOOK.md).

Without credentials the engine still boots and serves the UI; it says so in the boot notes rather
than pretending to have a feed.

## Status

| Step | What | Done when | State |
|---|---|---|---|
| 1 | Skeleton, closed config, bus, session calendar, 5paisa REST/WS/scrip master, CI | boot fails on a misspelled key; CI green | ✅ |
| 2 | Bars: ticks → 1m → 30m on the session grid, REST backfill, partial-bar tagging, **REST reconciliation** | live bars match the broker's own candles | ✅ measured 2026-09-21: 1m 87.8% exact from the snapshot feed; closed bars are then made exact by `bars/verify.py`, and the 30m decision waits for the exchange's candle |
| 3 | Pivots + confluence: MTF zones, stop, T1–T4, grade | levels match a hand-computed bhavcopy case | ✅ (DABUR golden case in tests) |
| 4 | FUDKII + FUKAA, pure, gate-counted | a signal reproduces end to end from a fixed bar series | ✅ (tested) |
| 5 | Risk: wallets, sizing from the stop, exits, exposure, cost model | a null strategy costs exactly −charges | ✅ (tested) |
| 6 | Gateway SHADOW/PAPER + ledger + API + UI | every page reads off a live paper run | 🟡 built, needs a session |
| 7 | Live: real orders, reconciliation, kill | 5 restarts with an open position → zero unreconciled | ⬜ built, **never run against the broker**; no resting stop at the venue ([runbook](docs/RUNBOOK.md)) |
| 8 | Backtester replaying the same strategy/risk code | a null strategy backtests to exactly −charges | ✅ (tested; the option leg is modelled, not measured) |
| 9 | Review committee: forensic tables, Claude post-mortems, hypothesis → backtest → grade loop ([docs](docs/COMMITTEE.md)) | a hypothesis is confirmed or refuted by the backtester, never by prose | 🟡 built; forensics live, reviews off until `KN_ANTHROPIC_API_KEY` |

**Nothing here has traded, and the first real backtest says it should not** — FUDKII with the inherited parameters averages −1.40R (t = −8) over 481 trades on a year of real data; see `docs/strategies/FUDKII.md` §8. No parameter in this repo carries a *validated* artefact; the ones
inherited from the old stack carry its evidence and its caveats, both recorded in
[`docs/strategies/`](docs/strategies/).

## Research

```bash
cd backend
uv run kotsin-nse fetch-history --symbols RELIANCE,TCS,INFY --start 2025-09-01
uv run kotsin-nse backtest --symbols RELIANCE,TCS,INFY
```

The backtester imports the **live** `Fudkii`, `Fukaa`, `ExitEngine`, `CostModel` and sizing — it
does not reimplement them, because hand-rolled replays in the old stack erred between −80% and
+185% against production. It is deliberately pessimistic: entry on the next bar's open, the stop
assumed first when a bar covers both stop and target, and a stop fill worse than the stop price.

It measures the **underlying**. There is no option-chain history from this broker — expired
contracts leave the scrip master — so the option leg is a *model*, reported separately and
labelled. Results land in `data/backtests/` and on the Backtest page.

Statistics are day-clustered, and `research/stats.py` carries the within-day permutation test that
twice overturned a "significant" split in the old stack.
