"""Pivots from the sessions before the day, and the zones where several of them meet."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kotsin_nse.market.session import ist_at
from kotsin_nse.structure.levels import PivotLevel, clusters, daily_levels, pivot_levels


def test_the_book_example_reproduces():
    # Pivot Boss p.117: H 7743, L 7587, C 7618 -> P 7649.33
    lv = {x.name: x.price for x in daily_levels(7743, 7587, 7618)}
    assert lv["P"] == pytest.approx(7649.33, abs=0.01)
    assert lv["R1"] == pytest.approx(2 * lv["P"] - 7587) and lv["S2"] == pytest.approx(lv["P"] - 156)
    assert lv["BC"] == pytest.approx(min(7665, 2 * lv["P"] - 7665)) and lv["TC"] >= lv["BC"]
    assert lv["H3"] == pytest.approx(7618 + 156 * 1.1 / 4) and lv["L4"] == pytest.approx(7618 - 156 * 1.1 / 2)


def test_only_sessions_before_the_day_count_and_week_and_month_are_the_previous_complete_ones():
    rows, d = [], date(2026, 9, 1)
    while d <= date(2026, 10, 9):
        if d.weekday() < 5:
            rows.append((ist_at(d, "00:00"), 100.0 + d.day, 90.0 + d.day, 95.0 + d.day))
        d += timedelta(days=1)
    lv = {x.name: x for x in pivot_levels(rows, date(2026, 10, 7))}
    assert lv["PDH"].price == 106.0  # 6 Oct, never 7 Oct itself
    assert lv["PWH"].price == 130.0 and lv["PWL"].price == 91.0  # the week of 28 Sep - 2 Oct: high 30 Sep, low 1 Oct
    assert lv["PMH"].price == 130.0 and lv["MP"].frame == "M"  # September


def test_levels_close_together_become_one_weighted_zone_on_the_right_side():
    lv = [PivotLevel("S1", 99.0, "D"), PivotLevel("WP", 99.4, "W"), PivotLevel("MS1", 99.7, "M"),
          PivotLevel("R1", 110.0, "D"), PivotLevel("PDH", 110.3, "D"), PivotLevel("R2", 120.0, "D")]
    cl = clusters(lv, adr=5.0, last=105.0)  # tolerance 0.5
    assert [(c.kind, c.weight, c.members) for c in cl] == [("SUPPORT", 6, ["S1", "WP", "MS1"]), ("RESISTANCE", 2, ["R1", "PDH"])]
    assert cl[0].low == 99.0 and cl[0].high == 99.7 and cl[1].distance_pct == pytest.approx((110 / 105 - 1) * 100)
    assert clusters(lv, adr=None, last=105.0) == []
