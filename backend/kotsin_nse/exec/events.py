"""Order and trade confirmations as 5paisa pushes them.

The broker streams them on the feed socket (``Method: OrderTradeConfirmations``, documented at
xstream.5paisa.com/dev-docs/order-tracking-system/web-socket-trade): one frame per request — ``ReqType``
``P`` place, ``M`` modify, ``C`` cancel, ``T`` trade, ``S`` stop-loss trigger — with ``ReqStatus`` 0/1 and our
``RemoteOrderId`` on every one. The executor waits on these first and polls ``V2/OrderStatus`` only as the
fallback, so a fill is known the moment the exchange reports it, and its latency is measured from the
frames the broker sent rather than from how often we asked.

Nothing here trusts a frame's shape: an unreadable field is a parse error counted, never an exception on
the socket's reader. The schema was read from the documentation on 2026-11-01 and has not yet been seen
on a real order — the first live order archives its raw frames (``archive/orders``) so the schema can be
confirmed and ``docs/FIVEPAISA_FACTS.md`` corrected.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

TERMINAL_OK = frozenset({"fully executed", "fully_executed", "executed", "filled", "complete", "completed"})
TERMINAL_CANCELLED = frozenset({"cancelled", "canceled", "expired", "ah cancelled"})
REJECTED_PREFIX = "rejected"

OnFrame = Callable[[dict[str, Any], float], None]


def is_order_frame(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and "ReqType" in row
        and any(k in row for k in ("RemoteOrderId", "RemoteOrderID", "BrokerOrderID", "BrokerOrderId"))
    )


def remote_id_of(row: dict[str, Any]) -> str:
    return str(row.get("RemoteOrderId") or row.get("RemoteOrderID") or "").strip()


def classify(status: str, *, req_type: str = "", req_status: int | None = None, traded: int = 0, qty: int = 0) -> str:
    """``filled`` | ``rejected`` | ``cancelled`` | ``partial`` | ``open`` from what the broker said."""
    s = (status or "").strip().lower()
    if req_type == "P" and req_status == 1:
        return "rejected"
    if s in TERMINAL_OK:
        return "filled"
    if s.startswith(REJECTED_PREFIX):
        return "rejected"
    if s in TERMINAL_CANCELLED:
        return "cancelled"
    if qty > 0 and traded >= qty:
        return "filled"
    if traded > 0:
        return "partial"
    return "open"


@dataclass(slots=True)
class OrderState:
    remote_id: str
    first_ts: float
    last_ts: float = 0.0
    frames: int = 0
    status: str = ""
    outcome: str = "open"
    req_type: str = ""
    req_status: int | None = None
    broker_order_id: str = ""
    exch_order_id: str = ""
    scrip_code: str = ""
    side: str = ""
    qty: int = 0
    traded_qty: int = 0
    pending_qty: int | None = None
    price: float = 0.0
    avg_price: float = 0.0
    traded_value: float = 0.0
    reason: str = ""
    ack_ts: float | None = None
    fill_ts: float | None = None

    @property
    def terminal(self) -> bool:
        return self.outcome in ("filled", "rejected", "cancelled")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class OrderEvents:
    def __init__(self, *, on_frame: OnFrame | None = None, clock: Callable[[], float] = time.time) -> None:
        self.states: dict[str, OrderState] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self.on_frame = on_frame
        self._clock = clock
        self.frames = 0
        self.unmatched = 0
        self.parse_errors = 0

    # -- ingest ----------------------------------------------------------------------------------

    def ingest(self, row: dict[str, Any], recv_ts: float | None = None) -> OrderState | None:
        ts = recv_ts if recv_ts is not None else self._clock()
        self.frames += 1
        if self.on_frame is not None:
            try:
                self.on_frame(row, ts)
            except Exception:  # noqa: BLE001 - the archive never blocks the order path
                pass
        rid = remote_id_of(row)
        if not rid:
            self.unmatched += 1
            return None
        st = self.states.get(rid)
        if st is None:
            st = OrderState(remote_id=rid, first_ts=ts)
            self.states[rid] = st
        try:
            self._apply(st, row, ts)
        except (TypeError, ValueError):
            self.parse_errors += 1
        ev = self._waiters.get(rid)
        if ev is not None:
            ev.set()
        return st

    @staticmethod
    def _apply(st: OrderState, row: dict[str, Any], ts: float) -> None:
        st.frames += 1
        st.last_ts = ts
        rt = str(row.get("ReqType") or "").strip().upper()
        if rt:
            st.req_type = rt
        rs = row.get("ReqStatus")
        if rs is not None and str(rs) != "":
            st.req_status = int(rs)
        bid = row.get("BrokerOrderID") or row.get("BrokerOrderId")
        if bid:
            st.broker_order_id = str(bid)
        eid = row.get("ExchOrderID") or row.get("ExchangeOrderID")
        if eid:
            st.exch_order_id = str(eid)
        if row.get("ScripCode"):
            st.scrip_code = str(row["ScripCode"])
        if row.get("BuySell"):
            st.side = str(row["BuySell"])
        q = row.get("OrderQty") or row.get("Qty")
        if q:
            st.qty = int(q)
        if rt == "T":
            total = int(row.get("TotalTradedQty") or row.get("TradedQty") or 0)
            px = float(row.get("Price") or 0)
            inc = max(0, total - st.traded_qty)
            if inc and px > 0:
                st.traded_value += inc * px
            st.traded_qty = max(st.traded_qty, total)
            if st.traded_qty > 0 and st.traded_value > 0:
                st.avg_price = st.traded_value / st.traded_qty
            if row.get("OrderPrice"):
                st.price = float(row["OrderPrice"])
        else:
            if row.get("Price"):
                st.price = float(row["Price"])
            if row.get("TradedQty") is not None and str(row.get("TradedQty")) != "":
                st.traded_qty = max(st.traded_qty, int(row["TradedQty"] or 0))
            if row.get("PendingQty") is not None and str(row.get("PendingQty")) != "":
                st.pending_qty = int(row["PendingQty"] or 0)
            if rt == "P" and st.ack_ts is None:
                st.ack_ts = ts
        status = str(row.get("Status") or row.get("OrderStatus") or "")
        if status:
            st.status = status
        remark = str(row.get("Remark") or row.get("Reason") or "")
        if remark:
            st.reason = remark
        st.outcome = classify(st.status, req_type=rt, req_status=st.req_status, traded=st.traded_qty, qty=st.qty)
        if st.outcome == "filled" and st.fill_ts is None:
            st.fill_ts = ts

    # -- wait ------------------------------------------------------------------------------------

    async def wait(self, remote_id: str, timeout_s: float) -> OrderState | None:
        """The order's state once a frame for it arrives, or after ``timeout_s`` seconds."""
        st = self.states.get(remote_id)
        if st is not None and st.terminal:
            return st
        ev = self._waiters.setdefault(remote_id, asyncio.Event())
        ev.clear()
        try:
            async with asyncio.timeout(max(0.0, timeout_s)):
                await ev.wait()
        except TimeoutError:
            pass
        return self.states.get(remote_id)

    def forget(self, remote_id: str) -> None:
        self._waiters.pop(remote_id, None)

    def stats(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "orders": len(self.states),
            "unmatched": self.unmatched,
            "parse_errors": self.parse_errors,
            "terminal": sum(1 for s in self.states.values() if s.terminal),
        }
