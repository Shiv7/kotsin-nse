"""The board, live: the engine's own bar store read the way the replay reads the cache.

Each minute, two seconds after the 1m bars close, every underlying's state is rebuilt from today's
closed 1m bars (open, running high/low, last close, VWAP) plus the previous close and the guarded
ADR from its daily bars, and :func:`~kotsin_nse.scan.board.build_board` runs over all of them —
the same function the replay calls.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import date

from ..bars.store import BarStore
from ..market.session import ist_day
from .board import COMMODITY, Board, OIRead, build_board
from .replay import guarded_adr
from .sectors import sectors
from .strength import DayState


class LiveScanner:
    def __init__(self, store: BarStore) -> None:
        self.store = store
        self.sectors = sectors()
        self._day: date | None = None
        self._daily: dict[str, tuple[float, float | None]] = {}  # symbol → (prev close, adr)
        self.board: Board | None = None
        self.scans = 0

    def _daily_for(self, symbol: str, today: date) -> tuple[float, float | None] | None:
        if self._day != today:
            self._day, self._daily = today, {}
        hit = self._daily.get(symbol)
        if hit is not None:
            return hit
        prior = [b for b in self.store.bars(symbol, "1d") if ist_day(b.ts) < today]
        if not prior:
            return None
        out = (
            prior[-1].close,
            guarded_adr([b.high for b in prior], [b.low for b in prior], [b.close for b in prior]),
        )
        self._daily[symbol] = out
        return out

    def state(self, symbol: str, now: float) -> DayState | None:
        today = ist_day(now)
        bars = [
            b for b in self.store.bars(symbol, "1m") if ist_day(b.ts) == today and b.ts + 60 <= now
        ]
        if not bars:
            return None
        daily = self._daily_for(symbol, today)
        if daily is None:
            return None
        prev_close, adr = daily
        pv = vol = 0.0
        for b in bars:
            if b.volume > 0:
                pv += (b.high + b.low + b.close) / 3 * b.volume
                vol += b.volume
        return DayState(
            symbol=symbol,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            last=bars[-1].close,
            prev_close=prev_close,
            adr=adr,
            volume=vol,
            vwap=pv / vol if vol > 0 else None,
        )

    def scan(
        self,
        symbols: Iterable[str],
        now: float,
        oi: Mapping[str, OIRead] | None = None,
        *,
        commodities: Collection[str] = (),
    ) -> Board:
        """``commodities`` are shown under their own sector and never picked."""
        states = [s for sym in symbols if (s := self.state(sym, now)) is not None]
        sectors = {**self.sectors, **dict.fromkeys(commodities, COMMODITY)}
        no_pick = dict.fromkeys(commodities, "NOT PICKED · MCX, rule untested")
        self.board = build_board(int(now), states, sectors, oi, no_pick=no_pick)
        self.scans += 1
        return self.board

