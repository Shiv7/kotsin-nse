"""5paisa market-data WebSocket.

Wire format, reproduced exactly (a malformed control frame is **not** rejected — the broker simply
never sends data for that scrip, which presents downstream as "an instrument that never ticks" and
is close to impossible to attribute):

.. code-block:: json

   {"ClientCode":"50000001","MarketFeedData":[{"ExchType":"C","Exch":"N","ScripCode":"1660"}],
    "Method":"MarketFeedV3","Operation":"Subscribe"}

Outer key order is ``ClientCode, MarketFeedData, Method, Operation``; each entry is
``ExchType, Exch, ScripCode``. That ordering is not cosmetic — it is what the old json-simple
implementation happened to emit (``JSONObject extends HashMap``, so bucket order) and what the
broker has been accepting for two years. ``json.dumps`` preserves insertion order, so building the
dicts in this order is the whole mechanism.

Channels: ``MarketFeedV3`` (ticks), ``MarketDepthService`` (order book), ``GetScripInfoForFuture``
(open interest — futures and options only; cash equity has no OI, which is the defect that made
MicroAlpha's OI term structurally zero for its entire life).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
import websockets

from ...config import Settings
from ...domain import Instrument
from .auth import Authenticator

log = structlog.get_logger(__name__)

METHOD = {"mf": "MarketFeedV3", "md": "MarketDepthService", "oi": "GetScripInfoForFuture"}
OPERATION = {"s": "Subscribe", "u": "Unsubscribe"}

#: The broker's 20-level depth frames are large; a batch bigger than this is split.
MAX_ENTRIES_PER_FRAME = 200


def build_frame(
    client_code: str, channel: str, operation: str, instruments: list[Instrument]
) -> str:
    return json.dumps(
        {
            "ClientCode": client_code,
            "MarketFeedData": [
                {"ExchType": i.exch_type, "Exch": i.exch, "ScripCode": i.scrip_code}
                for i in instruments
            ],
            "Method": METHOD[channel],
            "Operation": OPERATION[operation],
        },
        separators=(",", ":"),
    )


def parse_broker_date(raw: Any) -> float | None:
    """``/Date(1758271500000)/`` or ``/Date(1758271500000+0530)/`` → epoch seconds."""
    if raw is None:
        return None
    text = str(raw)
    start = text.find("(")
    if start < 0:
        return None
    digits = ""
    for ch in text[start + 1 :]:
        if ch.isdigit() or (ch == "-" and not digits):
            digits += ch
        else:
            break
    return int(digits) / 1000 if digits else None


@dataclass(slots=True)
class FeedHealth:
    connected: bool = False
    connected_since: float | None = None
    last_message_ts: float | None = None
    messages: int = 0
    ticks: int = 0
    depth: int = 0
    oi: int = 0
    reconnects: int = 0
    parse_errors: int = 0
    subscriptions: dict[str, int] = field(default_factory=dict)
    last_error: str = ""

    @property
    def silence_s(self) -> float | None:
        return None if self.last_message_ts is None else time.time() - self.last_message_ts


class FivePaisaFeed:
    """One socket, resubscribed from the desired-state map on every reconnect.

    The old implementation could end up with *two* live sockets after a handshake that timed out and
    then succeeded, each with its own listener, each publishing every tick. Here the socket is owned
    by a single task and the connect loop is the only thing that creates one.
    """

    def __init__(
        self,
        settings: Settings,
        auth: Authenticator,
        *,
        on_tick: Callable[[dict[str, Any]], Awaitable[None]],
        on_depth: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_oi: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.s = settings
        self.auth = auth
        self.on_tick = on_tick
        self.on_depth = on_depth
        self.on_oi = on_oi
        self.health = FeedHealth()
        self._desired: dict[str, dict[str, Instrument]] = {"mf": {}, "md": {}, "oi": {}}
        self._ws: Any = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    # -- subscription state ----------------------------------------------------------------------

    async def subscribe(self, channel: str, instruments: list[Instrument]) -> None:
        fresh = [i for i in instruments if i.scrip_code not in self._desired[channel]]
        for i in fresh:
            self._desired[channel][i.scrip_code] = i
        self.health.subscriptions = {k: len(v) for k, v in self._desired.items()}
        if fresh and self._ws is not None:
            await self._send_batched(channel, "s", fresh)

    async def unsubscribe(self, channel: str, instruments: list[Instrument]) -> None:
        gone = [i for i in instruments if self._desired[channel].pop(i.scrip_code, None)]
        self.health.subscriptions = {k: len(v) for k, v in self._desired.items()}
        if gone and self._ws is not None:
            await self._send_batched(channel, "u", gone)

    async def _send_batched(self, channel: str, op: str, instruments: list[Instrument]) -> None:
        ws = self._ws
        if ws is None:
            return
        for start in range(0, len(instruments), MAX_ENTRIES_PER_FRAME):
            chunk = instruments[start : start + MAX_ENTRIES_PER_FRAME]
            try:
                await ws.send(build_frame(str(self.s.fp_client_code), channel, op, chunk))
            except Exception as exc:  # noqa: BLE001
                log.warning("feed.send_failed", channel=channel, op=op, error=str(exc))
                return
            await asyncio.sleep(0.05)

    # -- lifecycle --------------------------------------------------------------------------------

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_read()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop's whole job is to survive these
                self.health.connected = False
                self.health.last_error = str(exc)
                self.health.reconnects += 1
                log.warning("feed.reconnect", error=str(exc), backoff_s=round(backoff, 1))
                await asyncio.sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def _connect_and_read(self) -> None:
        session = await self.auth.token()
        url = f"{self.s.endpoints.ws}{session.access_token}|{session.client_code}"
        async with websockets.connect(url, ping_interval=25, ping_timeout=45, max_size=8 << 20) as ws:
            async with self._lock:
                self._ws = ws
            self.health.connected = True
            self.health.connected_since = time.time()
            log.info("feed.connected", subscriptions=self.health.subscriptions)
            for channel, wanted in self._desired.items():
                if wanted:
                    await self._send_batched(channel, "s", list(wanted.values()))
            try:
                async for raw in ws:
                    await self._handle(raw)
            finally:
                async with self._lock:
                    self._ws = None
                self.health.connected = False

    # -- message handling ---------------------------------------------------------------------------

    async def _handle(self, raw: str | bytes) -> None:
        self.health.messages += 1
        self.health.last_message_ts = time.time()
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 - a malformed frame must not kill the reader
            self.health.parse_errors += 1
            return
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                await self._dispatch(row)
            except Exception as exc:  # noqa: BLE001
                self.health.parse_errors += 1
                log.warning("feed.dispatch_failed", error=str(exc))

    async def _dispatch(self, row: dict[str, Any]) -> None:
        if "Details" in row or "MarketDepthData" in row:
            if self.on_depth:
                self.health.depth += 1
                await self.on_depth(self._depth(row))
            return
        if "OpenInterest" in row:
            if self.on_oi:
                self.health.oi += 1
                await self.on_oi(self._oi(row))
            return
        if "LastRate" in row or "Token" in row:
            self.health.ticks += 1
            await self.on_tick(self._tick(row))

    @staticmethod
    def _tick(row: dict[str, Any]) -> dict[str, Any]:
        ts = parse_broker_date(row.get("TickDt")) or float(row.get("Time") or 0) or time.time()
        return {
            "scrip_code": str(row.get("Token")),
            "exch": str(row.get("Exch") or ""),
            "exch_type": str(row.get("ExchType") or ""),
            "ltp": float(row.get("LastRate") or 0),
            "last_qty": int(row.get("LastQty") or 0),
            "total_qty": int(row.get("TotalQty") or 0),
            "open": float(row.get("OpenRate") or 0),
            "high": float(row.get("High") or 0),
            "low": float(row.get("Low") or 0),
            "prev_close": float(row.get("PClose") or 0),
            "avg": float(row.get("AvgRate") or 0),
            "bid": float(row.get("BidRate") or 0),
            "ask": float(row.get("OffRate") or 0),
            "bid_qty": int(row.get("BidQty") or 0),
            "ask_qty": int(row.get("OffQty") or 0),
            "ts": ts,
            "recv_ts": time.time(),
        }

    @staticmethod
    def _oi(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "scrip_code": str(row.get("Token") or row.get("ScripCode")),
            "symbol": str(row.get("Symbol") or ""),
            "open_interest": int(row.get("OpenInterest") or 0),
            "oi_change": int(row.get("OIChange") or 0),
            "oi_change_pct": float(row.get("OIChangePercent") or 0),
            "ltp": float(row.get("LastRate") or 0),
            "volume": int(row.get("Volume") or 0),
            "ts": parse_broker_date(row.get("TickDt")) or time.time(),
            "recv_ts": time.time(),
        }

    @staticmethod
    def _depth(row: dict[str, Any]) -> dict[str, Any]:
        details = row.get("Details") or row.get("MarketDepthData") or []
        bids: list[tuple[float, int]] = []
        asks: list[tuple[float, int]] = []
        for d in details:
            price = float(d.get("Price") or 0)
            qty = int(d.get("Quantity") or d.get("Qty") or 0)
            if price <= 0 or qty <= 0:
                continue
            # BbBuySellFlag: 66 = 'B' (bid), 83 = 'S' (ask) on this feed.
            flag = d.get("BbBuySellFlag", d.get("Flag"))
            (bids if str(flag) in ("66", "B", "b") else asks).append((price, qty))
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return {
            "scrip_code": str(row.get("Token") or row.get("ScripCode")),
            "bids": bids,
            "asks": asks,
            "ts": parse_broker_date(row.get("TickDt")) or time.time(),
            "recv_ts": time.time(),
        }
