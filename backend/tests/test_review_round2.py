"""The second review of phase 10 (2026-09-26), finding by finding: each test is the reviewer's probe,
turned round to assert the fix."""

from __future__ import annotations

import asyncio
import time

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Fill,
    Instrument,
    InstrumentKind,
    OptionType,
    OrderIntent,
    OrderSide,
    Purpose,
)
from kotsin_nse.engine import IN_TREND_BOOKS
from kotsin_nse.exec.gateway import Decision, Gateway, LiveCaps, LiveContext, Mode
from kotsin_nse.exec.live import LiveResult
from kotsin_nse.exec.reconcile import Reconciler
from kotsin_nse.strategy.keys import StrategyKey
from kotsin_nse.venue.base import VenueError
from tests.test_limit_orders import _book, _engine, _rows, _sig

# -- A1: one trade per book per name, also while its entry is at the broker -------------------------


@pytest.mark.asyncio
async def test_a_take_while_the_books_own_entry_is_at_the_broker_is_refused(settings, midday):
    """The reviewer's probe: FUDKII's automatic entry waiting on the broker (a LIVE order polls for up
    to ~20 s) and an operator TAKE of the same trigger ended with TWO FUDKII positions."""
    e = await _engine(settings, midday, limit_orders=False)
    try:
        _book(e, 16.95, 17.25, midday[0])
        sig = _sig(midday)
        orig = e._submit

        async def slow_submit(intent, **kw):
            await asyncio.sleep(0.1)
            return await orig(intent, **kw)

        e._submit = slow_submit  # type: ignore[method-assign]
        auto = asyncio.create_task(e._handle_signal(sig, None))
        await asyncio.sleep(0.02)
        assert ("FUDKII", "TATASTEEL") in e._entering
        with pytest.raises(RuntimeError, match="is placing an entry"):
            await e.operator_take("FUDKII", sig.signal_id)
        await auto
        assert len([p for p in e.positions.values() if p.strategy == "FUDKII"]) == 1
        assert not e._entering, "released once the entry is booked"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_second_entry_of_the_same_book_while_the_first_is_placed_is_refused(settings, midday):
    e = await _engine(settings, midday, limit_orders=False)
    try:
        _book(e, 16.95, 17.25, midday[0])
        sig = _sig(midday)
        orig = e._submit

        async def slow_submit(intent, **kw):
            await asyncio.sleep(0.05)
            return await orig(intent, **kw)

        e._submit = slow_submit  # type: ignore[method-assign]
        a, b = await asyncio.gather(e._handle_signal(sig, None, take=True), e._handle_signal(sig, None, take=True))
        decisions = sorted([a["FUDKII"]["decision"], b["FUDKII"]["decision"]])
        assert decisions == ["ALREADY_ENTERING", "PAPER_FILLED"]
        assert len([p for p in e.positions.values() if p.strategy == "FUDKII"]) == 1
        row = next(r for r in await _rows(e, "signals") if r["signal_id"] == sig.signal_id)
        assert row["decision"] == "PAPER_FILLED", "the refused attempt never writes the row ahead of the fill"
        assert e.wallets["FUDKII"].deployed == pytest.approx(e._deployed_expected("FUDKII", e.wallets["FUDKII"]))
    finally:
        await e.stop()


# -- A4: the volume read blocks no one and holds up only the books that gate on it ------------------


@pytest.mark.asyncio
async def test_the_parent_places_its_order_without_waiting_on_the_volume_read(settings, midday):
    e = await _engine(settings, midday)
    try:
        _book(e, 16.95, 17.25, midday[0])
        ev: list[str] = []

        async def slow_vol(u, **_kw):
            ev.append("vol-start")
            await asyncio.sleep(0.1)
            ev.append("vol-end")
            return {}

        e._volume_surges = slow_vol  # type: ignore[method-assign]
        orig = e.gateway.place_limit

        def place(intent):
            ev.append(f"place:{intent.strategy}")
            return orig(intent)

        e.gateway.place_limit = place  # type: ignore[method-assign]
        await e._handle_signal(_sig(midday), None, books=IN_TREND_BOOKS)
        assert ev.index("place:FUDKII") < ev.index("vol-end"), ev
        assert ev.count("vol-start") == 1, "one read for every gating book"
        assert {"place:FUDKII_RT_X", "place:FUDKII_RT_Y"} <= set(ev)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_failed_volume_read_costs_no_book_its_entry(settings, midday):
    e = await _engine(settings, midday)
    try:
        _book(e, 16.95, 17.25, midday[0])

        async def bad_vol(u):
            raise KeyError("v")

        e._volume_surges = bad_vol  # type: ignore[method-assign]
        out = await e._handle_signal(_sig(midday), None, books=IN_TREND_BOOKS)
        assert all(o["decision"] != "ERROR" for o in out.values()), out
        resting = {r.intent.strategy for r in e._resting.values() if r.kind == "entry"}
        assert {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"} == resting
    finally:
        await e.stop()


# -- A5: the id the broker keeps is the id polled for ------------------------------------------------


@pytest.mark.asyncio
async def test_an_order_placed_without_an_echo_is_polled_by_the_id_as_sent(settings):
    from kotsin_nse.venue.fivepaisa.rest import REMOTE_ID_MAX, FivePaisaREST

    rest = FivePaisaREST.__new__(FivePaisaREST)
    rest.s = settings
    sent: list[dict] = []

    async def post(path, body):
        sent.append(body)
        return {}

    rest._post = post  # type: ignore[method-assign]
    opt = Instrument("153805", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=2750, strike=190.0,
                     option_type=OptionType.CE, underlying="TATASTEEL")
    cid = "FII-RTX-260926-192510-007-EN-TATASTEEL-190CE-L4"
    got = await rest.place_order(opt, OrderSide.BUY, 2750, price=17.1, remote_order_id=cid)
    assert got == cid[:REMOTE_ID_MAX] == sent[0]["RemoteOrderID"]
    await rest.order_status(opt.exch, cid)
    assert sent[1]["OrdStatusReqList"][0]["RemoteOrderID"] == cid[:REMOTE_ID_MAX]


# -- A6: order numbers after a restart, also a shadow's -----------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_never_reuses_an_order_number_not_even_a_shadows(settings, midday):
    e = await _engine(settings, midday, limit_orders=False)
    _book(e, 16.95, 17.25, midday[0])
    await e._handle_signal(_sig(midday), None, books=IN_TREND_BOOKS)
    w1 = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y_W1")
    old = w1.exec_log["ref"]
    await e.stop()
    e2 = await _engine(settings, midday, limit_orders=False)  # same database, the same second
    try:
        assert e2._order_ref("FUDKII_RT_Y_W1", midday[0]) != old
    finally:
        await e2.stop()


# -- A7: a ledger error after a fill ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_ledger_error_after_a_fill_keeps_the_order_row_and_the_outcome(settings, midday):
    e = await _engine(settings, midday, limit_orders=False)
    try:
        _book(e, 16.95, 17.25, midday[0])

        async def boom(*a, **k):
            raise RuntimeError("disk full")

        e.ledger.upsert_position = boom  # type: ignore[method-assign]
        out = await e._handle_signal(_sig(midday), None)
        assert out["FUDKII"]["decision"] == "PAPER_FILLED"
        assert len([o for o in await _rows(e, "orders") if o["strategy"] == "FUDKII"]) == 1
        w = e.wallets["FUDKII"]
        assert w.deployed == pytest.approx(e._deployed_expected("FUDKII", w))
    finally:
        await e.stop()


# -- A10: a refused take is not "entered" -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_take_reports_the_refusal_not_an_entry(settings, midday):
    e = await _engine(settings, midday, limit_orders=False)
    try:
        _book(e, 16.95, 17.25, midday[0])
        sig = _sig(midday)
        e._signals_today[sig.signal_id] = sig
        w = e.wallets["FUDKII_RT_X"]
        w.drawdown_halt = "DRAWDOWN 16.00%"
        w._sync_halt()
        r = await e.operator_take("FUDKII_RT_X", sig.signal_id)
        assert r["entered"] is False and r["decision"] == "WALLET_HALTED"
    finally:
        await e.stop()


# -- A11: the Risk page's reconcile buttons ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconcile_buttons_drive_the_position_reconciler(settings, midday):
    e = await _engine(settings, midday, limit_orders=False)
    try:
        with pytest.raises(RuntimeError, match="no broker session"):
            await e.reconcile_now()

        class Rest:
            fail = True

            async def net_positions(self):
                if self.fail:
                    raise VenueError("timeout")
                return []

        rest = Rest()
        e.reconciler_positions = Reconciler(rest)  # type: ignore[arg-type]
        r = await e.reconcile_now()
        assert r["frozen"] is True and e.halted()[0], "a failed read freezes (fails closed)"
        a = await e.acknowledge_reconcile()
        assert a["frozen"] is False and not e.halted()[0], "the operator can lift it"
        rest.fail = False
        e.reconciler_positions._freeze("x")
        assert (await e.reconcile_now())["frozen"] is False, "a clean read thaws it"
    finally:
        await e.stop()


# -- the breaker: only entries count, and a trip survives a restart ------------------------------------


OPT = Instrument("1", "X", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=100, strike=100.0, option_type=OptionType.CE, underlying="X")


def _intent(book: str, n: int, purpose: Purpose = Purpose.ENTRY) -> OrderIntent:
    return OrderIntent(strategy=book, instrument=OPT, side=OrderSide.BUY if purpose is Purpose.ENTRY else OrderSide.SELL, qty=100,
                       purpose=purpose, signal_id="s", client_order_id=f"{book}-{purpose.value}-{n}", reason="t")


def test_an_exit_fill_does_not_end_a_run_of_rejected_entries(settings):
    class Matcher:
        reject = True

        def fill(self, intent, book, *, fallback_ltp=None, now=None):
            from kotsin_nse.exec.paper import NoBook

            if intent.purpose is Purpose.ENTRY:
                raise NoBook("no depth")
            return Fill(price=1.0, qty=intent.qty, ts=time.time(), charges=1.0)

    g = Gateway(matcher=Matcher(), mode=lambda: Mode.PAPER, halted=lambda: (False, ""), book_for=lambda c: None, ltp_for=lambda c: 1.0)  # type: ignore[arg-type]
    for n in range(11):
        g.submit(_intent("FUDKII", n))
        g.submit(_intent("FUDKII", n, Purpose.EXIT))
    assert g.rejects_by_book["FUDKII"] == 11, "eleven entries refused in a row; the exits in between filled"
    g.submit(_intent("FUDKII", 99))
    assert g.book_tripped("FUDKII")


@pytest.mark.asyncio
async def test_a_live_config_failure_never_counts_and_a_sent_order_counts_toward_the_day_cap():
    class Live:
        async def place(self, intent):
            return LiveResult(False, error="RMS: insufficient margin")

    ctx = LiveContext(balance=1e6, open_positions=0, day_pnl_inr=0.0, now_hm_ist="10:00", segment="NSE_FO")
    g = Gateway(matcher=None, mode=lambda: Mode.LIVE_CAPPED, halted=lambda: (False, ""), book_for=lambda c: None,  # type: ignore[arg-type]
                ltp_for=lambda c: 1.0, caps=LiveCaps(segments=("NSE_FO",)))
    for n in range(20):
        r = await g.submit_live(_intent("FUDKII", n), ctx=ctx)
        assert r.decision is Decision.REJECTED_CONFIG
    assert g.rejects_by_book.get("FUDKII", 0) == 0 and not g.book_tripped("FUDKII"), "no executor configured is not the broker"
    g.live = Live()  # type: ignore[assignment]
    r = await g.submit_live(_intent("FUDKII", 100), ctx=ctx)
    assert r.decision is Decision.REJECTED_BROKER and g.rejects_by_book["FUDKII"] == 1
    assert g.live_orders_by_book["FUDKII"] == 1, "sent and refused by the broker is still an order sent"


@pytest.mark.asyncio
async def test_a_tripped_breaker_survives_a_restart_until_it_is_reset(settings, midday):
    e = await _engine(settings, midday, limit_orders=False)
    e.gateway.tripped_books.add("FUDKII_RT_X")
    e.gateway._new_trips.append("FUDKII_RT_X")
    for book in e.gateway.take_new_trips():
        await e.ledger.event("gateway.book_breaker", {"book": book, "rejects": 12})
    await e.stop()
    e2 = await _engine(settings, midday, limit_orders=False)
    try:
        assert e2.gateway.book_tripped("FUDKII_RT_X"), "a deploy does not clear a tripped breaker"
        await e2.reset_breaker("FUDKII_RT_X")
    finally:
        await e2.stop()
    e3 = await _engine(settings, midday, limit_orders=False)
    try:
        assert not e3.gateway.book_tripped("FUDKII_RT_X"), "the reset is recorded too"
    finally:
        await e3.stop()


@pytest.mark.asyncio
async def test_a_resting_entry_of_a_book_whose_breaker_trips_is_cancelled(settings, midday):
    e = await _engine(settings, midday)
    try:
        _book(e, 16.95, 17.25, midday[0])
        await e._handle_signal(_sig(midday), None, books=(StrategyKey.FUDKII_RT_X,))
        r = next(r for r in e._resting.values() if r.kind == "entry")
        e.gateway.tripped_books.add("FUDKII_RT_X")
        midday[0] += 5
        await e._advance_one(r, midday[0])
        assert r.intent.client_order_id not in e._resting and not e.positions
        assert e.wallets["FUDKII_RT_X"].deployed == pytest.approx(0.0), "its hold released"
    finally:
        await e.stop()


# -- the wide-stop shadow: a halted purse is recorded, not silent --------------------------------------


@pytest.mark.asyncio
async def test_the_wide_stop_shadow_records_a_halted_purse(settings, midday):
    e = await _engine(settings, midday, limit_orders=False)
    try:
        _book(e, 16.95, 17.25, midday[0])
        w = e.wallets["FUDKII_RT_Y_W1"]
        w.drawdown_halt = "DRAWDOWN 15.20%"
        w._sync_halt()
        await e._handle_signal(_sig(midday), None, books=(StrategyKey.FUDKII_RT_Y,))
        assert any(p.strategy == "FUDKII_RT_Y" for p in e.positions.values())
        assert not any(p.strategy == "FUDKII_RT_Y_W1" for p in e.positions.values())
        skips = [ev for ev in await _rows(e, "events") if ev.get("kind") == "rt_twin.skipped" and ev.get("book") == "FUDKII_RT_Y_W1"]
        assert skips and "halted — DRAWDOWN 15.20%" in skips[0]["reason"]
    finally:
        await e.stop()


# -- A9: the shadow page counts gate B alone as gate B -------------------------------------------------


def test_the_gate_b_tab_counts_gate_b_skips_only():
    from kotsin_nse.api.shadow import _gate_b_skip

    def row(action, gate):
        return {"rtY": {"action": action, "gate": gate}}

    assert _gate_b_skip(row("SKIP", "breadth")) and _gate_b_skip(row("SKIP", "pivot_ahead"))
    assert not _gate_b_skip(row("SKIP", "dried_volume")), "dried volume is not gate B"
    assert not _gate_b_skip(row("MISSED", "missed")) and not _gate_b_skip(row("TAKE", None))
