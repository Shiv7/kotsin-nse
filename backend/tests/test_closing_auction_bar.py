"""Operator, 2026-10-02: an NSE stock's 15:15 bar is its closing auction — one print at the official
close. The broker's 30m history drops the bucket for 4-18 of the NIFTY50 on a back-filled day, and the
rows it does serve are a stray trade after the auction (1,599 of 1,773 past ones off the official close,
median 0.25 %, up to 3.4 %). FUDKII's SuperTrend / Bollinger read that bar; the 25 Sep - 1 Oct replay
changed 44 trades on it. Every session's 15:15 bar is set to the daily candle's official close."""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import from_ist, ist_day

STOCK = Instrument("21808", "SBILIFE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="SBILIFE", tick_size=0.1)
INDEX = Instrument("999920000", "NIFTY", Segment.NSE_EQ, InstrumentKind.INDEX, underlying="NIFTY")
D_NOCLOSE, D_SHORT, D_MISSING, D_STUB, D_GOOD, TODAY = (date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25),
                                                         date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))
CLOSE = {D_SHORT: 1781.0, D_MISSING: 1790.5, D_STUB: 1802.0, D_GOOD: 1811.3, TODAY: 1820.4}


def _t(d: date, hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return int(from_ist(datetime.combine(d, time(h, m))))


def _day(inst: Instrument, d: date, *, last: str = "14:45", close: float = 1790.0) -> list[UnifiedBar]:
    slots = [f"{9 + (15 + 30 * k) // 60:02d}:{(15 + 30 * k) % 60:02d}" for k in range(12)]  # 09:15 ... 14:45
    return [UnifiedBar(symbol=inst.symbol, scrip_code=inst.scrip_code, tf="30m", ts=_t(d, hm), open=close, high=close + 4,
                       low=close - 4, close=close, volume=1e5, source=BarSource.REST, complete=True, prev_close=close - 10)
            for hm in slots if hm <= last]


def _bar1515(inst: Instrument, d: date, price: float, vol: float) -> UnifiedBar:
    return UnifiedBar(symbol=inst.symbol, scrip_code=inst.scrip_code, tf="30m", ts=_t(d, "15:15"), open=price, high=price,
                      low=price, close=price, volume=vol, source=BarSource.REST, complete=True)


def _daily(inst: Instrument, d: date, close: float, *, hm: str = "00:00") -> UnifiedBar:
    return UnifiedBar(symbol=inst.symbol, scrip_code=inst.scrip_code, tf="1d", ts=_t(d, hm), open=close, high=close + 9,
                      low=close - 9, close=close, volume=2e6, source=BarSource.REST, complete=True)


def _engine(settings, *, today_daily: str | None = None) -> Engine:
    e = Engine(settings)
    for inst in (STOCK, INDEX):
        bars = [*_day(inst, D_NOCLOSE), *_day(inst, D_SHORT, last="12:45"), *_day(inst, D_MISSING), *_day(inst, D_STUB),
                _bar1515(inst, D_STUB, 1795.8, 161.0),            # the broker's stray trade, off the close
                *_day(inst, D_GOOD), _bar1515(inst, D_GOOD, CLOSE[D_GOOD], 512_000.0),  # a live-built auction print
                *_day(inst, TODAY), _bar1515(inst, TODAY, 1818.0, 90.0)]
        e.store.seed(inst.symbol, "30m", bars)
        dailies = [_daily(inst, d, c) for d, c in CLOSE.items() if d != TODAY]
        if today_daily:
            dailies.append(_daily(inst, TODAY, CLOSE[TODAY], hm=today_daily))
        e.store.seed(inst.symbol, "1d", dailies)
    path = settings.data_dir / "archive" / "bars"
    path.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"symbol": "SBILIFE", "ts": _t(D_MISSING, "15:14"), "v": 7_000.0},
                  {"symbol": "SBILIFE", "ts": _t(D_MISSING, "15:28"), "v": 494_000.0},
                  {"symbol": "SBILIFE", "ts": _t(D_MISSING, "15:28"), "v": 494_000.0}]).to_parquet(path / f"{D_MISSING}.parquet")
    return e


def _held(e: Engine, d: date) -> UnifiedBar | None:
    return {b.ts: b for b in e.store.bars("SBILIFE", "30m")}.get(_t(d, "15:15"))


def _during(d: date) -> float:
    return float(_t(d, "13:00"))


def test_every_past_session_gets_the_auction_print_at_the_official_close(settings):
    e = _engine(settings)
    assert e._set_closing_auction_bars([STOCK, INDEX], now=_during(TODAY)) == 3
    gap, stub, good = _held(e, D_MISSING), _held(e, D_STUB), _held(e, D_GOOD)
    assert (gap.open, gap.high, gap.low, gap.close, gap.volume) == (1790.5, 1790.5, 1790.5, 1790.5, 494_000.0), "a gap filled"
    assert (stub.close, stub.high, stub.low) == (1802.0, 1802.0, 1802.0) and stub.volume == 0.0, "a stray trade replaced"
    assert stub.extra["replaced"]["c"] == 1795.8, "and kept for the record"
    assert good.close == CLOSE[D_GOOD] and good.volume == 512_000.0, "an auction print keeps its own volume"
    ts = [b.ts for b in e.store.bars("SBILIFE", "30m")]
    assert ts == sorted(ts) and len([t for t in ts if ist_day(t) == D_MISSING]) == 13, "the session is whole again"
    assert e._set_closing_auction_bars([STOCK, INDEX], now=_during(TODAY)) == 0, "idempotent"


def test_what_it_leaves_alone(settings):
    e = _engine(settings)
    e._set_closing_auction_bars([STOCK, INDEX], now=_during(TODAY))
    assert _held(e, D_NOCLOSE) is None, "no official daily close: nothing invented"
    assert _held(e, D_SHORT) is None, "a session without its 14:45 bar is not a normal session"
    assert _held(e, TODAY).close == 1818.0, "today, before the close, is the live build's"
    nifty = {b.ts: b for b in e.store.bars("NIFTY", "30m")}
    assert nifty[_t(D_STUB, "15:15")].close == 1795.8 and _t(D_MISSING, "15:15") not in nifty, "an index's last bar is a real bar"


@pytest.mark.parametrize(("stamp", "set_"), [("00:00", True), ("09:15", False)])
def test_today_is_set_after_the_close_from_the_end_of_day_row_only(settings, stamp, set_):
    e = _engine(settings, today_daily=stamp)
    after = float(_t(TODAY, "15:50"))
    e._set_closing_auction_bars([STOCK], now=after)
    assert (_held(e, TODAY).close == CLOSE[TODAY]) is set_, "the provisional 09:15 daily row is never an official close"


@pytest.mark.asyncio
async def test_the_bar_close_check_keeps_the_live_auction_print(settings, monkeypatch):
    """At 15:30 the reconciler would install the broker's row for the 15:15 bucket — a stray trade after
    the auction — over the live build's auction print, before the carry decision reads it."""
    e = Engine(settings)
    e.underlyings["SBILIFE"] = STOCK
    e.reconciler_ready = True
    monkeypatch.setattr(type(e.s), "has_credentials", property(lambda self: True))
    asked: list[int] = []

    async def spy(bar, *, timeout_s):
        asked.append(bar.ts)
        from kotsin_nse.bars.verify import BarCheck

        return BarCheck(bar.symbol, bar.tf, bar.ts, found=False)

    async def nothing(*_a, **_k):
        return None

    e.reconciler.reconcile_bar = spy  # type: ignore[method-assign]
    e._decide = nothing  # type: ignore[method-assign]
    auction = _bar1515(STOCK, TODAY, 1820.4, 600_000.0)
    await e._reconcile_then_decide(auction)
    assert asked == [], "the auction print is not replaced by the broker's row"
    await e._reconcile_then_decide(_day(STOCK, TODAY)[-1])
    assert asked == [_t(TODAY, "14:45")], "a continuous bar is still reconciled"
