"""One health record, and the rules that turn it red.

A health check must not treat *slow* as *dead*. The old box spiralled because a 10-second
``mongosh`` probe timed out on a thrashing single core, the watchdog read that as a dead database
and restarted it every three minutes, which emptied its cache and made the next probe slower. So:
every degraded condition here needs ``consecutive_required`` observations before it counts, and the
record says how many it has seen.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    value: float | None = None


@dataclass(slots=True)
class HealthMonitor:
    consecutive_required: int = 3
    _streak: Counter[str] = field(default_factory=Counter)
    started_ts: float = field(default_factory=time.time)

    def evaluate(self, checks: list[Check]) -> dict[str, Any]:
        degraded: list[str] = []
        for c in checks:
            if c.ok:
                self._streak[c.name] = 0
                continue
            self._streak[c.name] += 1
            if self._streak[c.name] >= self.consecutive_required:
                degraded.append(c.name)
        return {
            "ts": time.time(),
            "uptime_s": round(time.time() - self.started_ts, 1),
            "status": "degraded" if degraded else "ok",
            "degraded": degraded,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "detail": c.detail,
                    "value": c.value,
                    "consecutive_failures": self._streak[c.name],
                }
                for c in checks
            ],
        }
