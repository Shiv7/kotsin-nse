"""Sweep and value-area strategies on hand-built days: a signal fires only on the rule, and the
simulator fills stops and targets the way it says."""

from __future__ import annotations

from datetime import date

import numpy as np

from kotsin_nse.config import Settings
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.playbooks import (
    DayData,
    PBParams,
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
