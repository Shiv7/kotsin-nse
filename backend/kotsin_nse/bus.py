"""In-process typed event bus.

Why not Kafka: one producer, one consumer, one process (``docs/adr/0001``). The NSE stack ran five
JVMs across Kafka, Redis and Mongo to move a tick from a socket to a strategy, and the failure modes
that cost the most time were all *between* those hops — a boundary keyed on candle event time
replaying an hours-old bar as a live signal, a consumer group lagging unnoticed, a topic whose
lifetime end-offset was zero. A queue in one process has none of those.

Policies: market-data topics ``DROP_OLDEST`` (a stale depth delta is worthless — drop it, count it,
show the count on the System page). Order / position / control topics ``BLOCK``: never lose one.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Topic(StrEnum):
    TICK = "tick"
    DEPTH = "depth"
    OI = "oi"
    BAR = "bar"
    BOUNDARY = "boundary"
    SIGNAL = "signal"
    ORDER_INTENT = "order_intent"
    ORDER_EVENT = "order_event"
    FILL = "fill"
    POSITION_EVENT = "position_event"
    CONTROL = "control"


class Policy(StrEnum):
    DROP_OLDEST = "drop_oldest"
    BLOCK = "block"


NEVER_DROP: frozenset[Topic] = frozenset(
    {
        Topic.BOUNDARY,
        Topic.SIGNAL,
        Topic.ORDER_INTENT,
        Topic.ORDER_EVENT,
        Topic.FILL,
        Topic.POSITION_EVENT,
        Topic.CONTROL,
    }
)


@dataclass
class Subscription:
    topic: Topic
    queue: asyncio.Queue[Any]
    policy: Policy
    dropped: int = 0


class Bus:
    def __init__(self) -> None:
        self._subs: dict[Topic, list[Subscription]] = defaultdict(list)
        self.published: dict[Topic, int] = defaultdict(int)

    def subscribe(
        self, topic: Topic, *, maxsize: int = 2000, policy: Policy | None = None
    ) -> Subscription:
        if policy is None:
            policy = Policy.BLOCK if topic in NEVER_DROP else Policy.DROP_OLDEST
        sub = Subscription(topic=topic, queue=asyncio.Queue(maxsize=maxsize), policy=policy)
        self._subs[topic].append(sub)
        return sub

    async def publish(self, topic: Topic, event: Any) -> None:
        self.published[topic] += 1
        for sub in self._subs[topic]:
            if sub.policy is Policy.BLOCK:
                await sub.queue.put(event)
                continue
            if sub.queue.full():
                sub.queue.get_nowait()  # single-threaded: no await between get and put
                sub.dropped += 1
            sub.queue.put_nowait(event)

    def stats(self) -> dict[str, Any]:
        return {
            topic.value: {
                "published": self.published[topic],
                "subscribers": [
                    {"depth": s.queue.qsize(), "dropped": s.dropped, "policy": s.policy.value}
                    for s in subs
                ],
            }
            for topic, subs in self._subs.items()
        }
