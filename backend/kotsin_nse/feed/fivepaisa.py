"""5paisa's openfeed socket as a :class:`~kotsin_nse.feed.base.MarketDataSource`.

A thin adapter: the socket itself (``venue/fivepaisa/ws.py``) keeps its byte-exact wire format and
its own channel names; this maps the generic channels onto them and stamps ``source`` on every
message. Measured facts about what it delivers are in ``docs/FIVEPAISA_FACTS.md``.
"""

from __future__ import annotations

from typing import Any, cast

from ..config import Settings
from ..domain import Instrument
from ..venue.fivepaisa.auth import Authenticator
from ..venue.fivepaisa.ws import FeedHealth, FivePaisaFeed
from .base import Capabilities, Channel, DepthMsg, OIMsg, OnDepth, OnOI, OnTick, TickMsg

NAME = "fivepaisa"
WIRE = {Channel.QUOTES: "mf", Channel.DEPTH: "md", Channel.OI: "oi"}
CAPABILITIES = Capabilities(
    name=NAME,
    depth_levels=20,
    trade_side=False,
    snapshots=True,
    note="MarketFeedV3 is a snapshot of LastRate and cumulative TotalQty, ~6.5 frames/min/symbol "
    "measured on MCX (2026-09-21); NSE not yet measured",
)


class FivePaisaSource:
    capabilities = CAPABILITIES

    def __init__(
        self,
        settings: Settings,
        auth: Authenticator,
        *,
        on_tick: OnTick,
        on_depth: OnDepth | None = None,
        on_oi: OnOI | None = None,
    ) -> None:
        async def tick(row: dict[str, Any]) -> None:
            row["source"] = NAME
            await on_tick(cast(TickMsg, row))

        async def depth(row: dict[str, Any]) -> None:
            row["source"] = NAME
            if on_depth is not None:
                await on_depth(cast(DepthMsg, row))

        async def oi(row: dict[str, Any]) -> None:
            row["source"] = NAME
            if on_oi is not None:
                await on_oi(cast(OIMsg, row))

        self.feed = FivePaisaFeed(settings, auth, on_tick=tick, on_depth=depth, on_oi=oi)

    @property
    def health(self) -> FeedHealth:
        return self.feed.health

    @property
    def token_expires_at(self) -> float | None:
        return self.feed.token_expires_at

    async def subscribe(self, channel: Channel, instruments: list[Instrument]) -> None:
        await self.feed.subscribe(WIRE[channel], instruments)

    async def unsubscribe(self, channel: Channel, instruments: list[Instrument]) -> None:
        await self.feed.unsubscribe(WIRE[channel], instruments)

    async def run(self) -> None:
        await self.feed.run()

    async def stop(self) -> None:
        await self.feed.stop()

    async def reconnect(self, reason: str = "") -> None:
        await self.feed.reconnect(reason)
