"""Ticks → 1m bars → higher timeframes, bucketed on **trade time**, anchored on the session open.

Four decisions here, each one a bug the old stack actually shipped:

1. **Bucket by the tick's own timestamp, never by arrival order.** A trade that matches at 11:04:59
   and reaches us at 11:05:00 belongs to the 11:00 bar. Bucketing by arrival put it in the next one
   and made our bars disagree with the broker's in cancelling pairs.
2. **Volume is the delta of the cumulative day total**, not a sum of ``LastQty``. ``TotalQty`` is
   monotonic within a session, so a dropped tick costs us the *attribution* of that volume to a bar
   but never the volume itself; summing ``LastQty`` loses it permanently.
3. **The opening minute is 09:15 and it is included.** The old ``tick_candles_1m`` started at 09:16
   and lost the minute that often holds the day's extreme.
4. **A bucket that began before we connected is tagged ``PARTIAL``** and excluded from any
   determinism check, instead of quietly looking like a volume collapse.

Higher timeframes are rolled from *closed* 1m bars, so a 30m bar exists only once its last minute
has closed — which is exactly when FUDKII's boundary should fire.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..config import Segment
from ..domain import Instrument
from ..market.session import TF_SECONDS, bucket_start, ist_day, session_open_ts
from .store import BarStore
from .unified import BarSource, UnifiedBar

log = structlog.get_logger(__name__)

#: Timeframes the engine maintains. 30m is FUDKII's and FUKAA's decision frame; the rest are for
#: the chart, the ATR used by risk, and future strategies.
TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "30m")


@dataclass(slots=True)
class SymbolState:
    instrument: Instrument
    last_total_qty: int = 0
    day: str = ""
    session_pv: float = 0.0  # Σ typical × volume, for the session VWAP
    session_v: float = 0.0
    prev_close: float | None = None
    oi: int | None = None
    oi_change_pct: float | None = None
    fut_scrip_code: str | None = None
    connected_since: float = field(default_factory=time.time)
    ticks: int = 0


class Aggregator:
    """One instance owns every symbol's bars. Single-threaded by construction."""

    def __init__(
        self,
        store: BarStore,
        *,
        on_bar_close: Callable[[UnifiedBar], Awaitable[None]] | None = None,
        timeframes: tuple[str, ...] = TIMEFRAMES,
    ) -> None:
        self.store = store
        self.on_bar_close = on_bar_close
        self.timeframes = timeframes
        self.state: dict[str, SymbolState] = {}
        self.bars_closed = 0
        self.partial_bars = 0
        self.late_ticks = 0

    # -- registration ------------------------------------------------------------------------------

    def track(self, instrument: Instrument) -> None:
        self.state.setdefault(instrument.scrip_code, SymbolState(instrument=instrument))

    def set_oi(self, scrip_code: str, *, oi: int, change_pct: float, fut_code: str) -> None:
        """Stamp the underlying's OI from its front-month future.

        Cash equity has no open interest. Reading OI off the cash segment is what pinned 15% of
        MicroAlpha's conviction score at exactly zero for its whole life; the future's code is
        resolved from today's scrip master, never from a value stored at calibration time.
        """
        st = self.state.get(scrip_code)
        if st is None:
            return
        st.oi, st.oi_change_pct, st.fut_scrip_code = oi, change_pct, fut_code

    # -- tick path ----------------------------------------------------------------------------------

    async def on_tick(self, tick: dict[str, Any]) -> None:
        code = str(tick.get("scrip_code"))
        st = self.state.get(code)
        if st is None:
            return
        price = float(tick.get("ltp") or 0)
        if price <= 0:
            return
        ts = float(tick.get("ts") or 0) or time.time()
        st.ticks += 1

        segment = st.instrument.segment
        day = ist_day(ts).isoformat()
        if day != st.day:
            self._roll_day(st, day, tick)

        qty = self._volume_delta(st, tick)
        await self._apply(st, segment, ts, price, qty)

    def _roll_day(self, st: SymbolState, day: str, tick: dict[str, Any]) -> None:
        prev = float(tick.get("prev_close") or 0)
        st.day = day
        st.session_pv = st.session_v = 0.0
        st.last_total_qty = 0
        st.prev_close = prev if prev > 0 else st.prev_close

    @staticmethod
    def _volume_delta(st: SymbolState, tick: dict[str, Any]) -> float:
        total = int(tick.get("total_qty") or 0)
        if total <= 0:
            return float(tick.get("last_qty") or 0)
        if total < st.last_total_qty:  # session reset or a stale frame; do not emit negative volume
            st.last_total_qty = total
            return 0.0
        delta = total - st.last_total_qty
        st.last_total_qty = total
        return float(delta)

    async def _apply(
        self, st: SymbolState, segment: Segment, ts: float, price: float, qty: float
    ) -> None:
        typical_v = price * qty
        st.session_pv += typical_v
        st.session_v += qty
        sess_vwap = (st.session_pv / st.session_v) if st.session_v > 0 else price

        for tf in self.timeframes:
            bucket = int(bucket_start(segment, ts, tf))
            cur = self.store.forming(st.instrument.symbol, tf)
            if cur is not None and bucket > cur.ts:
                await self._close(cur)
                cur = None
            elif cur is not None and bucket < cur.ts:
                self.late_ticks += 1  # a tick older than the bar we are on; count, do not rewrite
                continue
            if cur is None:
                cur = self._open_bar(st, tf, bucket, price, segment)
                self.store.set_forming(cur)
            cur.merge_tick(price, qty)
            cur.vwap = sess_vwap
            cur.prev_close = st.prev_close
            cur.oi, cur.oi_change_pct = st.oi, st.oi_change_pct
            cur.fut_scrip_code = st.fut_scrip_code

    def _open_bar(
        self, st: SymbolState, tf: str, bucket: int, price: float, segment: Segment
    ) -> UnifiedBar:
        # A bucket whose start predates our connection is missing its first trades.
        began_before_connect = bucket < st.connected_since - 1
        source = BarSource.PARTIAL if began_before_connect else BarSource.LIVE
        if source is BarSource.PARTIAL:
            self.partial_bars += 1
        return UnifiedBar(
            symbol=st.instrument.symbol,
            scrip_code=st.instrument.scrip_code,
            tf=tf,
            ts=bucket,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0.0,
            source=source,
            prev_close=st.prev_close,
        )

    async def _close(self, bar: UnifiedBar) -> None:
        self.store.close(bar)
        self.bars_closed += 1
        if self.on_bar_close is not None:
            await self.on_bar_close(bar)

    # -- clock path ------------------------------------------------------------------------------

    async def flush_stale(self, now: float | None = None) -> int:
        """Close any forming bar whose bucket has ended.

        Required because a bar is otherwise only closed by the *next* tick — an illiquid scrip that
        stops trading at 14:32 would leave its 14:30 bar forming until 15:30, and FUDKII's 14:45
        boundary would see stale data. Called on a 1-second clock.
        """
        now = now or time.time()
        closed = 0
        for st in list(self.state.values()):
            for tf in self.timeframes:
                cur = self.store.forming(st.instrument.symbol, tf)
                if cur is None:
                    continue
                if now >= cur.ts + TF_SECONDS[tf]:
                    await self._close(cur)
                    closed += 1
        return closed

    # -- backfill ----------------------------------------------------------------------------------

    def seed(
        self, instrument: Instrument, tf: str, rows: list[dict[str, Any]], *, ts_of: Callable[[str], float]
    ) -> int:
        """Load REST history into the store. ``rows`` are the broker's oldest-first OHLCV dicts."""
        self.track(instrument)
        segment = instrument.segment
        bars: list[UnifiedBar] = []
        prev_day_close: float | None = None
        last_day = ""
        for row in rows:
            ts = ts_of(row["dt"])
            bucket = int(bucket_start(segment, ts, tf))
            day = ist_day(ts).isoformat()
            if last_day and day != last_day and bars:
                prev_day_close = bars[-1].close
            last_day = day
            bars.append(
                UnifiedBar(
                    symbol=instrument.symbol,
                    scrip_code=instrument.scrip_code,
                    tf=tf,
                    ts=bucket,
                    open=row["o"],
                    high=row["h"],
                    low=row["l"],
                    close=row["c"],
                    volume=row["v"],
                    source=BarSource.REST,
                    complete=True,
                    prev_close=prev_day_close,
                )
            )
        _stamp_session_vwap(bars, segment)
        return self.store.seed(instrument.symbol, tf, bars)

    def stats(self) -> dict[str, Any]:
        return {
            "symbols": len(self.state),
            "bars_closed": self.bars_closed,
            "partial_bars": self.partial_bars,
            "late_ticks": self.late_ticks,
            "ticks": sum(s.ticks for s in self.state.values()),
            "with_oi": sum(1 for s in self.state.values() if s.oi is not None),
        }


def _stamp_session_vwap(bars: list[UnifiedBar], segment: Segment) -> None:
    """VWAP on **every** backfilled bar, not only the last.

    CAN2 stamped it on ``candles[-1]`` alone while its n-of-3 entry test read VWAP on three bars, so
    two of the three compared against a value left over from the previous cycle. Cheap to do right.
    """
    day_open = None
    pv = v = 0.0
    for b in bars:
        start = session_open_ts(segment, ist_day(b.ts))
        if day_open != start:
            day_open, pv, v = start, 0.0, 0.0
        pv += b.typical * b.volume
        v += b.volume
        b.vwap = (pv / v) if v > 0 else b.close
