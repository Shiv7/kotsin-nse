"""The market-data seam: what the engine needs from a live data source, whoever supplies it.

Orders always go to 5paisa. *Data* may come from elsewhere: a vendor (Accelpix, GFDL) streams
NSE snapshots about once a second where 5paisa's socket was measured at ~6.5 a minute on MCX, and
keeps tick history 5paisa does not. So the feed is a seam, not a dependency: a source turns its own
wire format into the three messages below, keyed by the engine's instrument key, and nothing above
this module knows which source it is talking to.

The instrument key is the 5paisa scrip code, because the catalogue (and every order) is 5paisa's.
A vendor source maps an :class:`~kotsin_nse.domain.Instrument` to its own ticker on subscribe and
back to ``scrip_code`` on every message.

Every message carries ``source``, the exchange time when the source gives one (``ts``) and when we
received it (``recv_ts``). Two sources are never mixed silently: the rate meter and the archive
record which one each frame came from.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NotRequired, Protocol, TypedDict

from ..domain import Instrument


class Channel(StrEnum):
    QUOTES = "quotes"  # last price, cumulative volume, best bid/ask
    DEPTH = "depth"  # the order book, as many levels as the source has
    OI = "oi"  # open interest (futures and options only — cash equity has none)


class TickMsg(TypedDict):
    source: str
    scrip_code: str
    ltp: float
    #: cumulative traded quantity for the session. Bar volume is its DELTA; a dropped frame
    #: shifts volume to the next bar instead of losing it.
    total_qty: int
    last_qty: int
    bid: float
    ask: float
    bid_qty: int
    ask_qty: int
    open: float
    high: float
    low: float
    prev_close: float
    ts: float
    recv_ts: float
    exch: NotRequired[str]
    exch_type: NotRequired[str]
    avg: NotRequired[float]


class DepthMsg(TypedDict):
    source: str
    scrip_code: str
    bids: list[tuple[float, int]]  # best first
    asks: list[tuple[float, int]]  # best first
    ts: float
    recv_ts: float


class OIMsg(TypedDict):
    source: str
    scrip_code: str
    open_interest: int
    oi_change: int
    oi_change_pct: float
    ltp: float
    volume: int
    ts: float
    recv_ts: float
    symbol: NotRequired[str]


OnTick = Callable[[TickMsg], Awaitable[None]]
OnDepth = Callable[[DepthMsg], Awaitable[None]]
OnOI = Callable[[OIMsg], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a source can and cannot tell us — read by anything that would otherwise assume."""

    name: str
    depth_levels: int
    #: does each quote frame name the side that initiated the trade? No retail NSE feed does;
    #: a footprint built without it is an estimate and must say so.
    trade_side: bool
    #: True when frames are snapshots (trades between two frames are merged), not one per trade
    snapshots: bool
    note: str = ""


class FeedHealthView(Protocol):
    connected: bool
    messages: int
    ticks: int
    depth: int
    oi: int
    reconnects: int
    last_error: str
    subscriptions: dict[str, int]

    @property
    def silence_s(self) -> float | None: ...


class MarketDataSource(Protocol):
    capabilities: Capabilities
    health: FeedHealthView
    #: when the session this source logged in with dies; None if it does not expire
    token_expires_at: float | None

    async def subscribe(self, channel: Channel, instruments: list[Instrument]) -> None: ...

    async def unsubscribe(self, channel: Channel, instruments: list[Instrument]) -> None: ...

    async def run(self) -> None:
        """Connect, keep connected, resubscribe on every reconnect. Returns only on stop()."""

    async def stop(self) -> None: ...

    async def reconnect(self, reason: str = "") -> None: ...


__all__ = [
    "Any",
    "Capabilities",
    "Channel",
    "DepthMsg",
    "MarketDataSource",
    "OIMsg",
    "OnDepth",
    "OnOI",
    "OnTick",
    "TickMsg",
]
