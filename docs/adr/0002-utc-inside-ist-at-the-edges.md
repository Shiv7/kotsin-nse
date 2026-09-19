# ADR-0002 — UTC inside, IST only at the edges

**Status:** accepted, 2026-09-20

## Context

61 classes in one service of the NSE stack and 45 in another hard-coded `Asia/Kolkata`. The
consequence was not a bug — it was that **none of the maths was portable**. When the same
indicators were wanted for a 24/7 venue, the only options were a rewrite or a session-shaped
abstraction bolted onto code that assumed a 09:15 open everywhere.

## Decision

Every timestamp inside the engine is **UTC epoch seconds**. Exactly one module —
`market/session.py` — imports `zoneinfo`, and it owns every session rule: open and close, the bar
grid, the entry cutoff, the force-flat, and the trading calendar. `import-linter` forbids
`strategy`, `risk`, `bars` and `exec` from importing `zoneinfo` directly.

The UI renders IST; that is a presentation choice made in one place (`lib/api.ts`).

## Consequences

* An indicator is a function of a list of bars. It has no opinion about when the session opens, so
  it can be reused, unit-tested with synthetic data, and moved to another market unchanged.
* Session-dependent behaviour is visible: `session_phase()` and `past_force_flat()` are explicit
  calls, not an implicit consequence of a local `LocalTime.now()` somewhere.
* **Cost:** one indirection. `bucket_start(segment, ts, tf)` instead of a naive floor. That is the
  whole price, and it also buys the correct 09:15 opening bucket — the old 1-minute collection
  started at 09:16 and lost the minute that often holds the day's extreme.
