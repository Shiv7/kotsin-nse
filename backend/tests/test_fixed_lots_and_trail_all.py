"""Operator, 2026-09-27: "take 4 lots min as long as it is less than 75,000/-" and "exit 1 lot and
trail all for T2, T3, T4 or beyond till the 3% drop from the current latest peak" (RT-Y, RT-X, CT-Y,
CT-X only)."""

from __future__ import annotations

import time
from dataclasses import replace

from kotsin_nse.config import Settings
from kotsin_nse.domain import Direction
from kotsin_nse.instrument.select import Quote, SelectionPolicy, select_option
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.risk.exits import ExitEngine, apply_exit
from kotsin_nse.risk.limits import CT_X_LIMITS, RT_X_LIMITS, RT_Y_LIMITS, RiskLimits
from kotsin_nse.risk.sizing import size_position

from .test_exits_rt import _own, _view

FIXED = RiskLimits(max_lots=4, fixed_lots_under_inr=75_000.0)
COSTS = CostModel(Settings(_env_file=None))


# -- sizing ------------------------------------------------------------------------------------------


def test_four_lots_whatever_the_stop_when_they_cost_under_75000(option):
    """ADANIENT 24 Sep: 27.15 with its stop at 8.57 was cut to ONE lot by a 1 % risk budget — never
    the operator's rule. Fixed size buys 4 lots however far the stop is."""
    res = size_position(instrument=option, premium=27.15, option_stop=8.57, option_target1=None,
                        balance=1_000_000, available=1_000_000, limits=FIXED, costs=COSTS)
    assert res.ok and res.lots == 4 and res.qty == 4 * option.lot_size
    assert res.outlay == 27.15 * 4 * option.lot_size < 75_000
    old = size_position(instrument=option, premium=27.15, option_stop=8.57, option_target1=None,
                        balance=1_000_000, available=1_000_000, limits=RiskLimits(max_lots=4), costs=COSTS)
    assert old.lots == 2, "the risk budget it replaces: ₹10,000 / (18.58 × 250) = 2 lots"


def test_four_lots_at_75000_or_more_are_declined_not_cut(option):
    res = size_position(instrument=option, premium=75.0, option_stop=60.0, option_target1=None,  # 4 × 250 × 75 = 75,000
                        balance=1_000_000, available=1_000_000, limits=FIXED, costs=COSTS)
    assert not res.ok and "not under ₹75,000" in res.reason and res.declined == "wallet"


def test_four_lots_the_purse_cannot_pay_for_are_declined(option):
    res = size_position(instrument=option, premium=20.0, option_stop=15.0, option_target1=None,
                        balance=1_000_000, available=10_000, limits=FIXED, costs=COSTS)
    assert not res.ok and "left in the purse" in res.reason


def test_the_costs_against_t1_test_still_applies(option):
    res = size_position(instrument=option, premium=1.0, option_stop=0.5, option_target1=1.02,
                        balance=1_000_000, available=1_000_000, limits=FIXED, costs=COSTS)
    assert not res.ok and res.declined == "cost"


# -- the selector steps out ------------------------------------------------------------------------------


def test_the_selector_passes_over_a_strike_whose_four_lots_cost_75000_or_more(option):
    """Operator, 2026-09-22: "try identifying far otm who's 4 lots rest within our cap"."""
    near = replace(option, scrip_code="near", strike=1510.0)
    far = replace(option, scrip_code="far", strike=1550.0)
    now = time.time()
    quotes = {"near": Quote(ltp=80.0, bid=79.5, ask=80.5, ts=now),   # 4 × 250 × 80 = 80,000
              "far": Quote(ltp=40.0, bid=39.5, ask=40.5, ts=now)}    # 40,000
    pol = SelectionPolicy(outlay_lots=4, outlay_under_inr=75_000.0)
    sel = select_option(chain=[near, far], quotes=quotes, spot=1500.0, target1=1505.0, direction=Direction.BULLISH, now=now, policy=pol)
    assert sel.ok and sel.instrument.scrip_code == "far"
    plain = select_option(chain=[near, far], quotes=quotes, spot=1500.0, target1=1505.0, direction=Direction.BULLISH, now=now)
    assert plain.instrument.scrip_code == "near", "without the rule the nearest strike is chosen"


# -- trail all after T1 ------------------------------------------------------------------------------------


def _trail_all(lim):
    return ExitEngine(replace(lim, trail_all_after_t1=True, peak_giveback_spread_mult=0.0))


def test_t1_sells_one_lot_then_nothing_is_sold_at_t2_t3_t4():
    e = _trail_all(RT_X_LIMITS)
    pos = _own(qty=400)  # 4 lots of 100, entry 20.00, own ladder 24 / 28 / 32 / 36
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, now=2000.0))
    assert d is not None and d.reason.value == "TARGET" and d.qty == 100, "T1: one lot"
    apply_exit(pos, d, fill_price=24.0, charges=0, now=2000.0)
    assert pos.ratchet_sl == 20.0 and pos.armed_ts == 2000.0, "SL at breakeven and the line armed at the touch (RT-X too)"
    for k, px in enumerate((26.0, 28.0, 30.0, 32.0, 34.0, 36.0, 38.0), 1):
        assert e.evaluate(pos, _view(option_ltp=px, option_mid=px, now=2000.0 + 60 * k)) is None, f"{px}: nothing sold at a rung"
    assert pos.qty_remaining == 300 and pos.ratchet_sl == 20.0, "the SL steps no further than breakeven"
    assert e.resting_target(pos) is None, "no target sell rests after T1"


def test_the_rest_leaves_on_a_flat_3pct_drop_from_the_latest_peak():
    e = _trail_all(RT_X_LIMITS)
    pos = _own(qty=400)
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, now=2000.0))
    apply_exit(pos, d, fill_price=24.0, charges=0, now=2000.0)
    for k, px in enumerate((30.0, 36.0, 40.0), 1):
        assert e.evaluate(pos, _view(option_ltp=px, option_mid=px, now=2000.0 + 60 * k)) is None
    assert pos.peak_mid == 40.0
    # a wide spread no longer widens the line: 3 % of 40.00 is 38.80, whatever the book
    assert e.evaluate(pos, _view(option_ltp=38.85, option_mid=38.85, spread_pct=0.05, now=2300.0)) is None
    out = e.evaluate(pos, _view(option_ltp=38.75, option_mid=38.75, spread_pct=0.05, now=2315.0))
    assert out is not None and out.qty == 300, "every remaining lot, on one read through (RT-X)"
    assert "38.80" in out.note


def test_rt_y_keeps_its_floor_and_its_three_reads():
    e = _trail_all(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", qty=400, option_t1=20.5, option_targets=(20.5, 28.0, 32.0, 36.0))
    assert e.evaluate(pos, _view(option_ltp=20.6, option_mid=20.6, now=2000.0)) is None, "T1 = max(own T1, +5 %) = 21.00"
    d = e.evaluate(pos, _view(option_ltp=21.0, option_mid=21.0, now=2001.0))
    assert d is not None and d.qty == 100
    apply_exit(pos, d, fill_price=21.0, charges=0, now=2001.0)
    assert e.evaluate(pos, _view(option_ltp=30.0, option_mid=30.0, now=2100.0)) is None, "28.00 (T2) passed: nothing sold"
    assert pos.qty_remaining == 300
    reads = [e.evaluate(pos, _view(option_ltp=29.0, option_mid=29.0, now=2200.0 + k)) for k in range(3)]
    assert reads[0] is None and reads[1] is None and reads[2] is not None and reads[2].qty == 300, "three reads under 29.10"


def test_a_stop_still_exits_every_remaining_lot():
    e = _trail_all(CT_X_LIMITS)
    pos = _own(strategy="FUDKII_CT_X", qty=400)
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, now=2000.0))
    apply_exit(pos, d, fill_price=24.0, charges=0, now=2000.0)
    out = e.evaluate(pos, _view(option_ltp=19.9, option_mid=19.9, now=2100.0))
    assert out is not None and out.qty == 300, "back through breakeven: all of it"


def test_the_books_without_the_flag_are_unchanged():
    e = ExitEngine(RT_X_LIMITS)
    pos = _own(qty=400)
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, now=2000.0))
    apply_exit(pos, d, fill_price=24.0, charges=0, now=2000.0)
    assert pos.armed_ts is None, "RT-X as coded arms the line at the sustain, not the touch"
    d2 = e.evaluate(pos, _view(option_ltp=28.0, option_mid=28.0, now=2001.0))
    assert d2 is not None and d2.qty == 100, "and sells a lot at T2"
