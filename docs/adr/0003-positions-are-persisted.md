# ADR-0003 — Positions are persisted, and the broker is the authority

**Status:** accepted, 2026-09-20

## Context

One live book held its open positions in an in-memory dictionary with no persistence and no
re-hydration. Every restart orphaned whatever was live: the strategy reported `open=0` while the
executor still held the trade, and the log looked entirely normal. A separate book had its stop
written at entry by one service, rewritten daily by a second, and independently trailed by two
rules in a third — one of which once set a stop *above* the live price and stopped the position out
instantly.

## Decision

1. Every position is written to SQLite on every change and re-hydrated at boot.
2. The **broker** is the source of truth. `exec/reconcile.py` compares `NetPositionNetWise` against
   the local book on boot and on a timer, classifying disagreements as ORPHAN, PHANTOM or
   QTY_MISMATCH.
3. Any mismatch — or a *failed* reconciliation — **freezes new entries**. Exits are never frozen.
4. Reconciliation never places an order. It reports; a human or an explicit kill decides.
5. `risk/exits.py` is the only module permitted to move a stop or close a position.

## Consequences

* A crash mid-position is recoverable, and the recovery is visible rather than silent.
* A reconciliation failure is treated as a mismatch, not as "probably fine".
* **Cost:** a SQLite write on every position mutation, and entries that stop on a disagreement that
  might be benign. Both are cheap next to an orphaned live position.
