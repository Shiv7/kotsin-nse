"""What the engine needs from a broker. One implementation today (5paisa); the seam exists so the
strategy and risk code never names a broker — the NSE stack could not be tested offline because
``FivePaisaClient`` was reachable from everywhere."""

from __future__ import annotations

from typing import Any, Protocol

from ..domain import Instrument, OrderSide


class VenueError(RuntimeError):
    """Any broker-side failure. Carries the raw response so an audit row can hold the truth."""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.raw = raw


class Quote(Protocol):
    ltp: float
    prev_close: float
    high: float
    low: float
    bid: float
    ask: float
    volume: int
    ts: float


class BrokerREST(Protocol):
    async def quotes(self, instruments: list[Instrument]) -> dict[str, Any]: ...

    async def candles(
        self, instrument: Instrument, interval: str, start: str, end: str
    ) -> list[dict[str, Any]]: ...

    async def place_order(
        self,
        instrument: Instrument,
        side: OrderSide,
        qty: int,
        *,
        price: float = 0.0,
        intraday: bool = True,
        remote_order_id: str,
    ) -> str: ...

    async def cancel_order(self, exch_order_id: str) -> None: ...

    async def net_positions(self) -> list[dict[str, Any]]: ...
