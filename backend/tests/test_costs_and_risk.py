"""Costs, sizing, wallets, exits, exposure.

The cost tests reproduce the measured NSE cash economics: at ₹33,000 a position the round trip was
~0.30% and flat brokerage was ~81% of it. If those numbers stop holding, either the model or the
finding is wrong, and we want to know which.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from itertools import pairwise

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import (
    Direction,
    ExitReason,
    Instrument,
    InstrumentKind,
    OrderSide,
    Position,
    PosSide,
)
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.risk.exits import ExitEngine, MarketView, apply_exit
from kotsin_nse.risk.exposure import ExposureBook
from kotsin_nse.risk.limits import RiskLimits
from kotsin_nse.risk.sizing import size_position
from kotsin_nse.risk.wallet import Wallet

from .conftest import ist_ts


def _settings() -> Settings:
    return Settings(_env_file=None)


# -- costs ------------------------------------------------------------------------------------------


def test_round_trip_on_nse_cash_at_33k_matches_the_measured_030_percent(equity):
    """The number that decided this book's fate. 0.299% measured; the model must land on it."""
    costs = CostModel(_settings())
    price, qty = 330.0, 100  # ₹33,000
    pct = costs.round_trip_pct(equity, price, qty)
    assert 0.28 <= pct <= 0.35, pct


def test_a_percentage_slab_does_not_silently_replace_the_flat_charge(equity):
    """With the slab off (the default) the flat ₹40 is what is charged. Turning it on must only
    ever reduce the charge, never be the reason a small position looks cheap."""
    flat = CostModel(Settings(_env_file=None))
    slab = CostModel(Settings(_env_file=None, cost_brokerage_pct=0.03))
    assert flat.leg(equity, OrderSide.BUY, 330.0, 100).brokerage == 40.0
    assert slab.leg(equity, OrderSide.BUY, 330.0, 100).brokerage < 40.0


def test_flat_brokerage_dominates_the_round_trip_at_small_size(equity):
    """81% of the round trip was ₹40/order × 2. That is why entries, not exits, were the binding
    problem: a fixed cost does not scale down."""
    costs = CostModel(_settings())
    ch = costs.round_trip(equity, 330.0, 330.0, 100)
    assert ch.brokerage == 80.0  # ₹40 × 2 legs
    assert ch.brokerage / ch.total > 0.70


def test_cost_share_collapses_at_larger_size(equity):
    """Break-even needed ~₹1.3 lakh per position. The model must show that."""
    costs = CostModel(_settings())
    small = costs.round_trip_pct(equity, 330.0, 100)  # ₹33k
    large = costs.round_trip_pct(equity, 330.0, 400)  # ₹132k
    assert large < small / 2


def test_option_charges_are_levied_on_premium_turnover_not_notional(option):
    costs = CostModel(_settings())
    assert costs.turnover(option, 50.0, 250) == 50.0 * 250
    ch = costs.leg(option, OrderSide.SELL, 50.0, 250)
    assert ch.stt > 0  # STT on the sell leg only
    assert costs.leg(option, OrderSide.BUY, 50.0, 250).stt == 0


def test_mcx_multiplier_is_applied_to_turnover(mcx_future):
    """ALUMINI is quoted per kg on a 1,000 kg contract. A 286-qty entry logged ₹99,943 when the
    real notional was ₹99.9 million."""
    costs = CostModel(_settings())
    assert costs.turnover(mcx_future, 349.45, 286) == 349.45 * 286 * 1000


# -- sizing ------------------------------------------------------------------------------------------


def test_sizing_is_driven_by_the_stop(option):
    costs = CostModel(_settings())
    tight = size_position(
        instrument=option, premium=50.0, option_stop=45.0, option_target1=80.0,
        balance=1_000_000, available=1_000_000, limits=RiskLimits(), costs=costs,
    )
    wide = size_position(
        instrument=option, premium=50.0, option_stop=25.0, option_target1=120.0,
        balance=1_000_000, available=1_000_000, limits=RiskLimits(), costs=costs,
    )
    assert tight.ok and wide.ok
    assert tight.qty > wide.qty, "a wider stop must buy fewer lots"


def test_sizing_respects_lot_granularity(option):
    res = size_position(
        instrument=option, premium=50.0, option_stop=45.0, option_target1=80.0,
        balance=1_000_000, available=1_000_000, limits=RiskLimits(), costs=CostModel(_settings()),
    )
    assert res.qty % option.lot_size == 0
    assert res.lots == res.qty // option.lot_size


def test_sizing_declines_an_unknown_contract_size_rather_than_guessing():
    broken = Instrument(
        scrip_code="1", symbol="X", segment=Segment.MCX_FO, kind=InstrumentKind.FUTURE, multiplier=0
    )
    res = size_position(
        instrument=broken, premium=100.0, option_stop=90.0, option_target1=130.0,
        balance=1_000_000, available=1_000_000, limits=RiskLimits(), costs=CostModel(_settings()),
    )
    assert not res.ok
    assert "declined" in res.reason or "unknown" in res.reason


def test_sizing_declines_when_costs_eat_the_move_to_t1(option):
    """The guard the old book did not have and its own economics asked for."""
    res = size_position(
        instrument=option, premium=50.0, option_stop=49.0, option_target1=50.2,
        balance=1_000_000, available=1_000_000, limits=RiskLimits(), costs=CostModel(_settings()),
    )
    assert not res.ok
    assert "costs are" in res.reason


def test_sizing_is_capped_by_the_position_budget(option):
    lim = RiskLimits(max_position_inr=20_000, risk_per_trade_pct=100.0)
    res = size_position(
        instrument=option, premium=50.0, option_stop=45.0, option_target1=90.0,
        balance=1_000_000, available=1_000_000, limits=lim, costs=CostModel(_settings()),
    )
    assert res.outlay <= 20_000


def test_sizing_shrinks_with_the_wallet(option):
    """CAN2 sized at a flat ₹33,000 and never read its wallet, so a position did not shrink in a
    drawdown."""
    costs, lim = CostModel(_settings()), RiskLimits()
    rich = size_position(instrument=option, premium=50.0, option_stop=45.0, option_target1=90.0,
                         balance=1_000_000, available=1_000_000, limits=lim, costs=costs)
    poor = size_position(instrument=option, premium=50.0, option_stop=45.0, option_target1=90.0,
                         balance=200_000, available=200_000, limits=lim, costs=costs)
    assert poor.qty < rich.qty


# -- wallet -------------------------------------------------------------------------------------------


def test_wallet_day_rolls_on_the_ist_calendar_not_utc():
    w = Wallet.new("FUDKII", 1_000_000, now=ist_ts("2026-09-18", "10:00"))
    assert w.rollover(ist_ts("2026-09-18", "23:00")) is False  # same IST day
    assert w.rollover(ist_ts("2026-09-19", "09:30")) is True


def test_daily_loss_breaker_trips_once_and_lifts_on_rollover():
    lim = RiskLimits(daily_loss_limit_pct=3.0)
    w = Wallet.new("FUDKII", 100_000, now=ist_ts("2026-09-18", "10:00"))
    w.apply_close(-4_000, ist_ts("2026-09-18", "11:00"))
    assert w.check_breakers(lim, ist_ts("2026-09-18", "11:00")) is not None
    assert w.halted
    assert w.check_breakers(lim, ist_ts("2026-09-18", "11:01")) is None  # only once
    w.rollover(ist_ts("2026-09-19", "09:30"))
    assert not w.halted


def test_reserve_and_release_track_deployed_capital():
    w = Wallet.new("FUDKII", 100_000)
    assert w.reserve(40_000, time.time()) is True
    assert w.available == 60_000
    assert w.reserve(70_000, time.time()) is False
    w.release(40_000, time.time())
    assert w.available == 100_000


# -- exits ---------------------------------------------------------------------------------------------


def _position(option, *, entry=50.0, stop=40.0, targets=(70.0, 90.0, 110.0, 130.0)) -> Position:
    return Position(
        id="p1", strategy="FUDKII", instrument=option, underlying=option, side=PosSide.LONG,
        qty=1000, entry=entry, opened_ts=time.time(), signal_id="s1", direction=Direction.BULLISH,
        equity_entry=1500.0, equity_sl=1460.0, equity_targets=(1550.0,),
        option_sl=stop, option_targets=targets,
    )


def test_stop_beats_force_flat_on_the_same_bar(option):
    e = ExitEngine(RiskLimits())
    pos = _position(option)
    d = e.evaluate(pos, MarketView(option_ltp=39.0, underlying_ltp=1500.0, now=time.time(),
                                   bars_held=1, past_force_flat=True))
    assert d is not None and d.reason is ExitReason.SL_OP


def test_underlying_stop_is_distinct_from_the_option_stop(option):
    e = ExitEngine(RiskLimits())
    pos = _position(option)
    d = e.evaluate(pos, MarketView(option_ltp=55.0, underlying_ltp=1450.0, now=time.time(),
                                   bars_held=1, past_force_flat=False))
    assert d is not None and d.reason is ExitReason.SL_EQ


def test_target_ladder_takes_partials_in_order(option):
    e = ExitEngine(RiskLimits())
    pos = _position(option)
    d = e.evaluate(pos, MarketView(option_ltp=71.0, underlying_ltp=1600.0, now=time.time(),
                                   bars_held=1, past_force_flat=False))
    assert d is not None and d.reason is ExitReason.TARGET
    # 40% of 4 lots is 1.6 lots; an option can only be sold in whole lots, so it floors to 1.
    assert d.qty == 250
    assert d.qty % option.lot_size == 0
    apply_exit(pos, d, fill_price=71.0, charges=10.0, now=time.time())
    assert pos.targets_hit == 1 and pos.qty_remaining == 750


def test_stop_moves_to_breakeven_after_t1(option):
    e = ExitEngine(RiskLimits())
    pos = _position(option)
    d = e.evaluate(pos, MarketView(option_ltp=71.0, underlying_ltp=1600.0, now=time.time(),
                                   bars_held=1, past_force_flat=False))
    apply_exit(pos, d, fill_price=71.0, charges=0.0, now=time.time())
    e.evaluate(pos, MarketView(option_ltp=72.0, underlying_ltp=1600.0, now=time.time(),
                               bars_held=2, past_force_flat=False))
    assert pos.option_sl >= pos.entry


def test_the_stop_only_ever_tightens(option):
    """Four places could move a HotStocks stop and one of them set it above the live price,
    stopping the position out instantly. One owner, and it only ratchets."""
    e = ExitEngine(RiskLimits())
    pos = _position(option)
    seen = [pos.option_sl]
    for ltp in (55.0, 65.0, 60.0, 58.0, 75.0, 52.0):
        e.evaluate(pos, MarketView(option_ltp=ltp, underlying_ltp=1600.0, now=time.time(),
                                   bars_held=1, past_force_flat=False))
        seen.append(pos.option_sl)
    assert all(b >= a for a, b in pairwise(seen))


def test_time_stop_and_eod_are_backstops(option):
    e = ExitEngine(RiskLimits(time_stop_bars=3))
    pos = _position(option)
    d = e.evaluate(pos, MarketView(option_ltp=52.0, underlying_ltp=1510.0, now=time.time(),
                                   bars_held=5, past_force_flat=False))
    assert d is not None and d.reason is ExitReason.TIME_STOP
    d = e.evaluate(_position(option), MarketView(option_ltp=52.0, underlying_ltp=1510.0,
                                                 now=time.time(), bars_held=1, past_force_flat=True))
    assert d is not None and d.reason is ExitReason.EOD


def test_apply_exit_reports_gross_and_accumulates_charges(option):
    e = ExitEngine(RiskLimits())
    pos = _position(option)
    d = e.evaluate(pos, MarketView(option_ltp=39.0, underlying_ltp=1500.0, now=time.time(),
                                   bars_held=1, past_force_flat=False))
    gross = apply_exit(pos, d, fill_price=39.0, charges=120.0, now=1000.0)
    assert math.isclose(gross, (39.0 - 50.0) * 1000)
    assert pos.charges == 120.0
    assert pos.status == "CLOSED" and pos.closed_ts == 1000.0


# -- exposure ---------------------------------------------------------------------------------------------


def test_a_book_may_not_double_up_on_one_underlying(option):
    book = ExposureBook(RiskLimits(max_positions_per_underlying=1))
    p = _position(option)
    p.strategy = "FUDKII"
    v = book.check(strategy="FUDKII", underlying="RELIANCE", outlay=10_000,
                   positions=[p], total_capital=1_000_000)
    assert not v.allowed and "already holds" in v.reason


def test_a_derived_book_is_not_locked_out_by_its_own_parent(option):
    """Regression, measured 2026-09-21 on a seeded session: 10 of 10 FUKAA signals were rejected
    with "1 already open in X". FUKAA is derived from FUDKII, so the two always fire on the same
    underlying in the same batch; a shared per-underlying count meant whichever was handled first
    took the slot and the other could never fill a single trade in its life."""
    book = ExposureBook(RiskLimits(max_positions_per_underlying=1))
    held = _position(option)
    held.strategy = "FUDKII"
    v = book.check(strategy="FUKAA", underlying="RELIANCE", outlay=10_000,
                   positions=[held], total_capital=1_000_000)
    assert v.allowed, v.reason


def test_money_is_still_capped_across_books(option):
    """Counting per book must not reopen P15: one trigger fanning into several funded positions
    is fine only while the total premium at risk in that underlying is capped."""
    book = ExposureBook(RiskLimits(max_underlying_exposure_pct=5.0))
    held = _position(option)
    held.strategy = "FUDKII"
    v = book.check(strategy="FUKAA", underlying="RELIANCE", outlay=40_000,
                   positions=[held], total_capital=1_000_000)
    assert not v.allowed and "exposure" in v.reason


def test_the_all_books_position_count_still_binds(option):
    book = ExposureBook(RiskLimits(max_positions_all_books=2))
    live = []
    for i, sym in enumerate(("A", "B")):
        p = _position(option)
        p.id, p.strategy = f"p{i}", "FUDKII"
        p.underlying = replace(option, symbol=sym, underlying=sym)
        live.append(p)
    v = book.check(strategy="FUKAA", underlying="C", outlay=1_000,
                   positions=live, total_capital=1_000_000)
    assert not v.allowed and "across all books" in v.reason


def test_exposure_snapshot_buckets_by_symbol(option):
    book = ExposureBook(RiskLimits())
    snap = book.snapshot([_position(option)], 1_000_000)
    assert "RELIANCE" in snap["by_underlying"]
    assert snap["gross"] > 0


def _option(*, lot_size: int):
    from kotsin_nse.config import Segment
    from kotsin_nse.domain import Instrument, InstrumentKind, OptionType

    return Instrument(
        scrip_code="1", symbol="X", segment=Segment.NSE_FO, kind=InstrumentKind.OPTION,
        name="X CE", lot_size=lot_size, tick_size=0.05, multiplier=1, expiry="2026-09-29",
        strike=1500.0, option_type=OptionType.CE, underlying="X",
    )


def test_the_lot_cap_binds_alongside_the_rupee_cap_whichever_is_lower():
    """BLUESTARCO on 2026-09-22: Rs 1,00,000 of a 15.26 premium is 20 lots of 325, and it took all
    20 because nothing capped the count. FUDKII-RT's exit ladder is written in lots — one at T1, the
    rest on the trail — so for that book the count has to be the specified four."""
    from kotsin_nse.risk.limits import RT_X_LIMITS, RiskLimits
    from kotsin_nse.risk.sizing import size_position

    inst = _option(lot_size=325)
    common = dict(
        instrument=inst, premium=15.26, option_stop=14.0, option_target1=21.0,
        balance=1_000_000.0, available=1_000_000.0, costs=CostModel(Settings(_env_file=None)),
    )
    uncapped = size_position(limits=RiskLimits(), **common)
    capped = size_position(limits=RT_X_LIMITS, **common)

    assert uncapped.lots == 20, "the base book is unchanged — the rupee cap alone bound"
    assert capped.lots == 4
    assert capped.qty == 4 * 325
    assert "lot cap 4" in capped.reason
    assert capped.outlay < uncapped.outlay


def test_a_rupee_cap_tighter_than_the_lot_cap_still_wins():
    """Whichever binds LOWER: a rich premium can seat fewer than four lots and that is the answer."""
    from kotsin_nse.risk.limits import RT_X_LIMITS
    from kotsin_nse.risk.sizing import size_position

    out = size_position(
        instrument=_option(lot_size=325), premium=120.0, option_stop=110.0, option_target1=180.0,
        balance=1_000_000.0, available=1_000_000.0, limits=RT_X_LIMITS, costs=CostModel(Settings(_env_file=None)),
    )
    assert out.lots < 4, "Rs 1,00,000 does not seat four lots of a 120.00 premium"
    assert out.reason == "ok", "the lot ceiling did not bind, so it is not reported"


def test_the_rt_pool_is_thirty_slots_and_the_base_book_keeps_its_own():
    from kotsin_nse.risk.limits import RT_X_LIMITS, RiskLimits

    assert RT_X_LIMITS.max_positions_per_strategy == 30
    assert RT_X_LIMITS.max_lots == 4
    # The all-books ceiling counts FUDKII's positions too, so the base book's 6 would have stopped
    # the twins at three pairs.
    assert RT_X_LIMITS.max_positions_all_books == 60
    assert RiskLimits().max_lots is None
    assert RiskLimits().max_positions_per_strategy == 3


def test_the_mcx_rt_book_has_its_own_wallet_and_enough_of_it_to_reach_its_slots():
    """Commodities and equities do not share a purse: one CRUDEOIL lot is a different size of bet
    from one BLUESTARCO lot, and a shared wallet would let whichever fired first decide what the
    other could afford."""
    from kotsin_nse.strategy.keys import INITIAL_INR, StrategyKey

    assert INITIAL_INR[StrategyKey.FUDKII_RT_MCX] == 3_000_000.0
    assert StrategyKey.FUDKII_RT_X not in INITIAL_INR, "the NSE book keeps the default"
    # 30 slots at the Rs 1,00,000 per-trade cap needs Rs 30,00,000 to be reachable at all; the
    # NSE book's Rs 10,00,000 binds at ten, which is the number that actually applies there.
    assert INITIAL_INR[StrategyKey.FUDKII_RT_MCX] / 100_000 == 30
