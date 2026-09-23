"""The counter-trend route (strategy/counter.py): the reference stack's wall-strength COUNTER."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from kotsin_nse.bars.pivots import PivotPoint, Zone
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType, Position, PosSide
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.counter import (
    COUNTER_WALL_MIN,
    Leg,
    at_pivot_read,
    counter_route,
    evaluate_wall,
    flipped_signal,
)
from kotsin_nse.strategy.keys import StrategyKey


def _pt(price, label, w=None):
    tf = label.split(".")[0]
    return PivotPoint(price, label, tf, w if w is not None else {"1d": 4.0, "1wk": 3.2, "1mo": 2.0}[tf])


# a bullish trigger candle: 98.5 → 101, close 100, ATR30m 2.0
BULL = dict(bullish=True, close=100.0, high=101.0, low=98.5, atr=2.0)


def test_a_daily_plus_a_weekly_level_ahead_is_a_wall_a_lone_daily_level_is_not():
    """5.2 means "one daily level is not a wall; a daily plus a weekly is". Nearness ranks the
    nearer level in full and the next at 80 %: 4.0 × 1.0 + 3.2 × 0.8 = 6.56."""
    w = evaluate_wall([_pt(100.6, "1d.R1"), _pt(100.9, "1wk.R1")], **BULL)
    assert w.strength == pytest.approx(6.56) and w.members == ("1d.R1", "1wk.R1") and w.timeframes == "1d/1wk"
    assert w.dist_atr == pytest.approx(0.3) and w.grade() == "STRONG"
    lone = evaluate_wall([_pt(100.6, "1d.R1")], **BULL)
    assert lone.strength == 4.0 and lone.grade() == "AVERAGE"


def test_only_levels_ahead_in_the_candle_or_just_past_the_extreme_count():
    # behind the close, beyond the reach (high + 0.5 ATR = 102.0), or on the other side: not a wall
    assert evaluate_wall([_pt(99.5, "1d.R1"), _pt(102.3, "1wk.R1"), _pt(97.0, "1d.S1")], **BULL).strength == 0.0
    # just past the extreme, inside the reach: counts
    assert evaluate_wall([_pt(101.8, "1d.R1"), _pt(101.9, "1wk.R1")], **BULL).strength == pytest.approx(6.56)
    # two clusters: the stronger one is the wall, not the nearer one
    w = evaluate_wall([_pt(100.2, "1d.TC"), _pt(101.6, "1wk.R1"), _pt(101.7, "1mo.R1"), _pt(101.8, "1d.R1")], **BULL)
    assert w.members == ("1wk.R1", "1mo.R1", "1d.R1")


def test_bearish_is_the_mirror_image():
    bear = dict(bullish=False, close=100.0, high=101.5, low=99.0, atr=2.0)
    w = evaluate_wall([_pt(99.4, "1d.S1"), _pt(99.0, "1wk.S1"), _pt(100.6, "1d.R1")], **bear)
    assert w.members == ("1wk.S1", "1d.S1") and w.strength == pytest.approx(6.56)
    # 1.2 apart is two clusters at 0.25 x ATR: the daily level alone is the wall
    assert evaluate_wall([_pt(99.4, "1d.S1"), _pt(98.2, "1wk.S1")], **bear).members == ("1d.S1",)


def _leg(points, *, name="equity", o=99.0, h=101.0, l=98.5, c=100.0, atr=2.0, st=None, st1=None):  # noqa: E741
    return Leg(name, o, h, l, c, atr, points, st, st1)


def test_the_route_needs_a_genuine_st_flip_and_a_wall_of_at_least_the_threshold():
    pts = [_pt(100.6, "1d.R1"), _pt(100.9, "1wk.R1")]
    d = counter_route([_leg(pts)], bullish=True, st_flipped=True)
    assert d.route == "COUNTER" and "wall-counter" in d.reason and d.wall.strength >= COUNTER_WALL_MIN and d.wall_leg == "equity"
    assert counter_route([_leg(pts)], bullish=True, st_flipped=False).route == "IN_TREND"
    weak = counter_route([_leg([_pt(100.6, "1d.R1")])], bullish=True, st_flipped=True)
    assert weak.route == "IN_TREND" and "AVERAGE wall 4.00" in weak.reason
    assert counter_route([_leg([])], bullish=True, st_flipped=True).route == "IN_TREND"
    assert counter_route([], bullish=True, st_flipped=True).route == "IN_TREND"
    # the wall is read on the future too
    d = counter_route([_leg([]), _leg(pts, name="future")], bullish=True, st_flipped=True)
    assert d.route == "COUNTER" and d.wall_leg == "future"


def test_at_pivot_the_sbilife_future_closing_on_its_s1_on_dried_volume_is_a_fade():
    """2026-09-23 09:45: SBILIFE's SEP future closed 1746.2 against its daily S1 1746.30 (0.01 ATR)
    on 0.79 / 0.51 volume; the equity's own S1 was 0.64 ATR away and its volume live."""
    fut = Leg("future", 1777.0, 1777.0, 1745.9, 1746.2, 7.84, [_pt(1746.3, "1d.S1"), _pt(1734.5, "1d.S2"), _pt(1762.0, "1d.PIVOT")], 0.79, 0.51)
    eq = Leg("equity", 1766.3, 1768.0, 1744.4, 1745.0, 7.84, [_pt(1739.97, "1d.S1"), _pt(1756.33, "1d.PIVOT")], 1.48, 2.51)
    r = at_pivot_read(fut, bullish=False, other_volume=eq.volume)
    assert r is not None and r.members == ("1d.S1",) and r.dist_atr == pytest.approx(0.013, abs=0.01)
    assert r.volume == "dried" and r.score > 0.7
    d = counter_route([eq, fut], bullish=False, st_flipped=True)
    assert d.route == "COUNTER" and "at-pivot: future 1d.S1" in d.reason
    # the equity alone: 0.64 ATR from its S1 with live volume — nothing to fade
    assert counter_route([eq], bullish=False, st_flipped=True).route == "IN_TREND"


def test_at_pivot_is_graded_a_mile_past_the_level_or_a_surge_is_a_breakout_not_a_fade():
    pts = [_pt(99.0, "1d.R1"), _pt(99.2, "1wk.R1")]  # a 7.2 wall the candle has crossed
    # the nearest member (1wk.R1 99.2) is the line; a close 0.5 ATR past it: nearness 0, score 0
    assert at_pivot_read(_leg(pts, c=100.2, st=0.5, st1=0.5), bullish=True).score == 0.0
    # crossed by 0.1 ATR on dried volume: a fade (0.8 x 1.38 x 0.8 x 1.0)
    r = at_pivot_read(_leg(pts, c=99.2, h=99.6, st=0.5, st1=0.5), bullish=True)
    assert r is not None and r.crossed_atr == pytest.approx(0.0) and r.score >= 0.5
    # the same geometry on a 3x surge: conviction, no fade — on this leg or the other
    assert at_pivot_read(_leg(pts, c=99.2, h=99.6, st=3.0, st1=0.5), bullish=True).score == 0.0
    assert at_pivot_read(_leg(pts, c=99.2, h=99.6, st=0.5, st1=0.5), bullish=True, other_volume="surge").score == 0.0
    # average volume halves it: a lone daily level then does not qualify, a wall still does
    lone = at_pivot_read(_leg([_pt(99.2, "1d.R1")], c=99.2, h=99.6, st=1.1, st1=1.0), bullish=True)
    assert lone is not None and lone.score < 0.5
    wall = at_pivot_read(_leg(pts, c=99.2, h=99.6, st=1.1, st1=1.0), bullish=True)
    assert wall is not None and wall.score >= 0.5


def test_a_rejection_candle_counts_for_three_quarters_on_its_own():
    """High pierced R1 by 0.4 ATR, close back 0.1 ATR under it in the lower half of the range."""
    pts = [_pt(100.2, "1d.R1"), _pt(100.4, "1wk.R1")]
    r = at_pivot_read(_leg(pts, o=99.5, h=101.0, l=98.5, c=100.0, st=1.2, st1=1.0), bullish=True)
    assert r is not None and r.rejected and r.pierced_atr == pytest.approx(0.4) and r.score >= 0.5


def _sig(**kw):
    base = dict(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BULLISH, ts=1_790_135_100,
                entry=100.0, stop=98.0, targets=(104.0,), grade="A", rr=2.0, reason="ST flip UP + close above upper band")
    base.update(kw)
    return Signal(**base)


def test_the_fade_flips_the_direction_and_replans_on_the_flipped_side():
    zones = [Zone(price=102.0, strength=7.2, members=["1d.R1", "1wk.R1"]),  # above: the wall (the bull's target)
             Zone(price=100.6, strength=4.0, members=["1d.TC"]),            # the fade's stop
             Zone(price=97.5, strength=6.0, members=["1d.S1", "1wk.S1"]),    # the fade's T1
             Zone(price=95.0, strength=4.0, members=["1d.S2"])]
    d = counter_route([_leg([_pt(100.6, "1d.TC"), _pt(100.9, "1wk.R1")])], bullish=True, st_flipped=True)
    fade = flipped_signal(_sig(), key=StrategyKey.FUDKII_CT_X, zones=zones, atr=2.0, tick_size=0.05, decision=d)
    assert fade is not None and fade.direction is Direction.BEARISH and fade.strategy is StrategyKey.FUDKII_CT_X
    # T1 97.5 snaps to the round figure 97.0 (round_figure_snap, within its 20 % cap) — engine behaviour
    assert fade.entry == 100.0 and fade.stop == 100.6 and fade.targets == (97.0,)
    assert fade.source_signal_id == _sig().signal_id and fade.signal_id != _sig().signal_id
    assert fade.context["counter"]["route"] == "COUNTER" and "COUNTER fade" in fade.reason
    # no wall on the flipped side → no fade
    assert flipped_signal(_sig(), key=StrategyKey.FUDKII_CT_X, zones=zones[:2], atr=2.0, tick_size=0.05, decision=d) is None


@pytest.mark.asyncio
async def test_a_counter_route_enters_ct_x_through_the_ordinary_entry_path(settings, monkeypatch):
    """The engine hook: a FUDKII trigger into a wall hands a flipped CT-X signal to _handle_signal;
    an in-trend route hands nothing and only stamps the card."""
    from kotsin_nse.bars.unified import BarSource, UnifiedBar
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE", tick_size=0.05)
    e.underlyings["RELIANCE"] = und
    e._pivot_points = lambda symbol: [_pt(100.6, "1d.R1"), _pt(100.9, "1wk.R1")]  # type: ignore[method-assign]
    e.zones_for = lambda symbol: [Zone(102.0, 7.2, ["1d.R1", "1wk.R1"]), Zone(100.6, 4.0, ["1d.TC"]), Zone(97.5, 6.0, ["1d.S1", "1wk.S1"])]  # type: ignore[method-assign]
    from kotsin_nse import engine as engine_mod
    monkeypatch.setattr(engine_mod, "atr", lambda bars, n: 2.0)  # ATR30m for the test

    async def no_future(underlying):
        return None

    e._fut_context = no_future  # type: ignore[method-assign]
    handled, routes = [], []

    async def capture(sig, bar):
        handled.append(sig)

    e._handle_signal = capture  # type: ignore[method-assign]
    e.alerts.mark_route = lambda signal_id, *, decision: routes.append((signal_id, decision["route"]))  # type: ignore[method-assign]
    bar = UnifiedBar(symbol="RELIANCE", scrip_code="2885", tf="30m", ts=1_790_135_100, open=99.0, high=101.0, low=98.5, close=100.0, volume=1e5, source=BarSource.LIVE, complete=True)
    await e._handle_counter(_sig(), bar)
    assert [s.strategy for s in handled] == [StrategyKey.FUDKII_CT_X] and handled[0].direction is Direction.BEARISH
    assert routes == [(_sig().signal_id, "COUNTER")]
    # a lone level: IN_TREND, nothing entered, the route still on the card
    e._pivot_points = lambda symbol: [_pt(100.6, "1d.R1")]  # type: ignore[method-assign]
    await e._handle_counter(_sig(), bar)
    assert len(handled) == 1 and routes[-1][1] == "IN_TREND"


@pytest.mark.asyncio
async def test_a_ct_x_fill_is_mirrored_into_ct_y_only(settings):
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    await e.start()
    try:
        opt = Instrument("45679", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=250, strike=1450.0,
                         option_type=OptionType.PE, underlying="RELIANCE")
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        now = time.time()
        pos = Position(id="c1", strategy="FUDKII_CT_X", instrument=opt, underlying=und, side=PosSide.LONG, qty=250,
                       entry=30.0, opened_ts=now, signal_id="s1", direction=Direction.BEARISH,
                       equity_entry=1500.0, equity_sl=1510.0, option_sl=24.0, option_targets=(45.0,))
        e.positions[pos.id] = pos
        await e._open_rt_twin(pos, opt, SimpleNamespace(fill=SimpleNamespace(ts=now, charges=12.5)))
        books = sorted(p.strategy for p in e.positions.values() if p.id != "c1")
        assert books == ["FUDKII_CT_Y"], "the fade never leaks into the in-trend books"
        assert e.wallets["FUDKII_RT_X"].available == e.wallets["FUDKII_RT_X"].balance
    finally:
        await e.stop()
