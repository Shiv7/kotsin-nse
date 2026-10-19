"""MCX's own calendar (2026-10-19). ``data/holidays.txt`` is NSE's list and was applied to MCX too: on an NSE
holiday with an MCX evening session (20 Oct, 10 Nov, 24 Nov 2026) the engine called MCX closed all day, so the
feed watchdog, the REST sweep, the held-quote refresh and every health feed line were off while MCX traded
17:00-23:30. ``data/holidays_mcx.txt`` now holds MCX's days (circular MCX/TRD/636/2025); every NSE answer is
unchanged, and with no file MCX reads the NSE list exactly as before."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from kotsin_nse.bars.daily import audit
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.market.session import (
    IST,
    MCX_HOLIDAYS_FILE,
    McxDays,
    TradingCalendar,
    in_entry_window,
    is_open,
    load_calendar,
    session_buckets_back,
)

DATA = Path(__file__).resolve().parents[1] / "data"
NSE_SEGMENTS = (Segment.NSE_EQ, Segment.NSE_FO, Segment.NSE_IDX)


def _at(d: date, hh: int, mm: int = 0) -> float:
    return datetime.combine(d, time(hh, mm), tzinfo=IST).timestamp()


def _cal(mcx: bool = True) -> TradingCalendar:
    return TradingCalendar.from_file(DATA / "holidays.txt", DATA / MCX_HOLIDAYS_FILE if mcx else None)


def test_the_shipped_list_parses_and_every_day_is_a_weekday():
    days = McxDays.from_file(DATA / MCX_HOLIDAYS_FILE)
    assert days is not None
    every = days.closed | days.evening | days.morning
    assert len(every) == 17 and all(d.weekday() < 5 for d in every)
    assert date(2026, 10, 20) in days.evening and date(2026, 11, 10) in days.evening and date(2026, 11, 24) in days.evening
    assert date(2026, 12, 25) in days.closed and date(2026, 10, 2) in days.closed and date(2026, 1, 1) in days.morning
    nse = _cal().holidays
    assert {date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24)} <= nse, "the evening days are NSE holidays"


def test_every_nse_answer_is_unchanged_by_the_mcx_list():
    with_mcx, without = _cal(True), _cal(False)
    d = date(2026, 1, 1)
    while d <= date(2027, 1, 10):
        for seg in (None, *NSE_SEGMENTS):
            assert with_mcx.is_trading_day(d, seg) == without.is_trading_day(d)
            assert with_mcx.previous_trading_day(d, seg) == without.previous_trading_day(d)
        for seg in NSE_SEGMENTS:
            for hh, mm in ((9, 0), (9, 15), (12, 0), (14, 45), (15, 29), (15, 30), (15, 31), (18, 0)):
                ts = _at(d, hh, mm)
                assert is_open(seg, ts, with_mcx) == is_open(seg, ts, without)
                assert in_entry_window(seg, ts, with_mcx) == in_entry_window(seg, ts, without)
        d += timedelta(days=1)


def test_without_the_file_mcx_reads_the_nse_list_as_before():
    cal = _cal(False)
    assert cal.mcx is None
    tue = date(2026, 10, 20)
    assert not cal.is_trading_day(tue, Segment.MCX_FO) and not is_open(Segment.MCX_FO, _at(tue, 18), cal)
    assert cal.previous_trading_day(date(2026, 10, 21), Segment.MCX_FO) == date(2026, 10, 19)


def test_dussehra_nse_shut_mcx_trades_the_evening():
    cal, tue = _cal(), date(2026, 10, 20)
    for seg in NSE_SEGMENTS:
        assert not is_open(seg, _at(tue, 10), cal) and not is_open(seg, _at(tue, 18), cal)
    assert cal.is_trading_day(tue, Segment.MCX_FO)
    assert not is_open(Segment.MCX_FO, _at(tue, 9, 30), cal), "the morning session is closed"
    assert not is_open(Segment.MCX_FO, _at(tue, 16, 59), cal)
    assert is_open(Segment.MCX_FO, _at(tue, 17, 0), cal) and is_open(Segment.MCX_FO, _at(tue, 23, 0), cal)
    assert not is_open(Segment.MCX_FO, _at(tue, 23, 45), cal)
    assert in_entry_window(Segment.MCX_FO, _at(tue, 17, 30), cal) and not in_entry_window(Segment.MCX_FO, _at(tue, 23, 10), cal)
    assert cal.session_hours(Segment.MCX_FO, tue) == (time(17, 0), time(23, 30))
    wed = date(2026, 10, 21)
    assert cal.previous_trading_day(wed, Segment.MCX_FO) == tue, "MCX's previous session is the evening"
    assert cal.previous_trading_day(wed) == date(2026, 10, 19), "NSE's is Monday"


def test_a_normal_day_and_the_full_and_morning_closures():
    cal = _cal()
    mon = date(2026, 10, 19)
    assert cal.session_hours(Segment.MCX_FO, mon) == (time(9, 0), time(23, 30))
    assert is_open(Segment.MCX_FO, _at(mon, 9, 0), cal) and is_open(Segment.NSE_EQ, _at(mon, 9, 15), cal)
    xmas = date(2026, 12, 25)
    assert cal.session_hours(Segment.MCX_FO, xmas) is None and not is_open(Segment.MCX_FO, _at(xmas, 18), cal)
    ny = date(2026, 1, 1)  # NSE open; MCX morning only
    assert is_open(Segment.MCX_FO, _at(ny, 10), cal) and not is_open(Segment.MCX_FO, _at(ny, 18), cal)
    assert is_open(Segment.NSE_EQ, _at(ny, 10), cal)
    assert cal.previous_trading_day(date(2026, 10, 5), Segment.MCX_FO) == date(2026, 10, 1), "2 Oct: both closed"
    assert not cal.is_trading_day(date(2026, 10, 24), Segment.MCX_FO), "weekends never trade"


def test_the_mcx_bucket_walk_crosses_into_the_evening_day():
    cal = _cal()
    slots = session_buckets_back(Segment.MCX_FO, _at(date(2026, 10, 21), 9, 30), 3, "30m", cal)
    assert [datetime.fromtimestamp(t, IST).strftime("%m-%d %H:%M") for t in slots] == ["10-20 23:00", "10-21 09:00", "10-21 09:30"]
    nse = session_buckets_back(Segment.NSE_EQ, _at(date(2026, 10, 21), 9, 45), 3, "30m", cal)
    assert datetime.fromtimestamp(nse[0], IST).strftime("%m-%d %H:%M") == "10-19 15:15"


def test_load_calendar_says_when_mcx_is_left_on_the_nse_list(tmp_path):
    (tmp_path / "holidays.txt").write_text((DATA / "holidays.txt").read_text())
    cal, warn = load_calendar(tmp_path)
    assert cal.mcx is None and warn and "MCX follows the NSE holiday list" in warn
    (tmp_path / MCX_HOLIDAYS_FILE).write_text("2026-10-20 evening\n2026-10-21 half\n")
    cal, warn = load_calendar(tmp_path)
    assert cal.mcx is None and warn and "unreadable" in warn and cal.holidays, "NSE's list still loads"
    (tmp_path / MCX_HOLIDAYS_FILE).write_text("2026-10-20 evening\n2026-10-20 closed\n")
    assert "unreadable" in (load_calendar(tmp_path)[1] or "")
    (tmp_path / MCX_HOLIDAYS_FILE).write_text((DATA / MCX_HOLIDAYS_FILE).read_text())
    cal, warn = load_calendar(tmp_path)
    assert warn is None and cal.mcx is not None and date(2026, 10, 20) in cal.mcx.evening


def _daily(sym: str, d: date) -> UnifiedBar:
    ts = int(datetime.combine(d, time(0, 0), tzinfo=IST).timestamp())
    return UnifiedBar(sym, sym, "1d", ts, 100.0, 101.0, 99.0, 100.0, 1.0, source=BarSource.REST, complete=True)


def test_the_daily_audit_expects_each_exchange_s_own_previous_session():
    cal, wed = _cal(), date(2026, 10, 21)
    days = [wed - timedelta(days=k) for k in range(60, 1, -1) if (wed - timedelta(days=k)).weekday() < 5]
    mcx_days = [*days, date(2026, 10, 20)]
    nse_days = [d for d in days if d != date(2026, 10, 20)]
    series = {"RELIANCE": [_daily("RELIANCE", d) for d in nse_days],
              "CRUDEOIL": [_daily("CRUDEOIL", d) for d in mcx_days],
              "GOLDM": [_daily("GOLDM", d) for d in nse_days]}  # never got the 20 Oct evening candle
    segs = {"RELIANCE": Segment.NSE_EQ, "CRUDEOIL": Segment.MCX_FO, "GOLDM": Segment.MCX_FO}
    a = audit(series, wed, cal, segments=segs)
    assert a.expected_prev == date(2026, 10, 19) and a.expected("CRUDEOIL") == date(2026, 10, 20)
    assert sorted(a.ok) == ["CRUDEOIL", "RELIANCE"] and a.stale == ["GOLDM"], "GOLDM is re-asked for its evening candle"
    old = audit(series, wed, _cal(False), segments=segs)
    assert sorted(old.ok) == ["CRUDEOIL", "GOLDM", "RELIANCE"], "the NSE list called 19 Oct current for MCX"


@pytest.mark.asyncio
async def test_the_engine_sees_the_mcx_evening_as_open(settings, monkeypatch):
    from kotsin_nse.engine import Engine

    (settings.data_dir / "holidays.txt").write_text((DATA / "holidays.txt").read_text())
    (settings.data_dir / MCX_HOLIDAYS_FILE).write_text((DATA / MCX_HOLIDAYS_FILE).read_text())
    e = Engine(settings)
    assert e.calendar.mcx is not None and e._calendar_warning is None
    tue_evening = _at(date(2026, 10, 20), 18, 0)
    monkeypatch.setattr("kotsin_nse.engine.time.time", lambda: tue_evening)
    e.groups = {"RELIANCE": SimpleNamespace(segment=Segment.NSE_FO), "CRUDEOIL": SimpleNamespace(segment=Segment.MCX_FO)}  # type: ignore[dict-item]
    assert e.market_open_now(), "MCX trades: the health feed lines and the watchdog are on"
    e.groups = {"RELIANCE": SimpleNamespace(segment=Segment.NSE_FO)}  # type: ignore[dict-item]
    assert not e.market_open_now(), "NSE alone: closed"
