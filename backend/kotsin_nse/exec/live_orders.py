"""Real LIMIT orders at the broker, worked the way the paper books work theirs (exec/resting.py).

Operator, 2026-09-27: "all fudkii strategies are live … each must have its dedicated orderbook" and
"build the live limit-order manager … ensure all strategies, parent, variant and twin are armed as
per their respective target and arm-logic". The engine decides WHERE an order rests and WHEN it is
repriced, crossed or given up exactly as it does on paper (engine.py ``_advance_one``); this module
is the other half — the broker's side of each order:

* ``place`` sends a LIMIT (or, with no price, a MARKET) order under the engine's readable client id
  (``RemoteOrderID``, the first 38 characters — venue/fivepaisa/rest.py ``REMOTE_ID_MAX``);
* ``refresh`` asks the broker, about once a second, what became of every working order — one
  ``V2/OrderStatus`` call for all of them, and the order book (``V4/OrderBook``) for any order the
  status call did not answer for;
* ``modify`` / ``cancel`` act on the broker's EXCHANGE order id, learned from the placement answer or
  the first status row that carries one — until then they are refused and the engine asks again on
  its next tick.

**The broker is the source of truth, and nothing is assumed.** A fill is what the broker reports:
``TradedQty`` and the average price, partial or whole. An order is over (``settled``) only when the
broker says so in words this module knows exactly — "Cancel Pending" is not a cancel (review
2026-09-28, A1) — with a traded quantity it can read, and a cancel only once nothing is left pending.
Until then it is WORKING: tracked, polled, never forgotten, and the engine sends no other SELL for its
position. Anything the module does not recognise (a status word, a missing quantity, a placement whose
answer was lost) keeps the order working and raises an alert (``on_alert``).

**What is assumed, not verified** (no real order has been sent through this path; each field name is
read defensively and listed in ``ASSUMED_FIELDS``): the placement answer's ``BrokerOrderID`` /
``ExchOrderID``; the status row's ``Status``, ``TradedQty``, ``PendingQty``, ``OrderQty``,
``OrderRate``/``Rate`` and an average price (``AvgRate``/``AveragePrice``/``AvgPrice``); the status
words (``FILLED_WORDS`` …); the order book's ``RemoteOrderID`` / ``ExchOrderID``; that
ModifyOrderRequest takes the order's full quantity (a partly filled order is therefore never modified).
"""

from __future__ import annotations

import asyncio
import collections
import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

from ..domain import OrderIntent, OrderSide
from ..market.session import ist_day
from ..venue.base import VenueError, never_sent

log = structlog.get_logger(__name__)

#: the broker's 38-character RemoteOrderID (venue/fivepaisa/rest.py)
REMOTE_ID_MAX = 38

#: every field of the broker's answers this module reads — confirm on the first armed session
ASSUMED_FIELDS: dict[str, tuple[str, ...]] = {
    "place.exchange_order_id": ("ExchOrderID", "ExchangeOrderID"),
    "place.broker_order_id": ("BrokerOrderID",),
    "place.remote_id_echo": ("RemoteOrderID",),
    "status.remote_id": ("RemoteOrderID",),
    "status.exchange_order_id": ("ExchOrderID", "ExchangeOrderID"),
    "status.status_text": ("Status", "OrderStatus"),
    "status.traded_qty": ("TradedQty",),
    "status.pending_qty": ("PendingQty",),
    "status.order_qty": ("OrderQty", "Qty"),
    "status.working_rate": ("OrderRate", "Rate"),
    "status.avg_price": ("AvgRate", "AveragePrice", "AvgPrice"),
    "status.reason": ("Reason", "Message", "RejectReason"),
}

#: The broker's words for an order that is OVER, matched EXACTLY (upper-cased, spaces collapsed, a
#: trailing full stop dropped). A word merely containing CANCEL or REJECT is not one of them: "Cancel
#: Pending", "Cancel Order Req Received" are an order still live at the exchange (review14 A1).
#: Only an explicit FULLY-filled word stands for the whole quantity when no traded quantity can be
#: read; a bare "Executed" / "Traded" could be a part — then the quantity is unknown (review14b).
FULL_FILL_WORDS = frozenset({"FULLY EXECUTED", "FULLY TRADED", "FULLY FILLED"})
FILLED_WORDS = FULL_FILL_WORDS | frozenset({"EXECUTED", "FILLED", "COMPLETE", "COMPLETED", "TRADED"})
CANCELLED_WORDS = frozenset({"CANCELLED", "CANCELED", "CANCELLED BY EXCHANGE", "CANCELLED BY USER", "ORDER CANCELLED", "EXPIRED"})
REJECTED_WORDS = frozenset({"REJECTED", "REJECTED BY 5P", "REJECTED BY EXCH", "REJECTED BY EXCHANGE", "RMS REJECTED", "ORDER REJECTED"})
#: words for a WORKING order this module knows; any word outside all four sets raises an alert
WORKING_WORDS = frozenset({
    "PENDING", "OPEN", "PLACED", "XMITTED", "TRANSMITTED", "MODIFIED", "EXCHANGE ORDER RECEIVED", "ORDER RECEIVED",
    "TRIGGER PENDING", "PARTIALLY EXECUTED", "PARTIALLY FILLED", "PARTIALLY TRADED", "CANCEL PENDING", "MODIFY PENDING",
})
FINAL_STATES = frozenset({"filled", "cancelled", "rejected"})


class RestLike(Protocol):
    async def place_order_raw(self, instrument: Any, side: OrderSide, qty: int, *, price: float = 0.0, intraday: bool = True,
                              remote_order_id: str) -> dict[str, Any]: ...
    async def order_status_many(self, orders: Sequence[tuple[str, str]]) -> list[dict[str, Any]]: ...
    async def order_book(self) -> list[dict[str, Any]]: ...
    async def modify_order(self, exch_order_id: str, *, price: float, qty: int) -> None: ...
    async def cancel_order(self, exch_order_id: str) -> None: ...


def _first(row: dict[str, Any], names: Iterable[str]) -> Any:
    for n in names:
        v = row.get(n)
        if v not in (None, "", 0, "0", 0.0):
            return v
    return None


def _qty(row: dict[str, Any], names: Iterable[str]) -> int | None:
    """A quantity the row CARRIES — 0 included — or None when no field of that name holds a number."""
    for n in names:
        if n in row:
            try:
                return int(float(row[n]))
            except (TypeError, ValueError):
                continue
    return None


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def status_words(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().rstrip(".").strip().upper()


def normalise_status(text: str, traded: int | None, qty: int, pending: int | None = None) -> tuple[str, bool]:
    """``(state, recognised)``: ``open`` | ``partial`` | ``filled`` | ``cancelled`` | ``rejected``.

    Over only on an exact final word: filled (or all of it traded, when the traded quantity is
    readable), cancelled, rejected. Whether what it traded can be READ is the caller's business
    (``_apply``: an order over with no readable traded quantity is never settled). ``pending`` is not
    required to be 0 for a cancel: a broker may keep the unfilled remainder there on a cancelled order.
    Everything else is still working — ``recognised`` False for a word this module does not know."""
    t = status_words(text)
    if t in REJECTED_WORDS:
        return "rejected", True
    if traded is not None and qty and traded >= qty:
        return "filled", True
    working = "partial" if (traded or "PARTIAL" in t) else "open"
    if t in FILLED_WORDS:
        if traded is None:
            return "filled", True  # over; the quantity is the caller's to judge (FULL_FILL_WORDS only)
        return working, False  # "executed" while the quantities say part of it: not over until they agree
    if t in CANCELLED_WORDS:
        return "cancelled", True
    return working, t in WORKING_WORDS


@dataclass(slots=True)
class BrokerOrder:
    """One order at the broker, as the broker last described it."""

    client_order_id: str
    remote_id: str
    exch: str
    qty: int
    side: str
    limit: float | None
    placed_ts: float
    exch_order_id: str = ""
    broker_order_id: str = ""
    state: str = "open"  # open | partial | filled | cancelled | rejected
    filled_qty: int = 0
    avg_price: float = 0.0
    reason: str = ""
    last_poll_ts: float = 0.0
    #: when the broker last answered for this order (a status row, or the order book)
    last_seen_ts: float = 0.0
    cancel_requested_ts: float | None = None
    #: consecutive cancels the broker refused (the engine alerts, and stops the book's entries, at N)
    cancel_failures: int = 0
    #: the price a modify asked for and the broker has not yet shown working
    modify_asked: float | None = None
    modify_asked_ts: float = 0.0
    modifies_ignored: int = 0
    #: the broker-filled quantity the engine has booked, and the charges booked for it (a restart
    #: books only the rest)
    booked_qty: int = 0
    charged: float = 0.0
    #: the placement's answer was lost (a transport error, not a refusal): the order may or may not be
    #: at the broker — tracked, and searched for, until the broker shows it (never taken as refused)
    unconfirmed: bool = False
    #: the broker's last row carried no readable traded quantity: nothing about it is final
    qty_unknown: bool = False
    #: the last row's status word was not one this module knows
    unknown_status: bool = False
    status_text: str = ""
    #: a fill whose average price the broker did not report (booked at the limit or the bid, flagged)
    avg_known: bool = True
    #: how many times the broker has answered for it (a status row or an order-book row)
    answers: int = 0
    #: when the placement actually left (the real clock at dispatch, not a tick's)
    sent_ts: float = 0.0
    #: consecutive successful reads — the status call AND a complete order-book read — that did not
    #: list an unconfirmed order (reset by any failed read)
    absent_reads: int = 0
    #: taken as never placed, then found at the broker after all: cancelled at once, never adopted
    resurrected: bool = False
    #: resolved as never having reached the broker (``resolve_never_placed``)
    never_placed: bool = False
    #: the engine's context, for a restart to adopt the order: what it was for
    kind: str = ""
    book: str = ""
    position_id: str = ""
    reason_code: str = ""
    symbol: str = ""
    scrip: str = ""
    rung: int = -1
    cross_n: int = 0
    rung_taken: bool = False
    #: the alerts already raised for this order (each once)
    alerted: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        """The broker says it is over."""
        return self.state in FINAL_STATES

    @property
    def settled(self) -> bool:
        """Over, with nothing unknown about it: what it filled can be booked and it can be forgotten."""
        return self.state in FINAL_STATES and not self.qty_unknown and not self.unconfirmed

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


class TaskLock:
    """An asyncio lock the task holding it may take again. One per position: every live SELL of it —
    the target sell, the exit, the cross, a supersede — is cancel-then-place under it, and those
    steps call one another (an exit takes the target off, whose fill rests the next rung …)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def __aenter__(self) -> TaskLock:
        me = asyncio.current_task()
        if self._owner is me and me is not None:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner, self._depth = me, 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class LiveOrderManager:
    #: a modify the broker has not shown working after this long is counted as ignored
    MODIFY_CONFIRM_S = 5.0
    #: an order the broker has not answered for in this long (an unconfirmed placement, or one that
    #: vanished from its status and its book) is alerted — and kept, and searched for
    UNSEEN_ALERT_S = 30.0
    #: ``auto_resolve`` only (off until 5paisa's RemoteOrderID echo and order book are verified with a
    #: real order): an unconfirmed placement is resolved never placed once ``NEVER_PLACED_AFTER_S`` have
    #: passed since it was SENT and ``NEVER_PLACED_READS`` consecutive successful reads — the status call
    #: by RemoteOrderID and a complete order-book read, both — have not listed it (review14c R3-1)
    NEVER_PLACED_READS = 3
    NEVER_PLACED_AFTER_S = 120.0
    #: an unconfirmed order is alerted at 30 s after it was sent, and every 60 s after
    UNCONFIRMED_ALERT_EVERY_S = 60.0

    def __init__(self, rest: RestLike, *, poll_interval_s: float = 1.0, book_fallback_s: float = 5.0,
                 store: Path | None = None, call_timeout_s: float = 5.0, auto_resolve: bool = False) -> None:
        self.rest = rest
        #: resolve unconfirmed placements never placed by themselves (``live_auto_resolve_unconfirmed``);
        #: off, only the operator's release does
        self.auto_resolve = auto_resolve
        self.call_timeout_s = call_timeout_s
        self.poll_interval_s = poll_interval_s
        self.book_fallback_s = book_fallback_s
        #: working and recently finished orders, by the engine's client id
        self.orders: dict[str, BrokerOrder] = {}
        #: the working orders, written on every change, so a restart knows what it left at the broker
        self.store = store
        #: ``(key, text)`` — the engine's alert channel (Telegram); set by the engine
        self.on_alert: Callable[[str, str], None] | None = None
        #: orders resolved never placed, by remote id — looked for on EVERY refresh, by status and in the
        #: order book, for the rest of their IST day, and written to the store (review14c R3-4)
        self.watch: dict[str, BrokerOrder] = {}
        #: orders that appeared after they were resolved never placed: tracked again, for the engine to take
        self.resurrected: list[BrokerOrder] = []
        self._last_book_ts = 0.0
        #: order-book reads: all of them, and the times of the recent ones (health: reads a minute)
        self.book_calls = 0
        self._book_times: collections.deque[float] = collections.deque(maxlen=600)
        self.placed = 0
        self.rejected = 0
        self.modified = 0
        self.cancelled = 0
        self.status_calls = 0
        self.errors = 0
        self.alerts = 0
        self.last_error = ""

    # -- place / modify / cancel -------------------------------------------------------------------

    async def place(self, intent: OrderIntent, *, limit: float | None, now: float, kind: str = "",
                    reason_code: str = "", rung: int = -1, cross_n: int = 0, rung_taken: bool = False) -> BrokerOrder:
        """Send the order. Raises ``VenueError`` when it certainly did not become an order at the
        broker (a refusal, no session, no connection) — nothing is tracked then. Any other failure
        (a timeout, a dropped answer, the task torn down mid-request, an error nobody foresaw) may have
        placed it: it is tracked as UNCONFIRMED and searched for — never dropped.

        The session comes FIRST, outside the send timeout: a re-login can wait half a minute for its
        TOTP window, and a timeout spent there — before the request left — used to leave an order
        "unconfirmed" that never existed, blocking its position's every SELL (review14b N1)."""
        inst = intent.instrument
        meta = {"kind": kind, "reason_code": reason_code, "rung": rung, "cross_n": cross_n, "rung_taken": rung_taken}
        ensure = getattr(self.rest, "ensure_session", None)
        if ensure is not None:
            try:
                await ensure()
            except VenueError:
                self.rejected += 1
                raise
            except Exception as exc:
                self.rejected += 1
                raise VenueError(f"no session: {exc}", maybe_sent=False) from exc
        unconfirmed, why = False, ""
        meta["sent_ts"] = time.time()  # the real clock at dispatch: every age of the order counts from here
        try:
            resp = await asyncio.wait_for(self.rest.place_order_raw(
                inst, intent.side, intent.qty, price=limit or 0.0, intraday=True, remote_order_id=intent.client_order_id,
            ), timeout=self.call_timeout_s)
        except VenueError as exc:
            if not getattr(exc, "maybe_sent", False):  # the broker said no, or nothing was sent (no session)
                self.rejected += 1
                raise
            resp, unconfirmed, why = {}, True, str(exc)
        except TimeoutError:
            resp, unconfirmed, why = {}, True, "timed out"
        except asyncio.CancelledError:
            # torn down mid-request (a shutdown): the order may be working at the broker — recorded, and
            # written to the store, before the task goes (review14b N5)
            self._record(intent, {}, limit, now, unconfirmed=True, why="cancelled mid-request", **meta)
            raise
        except Exception as exc:  # an error nobody foresaw: sent or not, it is not known
            if never_sent(exc):
                self.rejected += 1
                raise VenueError(f"not sent: {exc}", maybe_sent=False) from exc
            resp, unconfirmed, why = {}, True, f"{type(exc).__name__}: {exc}"
        return self._record(intent, resp if isinstance(resp, dict) else {}, limit, now, unconfirmed=unconfirmed, why=why, **meta)

    def _record(self, intent: OrderIntent, resp: dict[str, Any], limit: float | None, now: float, *, unconfirmed: bool, why: str,
                kind: str, reason_code: str, rung: int, cross_n: int, rung_taken: bool, sent_ts: float) -> BrokerOrder:
        inst = intent.instrument
        self.placed += 1
        bo = BrokerOrder(
            client_order_id=intent.client_order_id,
            remote_id=str(_first(resp, ASSUMED_FIELDS["place.remote_id_echo"]) or intent.client_order_id[:REMOTE_ID_MAX]),
            exch=inst.exch, qty=int(intent.qty), side=intent.side.value, limit=limit, placed_ts=sent_ts, sent_ts=sent_ts,
            exch_order_id=str(_first(resp, ASSUMED_FIELDS["place.exchange_order_id"]) or ""),
            broker_order_id=str(_first(resp, ASSUMED_FIELDS["place.broker_order_id"]) or ""),
            kind=kind, book=intent.strategy, position_id=intent.position_id or "", reason_code=reason_code, symbol=inst.symbol,
            scrip=inst.scrip_code, rung=rung, cross_n=cross_n, rung_taken=rung_taken, unconfirmed=unconfirmed, raw=dict(resp),
        )
        self.orders[bo.client_order_id] = bo
        self._save()
        if unconfirmed:
            log.error("live.place_unconfirmed", cid=bo.client_order_id, error=why[:160])
        log.info("live.order_placed", cid=bo.client_order_id, remote=bo.remote_id, exch_id=bo.exch_order_id or "—",
                 side=bo.side, qty=bo.qty, limit=limit, unconfirmed=unconfirmed)
        return bo

    async def modify(self, bo: BrokerOrder, price: float, now: float) -> bool:
        """Move a working order's price. False — and nothing sent — unless the broker shows the order,
        in a status read in this same step, working and untouched: no fill (whether ModifyOrderRequest's
        ``Qty`` is the total or the remainder is not verified; read the other way it would resize a
        partly filled order), a readable traded quantity, a status word it knows (review14 B1)."""
        if (bo.terminal or bo.cancel_requested_ts is not None or not bo.exch_order_id or bo.filled_qty > 0
                or bo.qty_unknown or bo.unconfirmed):
            return False
        answers = bo.answers
        await self.refresh(now, [bo], force=True)
        if bo.answers == answers:  # no answer in this step: nothing is sent on an old one
            return False
        if bo.state != "open" or bo.filled_qty > 0 or bo.qty_unknown or bo.unknown_status:
            return False
        try:
            await asyncio.wait_for(self.rest.modify_order(bo.exch_order_id, price=price, qty=bo.qty), timeout=self.call_timeout_s)
        except Exception as exc:  # noqa: BLE001 — any failure: not modified, asked again next time
            self.errors += 1
            self.last_error = f"modify {bo.client_order_id}: {exc}"[:200]
            log.warning("live.modify_failed", cid=bo.client_order_id, price=price, error=str(exc)[:120])
            return False
        self.modified += 1
        bo.modify_asked, bo.modify_asked_ts = price, now
        return True

    async def cancel(self, bo: BrokerOrder, now: float) -> bool:
        """Ask the broker to cancel. True when the request went (the cancel is confirmed by a later
        status, never assumed); False when it cannot be sent yet or the broker refused it — counted in
        ``cancel_failures``."""
        if bo.terminal:
            return True
        if not bo.exch_order_id:
            return False
        try:
            await asyncio.wait_for(self.rest.cancel_order(bo.exch_order_id), timeout=self.call_timeout_s)
        except Exception as exc:  # noqa: BLE001 — any failure: not cancelled, asked again
            self.errors += 1
            bo.cancel_failures += 1
            self.last_error = f"cancel {bo.client_order_id}: {exc}"[:200]
            log.warning("live.cancel_failed", cid=bo.client_order_id, failures=bo.cancel_failures, error=str(exc)[:120])
            self._save()
            return False
        self.cancelled += 1
        bo.cancel_failures = 0
        bo.cancel_requested_ts = now
        self._save()
        return True

    # -- what the broker says ------------------------------------------------------------------------

    async def refresh(self, now: float, orders: Iterable[BrokerOrder] | None = None, *, force: bool = False) -> None:
        """One status call — by RemoteOrderID — for every unsettled order due a look and, on the
        exit loop's own refresh (``orders`` None), every order in the late-appearance watch; then the
        order book for any the status call did not answer for (at once for an unconfirmed one) and for
        the watch. A failed call changes nothing: the orders keep what they had. Rows are matched by
        RemoteOrderID or by exchange order id — never by their position in the answer (review14 B5).

        An UNCONFIRMED placement (its answer lost) stays tracked and blocks its position's SELLs; it is
        alerted at 30 s after it was sent and every 60 s after. Only with ``auto_resolve`` is it
        resolved never placed by itself: ``NEVER_PLACED_AFTER_S`` since it was SENT and
        ``NEVER_PLACED_READS`` consecutive reads in which BOTH the status call and a complete order
        book omit it (review14c R3-1). Otherwise only the operator's release does."""
        pool = list(orders) if orders is not None else list(self.orders.values())
        due = [bo for bo in pool if not bo.settled and (force or now - bo.last_poll_ts >= self.poll_interval_s)]
        self._expire_watch()
        watched = [bo for bo in self.watch.values() if force or now - bo.last_poll_ts >= self.poll_interval_s] if orders is None else []
        if not due and not watched:
            return
        for bo in due + watched:
            bo.last_poll_ts = now
        self.status_calls += 1
        try:
            rows = await asyncio.wait_for(self.rest.order_status_many([(bo.exch, bo.remote_id) for bo in due + watched]),
                                          timeout=self.call_timeout_s)
        except Exception as exc:  # noqa: BLE001 — no answer: the orders keep what they had
            self.errors += 1
            self.last_error = f"status: {exc}"[:200]
            log.warning("live.status_failed", orders=len(due) + len(watched), error=str(exc)[:120])
            for bo in due:
                bo.absent_reads = 0  # not a successful read: the streak starts again
            self._alert_unconfirmed(due)
            return
        rows = rows or []
        self._check_watch(rows, now)
        missing = self._match(due, rows, now)
        unconfirmed = [bo for bo in missing if bo.unconfirmed]
        # the whole order book at most every ``book_fallback_s`` (``live_order_book_every_s``), whatever is
        # missing, unconfirmed or watched: the status call by RemoteOrderID goes every refresh, the book —
        # a payload that grows all day, on the account's shared rate limit — does not (review14d)
        book_due = now - self._last_book_ts >= self.book_fallback_s
        if (unconfirmed or watched or missing) and book_due:
            self._last_book_ts = now
            self.book_calls += 1
            self._book_times.append(time.time())
            try:
                book = await asyncio.wait_for(self.rest.order_book(), timeout=self.call_timeout_s)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"order book: {exc}"[:200]
                book = None
            if book is None:
                for bo in unconfirmed:
                    bo.absent_reads = 0
            else:
                self._check_watch(book, now)
                missing = self._match(missing, book, now, strays=False)
                for bo in [b for b in missing if b.unconfirmed]:
                    bo.absent_reads += 1  # neither the status call nor the whole order book lists it
                    if (self.auto_resolve and bo.absent_reads >= self.NEVER_PLACED_READS
                            and time.time() - (bo.sent_ts or bo.placed_ts) >= self.NEVER_PLACED_AFTER_S):
                        self.resolve_never_placed(bo, f"absent from {bo.absent_reads} status and order-book reads running, "
                                                      f"{time.time() - (bo.sent_ts or bo.placed_ts):.0f} s after it was sent")
        self._alert_unconfirmed([bo for bo in missing if bo.unconfirmed])
        for bo in missing:
            if not bo.unconfirmed and not bo.never_placed and now - max(bo.last_seen_ts, bo.placed_ts) >= self.UNSEEN_ALERT_S:
                self.alert(bo, "unseen", f"⚠️ LIVE order {bo.client_order_id}: the broker has stopped answering for it for "
                                         f"{now - max(bo.last_seen_ts, bo.placed_ts):.0f} s — still tracked; no other SELL for its "
                                         f"position until it is found")
        self._save()

    def _alert_unconfirmed(self, orders: list[BrokerOrder]) -> None:
        """LOUD, at 30 s after it was sent and every 60 s after, until it is found or released."""
        for bo in orders:
            if not bo.unconfirmed:
                continue
            age = time.time() - (bo.sent_ts or bo.placed_ts)
            if age < self.UNSEEN_ALERT_S:
                continue
            window = int((age - self.UNSEEN_ALERT_S) // self.UNCONFIRMED_ALERT_EVERY_S)
            self.alert(bo, f"unconfirmed:{window}",
                       f"🚨 unconfirmed {bo.client_order_id} ({bo.side} {bo.qty} {bo.symbol}, sent {age:.0f} s ago) — check the "
                       f"broker's order book; release with POST /control/live-order/{bo.client_order_id}/release if it is not there")

    def resolve_never_placed(self, bo: BrokerOrder, why: str) -> None:
        """An unconfirmed placement taken as never having reached the broker: over (rejected, nothing
        filled), so it blocks nothing — and WATCHED: looked for on every refresh, by status and in the
        order book, for the rest of its IST day, and written to the store (review14c R3-4)."""
        bo.state, bo.reason, bo.never_placed = "rejected", f"never reached the broker — {why}", True
        bo.unconfirmed = bo.qty_unknown = False
        self.watch[bo.remote_id] = bo
        self.alert(bo, "never-placed", f"⚠️ LIVE order {bo.client_order_id} taken as NEVER PLACED ({why}) — its position's "
                                       f"orders go on; it is watched for the rest of the day and cancelled if it shows up. "
                                       f"Live entries into {bo.symbol} {bo.scrip} are blocked for the rest of the day while it is "
                                       f"watched — lift with POST /control/live-order/{bo.client_order_id}/unwatch")
        self._save()

    def _expire_watch(self) -> None:
        today = ist_day(time.time())
        for rid in [r for r, bo in self.watch.items() if ist_day(bo.sent_ts or bo.placed_ts) != today]:
            self.watch.pop(rid, None)

    def _check_watch(self, rows: list[dict[str, Any]], now: float) -> None:
        """A watched order (taken as never placed) that a status row or the order book lists after all:
        tracked again as RESURRECTED — the engine cancels it at once and books what it filled."""
        if not self.watch:
            return
        by_exch = {bo.exch_order_id: bo for bo in self.watch.values() if bo.exch_order_id}
        for row in rows:
            rid = str(_first(row, ASSUMED_FIELDS["status.remote_id"]) or "")
            xid = str(_first(row, ASSUMED_FIELDS["status.exchange_order_id"]) or "")
            bo = self.watch.get(rid) if rid else None
            if bo is None and xid:
                bo = by_exch.get(xid)
            if bo is None:
                continue
            self.watch.pop(bo.remote_id, None)
            bo.never_placed, bo.resurrected, bo.state, bo.reason = False, True, "open", ""
            self._apply(bo, row, now)
            self.orders[bo.client_order_id] = bo
            self.resurrected.append(bo)
            self.alert(bo, "resurrected", f"🚨 LIVE order {bo.client_order_id} taken as never placed IS at the broker "
                                          f"({bo.status_text!r}, traded {bo.filled_qty}) — cancelled at once")
        self._save()

    def _match(self, due: list[BrokerOrder], rows: list[dict[str, Any]], now: float, *, strays: bool = True) -> list[BrokerOrder]:
        by_remote = {bo.remote_id: bo for bo in due}
        by_exch = {bo.exch_order_id: bo for bo in due if bo.exch_order_id}
        answered: set[str] = set()
        for row in rows:
            rid = str(_first(row, ASSUMED_FIELDS["status.remote_id"]) or "")
            xid = str(_first(row, ASSUMED_FIELDS["status.exchange_order_id"]) or "")
            bo = by_remote.get(rid) if rid else None
            if bo is None and xid:
                bo = by_exch.get(xid)
            if bo is None:
                if strays and not rid and not xid:
                    log.warning("live.status_row_unmatched", row=str(row)[:200])
                continue
            self._apply(bo, row, now)
            answered.add(bo.client_order_id)
        return [bo for bo in due if bo.client_order_id not in answered]

    def _apply(self, bo: BrokerOrder, row: dict[str, Any], now: float) -> None:
        bo.raw = dict(row)
        bo.last_seen_ts = now
        bo.answers += 1
        bo.unconfirmed = False
        exch_id = _first(row, ASSUMED_FIELDS["status.exchange_order_id"])
        if exch_id:
            bo.exch_order_id = str(exch_id)
        traded = _qty(row, ASSUMED_FIELDS["status.traded_qty"])
        pending = _qty(row, ASSUMED_FIELDS["status.pending_qty"])
        text = str(_first(row, ASSUMED_FIELDS["status.status_text"]) or "")
        state, known = normalise_status(text, traded, bo.qty, pending)
        bo.status_text, bo.unknown_status = text, not known
        if not known:
            self.alert(bo, f"status:{status_words(text)}:{traded}:{pending}",
                       f"⚠️ LIVE order {bo.client_order_id}: the broker says {text!r} (traded {traded}, pending {pending}) — "
                       f"not a state this engine knows as final: the order is taken as still working")
        if traded is None:
            if state == "filled" and status_words(text) in FULL_FILL_WORDS:
                traded = bo.qty  # "fully executed": all of it — no other word is taken for the whole quantity
                bo.qty_unknown = False
            else:
                bo.qty_unknown = True
                self.alert(bo, "qty-unknown", f"⚠️ LIVE order {bo.client_order_id}: no traded quantity in the broker's answer "
                                              f"({text!r}) — nothing more is sold for its position until it is known")
        else:
            bo.qty_unknown = False
        if traded is not None:
            bo.filled_qty = max(bo.filled_qty, min(traded, bo.qty))  # a broker quantity never goes backwards
        if bo.filled_qty > 0:
            px = _num(_first(row, ASSUMED_FIELDS["status.avg_price"]))
            if px > 0:
                bo.avg_price, bo.avg_known = px, True
            else:
                bo.avg_known = False
                if bo.avg_price <= 0 and bo.limit:
                    bo.avg_price = bo.limit  # a limit order fills at its limit or better: booked there, provisional
        working = _num(_first(row, ASSUMED_FIELDS["status.working_rate"]))
        if bo.modify_asked is not None:
            if working > 0 and abs(working - bo.modify_asked) < 1e-6:
                bo.limit, bo.modify_asked = bo.modify_asked, None
            elif now - bo.modify_asked_ts >= self.MODIFY_CONFIRM_S:
                bo.modifies_ignored += 1
                log.warning("live.modify_not_seen", cid=bo.client_order_id, asked=bo.modify_asked, working=working or None)
                bo.modify_asked = None
                if working > 0:
                    bo.limit = working
        if state in FINAL_STATES and bo.state != state:
            log.info("live.order_" + state, cid=bo.client_order_id, filled=bo.filled_qty, of=bo.qty, avg=bo.avg_price or None,
                     reason=str(_first(row, ASSUMED_FIELDS["status.reason"]) or "")[:120], qty_known=not bo.qty_unknown)
        bo.state = state
        bo.reason = str(_first(row, ASSUMED_FIELDS["status.reason"]) or bo.reason)

    def alert(self, bo: BrokerOrder, key: str, text: str) -> None:
        """Once per order and key: the log, and the engine's alert channel."""
        if key in bo.alerted:
            return
        bo.alerted.append(key)
        del bo.alerted[:-20]
        self.alerts += 1
        log.error("live.alert", cid=bo.client_order_id, key=key, text=text[:300])
        if self.on_alert is not None:
            try:
                self.on_alert(f"live:{bo.client_order_id}:{key}", text)
            except Exception as exc:  # noqa: BLE001 — an alert must never cost the order path
                log.warning("live.alert_failed", error=str(exc)[:120])

    def mark_booked(self, bo: BrokerOrder, qty: int, charged: float | None = None) -> None:
        """What the engine has booked of this order — written at once (a restart books only the rest)."""
        bo.booked_qty = int(qty)
        if charged is not None:
            bo.charged = round(charged, 2)
        self._save()

    def forget(self, bo: BrokerOrder) -> None:
        """A settled order the engine has booked: out of the working set."""
        self.orders.pop(bo.client_order_id, None)
        self._save()

    def working(self) -> list[BrokerOrder]:
        """Every order not settled: working, or over with something about it unknown."""
        return [bo for bo in self.orders.values() if not bo.settled]

    def working_for(self, position_id: str) -> list[BrokerOrder]:
        """The SELLs of one position the broker may still be working — and the settled ones whose fill
        is not booked yet: until it is, the position's quantity is not what the broker holds, and no
        new SELL may be sized on it (review14d R4-1b)."""
        return [bo for bo in self.orders.values() if bo.position_id == position_id and bo.side == OrderSide.SELL.value
                and (not bo.settled or bo.filled_qty > bo.booked_qty)]

    # -- restart -------------------------------------------------------------------------------------

    def _save(self) -> None:
        if self.store is None:
            return
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps({"orders": [bo.to_json() for bo in self.orders.values()],
                                       "watch": [bo.to_json() for bo in self.watch.values()]}))
            tmp.replace(self.store)
        except OSError as exc:  # a failed save must never cost the order path
            self.errors += 1
            self.last_error = f"save: {exc}"[:200]

    def _read_store(self) -> dict[str, list[BrokerOrder]]:
        if self.store is None or not self.store.exists():
            return {"orders": [], "watch": []}
        try:
            data = json.loads(self.store.read_text())
        except (OSError, ValueError) as exc:
            log.error("live.orders_unreadable", error=str(exc)[:120])
            return {"orders": [], "watch": []}
        if isinstance(data, list):  # the store as it was written before the watch was kept in it
            data = {"orders": data, "watch": []}
        names = set(BrokerOrder.__dataclass_fields__)
        out: dict[str, list[BrokerOrder]] = {"orders": [], "watch": []}
        for part in out:
            for d in data.get(part) or []:
                d.pop("raw", None)
                try:
                    out[part].append(BrokerOrder(**{k: v for k, v in d.items() if k in names}))
                except TypeError:
                    continue
        return out

    def load_left_behind(self) -> list[BrokerOrder]:
        """The orders a previous run left at the broker (read once at boot)."""
        return self._read_store()["orders"]

    def unwatch(self, client_order_id: str) -> BrokerOrder | None:
        """Take an order out of the late-appearance watch (the operator's call): None if it is not watched."""
        bo = next((b for b in self.watch.values() if b.client_order_id == client_order_id), None)
        if bo is not None:
            self.watch.pop(bo.remote_id, None)
            self._save()
        return bo

    def restore_watch(self) -> int:
        """The late-appearance watch a previous run kept — today's only (read once at boot)."""
        for bo in self._read_store()["watch"]:
            self.watch.setdefault(bo.remote_id, bo)
        self._expire_watch()
        return len(self.watch)

    def stats(self) -> dict[str, Any]:
        return {
            "working": len(self.working()), "tracked": len(self.orders), "placed": self.placed, "rejected": self.rejected,
            "modified": self.modified, "cancelled": self.cancelled, "status_calls": self.status_calls,
            "modifies_ignored": sum(bo.modifies_ignored for bo in self.orders.values()),
            "unconfirmed": sum(1 for bo in self.orders.values() if bo.unconfirmed),
            "qty_unknown": sum(1 for bo in self.orders.values() if bo.qty_unknown),
            "unknown_status": sum(1 for bo in self.orders.values() if bo.unknown_status),
            "watched": sorted(bo.client_order_id for bo in self.watch.values()),
            "order_book_calls": self.book_calls,
            "order_book_per_min": sum(1 for t in self._book_times if time.time() - t <= 60.0),
            "alerts": self.alerts, "errors": self.errors, "last_error": self.last_error,
        }


__all__ = ["ASSUMED_FIELDS", "BrokerOrder", "LiveOrderManager", "TaskLock", "normalise_status", "status_words"]
