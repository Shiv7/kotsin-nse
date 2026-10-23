"""The owner's CPR sequence: a 5m double bottom under the CPR, the 1m Supertrend turning, a rejection at
P from above, and the first 1m close above the rejection bar's high — the entry; both stops scored."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from kotsin_nse.config import Settings
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.dbcpr import DbCprParams, trades_for
from kotsin_nse.risk.costs import CostModel

DAY = date(2024, 6, 4)


def _day() -> tuple[pd.DataFrame, pd.DataFrame]:
    # yesterday H 110, L 100, C 101: P 103.67, BC 102.33, TC 105
    d1 = pd.DataFrame({"ts": [ist_at(date(2024, 6, 3), "00:00"), ist_at(DAY, "00:00")],
                       "o": [105.0, 101.0], "h": [110.0, 112.0], "l": [100.0, 99.0], "c": [101.0, 111.0], "v": 1.0})
    c = np.r_[np.linspace(101, 99, 15),        # 09:15 a slide under the CPR: the first low
              np.linspace(99.1, 100.6, 15),    # 09:30 the bounce (the neckline)
              np.linspace(100.6, 99.1, 15),    # 09:45 the second low
              np.linspace(99.1, 104.2, 30),    # 10:00 through the neckline, the Supertrend turns, above P
              [103.8, 104.3],                  # 10:30 back to P and held; 10:31 above that bar's high
              np.linspace(104.4, 112, 60)]     # then up
    h, lo = c + 0.05, c - 0.05
    lo[75], h[75] = 103.6, 104.0  # the 10:30 rejection bar dips to P (103.67)
    t0 = ist_at(DAY, "09:15")
    m1 = pd.DataFrame({"ts": [t0 + 60 * i for i in range(len(c))], "o": c, "h": h, "l": lo, "c": c, "v": 1000.0})
    return m1, d1


def test_the_sequence_enters_on_the_push_from_a_held_pivot_with_both_stops_scored():
    m1, d1 = _day()
    got = trades_for("X", m1, d1, DbCprParams(fo_members=False), DAY, DAY, CostModel(Settings(_env_file=None)))
    longs = [t for t in got if t.side == "LONG"]
    assert {t.tags["stop"] for t in longs} == {"next level", "rejection bar"}
    for t in longs:
        assert t.signal_ts == ist_at(DAY, "10:31") + 60 and t.tags["level"] == "P"
        assert t.target == pytest.approx(t.entry + 2 * (t.entry - t.stop), abs=1e-3)
        assert t.exit_reason == "TARGET"
    nxt = next(t for t in longs if t.tags["stop"] == "next level")
    assert nxt.stop < 102.34, "a P rejection's next level down is BC"


def test_no_double_bottom_under_the_cpr_no_trade():
    m1, d1 = _day()
    m1[["o", "h", "l", "c"]] += 5.0  # the same shape, but the lows sit inside / above the CPR
    got = trades_for("X", m1, d1, DbCprParams(fo_members=False), DAY, DAY, CostModel(Settings(_env_file=None)))
    assert [t for t in got if t.side == "LONG"] == []
