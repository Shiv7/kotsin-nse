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


# ---- §7b: the expiry calendar, the roll count, the book ----

from kotsin_nse.research.events import admit, book_pnl, expiries, rolls  # noqa: E402


def _sessions(start: str, end: str, holidays: set[str] = frozenset()) -> list[str]:
    days = pd.bdate_range(start, end).strftime("%Y-%m-%d")
    return [d for d in days if d not in holidays]


def test_expiries_are_the_last_thursday_then_tuesday_from_september_2025_and_step_back_over_a_holiday():
    sess = _sessions("2025-07-01", "2025-10-31", holidays={"2025-08-28"})  # the last Thursday of Aug 2025 made a holiday
    exp = expiries(sess)
    assert "2025-07-31" in exp  # last Thursday
    assert "2025-08-27" in exp  # Wednesday: the session before the holiday Thursday
    assert "2025-09-30" in exp  # last Tuesday, after NSE's move
    assert "2025-10-28" in exp  # last Tuesday
    assert "2025-09-25" not in exp


def test_rolls_count_only_expiries_strictly_inside_the_hold():
    sess = _sessions("2025-01-01", "2025-03-31")
    exp = set(expiries(sess))
    assert exp == {"2025-01-30", "2025-02-27", "2025-03-27"}
    i = sess.index("2025-01-30")
    assert sess[i + 20] == "2025-02-27"
    assert rolls(i, i + 20, sess, exp) == 0  # entered on the Jan expiry, exits on the Feb expiry: no roll
    assert rolls(i, i + 21, sess, exp) == 1  # one session longer: Feb 27 is inside the hold
    assert rolls(i - 1, i + 21, sess, exp) == 2  # entered the day before the Jan expiry: two rolls
    k = sess.index("2025-01-02")
    assert rolls(k, k + 5, sess, exp) == 0


def test_the_book_admits_by_abs_z_until_the_cap_and_frees_room_on_the_exit_day():
    sess = _sessions("2025-01-01", "2025-03-31")
    cands = pd.DataFrame({
        "E": ["2025-01-02"] * 3 + ["2025-01-03"] + [sess[sess.index("2025-01-02") + 20]] * 2,
        "symbol": ["A", "B", "C", "D", "E", "F"],
        "z": [2.5, -4.0, 3.0, 2.2, 2.1, -2.0],
    })
    taken = admit(cands, sess, 20, 2)
    assert taken.tolist() == [False, True, True, False, True, True]  # B and C by |z|; D finds no room; A and E... A exits 20 sessions on, so two seats free
    assert admit(cands, sess, 20, None).all()


def test_book_pnl_sums_open_positions_and_charges_costs_on_the_exit_day():
    sess = _sessions("2025-01-01", "2025-02-28")
    i = sess.index("2025-01-02")
    closes = pd.DataFrame({"A": [100.0 * 1.01**k for k in range(len(sess))], "B": [50.0] * len(sess)}, index=sess)
    adm = pd.DataFrame({"E": ["2025-01-02", "2025-01-02"], "symbol": ["A", "B"], "side": [1.0, -1.0], "cost": [15.0, 30.0]})
    pnl = book_pnl(adm, closes, sess, 5)
    assert pnl["open"].iloc[i] == 0 and pnl["open"].iloc[i + 1] == 2 and pnl["open"].iloc[i + 5] == 2 and pnl["open"].iloc[i + 6] == 0
    assert abs(pnl["gross_bps"].iloc[i + 1] - 100.0) < 1e-6  # A up 1 % (100 bps), B flat
    assert pnl["cost_bps"].iloc[i + 5] == 45.0 and pnl["cost_bps"].sum() == 45.0
    assert abs(pnl["net_bps"].sum() - (500.0 - 45.0)) < 1e-6
