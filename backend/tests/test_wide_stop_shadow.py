"""The wide-stop shadow of RT-Y (operator, 2026-09-26: ""1% past" looks good this week — can we
shadow this"): every RT-Y entry is mirrored into FUDKII_RT_Y_W1 with ONE change — the equity stop
1 % further from entry and the option stop re-projected for it — so the two books answer, on the
same trades, whether the wider stop beats the touch. Sep 1–25 replay: unproven (+0.44 ± 1.86
points a trade on gate-B trades), which is why it runs as a shadow and not as RT-Y's rule."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    ExitReason,
    Instrument,
    InstrumentKind,
    OptionType,
    OrderSide,
)
from kotsin_nse.engine import IN_TREND_BOOKS, Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote, Selection, estimate_delta, map_levels_to_option
from kotsin_nse.risk.exits import MarketView
from kotsin_nse.risk.limits import RT_Y_LIMITS, RT_Y_W1_LIMITS
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import SHADOW_OF, StrategyKey

UND = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")


def _contract(direction: Direction) -> Instrument:
    bull = direction is Direction.BULLISH
    return Instrument("45678" if bull else "45679", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=250, tick_size=0.05,
                      strike=1520.0 if bull else 1480.0, option_type=OptionType.CE if bull else OptionType.PE, underlying="RELIANCE")


async def _rt_y_trigger(settings, direction: Direction, share: float) -> tuple[Engine, Instrument, Signal]:
    """A trigger into the in-trend books, the market ``share`` with it; the contract quoted at 20.00."""
    e = Engine(settings.model_copy(update={"paper_limit_orders": False}))
    await e.start()
    await e.set_mode(Mode.PAPER)
    e.underlyings["RELIANCE"] = UND
    opt = _contract(direction)
    bull = direction is Direction.BULLISH
    sig = Signal(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=direction, ts=int(time.time()) // 1800 * 1800,
                 entry=1500.0, stop=1490.0 if bull else 1510.0, targets=(1530.0 if bull else 1470.0,))
    e._breadth_at[sig.signal_id] = {"share": share, "names": 200}

    async def select(underlying, s, *, tape=True):
        return Selection(opt, premium=20.0, reason="ok", spread_pct=0.5)

    e._select_instrument = select  # type: ignore[method-assign]
    now = time.time()
    e.books[opt.scrip_code] = BookSnapshot(opt.scrip_code, bids=[(19.95, 500_000)], asks=[(20.0, 500_000)], ts=now)
    e.quotes[opt.scrip_code] = Quote(ltp=20.0, bid=19.95, ask=20.0, ts=now)
    return e, opt, sig


def test_the_shadow_is_rt_y_with_one_number_changed():
    assert SHADOW_OF == {StrategyKey.FUDKII_RT_Y_W1: StrategyKey.FUDKII_RT_Y}
    assert RT_Y_W1_LIMITS.equity_stop_buffer_pct == 1.0 and RT_Y_LIMITS.equity_stop_buffer_pct is None
    from dataclasses import replace

    assert RT_Y_LIMITS.max_premium_loss_pct == 25.0 and RT_Y_W1_LIMITS.max_premium_loss_pct is None, "a capped stop is not a wider one"
    # its stop is the PLAN's, 1 % further — never RT-Y's floored one, should that be switched on (2026-10-01)
    assert RT_Y_W1_LIMITS.min_equity_stop_atr is None
    assert replace(RT_Y_W1_LIMITS, equity_stop_buffer_pct=None, max_premium_loss_pct=25.0,
                   min_equity_stop_atr=RT_Y_LIMITS.min_equity_stop_atr) == RT_Y_LIMITS, "every other exit rule is RT-Y's"


@pytest.mark.asyncio
@pytest.mark.parametrize(("direction", "wide"), [(Direction.BULLISH, 1490.0 * 0.99), (Direction.BEARISH, 1510.0 * 1.01)])
async def test_an_rt_y_entry_opens_the_shadow_with_the_wider_stop(settings, direction, wide):
    e, opt, sig = await _rt_y_trigger(settings, direction, 0.7)  # the market with it: RT-Y enters
    try:
        before = e.wallets["FUDKII_RT_Y_W1"].available
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        y = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        w = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y_W1")
        assert (w.instrument, w.qty, w.entry, w.opened_ts, w.option_targets) == (y.instrument, y.qty, y.entry, y.opened_ts, y.option_targets)
        assert w.id != y.id and w.status == "OPEN" and f"shadow of {y.id}" in w.note
        assert y.equity_sl == sig.stop, "RT-Y keeps the stop as planned"
        assert w.equity_sl == pytest.approx(round(wide, 2)), "1 % further from entry"
        d = estimate_delta(spot=1500.0, strike=opt.strike, option_type=opt.option_type)
        projected, _ = map_levels_to_option(equity_entry=1500.0, equity_stop=w.equity_sl, equity_targets=(), option_premium=20.0, delta=d)
        assert w.option_sl == pytest.approx(projected) and w.option_sl < y.option_sl, "the option stop re-projected for the wider level"
        # its own purse, and its own order's charges (the size is RT-Y's own: 4 lots here)
        assert e.wallets["FUDKII_RT_Y_W1"].available == pytest.approx(before - 20.0 * y.qty - e.costs.leg(opt, OrderSide.BUY, 20.0, y.qty).total)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_trigger_rt_y_stands_aside_from_never_reaches_the_shadow(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.3)  # gate B: breadth
    try:
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        books = {p.strategy for p in e.positions.values()}
        assert "FUDKII_RT_Y" not in books and "FUDKII_RT_Y_W1" not in books
        assert {"FUDKII_RT_X", "FUDKII_RT_N"} <= books
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_shadow_exits_through_its_own_engine_on_its_own_stop(settings):
    """The underlying breaks RT-Y's stop (1490) but not the shadow's (1475.10): RT-Y's engine exits
    on the equity stop, the shadow's holds — then the shadow's own level takes it out."""
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        y = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        w = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y_W1")
        ey, ew = e._exits_by_strategy["FUDKII_RT_Y"], e._exits_by_strategy["FUDKII_RT_Y_W1"]
        assert ey is not ew and ew.limits is RT_Y_W1_LIMITS
        now = time.time()
        view = lambda und, t: MarketView(option_ltp=17.0, underlying_ltp=und, now=t, bars_held=1, past_force_flat=False,  # noqa: E731
                                         option_mid=17.0, spread_pct=0.01)
        dy = ey.evaluate(y, view(1488.0, now))
        assert dy is not None and dy.reason is ExitReason.SL_EQ
        assert ew.evaluate(w, view(1488.0, now)) is None, "1488 is inside the wider stop"
        dw = ew.evaluate(w, view(1474.0, now + 1))
        assert dw is not None and dw.reason is ExitReason.SL_EQ
    finally:
        await e.stop()
