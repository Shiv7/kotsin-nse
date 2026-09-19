"""Session rules. Every one of these encodes a bug the old stack shipped."""

from __future__ import annotations

from datetime import date

from kotsin_nse.config import Segment
from kotsin_nse.market.session import (
    TradingCalendar,
    boundaries,
    bucket_start,
    in_entry_window,
    is_open,
    ist_hm,
    ist_naive_to_ts,
    past_force_flat,
    session_open_ts,
)

from .conftest import ist_ts


def test_first_bucket_is_0915_not_0916():
    """``tick_candles_1m`` started at 09:16 and lost the minute that often holds the day's extreme."""
    t = ist_ts("2026-09-18", "09:15")
    assert bucket_start(Segment.NSE_EQ, t, "1m") == t
    # 30 seconds into the opening minute still belongs to 09:15, not to 09:16.
    assert ist_hm(bucket_start(Segment.NSE_EQ, t + 30, "1m")) == "09:15"
    # A tick before the open is clamped forward rather than creating a pre-market bucket.
    assert bucket_start(Segment.NSE_EQ, t - 600, "5m") == t


def test_30m_grid_is_anchored_on_the_open_not_the_hour():
    """FUDKII's boundaries are :15 and :45, because NSE opens at 09:15. An hour-anchored grid
    would put one at 09:30 and silently shift every SuperTrend bar."""
    labels = boundaries(Segment.NSE_EQ, "30m")
    assert labels[:4] == ("09:15", "09:45", "10:15", "10:45")
    assert labels[-1] == "15:15"


def test_mcx_grid_starts_at_0900():
    labels = boundaries(Segment.MCX_FO, "30m")
    assert labels[0] == "09:00"
    assert labels[1] == "09:30"
    assert "23:00" in labels


def test_bucket_start_floors_within_the_bucket():
    assert bucket_start(Segment.NSE_EQ, ist_ts("2026-09-18", "11:07"), "30m") == ist_ts(
        "2026-09-18", "10:45"
    )
    assert bucket_start(Segment.NSE_EQ, ist_ts("2026-09-18", "11:00"), "5m") == ist_ts(
        "2026-09-18", "11:00"
    )


def test_force_flat_differs_by_segment():
    """A single NSE-shaped 15:20 constant would flatten every MCX position eight hours early."""
    t = ist_ts("2026-09-18", "15:25")
    assert past_force_flat(Segment.NSE_EQ, t) is True
    assert past_force_flat(Segment.MCX_FO, t) is False
    assert past_force_flat(Segment.MCX_FO, ist_ts("2026-09-18", "23:25")) is True


def test_entry_window_closes_before_the_session_does():
    cal = TradingCalendar()
    assert in_entry_window(Segment.NSE_EQ, ist_ts("2026-09-18", "14:30"), cal) is True
    assert in_entry_window(Segment.NSE_EQ, ist_ts("2026-09-18", "15:00"), cal) is False
    assert is_open(Segment.NSE_EQ, ist_ts("2026-09-18", "15:00"), cal) is True


def test_weekend_and_holiday_are_not_trading_days():
    cal = TradingCalendar(frozenset({date(2026, 9, 17)}))  # a Thursday, declared a holiday
    assert cal.is_trading_day(date(2026, 9, 18)) is True  # Friday
    assert cal.is_trading_day(date(2026, 9, 19)) is False  # Saturday
    assert cal.is_trading_day(date(2026, 9, 21)) is True  # Monday
    assert cal.is_trading_day(date(2026, 9, 17)) is False  # the declared holiday
    assert cal.previous_trading_day(date(2026, 9, 21)) == date(2026, 9, 18)


def test_missing_holiday_list_is_announced_not_assumed():
    assert TradingCalendar().missing_holidays_warning() is not None
    assert TradingCalendar(frozenset({date(2026, 1, 26)})).missing_holidays_warning() is None


def test_broker_timestamps_are_naive_ist():
    assert ist_naive_to_ts("2026-09-18T11:05:00") == ist_ts("2026-09-18", "11:05")
    assert ist_naive_to_ts("2026-09-18 11:05:00") == ist_ts("2026-09-18", "11:05")


def test_session_open_matches_spec():
    assert ist_hm(session_open_ts(Segment.NSE_EQ, date(2026, 9, 18))) == "09:15"
    assert ist_hm(session_open_ts(Segment.MCX_FO, date(2026, 9, 18))) == "09:00"
