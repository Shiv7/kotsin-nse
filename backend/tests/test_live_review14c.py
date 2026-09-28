"""The round-3 review's probes (review14c R3-1..R3-4, the lead's decision on unconfirmed orders, and
the real clock in the live turns), turned round: every ``test_bug_*`` asserted a defect — each one
here asserts the correct behaviour and fails on the code the review read. The ``test_ok_*`` checks
are kept as they were. Fake broker only - no network, no money."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from kotsin_nse.domain import ExitDecision, ExitReason
from kotsin_nse.engine import IN_TREND_BOOKS, Engine, _position_json
from kotsin_nse.exec.reconcile import Reconciler
from kotsin_nse.venue.base import VenueError

from .fake_broker import FakeBroker
from .test_limit_orders import OPT, _book, _sig
from .test_live_orders import LOT, _calls, _held, _hold, _live, _step

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


def _alerts(e: Engine) -> list[str]:
    sent: list[str] = []
    e.telegram.fire_and_forget = lambda text, *, key=None: sent.append(text)  # type: ignore[method-assign]
    return sent


async def _tick(e: Engine, clock, dt: float = 1.0) -> None:
    clock[0] += dt
    await e._manage_positions()
    await e._live_quiesce()


# ---------------------------------------------------------------------------------------------------
# R3-1: an unconfirmed SELL on a broker BACKLOG — never resolved on a guess.
# ---------------------------------------------------------------------------------------------------


class Backlog(FakeBroker):
    """The broker accepts the order but answers nothing (the answer is lost); the order only appears in
    its status and its book ``delay`` seconds (of the test clock) later."""

    def __init__(self, clock, delay: float):
        super().__init__()
        self._clock = clock
        self.delay = delay
        self.lose_next = False
        self.fill_on_arrival = True
        self.hidden: dict[str, float] = {}

    async def place_order_raw(self, *a, **k):
        resp = await super().place_order_raw(*a, **k)
        if self.lose_next:
            self.lose_next = False
            rid = k["remote_order_id"][:38]
            self.hidden[rid] = self._clock[0] + self.delay
            raise VenueError("V1/PlaceOrderRequest failed: ReadTimeout", maybe_sent=True)
        return resp

    def _visible(self, rid: str) -> bool:
        ok = self._clock[0] >= self.hidden.get(rid, 0.0)
        if ok and rid in self.hidden and self.fill_on_arrival:
            self.hidden.pop(rid)
            o = self.orders[rid]
            if o["status"] not in FINAL:
                self.fill(rid, o["qty"] - o["traded"], 14.60)  # it reaches the exchange and matches at once
        return ok

    async def order_status_many(self, orders):
        return [r for r in await super().order_status_many(orders) if self._visible(r["RemoteOrderID"])]

    async def order_book(self):
        return [r for r in await super().order_book() if self._visible(r["RemoteOrderID"])]


async def _backlog_run(settings, clock, delay: float, **update) -> tuple[int, FakeBroker, Engine, list[str], object]:
    fake = Backlog(clock, delay=delay)
    e, fake = await _live(settings, clock, fake, **update)
    sent = _alerts(e)
    pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
    await e.ledger.upsert_position(_position_json(pos))
    _book(e, 14.60, 15.00, clock[0])
    fake.lose_next = True
    await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
    first = e._exit_resting(pos.id)
    assert first is not None and first.bo.unconfirmed
    return fake, e, sent, pos, first


@pytest.mark.asyncio
@pytest.mark.parametrize("auto", [False, True])
async def test_bug_r3_1_a_backlogged_sell_is_never_resolved_on_a_guess_and_no_second_sell_goes(settings, clock, auto):
    fake, e, _sent, pos, first = await _backlog_run(settings, clock, 25.0, live_auto_resolve_unconfirmed=auto)
    try:
        worst = 0
        for _ in range(30):  # 30 s of ticks, the option through its stop
            clock[0] += 1
            _book(e, 14.60, 15.00, clock[0])
            fake._visible(first.bo.remote_id)  # the backlog clears on the broker's own clock
            held = _broker_net(fake, OPT.scrip_code, 4 * LOT)
            working = sum(o["qty"] - o["traded"] for o in _working_sells(fake) if fake._visible(o["rid"]))
            worst = max(worst, working - held)
            await e._manage_positions()
            await e._live_quiesce()
        assert worst <= 0, "never a SELL working for lots the account no longer holds"
        assert len(_sells(fake)) == 1 and not first.bo.never_placed
        assert pos.status == "CLOSED" and _broker_net(fake, OPT.scrip_code, 4 * LOT) == 0, "found at 25 s, filled, booked"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_r3_1_an_unconfirmed_order_stays_blocking_and_is_alerted_every_minute_by_default(settings, clock):
    fake, e, sent, pos, first = await _backlog_run(settings, clock, 10_000.0)
    try:
        n_status, n_book = len(_calls(fake, "status")), len(_calls(fake, "book"))
        for _ in range(200):
            _book(e, 14.60, 15.00, clock[0] + 1)
            await _tick(e, clock)
        assert first.bo.unconfirmed and not first.bo.never_placed and e._exit_resting(pos.id) is first, "never resolved by itself"
        assert len(_sells(fake)) == 1, "its position's every other SELL stays blocked"
        loud = [a for a in sent if a.startswith("🚨 unconfirmed")]
        assert len(loud) == 3, loud  # at 30 s, 90 s, 150 s
        assert f"POST /control/live-order/{first.bo.client_order_id}/release" in loud[0]
        assert len(_calls(fake, "status")) - n_status >= 190, "looked for every refresh by status (RemoteOrderID)"
        assert 38 <= len(_calls(fake, "book")) - n_book <= 42, "and in the whole order book every 5 s (review14d)"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_r3_1_with_auto_resolve_only_after_120_s_from_the_send_and_three_running_reads(settings, clock):
    fake, e, _sent, _pos, first = await _backlog_run(settings, clock, 10_000.0, live_auto_resolve_unconfirmed=True)

    async def until_book_read(n: int = 1) -> None:  # ticks until ``n`` more order-book reads (one every 5 s)
        goal = len(_calls(fake, "book")) + n
        while len(_calls(fake, "book")) < goal:
            _book(e, 14.60, 15.00, clock[0] + 1)
            await _tick(e, clock)

    try:
        sent_at = clock[0]
        for _ in range(119):
            _book(e, 14.60, 15.00, clock[0] + 1)
            await _tick(e, clock)
        assert first.bo.unconfirmed and first.bo.absent_reads >= 20, "119 s: twenty-odd reads, still not resolved"
        real_book = fake.order_book

        async def down():
            fake.calls.append(("book",))
            raise VenueError("V4/OrderBook: 502", maybe_sent=True)

        fake.order_book = down  # type: ignore[method-assign]
        await until_book_read()  # past 120 s — but this read failed: the streak starts again
        assert first.bo.unconfirmed and first.bo.absent_reads == 0
        fake.order_book = real_book  # type: ignore[method-assign]
        await until_book_read(2)
        assert first.bo.unconfirmed, "two running reads: not yet"
        await until_book_read()
        assert first.bo.never_placed, "past 120 s from the send AND three running reads without it"
        assert first.bo.sent_ts == sent_at, "the age counts from the real moment it was sent"
        _book(e, 14.60, 15.00, clock[0] + 1)
        await _tick(e, clock)
        assert len(_sells(fake)) == 2, "the stop goes again"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_r3_1_no_live_entry_into_a_contract_while_an_order_of_it_is_unconfirmed(settings, clock):
    fake = Backlog(clock, delay=10_000.0)
    e, fake = await _live(settings, clock, fake)
    try:
        _book(e, 16.95, 17.25, clock[0])
        fake.lose_next = True
        out = await e._handle_signal(_sig(clock), None)  # FUDKII's BUY: its answer lost
        assert out["FUDKII"]["decision"] == "RESTING" and next(iter(e._resting.values())).bo.unconfirmed
        out2 = await e._handle_signal(replace(_sig(clock), ts=_sig(clock).ts + 1), None, books=(IN_TREND_BOOKS[1],))
        assert out2["FUDKII_RT_X"]["decision"] == "REJECTED_HALT" and "unconfirmed or watched" in out2["FUDKII_RT_X"]["reason"], out2
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# R3-2: a killed position closes only on a broker read ASKED FOR after the kill.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_r3_2_a_killed_position_is_never_closed_on_a_stale_flat_read(settings, clock):
    e, _fake = await _live(settings, clock)
    try:
        class Net:
            fail = False

            async def net_positions(self):
                if self.fail:
                    raise VenueError("V2/NetPositionNetWise failed: ReadTimeout", maybe_sent=True)
                return []  # flat

        net = Net()
        e.reconciler_positions = Reconciler(net)  # type: ignore[arg-type]
        await e.reconcile_now()  # flat: the read taken before the trade
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        await e.ledger.upsert_position(_position_json(pos))

        class Exec:
            async def square_off_all(self):
                raise VenueError("SquareOffAll failed: ReadTimeout", maybe_sent=True)

        e.live_exec = Exec()  # type: ignore[assignment]
        net.fail = True  # the broker is degraded: the square-off and every reconcile read fail
        clock[0] += 5
        out = await e.kill()
        assert not out["square_off_requested"] and out["square_off_error"]
        clock[0] += 5
        rep = await e.reconcile_now()
        assert rep["error"], rep
        assert pos.status == "OPEN", "no read since the KILL: nothing says the broker is flat"
        net.fail = False
        clock[0] += 5
        await e.reconcile_now()  # a good read, asked for after the KILL, shows it flat
        assert pos.status == "CLOSED"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# R3-3: the live task table holds only the tasks at work.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_r3_3_finished_turn_tasks_are_dropped(settings, clock):
    e, _fake = await _live(settings, clock)
    try:
        for i in range(30):
            pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
            pos.id = f"p{i}"
            e.positions[pos.id] = pos
            e.positions.pop("p-FUDKII", None)
            await _step(e, clock, 1, 17.40, 17.60)
            pos.status = "CLOSED"
            e.positions.pop(pos.id, None)
        await asyncio.sleep(0)
        done = [k for k, t in e._live_tasks.items() if t.done()]
        assert not done, done
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# OK: KILL while a SELL placement is in flight LONGER than live_kill_wait_s - the square-off goes, and
# the follow-up takes the late SELL off as soon as it registers.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_kill_with_a_placement_slower_than_the_kill_wait(settings, clock):
    class LateAnswer(FakeBroker):
        delay = 1.6

        async def place_order_raw(self, *a, **k):
            resp = await super().place_order_raw(*a, **k)
            await asyncio.sleep(self.delay)
            return resp

    e, fake = await _live(settings, clock, LateAnswer())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 17.40, 17.60, clock[0])

        class Exec:
            async def square_off_all(self):
                fake.calls.append(("squareoff",))

        e.live_exec = Exec()  # type: ignore[assignment]
        placing = asyncio.create_task(e._ensure_resting_target(pos, clock[0]))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await e.kill()
        took = time.monotonic() - t0
        assert took < 2.5 and ("squareoff",) in fake.calls
        await placing
        await asyncio.sleep(2.5)  # the follow-up's next rounds
        print("kill took", round(took, 2), "working after follow-up:", [(o["rid"], o["status"]) for o in _working_sells(fake)])
        assert not _working_sells(fake)
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# OK: the stop fires while the target's cancel is refused (cancel VenueError) - never two SELLs.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_stop_while_the_target_cancel_is_refused(settings, clock):
    class Refuse(FakeBroker):
        refuse = False

        async def cancel_order(self, exch_order_id):
            if self.refuse:
                self.calls.append(("cancel-refused", exch_order_id))
                raise VenueError("V1/CancelOrderRequest: order in process")
            return await super().cancel_order(exch_order_id)

    e, fake = await _live(settings, clock, Refuse())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        assert e._target_resting(pos.id) is not None
        fake.refuse = True
        for _ in range(20):
            await _step(e, clock, 1, 14.60, 15.00)
        assert len(_sells(fake)) == 1 and fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT
        fake.refuse = False
        for _ in range(5):
            await _step(e, clock, 1, 14.60, 15.00)
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT and len(_working_sells(fake)) == 1
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# R3-4: the late-appearance watch is kept in the store, looked for every refresh with or without
# positions, for the rest of the IST day; a late appearance is cancelled at once; no entry meanwhile.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_r3_4_a_late_sell_after_the_position_closed_is_seen_and_cancelled_at_once(settings, clock):
    fake = Backlog(clock, delay=400.0)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake)
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 14.60, 15.00, clock[0])
        fake.lose_next = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        first = e._exit_resting(pos.id)
        for _ in range(40):
            await _step(e, clock, 1, 14.60, 15.00)
        assert first.bo.unconfirmed and len(_sells(fake)) == 1, "blocking: not resolved by itself"
        await e.release_live_order(first.bo.client_order_id)  # the operator checked the broker's book: not there
        await _step(e, clock, 1, 14.60, 15.00)
        await _step(e, clock, 1, 14.60, 15.00)
        second = e._exit_resting(pos.id)
        assert second is not None and second.bo is not first.bo
        fake.fill(second.bo.remote_id, pos.qty, 14.60)  # the replacement sells the 4 lots
        await _step(e, clock, 1, 14.60, 15.00)
        assert pos.status == "CLOSED" and not e.live_orders.orders and first.bo.remote_id in e.live_orders.watch
        assert e._live_work(), "the watch alone is live work"
        # the watch is in the store: a restart keeps looking
        data = json.loads((settings.data_dir / "live_orders.json").read_text())
        assert [w["client_order_id"] for w in data["watch"]] == [first.bo.client_order_id]
        # no new live entry into that contract while it is watched
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(replace(_sig(clock), ts=_sig(clock).ts + 3600), None)
        assert out["FUDKII"]["decision"] == "REJECTED_HALT" and "unconfirmed or watched" in out["FUDKII"]["reason"], out
        n_book = len(_calls(fake, "book"))
        for _ in range(400):  # at 400 s the first SELL reaches the exchange and rests there
            clock[0] += 1
            fake._visible(first.bo.remote_id)
            await e._manage_positions()
            await e._live_quiesce()
            if not _working_sells(fake) and first.bo.client_order_id not in e.live_orders.watch and fake.orders[first.bo.remote_id]["status"] == "Cancelled":
                break
        assert len(_calls(fake, "book")) > n_book, "the order book is read again and again"
        assert fake.orders[first.bo.remote_id]["status"] == "Cancelled" and not _working_sells(fake), "seen, and cancelled at once"
        assert any("IS at the broker" in a for a in sent), sent
        assert _broker_net(fake, OPT.scrip_code, 4 * LOT) == 0, "never short"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_r3_4_the_watch_survives_a_restart_and_is_looked_for_with_no_position_at_all(settings, clock):
    fake = Backlog(clock, delay=10_000.0)
    fake.fill_on_arrival = False
    e, fake = await _live(settings, clock, fake)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        fake.lose_next = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        lost = e._exit_resting(pos.id).bo
        await e.release_live_order(lost.client_order_id)
        e.positions.pop(pos.id)  # the position is gone (closed another way)
    finally:
        await e.stop()
    e2 = Engine(settings.model_copy(update={"paper_limit_orders": True}))
    await e2.start()
    try:
        e2.live_orders.rest = fake
        await e2._settle_left_behind_live_orders()
        assert lost.remote_id in e2.live_orders.watch, "restored from the store"
        n = len(_calls(fake, "status"))
        await _tick(e2, clock)
        await _tick(e2, clock)
        assert len(_calls(fake, "status")) >= n + 2, "looked for every refresh, with no position and no other order"
        fake.hidden.clear()  # it turns up
        await _tick(e2, clock)
        await _tick(e2, clock)
        assert fake.orders[lost.remote_id]["status"] == "Cancelled"
    finally:
        await e2.stop()


# ---------------------------------------------------------------------------------------------------
# The real clock in the live turns: a turn that waited (a lock, a slow broker) judges deadlines and
# stamps orders on the time as it is, never the tick's that started it.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_turn_judges_its_deadline_on_the_real_clock_not_the_ticks(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        t0 = clock[0]
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), t0 - 100)  # a stale tick's time
        r = e._exit_resting(pos.id)
        assert r.placed_ts == t0, "stamped with the real moment it was sent"
        clock[0] = t0 + 16  # the turn ran 16 s after its tick began (it waited on the lock)
        await e._advance_one(r, t0)  # the tick's own, stale, time
        assert _calls(fake, "cancel"), "15 s passed on the real clock: the deadline's cancel went"
    finally:
        await e.stop()
