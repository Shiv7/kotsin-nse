"""The card's live strip (operator, 2026-10-06: "factor in india vix at the time of trigger and its allignment
(trigger direction allignment with nifty50 constituents and countre-trend allignment as well. keep both live and
updated just like the "now" ticker and add current price of Options and equity there too"): the NIFTY 50 member
stocks with and against the trigger at its bar's close and now, India VIX at the trigger / the open / now, and the
stock's and the option's prices now. Descriptive: nothing decides on it."""

from __future__ import annotations

from datetime import datetime
from datetime import time as dtime

import pytest

import kotsin_nse.engine as engine_mod
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.instrument.select import Quote
from kotsin_nse.market.indices import NIFTY50
from kotsin_nse.market.session import IST, ist_today


def _at(hh: int, mm: int, ss: int = 0) -> int:
    return int(datetime.combine(ist_today(), dtime(hh, mm, ss), tzinfo=IST).timestamp())


def _bar(sym: str, ts: int, o: float, c: float) -> UnifiedBar:
    return UnifiedBar(symbol=sym, scrip_code=sym, tf="30m", ts=ts, open=o, high=max(o, c), low=min(o, c), close=c,
                      volume=1000, source=BarSource.REST, complete=True)


@pytest.fixture
def engine(settings):
    e = Engine(settings)
    members = sorted(NIFTY50)[:30]
    for m in members:
        e.underlyings[m] = Instrument(m, m, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=m)
    # at the 09:45 close: 18 above their 100 open, 10 below, 2 on it; the 09:45 bar then moves on
    closes = [101.0] * 18 + [99.0] * 10 + [100.0] * 2
    for m, c in zip(members, closes, strict=True):
        e.store.seed(m, "30m", [_bar(m, _at(9, 15), 100.0, c), _bar(m, _at(9, 45), c, c)])
    return e


def test_the_nifty50_members_with_and_against_a_trigger_at_its_bar_and_now(engine, monkeypatch):
    members = sorted(m for m in NIFTY50 if m in engine.underlyings)
    monkeypatch.setattr(engine_mod.time, "time", lambda: _at(10, 30))
    then = engine.n50_alignment(Direction.BULLISH, at_ts=_at(9, 15))
    assert then == {"with": 18, "against": 10, "flat": 2, "names": 30}
    assert engine.n50_alignment(Direction.BEARISH, at_ts=_at(9, 15)) == {"with": 10, "against": 18, "flat": 2, "names": 30}
    assert (_at(9, 15), "BULLISH") in engine._n50_align, "a closed bar's reading is kept"
    # now: each name's live price against its own day open — not the trigger bar
    for m, px in zip(members, [102.0] * 12 + [98.0] * 17 + [100.0], strict=True):
        engine.ltps[m] = px
    assert engine.n50_alignment(Direction.BULLISH) == {"with": 12, "against": 17, "flat": 1, "names": 30}
    # without a live price the latest 30m close stands in
    engine.ltps.clear()
    assert engine.n50_alignment(Direction.BULLISH) == {"with": 18, "against": 10, "flat": 2, "names": 30}


def test_under_25_members_is_no_reading_and_a_forming_bar_is_not_kept(engine, monkeypatch):
    for m in sorted(m for m in NIFTY50 if m in engine.underlyings)[:6]:
        del engine.underlyings[m]
    assert engine.n50_alignment(Direction.BULLISH, at_ts=_at(9, 15)) is None
    for m in sorted(NIFTY50)[:6]:
        engine.underlyings[m] = Instrument(m, m, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=m)
    monkeypatch.setattr(engine_mod.time, "time", lambda: _at(10, 0))  # the 09:45 bar is still forming
    assert engine.n50_alignment(Direction.BULLISH, at_ts=_at(9, 45)) is not None
    assert (_at(9, 45), "BULLISH") not in engine._n50_align


def test_india_vix_at_the_trigger_and_the_open_from_the_days_path(engine):
    for ts, v in ((_at(9, 14), 14.78), (_at(9, 15, 10), 14.70), (_at(9, 15, 40), 14.66), (_at(9, 44, 30), 14.20),
                  (_at(9, 45, 20), 14.21), (_at(11, 0, 5), 14.12)):
        engine._note_vix(ts, v)
    assert len(engine._vix_path) == 5, "one sample a minute: the minute's last print"
    assert engine.vix_at(_at(9, 45)) == 14.20, "the last print at or before the trigger's close"
    assert engine.vix_at(_at(9, 0)) is None, "before the engine's first print: unknown"
    assert engine.vix_open() == 14.66
    # an engine started after the open does not know it
    engine._vix_path[:] = [(_at(10, 5), 14.15)]
    assert engine.vix_open() is None
    engine._roll_day_state(ist_today())
    assert engine._vix_path == [] and engine._n50_align == {}


def test_the_card_strip(engine, monkeypatch):
    monkeypatch.setattr(engine_mod.time, "time", lambda: _at(11, 24))
    und = Instrument("1922", "KOTAKBANK", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="KOTAKBANK")
    engine.ltps["1922"] = 430.55
    engine.quotes["1922"] = Quote(430.55, 430.5, 430.6, ts=_at(11, 23, 58))
    engine.quotes["88643"] = Quote(5.43, 5.40, 5.45, ts=_at(11, 23, 59))
    engine._note_vix(_at(9, 44, 50), 14.19)
    engine.ltps[engine_mod.INDIA_VIX_SCRIP] = 14.15
    sgn = {"direction": "BULLISH", "ts": _at(9, 15)}
    ps = {"instrument": {"scrip_code": "88643", "name": "KOTAKBANK 27 OCT 2026 CE 440.00"}}
    out = engine._card_market(sgn, ps, None, und, {"vix": 14.21}, {})
    assert out["n50"]["atTrigger"] == {"with": 18, "against": 10, "flat": 2, "names": 30}
    assert out["vix"] == {"atTrigger": 14.21, "open": None, "now": 14.15, "band": "NEUTRAL"}, "the trigger's own record first"
    assert out["stock"] == {"ltp": 430.55, "ageS": 2}
    assert out["option"] == {"contract": "KOTAKBANK 27 OCT 2026 CE 440.00", "held": True, "ltp": 5.43, "bid": 5.40, "ask": 5.45, "ageS": 1}
    # an older trigger without the record reads the day's path; a card with no contract shows no option
    out = engine._card_market(sgn, None, None, und, None, {})
    assert out["vix"]["atTrigger"] == 14.19 and out["option"] is None
    # the book's would-be contract when nothing is held
    out = engine._card_market(sgn, None, {"scripCode": "88643", "contract": "KOTAKBANK 440 CE"}, und, None, {})
    assert out["option"]["held"] is False and out["option"]["contract"] == "KOTAKBANK 440 CE"
    mcx = Instrument("G1", "GOLD", Segment.MCX_FO, InstrumentKind.FUTURE, underlying="GOLD")
    assert engine._card_market(sgn, None, None, mcx, None, {}) is None, "NSE only"
