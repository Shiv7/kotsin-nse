"""The live route (2026-11-01): protected limits, the broker's own confirmations, the order throttle, the
reconciler, and the live overlay on the shadow book. Every broker here is a fake that answers from a script;
nothing opens a socket."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import (
    Fill,
    Instrument,
    InstrumentKind,
    Order,
    OrderIntent,
    OrderSide,
    Purpose,
)
from kotsin_nse.engine import Engine
from kotsin_nse.exec.events import OrderEvents, classify, is_order_frame
from kotsin_nse.exec.gateway import Decision, Gateway, LiveCaps, LiveContext, Mode, OrderResult
from kotsin_nse.exec.live import LiveExecutor, parse_status_row
from kotsin_nse.exec.paper import PaperMatcher
from kotsin_nse.exec.reconcile import LocalPosition, MismatchKind, Reconciler
from kotsin_nse.exec.throttle import OrderThrottle
from kotsin_nse.ops.archive import DailyArchive
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.venue.base import VenueError
from kotsin_nse.venue.fivepaisa.auth import Authenticator
from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST
from kotsin_nse.venue.fivepaisa.ws import FivePaisaFeed, build_order_frame, ws_url_for
from tests import test_strategy as ts

SETTINGS = Settings(_env_file=None)
COSTS = CostModel(SETTINGS)


def _inst(code: str = "2885", symbol: str = "RELIANCE") -> Instrument:
    return Instrument(scrip_code=code, symbol=symbol, segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY, name=symbol, underlying=symbol)


def _intent(purpose: Purpose = Purpose.ENTRY, side: OrderSide = OrderSide.BUY, qty: int = 10, coid: str = "sig-000000000001|E|pos-000000000001", ref: float = 100.0) -> OrderIntent:
    return OrderIntent(strategy="cpr_retest", instrument=_inst(), side=side, qty=qty, purpose=purpose, signal_id="sig-000000000001",
                       client_order_id=coid, position_id="pos-000000000001", ref_price=ref)


class FakeREST:
    """A scripted broker: ``place_order`` echoes the id, ``order_status`` answers from a queue, every call is kept."""

    def __init__(self, statuses=None, *, place_error: str | None = None, order_book=None, modify_error: str | None = None, positions=None):
        self.statuses = list(statuses or [])
        self.calls: list[tuple] = []
        self.place_error = place_error
        self.book = list(order_book or [])
        self.modify_error = modify_error
        self.positions = list(positions or [])

    async def place_order(self, inst, side, qty, *, price, intraday, remote_order_id, stop_trigger=0.0):
        self.calls.append(("place", inst.scrip_code, side.value, qty, price, remote_order_id))
        if self.place_error:
            raise VenueError(self.place_error)
        return remote_order_id

    async def order_status(self, exch, remote_id):
        self.calls.append(("status", remote_id))
        if not self.statuses:
            return {}
        row = self.statuses.pop(0)
        if isinstance(row, Exception):
            raise row
        return row

    async def modify_order(self, exch_order_id, *, price, qty):
        self.calls.append(("modify", exch_order_id, price, qty))
        if self.modify_error:
            raise VenueError(self.modify_error)

    async def cancel_order(self, exch_order_id):
        self.calls.append(("cancel", exch_order_id))

    async def order_book(self):
        self.calls.append(("order_book",))
        return list(self.book)

    async def net_positions(self):
        return list(self.positions)

    async def square_off_all(self):
        self.calls.append(("square_off_all",))


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        await asyncio.sleep(0)


def _exec(rest: FakeREST, **kw) -> tuple[LiveExecutor, FakeClock]:
    clock = FakeClock()
    kw.setdefault("poll_interval_s", 1.0)
    kw.setdefault("fill_timeout_s", 3.0)
    kw.setdefault("chase_steps", 1)
    return LiveExecutor(rest, COSTS, clock=clock, sleep=clock.sleep, **kw), clock


FILLED = {"Status": "Fully Executed", "TradedQty": 10, "PendingQty": 0, "AveragePrice": 100.3, "ExchOrderID": "e1"}
PENDING = {"Status": "Pending", "TradedQty": 0, "PendingQty": 10, "ExchOrderID": "e1"}


# -- the throttle ----------------------------------------------------------------------------------------


async def test_the_throttle_waits_once_the_second_is_full():
    clock = FakeClock()
    th = OrderThrottle(2, clock=clock, sleep=clock.sleep)
    assert await th.acquire("N") == 0.0 and await th.acquire("N") == 0.0
    waited = await th.acquire("N")
    assert waited >= 1.0 and th.stats() == {"per_second": 2.0, "acquired": 3, "waited": 1, "waited_s": round(waited, 3)}
    assert await th.acquire("B") == 0.0  # another exchange has its own window


# -- the broker's confirmations ----------------------------------------------------------------------------


def test_statuses_classify_as_the_docs_name_them():
    assert classify("Fully Executed") == "filled"
    assert classify("Rejected by Exch") == "rejected" and classify("Rejected By 5P") == "rejected"
    assert classify("Cancelled") == "cancelled" and classify("Pending") == "open"
    assert classify("Pending", traded=3, qty=10) == "partial" and classify("Modified", traded=10, qty=10) == "filled"
    assert classify("Placed", req_type="P", req_status=1) == "rejected"
    assert is_order_frame({"ReqType": "P", "RemoteOrderId": "x"}) and not is_order_frame({"Token": 1, "LastRate": 2})


def test_events_follow_place_trade_and_fill_frames():
    seen: list[tuple[str, float]] = []
    ev = OrderEvents(on_frame=lambda row, ts: seen.append((row["ReqType"], ts)))
    rid = "sig-000000000001|E|pos-000000000001"
    st = ev.ingest({"ReqType": "P", "ReqStatus": 0, "RemoteOrderId": rid, "Status": "Placed", "Price": 100.1, "Qty": 10, "ExchOrderID": "e1", "BrokerOrderID": 7, "ScripCode": 2885, "BuySell": "B"}, 10.0)
    assert st is not None and st.outcome == "open" and st.ack_ts == 10.0 and st.exch_order_id == "e1" and st.broker_order_id == "7"
    ev.ingest({"ReqType": "T", "RemoteOrderId": rid, "TotalTradedQty": 4, "Price": 100.1, "OrderQty": 10, "OrderPrice": 100.1}, 10.4)
    assert st.outcome == "partial" and st.traded_qty == 4
    ev.ingest({"ReqType": "T", "RemoteOrderId": rid, "TotalTradedQty": 10, "Price": 100.2, "OrderQty": 10}, 10.6)
    assert st.outcome == "filled" and st.fill_ts == 10.6 and st.avg_price == pytest.approx((4 * 100.1 + 6 * 100.2) / 10)
    ev.ingest({"ReqType": "P", "RemoteOrderId": rid, "Status": "Fully Executed", "TradedQty": 10, "PendingQty": 0}, 10.7)
    assert st.terminal and st.status == "Fully Executed" and st.frames == 4
    assert ev.ingest({"ReqType": "T", "BrokerOrderID": 9}, 11.0) is None and ev.stats()["unmatched"] == 1
    assert [r for r, _ in seen] == ["P", "T", "T", "P", "T"]


async def test_events_wait_wakes_on_the_frame_not_on_the_timeout():
    ev = OrderEvents()
    task = asyncio.create_task(ev.wait("x", 5.0))
    await asyncio.sleep(0.02)
    ev.ingest({"ReqType": "P", "ReqStatus": 1, "RemoteOrderId": "x", "Status": "Placed", "Remark": "RMS: insufficient margin"})
    t0 = time.monotonic()
    st = await task
    assert time.monotonic() - t0 < 1.0 and st is not None and st.outcome == "rejected" and st.reason.startswith("RMS")
    assert (await ev.wait("x", 0.0)) is st  # terminal: answered at once


# -- the executor ------------------------------------------------------------------------------------------


def test_the_protected_limit_is_the_touch_moved_through_the_book():
    ex, _ = _exec(FakeREST())
    buy = _intent()
    assert ex.limit_price(buy, 101.0) == 101.1  # 10 bps above the ask, on the tick
    sell_exit = _intent(Purpose.EXIT, OrderSide.SELL)
    assert ex.limit_price(sell_exit, 101.0) == 100.7  # 30 bps below the bid
    assert ex.limit_price(sell_exit, 101.0, step=1) == 100.4  # chased: another band
    row = parse_status_row({"Status": "Modified", "TradedQty": 0, "PendingQty": 1, "OrderRate": 431.05, "AveragePrice": 0, "ExchOrderID": 10000365323}, 1)
    assert row.outcome == "open" and row.avg_price == 0 and row.exch_order_id == "10000365323"  # the limit sent is never a fill price


async def test_an_entry_fills_through_the_status_poll_when_no_frame_comes():
    rest = FakeREST([PENDING, FILLED])
    ex, _ = _exec(rest)
    res = await ex.place(_intent())
    assert res.ok and res.fill is not None and res.fill.price == 100.3 and res.fill.qty == 10 and res.via == "poll"
    assert rest.calls[0] == ("place", "2885", "BUY", 10, 100.1, "sig-000000000001|E|pos-000000000001")  # ref 100 + 10 bps
    assert res.price_sent == 100.1 and res.fill.charges > 0 and ex.stats()["filled"] == 1


async def test_the_touch_comes_from_the_book_when_there_is_one():
    rest = FakeREST([FILLED])
    ex, _ = _exec(rest, touch_for=lambda i: 101.0)
    await ex.place(_intent())
    assert rest.calls[0][4] == 101.1


async def test_a_rejection_by_the_exchange_is_a_rejection_not_a_timeout():
    rest = FakeREST([{"Status": "Rejected by Exch", "Reason": "RMS: insufficient margin"}])
    ex, _ = _exec(rest)
    res = await ex.place(_intent())
    assert not res.ok and "insufficient" in res.error and ex.stats()["rejected"] == 1 and ("cancel", "e1") not in rest.calls


async def test_a_fill_without_a_rate_is_read_from_the_order_book_never_booked_at_the_limit():
    no_rate = {"Status": "Fully Executed", "TradedQty": 10, "PendingQty": 0}
    rest = FakeREST([no_rate] * 6, order_book=[{"RemoteOrderID": "sig-000000000001|E|pos-000000000001", "OrderStatus": "Fully Executed", "AveragePrice": 99.9, "TradedQty": 10}])
    ex, _ = _exec(rest)
    res = await ex.place(_intent())
    assert res.ok and res.fill is not None and res.fill.price == 99.9 and res.via == "order_book"


async def test_an_unfilled_entry_is_cancelled_and_reported():
    rest = FakeREST([PENDING] * 8)
    ex, _ = _exec(rest)
    res = await ex.place(_intent())
    assert not res.ok and "not filled" in res.error and ("cancel", "e1") in rest.calls and ex.stats()["unfilled"] == 1


async def test_a_partial_entry_books_the_traded_quantity():
    part = {"Status": "Pending", "TradedQty": 4, "PendingQty": 6, "AveragePrice": 100.05, "ExchOrderID": "e1"}
    rest = FakeREST([part] * 8)
    ex, _ = _exec(rest)
    res = await ex.place(_intent())
    assert res.ok and res.partial and res.fill is not None and res.fill.qty == 4 and res.fill.price == 100.05
    assert ("cancel", "e1") in rest.calls and "partial" in res.error


async def test_an_unfilled_exit_is_chased_then_raises():
    rest = FakeREST([PENDING] * 12)
    ex, _ = _exec(rest)
    with pytest.raises(VenueError, match="EXIT"):
        await ex.place(_intent(Purpose.EXIT, OrderSide.SELL))
    assert ("modify", "e1", 99.4, 10) in rest.calls  # 30 bps, then another 30
    assert ("cancel", "e1") in rest.calls and ex.stats()["chased"] == 1


async def test_a_rejected_exit_raises():
    ex, _ = _exec(FakeREST([{"Status": "Rejected By 5P", "Reason": "scrip not allowed"}]))
    with pytest.raises(VenueError, match="scrip not allowed"):
        await ex.place(_intent(Purpose.EXIT, OrderSide.SELL))


async def test_a_placement_refused_by_the_broker_is_quiet_for_an_entry_and_loud_for_an_exit():
    ex, _ = _exec(FakeREST(place_error="RMS: static IP not registered"))
    res = await ex.place(_intent())
    assert not res.ok and "static IP" in res.error
    with pytest.raises(VenueError):
        await ex.place(_intent(Purpose.EXIT, OrderSide.SELL))


async def test_the_frame_resolves_the_order_before_the_poll_does():
    ev = OrderEvents()
    rest = FakeREST([])  # the poll has nothing to say
    ex = LiveExecutor(rest, COSTS, events=ev, poll_interval_s=0.5, fill_timeout_s=3.0, chase_steps=0)
    rid = "sig-000000000001|E|pos-000000000001"
    task = asyncio.create_task(ex.place(_intent()))
    await asyncio.sleep(0.05)
    ev.ingest({"ReqType": "P", "ReqStatus": 0, "RemoteOrderId": rid, "Status": "Placed", "Price": 100.1, "Qty": 10, "ExchOrderID": "e1"})
    ev.ingest({"ReqType": "T", "RemoteOrderId": rid, "TotalTradedQty": 10, "Price": 100.05, "OrderQty": 10})
    t0 = time.monotonic()
    res = await task
    assert time.monotonic() - t0 < 1.0 and res.ok and res.via == "event" and res.fill is not None and res.fill.price == 100.05
    assert res.latency_ms is not None and res.latency_ms >= 0 and ex.stats()["via"]["event"] == 1


async def test_the_rest_client_refuses_a_market_order_and_a_long_id():
    s = Settings(_env_file=None, fp_client_code="50000001")
    async with httpx.AsyncClient() as client:
        rest = FivePaisaREST(s, client, Authenticator(s, client))
        with pytest.raises(VenueError, match="limit orders only"):
            await rest.place_order(_inst(), OrderSide.BUY, 1, price=0.0, remote_order_id="x")
        with pytest.raises(VenueError, match="38"):
            await rest.place_order(_inst(), OrderSide.BUY, 1, price=100.0, remote_order_id="x" * 39)


# -- the gateway -------------------------------------------------------------------------------------------


def _gateway(mode: Mode, *, router=None, ctx=None, live=None, caps=None) -> Gateway:
    return Gateway(matcher=PaperMatcher(COSTS), mode=lambda: mode, halted=lambda: (False, ""), book_for=lambda _c: None, ltp_for=lambda _c: 100.0,
                   caps=caps or LiveCaps(), live=live, router=router, ctx_for=(lambda _i: ctx) if ctx is not None else None)


def _ctx(**kw) -> LiveContext:
    base = {"balance": 50_000.0, "open_positions": 0, "day_pnl_inr": 0.0, "now_hm_ist": "10:30", "segment": "NSE_EQ"}
    base.update(kw)
    return LiveContext(**base)


def test_submit_in_a_live_mode_routes_and_answers_submitted():
    routed: list[tuple[OrderIntent, Order]] = []
    gw = _gateway(Mode.LIVE, router=lambda i, o: routed.append((i, o)), live=object())
    res = gw.submit(_intent())
    assert res.decision is Decision.SUBMITTED and res.order.status == "SUBMITTED" and res.fill is None
    assert len(routed) == 1 and gw.stats()["routed"] == 1 and gw.stats()["live_pending"] == 1 and gw.stats()["live_routed"]
    assert gw.submit(_intent()).decision is Decision.DUP_BLOCKED  # the id was spent when it was routed


def test_live_capped_checks_the_caps_before_routing():
    routed: list = []
    gw = _gateway(Mode.LIVE_CAPPED, router=lambda i, o: routed.append(i), live=object(), ctx=_ctx(open_positions=2))
    res = gw.submit(_intent())
    assert res.decision is Decision.REJECTED_CAP and "positions open" in res.order.note and not routed
    gw2 = _gateway(Mode.LIVE_CAPPED, router=lambda i, o: routed.append(i), live=object(), ctx=_ctx(exposure_ok=False, exposure_reason="balance unknown"))
    assert "balance unknown" in gw2.submit(_intent(coid="c2")).order.note
    gw3 = _gateway(Mode.LIVE_CAPPED, router=lambda i, o: routed.append(i), live=object(), ctx=_ctx(open_positions=2))
    assert gw3.submit(_intent(Purpose.EXIT, OrderSide.SELL, coid="c3")).decision is Decision.SUBMITTED  # exits are never capped


def test_a_live_mode_without_an_executor_is_refused_before_routing():
    routed: list = []
    gw = _gateway(Mode.LIVE, router=lambda i, o: routed.append(i), live=None)
    assert gw.submit(_intent()).decision is Decision.REJECTED_BROKER and not routed


async def test_place_live_reports_the_fill_its_slippage_and_the_measurements():
    ex, _ = _exec(FakeREST([FILLED]))
    gw = _gateway(Mode.LIVE, router=lambda i, o: None, live=ex)
    sub = gw.submit(_intent())
    res = await gw.place_live(_intent(), sub.order)
    assert res.decision is Decision.LIVE_FILLED and res.order.status == "FILLED" and res.order.avg_price == 100.3 and res.order.filled == 10
    assert res.order.slippage_bps == pytest.approx(30.0) and res.meta["via"] == "poll" and res.meta["price_sent"] == 100.1
    assert gw.stats()["live_pending"] == 0 and gw.orders_today == 1 and gw.consecutive_rejects == 0


async def test_place_live_exit_failure_raises_and_counts_toward_the_breaker():
    ex, _ = _exec(FakeREST([PENDING] * 12))
    gw = _gateway(Mode.LIVE, router=lambda i, o: None, live=ex, caps=LiveCaps(breaker_consecutive_rejects=1))
    intent = _intent(Purpose.EXIT, OrderSide.SELL)
    sub = gw.submit(intent)
    with pytest.raises(VenueError):
        await gw.place_live(intent, sub.order)
    assert sub.order.status == "FAILED" and gw.breaker_tripped and gw.stats()["live_pending"] == 0


# -- the runner's live overlay ------------------------------------------------------------------------------


async def test_the_book_carries_the_broker_fills_beside_the_rule_prices(tmp_path):
    """A live-mode runner: the entry and the exit go out as SUBMITTED, the book is scored on the rule's prices,
    and the broker's fills land beside them with slippage and a live net."""
    store = ts.BarStore()
    ts._seed_daily(store, "ABC", "1")
    r = ts._runner(tmp_path, store)
    routed: list[tuple[OrderIntent, Order]] = []
    r.gateway = Gateway(matcher=PaperMatcher(COSTS), mode=lambda: Mode.LIVE, halted=lambda: (False, ""), book_for=lambda _c: None,
                        ltp_for=lambda _c: None, live=object(), router=lambda i, o: routed.append((i, o)))
    await ts._feed(r, store, ts.DAY_BARS)
    snap = r.snapshot()
    assert snap["summary"]["taken"] == 1 and snap["summary"]["closed"] == 1 and len(routed) == 2
    entry_intent, entry_order = routed[0]
    exit_intent, exit_order = routed[1]
    assert entry_intent.purpose is Purpose.ENTRY and "|E|" in entry_intent.client_order_id and len(entry_intent.client_order_id) <= 38
    assert exit_intent.purpose is Purpose.EXIT and "|X|" in exit_intent.client_order_id and len(exit_intent.client_order_id) <= 38
    trade = snap["trades"][0]
    assert trade["live"]["status"] == "PENDING" and trade["live_exit"]["status"] == "PENDING"
    rule_entry, rule_exit, qty = trade["entry"], trade["exit"], trade["qty"]
    # the broker fills the entry a little worse, the exit a little worse: both recorded, the book's prices untouched
    r.on_live_result(entry_intent.client_order_id, OrderResult(Decision.LIVE_FILLED, entry_order, Fill(price=rule_entry + 0.1, qty=qty, ts=1.0, charges=20.0), meta={"via": "event", "latency_ms": 120.0}))
    r.on_live_result(exit_intent.client_order_id, OrderResult(Decision.LIVE_FILLED, exit_order, Fill(price=rule_exit - 0.1, qty=qty, ts=2.0, charges=20.0), meta={"via": "poll"}))
    await asyncio.gather(*r._tasks)  # type: ignore[attr-defined]
    t = r.snapshot()["trades"][0]
    assert t["entry"] == rule_entry and t["exit"] == rule_exit
    assert t["live"]["status"] == "FILLED" and t["live"]["price"] == pytest.approx(rule_entry + 0.1) and t["live"]["latency_ms"] == 120.0
    assert t["live"]["slippage_bps"] == pytest.approx(0.1 / rule_entry * 1e4, rel=1e-3) and t["live_exit"]["slippage_bps"] > 0
    assert t["live_net_inr"] == pytest.approx((rule_exit - 0.1 - rule_entry - 0.1) * qty - 40.0)
    assert r.snapshot()["summary"]["live"] == {"pending": 0, "filled": 1, "unknown": 0, "rejected": 0, "exit_filled": 1, "exit_failed": 0, "net_inr": t["live_net_inr"]}
    assert (r.dir / f"{r.day}.json").exists()


async def test_an_entry_the_broker_refused_leaves_no_position(tmp_path):
    store = ts.BarStore()
    ts._seed_daily(store, "ABC", "1")
    r = ts._runner(tmp_path, store)
    routed: list[tuple[OrderIntent, Order]] = []
    r.gateway = Gateway(matcher=PaperMatcher(COSTS), mode=lambda: Mode.LIVE, halted=lambda: (False, ""), book_for=lambda _c: None,
                        ltp_for=lambda _c: None, live=object(), router=lambda i, o: routed.append((i, o)))
    # feed bar by bar up to the entry: the position is open and pending when the refusal arrives
    for i, (o, h, lo, c) in enumerate(ts.DAY_BARS):
        b = ts._ubar("ABC", "1", "5m", ts.OPEN + 300 * i, o, h, lo, c)
        store.close(b)
        await r.on_bar(b)
        if r.positions:
            break
    await asyncio.gather(*r._tasks)  # type: ignore[attr-defined]
    assert len(r.positions) == 1 and routed
    intent, order = routed[0]
    order.status, order.note = "REJECTED", "RMS: insufficient margin"
    r.on_live_result(intent.client_order_id, OrderResult(Decision.REJECTED_BROKER, order))
    await asyncio.gather(*r._tasks)  # type: ignore[attr-defined]
    assert not r.positions and not r.plans and r.live_rejected == 1
    sig = r.snapshot()["signals"][0]
    assert not sig["taken"] and "insufficient margin" in sig["why"] and r.snapshot()["summary"]["live"]["rejected"] == 1


def test_a_pending_order_at_restart_is_unknown_until_reconciled(tmp_path):
    store = ts.BarStore()
    r = ts._runner(tmp_path, store)
    r.day = "2026-01-05"
    r.positions["pos-1"] = {"id": "pos-1", "symbol": "ABC", "side": "LONG", "qty": 10, "entry": 100.0, "opened_ts": 1, "live": {"status": "PENDING", "client_order_id": "x"}, "live_exit": None, "pnl_bps": 0.0}
    r.signals, r.trades, r.plans = [], [], {}
    r._dump(r.snapshot(), r.day)
    r2 = ts._runner(tmp_path, store)
    r2._roll(__import__("datetime").date.fromisoformat("2026-01-05"))
    assert r2.positions["pos-1"]["live"]["status"] == "UNKNOWN"


# -- the socket --------------------------------------------------------------------------------------------


async def test_order_frames_go_to_the_order_handler_and_ticks_still_tick():
    s = Settings(_env_file=None, fp_client_code="50000001")
    ticks: list = []
    orders: list = []

    async def on_tick(row):
        ticks.append(row)

    async def on_order(row):
        orders.append(row)

    feed = FivePaisaFeed(s, None, on_tick=on_tick, on_order=on_order)  # type: ignore[arg-type]
    await feed._dispatch({"ReqType": "P", "ReqStatus": 0, "RemoteOrderId": "x", "Status": "Placed", "ScripCode": 2885})
    await feed._dispatch({"Token": 2885, "LastRate": 101.5, "TotalQty": 500})
    assert len(orders) == 1 and len(ticks) == 1 and feed.health.orders == 1 and feed.health.ticks == 1
    assert build_order_frame("50000001") == '{"Method":"OrderTradeConfirmations","Operation":"Subscribe","ClientCode":"50000001"}'
    base = "wss://openfeed.5paisa.com/Feeds/api/chat?Value1="
    assert ws_url_for(base, "A") == "wss://aopenfeed.5paisa.com/Feeds/api/chat?Value1="
    assert ws_url_for(base, "b") == "wss://bopenfeed.5paisa.com/Feeds/api/chat?Value1="
    assert ws_url_for(base, "O") == base and ws_url_for(base, "") == base and ws_url_for(base, None) == base


# -- the reconciler ----------------------------------------------------------------------------------------


async def test_the_reconciler_reads_the_engine_positions_in_broker_terms():
    rest = FakeREST(positions=[{"scrip_code": "2885", "exch": "N", "exch_type": "C", "net_qty": 10, "symbol": "RELIANCE"}])
    rc = Reconciler(rest)  # type: ignore[arg-type]
    assert (await rc.run([LocalPosition("2885", "RELIANCE", 10)])).clean and not rc.frozen
    rep = await rc.run([LocalPosition("2885", "RELIANCE", 5)])
    assert rep.mismatches[0].kind is MismatchKind.QTY_MISMATCH and rc.frozen and rc.stats()["runs"] == 2
    rep = await rc.run([])
    assert rep.mismatches[0].kind is MismatchKind.ORPHAN


# -- the engine's switch -----------------------------------------------------------------------------------


async def test_arming_names_every_blocker(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path, engine_enabled=False, strategy_enabled=True)
    e = Engine(s)
    await e.start()
    try:
        with pytest.raises(ValueError, match="not routed") as exc:
            await e.set_mode(Mode.LIVE_CAPPED, armed_minutes=30)
        assert "KN_LIVE_ROUTE_ENABLED" in str(exc.value) and e.mode() is Mode.SHADOW
        st = e.live_status()
        assert not st["ready"] and any("no live executor" in b for b in st["blockers"]) and st["route_enabled"] is False
        assert e.health_snapshot()["live"]["ready"] is False
    finally:
        await e.stop()


async def test_the_route_switch_alone_does_not_arm(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path, engine_enabled=False, strategy_enabled=True, live_route_enabled=True, live_static_ip="1.2.3.4")
    e = Engine(s)
    await e.start()
    try:
        with pytest.raises(ValueError) as exc:
            await e.set_mode(Mode.LIVE, armed_minutes=5)
        msg = str(exc.value)
        assert "no live executor" in msg and "static IP" in msg and "KN_LIVE_ROUTE_ENABLED" not in msg
    finally:
        await e.stop()


# -- the archive -------------------------------------------------------------------------------------------


def test_order_frames_are_archived_raw(tmp_path):
    a = DailyArchive(tmp_path / "archive")
    a.order({"ReqType": "T", "RemoteOrderId": "x", "Status": "Fully Executed", "ScripCode": 2885, "Price": 100.05}, 1_762_000_000.0)
    assert a.flush() == 1
    import pandas as pd

    df = pd.read_parquet(next((tmp_path / "archive" / "orders").glob("*.parquet")))
    assert df.iloc[0]["remote_id"] == "x" and df.iloc[0]["req_type"] == "T" and '"Price": 100.05' in df.iloc[0]["raw"]
