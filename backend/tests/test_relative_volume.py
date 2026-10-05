"""Relative volume (operator, 2026-10-05: "relative vol surge ... from all nifty50 companies ... above 1 ok, 3x high,
5x very high, 7x strong"; "even out the morning surge vs afternoon slug"). A trigger's volume is read against its own
usual for that half hour AND against the NIFTY50 members' same reading at the same bar — labels on every trigger and
card; nothing gates on them."""

from __future__ import annotations

from datetime import datetime

import pytest

from kotsin_nse.bars.volume_read import VolBar, VolumeReading, rel_volume_tier, slot_reading
from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.market.indices import NIFTY50
from kotsin_nse.market.session import IST


def _ts(day: int, hh: int, mm: int) -> int:
    return int(datetime(2026, 9 if day > 5 else 10, day, hh, mm, tzinfo=IST).timestamp())


SEPT = [22, 23, 24, 25, 26, 29, 30]  # earlier sessions (the calendar does not matter here: only the clock time)


def _bars(usual_1015: float, today_1015: float, *, sessions: list[int] = SEPT) -> list[VolBar]:
    out = []
    for d in sessions:
        out += [VolBar(_ts(d, 9, 15), 1_000_000.0), VolBar(_ts(d, 9, 45), 300_000.0), VolBar(_ts(d, 10, 15), usual_1015)]
    out += [VolBar(_ts(5, 9, 15), 1_040_545.0), VolBar(_ts(5, 9, 45), 284_454.0), VolBar(_ts(5, 10, 15), today_1015)]
    return out


def test_a_bar_is_read_against_the_same_half_hour_on_earlier_sessions():
    # HCLTECH 5 Oct: 357,327 at 10:15 against a usual ~119k — 3.0x, where the six-bar baseline read 0.55x "dried"
    assert slot_reading(_bars(118_981.0, 357_327.0), _ts(5, 10, 15)).ratio == pytest.approx(3.003, abs=0.001)
    assert not slot_reading(_bars(118_981.0, 357_327.0, sessions=SEPT[:4]), _ts(5, 10, 15)).ok, "under 5 sessions: no reading"
    assert not slot_reading(_bars(118_981.0, 0.0), _ts(5, 10, 15)).ok, "an empty bar is no reading"
    assert not slot_reading(_bars(118_981.0, 357_327.0), _ts(5, 10, 45)).ok, "a missing bar is no reading"


def test_the_operators_tiers():
    assert [rel_volume_tier(x) for x in (0.7, 1.0, 2.9, 3.0, 5.6, 7.0, None)] == [
        "below market", "normal", "normal", "high", "very high", "strong", None]


@pytest.fixture
def engine(settings, monkeypatch):
    e = Engine(settings)
    for m in sorted(NIFTY50)[:30]:
        e.underlyings[m] = Instrument(m, m, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=m)
    return e


def test_the_nifty50_reading_is_the_members_median_and_waits_for_25_of_them(engine, monkeypatch):
    members = sorted(m for m in NIFTY50 if m in engine.underlyings)
    surge = {m: 1.0 + i * 0.1 for i, m in enumerate(members)}  # 1.0 … 3.9: one heavy opener cannot move a median
    surge[members[-1]] = 40.0
    monkeypatch.setattr(engine, "_volume_reading", lambda m, t, bars=None: VolumeReading(surge.get(m), 1.0, 1000.0))
    monkeypatch.setattr(engine, "_slot_ratio", lambda m, t: 1.2 if m in surge else None)
    t = _ts(5, 13, 15)
    n50_t, n50_slot = engine.n50_relative_volume(t)
    assert n50_t == pytest.approx(2.45) and n50_slot == pytest.approx(1.2)
    assert t in engine._n50_rel, "read once per bar"
    # too early in the bar's close: fewer than 25 members have it — not judged, and asked again later
    engine._n50_rel.clear()
    monkeypatch.setattr(engine, "_slot_ratio", lambda m, t: 1.2 if m in members[:20] else None)
    assert engine.n50_relative_volume(t)[1] is None and t not in engine._n50_rel


def test_a_trigger_reads_twice_relative_and_gets_its_tier(engine, monkeypatch):
    # ADANIGREEN 5 Oct 13:15: surge 5.40x vs the members' 0.96x — but 1.26x its usual half hour vs their 1.22x
    monkeypatch.setattr(engine, "n50_relative_volume", lambda t: (0.964, 1.22))
    monkeypatch.setattr(engine, "_slot_ratio", lambda m, t: 1.26)
    out = engine.relative_volume("ADANIGREEN", _ts(5, 13, 15), 5.40)
    assert out["relVolMkt"] == pytest.approx(5.602, abs=0.001)
    assert out["relVol"] == pytest.approx(1.033, abs=0.001) and out["relVolTier"] == "normal"
    assert out["slotVol"] == 1.26 and out["n50SlotVol"] == 1.22
    # no NIFTY50 reading yet: the stock's own time-of-day ratio still shows, without a tier
    monkeypatch.setattr(engine, "n50_relative_volume", lambda t: (None, None))
    assert engine.relative_volume("ADANIGREEN", _ts(5, 13, 15), 5.40) == {"slotVol": 1.26}


def test_every_stock_is_read_at_each_closed_bar_highest_first_and_kept_once_the_nifty50_stands(engine, monkeypatch):
    from kotsin_nse.bars.unified import BarSource, UnifiedBar

    t = _ts(5, 13, 15)
    for sym in ("ADANIGREEN", "UNITDSPR"):
        engine.underlyings[sym] = Instrument(sym, sym, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=sym)
        engine.store.seed(sym, "30m", [UnifiedBar(symbol=sym, scrip_code=sym, tf="30m", ts=t, open=1, high=1, low=1, close=1,
                                                   volume=1000, source=BarSource.REST, complete=True)])
    monkeypatch.setattr(engine, "_volume_reading", lambda m, ts, bars=None: VolumeReading(2.0, 1.0, 1000.0))
    rel = {"ADANIGREEN": 3.13, "UNITDSPR": 3.21}
    monkeypatch.setattr(engine, "relative_volume", lambda m, ts, s: {"relVol": rel.get(m), "relVolTier": rel_volume_tier(rel.get(m))})
    monkeypatch.setattr(engine, "n50_relative_volume", lambda ts: (1.43, 1.5))
    out = engine.volume_table()
    assert out["ts"] == t and [r["symbol"] for r in out["rows"]] == ["UNITDSPR", "ADANIGREEN"]
    assert out["rows"][0]["relVolTier"] == "high" and t in engine._vol_table, "kept for the bar"
    engine._vol_table.clear()
    monkeypatch.setattr(engine, "n50_relative_volume", lambda ts: (None, None))
    engine.volume_table(t)
    assert t not in engine._vol_table, "the NIFTY50 has not read yet: asked again on the next call"
