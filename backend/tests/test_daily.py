from datetime import date, datetime, timedelta

from kotsin_nse.bars.daily import MIN_DAILY_BARS, DailyCache, audit, is_official, previous_session
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.market.session import TradingCalendar

HOLIDAY = date(2026, 9, 14)
CAL = TradingCalendar(frozenset({HOLIDAY}))
TODAY = date(2026, 9, 23)


def _bar(day: date, close: float = 100.0, *, source=BarSource.REST, complete=True) -> UnifiedBar:
    ts = datetime(day.year, day.month, day.day, 9, 15).timestamp()
    return UnifiedBar(symbol="X", scrip_code="1", tf="1d", ts=ts, open=close, high=close + 1,
                      low=close - 1, close=close, volume=1000, source=source, complete=complete)


def _series(last: date, n: int = MIN_DAILY_BARS + 5, **kw) -> list[UnifiedBar]:
    out, d = [], last
    while len(out) < n:
        if CAL.is_trading_day(d):
            out.append(_bar(d, **kw))
        d -= timedelta(days=1)
    return list(reversed(out))


def test_previous_session_never_uses_todays_own_bar():
    bars = [*_series(date(2026, 9, 22)), _bar(TODAY, source=BarSource.LIVE, complete=False)]
    prev = previous_session(bars, TODAY)
    assert prev is not None and datetime.fromtimestamp(prev.ts).date() == date(2026, 9, 22)


def test_only_the_brokers_completed_candle_is_official():
    assert is_official(_bar(TODAY))
    assert not is_official(_bar(TODAY, source=BarSource.LIVE))
    assert not is_official(_bar(TODAY, source=BarSource.PARTIAL))
    assert not is_official(_bar(TODAY, complete=False))


def test_audit_classifies_every_failure_mode_separately():
    series = {
        "OK": _series(date(2026, 9, 22)),
        "MISSING": [_bar(TODAY, source=BarSource.LIVE, complete=False)],
        "UNOFFICIAL": [*_series(date(2026, 9, 21)), _bar(date(2026, 9, 22), source=BarSource.LIVE)],
        "STALE": _series(date(2026, 9, 18)),
        "SHORT": _series(date(2026, 9, 22), n=5),
    }
    a = audit(series, TODAY, CAL)
    assert a.expected_prev == date(2026, 9, 22)
    assert a.ok == ["OK"] and a.missing == ["MISSING"] and a.unofficial == ["UNOFFICIAL"]
    assert a.stale == ["STALE"] and a.short == ["SHORT"]
    assert a.needs_refresh == ["MISSING", "SHORT", "STALE", "UNOFFICIAL"]
    assert not a.ready and not a.holiday_suspected


def test_the_calendar_walks_over_the_holiday():
    """On Tue 15-Sep the previous session is Fri 11-Sep: Mon 14-Sep is in the holiday file."""
    a = audit({"OK": _series(date(2026, 9, 11))}, date(2026, 9, 15), CAL)
    assert a.expected_prev == date(2026, 9, 11) and a.ok == ["OK"] and a.ready


def test_a_holiday_missing_from_the_file_is_suspected_not_treated_as_two_hundred_failures():
    """The file does not know 14-Sep; every name's latest session is the 11th. That is one
    unknown holiday, not a data outage — and it must not trigger a refetch storm."""
    no_holiday = TradingCalendar(frozenset())
    series = {f"S{i}": _series(date(2026, 9, 11)) for i in range(5)}
    a = audit(series, date(2026, 9, 15), no_holiday)
    assert a.expected_prev == date(2026, 9, 14) and a.consensus_prev == date(2026, 9, 11)
    assert a.holiday_suspected and a.stale == sorted(series) and a.ready
    assert "holiday" in a.summary()


def test_one_stale_name_among_fresh_ones_is_a_real_failure():
    series = {"FRESH": _series(date(2026, 9, 22)), "OLD": _series(date(2026, 9, 18))}
    a = audit(series, TODAY, CAL)
    assert not a.holiday_suspected and a.needs_refresh == ["OLD"]


def test_cache_round_trips_official_bars_only_and_survives_corruption(tmp_path):
    cache = DailyCache(tmp_path / "daily")
    bars = [*_series(date(2026, 9, 22), n=3), _bar(TODAY, source=BarSource.LIVE, complete=False)]
    assert cache.save("reliance", bars) == 3, "the tick-built bar is not cached"
    back = cache.load("RELIANCE", "2885")
    assert [b.close for b in back] == [b.close for b in bars[:3]]
    assert all(is_official(b) for b in back) and back[0].scrip_code == "2885"

    cache.path("RELIANCE").write_text("{not json")
    assert cache.load("RELIANCE", "2885") == []
    assert cache.load("NEVER", "0") == []
    assert cache.save("EMPTY", [_bar(TODAY, source=BarSource.LIVE)]) == 0


def test_a_contract_with_no_candles_at_all_is_dormant_not_missing():
    """COTTON, KAPAS, MCXBULLDEX…: listed, never traded, nothing at the broker. There is no
    session to be missing from, so it is reported and left alone rather than kept red all day."""
    a = audit({"OK": _series(date(2026, 9, 22)), "COTTON": []}, TODAY, CAL)
    assert a.dormant == ["COTTON"] and a.missing == [] and a.ok == ["OK"]
    assert a.ready and a.needs_refresh == [] and "1 dormant" in a.summary()
