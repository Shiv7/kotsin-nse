# ADR-0001 — One process, no Kafka

**Status:** accepted, 2026-09-20

## Context

The NSE stack moved a tick from a socket to a strategy through five JVMs, Kafka, MongoDB and Redis.
The failure modes that cost the most time were all *between* those hops:

* boundaries keyed on candle **event time** with `auto.offset.reset=earliest`, so a backlog drain
  republished hours-old bars as live signals — late, not missing, and nothing checked the age;
* a topic whose lifetime end-offset was zero, proving a strategy had never fired in its life —
  and nobody had ever run the query;
* a watchdog relaunching five JVMs inside 30 seconds with 19 GB of heap ceilings onto a 2 GB box;
* a health probe that spawned a Node process, timed out on a thrashing core, read "slow" as "dead",
  and restarted the database every three minutes.

None of those are Kafka's fault. They are the cost of having five deployables where the problem
needs one.

## Decision

One asyncio process. Stages are functions connected by bounded `asyncio.Queue`s behind a typed bus.
Market-data topics drop the oldest event under pressure and count it; order and control topics
block.

## Consequences

* A tick reaching a strategy is a function call, not a network hop. There is no offset, no consumer
  group, no replay-as-live hazard.
* Durability is the SQLite ledger, which is written synchronously on every state change.
* One systemd unit, one log, one thing to restart, one memory limit.
* **Cost:** no horizontal scale. At ≤ 200 symbols on 30-minute bars, that is not the binding
  constraint. Revisit when a second *process* genuinely needs the same stream — and then publish
  from one place rather than pointing five services at a broker.
