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
from .hub import LINE_LIMIT, PRIMED_CHANNELS, HubLink

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
    client_code: str, channel: str, operation: str, instruments: list[Instrument], *, record: bool = False
) -> str:
    body: dict[str, Any] = {
        "ClientCode": client_code,
        "MarketFeedData": [
            {"ExchType": i.exch_type, "Exch": i.exch, "ScripCode": i.scrip_code}
            for i in instruments
        ],
        "Method": METHOD[channel],
        "Operation": OPERATION[operation],
    }
    if record:
        # the feed hub's own marker (twin -> hub only, RECORD-ONLY): the twin's record band, pinned apart from its
        # engine's codes. Never on a broker frame: only the reading side sets it, and the hub re-frames what it sends
        body["Record"] = 1
    return json.dumps(body, separators=(",", ":"))


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
        # -- the pool (operator, 2026-10-05: "whichever subscribes, must put all data in a pool that everyone can
        # have access to without waiting ... no engine will have to duplicate subscription or fetch data again
        # and again") ------------------------------------------------------------------------------------
        #: the last frame of every code this socket (or the hub) carries, whoever asked for it:
        #: (channel, code) -> (row, arrived). The broker sends a code's frame once, to the one socket; the
        #: engine that subscribed second used to discard it as not yet wanted and then wait for the code's
        #: next change (BAJFINANCE 1010 CE, 5 Oct: phase 35 asked at 09:45:04.3, the broker's first frame
        #: 18.00 / 17.90 / 18.05 came at 04.432, phase 34 asked at 04.56, read only its own REST price —
        #: no bid or ask — and gave the strike up at 06.6; the next frame came at 09:45:17)
        self._pool: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        self.pool_primed = 0
        #: serving side: channel -> code -> since when a twin has held it at the broker
        self._pinned_since: dict[str, dict[str, float]] = {"mf": {}, "md": {}, "oi": {}}
        # -- the record set (RECORD-ONLY, operator 2026-10-19: record the 5-read and the strike walls "for research
        # only") --------------------------------------------------------------------------------------------------
        #: channel -> code -> instrument the RECORDER asked for (the record band, ``Engine._rec_sync``): streamed at
        #: the broker beside ``_desired``, but a frame of a code only here never reaches the engine's handlers
        self._record: dict[str, dict[str, Instrument]] = {"mf": {}, "md": {}, "oi": {}}
        #: serving side: twin -> channel -> code -> instrument the twin's RECORDER asked for (its frames carry the hub's
        #: ``Record`` marker): held at the broker like its engine's pins, but never read as the twin's engine holding it
        self._twin_records: dict[str, dict[str, dict[str, Instrument]]] = {}
        #: an engine subscribe of a code only a record set streams, answered from the pool as the broker answers a
        #: fresh subscribe (``_snapshot_from_pool``)
        self.pool_snapshots = 0
        #: reading side: the hub's broker socket was down from here to its ``#up`` — handed to ``on_broker_gap``
        self._hub_down_at: float | None = None
        self.on_broker_gap: Callable[[float, float], None] | None = None
        #: every market-feed and OI row this socket (or the hub) carries, whoever asked for it, handed to the
        #: recorder after — and apart from — the engine's own dispatch: ``(channel, row, arrived, pooled)``. It must
        #: not await and must not raise (it is wrapped anyway)
        self.on_record: Callable[[str, dict[str, Any], float, bool], None] | None = None
        self.record_tap_errors = 0

    # -- subscription state ----------------------------------------------------------------------

    async def subscribe(self, channel: str, instruments: list[Instrument]) -> None:
        fresh = [i for i in instruments if i.scrip_code not in self._desired[channel]]
        now = time.time()
        held = self._twin_codes(channel)
        # RECORD-ONLY: a code only a record set streams (this engine's band, or a twin's) is subscribed exactly as if it
        # were not — live from now, not primed with the pool's aged frame — and its broker snapshot is stood in for by
        # the code's current state, which the pool holds while the band streams it (``_snapshot_from_pool``). On the
        # reading side the hub answers the same way; nothing is deduplicated here (review, 20 Oct)
        own_rec = self._record.get(channel, {})
        rec_only = (set(own_rec) | self._twin_record_codes(channel)) - held if self.hub is None else set()
        pinned = self._pinned_since.get(channel, {})
        for i in fresh:
            self._desired[channel][i.scrip_code] = i
            # a code a twin already holds has streamed through this socket since the twin's pin: a frame
            # from then on is as current for this engine as for the twin
            since = min(now, pinned.get(i.scrip_code, now)) if i.scrip_code in held else now
            self._since.setdefault(channel, {})[i.scrip_code] = since
        self.health.subscriptions = {k: len(v) for k, v in self._desired.items()}
        # the pool's aged frame primes as before (5 Oct) — but not a code only a band streams here, nor, reading the
        # hub, a code this engine's own band holds: the snapshot below (or the hub's) answers those
        skip = rec_only if self.hub is None else set(own_rec)
        await self._prime(channel, [i for i in fresh if i.scrip_code not in skip])
        if rec_only:
            await self._snapshot_from_pool(channel, [i for i in fresh if i.scrip_code in rec_only])
        fresh = [i for i in fresh if i.scrip_code not in held and i.scrip_code not in rec_only]  # already at the broker
        if fresh and self._ws is not None:
            await self._send_batched(channel, "s", fresh)

    async def _snapshot_from_pool(self, channel: str, instruments: list[Instrument]) -> None:
        """RECORD-ONLY: this engine's subscribe of codes only a record set streams, answered as the broker answers a
        fresh subscribe — with the code's current state, now. The record set's stream is live, so the pool's last
        frame IS that state; it is dispatched as a broker frame arriving now (not ``pooled``: a price, a book and an
        OI print, exactly what the broker's snapshot carries), so a book reads what it would without the band, up to
        one round trip sooner. Only while the socket is up and only a frame of this connection: otherwise nothing,
        and the stream's (or the reconnect's) next frame reaches the engine as it lands."""
        connected = self.health.connected_since
        if self._ws is None or not self.health.connected or connected is None:
            return
        now = time.time()
        for i in instruments:
            hit = self._pool.get((channel, i.scrip_code))
            if hit is None or hit[1] < connected:
                continue
            try:
                await self._dispatch(hit[0], now)
                self.pool_snapshots += 1
            except Exception as exc:  # noqa: BLE001 - a stand-in snapshot is a head start, never a reason to stop
                self.health.parse_errors += 1
                log.warning("feed.snapshot_failed", channel=channel, scrip=i.scrip_code, error=str(exc)[:120])

    async def _prime(self, channel: str, instruments: list[Instrument]) -> None:
        """Newly wanted codes' last frames from the pool, dispatched now with the time they ARRIVED and marked
        ``pooled``: the engine reads a price and a book with their true age — never a trade, which the bars
        and the tape already saw when it came."""
        if channel not in PRIMED_CHANNELS:
            return
        for i in instruments:
            hit = self._pool.get((channel, i.scrip_code))
            if hit is None:
                continue
            row, arrived = hit
            try:
                await self._dispatch(row, arrived, pooled=True)
                self.pool_primed += 1
            except Exception as exc:  # noqa: BLE001 - a primed frame is a head start, never a reason to stop
                self.health.parse_errors += 1
                log.warning("feed.prime_failed", channel=channel, scrip=i.scrip_code, error=str(exc)[:120])

    def pooled(self, channel: str, codes: list[str]) -> list[tuple[dict[str, Any], float, float]]:
        """Serving side, for a twin's subscribe: the pool's last frame of each code this socket already
        streams for someone else's ENGINE, with since when it has — ``(row, arrived, since)``."""
        out: list[tuple[dict[str, Any], float, float]] = []
        for code in codes:
            hit = self._pool.get((channel, code))
            if hit is None:
                continue
            since = self.subscribed_since(channel, code)
            if since is None:
                since = self._pinned_since.get(channel, {}).get(code)
            if since is not None:
                out.append((hit[0], hit[1], since))
        return out

    def snapshots(self, channel: str, codes: list[str]) -> list[dict[str, Any]]:
        """Serving side, for a twin's subscribe of codes only a record set streams: each code's current state (the
        pool's last frame of this connection), to be sent as the broker's snapshot would be — fresh, arriving now.
        Nothing while the socket is down."""
        connected = self.health.connected_since
        if self._ws is None or not self.health.connected or connected is None:
            return []
        out = []
        for code in codes:
            hit = self._pool.get((channel, code))
            if hit is not None and hit[1] >= connected:
                out.append(hit[0])
        return out

    def streaming(self, channel: str, *, but_twin: str) -> set[str]:
        """Serving side: the codes the broker streams on ``channel`` for this ENGINE or any twin's engine but one — the
        5 Oct pool's (a record set's codes are ``record_only``'s)."""
        return set(self._desired.get(channel, {})) | self._twin_codes(channel, but=but_twin)

    def record_only(self, channel: str, *, but_twin: str) -> set[str]:
        """Serving side: the codes the broker streams on ``channel`` only for a record set (this engine's band or any
        twin's, the asking twin's included) — no engine's but the asking twin's own."""
        rec = set(self._record.get(channel, {})) | self._twin_record_codes(channel)
        return rec - self.streaming(channel, but_twin=but_twin)

    def pool_stats(self) -> dict[str, int]:
        return {"codes": len(self._pool), "primed": self.pool_primed}

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
        if self.hub is None:
            held = self._twin_codes(channel) | set(self._record.get(channel, {})) | self._twin_record_codes(channel)
            gone = [i for i in gone if i.scrip_code not in held]  # the twin (or a record band) still wants it
        # the reading side always tells the hub: its engine's pin goes, the hub keeps whatever its band still holds
        if gone and self._ws is not None:
            await self._send_batched(channel, "u", gone)

    # -- the record set (RECORD-ONLY) ---------------------------------------------------------------

    async def subscribe_record(self, channel: str, instruments: list[Instrument]) -> list[Instrument]:
        """The recorder's codes: streamed at the broker (sent only when neither this engine nor a twin already
        holds them — on the reading side always, to the hub, marked as the band's), their frames handed to
        ``on_record`` and never to the engine's handlers unless the engine subscribes them itself. Returns the codes
        newly added."""
        if channel not in self._record:
            return []
        mine = self._record[channel]
        fresh = [i for i in instruments if i.scrip_code not in mine]
        for i in fresh:
            mine[i.scrip_code] = i
        send = fresh if self.hub is not None else [i for i in fresh if i.scrip_code not in self._held_but_record(channel)]
        if send and self._ws is not None:
            await self._send_batched(channel, "s", send, record=True)
        return fresh

    async def unsubscribe_record(self, channel: str, instruments: list[Instrument]) -> list[Instrument]:
        """Drop codes from the record set; one this engine or a twin still holds is not unsubscribed at the broker (on
        the reading side the hub decides that)."""
        if channel not in self._record:
            return []
        gone = [i for i in instruments if self._record[channel].pop(i.scrip_code, None)]
        send = gone if self.hub is not None else [i for i in gone if i.scrip_code not in self._held_but_record(channel)]
        if send and self._ws is not None:
            await self._send_batched(channel, "u", send, record=True)
        return gone

    def _held_but_record(self, channel: str) -> set[str]:
        """What else holds codes at the broker on ``channel`` besides this engine's record set."""
        return set(self._desired.get(channel, {})) | self._twin_codes(channel) | self._twin_record_codes(channel)

    def is_record(self, channel: str, scrip_code: str) -> bool:
        return scrip_code in self._record.get(channel, {})

    def record_codes(self, channel: str) -> set[str]:
        return set(self._record.get(channel, {}))

    def record_instruments(self, channel: str) -> dict[str, Instrument]:
        return dict(self._record.get(channel, {}))

    def record_counts(self) -> dict[str, dict[str, int]]:
        """The record set per channel, the part of it this engine does not itself hold (the EXTRA subscriptions),
        and what the socket streams for this engine and its twins together."""
        out: dict[str, dict[str, int]] = {}
        for ch in self._desired:
            rec = set(self._record.get(ch, {}))
            mine = set(self._desired.get(ch, {}))
            out[ch] = {"record": len(rec), "extra": len(rec - mine),
                       "socket": len(mine | rec | self._twin_codes(ch) | self._twin_record_codes(ch))}
        return out

    def _tap_record(self, channel: str, row: dict[str, Any], arrived: float, pooled: bool) -> None:
        tap = self.on_record
        if tap is None or channel == "md":
            return
        try:
            tap(channel, row, arrived, pooled)
        except Exception as exc:  # noqa: BLE001 - the recorder never costs the reader a frame
            self.record_tap_errors += 1
            if self.record_tap_errors <= 5:
                log.warning("feed.record_tap_failed", error=str(exc)[:120])

    # -- the twin's subscriptions (serving side) ---------------------------------------------------

    def _twin_codes(self, channel: str, *, but: str | None = None) -> set[str]:
        """The codes the twins' ENGINES hold on ``channel`` (but one)."""
        return {c for t, chans in self._twins.items() if t != but for c in chans.get(channel, {})}

    def _twin_record_codes(self, channel: str) -> set[str]:
        """The codes the twins' RECORD bands hold on ``channel`` (RECORD-ONLY)."""
        return {c for chans in self._twin_records.values() for c in chans.get(channel, {})}

    def pinned_counts(self) -> dict[str, int]:
        return {ch: len(self._twin_codes(ch)) for ch in self._desired}

    def pinned_record_counts(self) -> dict[str, int]:
        return {ch: len(self._twin_record_codes(ch)) for ch in self._desired}

    async def pin(self, twin: str, channel: str, op: str, instruments: list[Instrument], *, record: bool = False) -> None:
        """The twin's subscribe / unsubscribe, held at the broker on its behalf: a code this engine
        or another twin already holds is not sent again, nor dropped while either still wants it. ``record``: the
        twin's record band (the hub's ``Record`` marker), pinned apart from its engine's codes."""
        if channel not in self._desired:
            return
        book = self._twin_records if record else self._twins
        mine = book.setdefault(twin, {}).setdefault(channel, {})
        pinned = self._pinned_since.setdefault(channel, {})
        if op == "s":
            fresh = [i for i in instruments if i.scrip_code not in mine]
            others = self._broker_held(channel)
            now = time.time()
            for i in fresh:
                mine[i.scrip_code] = i
                if not record:
                    pinned.setdefault(i.scrip_code, now)
            send = [i for i in fresh if i.scrip_code not in others]
        else:
            send = [mine.pop(i.scrip_code) for i in instruments if i.scrip_code in mine]
            if not record:
                for i in send:
                    if i.scrip_code not in self._twin_codes(channel):
                        pinned.pop(i.scrip_code, None)
            others = self._broker_held(channel)
            send = [i for i in send if i.scrip_code not in others]
        if send and self._ws is not None:
            await self._send_batched(channel, op, send)

    def _broker_held(self, channel: str) -> set[str]:
        """Every code the broker streams on ``channel`` through this socket: this engine's, its band's, the twins'."""
        return (set(self._desired.get(channel, {})) | set(self._record.get(channel, {})) | self._twin_codes(channel)
                | self._twin_record_codes(channel))

    async def unpin_all(self, twin: str) -> None:
        for book, record in ((self._twin_records, True), (self._twins, False)):
            for channel, codes in list(book.get(twin, {}).items()):
                await self.pin(twin, channel, "u", list(codes.values()), record=record)
            book.pop(twin, None)

    def _wants(self, row: dict[str, Any]) -> bool:
        return self._wants_cc(row_channel_code(row))

    def _wants_cc(self, cc: tuple[str, str] | None) -> bool:
        """Every engine reads only what it subscribed, as on a socket of its own: the serving side
        skips the rows only its twin asked for, the reading side the rows only the hub's engine did.
        Skipped is not lost: every row is in the pool first (``_handle``)."""
        if cc is None:
            return True
        channel, code = cc
        if code in self._desired.get(channel, {}):
            return True
        if code in self._record.get(channel, {}):
            return False  # the recorder's alone (RECORD-ONLY): it has had the row already
        if self.hub is not None:
            return False
        # membership, not a set of every twin's codes built per row
        return not any(code in chans.get(channel, ()) for book in (self._twins, self._twin_records) for chans in book.values())

    def hub_stats(self) -> dict[str, Any] | None:
        if self.hub is None:
            return None
        return {"role": "connect", "address": f"{self.hub[0]}:{self.hub[1]}", "messages": self.hub_lines,
                "lag_ms": round(self.hub_lag_ms, 2), "lag_max_ms": round(self.hub_lag_max_ms, 2)}

    async def _send_batched(self, channel: str, op: str, instruments: list[Instrument], *, record: bool = False) -> None:
        ws = self._ws
        if ws is None:
            return
        marked = record and self.hub is not None  # the hub's marker, never the broker's
        for start in range(0, len(instruments), MAX_ENTRIES_PER_FRAME):
            chunk = instruments[start : start + MAX_ENTRIES_PER_FRAME]
            try:
                await ws.send(build_frame(str(self.s.fp_client_code), channel, op, chunk, record=marked))
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
        """A fresh broker socket: this engine's desired set, every code a twin's engine holds, then the record sets
        (this engine's band, the twins') — the engines' own codes first (review, 20 Oct)."""
        for channel, wanted in self._desired.items():
            union = {**wanted,
                     **{c: i for t in self._twins.values() for c, i in t.get(channel, {}).items()},
                     **self._record.get(channel, {}),
                     **{c: i for t in self._twin_records.values() for c, i in t.get(channel, {}).items()}}
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
            for channel, rec in self._record.items():  # the band after the engine's own, marked as the band's
                if rec:
                    await self._send_batched(channel, "s", list(rec.values()), record=True)
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
                    self._hub_broker_state(self.health.connected)
                    continue
                if line.startswith(b"="):
                    await self._take_pooled(line[1:])
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

    def _hub_broker_state(self, up: bool) -> None:
        """Reading side: the hub's broker socket down (``#down``) and back (``#up``) is a hole in what this engine's
        recorder saw, whatever repair the engine runs (RECORD-ONLY: ``on_broker_gap``)."""
        now = time.time()
        if not up:
            if self._hub_down_at is None:
                self._hub_down_at = now
            return
        down, self._hub_down_at = self._hub_down_at, None
        if down is not None and self.on_broker_gap is not None:
            try:
                self.on_broker_gap(down, now)
            except Exception as exc:  # noqa: BLE001 - the recorder never costs the link
                log.warning("feed.broker_gap_failed", error=str(exc)[:120])

    async def _take_pooled(self, line: bytes) -> None:
        """Reading side: the hub's pool answering this engine's subscribe for a code the hub already streams
        (``HubServer`` writes ``=<arrived> <since> <row>``) — the frame the broker will not send again. Pooled
        here, and dispatched at once when the code is wanted, with the subscription read as live since the
        hub's own."""
        try:
            stamp, since_s, raw = line.rstrip(b"\r\n").split(b" ", 2)
            arrived, since = float(stamp), float(since_s)
            row = json.loads(raw)
        except (ValueError, TypeError):
            self.health.parse_errors += 1
            return
        cc = row_channel_code(row) if isinstance(row, dict) else None
        if cc is None:
            return
        channel, code = cc
        wanted = code in self._desired.get(channel, {}) and channel in PRIMED_CHANNELS
        if wanted:
            ch_since = self._since.setdefault(channel, {})
            ch_since[code] = min(ch_since.get(code, since), since)
        held = self._pool.get(cc)
        if held is not None and held[1] >= arrived:
            return  # this engine has the frame, or a newer one came off the stream meanwhile
        self._pool[cc] = (row, arrived)
        if wanted:
            try:
                await self._dispatch(row, arrived, pooled=True)
                self.pool_primed += 1
            except Exception as exc:  # noqa: BLE001
                self.health.parse_errors += 1
                log.warning("feed.prime_failed", channel=channel, scrip=code, error=str(exc)[:120])
        self._tap_record(channel, row, arrived, True)

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
            if not isinstance(row, dict):
                continue
            cc = row_channel_code(row)
            if cc is not None:
                self._pool[cc] = (row, arrived)
            if not self._wants_cc(cc):
                continue
            try:
                await self._dispatch(row, arrived)
            except Exception as exc:  # noqa: BLE001
                self.health.parse_errors += 1
                log.warning("feed.dispatch_failed", error=str(exc))
        self.health.note_dispatch(arrived, time.time())
        if self.on_record is not None:
            # the recorder after the engine has the message (review, 20 Oct: 2.6 µs a watched row, off the engine's path)
            for row in rows:
                if isinstance(row, dict):
                    cc = row_channel_code(row)
                    if cc is not None:
                        self._tap_record(cc[0], row, arrived, False)

    async def _dispatch(self, row: dict[str, Any], arrived: float, *, pooled: bool = False) -> None:
        if "Details" in row or "MarketDepthData" in row:
            if self.on_depth:
                depth = self._depth(row, arrived)
                if pooled:
                    depth["pooled"] = True
                else:
                    self.health.depth += 1
                await self.on_depth(depth)
            return
        if "OpenInterest" in row:
            if self.on_oi and not pooled:
                self.health.oi += 1
                await self.on_oi(self._oi(row, arrived))
            return
        if "LastRate" in row or "Token" in row:
            tick = self._tick(row, arrived)
            if pooled:
                tick["pooled"] = True
            else:
                self.health.ticks += 1
            await self.on_tick(tick)

    @staticmethod
    def _tick(row: dict[str, Any], arrived: float | None = None) -> dict[str, Any]:
        exch = parse_broker_date(row.get("TickDt"))
        ts = exch or float(row.get("Time") or 0) or time.time()
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
            # the frame's own exchange time, None when TickDt is missing or .NET's minimum date (a negative date):
            # what the official-open capture trusts (bars/official_open.py). ``ts`` keeps its fallbacks, unchanged
            "exch_ts": exch if exch is not None and exch > 0 else None,
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
