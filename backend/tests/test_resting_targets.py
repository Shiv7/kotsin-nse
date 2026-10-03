"""Target sells placed in advance (operator, 2026-09-26).

"upon approaching the target … why not place order in advance? … first come first serve has our
name too and in case it is a touch-and-fall case, we at least make profit on lot 1"."""

from __future__ import annotations

import copy
import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.domain import Direction, ExitReason, Position, PosSide
from kotsin_nse.engine import Engine
from kotsin_nse.exec.resting import touch_fills
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.risk.exits import MarketView

from .test_limit_orders import OPT, UND, _book, _engine, _rows

LOT = OPT.lot_size  # 2750


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _pos(book: str, now: float, *, targets: tuple[float, ...], lots: int = 4, entry: float = 17.0, option_sl: float = 15.0) -> Position:
    return Position(id=f"p-{book}", strategy=book, instrument=OPT, underlying=UND, side=PosSide.LONG, qty=lots * LOT, entry=entry,
                    opened_ts=now - 600, signal_id=f"s-{book}", direction=Direction.BULLISH, equity_entry=187.0, equity_sl=185.0,
                    equity_targets=(190.0, 192.0), option_sl=option_sl, option_targets=targets, option_t1=targets[0] if targets else 0.0)


async def _hold(e: Engine, pos: Position, now: float) -> Position:
    e.positions[pos.id] = pos
    e.wallets[pos.strategy].commit(pos.entry * pos.qty, now)
    return pos


async def _tick(e: Engine, clock, dt: float, bid: float, ask: float, *, und: float | None = 187.5) -> None:
    clock[0] += dt
    _book(e, bid, ask, clock[0])
    if und is not None:
        e.ltps[UND.scrip_code] = und
    await e._manage_positions()


def _sells(orders: list[dict]) -> list[dict]:
    return [o for o in orders if o.get("side") == "SELL" and o.get("status") == "FILLED"]


def test_a_resting_target_fills_on_a_touch():
    assert touch_fills(19.0, 19.0, None), "the bid came up to it"
    assert touch_fills(19.0, 18.9, 19.0), "a print at the level"
    assert not touch_fills(19.0, 18.9, 18.95)


@pytest.mark.asyncio
async def test_the_parents_share_ladder_rests_t1_and_t2_at_once(settings, clock):
    """Every rung rests from the fill (operator, 2026-10-03: "adding all targets immediately ... to make
    the most of first come first serve"); T2's order keeps its place when T1 fills."""
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII", clock[0], targets=(19.0, 21.0)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        t0 = clock[0]
        rs = e._targets_resting(pos.id)
        assert [(r.ctx[1], r.limit, r.intent.qty) for r in rs] == [(0, 19.0, LOT), (1, 21.0, 3 * LOT)], \
            "T1: 40 % of 4 lots, floored to a lot; T2, the last rung, the rest"
        t2_id = rs[1].intent.client_order_id
        await _tick(e, clock, 5, 18.60, 18.90)
        assert pos.targets_hit == 0 and e._target_resting(pos.id) is rs[0], "not touched: still resting"
        await _tick(e, clock, 5, 19.00, 19.20)  # the bid touches T1
        assert pos.targets_hit == 1 and pos.qty_remaining == 3 * LOT
        x = pos.exec_log["exits"][-1]
        assert x["reason"] == "TARGET" and x["fillPrice"] == 19.0 and x["outcome"] == "T1 resting limit filled"
        r2 = e._target_resting(pos.id)
        assert r2.intent.client_order_id == t2_id and (r2.ctx[1], r2.limit, r2.intent.qty) == (1, 21.0, 3 * LOT), "the same T2 order: its place kept"
        assert [t["outcome"] for t in pos.exec_log["targets"]] == ["placed", "placed", "filled"]
        assert len(_sells(await _rows(e, "orders"))) == 1
        # the card shows what is resting; the order trail (the ledger's copy) shows its life
        assert e._resting_target_card({"id": pos.id, "status": "OPEN"}) == {"rung": 2, "limit": 21.0, "qty": 3 * LOT, "placedTs": t0, "lots": 3}
        row = next(p for p in await _rows(e, "positions") if p["id"] == pos.id)
        assert [t["outcome"] for t in row["exec_log"]["targets"]] == ["placed", "placed", "filled"]
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["FUDKII_RT_X", "FUDKII_CT_X"])
async def test_an_own_ladder_touch_book_rests_one_lot_per_rung_with_the_touchs_state(settings, clock, book):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos(book, clock[0], targets=(18.5, 19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        r = e._target_resting(pos.id)
        assert (r.ctx[1], r.limit, r.intent.qty) == (0, 18.5, LOT)
        # what the exit engine's own touch would have done to the same position
        ref = copy.deepcopy(pos)
        exits = e._exits_by_strategy[book]
        d = exits._touch(ref, MarketView(option_ltp=18.5, underlying_ltp=None, now=clock[0] + 1, bars_held=0, past_force_flat=False,
                                         option_mid=18.55), 18.55, "option", "x")
        await _tick(e, clock, 1, 18.50, 18.60)
        assert pos.targets_hit == 1 and pos.qty_remaining == 3 * LOT and d.qty == LOT
        assert (pos.ratchet_sl, pos.option_sl, pos.armed_by, pos.armed_ts) == (ref.ratchet_sl, ref.option_sl, ref.armed_by, ref.armed_ts)
        assert pos.ratchet_sl == 17.0, "the hard SL steps to breakeven, as on the touch"
        r2 = e._target_resting(pos.id)
        assert (r2.ctx[1], r2.limit, r2.intent.qty) == (1, 19.5, LOT)
        await _tick(e, clock, 1, 19.50, 19.60)
        await _tick(e, clock, 1, 20.50, 20.60)
        r4 = e._target_resting(pos.id)
        assert pos.targets_hit == 3 and r4 is None, "no rung left: nothing rests"
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["FUDKII_RT_Y", "FUDKII_CT_Y", "FUDKII_RT_Y_W1"])
async def test_an_arm_at_pct_book_rests_its_first_tranche_no_lower_than_entry_plus_five_percent(settings, clock, book):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos(book, clock[0], targets=(17.30, 18.50)), clock[0])  # own T1 only +1.8 %
        await _tick(e, clock, 1, 17.10, 17.20, und=None)
        r = e._target_resting(pos.id)
        assert (r.ctx[1], r.limit, r.intent.qty) == (0, 17.85, LOT), "max(own T1, entry +5 %)"
        await _tick(e, clock, 1, 17.85, 17.95, und=None)
        assert pos.targets_hit == 1 and pos.option_targets == (17.85, 18.50), "the minimum became T1, as the touch would make it"
        assert pos.armed_ts is not None and pos.ratchet_sl == 17.0, "arm_at_pct books arm the band at the touch"
        r2 = e._target_resting(pos.id)
        assert (r2.ctx[1], r2.limit) == (1, 18.5)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_rt_n_rests_its_own_r1_and_a_touch_sells_lot_one_and_arms_it(settings, clock):
    """Operator, 2026-09-26: "yes RT-N to get advance target sells too". RT-N's T1 used to arm only on
    a 1-minute CLOSE over its own R1; its R1 sell now rests from the fill, so a touch sells lot one."""
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_N", clock[0], targets=(18.5, 19.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        r = e._target_resting(pos.id)
        assert r is not None and r.limit == 18.5 and r.intent.qty == LOT, "its own R1, one lot"
        await _tick(e, clock, 1, 18.50, 18.70)  # the bid touches R1
        assert pos.targets_hit == 1 and pos.qty_remaining == 3 * LOT and pos.armed_ts is not None, "lot one out at R1, armed at the touch"
        assert _sells(await _rows(e, "orders"))[0]["avg_price"] == 18.5
        r2 = e._target_resting(pos.id)
        assert r2 is not None and r2.limit == 19.5, "the next rung rests at once"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_touch_and_fall_keeps_lot_ones_profit_and_the_stop_cancels_the_next_rung(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        await _tick(e, clock, 1, 18.50, 18.60)  # the touch: lot 1 sold at 18.50
        assert pos.targets_hit == 1 and e._target_resting(pos.id).ctx[1] == 1
        gross_t1 = pos.realised_gross
        assert gross_t1 == pytest.approx((18.5 - 17.0) * LOT)
        await _tick(e, clock, 1, 16.80, 16.95)  # ... and the fall: through breakeven
        assert e._target_resting(pos.id) is None, "the T2 sell is off the book before the stop's SELL goes in"
        ex = e._exit_resting(pos.id)
        assert ex is not None and ex.intent.qty == 3 * LOT and ex.ctx[1].reason is ExitReason.SL_OP
        assert pos.exec_log["targets"][-1]["outcome"].startswith("cancelled — SL-OP")
        await _tick(e, clock, 16, 16.80, 16.95)  # crossed at the deadline
        assert pos.status == "CLOSED"
        orders = await _rows(e, "orders")
        assert len(_sells(orders)) == 2, "T1 and the stop — never a second SELL for the same lots"
        assert [o for o in orders if o.get("status") == "CANCELLED" and "|TGT|" in o.get("client_order_id", "")]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_equity_stop_cancels_the_resting_target_and_sells_the_whole_position_once(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII", clock[0], targets=(19.0, 21.0)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        assert e._target_resting(pos.id) is not None
        await _tick(e, clock, 1, 16.00, 16.20, und=184.5)  # the underlying through its stop: an urgent stop
        assert e._target_resting(pos.id) is None and e._exit_resting(pos.id) is None, "sold into the bid at once"
        x = pos.exec_log["exits"][-1]
        assert pos.status == "CLOSED" and x["reason"] == "SL-EQ" and x["qty"] == 4 * LOT and "at once" in x["outcome"]
        assert pos.exec_log["targets"][-1]["outcome"].startswith("cancelled — SL-EQ"), "the T1 sell came off first"
        await _tick(e, clock, 1, 19.00, 19.20, und=184.5)  # a spike to T1 now can fill nothing: it was cancelled
        assert pos.targets_hit == 0 and len(_sells(await _rows(e, "orders"))) == 1
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_exit_engines_own_target_first_cancels_the_resting_one(settings, clock):
    """The equity-T1 arm path: the underlying reaches its T1 before the option reaches its own rung —
    the exit engine's TARGET exits as today, only T1's resting sell comes off first, and T2's keeps its
    place in the queue (resized when the ladder is re-read)."""
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        rs = e._targets_resting(pos.id)
        assert [(r.limit, r.intent.qty) for r in rs] == [(19.5, LOT), (20.5, 3 * LOT)]
        t2_id = rs[1].intent.client_order_id
        await _tick(e, clock, 1, 18.20, 18.40, und=190.2)  # equity T1 190.0 reached, option at 18.30
        assert [r.limit for r in e._targets_resting(pos.id)] == [20.5], "T1's sell came off; T2's stays"
        ex = e._exit_resting(pos.id)
        assert ex is not None and ex.ctx[1].reason is ExitReason.TARGET and ex.intent.qty == LOT
        assert pos.option_targets[0] == pytest.approx(18.3), "the equity path's T1, as today"
        await _tick(e, clock, 1, 18.20, 18.40, und=190.2)
        assert [r.intent.client_order_id for r in e._targets_resting(pos.id)] == [t2_id], "nothing moves while the exit works"
        await _tick(e, clock, 46, 18.20, 18.40, und=190.2)  # the target exit crosses at 45 s
        assert pos.targets_hit == 1 and pos.qty_remaining == 3 * LOT
        await _tick(e, clock, 1, 18.20, 18.40, und=190.2)
        rs = e._targets_resting(pos.id)
        assert [(r.ctx[1], r.limit, r.intent.qty) for r in rs] == [(1, 19.5, LOT), (2, 20.5, 2 * LOT)], "the ladder as it now stands"
        assert rs[1].intent.client_order_id == t2_id, "the 20.5 order kept its place, cut to its new size"
        assert sum(r.intent.qty for r in rs) == pos.qty_remaining
        assert len(_sells(await _rows(e, "orders"))) == 1
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_restart_re_places_the_resting_target_from_the_positions_state(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = await _hold(e, _pos("FUDKII_RT_X", clock[0], targets=(18.5, 19.5, 20.5)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        await _tick(e, clock, 1, 18.50, 18.60)  # T1 filled, T2 resting
        before = e._target_resting(pos.id)
        assert before.ctx[1] == 1
    finally:
        await e.stop()
    e2 = await _engine(settings, clock)  # the same ledger: the position comes back, the order does not
    try:
        again = e2.positions[pos.id]
        assert again.targets_hit == 1 and again.ratchet_sl == 17.0 and e2._target_resting(pos.id) is None
        await _tick(e2, clock, 1, 18.60, 18.80)
        r = e2._target_resting(pos.id)
        assert r is not None and (r.ctx[1], r.limit, r.intent.qty) == (before.ctx[1], before.limit, before.intent.qty)
    finally:
        await e2.stop()


@pytest.mark.asyncio
async def test_switched_off_the_exit_engine_sells_targets_as_before(settings, clock):
    e = await _engine(settings, clock)
    from dataclasses import replace

    e.limit_policy = replace(e.limit_policy, rest_targets=False)
    try:
        pos = await _hold(e, _pos("FUDKII", clock[0], targets=(19.0, 21.0)), clock[0])
        await _tick(e, clock, 1, 17.40, 17.60)
        assert e._target_resting(pos.id) is None
        await _tick(e, clock, 1, 19.00, 19.20)
        ex = e._exit_resting(pos.id)
        assert ex is not None and ex.ctx[1].reason is ExitReason.TARGET, "the exit engine's touch → a resting exit at the mid"
    finally:
        await e.stop()
