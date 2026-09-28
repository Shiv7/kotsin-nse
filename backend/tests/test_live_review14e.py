"""The round-5 review's probes (review14e NEW-5a, NEW-5b), turned round: each ``test_bug_*`` asserted
a defect — here it asserts the correct behaviour and fails on the code the review read. The
``test_ok_*`` checks and the control are kept as they were. Fake broker only."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.domain import ExitDecision, ExitReason
from kotsin_nse.engine import _position_json
from kotsin_nse.venue.base import VenueError

from .fake_broker import FakeBroker
from .test_limit_orders import OPT, _book
from .test_live_orders import LOT, _calls, _held, _hold, _live, _step
from .test_live_review14c import Backlog, _alerts

FINAL = ("Fully Executed", "Cancelled", "Rejected")


@pytest.fixture
def clock(monkeypatch):
    from datetime import datetime
    from datetime import time as dtime

    from kotsin_nse.market.session import IST, ist_today

    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _sells(fake: FakeBroker) -> list[tuple]:
    return [c for c in _calls(fake, "place") if c[2] == "SELL"]


def _working_sells(fake: FakeBroker, code: str = OPT.scrip_code) -> list[dict]:
    return [o for o in fake.orders.values() if not o["buy"] and o["code"] == code and o["status"] not in FINAL]


def _broker_net(fake: FakeBroker, code: str, held_before: int) -> int:
    net = held_before
    for o in fake.orders.values():
        if o["code"] == code:
            net += o["traded"] if o["buy"] else -o["traded"]
    return net


class HoldableBacklog(Backlog):
    """Backlog + cancels that can be held (a broker slow to confirm)."""

    async def cancel_order(self, exch_order_id):
        return await FakeBroker.cancel_order(self, exch_order_id)


async def _release_scenario(settings, clock, *, reason=ExitReason.SL_OP, delay=45.0):
    fake = HoldableBacklog(clock, delay=delay)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake)
    sent = _alerts(e)
    pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
    await e.ledger.upsert_position(_position_json(pos))
    _book(e, 14.60, 15.00, clock[0])
    fake.lose_next = True
    await e._exit(pos, ExitDecision(pos.id, reason, 14.8, pos.qty, reason.value), clock[0])
    first = e._exit_resting(pos.id)
    for _ in range(35):
        await _step(e, clock, 1, 14.60, 15.00)
    await e.release_live_order(first.bo.client_order_id)
    await _step(e, clock, 1, 14.60, 15.00)
    await _step(e, clock, 1, 14.60, 15.00)
    second = e._exit_resting(pos.id)
    assert second is not None and second.bo is not first.bo
    return fake, e, sent, pos, first, second


async def _run(e, fake, clock, first, ticks, on_tick=None):
    """Ticks; returns [(t, held at broker, SELL working at broker)] and the worst excess."""
    rows, worst = [], 0
    for t in range(ticks):
        clock[0] += 1
        fake._visible(first.bo.remote_id)
        if on_tick:
            on_tick(t)
        _book(e, 14.60, 15.00, clock[0])
        await e._manage_positions()
        await e._live_quiesce()
        held = _broker_net(fake, OPT.scrip_code, 4 * LOT)
        working = sum(o["qty"] - o["traded"] for o in _working_sells(fake) if fake._visible(o["rid"]))
        rows.append((t, held, working))
        worst = max(worst, working - held)
    return rows, worst


# ---------------------------------------------------------------------------------------------------
# R4-1 re-checks with timing variations.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["mid_resize_slow_cancel", "mid_cross", "trail_long_deadline"])
async def test_ok_r4_1_resurrected_fill_variations_never_leave_a_sell_over_the_holding(settings, clock, variant):
    reason = ExitReason.TRAIL if variant == "trail_long_deadline" else ExitReason.SL_OP
    delay = {"mid_resize_slow_cancel": 45.0, "mid_cross": 50.0, "trail_long_deadline": 45.0}[variant]
    fake, e, _sent, _pos, first, _second = await _release_scenario(settings, clock, reason=reason, delay=delay)
    try:
        def on_tick(t):
            o = fake.orders.get(first.bo.remote_id)
            if o and fake._visible(first.bo.remote_id) and o["traded"] < 3 * LOT and o["status"] not in FINAL:
                fake.fill(first.bo.remote_id, LOT, 14.70)  # the late order fills a lot a second
            if variant == "mid_resize_slow_cancel":
                fake.hold_cancels = 6 <= t < 13  # cancels (the resize's, the late order's) are slow to confirm
                if t == 13:
                    fake.release_cancels()
        rows, worst = await _run(e, fake, clock, first, 30, on_tick)
        print(variant, "worst excess", worst, "rows", [r for r in rows if r[2] or r[1] != 4 * LOT][:14])
        # every working SELL fills at the end: the account must never go short
        for o in _working_sells(fake):
            fake.fill(o["rid"], o["qty"] - o["traded"], 14.5)
        assert _broker_net(fake, OPT.scrip_code, 4 * LOT) >= 0, rows
        n_over = sum(1 for r in rows if r[2] > r[1])
        print(variant, "ticks with SELL working beyond the holding:", n_over)
        if variant != "mid_resize_slow_cancel":
            assert n_over <= 1, (n_over, rows)  # the builder's stated residual: at most the one refresh
        # with cancels slow to confirm, the overlap lasts as long as the broker takes to confirm them
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# NEW-5a: a deferred exit (_pending_exit) the broker refuses backs off like any exit — not re-sent
# every tick.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new5a_a_refused_deferred_exit_backs_off_like_any_exit(settings, clock):
    class RefuseSells(FakeBroker):
        refuse = False

        async def place_order_raw(self, instrument, side, qty, *, price=0.0, intraday=True, remote_order_id):
            if self.refuse and side.value == "SELL":
                self.calls.append(("place", remote_order_id, side.value, int(qty), float(price)))
                raise VenueError("V1/PlaceOrderRequest: RMS - price outside the band", raw={"Status": 1})
            return await super().place_order_raw(instrument, side, qty, price=price, intraday=intraday,
                                                 remote_order_id=remote_order_id)

    e, fake = await _live(settings, clock, RefuseSells())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        # an exit whose next order (a resize or a cross) was deferred behind another SELL, now over
        e._pending_exit[pos.id] = (ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), 1, False)
        fake.refuse = True
        for _ in range(10):
            await _step(e, clock, 1, 14.60, 15.00)
        tries = len(_sells(fake))
        assert tries <= 4, f"{tries} refused SELLs in 10 ticks: the back-off (2, 4, 8 s) as on the normal path"
        assert pos.id in e._pending_exit, "still kept: sent again once the back-off is over"
        fake.refuse = False
        for _ in range(20):
            await _step(e, clock, 1, 14.60, 15.00)
        assert e._exit_resting(pos.id) is not None and pos.id not in e._pending_exit, "sent once the broker takes it"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_control_new5a_the_normal_exit_path_backs_off(settings, clock):
    class RefuseSells(FakeBroker):
        async def place_order_raw(self, instrument, side, qty, *, price=0.0, intraday=True, remote_order_id):
            self.calls.append(("place", remote_order_id, side.value, int(qty), float(price)))
            raise VenueError("V1/PlaceOrderRequest: RMS - price outside the band", raw={"Status": 1})

    e, fake = await _live(settings, clock, RefuseSells())
    try:
        await _hold(e, _held("FUDKII", clock[0], targets=()))
        for _ in range(10):
            await _step(e, clock, 1, 14.60, 15.00)
        print("normal path, refused SELL placements in 10 ticks:", len(_sells(fake)))
        assert len(_sells(fake)) <= 4
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# NEW-5b: a failed ledger write while a live target's fill is booked is alerted, and never blocks the
# position's stop: the fill is booked in memory before any write.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new5b_a_failed_ledger_write_during_a_target_fill_never_blocks_the_stop(settings, clock):
    e, fake = await _live(settings, clock)
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await e.ledger.upsert_position(_position_json(pos))
        await _step(e, clock, 1, 17.40, 17.60)
        tgt = e._target_resting(pos.id)
        assert tgt is not None
        real = e.ledger.insert_order
        state = {"n": 0}

        async def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("database or disk is full")
            return await real(*a, **k)

        e.ledger.insert_order = flaky  # type: ignore[method-assign]
        fake.fill(tgt.bo.remote_id, LOT, 19.0)
        await _step(e, clock, 1, 18.9, 19.1)
        e.ledger.insert_order = real  # type: ignore[method-assign]
        assert state["n"] >= 1, "the write did fail"
        assert tgt.bo.booked_qty == LOT and pos.qty_remaining == 3 * LOT and pos.targets_hit == 1, "booked in memory all the same"
        assert tgt.intent.client_order_id not in e._resting and tgt.bo.client_order_id not in e.live_orders.orders
        assert any("recording its fill failed" in a for a in sent), sent
        n = len(_sells(fake))
        for _ in range(20):  # the option falls through its stop
            await _step(e, clock, 1, 14.60, 15.00)
        assert len(_sells(fake)) > n and not [b for b in e._live_sells_working(pos) if b is tgt.bo], "the stop goes"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# OK: unwatch while the order IS tracked and working is refused; a stop still goes after an unwatch.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_unwatch_is_refused_while_the_order_is_tracked_and_working(settings, clock):
    fake, e, _sent, _pos, first, second = await _release_scenario(settings, clock, delay=10_000.0)
    try:
        with pytest.raises(ValueError):
            await e.unwatch_live_order(second.bo.client_order_id)  # a working order, not watched
        fake.hidden.clear()  # the released one turns up: resurrected, in the working set again
        await _step(e, clock, 1, 14.60, 15.00)
        if first.bo.client_order_id in e.live_orders.orders and not e.live_orders.orders[first.bo.client_order_id].settled:
            with pytest.raises(ValueError):
                await e.unwatch_live_order(first.bo.client_order_id)
    finally:
        await e.stop()
