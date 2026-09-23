from datetime import date, datetime, timedelta

from kotsin_nse.bars.periods import monthly, previous_complete, weekly
from kotsin_nse.bars.unified import UnifiedBar


def _daily(day: date, close: float) -> UnifiedBar:
    ts = datetime(day.year, day.month, day.day, 9, 15).timestamp()
    return UnifiedBar(symbol="X", scrip_code="1", tf="1d", ts=ts,
                      open=close, high=close + 1, low=close - 1, close=close, volume=1000)


def test_periods_are_ordered_by_the_calendar_not_the_keys_repr():
    """``sorted(groups, key=str)`` ordered ISO week 9 after week 38 — "(2026, 9)" > "(2026, 38)"
    as strings — so previous_complete returned the last week of February as "last week" for seven
    months. Every weekly pivot in the book was built on it."""
    bars = [_daily(date(2026, 2, 23) + timedelta(days=i), 100 + i) for i in range(5)]      # week 9
    bars += [_daily(date(2026, 9, 15) + timedelta(days=i), 200 + i) for i in range(4)]      # week 38
    weeks = weekly(bars)
    assert [p.start for p in weeks] == [date(2026, 2, 23), date(2026, 9, 15)], "chronological"
    prev = previous_complete(weeks, date(2026, 9, 23))
    assert prev is not None and prev.start == date(2026, 9, 15), "last week, not February"


def test_monthly_survives_the_two_digit_month_that_would_have_broken_it_in_november():
    """(2026, 9) sorted after (2026, 10) as a string, so from November the "previous complete
    month" would have been September for the rest of the year."""
    bars: list[UnifiedBar] = []
    for m in (9, 10):
        bars += [_daily(date(2026, m, d), 100.0 + m) for d in (1, 15, 28)]
    bars += [_daily(date(2026, 11, d), 130.0) for d in (2, 16)]
    months = monthly(bars)
    assert [p.label for p in months] == ["(2026, 9)", "(2026, 10)", "(2026, 11)"]
    prev = previous_complete(months, date(2026, 11, 17))
    assert prev is not None and prev.start.month == 10, "October, not September"


def test_the_running_period_is_never_complete_even_before_todays_bar_arrives():
    """Pre-open, the running week's last observed bar is yesterday, so ``p.end < today`` alone
    would present a partial current week as the previous completed one."""
    bars = [_daily(date(2026, 9, 15) + timedelta(days=i), 200 + i) for i in range(4)]   # wk 38
    bars += [_daily(date(2026, 9, 21) + timedelta(days=i), 300 + i) for i in range(2)]  # wk 39, Mon-Tue
    weeks = weekly(bars)
    # Wednesday the 23rd, before today's daily bar has formed: week 39 ends Tuesday.
    prev = previous_complete(weeks, date(2026, 9, 23))
    assert prev is not None and prev.start == date(2026, 9, 15), "week 38, not the partial week 39"


def test_previous_complete_does_not_trust_caller_ordering():
    bars = [_daily(date(2026, 9, 15) + timedelta(days=i), 200 + i) for i in range(4)]
    bars += [_daily(date(2026, 2, 23) + timedelta(days=i), 100 + i) for i in range(5)]
    shuffled = list(reversed(weekly(bars)))
    prev = previous_complete(shuffled, date(2026, 9, 23))
    assert prev is not None and prev.start == date(2026, 9, 15)
