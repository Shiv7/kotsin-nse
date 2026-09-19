"""The bar every strategy sees.

One record merges the tape (OHLCV), the derivatives leg (open interest on the front future) and the
session context (VWAP, prior-day close). Strategies never join those themselves — the FUDKII family
needed OI on a *future* while trading a signal computed on *cash*, and every service that did that
join by hand got it wrong at least once.

``source`` is part of the contract:

* ``live``   — built from ticks we received end to end;
* ``rest``   — backfilled from the broker's historical endpoint at boot;
* ``partial`` — the socket (re)connected mid-bucket, so the first trades of that minute are missing.
  A partial bar is kept for indicator warmth and **excluded** from any determinism check. Silently
  treating it as complete is how a reconnect turns into a phantom volume collapse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BarSource(StrEnum):
    LIVE = "live"
    REST = "rest"
    PARTIAL = "partial"


@dataclass(slots=True)
class UnifiedBar:
    symbol: str
    scrip_code: str
    tf: str
    ts: int  # bucket START, epoch seconds UTC. A 5m bar labelled 11:00 IST covers 11:00–11:05.
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int = 0
    source: BarSource = BarSource.LIVE
    complete: bool = False
    #: session context, stamped by the aggregator on every bar (not only the last one — CAN2's
    #: n-of-3 test read a VWAP that was one cycle stale on two of the three bars it examined)
    vwap: float | None = None
    prev_close: float | None = None
    #: derivatives leg, from the front-month future of the same underlying
    oi: int | None = None
    oi_change_pct: float | None = None
    fut_scrip_code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def has_volume(self) -> bool:
        return self.volume > 0

    @property
    def has_oi(self) -> bool:
        return self.oi is not None

    def merge_tick(self, price: float, qty: float) -> None:
        if price <= 0:
            return
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += qty
        self.trades += 1

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "scrip_code": self.scrip_code,
            "tf": self.tf,
            "ts": self.ts,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "trades": self.trades,
            "source": self.source.value,
            "complete": self.complete,
            "vwap": self.vwap,
            "prev_close": self.prev_close,
            "oi": self.oi,
            "oi_change_pct": self.oi_change_pct,
        }
