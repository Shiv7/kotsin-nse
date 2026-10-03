"""FUDKII-CT-M (operator, 2026-10-03: "ok to 'fade when the market is clearly against' as a shadow"): a
published NSE trigger at most 45 % of the market agrees with is faded with CT-Y's plan under CT-M's own key
and wallet — beside CT-Y's 09:45 gap fade, never instead of it — and a trigger the market is not against is
recorded on CT-M's card as a skip with the share."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.bars.pivots import Zone
from kotsin_nse.domain import Direction
from kotsin_nse.engine import NO_PREMIUM_FLOOR, Engine
from kotsin_nse.risk.limits import CT_M_LIMITS, CT_M_MARKET_AGAINST_MAX, CT_Y_LIMITS
from kotsin_nse.strategy.keys import SHADOW_BOOKS, StrategyKey
from tests.test_gate_b_and_gap_fade import UND, _seed, _trigger


async def _route(settings, ctx: dict) -> tuple[Engine, list, list[dict]]:
    """The counter route on a bullish TATASTEEL trigger logged with ``ctx``: what reached the books."""
    e = Engine(settings)
    await e.start()
    _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT", "1wk.BC"])])
    trig = _trigger(Direction.BULLISH)
    e._breadth_at[trig.signal_id] = ctx
    handled: list = []

    async def capture(sig, bar, *, adopt=True):
        handled.append((sig, adopt))

    async def no_legs(underlying, bar, **_kw):
        return []

    e._handle_signal = capture  # type: ignore[method-assign]
    e._counter_legs = no_legs  # type: ignore[method-assign]
    await e._handle_counter(trig, None)  # type: ignore[arg-type]
    events = await e.ledger.rows_between("events", 0, time.time() + 5)
    return e, handled, events


@pytest.mark.asyncio
@pytest.mark.parametrize("share", [0.30, 0.45])
async def test_ct_m_fades_a_trigger_the_market_is_clearly_against(settings, share):
    e, handled, events = await _route(settings, {"share": share, "names": 220, "openBar": False, "gapDatr": 0.05, "atr30": 2.0})
    try:
        assert [s.strategy for s, _ in handled] == [StrategyKey.FUDKII_CT_M], "CT-M alone: no 09:45 gap, so no CT-Y gap fade"
        fade, adopt = handled[0]
        assert fade.direction is Direction.BEARISH and adopt is False
        assert fade.stop == pytest.approx(189.25), "CT-Y's plan: 1 ATR30 past the close, against the fade"
        assert fade.targets[0] == pytest.approx(184.0) and "MARKET FADE" in fade.reason
        assert fade.source_signal_id == _trigger(Direction.BULLISH).signal_id
        ev = [x for x in events if x.get("kind") == "counter.market_fade"]
        assert ev and ev[-1]["breadth"] == share and ev[-1]["side"] == "PE"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_ct_m_stands_aside_when_the_market_is_not_against_and_says_so(settings):
    e, handled, events = await _route(settings, {"share": 0.46, "names": 220, "openBar": False, "atr30": 2.0})
    try:
        assert handled == []
        skip = [x for x in events if x.get("kind") == "rt_twin.skipped" and x.get("book") == "FUDKII_CT_M"]
        assert skip and skip[-1]["gate"] == "market_with" and "46%" in skip[-1]["reason"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_breadth_that_cannot_be_read_decides_nothing(settings):
    e, handled, events = await _route(settings, {"share": None, "names": 12, "openBar": False, "atr30": 2.0})
    try:
        assert handled == []
        skip = [x for x in events if x.get("kind") == "rt_twin.skipped" and x.get("book") == "FUDKII_CT_M"]
        assert skip and skip[-1]["gate"] == "breadth_unread"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_ct_m_and_ct_ys_gap_fade_each_decide_for_themselves(settings):
    e, handled, _ = await _route(settings, {"share": 0.30, "names": 220, "openBar": True, "gapDatr": 0.45, "atr30": 2.0})
    try:
        assert sorted(s.strategy.value for s, _ in handled) == ["FUDKII_CT_M", "FUDKII_CT_Y"]
        by = {s.strategy: s for s, _ in handled}
        assert by[StrategyKey.FUDKII_CT_M].stop == by[StrategyKey.FUDKII_CT_Y].stop, "one plan, two books"
        assert "GAP FADE" in by[StrategyKey.FUDKII_CT_Y].reason and "MARKET FADE" in by[StrategyKey.FUDKII_CT_M].reason
    finally:
        await e.stop()


def test_the_gap_fade_keeps_its_plan_and_shares_it(settings):
    e = Engine(settings)
    _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT", "1wk.BC"]), Zone(182.1, 7.0, ["1d.S1", "1mo.TC"])])
    ctx = {"openBar": True, "gapDatr": 0.45, "atr30": 2.0}
    gap = e.gap_fade_plan(_trigger(Direction.BULLISH), ctx)
    plain = e.fade_plan(_trigger(Direction.BULLISH), ctx)
    assert gap.pop("gapDatr") == 0.45 and gap == plain
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "openBar": False}) is None
    assert e.fade_plan(_trigger(Direction.BULLISH), {**ctx, "openBar": False}) is not None, "CT-M's plan needs no gap"


def test_ct_m_is_a_paper_shadow_with_ct_ys_exits(settings):
    e = Engine(settings)
    assert StrategyKey.FUDKII_CT_M in SHADOW_BOOKS and StrategyKey.FUDKII_CT_M in NO_PREMIUM_FLOOR
    assert CT_M_MARKET_AGAINST_MAX == 0.45
    lim = e._exits_by_strategy[StrategyKey.FUDKII_CT_M.value].limits
    assert lim == CT_M_LIMITS and lim.gap_fade_datr is None and lim.max_premium_loss_pct is None
    assert lim.peak_giveback_pct == CT_Y_LIMITS.peak_giveback_pct and lim.fixed_lots_under_inr == CT_Y_LIMITS.fixed_lots_under_inr
    assert e.book_trades("FUDKII_CT_M", UND.segment)
