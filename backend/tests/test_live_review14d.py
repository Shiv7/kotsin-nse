"""The round-4 review's probes (review14d R4-1, R4-3 and the order-book polling cost), turned round:
every ``test_bug_*`` asserted a defect — each one here asserts the correct behaviour and fails on the
code the review read; the measurement is now a bound. The ``test_ok_*`` checks are kept as they
were. Fake broker only."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from kotsin_nse.domain import ExitDecision, ExitReason
from kotsin_nse.engine import Engine, _position_json

from .fake_broker import FakeBroker
from .test_limit_orders import OPT, _book, _sig
from .test_live_orders import LOT, _calls, _held, _hold, _live, _step
from .test_live_review14c import Backlog, _alerts


@pytest.fixture
def clock(monkeypatch):
    from datetime import datetime
    from datetime import time as dtime

    from kotsin_nse.market.session import IST, ist_today

    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


FINAL = ("Fully Executed", "Cancelled", "Rejected")


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


async def _released_then_replaced(settings, clock, *, delay: float, trail: bool = False, **update):
    """An exit whose answer is lost on a broker backlog; the operator (having looked at the broker's book,
    where it is not yet) releases it; the exit loop sends the replacement."""
    fake = Backlog(clock, delay=delay)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake, **update)
    sent = _alerts(e)
    pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
    await e.ledger.upsert_position(_position_json(pos))
    _book(e, 14.60, 15.00, clock[0])
    fake.lose_next = True
    reason = ExitReason.TRAIL if trail else ExitReason.SL_OP
    await e._exit(pos, ExitDecision(pos.id, reason, 14.8, pos.qty, reason.value), clock[0])
    first = e._exit_resting(pos.id)
    assert first.bo.unconfirmed
    for _ in range(35):
        await _step(e, clock, 1, 14.60, 15.00)
    await e.release_live_order(first.bo.client_order_id)
    await _step(e, clock, 1, 14.60, 15.00)
    await _step(e, clock, 1, 14.60, 15.00)
    second = e._exit_resting(pos.id)
    assert second is not None and second.bo is not first.bo and second.intent.qty == pos.qty
    return fake, e, sent, pos, first, second


# ---------------------------------------------------------------------------------------------------
# R4-1: the released SELL turns up and PART-fills — the replacement is taken off at once and sent again
# for what is held once that fill is booked: never more SELL working than lots held.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_r4_1_after_a_resurrected_part_fill_the_replacement_never_sells_more_than_is_held(settings, clock):
    fake, e, _sent, pos, first, _second = await _released_then_replaced(settings, clock, delay=45.0)
    try:
        timeline = []
        for t in range(12):
            clock[0] += 1
            if fake._visible(first.bo.remote_id) and fake.orders[first.bo.remote_id]["traded"] == 0:
                fake.fill(first.bo.remote_id, 2 * LOT, 14.70)  # it reaches the exchange and two of its four lots match
            _book(e, 14.60, 15.00, clock[0])
            await e._manage_positions()
            await e._live_quiesce()
            held = _broker_net(fake, OPT.scrip_code, 4 * LOT)
            working = sum(o["qty"] - o["traded"] for o in _working_sells(fake) if fake._visible(o["rid"]))
            timeline.append((t, pos.qty_remaining, held, working))
        assert not [x for x in timeline if x[3] > x[2]], timeline
        assert first.bo.client_order_id not in e.live_orders.orders, "the resurrected order was cancelled, its 2 lots booked"
        assert pos.qty_remaining == 2 * LOT and _broker_net(fake, OPT.scrip_code, 4 * LOT) == 2 * LOT, "booked once: the books agree"
        now_working = _working_sells(fake)
        assert [o["qty"] - o["traded"] for o in now_working] == [2 * LOT], "the exit works on for the 2 lots held"
        fake.fill(now_working[0]["rid"], 2 * LOT, 14.60)
        await _step(e, clock, 1, 14.60, 15.00)
        assert pos.status == "CLOSED" and _broker_net(fake, OPT.scrip_code, 4 * LOT) == 0, "flat — never short"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_bug_r4_1b_restart_variant_booked_once_and_never_oversold(settings, clock):
    fake = Backlog(clock, delay=10_000.0)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 14.60, 15.00, clock[0])
        fake.lose_next = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        lost = e._exit_resting(pos.id).bo
        await e.release_live_order(lost.client_order_id)
        await e.ledger.upsert_position(_position_json(pos))
    finally:
        await e.stop()  # (no replacement yet: the engine stopped at once)
    fake.hidden.clear()
    fake.fill(lost.remote_id, LOT, 14.50)  # while down: it reached the exchange, one lot matched, still working
    e2 = Engine(settings.model_copy(update={"paper_limit_orders": True}))
    await e2.start()
    try:
        e2.live_orders.rest = fake
        from kotsin_nse.exec.gateway import Mode
        await e2.set_mode(Mode.LIVE, armed_minutes=60)
        await e2._settle_left_behind_live_orders()
        p2 = e2.positions[pos.id]
        for _ in range(6):
            await _step(e2, clock, 1, 14.60, 15.00)
            held = _broker_net(fake, OPT.scrip_code, 4 * LOT)
            assert sum(o["qty"] - o["traded"] for o in _working_sells(fake)) <= held
        assert fake.orders[lost.remote_id]["status"] == "Cancelled"
        exits = [x for x in p2.exec_log.get("exits", []) if x.get("qty")]
        assert sum(x["qty"] for x in exits if x.get("outcome") == "booked from the broker's report") == LOT, exits
        assert p2.qty_remaining == 3 * LOT and fake.peak_sell_working.get(OPT.scrip_code, 0) <= 4 * LOT
        working = _working_sells(fake)
        assert [o["qty"] - o["traded"] for o in working] == [3 * LOT], "the stop works for the 3 lots held"
        fake.fill(working[0]["rid"], 3 * LOT, 14.60)
        await _step(e2, clock, 1, 14.60, 15.00)
        assert p2.status == "CLOSED" and _broker_net(fake, OPT.scrip_code, 4 * LOT) == 0
    finally:
        await e2.stop()


# ---------------------------------------------------------------------------------------------------
# R4-2: the whole order book is read at most every 5 s for an unconfirmed or watched order; the
# status call by RemoteOrderID every refresh; the health page counts the book reads a minute.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_r4_2_the_order_book_is_read_at_most_every_5_s_while_an_order_is_unconfirmed_or_watched(settings, clock):
    fake = Backlog(clock, delay=10_000.0)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        fake.lose_next = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        n0, s0 = len(_calls(fake, "book")), len(_calls(fake, "status"))
        for _ in range(60):
            await _step(e, clock, 1, 14.60, 15.00)
        unconfirmed_rate = (len(_calls(fake, "book")) - n0) / 60
        assert (len(_calls(fake, "status")) - s0) / 60 >= 0.95, "by RemoteOrderID every refresh"
        lost = e._exit_resting(pos.id).bo
        await e.release_live_order(lost.client_order_id)
        e.positions.pop(pos.id)
        e._resting.clear()
        n1 = len(_calls(fake, "book"))
        for _ in range(60):
            await _step(e, clock, 1, 14.60, 15.00)
        watch_rate = (len(_calls(fake, "book")) - n1) / 60
        assert unconfirmed_rate <= 0.25 and watch_rate <= 0.25, (unconfirmed_rate, watch_rate)
        stats = e.live_orders.stats()
        assert stats["order_book_calls"] >= 20 and 0 < stats["order_book_per_min"] <= 13, stats
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# R4-3: the day-long block on entries into a watched order's contract is said in the alerts, and the
# operator can lift it (unwatch) — refused while the order is working or unconfirmed at the broker.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_r4_3_the_contract_block_is_alerted_and_the_operator_can_lift_it(settings, clock):
    fake = Backlog(clock, delay=10_000_000.0)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake)
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        fake.lose_next = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        lost = e._exit_resting(pos.id).bo
        out = await e.release_live_order(lost.client_order_id)
        assert "blocked for the rest of the day" in out.get("note", "")
        assert any("entries into" in a and "blocked for the rest of the day" in a for a in sent), sent
        e.positions.pop(pos.id)
        e._resting.clear()
        clock[0] += 3 * 3600  # three hours later, same IST day
        from kotsin_nse.exec.gateway import Mode
        await e.set_mode(Mode.LIVE, armed_minutes=60)
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(replace(_sig(clock), ts=_sig(clock).ts + 3 * 3600), None)
        assert out["FUDKII"]["decision"] == "REJECTED_HALT" and "watched" in out["FUDKII"]["reason"], out
        with pytest.raises(KeyError):
            await e.unwatch_live_order("no-such-order")
        done = await e.unwatch_live_order(lost.client_order_id)
        assert done["unwatched"] and lost.remote_id not in e.live_orders.watch
        assert any("no longer watched" in a for a in sent)
        out = await e._handle_signal(replace(_sig(clock), ts=_sig(clock).ts + 3 * 3600 + 1800), None)
        assert out["FUDKII"]["decision"] == "RESTING", out
        entry = next(iter(e._resting.values())).bo
        with pytest.raises(ValueError):  # working at the broker by the latest read: never "unwatched"
            await e.unwatch_live_order(entry.client_order_id)
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# OK: default off - an unconfirmed SELL is never resolved by itself; alerted at 30 s then every 60 s.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_default_off_never_resolves_and_alerts_every_minute(settings, clock):
    fake = Backlog(clock, delay=10_000.0)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake)
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        fake.lose_next = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        lost = e._exit_resting(pos.id).bo
        for _ in range(330):
            await _step(e, clock, 1, 13.0, 13.4)
        n = len([a for a in sent if "unconfirmed" in a and lost.client_order_id in a])
        assert lost.unconfirmed and not lost.never_placed and len(_sells(fake)) == 1
        assert 5 <= n <= 6, n
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# OK: auto_resolve on - a failed read in the streak resets it; resolution needs >= 120 s from the send.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_auto_resolve_streak_resets_on_a_failed_read(settings, clock):
    from kotsin_nse.venue.base import VenueError

    class Flaky(Backlog):
        fail_every = 3
        n = 0

        async def order_book(self):
            self.n += 1
            if self.n % self.fail_every == 0:
                raise VenueError("V4/OrderBook: 502", maybe_sent=True)
            return await super().order_book()

    fake = Flaky(clock, delay=10_000.0)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake, live_auto_resolve_unconfirmed=True)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        fake.lose_next = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        lost = e._exit_resting(pos.id).bo
        for _ in range(200):  # every third order-book read fails: never three good ones running
            await _step(e, clock, 1, 13.0, 13.4)
        assert not lost.never_placed and len(_sells(fake)) == 1
    finally:
        await e.stop()
