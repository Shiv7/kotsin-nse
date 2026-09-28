"""The phase-14 review's probes (review14 A1-A12, B1-B11), turned round: each one asserted the defect,
each one here asserts the correct behaviour — and fails on the code the review read. Fake broker only:
no network, no money."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from kotsin_nse.domain import ExitDecision, ExitReason, OrderSide, Purpose
from kotsin_nse.engine import IN_TREND_BOOKS, Engine, _position_json
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.reconcile import Reconciler
from kotsin_nse.venue.base import VenueError

from .fake_broker import FakeBroker
from .test_limit_orders import OPT, _book, _sig
from .test_live_orders import LOT, OPT2, UND2, _calls, _held, _hold, _intent, _live, _step


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


def _broker_net(fake: FakeBroker, code: str, held_before: int) -> int:
    """What the account holds in ``code`` after the fake's traded orders: held_before + buys - sells."""
    net = held_before
    for o in fake.orders.values():
        if o["code"] != code:
            continue
        net += o["traded"] if o["buy"] else -o["traded"]
    return net


def _working_sells(fake: FakeBroker, code: str) -> list[dict]:
    return [o for o in fake.orders.values() if not o["buy"] and o["code"] == code and o["status"] not in FakeBroker.FINAL]


def _alerts(e: Engine) -> list[str]:
    sent: list[str] = []
    e.telegram.fire_and_forget = lambda text, *, key=None: sent.append(text)  # type: ignore[method-assign]
    return sent


def _paper_book(e: Engine, bid: float, ask: float, now: float) -> None:
    from kotsin_nse.exec.paper import BookSnapshot
    from kotsin_nse.instrument.select import Quote

    e.books[OPT2.scrip_code] = BookSnapshot(OPT2.scrip_code, bids=[(bid, 50_000)], asks=[(ask, 50_000)], ts=now)
    e.quotes[OPT2.scrip_code] = Quote(ltp=(bid + ask) / 2, bid=bid, ask=ask, ts=now)
    e.ltps[OPT2.scrip_code] = (bid + ask) / 2


# ---------------------------------------------------------------------------------------------------
# P1 (A1): an interim cancel status is an order still working — no cross until the broker says the
# order is over, in a word this engine knows, with nothing pending.
# ---------------------------------------------------------------------------------------------------


class InterimCancel(FakeBroker):
    """The broker acknowledges a cancel with an interim status (the exchange has not confirmed yet)."""

    interim = "Cancel Pending"

    async def cancel_order(self, exch_order_id: str) -> None:
        self.calls.append(("cancel", exch_order_id))
        o = self._by_exch(exch_order_id)
        if o is None:
            raise VenueError("no such order")
        if o["status"] not in ("Fully Executed", "Cancelled", "Rejected"):
            o["status"] = self.interim  # still live at the exchange


@pytest.mark.asyncio
@pytest.mark.parametrize("interim", ["Cancel Pending", "Cancel Order Req Received", "Cancellation Requested"])
async def test_p1_an_interim_cancel_status_is_still_working_and_nothing_is_crossed(settings, clock, interim):
    fake = InterimCancel()
    fake.interim = interim
    e, fake = await _live(settings, clock, fake)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        first = e._exit_resting(pos.id)
        assert first is not None
        await _step(e, clock, 16, 14.60, 15.00, manage=False)  # past the 15 s deadline: the cancel is asked
        for _ in range(5):
            await _step(e, clock, 1, 14.60, 15.00, manage=False)
        assert len(_sells(fake)) == 1, "no cross while the first SELL may still be working"
        assert fake.orders[first.bo.remote_id]["status"] == interim
        assert first.bo.client_order_id in e.live_orders.orders and e._exit_resting(pos.id) is first, "still tracked"
        # the exchange fills the original: the position closes, and the account is flat — never short
        fake.fill(first.bo.remote_id, pos.qty, 14.70)
        await _step(e, clock, 1, 14.60, 15.00, manage=False)
        assert pos.status == "CLOSED" and len(_sells(fake)) == 1
        assert _broker_net(fake, OPT.scrip_code, 4 * LOT) == 0
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_p1b_the_cross_goes_once_the_broker_confirms_the_cancel_with_nothing_pending(settings, clock):
    e, fake = await _live(settings, clock, InterimCancel())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        first = e._exit_resting(pos.id)
        fake.fill(first.bo.remote_id, LOT, 14.80)
        await _step(e, clock, 16, 14.60, 15.00, manage=False)
        assert len(_sells(fake)) == 1
        fake.orders[first.bo.remote_id]["status"] = "Cancelled"  # confirmed: PendingQty 0
        await _step(e, clock, 1, 14.60, 15.00, manage=False)
        cross = e._exit_resting(pos.id)
        assert cross is not None and cross.cross_n == 1 and cross.intent.qty == 3 * LOT, "the rest, crossed at the bid"
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_p13_a_partial_whose_traded_qty_is_unreadable_is_never_crossed(settings, clock):
    class OtherName(FakeBroker):
        def _row(self, o):
            row = super()._row(o)
            row["TradedQuantity"] = row.pop("TradedQty")  # a field name the manager does not know
            return row

    e, fake = await _live(settings, clock, OtherName())
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        fake.fill(ex.bo.remote_id, 3 * LOT, 14.80)  # "Partially Executed", 3 of 4
        await _step(e, clock, 1, 14.60, 15.00, manage=False)
        await _step(e, clock, 15, 14.60, 15.00, manage=False)  # deadline: cancel ("Cancelled", 3 traded — unreadable)
        for _ in range(3):
            await _step(e, clock, 1, 14.60, 15.00, manage=False)
        assert len(_sells(fake)) == 1, "no cross: how much sold is not known"
        assert ex.bo.client_order_id in e.live_orders.orders and not ex.bo.settled, "never forgotten"
        assert _broker_net(fake, OPT.scrip_code, 4 * LOT) == LOT, "the account is never short"
        assert any("traded quantity" in a for a in sent), sent
        # nothing else sells the position meanwhile (the exit loop's own stop included)
        await e._exit(pos, ExitDecision(pos.id, ExitReason.EOD, 14.6, pos.qty_remaining, "EOD"), clock[0])
        assert len(_sells(fake)) == 1
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P2 (A2, B8): the boot adopts a SELL a previous run left working, and keeps every order whose cancel
# is not confirmed — no second SELL for the same lots, no forgotten order.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_a_left_behind_sell_is_adopted_or_kept_until_confirmed_and_no_second_sell_goes(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        await e.ledger.upsert_position(_position_json(pos))
        # a SELL whose position no restart will know (closed in another run): only cancelling can end it
        gone = await e.live_orders.place(_intent("FII-P-260928-101500-004-SLE0-TATASTEEL-190CE-L4", side=OrderSide.SELL, qty=LOT,
                                                 purpose=Purpose.EXIT, pos="p-gone"), limit=14.8, now=clock[0], kind="exit")
    finally:
        await e.stop()
    fake.hold_cancels = True  # the broker is slow (>10 s) to confirm the boot's cancels
    e2 = Engine(settings.model_copy(update={"paper_limit_orders": True}))
    await e2.start()
    try:
        e2.live_orders.rest = fake
        await e2._settle_left_behind_live_orders()
        assert fake.orders[ex.bo.remote_id]["status"] == "Pending"
        assert ex.bo.client_order_id in e2.live_orders.orders and e2._exit_resting(pos.id) is not None, "adopted"
        assert gone.client_order_id in e2.live_orders.orders, "a cancel not yet confirmed: still tracked"
        assert {b.client_order_id for b in e2.live_orders.load_left_behind()} >= {ex.bo.client_order_id, gone.client_order_id}, \
            "a second restart knows them too"
        p2 = e2.positions[pos.id]
        _book(e2, 14.60, 15.00, clock[0])
        await e2._exit(p2, ExitDecision(p2.id, ExitReason.SL_OP, 14.8, p2.qty_remaining, "SL-OP"), clock[0])
        assert sum(o["qty"] for o in _working_sells(fake, OPT.scrip_code)) == p2.qty_remaining + LOT, "one SELL for the 4 lots"
        fake.release_cancels()
        clock[0] += 4
        await e2._advance_resting(clock[0])
        assert gone.client_order_id not in e2.live_orders.orders, "confirmed: now forgotten"
    finally:
        await e2.stop()


# ---------------------------------------------------------------------------------------------------
# P3/P10 (A3): the operator's SKIP against the exit loop — every live SELL of a position is
# cancel-then-place under one lock; never two working for the same lots.
# ---------------------------------------------------------------------------------------------------


class SlowBroker(FakeBroker):
    """Every call takes a moment, as the real network does."""

    delay = 0.01

    async def place_order_raw(self, *a, **k):
        await asyncio.sleep(self.delay)
        return await super().place_order_raw(*a, **k)

    async def cancel_order(self, exch_order_id):
        await asyncio.sleep(self.delay)
        return await super().cancel_order(exch_order_id)

    async def order_status_many(self, orders):
        await asyncio.sleep(self.delay)
        return await super().order_status_many(orders)


@pytest.mark.asyncio
async def test_p3_a_skip_while_a_target_sell_is_being_placed_takes_it_off_before_selling(settings, clock):
    e, fake = await _live(settings, clock, SlowBroker())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))  # 4 lots, T1 19.0 (1 lot)
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 17.40, 17.60, clock[0])
        t_target = asyncio.create_task(e._ensure_resting_target(pos, clock[0]))
        await asyncio.sleep(0)  # the exit loop is now waiting on the broker's answer
        skip = ExitDecision(pos.id, ExitReason.MANUAL, 17.5, pos.qty_remaining, "operator skip")
        await asyncio.gather(t_target, e._exit(pos, skip, clock[0]))
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT, "never more SELL working than lots held"
        assert e._target_resting(pos.id) is None and e._exit_resting(pos.id) is not None
        assert [o["qty"] for o in _working_sells(fake, OPT.scrip_code)] == [4 * LOT], "the SKIP alone"
        assert "MAN" in e._exit_resting(pos.id).intent.client_order_id
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_p3b_a_skip_while_the_cross_is_being_placed_sends_no_second_sell(settings, clock):
    e, fake = await _live(settings, clock, SlowBroker())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        clock[0] += 16
        _book(e, 14.60, 15.00, clock[0])
        n0 = len(_sells(fake))
        loop = asyncio.create_task(e._advance_resting(clock[0]))  # the deadline: cancel, then the cross
        skip = ExitDecision(pos.id, ExitReason.MANUAL, 14.8, pos.qty_remaining, "operator skip")
        for _ in range(200):  # the SKIP arrives while the loop is between the confirmed cancel and the cross
            await asyncio.sleep(0.001)
            if e._exit_resting(pos.id) is None and any(c[0] == "cancel" for c in fake.calls):
                break
        await asyncio.gather(loop, e._exit(pos, skip, clock[0]))
        new = _sells(fake)[n0:]
        assert len(new) == 1 and new[0][3] == pos.qty_remaining, new
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT
        assert len(_working_sells(fake, OPT.scrip_code)) == 1
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_p10_two_superseding_exits_at_once_never_have_two_sells_working(settings, clock):
    e, fake = await _live(settings, clock, SlowBroker())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.TRAIL, 14.8, pos.qty, "trail"), clock[0])  # 45 s deadline
        a = ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP")  # 15 s: more urgent
        b = ExitDecision(pos.id, ExitReason.MANUAL, 14.8, pos.qty, "operator skip")  # 10 s
        await asyncio.gather(e._exit(pos, a, clock[0]), e._exit(pos, b, clock[0]))
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT
        assert len(_working_sells(fake, OPT.scrip_code)) == 1
        # the SKIP that stood down is kept, and the next tick puts it in place of the stop's order
        await _step(e, clock, 1, 14.60, 15.00)
        assert "MAN" in e._exit_resting(pos.id).intent.client_order_id and pos.id not in e._pending_manual
        assert fake.peak_sell_working[OPT.scrip_code] <= 4 * LOT and len(_working_sells(fake, OPT.scrip_code)) == 1
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P4 (A6): a TARGET exit that partly fills and is then crossed takes its rung once.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p4_a_partly_filled_target_exit_that_crosses_takes_its_rung_once(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=(19.0, 21.0, 23.0)))
        _book(e, 18.90, 19.10, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.TARGET, 19.0, 2 * LOT, "T1"), clock[0])
        ex = e._exit_resting(pos.id)
        fake.fill(ex.bo.remote_id, LOT, 19.0)
        await _step(e, clock, 1, 18.90, 19.10, manage=False)
        assert pos.targets_hit == 1
        await _step(e, clock, 45, 18.90, 19.10, manage=False)  # the 45 s deadline: cancel, cross the other lot
        cross = e._exit_resting(pos.id)
        assert cross is not None and cross.cross_n == 1 and cross.intent.qty == LOT
        fake.fill(cross.bo.remote_id, LOT, 18.90)
        await _step(e, clock, 1, 18.90, 19.10, manage=False)
        assert pos.qty_remaining == 2 * LOT
        assert pos.targets_hit == 1, "ONE rung (T1) was sold: the ladder moves one rung"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P5 (A7): LIVE_CAPPED max_positions counts the book's working live entries.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p5_live_capped_max_positions_counts_the_books_working_entries(settings, clock):
    e, fake = await _live(settings, clock, mode=Mode.LIVE_CAPPED, live_segments="NSE_FO", live_max_qty_rupees=1_000_000.0,
                          live_max_positions=1, live_max_orders_per_day=6)
    try:
        e.underlyings["INFY"] = UND2
        from kotsin_nse.instrument.select import Selection as Sel

        async def select(underlying, sig, *, tape=True):
            return Sel(OPT2 if underlying.symbol == "INFY" else OPT, premium=17.10, reason="ok", spread_pct=1.0)

        e._select_instrument = select  # type: ignore[method-assign]
        _book(e, 16.95, 17.25, clock[0])
        _paper_book(e, 16.95, 17.25, clock[0])
        o1 = await e._handle_signal(_sig(clock), None, books=(IN_TREND_BOOKS[0],))
        o2 = await e._handle_signal(replace(_sig(clock), symbol="INFY"), None, books=(IN_TREND_BOOKS[0],))
        assert o1["FUDKII"]["decision"] == "RESTING", o1
        assert o2["FUDKII"]["decision"] == "REJECTED_CAP" and "1 positions open ≥ cap 1" in o2["FUDKII"]["reason"], o2
        assert len([c for c in _calls(fake, "place") if "-EN-" in c[1]]) == 1
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P6 (A10): a target sell the broker refuses backs off and is alerted — not re-sent every tick.
# ---------------------------------------------------------------------------------------------------


class RefuseTargets(FakeBroker):
    async def place_order_raw(self, instrument, side, qty, *, price=0.0, intraday=True, remote_order_id):
        if "-T" in remote_order_id and "V" in remote_order_id.split("-")[5]:
            self.calls.append(("place", remote_order_id, side.value, int(qty), float(price)))
            raise VenueError("V1/PlaceOrderRequest: price outside the day's band", raw={"Status": 1})
        return await super().place_order_raw(instrument, side, qty, price=price, intraday=intraday, remote_order_id=remote_order_id)


@pytest.mark.asyncio
async def test_p6_a_refused_target_backs_off_and_is_alerted(settings, clock):
    e, fake = await _live(settings, clock, RefuseTargets())
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        for _ in range(10):
            await _step(e, clock, 1, 17.40, 17.60)
        tries = [c for c in _calls(fake, "place") if "-T1V" in c[1]]
        assert len(tries) == 3, "refused at 1 s, again at 3 s and 7 s: 2, 4, 8 s apart — not every tick"
        assert pos.exec_log["tgtSeq"] == 3
        assert any("target sell refused 3×" in a for a in sent), sent
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P7 (A9): a partly filled resting target is no mismatch: the reconcile counts the broker's fills of
# the engine's working orders.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p7_a_partly_filled_target_does_not_freeze_the_engine(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], lots=10, targets=(19.0, 21.0)))
        await _step(e, clock, 1, 17.40, 17.60)
        tgt = e._target_resting(pos.id)
        assert tgt.intent.qty == 4 * LOT
        fake.fill(tgt.bo.remote_id, LOT, 19.0)  # one lot of four sold; the price falls back
        await _step(e, clock, 1, 18.80, 18.90)
        assert pos.qty_remaining == 10 * LOT and tgt.bo.filled_qty == LOT

        class Net:
            async def net_positions(self):
                return [{"scrip_code": OPT.scrip_code, "net_qty": _broker_net(fake, OPT.scrip_code, 10 * LOT), "symbol": "TATASTEEL"}]

        e.reconciler_positions = Reconciler(Net())  # type: ignore[arg-type]
        rep = await e.reconcile_now()
        assert not rep["frozen"] and not rep["mismatches"], rep
        assert not e.halted()[0]
        for _ in range(3):
            await _step(e, clock, 60, 18.80, 18.90)
            assert not (await e.reconcile_now())["frozen"]
        # a real difference is still caught: the broker shows a lot less than engine and orders account for

        class Off(Net):
            async def net_positions(self):
                rows = await super().net_positions()
                rows[0]["net_qty"] -= LOT
                return rows

        e.reconciler_positions = Reconciler(Off())  # type: ignore[arg-type]
        rep = await e.reconcile_now()
        assert rep["frozen"] and rep["mismatches"][0]["kind"] == "QTY_MISMATCH"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P8 (A4): the operator's SKIP is kept until an exit for it is working.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p8_a_skip_survives_a_target_cancel_that_confirms_a_tick_later(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        old = e._target_resting(pos.id)
        assert old is not None
        fake.hold_cancels = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.MANUAL, 17.5, pos.qty_remaining, "operator skip"), clock[0])
        assert e._exit_resting(pos.id) is None, "SKIP waits for the target's cancel"
        fake.release_cancels()
        for _ in range(3):
            await _step(e, clock, 1, 17.40, 17.60)
        ex = e._exit_resting(pos.id)
        assert ex is not None and "MAN" in ex.intent.client_order_id and ex.intent.qty == 4 * LOT, "the SKIP is working"
        assert e._target_resting(pos.id) is None and len([c for c in _sells(fake) if "-T1V" in c[1]]) == 1, "never re-placed"
        assert pos.id not in e._pending_manual
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P9 (A5, B7): a broker fault never stops a paper stop; a failed login is a VenueError; no leaked hold.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p9_a_transport_error_in_the_status_call_never_stops_a_paper_stop(settings, clock):
    import httpx

    class Broken(FakeBroker):
        broken = False

        async def order_status_many(self, orders):
            if self.broken:
                raise httpx.ConnectError("TOTPLogin: [Errno 8] nodename nor servname provided")
            return await super().order_status_many(orders)

    e, fake = await _live(settings, clock, Broken())
    try:
        live = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        assert e._target_resting(live.id) is not None  # a live order is working
        paper = await _hold(e, replace(_held("FUDKII", clock[0], venue="paper", other=True), id="p-paper"))
        fake.broken = True
        clock[0] += 2
        _book(e, 14.60, 15.00, clock[0])  # both options through their 15.00 stop
        _paper_book(e, 14.60, 15.00, clock[0])
        await e._manage_positions()  # raises nothing
        assert e._exit_resting(paper.id) is not None or paper.status == "CLOSED", "the PAPER position's stop ran"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_p9_control_without_the_error_the_paper_stop_runs(settings, clock):
    """The reviewer's control, unchanged: it passed before and passes now."""
    e, _fake = await _live(settings, clock)
    try:
        await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        paper = await _hold(e, replace(_held("FUDKII", clock[0], venue="paper", other=True), id="p-paper"))
        clock[0] += 2
        _book(e, 14.60, 15.00, clock[0])
        _paper_book(e, 14.60, 15.00, clock[0])
        await e._manage_positions()
        assert e._exit_resting(paper.id) is not None or paper.status == "CLOSED"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_p9b_a_failed_login_is_a_venue_error_nothing_sent_and_the_manager_never_raises(settings, clock):
    import httpx
    from pydantic import SecretStr

    from kotsin_nse.exec.live_orders import BrokerOrder, LiveOrderManager
    from kotsin_nse.venue.fivepaisa.auth import Authenticator
    from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided", request=request)

    s = settings.model_copy(update={"fp_client_code": "1", "fp_app_key": SecretStr("k"), "fp_encrypt_key": SecretStr("e"),
                                    "fp_user_id": SecretStr("u"), "fp_pin": SecretStr("1"),
                                    "fp_totp_secret": SecretStr("JBSWY3DPEHPK3PXP"), "fp_public_ip": "1.2.3.4"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = Authenticator(s, http)

        async def now_window():
            return 0

        auth._wait_for_fresh_window = now_window  # type: ignore[method-assign]
        rest = FivePaisaREST(s, http, auth)
        with pytest.raises(VenueError) as err:
            await rest.order_book()
        assert err.value.maybe_sent is False, "no session: nothing was sent"
        m = LiveOrderManager(rest)
        bo = BrokerOrder(client_order_id="FII-P-260928-110000-001-EN-X", remote_id="FII-P-260928-110000-001-EN-X", exch="N",
                         qty=LOT, side="BUY", limit=17.1, placed_ts=clock[0], exch_order_id="100")
        m.orders[bo.client_order_id] = bo
        await m.refresh(clock[0], [bo], force=True)  # recorded, not raised
        assert m.errors >= 1 and bo.state == "open"
        assert await m.cancel(bo, clock[0]) is False and bo.cancel_failures == 1
        with pytest.raises(VenueError):  # a placement that was never sent is refused, never tracked
            await m.place(_intent(), limit=17.1, now=clock[0])
        assert "FII-P-260928-110000-001-EN-TATASTEEL-190CE-L4" not in m.orders


@pytest.mark.asyncio
async def test_p9c_a_placement_that_never_connected_holds_no_margin(settings, clock):
    import httpx

    class DropOnce(FakeBroker):
        boom = True

        async def place_order_raw(self, *a, **k):
            if self.boom:
                self.boom = False
                raise httpx.ConnectError("TOTPLogin: connection refused")
            return await super().place_order_raw(*a, **k)

    e, _fake = await _live(settings, clock, DropOnce(margin=80_000.0))
    try:
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(_sig(clock), None)
        assert out["FUDKII"]["decision"] == "REJECTED_BROKER", out
        assert not e._live_reserved, "never sent: no hold"
        clock[0] += 3600
        _book(e, 16.95, 17.25, clock[0])
        out2 = await e._handle_signal(replace(_sig(clock), ts=_sig(clock).ts + 3600), None)
        assert out2["FUDKII"]["decision"] == "RESTING", out2
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P11 (A11): a restart after a booked exit slice books it once.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("store_lags", [False, True])
async def test_p11_a_restart_after_a_booked_exit_slice_books_it_once(settings, clock, store_lags):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        await e.ledger.upsert_position(_position_json(pos))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        fake.fill(ex.bo.remote_id, LOT, 14.80)
        await _step(e, clock, 1, 14.60, 15.00, manage=False)
        assert pos.qty_remaining == 3 * LOT and ex.bo.booked_qty == LOT
        stored = {b.client_order_id: b.booked_qty for b in e.live_orders.load_left_behind()}
        assert stored[ex.bo.client_order_id] == LOT, "the store says the slice is booked, at once"
    finally:
        await e.stop()  # a restart before the next status poll
    if store_lags:  # the crash came between the position's write and the store's: the position's record wins
        path = settings.data_dir / "live_orders.json"
        data = json.loads(path.read_text())
        for r in data["orders"] if isinstance(data, dict) else data:
            r["booked_qty"] = 0
        path.write_text(json.dumps(data))
    e2 = Engine(settings.model_copy(update={"paper_limit_orders": True}))
    await e2.start()
    try:
        e2.live_orders.rest = fake
        await e2._settle_left_behind_live_orders()
        await e2._advance_resting(clock[0] + 1)
        p2 = e2.positions[pos.id]
        assert _broker_net(fake, OPT.scrip_code, 4 * LOT) == 3 * LOT
        assert p2.qty_remaining == 3 * LOT, "the one-lot slice is booked once: the engine holds 3, as the broker does"
    finally:
        await e2.stop()


# ---------------------------------------------------------------------------------------------------
# P12 (A8): an entry refused before it is sent holds no margin against a sibling book.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p12_a_cap_refused_entry_holds_no_margin_against_the_next_book(settings, clock):
    e, fake = await _live(settings, clock, FakeBroker(margin=80_000.0), mode=Mode.LIVE_CAPPED, live_segments="NSE_FO",
                          live_max_qty_rupees=1_000_000.0, live_max_positions=1, live_max_orders_per_day=6)
    try:
        await _hold(e, _held("FUDKII", clock[0], other=True))  # FUDKII at its one-position cap
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(_sig(clock), None, books=(IN_TREND_BOOKS[0], IN_TREND_BOOKS[1]))
        assert out["FUDKII"]["decision"] == "REJECTED_CAP" and "positions open" in out["FUDKII"]["reason"], out
        assert out["FUDKII_RT_X"]["decision"] == "RESTING", out
        assert len([c for c in _calls(fake, "place") if "-EN-" in c[1]]) == 1
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# P14 (A12): the flat brokerage is paid once per ORDER, however many slices it fills in.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p14_a_sliced_order_pays_one_orders_charges(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        for _ in range(4):
            fake.fill(ex.bo.remote_id, LOT, 14.80)
            await _step(e, clock, 1, 14.60, 15.00, manage=False)
        assert pos.status == "CLOSED"
        one_order = e.costs.leg(OPT, OrderSide.SELL, 14.80, 4 * LOT)
        assert pos.charges == pytest.approx(one_order.total, abs=0.01), (pos.charges, one_order.total)
        row = next(o for o in await e.ledger.rows_between("orders", 0, clock[0] + 10) if o["client_order_id"] == ex.intent.client_order_id)
        assert row["charges"] == pytest.approx(one_order.total, abs=0.01), "the order's row carries the order's total"
    finally:
        await e.stop()


# ---------------------------------------------------------------------------------------------------
# B1-B11: the unconfirmed-but-plausible risks, each failing closed.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_a_modify_goes_only_on_a_status_read_in_the_same_step(clock):
    from kotsin_nse.exec.live_orders import LiveOrderManager

    class Flaky(FakeBroker):
        down = False

        async def order_status_many(self, orders):
            if self.down:
                raise VenueError("V2/OrderStatus: head status=9")
            return await super().order_status_many(orders)

    fake = Flaky()
    m = LiveOrderManager(fake)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    await m.refresh(clock[0] + 1)
    fake.down = True
    assert not await m.modify(bo, 17.30, clock[0] + 2), "no status answer in this step: nothing is modified"
    fake.down = False
    fake.fill(bo.remote_id, LOT, 17.10)  # a fill the last poll did not see
    assert not await m.modify(bo, 17.30, clock[0] + 3), "the same-step read found a fill: never modified"
    assert not _calls(fake, "modify") and bo.filled_qty == LOT


@pytest.mark.asyncio
async def test_b2_a_fill_without_an_average_price_is_booked_at_the_limit_flagged_provisional(settings, clock):
    class NoAvg(FakeBroker):
        def _row(self, o):
            row = super()._row(o)
            row.pop("AvgRate")
            return row

    e, fake = await _live(settings, clock, NoAvg())
    sent = _alerts(e)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        fake.fill(r.bo.remote_id, r.intent.qty, 17.05)  # the broker filled it better — and does not say at what
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        pos = next(iter(e.positions.values()))
        assert pos.entry == 17.10 and pos.exec_log["entry"]["broker"]["priceProvisional"] is True
        assert any("PROVISIONAL" in a for a in sent), sent
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_b3_no_record_found_from_the_order_calls_is_an_empty_answer_never_a_relogin(settings):
    from types import SimpleNamespace

    import httpx

    from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"head": {"status": "1", "statusDescription": "No record found."}, "body": {}})

    class Auth:
        invalidated = 0

        async def token(self):
            return SimpleNamespace(access_token="t")

        def invalidate(self, **_k):
            self.invalidated += 1
            return True

    auth = Auth()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rest = FivePaisaREST(settings.model_copy(update={"fp_client_code": "1"}), http, auth)  # type: ignore[arg-type]
        rest._head = lambda: {"key": "k"}  # type: ignore[method-assign]
        assert await rest.order_status_many([("N", "FII-P-1")]) == []
        assert await rest.order_book() == []
    assert auth.invalidated == 0, "the session was never dropped"


@pytest.mark.asyncio
async def test_b6_a_cancel_the_broker_keeps_refusing_is_alerted_and_stops_the_books_entries(settings, clock):
    class NoCancel(FakeBroker):
        refuse = True

        async def cancel_order(self, exch_order_id):
            if self.refuse:
                self.calls.append(("cancel", exch_order_id))
                raise VenueError("V1/CancelOrderRequest: order in an indeterminate state")
            return await super().cancel_order(exch_order_id)

    e, fake = await _live(settings, clock, NoCancel(), live_cancel_alert_after=3)
    sent = _alerts(e)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        await _step(e, clock, 61, 16.95, 17.25, manage=False)  # the 60 s miss: the cancel is refused
        for _ in range(3):
            await _step(e, clock, 3, 16.95, 17.25, manage=False)
        assert len(_calls(fake, "cancel")) >= 3 and e._resting.get(r.intent.client_order_id) is r, "never assumed off"
        assert "FUDKII" in e._live_blocked_books and any("cancels" in a for a in sent), sent
        e.underlyings["INFY"] = UND2
        from kotsin_nse.instrument.select import Selection as Sel

        async def select(underlying, sig, *, tape=True):
            return Sel(OPT2 if underlying.symbol == "INFY" else OPT, premium=17.10, reason="ok", spread_pct=1.0)

        e._select_instrument = select  # type: ignore[method-assign]
        _paper_book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(replace(_sig(clock), symbol="INFY"), None)
        assert out["FUDKII"]["decision"] == "REJECTED_HALT" and "live entries stopped" in out["FUDKII"]["reason"], out
        fake.refuse = False
        await _step(e, clock, 3, 16.95, 17.25, manage=False)  # the cancel goes through
        await _step(e, clock, 1, 16.95, 17.25, manage=False)  # and the broker shows it over
        assert not e._resting and "FUDKII" not in e._live_blocked_books, "the order is over: the book trades again"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_b7_a_broker_that_does_not_answer_never_holds_up_a_paper_stop(settings, clock):
    class Hung(FakeBroker):
        hung = False

        async def order_status_many(self, orders):
            if self.hung:
                await asyncio.sleep(0.5)
            return await super().order_status_many(orders)

    e, fake = await _live(settings, clock, Hung(), live_tick_budget_s=0.05)
    try:
        await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)  # a live target is working
        paper = await _hold(e, replace(_held("FUDKII", clock[0], venue="paper", other=True), id="p-paper"))
        fake.hung = True
        clock[0] += 2
        _book(e, 17.40, 17.60, clock[0])
        _paper_book(e, 14.60, 15.00, clock[0])
        t0 = time.monotonic()
        await e._manage_positions()
        assert time.monotonic() - t0 < 0.3, "the tick did not wait on the broker"
        assert e._exit_resting(paper.id) is not None or paper.status == "CLOSED", "the paper stop ran"
        t1 = time.monotonic()
        await e._manage_positions()  # the live half is still busy: this tick's paper half runs, its live half waits
        assert time.monotonic() - t1 < 0.1 and e._live_phase_overruns == 1
        await e._live_phase
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_b9_the_kill_switch_cancels_every_live_order_first_then_squares_off_and_sells_nothing_after(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        tgt = e._target_resting(pos.id)
        await e.live_orders.place(_intent("FII-RTX-260928-110000-009-EN-TATASTEEL-190CE-L4"), limit=17.10, now=clock[0], kind="entry")

        class Exec:
            async def square_off_all(self):
                fake.calls.append(("squareoff",))

        e.live_exec = Exec()  # type: ignore[assignment]
        out = await e.kill()
        kinds = [c[0] for c in fake.calls]
        assert kinds.index("squareoff") > max(i for i, k in enumerate(kinds) if k == "cancel"), "every cancel before the square-off"
        assert fake.orders[tgt.bo.remote_id]["status"] == "Cancelled" and out["square_off_requested"] and not out["cancels_unconfirmed"]
        n = len(_sells(fake))
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 15.0, pos.qty_remaining, "SL-OP"), clock[0])
        await _step(e, clock, 1, 17.40, 17.60)
        assert len(_sells(fake)) == n and e._target_resting(pos.id) is None, "the engine sells nothing of its own after a KILL"
        assert e.halted()[0]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_b10_the_notional_cap_is_judged_at_the_entrys_cap_price(settings, clock):
    e, fake = await _live(settings, clock, mode=Mode.LIVE_CAPPED, live_segments="NSE_FO", live_max_qty_rupees=69_000.0,
                          live_max_positions=5)
    try:
        _book(e, 16.95, 17.25, clock[0])  # 4 lots at 17.10 = ₹68,400; at the +3 % cap 17.60 = ₹70,400
        out = await e._handle_signal(_sig(clock), None)
        assert out["FUDKII"]["decision"] == "REJECTED_CAP" and "notional ₹70,400 > cap ₹69,000" in out["FUDKII"]["reason"], out
        assert not _calls(fake, "place")
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_b11_a_broker_fill_on_a_position_no_longer_open_is_alerted(settings, clock):
    e, fake = await _live(settings, clock)
    sent = _alerts(e)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        tgt = e._target_resting(pos.id)
        pos.status = "CLOSED"  # closed on the engine's side (by hand, say) while its target sell was working
        e.positions.pop(pos.id)
        fake.fill(tgt.bo.remote_id, LOT, 19.0)
        await _step(e, clock, 1, 18.90, 19.10)
        assert any("not booked" in a for a in sent), sent
    finally:
        await e.stop()
