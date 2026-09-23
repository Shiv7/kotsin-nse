"""The ₹5 premium floor is off for the FUDKII family, and the option stop on a cheap contract
is floored at eight ticks rather than the one-tick δ projection."""

from __future__ import annotations

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType, Position, PosSide
from kotsin_nse.engine import MIN_STOP_TICKS, SELECTION_POLICY, Engine
from kotsin_nse.instrument.select import Quote, select_option
from kotsin_nse.risk.exits import ExitEngine, MarketView
from kotsin_nse.risk.limits import RT_X_LIMITS
from kotsin_nse.strategy.keys import StrategyKey

CANBK = Instrument("95189", "CANBK", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=6750, tick_size=0.05,
                   expiry="2026-09-29", strike=125.0, option_type=OptionType.CE, underlying="CANBK")


def test_the_fudkii_family_selects_without_the_floor_and_fukaa_keeps_it():
    for k in (StrategyKey.FUDKII, StrategyKey.FUDKII_RT_X, StrategyKey.FUDKII_RT_N, StrategyKey.FUDKII_RT_Y, StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y):
        assert Engine.selection_policy_for(k).min_premium == 0.0
    assert Engine.selection_policy_for(StrategyKey.FUKAA).min_premium == SELECTION_POLICY.min_premium == 5.0


def test_a_one_rupee_contract_is_selectable_under_the_family_policy_and_not_under_the_shared_one():
    """CANBK 2026-09-23 09:45: 125 CE at ₹1.56 with 1.39 Cr OI, refused on the floor."""
    quotes = {"95189": Quote(ltp=1.56, bid=1.55, ask=1.60, ts=1_000.0)}
    family = Engine.selection_policy_for(StrategyKey.FUDKII)
    sel = select_option(chain=[CANBK], quotes=quotes, spot=124.94, target1=125.35, direction=Direction.BULLISH, now=1_000.0, policy=family)
    assert sel.ok and sel.instrument is CANBK and sel.premium == pytest.approx(1.575), "the mid — the matcher walks the ask"
    shared = select_option(chain=[CANBK], quotes=quotes, spot=124.94, target1=125.35, direction=Direction.BULLISH, now=1_000.0, policy=SELECTION_POLICY)
    assert not shared.ok and "premium" in shared.reason


def test_the_option_stop_is_floored_at_eight_ticks_for_the_family_only():
    assert Engine.floored_option_stop(StrategyKey.FUDKII, 1.60, 1.53, 0.05) == pytest.approx(1.60 - MIN_STOP_TICKS * 0.05)
    assert Engine.floored_option_stop(StrategyKey.FUDKII_RT_Y, 1.60, 1.53, 0.05) == pytest.approx(1.20)
    assert Engine.floored_option_stop(StrategyKey.FUDKII, 37.70, 31.82, 0.05) == 31.82, "a stop already further than the floor is untouched"
    assert Engine.floored_option_stop(StrategyKey.FUKAA, 1.60, 1.53, 0.05) == 1.53, "FUKAA keeps its projection"
    assert Engine.floored_option_stop(StrategyKey.FUDKII, 0.30, 0.28, 0.05) == 0.05, "never below one tick"


def test_the_rt_reprojection_honours_the_tick_floor_and_the_ratchet_still_wins():
    pos = Position(id="p", strategy="FUDKII_RT_X", instrument=CANBK, underlying=CANBK, side=PosSide.LONG, qty=6750, entry=1.60,
                   opened_ts=1.0, signal_id="s", direction=Direction.BULLISH, equity_entry=124.94, equity_sl=124.85, option_sl=1.53)
    e = ExitEngine(RT_X_LIMITS)
    assert RT_X_LIMITS.min_stop_ticks == 8
    e._reproject_stop(pos, MarketView(option_ltp=1.6, underlying_ltp=124.94, now=100.0, bars_held=0, past_force_flat=False, option_mid=1.6, spread_pct=0.03, quote_ok=True))
    assert pos.option_sl == pytest.approx(1.20), "the δ projection (a tick) is floored at eight ticks"
    pos.ratchet_sl = 1.60  # T1 touched: breakeven
    pos.last_reproject_ts = 0.0
    e._reproject_stop(pos, MarketView(option_ltp=1.9, underlying_ltp=125.5, now=200.0, bars_held=0, past_force_flat=False, option_mid=1.9, spread_pct=0.03, quote_ok=True))
    assert pos.option_sl == 1.60, "the ratchet is never given back"
