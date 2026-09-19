# ADR-0004 — Python, not a port of the Java services

**Status:** accepted, 2026-09-20

## Context

The NSE stack is Java 17 / Spring Boot. There is a lot of working logic in it: the confluence
engine, the conviction matrix, the order gateway, the orderbook walker. The obvious move is to
port it.

Against that: 6,732 lines in one trigger class hosting four books; 232 classes in the executor;
106 classes across two services hard-coding the exchange timezone; and a research layer already in
Python, which is where the backtests, the calibration and the statistics actually happened.

## Decision

Rebuild in Python 3.12 + asyncio, porting the **maths and the contracts** rather than the code.

Ported deliberately: the pivot formulas including the Kite R3 convention, the confluence zone
clustering and grade, the conviction matrix with its per-exchange thresholds, the SuperTrend band
lock and Wilder ATR, the order-gateway pipeline (halt → idempotency → caps → place → audit), the
orderbook walk with its lots-first-then-price and 10%-ceiling rules, and the trade-record schema
(R-multiple, MFE/MAE, idempotency key).

Not ported: anything session-coupled, the Spring wiring, the four books that are out of scope, and
every dead config key catalogued in the pattern list.

## Consequences

* The backtester can import the *live* strategy and risk code. Hand-rolled replays in the old stack
  erred between −80% and +185%; this removes the class of error entirely.
* One language for the engine, the research and the tooling.
* `import-linter` gives the layering that Spring's component scan did not.
* **Cost:** the Java services keep running until this replaces them, and for a while two systems
  express the same ideas. That is why this repo has **zero code dependency** on the old one — it
  reads its lessons, not its classes.
