"""Sweep and value-area strategies on hand-built days: a signal fires only on the rule, and the
simulator fills stops and targets the way it says."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from kotsin_nse.config import Settings
from kotsin_nse.domain import OrderSide
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.obtest import _fut
from kotsin_nse.research.playbooks import (
    DayData,
    PBParams,
    run,
    signals_sweep,
    signals_va_reentry,
    simulate,
)
from kotsin_nse.risk.costs import CostModel

DAY = date(2026, 10, 16)
OPEN = ist_at(DAY, "09:15")


def day(path: list[tuple[float, float, float, float]], **kw) -> DayData:
    rows = [(OPEN + 60 * i, o, h, lo, c, 1000.0) for i, (o, h, lo, c) in enumerate(path)]
    base = dict(adr=2.0, pdh=102.0, pdl=99.0, pdc=100.0, pvah=101.0, pval=99.5, ppoc=100.2)
    base.update(kw)
    return DayData("X", np.array(rows, dtype=float), OPEN, **base)


def flat(n: int, px: float = 100.0) -> list[tuple[float, float, float, float]]:
    return [(px, px + 0.05, px - 0.05, px)] * n


def test_a_wick_under_the_previous_low_that_closes_back_is_a_bullish_sweep():
    path = flat(20) + [(100.0, 100.0, 98.8, 98.9)] * 4 + [(98.9, 99.6, 98.9, 99.5)]  # 09:35 bar: low 98.8, close 99.5
    path += flat(30, 99.6)
    sigs = signals_sweep(day(path), PBParams(sweep_levels="PDL"))
    assert sigs and sigs[0]["side"] == "BULLISH" and sigs[0]["setup"] == "SWEEP PDL"
    s = sigs[0]
    assert s["stop"] < 98.8 and s["entry"] == 99.5 and s["t1"] > s["entry"]


def test_a_close_below_the_level_is_a_break_not_a_sweep():
    path = flat(20) + [(100.0, 100.0, 98.6, 98.7)] * 5 + flat(30, 98.7)
    assert signals_sweep(day(path), PBParams(sweep_levels="PDL")) == []


def test_value_area_reentry_after_two_half_hours_inside():
    path = flat(30, 101.3) + flat(30, 100.5) + flat(30, 100.4) + flat(60, 100.0)  # opened above VAH 101, back inside
    sigs = signals_va_reentry(day(path, pval=98.5), PBParams())
    assert sigs and sigs[0]["side"] == "BEARISH" and sigs[0]["t2"] == 98.5
    assert sigs[0]["ts"] == OPEN + 90 * 60  # the close of the second half hour inside


def test_simulator_takes_half_at_t1_moves_the_stop_to_breakeven_then_exits_there():
    path = [*flat(5), (100.0, 101.2, 100.0, 101.1), (101.1, 101.1, 99.9, 99.95), *flat(5, 99.9)]
    d = day(path)
    plan = {"side": "BULLISH", "setup": "T", "ts": OPEN + 5 * 60, "entry": 100.0, "stop": 99.5, "t1": 101.0, "t2": 102.0, "rr": 4.0}
    t = simulate(d, plan, PBParams(slip_bps=0), CostModel(Settings(_env_file=None)), DAY)
    assert [e["why"] for e in t.exits] == ["T1", "BE_STOP"]
    assert t.gross == (101.0 - 100.0) * t.qty * 0.5


def test_an_initial_balance_low_sweep_fires_once_the_ib_is_set_and_only_while_untaken():
    # the first hour's low 99.6 at 09:40; a quiet 20 minutes after the IB closes; the 10:35 5m bar
    # dips to 99.3 and closes back at 99.8 — a sweep of IBL (the IB's own bars SET the level, they
    # must not count as having taken it)
    path = flat(60)
    path[25] = (100.0, 100.05, 99.6, 100.0)
    path += flat(20) + [(100.0, 100.0, 99.3, 99.8)] * 5 + flat(30, 99.9)
    sigs = signals_sweep(day(path), PBParams(sweep_levels="IBL"))
    assert [s["setup"] for s in sigs] == ["SWEEP IBL"]
    assert sigs[0]["ts"] == OPEN + 85 * 60 and sigs[0]["side"] == "BULLISH" and sigs[0]["level"] == 99.6
    # the IB low broken (closed under) after the IB closed is not resting liquidity any more
    taken = list(path)
    taken[70:75] = [(100.0, 100.0, 99.5, 99.55)] * 5
    assert signals_sweep(day(taken), PBParams(sweep_levels="IBL")) == []


def test_a_trade_that_exits_in_two_pieces_pays_three_orders_not_two():
    path = [*flat(5), (100.0, 101.2, 100.0, 101.1), (101.1, 101.1, 99.9, 99.95), *flat(5, 99.9)]
    plan = {"side": "BULLISH", "setup": "T", "ts": OPEN + 5 * 60, "entry": 100.0, "stop": 99.5, "t1": 101.0, "t2": 102.0, "rr": 4.0}
    settings = Settings(_env_file=None)
    costs = CostModel(settings)
    t = simulate(day(path), plan, PBParams(slip_bps=0), costs, DAY)
    assert [e["why"] for e in t.exits] == ["T1", "BE_STOP"]
    fut = _fut("X")
    legs = [costs.leg(fut, OrderSide.BUY, 100.0, t.qty), costs.leg(fut, OrderSide.SELL, 101.0, t.qty / 2), costs.leg(fut, OrderSide.SELL, 100.0, t.qty / 2)]
    assert t.costs == pytest.approx(sum(x.total for x in legs))
    assert sum(x.brokerage for x in legs) == 3 * settings.cost_brokerage_per_order_inr
    assert t.costs > costs.round_trip(fut, 100.0, 100.5, t.qty).total, "two legs at the average exit under-charge"


def test_the_holdout_is_refused():
    with pytest.raises(ValueError, match="holdout"):
        run(Settings(_env_file=None), [date(2025, 9, 30), date(2025, 10, 1)], PBParams())
