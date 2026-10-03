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
from .hub import LINE_LIMIT, HubLink

log = structlog.get_logger(__name__)

METHOD = {"mf": "MarketFeedV3", "md": "MarketDepthService", "oi": "GetScripInfoForFuture"}
OPERATION = {"s": "Subscribe", "u": "Unsubscribe"}

#: The broker's 20-level depth frames are large; a batch bigger than this is split.
MAX_ENTRIES_PER_FRAME = 200
#: a connection that lived this long was healthy: the next drop starts the backoff from 1 s again
HEALTHY_CONNECTION_S = 60.0
#: the twin reading from a feed hub retries this often at most: the hub is on this machine
HUB_BACKOFF_MAX_S = 5.0


def row_channel_code(row: dict[str, Any]) -> tuple[str, str] | None:
    """Which subscription a broker row answers — the channel ``_dispatch`` routes it to, and its code."""
    code = row.get("Token") or row.get("ScripCode")
    if code is None:
        return None
    if "Details" in row or "MarketDepthData" in row:
        return "md", str(code)
    if "OpenInterest" in row:
        return "oi", str(code)
    if "LastRate" in row or "Token" in row:
        return "mf", str(code)
    return None


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
    if not digits or digits == "-":
        return None
    ms = int(digits)
    return ms / 1000 if ms > 0 else None  # .NET's minimum date (negative) is "no date", not 1969


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
    #: Milliseconds between a frame ARRIVING at the socket and this process getting round to it.
    #: Zero on an idle loop. It is the backlog, and it is the number that matters: a book is only
    #: as fresh as the reader's last turn, so when this climbs every book in the engine is stale at
    #: once — which is what refused three orders and halted every book on 2026-09-24.
    dispatch_lag_ms: float = 0.0
    dispatch_lag_max_ms: float = 0.0
    frames_behind: int = 0

    def note_dispatch(self, arrived: float, done: float) -> None:
        lag = max(0.0, (done - arrived) * 1000)
        # a decayed mean, so one slow frame does not read as a stall and a real one cannot hide
        self.dispatch_lag_ms = lag if self.dispatch_lag_ms == 0 else self.dispatch_lag_ms * 0.9 + lag * 0.1
        self.dispatch_lag_max_ms = max(self.dispatch_lag_max_ms, lag)
        if lag > 1000:
            self.frames_behind += 1

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
        on_connect: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self.s = settings
        #: called after every connect with ``True`` when it is a RE-connect — the bars and volume
        #: baselines that span the gap are the aggregator's to repair
        self.on_connect = on_connect
        self._connects = 0
        self.auth = auth
        self.on_tick = on_tick
        self.on_depth = on_depth
        self.on_oi = on_oi
        self.health = FeedHealth()
        self._desired: dict[str, dict[str, Instrument]] = {"mf": {}, "md": {}, "oi": {}}
        #: channel -> code -> when the code last JOINED the desired set: a value held from before is
        #: from an earlier subscription, and nothing since says it still stands
        self._since: dict[str, dict[str, float]] = {"mf": {}, "md": {}, "oi": {}}
        self._ws: Any = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        #: expiry of the token this socket was opened with; None until connected
        self.token_expires_at: float | None = None
        # -- feed hub (hub.py; phase 34 and phase 35 on one broker socket, 2026-10-04) --------------
        #: serving side: every raw message, the instant it is read, and the socket's up/down
        self.tap: Callable[[str | bytes, float], None] | None = None
        self.on_state: Callable[[bool], None] | None = None
        #: serving side: twin -> channel -> code -> instrument the twin asked for (held at the broker)
        self._twins: dict[str, dict[str, dict[str, Instrument]]] = {}
        #: reading side: the hub to read from instead of opening a broker socket
        self.hub: tuple[str, int] | None = None
        self.hub_lines = 0
        self.hub_lag_ms = 0.0
        self.hub_lag_max_ms = 0.0

    # -- subscription state ----------------------------------------------------------------------

    async def subscribe(self, channel: str, instruments: list[Instrument]) -> None:
        fresh = [i for i in instruments if i.scrip_code not in self._desired[channel]]
        now = time.time()
        for i in fresh:
            self._desired[channel][i.scrip_code] = i
            self._since.setdefault(channel, {})[i.scrip_code] = now
        self.health.subscriptions = {k: len(v) for k, v in self._desired.items()}
        held = self._twin_codes(channel)
        fresh = [i for i in fresh if i.scrip_code not in held]  # already at the broker for the twin
        if fresh and self._ws is not None:
            await self._send_batched(channel, "s", fresh)

    def is_subscribed(self, channel: str, scrip_code: str) -> bool:
        """Is ``scrip_code`` on ``channel`` in the desired set — what every (re)connect subscribes."""
        return scrip_code in self._desired.get(channel, {})

    def subscribed_since(self, channel: str, scrip_code: str) -> float | None:
        """When ``scrip_code`` last joined ``channel``'s desired set; None when it is not on it."""
        return self._since.get(channel, {}).get(scrip_code) if self.is_subscribed(channel, scrip_code) else None

    async def unsubscribe(self, channel: str, instruments: list[Instrument]) -> None:
        gone = [i for i in instruments if self._desired[channel].pop(i.scrip_code, None)]
        for i in gone:
            self._since.get(channel, {}).pop(i.scrip_code, None)
        self.health.subscriptions = {k: len(v) for k, v in self._desired.items()}
        held = self._twin_codes(channel)
        gone = [i for i in gone if i.scrip_code not in held]  # the twin still wants it
        if gone and self._ws is not None:
            await self._send_batched(channel, "u", gone)

    # -- the twin's subscriptions (serving side) ---------------------------------------------------

    def _twin_codes(self, channel: str, *, but: str | None = None) -> set[str]:
        return {c for t, chans in self._twins.items() if t != but for c in chans.get(channel, {})}

    def pinned_counts(self) -> dict[str, int]:
        return {ch: len(self._twin_codes(ch)) for ch in self._desired}

    async def pin(self, twin: str, channel: str, op: str, instruments: list[Instrument]) -> None:
        """The twin's subscribe / unsubscribe, held at the broker on its behalf: a code this engine
        or another twin already holds is not sent again, nor dropped while either still wants it."""
        if channel not in self._desired:
            return
        mine = self._twins.setdefault(twin, {}).setdefault(channel, {})
        others = self._twin_codes(channel, but=twin) | set(self._desired[channel])
        if op == "s":
            fresh = [i for i in instruments if i.scrip_code not in mine]
            for i in fresh:
                mine[i.scrip_code] = i
            send = [i for i in fresh if i.scrip_code not in others]
        else:
            send = [mine.pop(i.scrip_code) for i in instruments if i.scrip_code in mine]
            send = [i for i in send if i.scrip_code not in others]
        if send and self._ws is not None:
            await self._send_batched(channel, op, send)

    async def unpin_all(self, twin: str) -> None:
        for channel, codes in list(self._twins.get(twin, {}).items()):
            await self.pin(twin, channel, "u", list(codes.values()))
        self._twins.pop(twin, None)

    def _wants(self, row: dict[str, Any]) -> bool:
        """Every engine reads only what it subscribed, as on a socket of its own: the serving side
        skips the rows only its twin asked for, the reading side the rows only the hub's engine did."""
        cc = row_channel_code(row)
        if cc is None:
            return True
        channel, code = cc
        if code in self._desired.get(channel, {}):
            return True
        if self.hub is not None:
            return False
        return code not in self._twin_codes(channel)

    def hub_stats(self) -> dict[str, Any] | None:
        if self.hub is None:
            return None
        return {"role": "connect", "address": f"{self.hub[0]}:{self.hub[1]}", "messages": self.hub_lines,
                "lag_ms": round(self.hub_lag_ms, 2), "lag_max_ms": round(self.hub_lag_max_ms, 2)}

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
            attempt = time.time()
            try:
                await self._connect_and_read()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop's whole job is to survive these
                self.health.connected = False
                self.health.last_error = str(exc)
                self.health.reconnects += 1
                # A socket that lived a healthy minute earns a fresh backoff. Only a CLEAN close
                # used to reset it, so after one bad patch every later drop — hours apart — waited
                # the full 60 s: a minute of blind tape per drop, in the middle of a session.
                since = self.health.connected_since
                if since is not None and since >= attempt and time.time() - since >= HEALTHY_CONNECTION_S:
                    backoff = 1.0
                log.warning("feed.reconnect", error=str(exc), backoff_s=round(backoff, 1))
                await asyncio.sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, HUB_BACKOFF_MAX_S if self.hub is not None else 60.0)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def reconnect(self, reason: str = "") -> None:
        """Drop the socket; the run loop reconnects with ``auth.token()`` — a fresh login if the
        old one expired. Needed nightly: the socket was opened with a JWT that dies at 23:59:59 IST,
        and a socket that stays "connected" on a dead token is the worst kind of feed — it looks
        alive and delivers nothing at the open."""
        log.info("feed.reconnect_requested", reason=reason)
        self.token_expires_at = None
        if self._ws is not None:
            await self._ws.close()

    async def _connect_and_read(self) -> None:
        if self.hub is not None:
            await self._connect_hub_and_read()
            return
        session = await self.auth.token()
        self.token_expires_at = session.expires_at
        url = f"{self.s.endpoints.ws}{session.access_token}|{session.client_code}"
        async with websockets.connect(url, ping_interval=25, ping_timeout=45, max_size=8 << 20) as ws:
            async with self._lock:
                self._ws = ws
            self.health.connected = True
            self.health.connected_since = time.time()
            log.info("feed.connected", subscriptions=self.health.subscriptions)
            await self._subscribe_everything()
            self._state(True)
            again = self._connects > 0
            self._connects += 1
            if self.on_connect is not None:
                try:
                    await self.on_connect(again)
                except Exception as exc:  # noqa: BLE001 - a repair hook never costs the socket
                    log.warning("feed.on_connect_failed", error=str(exc)[:160])
            try:
                async for raw in ws:
                    await self._handle(raw)
            finally:
                async with self._lock:
                    self._ws = None
                self.health.connected = False
                self._state(False)

    async def _subscribe_everything(self) -> None:
        """A fresh broker socket: this engine's desired set and every code a twin holds through it."""
        for channel, wanted in self._desired.items():
            union = {**wanted, **{c: i for t in self._twins.values() for c, i in t.get(channel, {}).items()}}
            if union:
                await self._send_batched(channel, "s", list(union.values()))

    def _state(self, up: bool) -> None:
        if self.on_state is not None:
            try:
                self.on_state(up)
            except Exception as exc:  # noqa: BLE001 - the twin never costs the socket
                log.warning("feed_hub.state_failed", error=str(exc)[:120])

    async def _connect_hub_and_read(self) -> None:
        """The twin's side: read the hub's lines as if they came off a broker socket of its own.
        ``connected`` follows the hub's broker socket (``#up`` / ``#down``), not the local link."""
        assert self.hub is not None
        host, port = self.hub
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=LINE_LIMIT), timeout=5.0)
        async with self._lock:
            self._ws = HubLink(writer)
        self.health.connected_since = time.time()
        log.info("feed.hub_connected", hub=f"{host}:{port}", subscriptions=self.health.subscriptions)
        try:
            for channel, wanted in self._desired.items():
                if wanted:
                    await self._send_batched(channel, "s", list(wanted.values()))
            again = self._connects > 0
            self._connects += 1
            if self.on_connect is not None:
                try:
                    await self.on_connect(again)
                except Exception as exc:  # noqa: BLE001 - a repair hook never costs the link
                    log.warning("feed.on_connect_failed", error=str(exc)[:160])
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("feed hub closed the link")
                if line.startswith(b"#"):
                    self.health.connected = line.strip() == b"#up"
                    continue
                stamp, _, raw = line.partition(b" ")
                try:
                    lag = max(0.0, (time.time() - float(stamp)) * 1000)
                except ValueError:
                    self.health.parse_errors += 1
                    continue
                self.hub_lines += 1
                self.hub_lag_ms = lag if self.hub_lines == 1 else self.hub_lag_ms * 0.99 + lag * 0.01
                self.hub_lag_max_ms = max(self.hub_lag_max_ms, lag)
                await self._handle(raw.rstrip(b"\r\n"))
        finally:
            async with self._lock:
                self._ws = None
            self.health.connected = False
            writer.close()

    # -- message handling ---------------------------------------------------------------------------

    async def _handle(self, raw: str | bytes) -> None:
        # Stamped the instant the frame comes off the socket, BEFORE any work. Everything
        # downstream ages from this, so "how old is this book" answers the real question — how old
        # is the data — rather than "how long since we last got round to it", which is what a
        # dispatch-time stamp measures and why a busy loop looked like a stale exchange.
        arrived = time.time()
        if self.tap is not None:
            # to the twin before any work of ours: its delay is the loopback hop, not our processing
            try:
                self.tap(raw, arrived)
            except Exception as exc:  # noqa: BLE001 - the twin never costs the socket
                log.warning("feed_hub.tap_failed", error=str(exc)[:120])
        self.health.messages += 1
        self.health.last_message_ts = arrived
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 - a malformed frame must not kill the reader
            self.health.parse_errors += 1
            return
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict) or not self._wants(row):
                continue
            try:
                await self._dispatch(row, arrived)
            except Exception as exc:  # noqa: BLE001
                self.health.parse_errors += 1
                log.warning("feed.dispatch_failed", error=str(exc))
        self.health.note_dispatch(arrived, time.time())

    async def _dispatch(self, row: dict[str, Any], arrived: float) -> None:
        if "Details" in row or "MarketDepthData" in row:
            if self.on_depth:
                self.health.depth += 1
                await self.on_depth(self._depth(row, arrived))
            return
        if "OpenInterest" in row:
            if self.on_oi:
                self.health.oi += 1
                await self.on_oi(self._oi(row, arrived))
            return
        if "LastRate" in row or "Token" in row:
            self.health.ticks += 1
            await self.on_tick(self._tick(row, arrived))

    @staticmethod
    def _tick(row: dict[str, Any], arrived: float | None = None) -> dict[str, Any]:
        # ``Time`` is seconds since midnight, not an epoch: as a fallback it put a frame in 1970.
        # No usable TickDt → the arrival time.
        ts = parse_broker_date(row.get("TickDt")) or (arrived if arrived is not None else time.time())
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
            "recv_ts": arrived if arrived is not None else time.time(),
            "dispatch_ts": time.time(),
        }

    @staticmethod
    def _oi(row: dict[str, Any], arrived: float | None = None) -> dict[str, Any]:
        return {
            "scrip_code": str(row.get("Token") or row.get("ScripCode")),
            "symbol": str(row.get("Symbol") or ""),
            "open_interest": int(row.get("OpenInterest") or 0),
            "oi_change": int(row.get("OIChange") or 0),
            "oi_change_pct": float(row.get("OIChangePercent") or 0),
            #: the broker's own time when the frame carries one — GetScripInfoForFuture frames have
            #: been seen without TickDt (2026-10-03); ``ts`` then falls back to the arrival
            "tick_ts": parse_broker_date(row.get("TickDt")),
            "ltp": float(row.get("LastRate") or 0),
            "volume": int(row.get("Volume") or 0),
            "ts": parse_broker_date(row.get("TickDt")) or time.time(),
            "recv_ts": arrived if arrived is not None else time.time(),
            "dispatch_ts": time.time(),
        }

    @staticmethod
    def _depth(row: dict[str, Any], arrived: float | None = None) -> dict[str, Any]:
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
            "recv_ts": arrived if arrived is not None else time.time(),
            "dispatch_ts": time.time(),
        }
