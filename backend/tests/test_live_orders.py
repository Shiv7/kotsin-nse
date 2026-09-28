"""The live order manager (exec/live_orders.py) and the engine's live path, against a fake 5paisa
(tests/fake_broker.py) — no network, no money.

Operator, 2026-09-27: "execute: All live" — in LIVE / LIVE_CAPPED every FUDKII book sends REAL orders
under exactly the paper order rules: the entry a BUY LIMIT held 30 s, then following the mid under
the +3 % cap, cancelled at 60 s; the target sells resting at the broker; the exits walked from the mid
to the bid and crossed at their deadline. The broker's word is the fill: nothing is booked from the
local book for a live order."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.domain import (
    Direction,
    ExitDecision,
    ExitReason,
    OrderIntent,
    OrderSide,
    Position,
    PosSide,
    Purpose,
)
from kotsin_nse.engine import IN_TREND_BOOKS, Engine, _position_from_json, _position_json
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.live_orders import LiveOrderManager, normalise_status
from kotsin_nse.exec.resting import exit_limit
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.venue.base import VenueError

from .fake_broker import FakeBroker
from .test_limit_orders import OPT, UND, _book, _engine, _rows, _sig

LOT = OPT.lot_size
#: another name, for a position that must not meet the exposure cap of TATASTEEL
UND2 = replace(UND, scrip_code="1594", symbol="INFY", name="INFY", underlying="INFY")
OPT2 = replace(OPT, scrip_code="160001", symbol="INFY", name="INFY 29 SEP 2026 CE 1500.00", strike=1500.0, underlying="INFY")


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _intent(cid: str = "FII-P-260928-110000-001-EN-TATASTEEL-190CE-L4", *, side: OrderSide = OrderSide.BUY, qty: int = 4 * LOT,
            purpose: Purpose = Purpose.ENTRY, pos: str | None = None) -> OrderIntent:
    return OrderIntent(strategy="FUDKII", instrument=OPT, side=side, qty=qty, purpose=purpose, signal_id="s", client_order_id=cid,
                       position_id=pos, limit_price=17.10)


async def _live(settings, clock, fake: FakeBroker | None = None, *, mode: Mode = Mode.LIVE, **update) -> tuple[Engine, FakeBroker]:
    """An engine armed LIVE whose broker is the fake: orders, status, modify, cancel and margin."""
    e = await _engine(settings.model_copy(update=update) if update else settings, clock)
    fake = fake or FakeBroker()
    e.live_orders.rest = fake
    e.rest.margin = fake.margin  # type: ignore[method-assign]
    await e.set_mode(mode, armed_minutes=60)
    return e, fake


async def _step(e: Engine, clock, dt: float, bid: float, ask: float, *, und: float | None = 187.5, manage: bool = True) -> None:
    """The exit loop's tick, ``dt`` seconds on, against a fresh book."""
    clock[0] += dt
    _book(e, bid, ask, clock[0])
    if und is not None:
        e.ltps[UND.scrip_code] = und
    if manage:
        await e._manage_positions()
        quiesce = getattr(e, "_live_quiesce", None)  # the live half runs in the background: wait for it
        if quiesce is not None:
            await quiesce()
    else:
        await e._advance_resting(clock[0])


def _calls(fake: FakeBroker, kind: str) -> list[tuple]:
    return [c for c in fake.calls if c[0] == kind]


def _held(book: str, now: float, *, lots: int = 4, entry: float = 17.0, targets: tuple[float, ...] = (19.0, 21.0),
          venue: str = "live", option_sl: float = 15.0, other: bool = False) -> Position:
    inst, und = (OPT2, UND2) if other else (OPT, UND)
    return Position(id=f"p-{book}", strategy=book, instrument=inst, underlying=und, side=PosSide.LONG, qty=lots * LOT, entry=entry,
                    opened_ts=now - 600, signal_id=f"s-{book}", direction=Direction.BULLISH, equity_entry=187.0, equity_sl=185.0,
                    equity_targets=(190.0, 192.0), option_sl=option_sl, option_targets=targets,
                    option_t1=targets[0] if targets else 0.0, venue=venue,
                    exec_log={"ref": f"{ {'FUDKII': 'FII-P', 'FUDKII_RT_X': 'FII-RTX'}.get(book, 'FII-RTN') }-260928-105000-001"})


async def _hold(e: Engine, pos: Position) -> Position:
    e.positions[pos.id] = pos
    e.wallets[pos.strategy].commit(pos.entry * pos.qty, time.time())
    return pos


# -- the manager alone -----------------------------------------------------------------------------


def test_the_brokers_words_and_quantities_become_one_state():
    assert normalise_status("Pending", 0, 100, 100) == ("open", True)
    assert normalise_status("Partially Executed", 40, 100, 60) == ("partial", True)
    assert normalise_status("Pending", 40, 100, 60) == ("partial", True), "a traded quantity is a partial whatever the words"
    assert normalise_status("Fully Executed", 100, 100, 0) == ("filled", True)
    assert normalise_status("Fully Executed", None, 100) == ("filled", True), "over — the whole quantity is _apply's to judge"
    assert normalise_status("Cancelled", 100, 100, 0) == ("filled", True), "all of it traded: filled, whatever the words"
    assert normalise_status("Cancelled", 40, 100, 0) == ("cancelled", True), "a partial then cancelled: over, 40 filled"
    assert normalise_status("Cancelled", 40, 100, 60) == ("cancelled", True), "the unfilled rest may stay in PendingQty (review14b)"
    assert normalise_status("Rejected By 5P", 0, 100) == ("rejected", True)
    assert normalise_status("Expired", 0, 100, 0) == ("cancelled", True)
    # never over on a word that merely CONTAINS cancel / reject
    assert normalise_status("Cancel Pending", 0, 100, 100) == ("open", True)
    assert normalise_status("Cancel Order Req Received", 0, 100, 100) == ("open", False), "unknown: working, alerted"
    assert normalise_status("Rejection Pending", 0, 100, 100) == ("open", False)
    assert normalise_status("Executed", 40, 100, 60) == ("partial", False), "an executed word with part of it traded: not over"


@pytest.mark.asyncio
async def test_an_accepted_order_is_tracked_by_its_ids_and_filled_by_the_brokers_status(clock):
    fake = FakeBroker()
    m = LiveOrderManager(fake, poll_interval_s=1.0)
    bo = await m.place(_intent(), limit=17.10, now=clock[0], kind="entry")
    assert bo.remote_id == _intent().client_order_id[:38] and bo.exch_order_id and bo.state == "open"
    assert fake.calls[0] == ("place", _intent().client_order_id, "BUY", 4 * LOT, 17.10)
    fake.fill(bo.remote_id, LOT, 17.05)
    await m.refresh(clock[0] + 1)
    assert (bo.state, bo.filled_qty, bo.avg_price) == ("partial", LOT, 17.05)
    fake.fill(bo.remote_id, 3 * LOT, 17.10)
    await m.refresh(clock[0] + 1.5)
    assert bo.state == "partial", "not due yet: one status call a second"
    await m.refresh(clock[0] + 2)
    assert (bo.state, bo.filled_qty) == ("filled", 4 * LOT) and bo.avg_price == pytest.approx(17.0875, abs=0.01)
    assert m.stats()["working"] == 0


@pytest.mark.asyncio
async def test_a_refusal_at_placement_raises_and_nothing_is_tracked(clock):
    fake = FakeBroker()
    fake.reject_next_place = "RMS: margin exceeds"
    m = LiveOrderManager(fake)
    with pytest.raises(VenueError):
        await m.place(_intent(), limit=17.10, now=clock[0])
    assert not m.orders and m.rejected == 1


@pytest.mark.asyncio
async def test_a_placement_whose_answer_was_lost_is_tracked_and_searched_for_never_taken_as_refused(clock):
    class Lost(FakeBroker):
        drop = True

        async def place_order_raw(self, *a, **k):
            resp = await super().place_order_raw(*a, **k)
            if self.drop:
                raise VenueError("V1/PlaceOrderRequest: transport: read timeout", maybe_sent=True)
            return resp

    fake = Lost()
    alerts: list[str] = []
    m = LiveOrderManager(fake, book_fallback_s=0.0)
    m.on_alert = lambda key, text: alerts.append(text)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    assert bo.unconfirmed and not bo.exch_order_id and not bo.settled, "it may be at the broker: tracked, never dropped"
    await m.refresh(clock[0] + 1)
    assert not bo.unconfirmed and bo.exch_order_id and bo.state == "open", "the status call found it by its RemoteOrderID"
    # one that never got there: nothing ever answers for it. By default it is NEVER resolved by itself
    # (review14c, the lead's decision): it stays unconfirmed, blocking, alerted at 30 s and every 60 s after
    t0 = clock[0]
    ghost = await m.place(_intent("FII-P-260928-110000-002-EN-TATASTEEL-190CE-L4"), limit=17.10, now=clock[0])
    fake.orders.pop(ghost.remote_id)
    for t in (5, 10, 31, 60, 95, 400):
        clock[0] = t0 + t
        await m.refresh(clock[0])
    assert ghost.unconfirmed and not ghost.settled and ghost in m.working() and not ghost.never_placed
    loud = [a for a in alerts if "unconfirmed FII-P-260928-110000-002" in a]
    assert len(loud) == 3 and "POST /control/live-order/" in loud[0], "at 30 s, 90 s … and the window 390-450 s"
    # "no session" (nothing was sent) is a refusal, not an unknown
    fake.drop = False
    fake.reject_next_place = "no session"
    with pytest.raises(VenueError):
        await m.place(_intent("FII-P-260928-110000-003-EN-TATASTEEL-190CE-L4"), limit=17.10, now=clock[0])


@pytest.mark.asyncio
async def test_no_remote_id_echo_and_a_silent_status_are_answered_by_the_exchange_id_never_by_row_order(clock):
    fake = FakeBroker(echo_remote_id=False)
    m = LiveOrderManager(fake, book_fallback_s=5.0)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    assert bo.remote_id == _intent().client_order_id[:38], "no echo: the id the engine sent, cut to the broker's 38"
    await m.refresh(clock[0] + 1)
    assert bo.last_seen_ts == clock[0] + 1, "matched by the exchange order id the placement answered with"
    fake.status_silent.add(bo.remote_id)
    fake.fill(bo.remote_id, 4 * LOT, 17.10)
    await m.refresh(clock[0] + 2)
    assert bo.state == "filled" and bo.filled_qty == 4 * LOT and ("book",) in fake.calls, "the order book, by exchange id"


@pytest.mark.asyncio
async def test_a_status_row_that_names_no_order_is_never_matched_by_its_place_in_the_answer(clock):
    """review14 B5: a row with neither RemoteOrderID nor an exchange id cannot be told apart — taken by
    its position in the answer it could be another order's fill."""
    fake = FakeBroker(echo_remote_id=False, exch_id_at_place=False)
    alerts: list[str] = []
    m = LiveOrderManager(fake, book_fallback_s=5.0)
    m.on_alert = lambda key, text: alerts.append(text)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    fake.fill(bo.remote_id, 4 * LOT, 17.10)
    orig = fake._row

    def anonymous(o):  # a row that names no order: no RemoteOrderID, no exchange id
        row = orig(o)
        row["ExchOrderID"] = 0
        return row

    fake._row = anonymous  # type: ignore[method-assign]
    await m.refresh(clock[0] + 1)
    assert bo.state == "open" and bo.filled_qty == 0 and not bo.exch_order_id, "not matched: still working"
    assert not await m.modify(bo, 17.2, clock[0] + 1) and not await m.cancel(bo, clock[0] + 1), "no exchange id: nothing to act on"
    await m.refresh(clock[0] + 40, force=True)
    assert bo in m.working() and alerts and "stopped answering" in alerts[0]


@pytest.mark.asyncio
async def test_a_partly_filled_order_is_never_modified(clock):
    """Whether ModifyOrderRequest's Qty is the order's total or its remainder is not verified: read the
    other way it would resize the order, so a partly filled order keeps its price."""
    fake = FakeBroker()
    m = LiveOrderManager(fake)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    fake.fill(bo.remote_id, LOT, 17.10)
    await m.refresh(clock[0] + 1)
    assert not await m.modify(bo, 17.30, clock[0] + 2) and not _calls(fake, "modify")
    assert await m.cancel(bo, clock[0] + 3), "a cancel still goes"


@pytest.mark.asyncio
async def test_a_stale_status_never_takes_a_fill_back(clock):
    fake = FakeBroker()
    m = LiveOrderManager(fake)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    fake.stale.add(bo.remote_id)
    await m.refresh(clock[0] + 1)  # the frozen row: nothing traded
    fake.stale.discard(bo.remote_id)
    fake.fill(bo.remote_id, 2 * LOT, 17.10)
    await m.refresh(clock[0] + 2)
    assert bo.filled_qty == 2 * LOT
    fake.stale.add(bo.remote_id)  # the old row comes back
    await m.refresh(clock[0] + 3)
    assert bo.filled_qty == 2 * LOT, "a broker quantity never goes backwards"


@pytest.mark.asyncio
async def test_a_modify_the_broker_ignores_is_counted_and_the_order_keeps_its_real_price(clock):
    fake = FakeBroker()
    fake.ignore_modify = True
    m = LiveOrderManager(fake)
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    assert await m.modify(bo, 17.30, clock[0] + 1)
    await m.refresh(clock[0] + 2)
    assert bo.modify_asked == 17.30, "not shown yet: still waiting"
    await m.refresh(clock[0] + 7)
    assert bo.modify_asked is None and bo.modifies_ignored == 1 and bo.limit == 17.10, "the broker's rate is the order's"
    fake.ignore_modify = False
    assert await m.modify(bo, 17.30, clock[0] + 8)
    await m.refresh(clock[0] + 9)
    assert bo.limit == 17.30 and bo.modify_asked is None


@pytest.mark.asyncio
async def test_a_cancel_is_never_assumed_and_a_fill_before_it_stands(clock, tmp_path):
    fake = FakeBroker()
    m = LiveOrderManager(fake, store=tmp_path / "live_orders.json")
    bo = await m.place(_intent(), limit=17.10, now=clock[0])
    fake.fill(bo.remote_id, LOT, 17.10)
    assert await m.cancel(bo, clock[0] + 1)
    assert bo.state == "open", "asked, not assumed"
    await m.refresh(clock[0] + 2)
    assert (bo.state, bo.filled_qty) == ("cancelled", LOT)
    # the store is what a restart reads
    left = LiveOrderManager(fake, store=tmp_path / "live_orders.json").load_left_behind()
    assert [(b.client_order_id, b.state, b.filled_qty) for b in left] == [(bo.client_order_id, "cancelled", LOT)]


# -- the engine: entries -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_entry_holds_30_s_then_follows_the_mid_by_modify_and_is_cancelled_at_60_s(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        assert r.venue == "live" and r.limit == 17.10 and not e.positions
        assert [c[1:] for c in _calls(fake, "place")] == [(r.intent.client_order_id, "BUY", r.intent.qty, 17.10)]
        await _step(e, clock, 10, 17.30, 17.60, manage=False)  # the market left the signal price: held
        await _step(e, clock, 10, 17.30, 17.60, manage=False)
        assert not _calls(fake, "modify"), "held for 30 s at the signal price"
        await _step(e, clock, 11, 17.30, 17.60, manage=False)
        assert _calls(fake, "modify")[-1][2] == 17.45 and r.limit == 17.45, "after the hold: a MODIFY to the mid"
        await _step(e, clock, 5, 17.50, 17.90, manage=False)
        assert r.limit == pytest.approx(17.60), "never above the +3 % cap (17.10 → 17.60)"
        fake.hold_cancels = True  # the broker is slow to confirm
        await _step(e, clock, 25, 17.50, 17.90, manage=False)  # 61 s
        assert _calls(fake, "cancel") and r.bo.client_order_id in e.live_orders.orders
        assert e._resting.get(r.intent.client_order_id) is r, "asked to cancel — on the book until the broker confirms"
        await _step(e, clock, 1, 17.50, 17.90, manage=False)
        assert e._resting.get(r.intent.client_order_id) is r and len(_calls(fake, "cancel")) == 1, "asked again only after 3 s"
        await _step(e, clock, 3, 17.50, 17.90, manage=False)
        assert len(_calls(fake, "cancel")) == 2
        fake.release_cancels()
        await _step(e, clock, 1, 17.50, 17.90, manage=False)
        assert not e._resting and not e.positions
        s = (await _rows(e, "signals"))[0]
        assert s["decision"] == "LIMIT_UNFILLED" and "limit not filled in 60 s" in s["decision_reason"]
        assert e.wallets["FUDKII"].deployed == 0.0
        assert not e.live_orders.orders, "forgotten once over"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_live_entry_filled_by_the_broker_is_the_position_at_the_brokers_price(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        fake.fill(r.bo.remote_id, r.intent.qty, 17.05)  # the broker filled it better than the limit
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        pos = next(iter(e.positions.values()))
        assert pos.venue == "live" and pos.entry == 17.05 and pos.qty == r.intent.qty
        o = next(o for o in await _rows(e, "orders") if o["purpose"] == "ENTRY")
        assert o["decision"] == "SUBMITTED" and o["broker_order_id"] == r.bo.exch_order_id and o["mode"] == "LIVE"
        assert o["charges"] == pytest.approx(e.costs.leg(OPT, OrderSide.BUY, 17.05, pos.qty).total, abs=0.01)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_partial_entry_at_the_deadline_keeps_what_filled_and_cancels_the_rest(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        fake.fill(r.bo.remote_id, LOT, 17.10)
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        assert not e.positions, "a partial is not a position until the order is over"
        await _step(e, clock, 60, 16.95, 17.25, manage=False)  # the deadline: the rest is cancelled
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        pos = next(iter(e.positions.values()))
        assert pos.qty == LOT and pos.entry == 17.10, "one lot of four: the position is what the broker filled"
        x = pos.exec_log["entry"] if "entry" in pos.exec_log else next(o for o in await _rows(e, "orders") if o["purpose"] == "ENTRY")
        assert x is not None
        assert e.wallets["FUDKII"].deployed == pytest.approx(17.10 * LOT, abs=0.5), "the hold became the one lot's cost"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_broker_rejection_is_a_missed_entry_counted_toward_the_books_breaker(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        fake.reject_next_place = "RMS: exposure limit"
        await e._handle_signal(_sig(clock), None)
        assert not e._resting and not e.positions
        s = (await _rows(e, "signals"))[0]
        assert s["decision"] == "REJECTED_BROKER" and "RMS: exposure limit" in s["decision_reason"]
        assert e.gateway.rejects_by_book["FUDKII"] == 1
        # accepted, then rejected by status
        sig2 = replace(_sig(clock), ts=_sig(clock).ts + 1800)
        await e._handle_signal(sig2, None)
        r = next(iter(e._resting.values()))
        fake.reject(r.bo.remote_id, "RMS: margin exceeds")
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        assert not e._resting and not e.positions and e.gateway.rejects_by_book["FUDKII"] == 2
        assert e.wallets["FUDKII"].deployed == 0.0
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["halt", "breaker"])
async def test_a_halt_or_a_tripped_breaker_cancels_a_resting_live_entry_and_a_fill_before_the_cancel_is_kept(settings, clock, stop):
    e, fake = await _live(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        if stop == "halt":
            await e.set_halt(True, "operator")
        else:
            e.gateway.tripped_books.add("FUDKII")
        fake.fill(r.bo.remote_id, 2 * LOT, 17.10)  # it filled in the same second
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        assert _calls(fake, "cancel"), "a halt asks the broker to take the entry off"
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        pos = next(iter(e.positions.values()))
        assert pos.qty == 2 * LOT, "the lots the broker filled before the cancel are a position, never ignored"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_stop_breached_while_resting_cancels_the_live_entry(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        await _step(e, clock, 1, 16.95, 17.25, und=184.9, manage=False)  # through the 185.00 stop
        assert _calls(fake, "cancel")
        await _step(e, clock, 1, 16.95, 17.25, und=184.9, manage=False)
        assert not e._resting and not e.positions
        assert "stop" in (await _rows(e, "signals"))[0]["decision_reason"]
    finally:
        await e.stop()


# -- the engine: margin, caps, routing ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_entries_on_one_margin_snapshot_cannot_both_pass(settings, clock):
    """Two books entering the same 09:45 bar read one broker snapshot: the second sees the first's
    working entry held against it."""
    e, fake = await _live(settings, clock, FakeBroker(margin=80_000.0))
    try:
        _book(e, 16.95, 17.25, clock[0])  # four lots at 17.10 = ₹68,400 each
        out = await e._handle_signal(_sig(clock), None, books=(IN_TREND_BOOKS[0], IN_TREND_BOOKS[1]))
        assert len(_calls(fake, "place")) == 1
        refused = [v for v in out.values() if v["decision"] == "REJECTED_CAP"]
        # held at the most the working entry may pay — its +3 % cap, 17.60 × 4,000 (review14 B10)
        assert len(refused) == 1 and "less ₹70,400 held for working live entries" in refused[0]["reason"], out
        # the working entry ends (missed): its hold lapses at the next snapshot taken after it ended
        r = next(iter(e._resting.values()))
        fake.orders[r.bo.remote_id]["status"] = "Cancelled"
        await _step(e, clock, 1, 16.95, 17.25, manage=False)
        assert not e._resting
        clock[0] += 16  # the next broker snapshot
        sig2 = replace(_sig(clock), ts=_sig(clock).ts + 1800)
        out2 = await e._handle_signal(sig2, None, books=(IN_TREND_BOOKS[1],))
        assert out2["FUDKII_RT_X"]["decision"] == "RESTING", out2
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_live_capped_caps_are_each_books_own_and_count_its_entries_only(settings, clock):
    e, fake = await _live(settings, clock, mode=Mode.LIVE_CAPPED, live_segments="NSE_FO", live_max_qty_rupees=1_000_000.0,
                          live_max_positions=1, live_max_orders_per_day=6)
    try:
        await _hold(e, _held("FUDKII", clock[0], other=True))  # FUDKII holds one live position: at its cap
        await _hold(e, _held("FUDKII_RT_X", clock[0], venue="paper", other=True))  # a paper one is not at the broker
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(_sig(clock), None, books=(IN_TREND_BOOKS[0], IN_TREND_BOOKS[1]))
        placed = [c[1] for c in _calls(fake, "place") if "-EN-" in c[1]]
        assert len(placed) == 1 and placed[0].startswith("FII-RTX"), out
        assert out["FUDKII"]["decision"] == "REJECTED_CAP" and "1 positions open ≥ cap 1" in out["FUDKII"]["reason"], out
        assert e.gateway.live_orders_by_book == {"FUDKII_RT_X": 1}, "resting target sells are not entries"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_real_money_test_buys_one_lot_per_book_under_live_capped(settings, clock):
    e, fake = await _live(settings, clock, mode=Mode.LIVE_CAPPED, live_segments="NSE_FO", live_max_qty_rupees=25_000.0,
                          live_capped_lots=1)
    try:
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        assert {c[3] for c in _calls(fake, "place")} == {LOT} and len(_calls(fake, "place")) == 4, out
        assert all(r.ctx.outlay == pytest.approx(17.10 * LOT) for r in e._resting.values())
        assert e.wallets["FUDKII"].deployed == pytest.approx(17.10 * LOT), "the hold is the one lot's"
    finally:
        await e.stop()
    p = await _engine(settings.model_copy(update={"live_capped_lots": 1}), clock)
    try:
        _book(p, 16.95, 17.25, clock[0])
        await p._handle_signal(_sig(clock), None)
        assert next(iter(p._resting.values())).intent.qty == 4 * LOT, "paper keeps its own size"
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_a_paper_position_exits_on_paper_in_live_and_a_live_one_at_the_broker_after_the_arm_expires(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        paper = await _hold(e, _held("FUDKII_RT_X", clock[0], venue="paper"))
        _book(e, 16.95, 17.05, clock[0])
        await e._exit(paper, ExitDecision(paper.id, ExitReason.SL_EQ, 17.0, paper.qty, "SL-EQ"), clock[0])
        r = e._exit_resting(paper.id)
        assert r is not None and r.venue == "paper", "a paper limit, walked the paper way"
        await _step(e, clock, 16, 16.95, 17.05, manage=False)  # its 15 s: crossed on paper
        assert paper.status == "CLOSED" and not fake.calls, "its lots were never bought at the broker"
        live = await _hold(e, _held("FUDKII", clock[0]))
        clock[0] += 3601  # the arm expired: the mode reads PAPER
        assert e.mode() is Mode.PAPER
        _book(e, 16.95, 17.05, clock[0])
        await e._exit(live, ExitDecision(live.id, ExitReason.SL_EQ, 17.0, live.qty, "SL-EQ"), clock[0])
        sells = [c for c in _calls(fake, "place") if c[2] == "SELL"]
        assert len(sells) == 1 and sells[0][3] == live.qty, "the real lots still leave at the broker"
        assert e._venue_positions() == [live] and e._at_venue(), "the reconcile still compares them"
    finally:
        await e.stop()


# -- the engine: resting targets and exits ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_target_sell_rests_at_the_broker_and_its_fill_takes_the_rung(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        r = e._target_resting(pos.id)
        assert r is not None and r.venue == "live" and (r.ctx[1], r.limit, r.intent.qty) == (0, 19.0, LOT)
        assert [c[2:] for c in _calls(fake, "place")] == [("SELL", LOT, 19.0)]
        fake.fill(r.bo.remote_id, LOT, 19.0)
        await _step(e, clock, 1, 18.90, 19.10)
        assert pos.targets_hit == 1 and pos.qty_remaining == 3 * LOT
        r2 = e._target_resting(pos.id)
        assert r2 is not None and (r2.ctx[1], r2.limit, r2.intent.qty) == (1, 21.0, 3 * LOT), "the next rung rests at once"
        assert [t["outcome"] for t in pos.exec_log["targets"]] == ["placed", "filled", "placed"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_live_target_is_replaced_when_the_ladder_moves_only_after_the_broker_confirms_the_cancel(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        old = e._target_resting(pos.id)
        assert old.limit == 19.0
        pos.option_targets, pos.option_t1 = (19.5, 21.0), 19.5  # the ladder moved
        fake.hold_cancels = True
        await _step(e, clock, 1, 17.40, 17.60)
        assert _calls(fake, "cancel") and len(_calls(fake, "place")) == 1, "the old sell is asked off; nothing new yet"
        await _step(e, clock, 1, 17.40, 17.60)
        assert len(_calls(fake, "place")) == 1 and e._target_resting(pos.id) is old
        fake.release_cancels()
        await _step(e, clock, 1, 17.40, 17.60)
        new = e._target_resting(pos.id)
        assert new is not old and new.limit == 19.5 and [c[4] for c in _calls(fake, "place")] == [19.0, 19.5]
        assert [t["outcome"] for t in pos.exec_log["targets"]][:2] == ["placed", "cancelled — replaced — the ladder moved on"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_decision_made_before_the_brokers_target_fill_was_seen_is_dropped_not_sent(settings, clock):
    """On paper the resting sell is judged before the exit engine. Live, the broker may have filled it
    a moment before its status said so: the exit engine's TARGET for that rung, taken on the stale
    position, must not sell a second tranche — the cancel finds the fill, books it, and the decision
    is made again next pass."""
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        tgt = e._target_resting(pos.id)
        fake.fill(tgt.bo.remote_id, LOT, 19.0)  # filled at the broker; the engine has not polled since
        await e._exit(pos, ExitDecision(pos.id, ExitReason.TARGET, 19.05, LOT, "T1"), clock[0])
        assert pos.targets_hit == 1 and pos.qty_remaining == 3 * LOT, "the broker's fill booked"
        assert [c[4] for c in _calls(fake, "place")] == [19.0], "and no second tranche sold on the stale decision"
        # not filled: the exit engine's own TARGET replaces the resting sell with a walked one, as on paper
        await _step(e, clock, 1, 18.90, 19.30, manage=False)
        tgt2 = e._target_resting(pos.id)
        if tgt2 is None:
            await e._ensure_resting_target(pos, clock[0])
            tgt2 = e._target_resting(pos.id)
        await e._exit(pos, ExitDecision(pos.id, ExitReason.TARGET, 21.0, 3 * LOT, "T2"), clock[0])
        assert e._target_resting(pos.id) is None and e._exit_resting(pos.id) is not None
        assert _calls(fake, "cancel")[-1][1] == tgt2.bo.exch_order_id
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_never_two_live_sells_for_one_position(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._place_live_exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        await e._place_live_exit(pos, ExitDecision(pos.id, ExitReason.EOD, 14.8, pos.qty, "EOD"), clock[0])
        assert len(_calls(fake, "place")) == 1
        # a more urgent exit supersedes the working one only once the broker shows it off
        fake.hold_cancels = True
        await e._exit(pos, ExitDecision(pos.id, ExitReason.EOD, 14.8, pos.qty, "EOD"), clock[0])
        assert len(_calls(fake, "place")) == 1 and _calls(fake, "cancel")
        fake.release_cancels()
        await e._exit(pos, ExitDecision(pos.id, ExitReason.EOD, 14.8, pos.qty, "EOD"), clock[0])
        ex = e._exit_resting(pos.id)
        assert len(_calls(fake, "place")) == 2 and ex.deadline_s == 10 and "EOD" in ex.intent.client_order_id
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_market_fill_without_an_average_price_is_booked_at_the_bid_and_flagged(settings, clock):
    class NoAvg(FakeBroker):
        def _row(self, o):
            row = super()._row(o)
            row.pop("AvgRate")
            return row

    e, fake = await _live(settings, clock, NoAvg())
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._place_live_exit(pos, ExitDecision(pos.id, ExitReason.EOD, 14.8, pos.qty, "EOD"), clock[0], cross_n=9)
        ex = e._exit_resting(pos.id)
        assert ex.intent.limit_price is None and _calls(fake, "place")[-1][4] == 0.0, "a MARKET order"
        fake.fill(ex.bo.remote_id, pos.qty, 14.55)
        await _step(e, clock, 1, 14.60, 15.00)
        assert pos.status == "CLOSED" and pos.exec_log["exits"][-1]["fillPrice"] == 14.60, "never at 0.00: the bid it met"
        assert "row" in pos.exec_log["exits"][-1]["broker"], "the broker's own answer on the trail"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_partly_filled_target_sell_is_booked_slice_by_slice_and_takes_the_rung_once(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], lots=10, targets=(19.0, 21.0)))
        await _step(e, clock, 1, 17.40, 17.60)
        r = e._target_resting(pos.id)
        assert r.intent.qty == 4 * LOT
        fake.fill(r.bo.remote_id, LOT, 19.0)
        await _step(e, clock, 1, 18.80, 18.90, manage=False)
        assert pos.targets_hit == 0 and pos.qty_remaining == 10 * LOT, "a working partial is booked when the order is over"
        fake.hold_cancels = True  # the broker is slow to confirm the cancel
        _book(e, 15.90, 16.10, clock[0])
        stop = ExitDecision(pos.id, ExitReason.SL_OP, 16.0, pos.qty_remaining, "SL-OP")
        await e._exit(pos, stop, clock[0])
        assert _calls(fake, "cancel") and len(_calls(fake, "place")) == 1, "the stop waits for the target's cancel"
        await _step(e, clock, 1, 15.90, 16.10, manage=False)
        await e._exit(pos, stop, clock[0])
        assert len(_calls(fake, "place")) == 1, "still unconfirmed: still no second SELL for the same lots"
        fake.release_cancels()
        await _step(e, clock, 1, 15.90, 16.10, manage=False)
        assert pos.targets_hit == 1 and pos.qty_remaining == 9 * LOT, "the lot sold at T1 is booked, the rung taken once"
        assert pos.exec_log["exits"][-1]["reason"] == "TARGET" and pos.exec_log["exits"][-1]["qty"] == LOT
        await e._exit(pos, replace(stop, qty=pos.qty_remaining), clock[0])
        ex = e._exit_resting(pos.id)
        assert ex is not None and ex.intent.qty == 9 * LOT and e._target_resting(pos.id) is None, "now the stop, for the rest"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_stop_takes_the_live_target_off_first_then_walks_from_the_mid_and_crosses(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0]))
        await _step(e, clock, 1, 17.40, 17.60)
        tgt = e._target_resting(pos.id)
        assert tgt is not None
        # the option falls through its 15.00 stop
        await _step(e, clock, 1, 14.60, 15.00)
        i_cancel = fake.calls.index(("cancel", tgt.bo.exch_order_id))
        i_sell = next(i for i, c in enumerate(fake.calls) if c[0] == "place" and c[1] != tgt.intent.client_order_id)
        assert i_cancel < i_sell and any(c[0] == "status" for c in fake.calls[i_cancel:i_sell]), \
            "the target is asked off, the broker shows it off, and only then the stop SELL"
        await _step(e, clock, 1, 14.60, 15.00)
        ex = e._exit_resting(pos.id)
        assert ex is not None and ex.venue == "live" and ex.limit == 14.80 and ex.intent.qty == pos.qty, "at the mid"
        await _step(e, clock, 5, 14.60, 15.00)
        assert _calls(fake, "modify")[-1][2] == exit_limit(14.60, 15.00, 6, 15) == 14.70, "6 s into 15: walked toward the bid by MODIFY"
        await _step(e, clock, 10, 14.60, 15.00)  # 15 s: the deadline — cancel, then cross at the bid
        await _step(e, clock, 1, 14.60, 15.00)
        cross = e._exit_resting(pos.id)
        assert cross is not None and cross.cross_n == 1 and cross.limit == 14.60 and cross.intent.client_order_id.endswith(
            cross.intent.client_order_id.split("-")[-1]) and "X-" in cross.intent.client_order_id
        # nobody at 14.60 any more: re-sent at the new bid, then a MARKET order
        for n in (2, 3, 4):
            await _step(e, clock, 5, 14.40 - n / 20, 14.80)
            await _step(e, clock, 1, 14.40 - n / 20, 14.80)
        last = [c for c in _calls(fake, "place") if c[2] == "SELL"][-1]
        assert last[4] == 0.0, "after 3 crosses at the bid: a market order"
        fake.fill(last[1], pos.qty_remaining, 14.20)
        await _step(e, clock, 1, 14.20, 14.80)
        assert pos.status == "CLOSED" and pos.exec_log["exits"][-1]["fillPrice"] == 14.20
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_partly_filled_exit_keeps_working_the_rest(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        fake.fill(ex.bo.remote_id, LOT, 14.80)
        await _step(e, clock, 1, 14.60, 15.00)
        assert pos.qty_remaining == 3 * LOT and pos.status == "OPEN" and e._exit_resting(pos.id) is ex, "the rest keeps working"
        fake.fill(ex.bo.remote_id, 3 * LOT, 14.70)
        await _step(e, clock, 1, 14.60, 15.00)
        assert pos.status == "CLOSED"
        # the second slice is priced out of the broker's running average (reported to the paisa: 14.72)
        assert [x["fillPrice"] for x in pos.exec_log["exits"]] == [14.80, pytest.approx(14.70, abs=0.011)]
        o = [o for o in await _rows(e, "orders") if o["client_order_id"] == ex.intent.client_order_id]
        assert len(o) == 1 and o[0]["filled"] == 4 * LOT and o[0]["avg_price"] == pytest.approx(14.72, abs=0.005), "one row, the whole order"
    finally:
        await e.stop()


# -- restart ------------------------------------------------------------------------------------------------


def test_the_venue_and_the_exit_attempt_survive_the_positions_round_trip(clock):
    pos = _held("FUDKII", clock[0])
    pos.exec_log["exitAttempt"] = 3
    back = _position_from_json(_position_json(pos))
    assert back.venue == "live" and back.exec_log["exitAttempt"] == 3
    old = _position_json(pos)
    old.pop("venue")
    assert _position_from_json(old).venue == "paper", "a position written before the field: paper"


@pytest.mark.asyncio
async def test_a_restart_never_resends_a_live_exits_id(settings, clock):
    e, _fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        first = e._exit_resting(pos.id).intent.client_order_id
        assert pos.exec_log["exitAttempt"] == 1
        await e.ledger.upsert_position(_position_json(pos))
    finally:
        await e.stop()
    e2 = Engine(settings.model_copy(update={"paper_limit_orders": True}))
    await e2.start()
    try:
        assert e2._exit_attempts[pos.id] == 1
        p2 = e2.positions[pos.id]
        from kotsin_nse.engine import exit_client_order_id

        assert exit_client_order_id(p2, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, ""), e2._exit_attempts[pos.id]) != first
    finally:
        await e2.stop()


@pytest.mark.asyncio
async def test_orders_left_at_the_broker_are_adopted_or_cancelled_and_booked_at_boot(settings, clock):
    e, fake = await _live(settings, clock)
    try:
        pos = await _hold(e, _held("FUDKII", clock[0], targets=()))
        _book(e, 14.60, 15.00, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 14.8, pos.qty, "SL-OP"), clock[0])
        ex = e._exit_resting(pos.id)
        await e.ledger.upsert_position(_position_json(pos))
        # an entry of another book, working when the process died
        await e.live_orders.place(_intent("FII-RTX-260928-110000-009-EN-TATASTEEL-190CE-L4"), limit=17.10, now=clock[0], kind="entry")
        fake.hold_cancels = True  # the shutdown's cancel of the entry is not confirmed before the process goes
    finally:
        await e.stop()
    assert ("cancel", fake.orders["FII-RTX-260928-110000-009-EN-TATASTEEL-190CE-L4"[:38]]["exch_id"]) in fake.calls, \
        "stop() cancels working entries"
    assert ("cancel", ex.bo.exch_order_id) not in fake.calls, "... and leaves the exit working"
    fake.fill(ex.bo.remote_id, 2 * LOT, 14.80)
    fake.fill("FII-RTX-260928-110000-009-EN-TATASTEEL-190CE-L4", LOT, 17.10)
    e2 = Engine(settings.model_copy(update={"paper_limit_orders": True}))
    await e2.start()
    try:
        e2.live_orders.rest = fake
        await e2._settle_left_behind_live_orders()
        p2 = e2.positions[pos.id]
        adopted = e2._exit_resting(pos.id)
        assert adopted is not None and adopted.bo.client_order_id == ex.bo.client_order_id, "the working exit is adopted"
        await e2.set_mode(Mode.LIVE, armed_minutes=60)
        clock[0] += 1
        _book(e2, 14.60, 15.00, clock[0])
        await e2._advance_resting(clock[0])
        assert p2.qty_remaining == 2 * LOT, "its fills are booked as it goes on"
        entry = e2.live_orders.orders["FII-RTX-260928-110000-009-EN-TATASTEEL-190CE-L4"]
        assert not entry.settled, "the entry's cancel is not confirmed yet: still tracked"
        fake.release_cancels()
        clock[0] += 4
        await e2._advance_resting(clock[0])
        assert "FII-RTX-260928-110000-009-EN-TATASTEEL-190CE-L4" not in e2.live_orders.orders, "settled, reported, forgotten"
        ev = [x for x in await e2.ledger.rows_between("events", 0, clock[0] + 10) if x.get("kind") == "live.left_behind"]
        assert ev
    finally:
        await e2.stop()

# -- parity: the same session on paper and live -------------------------------------------------------------


def _ramp(frm: float, to: float, step: float = 0.05) -> list[tuple[float, float, float]]:
    """``(dt, bid, ask)`` a second apart, the mid moving ``step`` a tick from ``frm`` to ``to``, 0.10 wide."""
    out, mid = [], frm
    step = step if to > frm else -step
    while (to - mid) * step > 1e-9:
        mid = round(mid + step, 2)
        out.append((1.0, round(mid - 0.05, 2), round(mid + 0.05, 2)))
    return out


def _flat(mid: float, n: int) -> list[tuple[float, float, float]]:
    return [(1.0, round(mid - 0.05, 2), round(mid + 0.05, 2))] * n


#: trade 1: the entry rests and fills, the option runs up through the ladders and back — target sells,
#: target exits and trails; trade 2: the entry rests past its hold and is followed up, then the option
#: falls through every stop — the exits walk from the mid, are crossed at the bid, re-crossed
UP = [(1.0, 16.95, 17.25), (1.0, 17.00, 17.10), (1.0, 17.00, 17.10), *_ramp(17.05, 21.5), *_ramp(21.5, 14.0), *_flat(14.0, 60)]
DOWN = [*[(1.0, 16.95, 17.25)] * 31, *[(1.0, 17.30, 17.50)] * 6, *[(1.0, 17.35, 17.40)] * 2,
        *_ramp(17.40, 15.0, 0.02), *_ramp(15.0, 13.0, 0.10), *_flat(13.0, 90)]


def _trail(p: Position) -> dict:
    return {"qty": p.qty, "entry": p.entry, "status": p.status, "targets_hit": p.targets_hit, "exit_price": p.exit_price,
            "charges": round(p.charges, 2), "entry_charges": round(p.entry_charges, 2), "opened": p.opened_ts,
            "exits": [(x["reason"], x["qty"], x["fillPrice"]) for x in p.exec_log.get("exits", [])]}


@pytest.mark.asyncio
async def test_parity_the_live_books_fill_as_the_paper_books_against_a_broker_that_fills_like_the_paper_matcher(settings, clock,
                                                                                                             tmp_path):
    """The same triggers and the same tape, once on paper and once LIVE against a broker that fills
    the way the paper matcher does: the same fills, the same positions, the same P&L — so a
    difference in the real-money test is the market's, not the order path's."""
    paper = await _engine(settings, clock)
    (tmp_path / "live").mkdir()
    holder: dict[str, Engine] = {}
    fake = FakeBroker(book=lambda code: holder["e"]._touch(code, time.time())[:3])
    live, _ = await _live(settings.model_copy(update={"data_dir": tmp_path / "live", "db_url": f"sqlite+aiosqlite:///{tmp_path}/live.db"}),
                          clock, fake)
    holder["e"] = live
    opened: dict[str, dict[str, Position]] = {"paper": {}, "live": {}}
    try:
        for n, path in enumerate((UP, DOWN)):
            sig = replace(_sig(clock), ts=_sig(clock).ts + 1800 * n)
            start = clock[0]
            for e in (paper, live):
                clock[0] = start
                _book(e, 16.95, 17.25, clock[0])
                e.ltps[UND.scrip_code] = 187.5
                await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
            assert len(paper._resting) == len(live._resting) == 4
            for dt, bid, ask in path:
                clock[0] += dt
                for name, e in (("paper", paper), ("live", live)):
                    _book(e, bid, ask, clock[0])
                    e.ltps[UND.scrip_code] = round(187.5 + ((bid + ask) / 2 - 17.10) * 1.2, 2)
                    await e._manage_positions()
                    await e._live_quiesce()
                    for p in e.positions.values():
                        opened[name].setdefault(f"{p.strategy}#{n}", p)
            assert not paper.positions or all(p.status == "CLOSED" for p in paper.positions.values())
        books = {f"{b}#{n}" for b in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y") for n in (0, 1)}
        assert books <= set(opened["paper"]) and set(opened["paper"]) == set(opened["live"])
        assert all(opened["live"][b].venue == "live" and opened["paper"][b].venue == "paper" for b in books)
        # the RT-Y wide shadow stays paper in the LIVE session — under the same paper rules as a paper session
        shadows = {f"FUDKII_RT_Y_W1#{n}" for n in (0, 1)}
        assert shadows <= set(opened["live"]) and all(opened["live"][b].venue == "paper" for b in shadows)
        for b in sorted(books | shadows):
            p, lv = _trail(opened["paper"][b]), _trail(opened["live"][b])
            assert p["status"] == "CLOSED", (b, p)
            assert lv == p, (b, p, lv)
        for b in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"):
            assert live.wallets[b].balance == pytest.approx(paper.wallets[b].balance, abs=0.01), b
            assert live.wallets[b].balance != live.wallets[b].day_start_balance, "a real P&L, not an idle book"
        reasons = {x[0] for b in books for x in _trail(opened["paper"][b])["exits"]}
        assert "TARGET" in reasons and reasons & {"SL-OP", "SL-EQ"}, reasons
        kinds = {c[0] for c in fake.calls}
        assert {"place", "status", "modify", "cancel"} <= kinds, "the path used every broker call"
        crosses = [c for c in fake.calls if c[0] == "place" and "X-" in c[1]]
        assert crosses, "an exit crossed at its deadline"
    finally:
        await paper.stop()
        await live.stop()
