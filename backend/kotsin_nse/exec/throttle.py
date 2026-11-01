"""The order-rate throttle.

SEBI's retail-algo framework (in force since 2026-04-01) caps API orders at ten a second per exchange and
5paisa answers the eleventh with HTTP 429. The runner can fire every symbol's exit in the same second at
15:10, so every live placement waits here first: a sliding one-second window per key, fair in order.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any


class OrderThrottle:
    def __init__(
        self,
        per_second: float = 8.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.per_second = max(1.0, float(per_second))
        self._clock = clock
        self._sleep = sleep
        self._sent: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.acquired = 0
        self.waited = 0
        self.waited_s = 0.0

    async def acquire(self, key: str = "N") -> float:
        """Take a slot for ``key`` (an exchange); returns the seconds waited for it."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        q = self._sent.setdefault(key, deque())
        async with lock:
            waited = 0.0
            while True:
                now = self._clock()
                while q and now - q[0] >= 1.0:
                    q.popleft()
                if len(q) < self.per_second:
                    q.append(now)
                    self.acquired += 1
                    if waited > 0:
                        self.waited += 1
                        self.waited_s += waited
                    return waited
                delay = max(0.005, 1.0 - (now - q[0]) + 0.005)
                waited += delay
                await self._sleep(delay)

    def stats(self) -> dict[str, Any]:
        return {
            "per_second": self.per_second,
            "acquired": self.acquired,
            "waited": self.waited,
            "waited_s": round(self.waited_s, 3),
        }
