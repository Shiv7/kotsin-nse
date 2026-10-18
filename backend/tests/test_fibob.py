"""The daily inputs of the Fibonacci + pivot + order-block strategy: classic pivots, CPR, the trend,
all from sessions before the day."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from kotsin_nse.market.session import ist_at
from kotsin_nse.research.fibob import ZONES, daily_table


def _days(n: int, start: date = date(2025, 1, 1), up: float = 1.0) -> pd.DataFrame:
    rows, d, px = [], start, 100.0
    while len(rows) < n:
        if d.weekday() < 5:
            rows.append({"ts": ist_at(d, "00:00"), "o": px, "h": px + 2, "l": px - 1, "c": px + 1, "v": 1e6})
            px += up
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def test_classic_pivots_and_cpr_come_from_the_previous_session():
    t = daily_table(_days(60))
    day = t.index[-1]
    prev = t.iloc[-2]
    h, lo, c = prev["h"], prev["l"], prev["c"]
    p = (h + lo + c) / 3
    r = t.loc[day]
    assert r["P"] == pytest.approx(p) and r["R1"] == pytest.approx(2 * p - lo) and r["S1"] == pytest.approx(2 * p - h)
    assert r["R2"] == pytest.approx(p + (h - lo)) and r["S2"] == pytest.approx(p - (h - lo))
    bc = (h + lo) / 2
    assert r["BC"] == pytest.approx(min(bc, 2 * p - bc)) and r["TC"] == pytest.approx(max(bc, 2 * p - bc))
    assert r["PDH"] == h and r["PDL"] == lo


def test_a_rising_market_is_an_uptrend_and_a_falling_one_a_downtrend():
    assert daily_table(_days(80, up=1.0))["trend"].iloc[-1] == 1
    assert daily_table(_days(80, up=-1.0))["trend"].iloc[-1] == -1


def test_the_control_band_is_not_a_fibonacci_band():
    assert ZONES["golden"] == (0.5, 0.618) and ZONES["ote"] == (0.618, 0.79)
    lo, hi = ZONES["control"]
    assert not any(lo <= f <= hi for f in (0.382, 0.618, 0.786)) and lo <= 0.5 <= hi
