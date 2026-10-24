"""The results calendar → event sessions: the broadcast time decides the session, a quarter's several
filings are one event, a revision days later is not a new one; the month splitter; the weekly stats."""

from __future__ import annotations

from datetime import date

import pandas as pd

from kotsin_nse.research.events import _weekly_stats, event_sessions, months

SESSIONS = ["2025-01-13", "2025-01-14", "2025-01-15", "2025-01-16", "2025-01-17", "2025-01-20", "2025-01-21"]


def _r(symbol: str, ist: str, period_end: str = "31-Dec-2024", source: str = "results") -> dict:
    return {"symbol": symbol, "broadcast_ts": pd.Timestamp(ist).timestamp(), "broadcast_ist": ist, "period_end": period_end, "source": source}


def test_the_broadcast_time_picks_the_event_session():
    rows = [
        _r("A", "2025-01-13 19:40:13"),  # after the close -> next session
        _r("B", "2025-01-14 12:27:21"),  # in hours -> the same session, flagged
        _r("C", "2025-01-15 08:50:00"),  # before the open -> the same session, not in hours
        _r("D", "2025-01-18 11:00:00"),  # a Saturday -> Monday
        _r("E", "2025-01-21 18:00:00"),  # after the last known session -> dropped
    ]
    ev = event_sessions(pd.DataFrame(rows), SESSIONS).set_index("symbol")
    assert ev.loc["A", "E"] == "2025-01-14" and not ev.loc["A", "in_hours"]
    assert ev.loc["B", "E"] == "2025-01-14" and ev.loc["B", "in_hours"]
    assert ev.loc["C", "E"] == "2025-01-15" and not ev.loc["C", "in_hours"]
    assert ev.loc["D", "E"] == "2025-01-20"
    assert "E" not in ev.index


def test_a_quarters_filings_are_one_event_and_a_revision_days_later_is_not_another():
    rows = [
        _r("X", "2025-01-13 19:40:13"),  # consolidated
        _r("X", "2025-01-13 19:45:00"),  # standalone, minutes later: the same event
        _r("X", "2025-01-15 10:00:00", period_end="31-DEC-2024", source="integrated"),  # the same quarter again (case differs): dropped
        _r("X", "2025-01-16 10:00:00", period_end="30-Sep-2024"),  # another quarter but 2 sessions later: a revision, collapsed
        _r("X", "2025-01-21 10:00:00", period_end="31-Mar-2024"),  # 4 sessions on: its own event
    ]
    ev = event_sessions(pd.DataFrame(rows), SESSIONS)
    assert list(ev["E"]) == ["2025-01-14", "2025-01-21"]
    assert ev.iloc[0]["broadcast_ist"] == "2025-01-13 19:40:13"


def test_months_split_a_range_on_calendar_months():
    m = months(date(2021, 1, 15), date(2021, 3, 10))
    assert m == [(date(2021, 1, 1), date(2021, 1, 31)), (date(2021, 2, 1), date(2021, 2, 28)), (date(2021, 3, 1), date(2021, 3, 10))]


def test_weekly_stats_sum_events_inside_a_week():
    s = pd.Series([10.0, 20.0, -5.0, 15.0], index=["2025-01-13", "2025-01-14", "2025-01-20", "2025-01-21"])
    st = _weekly_stats(s)
    assert st["n"] == 4 and st["weeks"] == 2 and st["weeks_positive"] == 100.0 and st["avg_bps"] == 10.0 and st["hit_rate"] == 75.0
    assert st["by_year_bps"] == {"2025": 10.0}
