"""Telegram alerts. No-op when unconfigured, and it never raises.

An alerting path that can throw is worse than none: it turns a notification failure into a trading
failure. Every send is best-effort and failures are counted, not propagated.

Rate-limited per key, because the real lesson from the old stack is not that it lacked logging —
it is that the diagnostics were perfect and nobody read them. One line printed 3,296 times to a
file over 25 days; another 7,946 times in a single day. Alert on *transitions*, at most once per
cooldown, or the channel becomes the same unread file.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class Telegram:
    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        *,
        client: httpx.AsyncClient | None = None,
        cooldown_s: float = 300.0,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self._client = client
        self.cooldown_s = cooldown_s
        self._last: dict[str, float] = {}
        self.sent = 0
        self.suppressed = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str, *, key: str | None = None, force: bool = False) -> bool:
        if not self.enabled:
            return False
        if key and not force:
            last = self._last.get(key, 0.0)
            if time.time() - last < self.cooldown_s:
                self.suppressed += 1
                return False
            self._last[key] = time.time()
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            r = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML"},
            )
            ok = r.status_code == 200
            self.sent += int(ok)
            self.failed += int(not ok)
            return ok
        except Exception as exc:  # noqa: BLE001 - alerting must never break trading
            self.failed += 1
            log.warning("telegram.failed", error=str(exc))
            return False
        finally:
            if self._client is None:
                await client.aclose()

    def fire_and_forget(self, text: str, *, key: str | None = None) -> None:
        if not self.enabled:
            return
        try:
            asyncio.get_running_loop().create_task(self.send(text, key=key))
        except RuntimeError:
            pass

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "failed": self.failed,
        }
