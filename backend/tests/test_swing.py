"""The swing strategy's pieces on a hand-built daily series: a double bottom with a rejection
candle on volume, the break of the last lower high, and a stop that follows structure up."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from kotsin_nse.config import Settings
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.swing import SwingParams, SwingTrade, daily_frame, setups, simulate
from kotsin_nse.risk.costs import CostModel


def frame(path: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    rows, d = [], date(2025, 1, 1)
    for o, h, lo, c, v in path:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append({"ts": ist_at(d, "00:00"), "o": o, "h": h, "l": lo, "c": c, "v": v})
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def series() -> list[tuple[float, float, float, float, float]]:
    base = [(100.0, 101.0, 99.0, 100.0, 1000.0)] * 60
    down = [(100 - i, 100.5 - i, 98.8 - i, 99 - i, 1000.0) for i in range(5)]  # to ~95
    first_low = [(95.0, 95.5, 90.0, 94.0, 1000.0)]  # swing low 90
    bounce = [(94.0, 97.0, 93.5, 96.5, 1000.0), (96.5, 98.0, 95.5, 97.5, 1000.0), (97.5, 97.8, 94.0, 94.5, 1000.0)]
    drift = [(94.5, 95.0, 92.0, 92.5, 1000.0), (92.5, 93.0, 91.0, 91.5, 1000.0), (91.5, 92.0, 91.0, 91.6, 1000.0)]
    # the second test of 90: a long lower tail, close near the high, volume 2× — a double bottom
    rejection = [(91.6, 92.4, 90.05, 92.2, 2000.0)]
    momentum = [(92.2, 96.0, 92.0, 95.8, 1200.0), (95.8, 98.6, 95.5, 98.4, 1200.0)]  # closes above the 98 lower high
    after = [(98.4, 99.5, 97.5, 99.0, 1000.0)] * 5
    return base + down + first_low + bounce + drift + rejection + momentum + after


def test_a_double_bottom_rejection_on_volume_then_the_lower_high_breaks():
    d = daily_frame(frame(series()))
    got = setups(d, SwingParams(use_bias=False, sides="long"), None)
    assert got, "no setup found"
    s = got[-1]
    assert s["level"][0] == "DOUBLE BOTTOM" and abs(s["level"][1] - 90.0) < 1e-9
    assert s["extreme"] == 90.05 and s["side"] == "BULLISH"
    assert d["c"].iloc[s["conf"]] > s["lh"]


def test_the_stop_follows_each_new_confirmed_higher_low_and_exits_on_the_trail():
    up = [(100.0 + i, 101.5 + i, 99.5 + i, 101.0 + i, 1000.0) for i in range(8)]
    dip = [(108.0, 108.5, 104.0, 105.0, 1000.0), (105.0, 109.5, 104.5, 109.0, 1000.0), (109.0, 111.0, 108.5, 110.5, 1000.0),
           (110.5, 112.0, 109.5, 111.5, 1000.0), (111.5, 111.6, 103.0, 103.5, 1000.0)]
    d = daily_frame(frame([(100.0, 101.0, 99.0, 100.0, 1000.0)] * 60 + up + dip))
    i0 = 60
    t = SwingTrade("X", "BULLISH", "T", 0, 99.0, 99.0, ist_at(d["day"].iloc[i0], "09:15"), 100.0, 98.0, 120.0, 120.0, 10.0, 100)
    simulate(t, d, i0, None, SwingParams(slip_bps=0, stop_buffer_adr=0.0), CostModel(Settings(_env_file=None)), adr=2.0)
    assert t.stop_path, "the stop never moved"
    assert t.exit_reason == "TRAIL" and t.exits[0]["price"] == t.stop_path[-1]["stop"] == 104.0
    assert t.r > 1.5
