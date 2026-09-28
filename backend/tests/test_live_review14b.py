"""The round-2 review's probes (review14b N1-N8 and the also-fix list), turned round: every
``test_bug_*`` asserted a defect — each one here asserts the correct behaviour, and fails on the code
the review read. The ``test_ok_*`` checks that the round-1 fixes hold are kept as they were. Fake
broker only - no network, no money."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from kotsin_nse.domain import ExitDecision, ExitReason
from kotsin_nse.engine import Engine, _position_json
from kotsin_nse.exec.gateway import Mode

from .fake_broker import FakeBroker
from .test_limit_orders import OPT, _book, _sig
from .test_live_orders import LOT, _calls, _held, _hold, _intent, _live, _step


@pytest.fixture
def clock(monkeypatch):
    from datetime import datetime
    from datetime import time as dtime

    from kotsin_nse.market.session import IST, ist_today

    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


async def _quiesce(e: Engine) -> None:
    q = getattr(e, "_live_quiesce", None)  # the live half runs in the background
    if q is not None:
        await q()
    elif e._live_phase is not None:
        await e._live_phase


def _alerts(e: Engine) -> list[str]:
    sent: list[str] = []
    e.telegram.fire_and_forget = lambda text, *, key=None: sent.append(text)  # type: ignore[method-assign]
    return sent

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


class Yielding(FakeBroker):
    """Every call yields to the loop ``n`` times first (await points for a racing task)."""

    n = 1

    async def _y(self):
        for _ in range(self.n):
            await asyncio.sleep(0)

    async def place_order_raw(self, *a, **k):
        await self._y()
        return await super().place_order_raw(*a, **k)

    async def cancel_order(self, exch_order_id):
        await self._y()
        return await super().cancel_order(exch_order_id)

    async def order_status_many(self, orders):
        await self._y()
        return await super().order_status_many(orders)

    async def order_book(self):
        await self._y()
        return await super().order_book()

    async def modify_order(self, *a, **k):
        await self._y()
        return await super().modify_order(*a, **k)


class LateAnswer(FakeBroker):
    """A placement reaches the broker at once; its ANSWER comes back only after ``delay`` s."""

    delay = 0.2
    late = True

    async def place_order_raw(self, *a, **k):
        resp = await super().place_order_raw(*a, **k)
        if self.late:
            await asyncio.sleep(self.delay)
        return resp


class NeverArrives(FakeBroker):
    """A placement that never reaches the broker and whose call hangs (e.g. queued behind a re-login)."""

    hang = False

    async def place_order_raw(self, *a, **k):
        if self.hang:
            self.calls.append(("place-lost", k.get("remote_order_id")))
            await asyncio.sleep(3600)
        return await super().place_order_raw(*a, **k)



# ---------------------------------------------------------------------------------------------------
# A3 re-check: the operator's SKIP injected at every await point of the exit loop's live half.
# ---------------------------------------------------------------------------------------------------


async def _skip_sweep(settings, clock, scenario: str, k: int, tmp_path) -> tuple[int, int, list]:
    fake = Yielding()
    d = tmp_path / f"{scenario}-{k}"
    d.mkdir()
    settings = settings.model_copy(update={"data_dir": d, "db_url": f"sqlite+aiosqlite:///{d}/t.db"})
    e, fake = await _live(settings, clock, fake)
    try:
        if scenario in ("stop_with_target", "target_fill_next_rung", "first_target"):
            pos = await _hold(e, _held("FUDKII", clock[0], lots=4, targets=(19.0, 21.0)))
        else:
            pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        await e.ledger.upsert_position(_position_json(pos))
        if scenario == "stop_with_target":
            await _step(e, clock, 1, 17.40, 17.60)  # the T1 sell rests
            assert e._target_resting(pos.id) is not None
            clock[0] += 1
            _book(e, 14.60, 15.00, clock[0])  # through the 15.00 stop
        elif scenario == "target_fill_next_rung":
            await _step(e, clock, 1, 17.40, 17.60)
            fake.fill(e._target_resting(pos.id).bo.remote_id, LOT, 19.0)
            clock[0] += 1
            _book(e, 18.90, 19.10, clock[0])
        elif scenario == "first_target":
            clock[0] += 1
            _book(e, 17.40, 17.60, clock[0])
        elif scenario == "cross":
            _book(e, 14.60, 15.00, clock[0])
            await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
            clock[0] += 16
            _book(e, 14.60, 15.00, clock[0])
        e.ltps["3499"] = 187.5
        tick = asyncio.create_task(e._live_phase_body(clock[0]))
        for _ in range(k):
            await asyncio.sleep(0)
        skip = ExitDecision(pos.id, ExitReason.MANUAL, 15.0, pos.qty_remaining, "operator skip")
        await e._exit(pos, skip, clock[0])
        await tick
        for _ in range(3):  # later ticks, SKIP pending or placed
            await _step(e, clock, 1, 14.60 if scenario in ("stop_with_target", "cross") else 17.40,
                        15.00 if scenario in ("stop_with_target", "cross") else 17.60, manage=False)
            await e._live_phase_body(clock[0])
        peak = fake.peak_sell_working.get(OPT.scrip_code, 0)
        # every working SELL fills at the exchange
        for o in _working_sells(fake):
            fake.fill(o["rid"], o["qty"] - o["traded"], 14.8)
        return peak, _broker_net(fake, OPT.scrip_code, 4 * LOT), [c for c in _sells(fake)]
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["stop_with_target", "target_fill_next_rung", "first_target", "cross"])
async def test_ok_a3_skip_at_every_await_point_never_two_sells(settings, clock, scenario, tmp_path):
    bad = []
    t0 = clock[0]
    for k in range(0, 40):
        clock[0] = t0
        peak, net, sells = await _skip_sweep(settings, clock, scenario, k, tmp_path)
        print(scenario, k, peak, net, [c[1].split("-")[5] for c in sells])
        if peak > 4 * LOT or net < 0:
            bad.append((k, peak, net, sells))
    assert not bad, bad


# ---------------------------------------------------------------------------------------------------
# A1 re-check: a cancel acknowledged with an interim state, then status calls failing, then the
# original fills - no cross while anything is unknown.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_a1_interim_cancel_then_status_errors_then_fill_no_second_sell(settings, clock):
    class Flaky(FakeBroker):
        failing = False

        async def cancel_order(self, exch_order_id):
            self.calls.append(("cancel", exch_order_id))
            o = self._by_exch(exch_order_id)
            if o["status"] not in FINAL:
                o["status"] = "Cancel Order Req Received"

        async def order_status_many(self, orders):
            if self.failing:
                from kotsin_nse.venue.base import VenueError
                raise VenueError("V2/OrderStatus: 502", maybe_sent=True)
            return await super().order_status_many(orders)

        async def order_book(self):
            if self.failing:
                from kotsin_nse.venue.base import VenueError
                raise VenueError("V4/OrderBook: 502", maybe_sent=True)
            return await super().order_book()

    e, fake = await _live(settings, clock, Flaky())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        await _step(e, clock, 16, 14.60, 15.00)  # deadline: cancel -> interim state
        fake.failing = True
        for _ in range(10):
            await _step(e, clock, 1, 14.60, 15.00)
        fake.fill(ex.bo.remote_id, 4 * LOT, 14.70)  # the exchange filled it after all
        fake.failing = False
        for _ in range(3):
            await _step(e, clock, 1, 14.60, 15.00)
        assert len(_sells(fake)) == 1 and fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT
        assert pos.status == "CLOSED" and _broker_net(fake, OPT.scrip_code, 4 * LOT) == 0
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N1: a placement whose call times out WITHOUT reaching the broker no longer blocks the position's
# SELLs for the day: absent from the order book after it was sent, it is taken as never placed.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new1_a_lost_placement_is_resolved_never_placed_and_the_stop_goes(settings, clock):
    # the automatic resolution is OFF by default (review14c, the lead's decision): this is the switch on
    e, fake = await _live(settings, clock, NeverArrives(), live_call_timeout_s=0.05, live_auto_resolve_unconfirmed=True)
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        fake.hang = True  # this one placement is lost (stuck behind a re-login, cancelled by the timeout)
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        fake.hang = False
        ex = e._exit_resting(pos.id)
        assert ex is not None and ex.bo.unconfirmed
        for _ in range(5):  # the option through its stop
            await _step(e, clock, 60, 13.00, 13.40)
        assert _working_sells(fake) and _working_sells(fake)[0]["qty"] == pos.qty_remaining, "the stop is at the broker now"
        assert ex.bo.never_placed and any("NEVER PLACED" in a for a in sent), sent
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_new1_the_session_is_got_before_the_send_timeout_starts(settings, clock):
    """A re-login slower than the send timeout is not a placement that may be working: it waits, then sends."""
    class SlowLogin(FakeBroker):
        """The session has expired: the first call re-logs in first (0.2 s — longer than the 0.05 s send timeout)."""

        ready = False

        async def _login(self):
            if not self.ready:
                await asyncio.sleep(0.2)
                self.ready = True

        async def ensure_session(self):
            await self._login()

        async def place_order_raw(self, *a, **k):
            await self._login()  # a call made without a session logs in first, inside the call
            return await super().place_order_raw(*a, **k)

    e, _fake = await _live(settings, clock, SlowLogin(), live_call_timeout_s=0.05)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        assert ex is not None and not ex.bo.unconfirmed and ex.bo.exch_order_id, "sent and answered, not 'unconfirmed'"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_new1_the_operator_releases_an_unconfirmed_order_and_a_late_appearance_is_taken_back(settings, clock):
    e, fake = await _live(settings, clock, NeverArrives(), live_call_timeout_s=0.05)
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        fake.hang = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        fake.hang = False
        lost = e._exit_resting(pos.id).bo
        with pytest.raises(KeyError):
            await e.release_live_order("no-such-order")
        out = await e.release_live_order(lost.client_order_id)
        assert out["released"] and lost.never_placed
        await _step(e, clock, 1, 14.60, 15.00)
        await _step(e, clock, 1, 14.60, 15.00)
        new = _working_sells(fake)
        assert len(new) == 1 and new[0]["rid"] != lost.remote_id, "the stop went again at once"
        # the lost order turns up at the broker after all: alerted, tracked again and cancelled
        fake.orders[lost.remote_id] = {"rid": lost.remote_id, "exch_id": "999", "code": OPT.scrip_code, "buy": False, "qty": 4 * LOT,
                                       "rate": 14.8, "traded": 0, "avg": 0.0, "status": "Pending", "seen_ltp": None, "target": False,
                                       "exch_known": True}
        for _ in range(3):
            await _step(e, clock, 6, 14.60, 15.00)
        assert any("IS at the broker" in a for a in sent), sent
        assert fake.orders[lost.remote_id]["status"] == "Cancelled", "the late one is asked off"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_new1_a_lost_entry_resolved_never_placed_frees_its_money_and_its_capped_slot(settings, clock):
    e, fake = await _live(settings, clock, NeverArrives(margin=80_000.0), live_call_timeout_s=0.05, live_auto_resolve_unconfirmed=True,
                          mode=Mode.LIVE_CAPPED,
                          live_segments="NSE_FO", live_max_qty_rupees=1_000_000.0, live_max_positions=1)
    try:
        _book(e, 16.95, 17.25, clock[0])
        fake.hang = True
        await e._handle_signal(_sig(clock), None)
        fake.hang = False
        r = next(iter(e._resting.values()))
        assert r.bo.unconfirmed and e._live_reserved and e.wallets["FUDKII"].deployed > 0
        for _ in range(8):  # 160 s: past the 120 s from its send, 3 running reads without it
            await _step(e, clock, 20, 16.95, 17.25, manage=False)
        assert not e._resting and not e._live_reserved and e.wallets["FUDKII"].deployed == 0.0, "the money and the slot are free"
        assert r.bo.never_placed and not e._live_entry_books
        assert e.gateway.rejects_by_book.get("FUDKII", 0) == 0, "never reached the broker: not the broker refusing"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N2: the KILL waits for a SELL whose placement is in flight, and cancels it; nothing rests after the
# square-off; an entry that fills after the KILL is killed, alerted and reported.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new2_kill_takes_off_a_target_sell_in_flight_and_nothing_rests_after_the_square_off(settings, clock):
    e, fake = await _live(settings, clock, LateAnswer())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 17.40, 17.60, clock[0])

        class Exec:
            async def square_off_all(self):
                fake.calls.append(("squareoff",))

        e.live_exec = Exec()  # type: ignore[assignment]
        placing = asyncio.create_task(e._ensure_resting_target(pos, clock[0]))  # the exit loop rests T1
        await asyncio.sleep(0.05)  # the SELL is at the broker; its answer is on the way
        out = await e.kill()
        await placing
        assert out["square_off_requested"] and ("squareoff",) in fake.calls
        for _ in range(10):  # the engine keeps ticking after the KILL
            await _step(e, clock, 1, 17.40, 17.60)
        assert not _working_sells(fake), "no SELL of the engine's works at the broker after the square-off"
        assert e._target_resting(pos.id) is None
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_new2_an_entry_that_fills_after_the_kill_is_killed_alerted_and_reported(settings, clock):
    e, fake = await _live(settings, clock)
    sent = _alerts(e)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        fake.hold_cancels = True  # the KILL's cancel of the entry does not land in time

        class Exec:
            async def square_off_all(self):
                fake.calls.append(("squareoff",))

        e.live_exec = Exec()  # type: ignore[assignment]
        await e.kill()
        fake.fill(r.bo.remote_id, r.intent.qty, 17.10)  # it filled after the square-off
        fake.release_cancels()
        await _step(e, clock, 1, 16.95, 17.25)
        pos = next(p for p in e.positions.values() if p.venue == "live")
        assert pos.exec_log.get("killed"), "killed at once: the engine sends no SELL of its own for it"
        assert e._kill_state["late_fills"] and e._kill_state["late_fills"][0]["order"] == r.intent.client_order_id
        assert any("AFTER the KILL" in a for a in sent), sent
        n = len(_sells(fake))
        await _step(e, clock, 1, 10.0, 10.4, und=180.0)
        assert len(_sells(fake)) == n
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N3: the KILL flag clears on resume; killed positions close once the broker shows them flat, or by
# the operator.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new3_after_a_kill_and_resume_a_new_live_position_has_its_exits(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        class Exec:
            async def square_off_all(self):
                fake.calls.append(("squareoff",))

        e.live_exec = Exec()  # type: ignore[assignment]
        await e.kill()
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(_sig(clock), None)
        assert out["FUDKII"]["decision"] in ("REJECTED_HALT", "ENGINE_HALTED"), out
        await e.set_halt(False, "resumed")
        await e.set_mode(Mode.LIVE, armed_minutes=60)
        assert not e.halted()[0]
        out = await e._handle_signal(replace(_sig(clock), ts=_sig(clock).ts + 1800), None)
        assert out["FUDKII"]["decision"] == "RESTING", out
        r = next(iter(e._resting.values()))
        fake.fill(r.bo.remote_id, r.intent.qty, 17.10)
        await _step(e, clock, 1, 16.95, 17.25)
        assert any(p.venue == "live" and p.strategy == "FUDKII" and p.status == "OPEN" for p in e.positions.values())
        n = len(_sells(fake))
        for _ in range(3):  # through its stop
            await _step(e, clock, 20, 10.00, 10.40, und=180.0)
        assert len(_sells(fake)) > n, "the position bought after the resume sells at its stop"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_new3_a_killed_position_closes_when_the_broker_shows_it_flat_or_by_the_operator(settings, clock):
    from kotsin_nse.exec.reconcile import Reconciler

    e, fake = await _live(settings, clock)
    sent = _alerts(e)
    try:
        a = await _hold(e, _held("FUDKII", clock[0], targets=()))
        b = await _hold(e, _held("FUDKII_RT_X", clock[0], targets=(), other=True))

        class Exec:
            async def square_off_all(self):
                fake.calls.append(("squareoff",))

        e.live_exec = Exec()  # type: ignore[assignment]
        await e.kill()
        e.position_marks[a.id] = {"mid": 14.2}
        clock[0] += 5  # the broker read below is taken after the KILL (only such a read can close a killed position)

        class Net:  # the square-off went through for A's contract; B's still shows
            async def net_positions(self):
                return [{"scrip_code": b.instrument.scrip_code, "net_qty": 4 * LOT, "symbol": "INFY"}]

        e.reconciler_positions = Reconciler(Net())  # type: ignore[arg-type]
        await e.reconcile_now()
        assert a.status == "CLOSED" and a.exit_price == 14.2 and b.status == "OPEN"
        assert a.exec_log["exits"][-1]["priceProvisional"] is True and any("PROVISIONAL" in m for m in sent)
        with pytest.raises(KeyError):
            await e.close_killed_position("p-nope")
        out = await e.close_killed_position(b.id, 13.9)
        assert out["closed"] and b.status == "CLOSED" and b.exit_price == 13.9
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N4: a status call slower than the tick budget is still learned: the live half is its own task and
# its refresh is never cut by the tick.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new4_a_status_slower_than_the_tick_budget_is_learned(settings, clock):
    class Slow(FakeBroker):
        slow = False

        async def order_status_many(self, orders):
            if self.slow:
                await asyncio.sleep(0.12)
            return await super().order_status_many(orders)

    e, fake = await _live(settings, clock, Slow(), live_tick_budget_s=0.05, live_call_timeout_s=1.0)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await e._manage_positions()
        clock[0] += 1
        _book(e, 17.40, 17.60, clock[0])
        await e._manage_positions()
        await _quiesce(e)
        tgt = e._target_resting(pos.id)
        assert tgt is not None
        fake.slow = True  # the broker answers status in 0.12 s: slower than the 0.05 s budget, inside the 1 s timeout
        fake.fill(tgt.bo.remote_id, LOT, 19.0)  # a spike through T1, and back
        for _ in range(4):  # a tick a (compressed) second: the broker's answers arrive between them
            clock[0] += 1
            _book(e, 17.40, 17.60, clock[0])
            await e._manage_positions()
            await asyncio.sleep(0.15)
        await _quiesce(e)
        assert tgt.bo.filled_qty == LOT and pos.targets_hit == 1 and pos.qty_remaining == 3 * LOT, "the T1 fill is learned and booked"

        class Net:
            async def net_positions(self):
                return [{"scrip_code": OPT.scrip_code, "net_qty": _broker_net(fake, OPT.scrip_code, 4 * LOT), "symbol": "TATASTEEL"}]

        from kotsin_nse.exec.reconcile import Reconciler
        e.reconciler_positions = Reconciler(Net())  # type: ignore[arg-type]
        rep = await e.reconcile_now()
        assert not rep["frozen"], rep
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N5: a placement cancelled mid-request is recorded (unconfirmed, in the store) before the task goes,
# and the next pass adopts it — never a second SELL.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new5_a_placement_cancelled_mid_request_is_recorded_and_adopted(settings, clock):
    e, fake = await _live(settings, clock, LateAnswer())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 14.60, 15.00, clock[0])
        t = asyncio.create_task(e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0]))
        await asyncio.sleep(0.05)  # the SELL is at the broker, its answer not yet back
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert len(_working_sells(fake)) == 1, "working at the broker"
        assert e.live_orders.orders, "recorded, not lost with the task"
        cid = next(iter(e.live_orders.orders))
        assert e.live_orders.orders[cid].unconfirmed and any(b.client_order_id == cid for b in e.live_orders.load_left_behind()), \
            "recorded — and in the store a restart reads"
        fake.late = False
        for _ in range(8):  # the option stays through its stop: the exit loop decides SL-OP again
            await _step(e, clock, 1, 14.60, 15.00)
        assert len(_working_sells(fake)) == 1 and sum(o["qty"] for o in _working_sells(fake)) == 4 * LOT, "one SELL, never two"
        assert e._exit_resting(pos.id) is not None and e._exit_resting(pos.id).bo.client_order_id == cid, "adopted as the exit"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# A2 re-check: a restart while the cross is working - the next boot adopts the cross, sends nothing
# else, and closes the position on its fill.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_a2_restart_mid_cross_adopts_the_cross_and_sends_no_second_sell(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        fake.fill(ex.bo.remote_id, LOT, 14.80)
        await _step(e, clock, 16, 14.60, 15.00)  # one lot sold; the deadline: cancel, cross the other three
        cross = e._exit_resting(pos.id)
        assert cross is not None and cross.cross_n == 1 and cross.intent.qty == 3 * LOT
    finally:
        await e.stop()
    n = len(_sells(fake))
    e2 = Engine(settings.model_copy(update={"paper_limit_orders": True}))
    await e2.start()
    try:
        e2.live_orders.rest = fake
        await e2.set_mode(Mode.LIVE, armed_minutes=60)
        await e2._settle_left_behind_live_orders()
        p2 = e2.positions[pos.id]
        assert p2.qty_remaining == 3 * LOT
        for _ in range(3):
            await _step(e2, clock, 1, 14.60, 15.00)
            if e2._live_phase is not None:
                await e2._live_phase
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT and len(_working_sells(fake)) == 1
        fake.fill(_working_sells(fake)[0]["rid"], _working_sells(fake)[0]["qty"], 14.55)
        for _ in range(3):
            await _step(e2, clock, 1, 14.60, 15.00)
            if e2._live_phase is not None:
                await e2._live_phase
        assert p2.status == "CLOSED" and _broker_net(fake, OPT.scrip_code, 4 * LOT) == 0, (p2.status, len(_sells(fake)) - n)
    finally:
        await e2.stop()


# ---------------------------------------------------------------------------------------------------
# A1/B6 re-check: the cancel at the exit deadline is refused every time - no cross, no second SELL.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_b6_cancel_refused_at_the_deadline_never_crosses(settings, clock):
    from kotsin_nse.venue.base import VenueError

    class Refuse(FakeBroker):
        async def cancel_order(self, exch_order_id):
            self.calls.append(("cancel", exch_order_id))
            raise VenueError("V1/CancelOrderRequest: order in process", raw={"Status": 1})

    e, fake = await _live(settings, clock, Refuse())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        for _ in range(40):
            await _step(e, clock, 1, 14.60, 15.00)
            if e._live_phase is not None:
                await e._live_phase
        assert len(_sells(fake)) == 1 and len(_calls(fake, "cancel")) >= 5
        assert "FUDKII" in e._live_blocked_books
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N6: the KILL's square-off goes after ONE round of cancels, bounded, whatever the broker does.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new6_kill_squares_off_after_one_bounded_round_of_cancels(settings, clock):
    class Hung(FakeBroker):
        hung = False

        async def cancel_order(self, exch_order_id):
            if self.hung:
                self.calls.append(("cancel-hung", exch_order_id))
                await asyncio.sleep(3600)
            return await super().cancel_order(exch_order_id)

        async def order_status_many(self, orders):
            if self.hung:
                await asyncio.sleep(3600)
            return await super().order_status_many(orders)

    e, fake = await _live(settings, clock, Hung(), live_kill_wait_s=0.05)
    try:
        for i in range(8):  # eight working live orders
            await e.live_orders.place(_intent(f"FII-RTX-260928-110000-{i:03d}-EN-TATASTEEL-190CE-L4"), limit=17.10,
                                      now=clock[0], kind="entry")
        fake.hung = True

        class Exec:
            async def square_off_all(self):
                fake.calls.append(("squareoff", time.monotonic()))

        e.live_exec = Exec()  # type: ignore[assignment]
        t0 = time.monotonic()
        out = await e.kill()
        took = time.monotonic() - t0
        hung_cancels = len([c for c in fake.calls if c[0] == "cancel-hung"])
        assert out["square_off_requested"] and took < 1.0, took
        assert hung_cancels == 8, "one round, all at once"
        assert len(out["cancels_unconfirmed"]) == 8, "reported: the follow-up keeps at them"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N7: a fill booked while the reconcile waits for the broker is counted once.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new7_a_fill_booked_during_the_reconcile_call_is_counted_once(settings, clock):
    from kotsin_nse.exec.reconcile import Reconciler

    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], lots=10, targets=(19.0, 21.0)))
        await e.ledger.upsert_position(_position_json(pos))
        await _step(e, clock, 1, 17.40, 17.60)
        tgt = e._target_resting(pos.id)
        fake.fill(tgt.bo.remote_id, LOT, 19.0)  # T1 part-filled: 1 lot of 4 sold, the order still working
        await _step(e, clock, 1, 18.80, 18.90)
        assert tgt.bo.filled_qty == LOT and pos.qty_remaining == 10 * LOT

        class Net:
            async def net_positions(self):
                # meanwhile the exit loop takes the target off (a stop, a SKIP, the ladder): its lot is booked
                await e._cancel_resting_target(pos, clock[0], "a stop")
                return [{"scrip_code": OPT.scrip_code, "net_qty": _broker_net(fake, OPT.scrip_code, 10 * LOT), "symbol": "TATASTEEL"}]

        e.reconciler_positions = Reconciler(Net())  # type: ignore[arg-type]
        rep = await e.reconcile_now()
        assert pos.qty_remaining == 9 * LOT and _broker_net(fake, OPT.scrip_code, 10 * LOT) == 9 * LOT, "the books agree"
        assert not rep["frozen"] and not rep["mismatches"], rep
        assert not e.halted()[0]
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# N8: a live position's stop no longer waits behind the other live orders' broker calls.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_new8_a_live_stop_does_not_wait_behind_other_orders_broker_calls(settings, clock, tmp_path):
    """Each live position has its own turn: the stop waits for the one shared status call at most,
    never for the other positions' reprices, cancels and supersedes (review14b N8)."""
    from .test_limit_orders import UND

    lat = 0.05

    class Latency(FakeBroker):
        on = False
        t0 = 0.0
        stop_at: float | None = None

        async def _w(self):
            if self.on:
                await asyncio.sleep(lat)

        async def place_order_raw(self, *a, **k):
            if self.on and k.get("remote_order_id", "").startswith("FII-P-") and self.stop_at is None:
                self.stop_at = time.monotonic() - self.t0  # the stop leaves for the broker
            await self._w()
            return await super().place_order_raw(*a, **k)

        async def order_status_many(self, orders):
            await self._w()
            return await super().order_status_many(orders)

        async def modify_order(self, *a, **k):
            await self._w()
            return await super().modify_order(*a, **k)

        async def cancel_order(self, x):
            await self._w()
            return await super().cancel_order(x)

    async def run(n_others: int) -> float:
        d = tmp_path / f"m{n_others}"
        d.mkdir()
        s = settings.model_copy(update={"data_dir": d, "db_url": f"sqlite+aiosqlite:///{d}/t.db"})
        e, fake = await _live(s, clock, Latency(), live_tick_budget_s=5.0)
        try:
            from kotsin_nse.exec.paper import BookSnapshot
            from kotsin_nse.instrument.select import Quote
            others = []
            for i in range(n_others):  # other live positions, each with an exit walking to the bid (reprice due)
                code = str(170000 + i)
                inst = replace(OPT, scrip_code=code)
                p = replace(_held("FUDKII_RT_X", clock[0], targets=()), id=f"o{i}", instrument=inst,
                            exec_log={"ref": f"FII-RTX-260928-105000-{i + 10:03d}"})
                await _hold(e, p)
                e.books[code] = BookSnapshot(code, bids=[(14.60, 50_000)], asks=[(15.00, 50_000)], ts=clock[0])
                e.quotes[code] = Quote(ltp=14.8, bid=14.60, ask=15.00, ts=clock[0])
                e.ltps[code] = 14.8
                await e._exit(p, ExitDecision(p.id, ExitReason.TRAIL, 14.8, p.qty, "trail"), clock[0])
                others.append(p)
            await _hold(e, _held("FUDKII", clock[0], targets=()))
            clock[0] += 6  # every exit is due a reprice
            for p in others:
                code = p.instrument.scrip_code
                e.books[code] = BookSnapshot(code, bids=[(14.40, 50_000)], asks=[(15.00, 50_000)], ts=clock[0])
                e.quotes[code] = Quote(ltp=14.7, bid=14.40, ask=15.00, ts=clock[0])
            _book(e, 14.60, 15.00, clock[0])  # FUDKII through its stop
            e.ltps[UND.scrip_code] = 187.5
            fake.on, fake.t0 = True, time.monotonic()
            await e._live_phase_body(clock[0])
            assert fake.stop_at is not None, "the stop was placed in this live half"
            return fake.stop_at
        finally:
            await e.stop()

    alone = await run(0)
    behind = await run(6)
    assert behind - alone < 2 * lat, f"the stop left {behind:.3f} s into the live half behind six others, {alone:.3f} s alone"


# ---------------------------------------------------------------------------------------------------
# Also fixed (review14b): the fill words, the cancel's pending quantity, modify's alert, the paper
# half never waiting, a failed live half logged.
# ---------------------------------------------------------------------------------------------------


def test_an_ambiguous_fill_word_without_a_traded_quantity_is_never_taken_as_the_whole_order(clock):
    from kotsin_nse.exec.live_orders import BrokerOrder, LiveOrderManager

    m = LiveOrderManager(FakeBroker())
    for word, known in (("Executed", False), ("Traded", False), ("Complete", False), ("Fully Executed", True)):
        bo = BrokerOrder(client_order_id="x", remote_id="x", exch="N", qty=4 * LOT, side="SELL", limit=14.8, placed_ts=clock[0])
        m._apply(bo, {"RemoteOrderID": "x", "Status": word}, clock[0])
        assert bo.terminal and bo.settled is known and bo.qty_unknown is not known, word
        assert bo.filled_qty == (4 * LOT if known else 0), word


def test_a_cancel_settles_on_its_word_and_a_readable_traded_qty_whatever_pending_says(clock):
    from kotsin_nse.exec.live_orders import BrokerOrder, LiveOrderManager

    m = LiveOrderManager(FakeBroker())
    bo = BrokerOrder(client_order_id="x", remote_id="x", exch="N", qty=4 * LOT, side="SELL", limit=14.8, placed_ts=clock[0])
    m._apply(bo, {"RemoteOrderID": "x", "Status": "Cancelled", "TradedQty": LOT, "PendingQty": 3 * LOT}, clock[0])
    assert bo.settled and bo.state == "cancelled" and bo.filled_qty == LOT
    bo2 = BrokerOrder(client_order_id="y", remote_id="y", exch="N", qty=4 * LOT, side="SELL", limit=14.8, placed_ts=clock[0])
    m._apply(bo2, {"RemoteOrderID": "y", "Status": "Cancelled", "PendingQty": 0}, clock[0])
    assert bo2.terminal and not bo2.settled, "no traded quantity: over, but never settled"


@pytest.mark.asyncio
async def test_a_modify_never_raises_a_spurious_unseen_alert(clock):
    from kotsin_nse.exec.live_orders import LiveOrderManager

    class NoBook(FakeBroker):
        async def order_book(self):
            raise RuntimeError("V4/OrderBook down")

    fake = NoBook()
    alerts: list[str] = []
    m = LiveOrderManager(fake)
    m.on_alert = lambda key, text: alerts.append(text)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    await m.refresh(clock[0] + 35)
    assert bo.last_seen_ts == clock[0] + 35
    fake.status_silent.add(bo.remote_id)  # this one answer is missing
    assert not await m.modify(bo, 17.3, clock[0] + 36)
    assert not alerts and bo.last_seen_ts == clock[0] + 35, "seen a second ago: no alert, and the time kept"


@pytest.mark.asyncio
async def test_the_paper_half_never_waits_on_the_live_half(settings, clock):
    class Hung(FakeBroker):
        hung = False

        async def order_status_many(self, orders):
            if self.hung:
                await asyncio.sleep(0.5)
            return await super().order_status_many(orders)

    e, fake = await _live(settings, clock, Hung())  # the default tick budget, 2 s
    try:
        await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)  # a live target is working
        fake.hung = True
        clock[0] += 1  # the next tick: its status poll is due
        t0 = time.monotonic()
        await e._manage_positions()
        assert time.monotonic() - t0 < 0.1, "the tick returned; the live half carries on by itself"
        await _quiesce(e)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_live_half_that_fails_after_its_budget_is_logged(settings, clock, monkeypatch):
    import kotsin_nse.engine as eng

    logged: list[str] = []
    real = eng.log.error
    monkeypatch.setattr(eng.log, "error", lambda event, **kw: logged.append(event) or real(event, **kw))
    e, _fake = await _live(settings, clock, live_tick_budget_s=0.01)
    try:
        await _hold(e, _held("FUDKII", clock[0]))

        async def boom(now):
            await asyncio.sleep(0.05)
            raise RuntimeError("a live half's fault")

        e._live_phase_body = boom  # type: ignore[method-assign]
        await e._manage_positions()
        await _quiesce(e)
        await asyncio.sleep(0)
        assert "live.task_failed" in logged, logged
    finally:
        await e.stop()
