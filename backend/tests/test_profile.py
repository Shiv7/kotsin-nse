"""Volume and market profile on hand-built sessions."""

from __future__ import annotations

import pytest

from kotsin_nse.profile.core import bin_size, market_profile, volume_profile

T0 = 1_790_000_000


def test_bin_size_is_about_five_basis_points_in_whole_ticks():
    assert bin_size(1200) == 0.6
    assert bin_size(50) == 0.05


def test_volume_profile_finds_the_poc_and_a_value_area_holding_seventy_percent():
    rows = [(100.0, 100.0, 50.0), (100.5, 100.5, 30.0), (101.0, 101.0, 10.0), (99.5, 99.5, 10.0)]
    vp = volume_profile(rows, step=0.5)
    assert vp is not None and vp.poc == 100.0
    assert vp.val == 100.0 and vp.vah == 100.5  # 80 of 100 held by the POC and its heavier neighbour
    assert vp.total == pytest.approx(100.0)


def test_a_bar_spreads_its_volume_over_its_range():
    vp = volume_profile([(101.0, 100.0, 30.0)], step=0.5)
    assert vp is not None and vp.prices == [100.0, 100.5, 101.0] and vp.volumes == [10.0, 10.0, 10.0]


def test_market_profile_letters_initial_balance_and_a_trend_day():
    rows = []
    # A and B (the first hour) hold 100..101; then each half hour climbs a point higher
    for k in range(60):
        rows.append((T0 + 60 * k, 101.0, 100.0))
    for p in range(2, 6):
        for k in range(30):
            lo = 100.0 + p
            rows.append((T0 + 60 * (30 * p + k), lo + 1.0, lo))
    mp = market_profile(rows, T0, step=0.5)
    assert mp is not None
    assert (mp.ib_low, mp.ib_high) == (100.0, 101.0)
    assert mp.letters[0] == "AB" and "F" in mp.letters[-1]
    assert mp.day_type == "TREND" and mp.periods == 6
    assert mp.poor_high is False  # the high was reached by one period only


def test_neutral_day_extends_both_sides_of_the_first_hour():
    rows = [(T0 + 60 * k, 101.0, 100.0) for k in range(60)]
    rows += [(T0 + 60 * (60 + k), 102.0, 100.5) for k in range(30)]
    rows += [(T0 + 60 * (90 + k), 100.5, 99.0) for k in range(30)]
    mp = market_profile(rows, T0, step=0.5)
    assert mp is not None and mp.day_type == "NEUTRAL"


def test_a_late_session_is_lettered_from_its_first_bar():
    """MCX on 2026-10-20 traded only from 17:00: lettering from the 09:00 open left it with no
    first hour, so the day type read UNKNOWN."""
    from kotsin_nse.bars.store import BarStore
    from kotsin_nse.config import Segment
    from kotsin_nse.market.session import session_open_ts
    from kotsin_nse.profile.service import from_store

    from .conftest import bar, ist_ts

    store = BarStore()
    t0 = ist_ts("2026-10-20", "17:00")
    for m in range(90):
        store.close(bar(t0 + 60 * m, 100, 101 + m / 10, 99, 100, symbol="CRUDEOIL", tf="1m"))
    from datetime import date

    out = from_store(store, "CRUDEOIL", t0 + 91 * 60, session_open_ts(Segment.MCX_FO, date(2026, 10, 20)))
    mp = out["market"]
    assert mp["ib_high"] is not None and mp["day_type"] != "UNKNOWN"
    assert mp["letters"][-1].startswith("C")  # the third period's high, not period Q
