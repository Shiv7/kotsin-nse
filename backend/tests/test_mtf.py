"""Multi-timeframe building blocks: candles on the session grid, the broker's end-of-day artefacts
removed, swings, and the portfolio's open-position cap."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from kotsin_nse.config import Settings
from kotsin_nse.domain import OrderSide
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.mtf import (
    MTFParams,
    MTFTrade,
    _simulate,
    clean_5m,
    portfolio,
    resample,
    run,
    swings,
)
from kotsin_nse.research.obtest import _fut
from kotsin_nse.risk.costs import CostModel

D = date(2025, 10, 8)


def day_5m(d: date, end: str = "15:30", late_volume: float = 1000.0) -> pd.DataFrame:
    t0, t1 = ist_at(d, "09:15"), ist_at(d, end)
    rows = [{"ts": t, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000.0} for t in range(t0, t1, 300)]
    df = pd.DataFrame(rows)
    df.loc[df["ts"] >= ist_at(d, "15:15"), "v"] = late_volume
    return df


def test_hour_and_four_hour_bars_start_on_the_session_grid():
    df = day_5m(D)
    h = resample(df, "60m")
    starts = [pd.Timestamp(x, unit="s", tz="UTC").tz_convert("Asia/Kolkata").strftime("%H:%M") for x in h["ts"]]
    assert starts == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    assert int(h["end"].iloc[0]) == ist_at(D, "10:15")
    f = resample(df, "240m")
    assert [pd.Timestamp(x, unit="s", tz="UTC").tz_convert("Asia/Kolkata").strftime("%H:%M") for x in f["ts"]] == ["09:15", "13:15"]
    assert f["v"].iloc[0] == 48 * 1000.0


def test_cleaning_drops_empty_and_lumped_end_of_day_prints_but_keeps_a_normal_close():
    assert len(clean_5m(day_5m(D))) == 75
    assert len(clean_5m(day_5m(D, late_volume=2.0))) == 72  # 15:15, 15:20, 15:25 near-empty
    assert len(clean_5m(day_5m(D, late_volume=1e9))) == 72  # lumped
    with_post_close = pd.concat([day_5m(D), pd.DataFrame([{"ts": ist_at(D, "15:50"), "o": 1, "h": 1, "l": 1, "c": 1, "v": 5.0}])])
    assert len(clean_5m(with_post_close)) == 75


def test_swings_need_lower_highs_on_both_sides():
    h = [1, 2, 5, 3, 2, 6, 1, 0]
    lo = [1, 0.5, 2, 1, 0.2, 3, 0.9, 1]
    hi_idx, lo_idx = swings(pd.Series(h).to_numpy(float), pd.Series(lo).to_numpy(float), 2)
    assert hi_idx == [2, 5] and lo_idx == [4]


def _t(entry: int, exit_: int) -> MTFTrade:
    t = MTFTrade("X", "BULLISH", "s", 0, 0, 0, entry, 1, 0.9, 1.1, 1.2, 2, 1)
    t.exit_ts = exit_
    return t


def test_the_portfolio_never_holds_more_than_its_cap():
    trades = [_t(0, 100), _t(10, 50), _t(20, 60), _t(55, 70)]
    taken = portfolio(trades, 2)
    assert [t.signal_ts for t in taken] == [0, 10, 55]  # the one at 20 found two open


def test_a_trade_that_exits_in_two_pieces_pays_three_orders_not_two():
    settings = Settings(_env_file=None)
    costs = CostModel(settings)
    t = MTFTrade("X", "BULLISH", "s", 0, 99.0, 99.0, 1_000, 100.0, 99.5, 101.0, 102.0, 4.0, 100)
    F = np.array([[1_000, 100.0, 101.2, 100.0, 101.1], [1_300, 101.1, 101.1, 99.9, 99.95], [1_600, 99.9, 99.95, 99.8, 99.9]])
    _simulate(t, F, MTFParams(slip_bps=0), costs)
    assert [e["why"] for e in t.exits] == ["T1", "BE_STOP"]
    fut = _fut("X")
    legs = [costs.leg(fut, OrderSide.BUY, 100.0, 100), costs.leg(fut, OrderSide.SELL, 101.0, 50), costs.leg(fut, OrderSide.SELL, 100.0, 50)]
    assert t.costs == pytest.approx(sum(x.total for x in legs))
    assert sum(x.brokerage for x in legs) == 3 * settings.cost_brokerage_per_order_inr
    assert t.costs > costs.round_trip(fut, 100.0, 100.5, 100).total, "two legs at the average exit under-charge"


def test_the_holdout_is_refused():
    with pytest.raises(ValueError, match="holdout"):
        run(Settings(_env_file=None), date(2025, 9, 1), date(2025, 10, 15), MTFParams())
