"""Every target sell rests from the fill (operator, 2026-10-03): "adding all targets immediately as we
know and in case we need to exit at a peak or a trail ... we can exit the full trade and then cancel all
exit orders of that trade, in case there is an edit in target 2, let target 3 and 4 be as is or
recalibrate all at once if needed to make the most of first come first serve" — and "cancel the target
sells first, then immediately exit"."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.domain import ExitDecision, ExitReason
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.risk.exits import ExitEngine
from kotsin_nse.risk.limits import RT_X_LIMITS, RT_Y_LIMITS, RiskLimits

from .test_limit_orders import _engine, _rows
from .test_resting_targets import LOT, _hold, _pos, _sells, _tick


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _ladder(e: Engine, pid: str) -> list[tuple[int, float, int]]:
    return [(r.ctx[1], r.limit, r.intent.qty) for r in e._targets_resting(pid)]


def _on_sale(e: Engine, pid: str) -> int:
    return sum(r.intent.qty for r in e._targets_resting(pid))


# -- the ladder, pure ------------------------------------------------------------------------------


def test_every_rung_with_its_lots_lowest_first_and_never_more_than_is_held(clock):
    rt_x = ExitEngine(RT_X_LIMITS)
    assert rt_x.resting_ladder(_pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5))) == [
        (0, 18.5, LOT), (1, 19.5, LOT), (2, 20.5, 2 * LOT)], "a lot a rung, the last the rest"
    assert rt_x.resting_ladder(_pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5, 21.5), lots=2)) == [
        (0, 18.5, LOT), (1, 19.5, LOT)], "fewer lots than rungs: the lowest rungs get them"
    assert rt_x.resting_ladder(_pos("FUDKII_RT_X", clock[0], targets=(18.5, 18.0, 20.5))) == [
        (0, 18.5, LOT), (2, 20.5, 3 * LOT)], "a rung not above the one below is not a rung"
    assert ExitEngine(RiskLimits()).resting_ladder(_pos("FUDKII", clock[0], targets=(19.0, 21.0))) == [
        (0, 19.0, LOT), (1, 21.0, 3 * LOT)], "the parent: its share ladder"
    rt_y = ExitEngine(RT_Y_LIMITS)
    assert rt_y.resting_ladder(_pos("FUDKII_RT_Y", clock[0], targets=(17.30, 18.50))) == [
        (0, 17.85, LOT), (1, 18.5, 3 * LOT)], "max(own T1, entry +5 %), then the own rungs above it"
    assert rt_y.resting_ladder(_pos("FUDKII_RT_Y", clock[0], targets=())) == [(0, 17.85, LOT)], \
        "no own rung: T1 at the minimum, one lot — the give-back band carries the rest"
    p = _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5))
    p.targets_hit, p.qty_remaining = 1, 3 * LOT
    assert rt_x.resting_ladder(p) == [(1, 19.5, LOT), (2, 20.5, 2 * LOT)]
    for lots in (1, 2, 3, 4, 7):
        for targets in ((18.5,), (18.5, 19.5), (18.5, 19.5, 20.5, 21.5)):
            q = _pos("FUDKII_RT_X", clock[0], targets=targets, lots=lots)
            assert sum(x[2] for x in rt_x.resting_ladder(q)) <= q.qty_remaining


# -- the engine ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_whole_ladder_rests_from_the_fill_and_the_card_shows_every_rung(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        assert _ladder(e, pos.id) == [(0, 18.5, LOT), (1, 19.5, LOT), (2, 20.5, 2 * LOT)]
        card = e._resting_targets_card({"id": pos.id, "status": "OPEN"})
        assert [(c["rung"], c["limit"], c["lots"]) for c in card] == [(1, 18.5, 1), (2, 19.5, 1), (3, 20.5, 2)]
        assert [t["outcome"] for t in pos.exec_log["targets"]] == ["placed"] * 3
        assert len({r.intent.client_order_id for r in e._targets_resting(pos.id)}) == 3
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_jump_through_two_rungs_books_them_lowest_first_and_the_stop_steps_in_order(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        await _tick(e, clock, 1, 19.60, 19.80)  # through T1 and T2 in one move
        assert pos.targets_hit == 2 and pos.qty_remaining == 2 * LOT
        assert [x["fillPrice"] for x in pos.exec_log["exits"]] == [18.5, 19.5], "T1 first, then T2"
        assert pos.ratchet_sl == 18.5, "the hard SL stepped to T1 after T2, through breakeven after T1"
        assert _ladder(e, pos.id) == [(2, 20.5, 2 * LOT)]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_higher_rung_touched_before_the_one_below_has_booked_waits_for_it(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        t2 = e._targets_resting(pos.id)[1]
        await e._target_filled(t2, clock[0], 19.5, 19.5, 19.6, 0.0)
        assert pos.targets_hit == 0 and not pos.exec_log.get("exits") and t2 in e._targets_resting(pos.id), "stays where it rests"
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "note"), [(ExitReason.SL_OP, "option stop"), (ExitReason.TRAIL, "give-back line"),
                                              (ExitReason.EOD, "segment force-flat")])
async def test_any_other_exit_cancels_every_target_sell_first_then_sells_what_is_held(settings, clock, reason, note):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        await _tick(e, clock, 1, 18.50, 18.60)  # T1: lot one out
        assert pos.targets_hit == 1 and len(e._targets_resting(pos.id)) == 2
        await e._exit(pos, ExitDecision(pos.id, reason, 18.0, pos.qty_remaining, note), clock[0])
        assert e._targets_resting(pos.id) == [], "every rung off the book before the SELL goes in"
        outcomes = [t["outcome"] for t in pos.exec_log["targets"]]
        assert outcomes[-2:] == [f"cancelled — {reason.value}: {note}"] * 2
        await _tick(e, clock, 50, 18.00, 18.10)  # the exit works and crosses; a spike after it fills nothing
        await _tick(e, clock, 1, 21.00, 21.20)
        assert pos.status == "CLOSED" and pos.targets_hit == 1
        sells = _sells(await _rows(e, "orders"))
        assert len(sells) == 2 and sum(o["qty"] for o in sells) == 4 * LOT, "T1 and the exit — never more than was held"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_edit_in_t2_moves_t2_alone_and_t3_keeps_its_place(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        ids = [r.intent.client_order_id for r in e._targets_resting(pos.id)]
        pos.option_targets = (18.5, 19.8, 20.5)
        await _tick(e, clock, 1, 17.40, 17.60)
        now = e._targets_resting(pos.id)
        assert [(r.ctx[1], r.limit) for r in now] == [(0, 18.5), (1, 19.8), (2, 20.5)]
        assert now[0].intent.client_order_id == ids[0] and now[2].intent.client_order_id == ids[2], "T1 and T3 untouched"
        assert now[1].intent.client_order_id != ids[1], "T2 re-placed at its new price"
        assert [t["outcome"] for t in pos.exec_log["targets"]][-2:] == ["cancelled — the ladder moved on", "placed"]
        assert _on_sale(e, pos.id) <= pos.qty_remaining
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_rebuilt_ladder_keeps_every_order_whose_price_still_stands(settings, clock):
    """The ladder re-read with a new T1 below the old one (the stock reaching its T1 first): T1 is new,
    the orders at prices that still stand keep their place under their new rung numbers."""
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(19.5, 20.5, 21.5), lots=5), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        ids = {r.limit: r.intent.client_order_id for r in e._targets_resting(pos.id)}
        pos.option_targets = (18.3, 19.5, 20.5, 21.5)
        await _tick(e, clock, 1, 17.40, 17.60)
        now = {r.limit: r for r in e._targets_resting(pos.id)}
        assert sorted(now) == [18.3, 19.5, 20.5, 21.5]
        assert all(now[p].intent.client_order_id == ids[p] for p in (19.5, 20.5, 21.5)), "kept, relabelled"
        assert [(now[p].ctx[1], now[p].intent.qty) for p in (18.3, 19.5, 20.5, 21.5)] == [(0, LOT), (1, LOT), (2, LOT), (3, 2 * LOT)]
        assert _on_sale(e, pos.id) == pos.qty_remaining == 5 * LOT
    finally:
        await e.stop()
