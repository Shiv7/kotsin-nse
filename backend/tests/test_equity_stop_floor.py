"""RT-Y's underlying-stop floor (stop study, 2026-10-01). HDFCLIFE 09:45: the plan's stop sat 0.65 under
a 527.20 close — 0.18 ATR30, inside one bar's noise — a print AT it (09:56:33) stopped every book, and
the call then ran from 14.60 to 18.20. Grace on the trigger (a sustain, a 1-minute close, a buffer) did
not save it: the option stop is the same level through delta and fired two minutes later. Moving the
LEVEL out to ``min_equity_stop_atr`` (0.5) ATR30 did. RT-Y only; the touch stays the trigger."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType, Position, PosSide
from kotsin_nse.engine import Engine, _position_from_json, _position_json
from kotsin_nse.risk.exits import ExitEngine, ExitReason, MarketView
from kotsin_nse.risk.limits import (
    CT_Y_LIMITS,
    RT_N_LIMITS,
    RT_X_LIMITS,
    RT_Y_F_LIMITS,
    RT_Y_LIMITS,
    RT_Y_STOP_FLOOR_ATR,
    RT_Y_W1_LIMITS,
)
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey

UND = Instrument("1232", "HDFCLIFE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="HDFCLIFE")
CALL = Instrument("78683", "HDFCLIFE 27 OCT 2026 CE 530.00", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=1100, tick_size=0.05,
                  strike=530.0, option_type=OptionType.CE, underlying="HDFCLIFE", expiry="2026-10-27")
ATR = 3.685
#: RT-Y with its validated floor switched on — what the floor tests exercise; live, it is off (1 Oct)
FLOORED = replace(RT_Y_LIMITS, min_equity_stop_atr=RT_Y_STOP_FLOOR_ATR)


def _pos(stop: float = 526.55, atr: float = ATR) -> Position:
    return Position(id="p1", strategy=StrategyKey.FUDKII_RT_Y.value, instrument=CALL, underlying=UND, side=PosSide.LONG,
                    qty=4400, entry=14.60, opened_ts=1_790_828_107.0, signal_id="FUDKII-HDFCLIFE-1790826300-B",
                    direction=Direction.BULLISH, equity_entry=527.20, equity_sl=stop, equity_targets=(534.2,),
                    option_sl=14.22, option_targets=(16.63, 19.78), equity_atr=atr)


def test_the_floor_is_built_and_off_until_the_operator_says() -> None:
    assert RT_Y_STOP_FLOOR_ATR == 0.5 and RT_Y_LIMITS.min_equity_stop_atr is None
    for lim in (RT_Y_W1_LIMITS, RT_Y_F_LIMITS, CT_Y_LIMITS, RT_X_LIMITS, RT_N_LIMITS):
        assert lim.min_equity_stop_atr is None
    assert RT_Y_F_LIMITS.max_premium_loss_pct == RT_Y_LIMITS.max_premium_loss_pct, "the graded-F shadow keeps RT-Y's other rules"


def test_a_stop_inside_the_noise_moves_out_and_the_premium_cap_still_binds() -> None:
    pos = _pos()
    Engine._floor_equity_stop(Engine, pos, FLOORED, StrategyKey.FUDKII_RT_Y.value)  # type: ignore[arg-type]
    assert pos.equity_sl == round(527.20 - 0.5 * ATR, 2) == 525.36
    assert pos.option_sl < 14.22 and pos.option_sl == pos.initial_option_sl and pos.r_unit == abs(pos.entry - pos.option_sl)
    assert "stop floored 526.55 -> 525.36" in pos.note
    Engine._protect_option_stop(Engine, pos, FLOORED, StrategyKey.FUDKII_RT_Y.value, time.time())  # type: ignore[arg-type]
    assert pos.option_sl >= round(14.60 * 0.75, 2), "never more than 25 % under the premium"


def test_a_wide_stop_a_book_without_a_floor_and_no_atr_are_left_alone() -> None:
    wide = _pos(stop=525.0)
    Engine._floor_equity_stop(Engine, wide, FLOORED, "FUDKII_RT_Y")  # type: ignore[arg-type]
    assert wide.equity_sl == 525.0 and wide.option_sl == 14.22
    for lim, key in ((RT_X_LIMITS, "FUDKII_RT_X"), (RT_Y_F_LIMITS, "FUDKII_RT_Y_F"), (RT_Y_LIMITS, "FUDKII_RT_Y")):
        off = _pos()
        Engine._floor_equity_stop(Engine, off, lim, key)  # type: ignore[arg-type]
        assert off.equity_sl == 526.55
    no_atr = _pos(atr=0.0)
    Engine._floor_equity_stop(Engine, no_atr, FLOORED, "FUDKII_RT_Y")  # type: ignore[arg-type]
    assert no_atr.equity_sl == 526.55, "no ATR at the fill: the stop as planned, never a guess"


def test_a_bearish_stop_moves_up() -> None:
    put = replace(CALL, scrip_code="78690", option_type=OptionType.PE, strike=525.0)
    pos = replace(_pos(stop=527.85), instrument=put, direction=Direction.BEARISH)
    Engine._floor_equity_stop(Engine, pos, FLOORED, "FUDKII_RT_Y")  # type: ignore[arg-type]
    assert pos.equity_sl == round(527.20 + 0.5 * ATR, 2)


def test_the_floored_stop_survives_a_restart() -> None:
    pos = _pos()
    Engine._floor_equity_stop(Engine, pos, FLOORED, "FUDKII_RT_Y")  # type: ignore[arg-type]
    back = _position_from_json(_position_json(pos))
    assert back.equity_sl == 525.36 and back.equity_atr == ATR and back.option_sl == pos.option_sl
    old = _position_json(_pos())
    old.pop("equity_atr")
    assert _position_from_json(old).equity_atr == 0.0, "a row saved before the floor reads as no ATR"


def test_the_touch_stays_the_trigger() -> None:
    """A print at the (floored) stop still exits at once — the floor moves the level, not the rule."""
    for lim in (RT_Y_LIMITS, RT_X_LIMITS):
        pos = _pos()
        view = MarketView(option_ltp=14.10, underlying_ltp=526.55, now=1_790_828_193.0, bars_held=0,
                          past_force_flat=False, option_mid=14.12, spread_pct=0.01)
        d = ExitEngine(lim).evaluate(pos, view)
        assert d is not None and d.reason is ExitReason.SL_EQ


@pytest.mark.asyncio
async def test_an_entry_is_killed_only_by_the_stop_its_own_book_holds(settings) -> None:
    """HDFCLIFE: a print at 526.50 while an entry rests. RT-X holds the plan's 526.55 — dead; RT-Y
    holds 525.36 — alive. Through 525.36 it is dead for RT-Y too."""
    e = Engine(settings)
    t0 = int(time.time() // 1800 * 1800) - 1800 * 30
    e.store.seed("HDFCLIFE", "30m", [UnifiedBar(symbol="HDFCLIFE", scrip_code="1232", tf="30m", ts=t0 + 1800 * k, open=527.0,
                                                high=527.0 + ATR / 2, low=527.0 - ATR / 2, close=527.0, volume=1e6,
                                                source=BarSource.REST, complete=True) for k in range(30)])
    sig = Signal(strategy=StrategyKey.FUDKII, symbol="HDFCLIFE", direction=Direction.BULLISH, ts=t0 + 1800 * 30, entry=527.20,
                 stop=526.55, targets=(534.2,), grade="A", rr=10.8, reason="ST flip UP + close above upper band")
    assert e._book_stop(sig, "FUDKII_RT_Y") == 526.55, "live: the floor is off — RT-Y holds the plan's stop"
    e._exits_by_strategy["FUDKII_RT_Y"] = ExitEngine(FLOORED)  # switched on, as it would be
    assert e._book_stop(sig, "FUDKII_RT_Y") == pytest.approx(525.36, abs=0.02)
    assert e._book_stop(sig, "FUDKII_RT_X") == 526.55 and e._book_stop(sig, None) == 526.55
    e.ltps["1232"] = 526.50
    assert e._stop_breached(sig, UND, "FUDKII_RT_X") is not None
    assert e._stop_breached(sig, UND, "FUDKII_RT_Y") is None
    assert e._stop_breached(sig, UND, "FUDKII_RT_Y_F") is not None, "the graded-F shadow has no floor"
    e.ltps["1232"] = 525.30
    assert e._stop_breached(sig, UND, "FUDKII_RT_Y") is not None


def test_the_wide_shadow_widens_the_plans_stop_and_never_sits_nearer_than_rt_ys() -> None:
    """1 Oct check: 0.5 ATR30 is at most ~1 % of price (1,064 triggers, max 1.01 %), so the plan's stop
    1 % further stays the wider; the floor's extra dip only deepened the shadow's stop-outs."""
    rt_y = _pos()
    Engine._floor_equity_stop(Engine, rt_y, FLOORED, "FUDKII_RT_Y")  # type: ignore[arg-type]
    shadow = replace(rt_y, strategy="FUDKII_RT_Y_W1", equity_sl=526.55)
    Engine._widen_stop(Engine, shadow, RT_Y_W1_LIMITS, not_nearer_than=rt_y.equity_sl)  # type: ignore[arg-type]
    assert shadow.equity_sl == round(526.55 * 0.99, 2) == 521.28, "the plan's 526.55, 1 % further — as before the floor"
    # a name whose 0.5 ATR30 is more than 1 % of price: never nearer than RT-Y's own stop
    tight = replace(_pos(atr=11.0), strategy="FUDKII_RT_Y")
    Engine._floor_equity_stop(Engine, tight, FLOORED, "FUDKII_RT_Y")  # type: ignore[arg-type]
    wide = replace(tight, strategy="FUDKII_RT_Y_W1", equity_sl=527.10)
    Engine._widen_stop(Engine, wide, RT_Y_W1_LIMITS, not_nearer_than=tight.equity_sl)  # type: ignore[arg-type]
    assert wide.equity_sl == tight.equity_sl == round(527.20 - 5.5, 2)
