"""Feed hub: one 5paisa connection feeding two engines.

Operator, 2026-10-03/04: phase 34 and phase 35 run side by side on one 5paisa account — "can we not treat
it like a single pipeline feeding 2 engines/buckets simultaneously like how most FUDKII_RT and its variants
and twin operate?" The engines differ inside the pipeline (quote clocks, zones, SuperTrend history), so
they cannot be books of one engine; what they can share is the source. The engine named ``serve`` keeps
the only broker socket and passes every raw message on, the instant it comes off the socket and before
any work of its own; the engine named ``connect`` reads them instead of opening a socket and decodes them
with its own code. Configured in ``<data_dir>/engine.json``::

    {"feed_hub": {"serve": "127.0.0.1:8510"}}      # phase 34
    {"feed_hub": {"connect": "127.0.0.1:8510"}}    # phase 35

Wire, newline-framed on a loopback TCP socket:

- hub → twin: ``<arrived epoch s> <raw broker message>`` per message; ``#up`` / ``#down`` when the
  broker socket opens / closes (and once on connect).
- twin → hub: the twin's own 5paisa subscription frames, verbatim. The hub subscribes them at the
  broker on the twin's behalf (held across its reconnects), and never unsubscribes a code the other
  side still wants.

The hub never waits on the twin: a twin whose unsent backlog passes ``MAX_CLIENT_BUFFER`` is dropped
(it reconnects and re-subscribes) rather than slow the engine that trades."""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ...config import Segment
from ...domain import Instrument, InstrumentKind

if TYPE_CHECKING:
    from .ws import FivePaisaFeed

log = structlog.get_logger(__name__)

FILE = "engine.json"
#: a twin this far behind is dropped, never waited for
MAX_CLIENT_BUFFER = 4 << 20
#: depth frames run to tens of kB; the line reader's limit sits well above the largest
LINE_LIMIT = 8 << 20
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_CHANNEL = {"MarketFeedV3": "mf", "MarketDepthService": "md", "GetScripInfoForFuture": "oi"}
_OP = {"Subscribe": "s", "Unsubscribe": "u"}


@dataclass(frozen=True, slots=True)
class HubConfig:
    serve: tuple[str, int] | None = None
    connect: tuple[str, int] | None = None


def _addr(text: Any) -> tuple[str, int] | None:
    host, _, port = str(text or "").rpartition(":")
    if host not in _LOOPBACK or not port.isdigit():
        if text:
            log.warning("feed_hub.refused", address=str(text), why="the hub is loopback only: host:port on this machine")
        return None
    return host, int(port)


def read_hub_config(data_dir: Path) -> HubConfig:
    """``feed_hub`` from ``engine.json``; none (the engine's own broker socket) when absent."""
    try:
        raw = json.loads((data_dir / FILE).read_text()).get("feed_hub") or {}
    except (OSError, ValueError, AttributeError):
        return HubConfig()
    serve, connect = _addr(raw.get("serve")), _addr(raw.get("connect"))
    if serve and connect:
        log.warning("feed_hub.refused", why="serve and connect both set — an engine is one or the other")
        return HubConfig()
    return HubConfig(serve=serve, connect=connect)


def wire_instruments(entries: list[dict[str, Any]]) -> list[Instrument]:
    """A subscription frame's entries back into instruments: the wire identity is all a frame needs."""
    out = []
    for e in entries:
        code = str(e.get("ScripCode") or "")
        if not code:
            continue
        if str(e.get("Exch")) == "M":
            seg, kind = Segment.MCX_FO, InstrumentKind.FUTURE
        elif str(e.get("ExchType")) == "C":
            seg, kind = Segment.NSE_EQ, InstrumentKind.EQUITY
        else:
            seg, kind = Segment.NSE_FO, InstrumentKind.FUTURE
        out.append(Instrument(code, "", seg, kind))
    return out


class HubServer:
    """The serving side, owned by the engine that keeps the broker socket."""

    def __init__(self, feed: FivePaisaFeed, host: str, port: int) -> None:
        self.feed = feed
        self.host, self.port = host, port
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[str, asyncio.StreamWriter] = {}
        self._ids = itertools.count(1)
        self.sent = 0
        self.dropped = 0
        self._up = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, self.port, limit=LINE_LIMIT)
        sock = self._server.sockets[0].getsockname() if self._server.sockets else None
        if sock:
            self.port = int(sock[1])
        self.feed.tap = self.tap
        self.feed.on_state = self.state
        log.info("feed_hub.serving", address=f"{self.host}:{self.port}")

    async def stop(self) -> None:
        for w in list(self._clients.values()):
            w.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # -- called by the feed, on its own loop turn: never awaits -------------------------------------

    def tap(self, raw: str | bytes, arrived: float) -> None:
        if not self._clients:
            return
        body = raw if isinstance(raw, bytes) else raw.encode()
        line = f"{arrived:.6f} ".encode() + body.replace(b"\n", b" ").replace(b"\r", b" ") + b"\n"
        for cid, w in list(self._clients.items()):
            self._write(cid, w, line)
        self.sent += 1

    def state(self, up: bool) -> None:
        self._up = up
        for cid, w in list(self._clients.items()):
            self._write(cid, w, b"#up\n" if up else b"#down\n")

    def _write(self, cid: str, w: asyncio.StreamWriter, line: bytes) -> None:
        if w.is_closing():
            return
        if w.transport.get_write_buffer_size() > MAX_CLIENT_BUFFER:
            self.dropped += 1
            log.warning("feed_hub.twin_dropped", twin=cid, why="too far behind; it reconnects and re-subscribes")
            w.close()
            return
        w.write(line)

    # -- one twin ---------------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        cid = f"twin-{next(self._ids)}"
        self._clients[cid] = writer
        log.info("feed_hub.twin_connected", twin=cid, peer=str(peer))
        writer.write(b"#up\n" if self._up else b"#down\n")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line)
                    channel, op = _CHANNEL[frame["Method"]], _OP[frame["Operation"]]
                    instruments = wire_instruments(list(frame.get("MarketFeedData") or []))
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("feed_hub.bad_request", twin=cid, error=str(exc)[:120])
                    continue
                await self.feed.pin(cid, channel, op, instruments)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            log.warning("feed_hub.twin_error", twin=cid, error=str(exc)[:120])
        finally:
            self._clients.pop(cid, None)
            writer.close()
            await self.feed.unpin_all(cid)
            log.info("feed_hub.twin_disconnected", twin=cid)

    def stats(self) -> dict[str, Any]:
        return {"role": "serve", "address": f"{self.host}:{self.port}", "twins": len(self._clients),
                "messages_sent": self.sent, "twins_dropped": self.dropped, "pinned": self.feed.pinned_counts()}


class HubLink:
    """The twin's side of the socket, standing in for the broker websocket: ``send`` takes the same
    subscription frames, ``close`` drops it (the feed's run loop reconnects)."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.w = writer

    async def send(self, text: str) -> None:
        self.w.write(text.encode() + b"\n")
        await self.w.drain()

    async def close(self) -> None:
        self.w.close()


__all__ = ["FILE", "LINE_LIMIT", "MAX_CLIENT_BUFFER", "HubConfig", "HubLink", "HubServer", "read_hub_config", "wire_instruments"]
