"""A 5paisa stand-in for the live order manager (exec/live_orders.py) — no network, no money.

Orders live in a dict keyed by the RemoteOrderID the engine sent. ``book`` (a callback to the
engine's own book: ``code -> (bid, ask, ltp)``) makes the fake fill LIKE THE PAPER MATCHER
(exec/resting.py ``fills`` / ``touch_fills``, price improvement at placement), which is what the
parity test needs; without it, orders fill only when a test scripts it (``fill``). Every broker
behaviour the manager must survive can be switched on: a placement refusal, a missing
RemoteOrderID echo, no exchange id until the first status, stale status rows, a modify the broker
ignores, a partial fill, a rejection after acceptance.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Sequence
from typing import Any

from kotsin_nse.domain import OrderSide
from kotsin_nse.exec.resting import fills, touch_fills
from kotsin_nse.venue.base import VenueError

_TARGET = re.compile(r"-T\d+V\d+-")


class FakeBroker:
    FINAL = ("Fully Executed", "Cancelled", "Rejected")
    def __init__(self, book: Callable[[str], tuple[float | None, float | None, float | None]] | None = None, *,
                 echo_remote_id: bool = True, exch_id_at_place: bool = True, margin: float = 5_000_000.0) -> None:
        self.book = book
        self.echo_remote_id = echo_remote_id
        self.exch_id_at_place = exch_id_at_place
        self.margin_avail = margin
        self.orders: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []
        self.reject_next_place: str | None = None
        self.ignore_modify = False
        #: cancels are accepted but take effect only on ``release_cancels`` (a broker slow to confirm)
        self.hold_cancels = False
        self._held_cancels: list[str] = []
        #: remote ids the status call does not answer for (only the order book knows them)
        self.status_silent: set[str] = set()
        #: remote ids whose status row is frozen at its last value (a stale answer)
        self.stale: set[str] = set()
        self._stale_rows: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1_000_000_001)
        #: per contract, the most SELL quantity ever working at once (the naked-short check)
        self.peak_sell_working: dict[str, int] = {}

    # -- the REST surface the manager uses ------------------------------------------------------------

    async def place_order_raw(self, instrument: Any, side: OrderSide, qty: int, *, price: float = 0.0, intraday: bool = True,
                              remote_order_id: str) -> dict[str, Any]:
        self.calls.append(("place", remote_order_id, side.value, int(qty), float(price)))
        if self.reject_next_place is not None:
            msg, self.reject_next_place = self.reject_next_place, None
            raise VenueError(f"V1/PlaceOrderRequest: {msg}", raw={"Status": 1, "Message": msg})
        rid = remote_order_id[:38]
        exch_id = str(next(self._ids))
        o = {"rid": rid, "exch_id": exch_id, "code": instrument.scrip_code, "buy": side is OrderSide.BUY, "qty": int(qty),
             "rate": float(price), "traded": 0, "avg": 0.0, "status": "Pending", "seen_ltp": None, "target": bool(_TARGET.search(rid)),
             "exch_known": self.exch_id_at_place}
        self.orders[rid] = o
        if not o["buy"]:
            working = sum(x["qty"] - x["traded"] for x in self.orders.values()
                          if not x["buy"] and x["code"] == o["code"] and x["status"] not in self.FINAL)
            self.peak_sell_working[o["code"]] = max(self.peak_sell_working.get(o["code"], 0), working)
        self._match(o, first=True)
        return {"Status": 0, "Message": "Success", "BrokerOrderID": int(exch_id) - 500,
                "ExchOrderID": exch_id if self.exch_id_at_place else 0, "RemoteOrderID": rid if self.echo_remote_id else ""}

    async def order_status_many(self, orders: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
        self.calls.append(("status", tuple(rid for _, rid in orders)))
        rows = []
        for _exch, rid in orders:
            o = self.orders.get(rid[:38])
            if o is None or rid in self.status_silent:
                continue
            self._match(o)
            o["exch_known"] = True
            if rid in self.stale:
                rows.append(self._stale_rows.setdefault(rid, self._row(o)))
            else:
                rows.append(self._row(o))
        return rows

    async def order_book(self) -> list[dict[str, Any]]:
        self.calls.append(("book",))
        out = []
        for o in self.orders.values():
            self._match(o)
            o["exch_known"] = True
            out.append(self._row(o))
        return out

    async def modify_order(self, exch_order_id: str, *, price: float, qty: int) -> None:
        self.calls.append(("modify", exch_order_id, float(price), int(qty)))
        o = self._by_exch(exch_order_id)
        if o is None or o["status"] in ("Fully Executed", "Cancelled", "Rejected"):
            raise VenueError("V1/ModifyOrderRequest: order not open", raw={"Status": 1, "Message": "order not open"})
        if not self.ignore_modify:
            o["rate"] = float(price)

    async def cancel_order(self, exch_order_id: str) -> None:
        self.calls.append(("cancel", exch_order_id))
        o = self._by_exch(exch_order_id)
        if o is None:
            raise VenueError("V1/CancelOrderRequest: no such order", raw={"Status": 1, "Message": "no such order"})
        if self.hold_cancels:
            self._held_cancels.append(o["rid"])
            return
        self._cancel(o)

    def _cancel(self, o: dict[str, Any]) -> None:
        if o["status"] not in ("Fully Executed", "Rejected"):
            self._match(o)  # a fill that came first stands
            if o["status"] != "Fully Executed":
                o["status"] = "Cancelled"

    async def margin(self) -> dict[str, Any]:
        return {"NetAvailableMargin": self.margin_avail}

    # -- test controls ---------------------------------------------------------------------------------

    def release_cancels(self) -> None:
        """The held cancels take effect now."""
        self.hold_cancels = False
        for rid in self._held_cancels:
            self._cancel(self.orders[rid])
        self._held_cancels.clear()

    def fill(self, rid: str, qty: int, price: float) -> None:
        """Script a (partial) fill: ``qty`` MORE traded at ``price`` (the average is kept)."""
        o = self.orders[rid[:38]]
        new = min(qty, o["qty"] - o["traded"])
        o["avg"] = (o["avg"] * o["traded"] + price * new) / (o["traded"] + new) if new else o["avg"]
        o["traded"] += new
        o["status"] = "Fully Executed" if o["traded"] >= o["qty"] else "Partially Executed"

    def reject(self, rid: str, why: str = "RMS: margin exceeds") -> None:
        o = self.orders[rid[:38]]
        o["status"], o["reason"] = "Rejected", why

    def order_for(self, prefix: str) -> dict[str, Any]:
        return next(o for rid, o in self.orders.items() if rid.startswith(prefix))

    # -- inner -----------------------------------------------------------------------------------------

    def _by_exch(self, exch_order_id: str) -> dict[str, Any] | None:
        return next((o for o in self.orders.values() if o["exch_id"] == str(exch_order_id)), None)

    def _row(self, o: dict[str, Any]) -> dict[str, Any]:
        return {"RemoteOrderID": o["rid"] if self.echo_remote_id else "", "ExchOrderID": o["exch_id"] if o["exch_known"] else 0,
                "Status": o["status"], "TradedQty": o["traded"],
                # an order that is over has nothing pending, as at the exchange
                "PendingQty": 0 if o["status"] in ("Fully Executed", "Cancelled", "Rejected") else o["qty"] - o["traded"], "OrderQty": o["qty"],
                "OrderRate": o["rate"], "AvgRate": round(o["avg"], 2) if o["traded"] else 0, "Reason": o.get("reason", "")}

    def _match(self, o: dict[str, Any], *, first: bool = False) -> None:
        """Fill it the paper matcher's way (only when a book was given)."""
        if self.book is None or o["status"] in ("Fully Executed", "Cancelled", "Rejected"):
            return
        bid, ask, ltp = self.book(o["code"])
        buy, limit = o["buy"], o["rate"]
        if limit <= 0:  # a market order takes the touch
            px = ask if buy else bid
            if px:
                self.fill(o["rid"], o["qty"] - o["traded"], px)
            return
        if o["target"]:
            hit = touch_fills(limit, bid, ltp)
        else:
            fresh = ltp if (not first and ltp is not None and ltp != o["seen_ltp"]) else None
            hit = fills(buy, limit, bid, ask, fresh)
        o["seen_ltp"] = ltp
        if not hit:
            return
        px = limit
        if first and buy and ask and ask < limit:
            px = ask
        elif first and not buy and bid and bid > limit:
            px = bid
        elif o["target"] and first and bid and bid > limit:
            px = max(limit, bid)
        self.fill(o["rid"], o["qty"] - o["traded"], px)
