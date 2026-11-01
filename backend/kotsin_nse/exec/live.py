"""Real orders. The only file in the package that can lose money.

Place a **protected limit** (5paisa's API takes limit orders only), then wait for the broker's own
confirmation — the order/trade frames it pushes on the feed socket (``exec/events.py``) first, the
``V2/OrderStatus`` poll as the fallback — and report the **actual** fill. Never assume a placement is a
fill: a 200 from ``PlaceOrderRequest`` means the request was accepted, and RMS can still reject it a
second later.

Rules that came out of the old executor's incident log and the 2026-11-01 review:

* **The broker is the source of truth.** Local state is a cache. Everything here reports what the
  broker's frames or order book say, not what we hoped.
* **Never book a fill without a rate.** A status of "Fully Executed" with no ``AveragePrice`` yet is
  waited on, then read from the order book; it is not booked at the limit we sent.
* **An exit must fill, and a failed exit must be loud.** An exit unfilled after the timeout is chased
  (its limit widened by another protection band, ``chase_steps`` times); what is still unfilled is
  cancelled and the failure *raises*, so the engine alerts and the reconciler sees the real position.
  CAN2 swallowed ``te-CLOSE failed`` as a warning and its book and the broker's silently diverged.
* **An entry may miss.** Unfilled after the timeout it is cancelled; a partial fill is booked at its
  traded quantity (the exit must not sell shares the broker never bought).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..domain import Fill, OrderIntent, OrderSide, Purpose
from ..risk.costs import CostModel
from ..venue.base import VenueError
from ..venue.fivepaisa.rest import FivePaisaREST
from .events import OrderEvents, OrderState, classify
from .throttle import OrderThrottle

log = structlog.get_logger(__name__)

TouchFor = Callable[[OrderIntent], float | None]


@dataclass(slots=True)
class LiveResult:
    ok: bool
    order_id: str = ""
    fill: Fill | None = None
    error: str = ""
    status: str = ""
    partial: bool = False
    via: str = ""  # "event" | "poll" | "order_book"
    latency_ms: float | None = None  # placement → the broker's first frame for it
    fill_ms: float | None = None  # placement → the fill known
    price_sent: float = 0.0
    chased: int = 0
    throttle_wait_s: float = 0.0

    def meta(self) -> dict[str, Any]:
        return {
            "via": self.via,
            "latency_ms": self.latency_ms,
            "fill_ms": self.fill_ms,
            "price_sent": self.price_sent,
            "chased": self.chased,
            "partial": self.partial,
            "status": self.status,
            "throttle_wait_s": self.throttle_wait_s,
        }


@dataclass(slots=True)
class _Seen:
    """What the broker has told us about one order so far, from frames and polls alike."""

    status: str = ""
    outcome: str = "open"
    traded: int = 0
    pending: int | None = None
    avg_price: float = 0.0
    exch_order_id: str = ""
    broker_order_id: str = ""
    reason: str = ""
    ack_ts: float | None = None
    via: str = ""
    notes: list[str] = field(default_factory=list)


def parse_status_row(row: dict[str, Any], qty: int) -> _Seen:
    """One ``OrdStatusResLst`` / ``OrderBookDetail`` row (xstream dev-docs, 2026-11-01)."""
    s = _Seen()
    s.status = str(row.get("Status") or row.get("OrderStatus") or "")
    s.traded = int(row.get("TradedQty") or 0)
    pend = row.get("PendingQty")
    s.pending = int(pend) if pend is not None and str(pend) != "" else None
    # AveragePrice is the executed average; Rate / OrderRate is the limit we sent — never a fill price
    s.avg_price = float(row.get("AveragePrice") or row.get("AvgRate") or 0)
    s.exch_order_id = str(row.get("ExchOrderID") or row.get("ExchangeOrderID") or "")
    s.broker_order_id = str(row.get("BrokerOrderId") or row.get("BrokerOrderID") or "")
    s.reason = str(row.get("Reason") or row.get("Remark") or row.get("Message") or "")
    s.outcome = classify(s.status, traded=s.traded, qty=qty)
    s.via = "poll"
    return s


def _merge_state(s: _Seen, st: OrderState) -> None:
    s.status = st.status or s.status
    s.outcome = st.outcome if st.outcome != "open" else s.outcome
    s.traded = max(s.traded, st.traded_qty)
    s.pending = st.pending_qty if st.pending_qty is not None else s.pending
    if st.avg_price > 0:
        s.avg_price = st.avg_price
    s.exch_order_id = st.exch_order_id or s.exch_order_id
    s.broker_order_id = st.broker_order_id or s.broker_order_id
    s.reason = st.reason or s.reason
    s.ack_ts = st.ack_ts if s.ack_ts is None else s.ack_ts
    s.via = "event"


class LiveExecutor:
    def __init__(
        self,
        rest: FivePaisaREST,
        costs: CostModel,
        *,
        events: OrderEvents | None = None,
        throttle: OrderThrottle | None = None,
        touch_for: TouchFor | None = None,
        protect_entry_bps: float = 10.0,
        protect_exit_bps: float = 30.0,
        fill_timeout_s: float = 15.0,
        chase_steps: int = 2,
        poll_interval_s: float = 1.0,
        intraday: bool = True,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rest = rest
        self.costs = costs
        self.events = events
        self.throttle = throttle
        self.touch_for = touch_for
        self.protect_entry_bps = protect_entry_bps
        self.protect_exit_bps = protect_exit_bps
        self.fill_timeout_s = fill_timeout_s
        self.chase_steps = max(0, int(chase_steps))
        self.poll_interval_s = poll_interval_s
        self.intraday = intraday
        self._clock = clock
        self._sleep = sleep
        self.placed = 0
        self.filled = 0
        self.partial = 0
        self.rejected = 0
        self.unfilled = 0
        self.chased = 0
        self.via: dict[str, int] = {"event": 0, "poll": 0, "order_book": 0}
        self._latencies: list[float] = []
        self._fill_ms: list[float] = []

    # -- pricing -------------------------------------------------------------------------------

    def protect_bps(self, intent: OrderIntent, step: int = 0) -> float:
        base = self.protect_entry_bps if intent.purpose is Purpose.ENTRY else self.protect_exit_bps
        return base * (1 + step)

    def limit_price(self, intent: OrderIntent, touch: float, *, step: int = 0) -> float:
        """The touch moved ``protect`` bps through the book: a buy above the ask, a sell below the bid,
        rounded to the tick. It is a limit the market can fill now, not a price we are hoping for."""
        bps = self.protect_bps(intent, step)
        buy = intent.side is OrderSide.BUY
        px = touch * (1 + bps / 1e4) if buy else touch * (1 - bps / 1e4)
        inst = intent.instrument
        px = inst.round_price(px)
        return max(px, inst.tick_size if inst.tick_size > 0 else 0.05)

    def _touch(self, intent: OrderIntent) -> float:
        t = self.touch_for(intent) if self.touch_for is not None else None
        if not t or t <= 0:
            t = intent.ref_price or 0.0
        if not t or t <= 0:
            raise VenueError(f"{intent.instrument.symbol}: no reference price to protect the limit")
        return float(t)

    # -- place -----------------------------------------------------------------------------------

    async def place(self, intent: OrderIntent) -> LiveResult:
        inst = intent.instrument
        touch = self._touch(intent) if not intent.limit_price else float(intent.limit_price)
        price = float(intent.limit_price) if intent.limit_price else self.limit_price(intent, touch)
        waited = await self.throttle.acquire(inst.exch) if self.throttle is not None else 0.0
        placed_ts = self._clock()
        try:
            remote_id = await self.rest.place_order(
                inst,
                intent.side,
                intent.qty,
                price=price,
                intraday=self.intraday,
                remote_order_id=intent.client_order_id,
            )
        except VenueError as exc:
            self.rejected += 1
            log.error(
                "live.place_failed",
                strategy=intent.strategy,
                symbol=inst.symbol,
                purpose=intent.purpose.value,
                price=price,
                error=str(exc),
            )
            if intent.purpose is Purpose.EXIT:
                raise
            return LiveResult(False, error=str(exc), price_sent=price, throttle_wait_s=waited)

        self.placed += 1
        log.info(
            "live.placed",
            strategy=intent.strategy,
            symbol=inst.symbol,
            scrip=inst.scrip_code,
            side=intent.side.value,
            qty=intent.qty,
            price=price,
            touch=touch,
            remote_id=remote_id,
            throttle_wait_s=round(waited, 3),
        )
        res = await self._await_fill(intent, remote_id, price, touch, placed_ts)
        res.throttle_wait_s = waited
        return res

    async def _await_fill(self, intent: OrderIntent, remote_id: str, price_sent: float, touch: float, placed_ts: float) -> LiveResult:
        inst = intent.instrument
        seen = _Seen()
        chased = 0
        deadline = self._clock() + self.fill_timeout_s
        last_poll = placed_ts - self.poll_interval_s  # the first poll may go at once if no frame comes
        while True:
            now = self._clock()
            if self.events is not None:
                st = await self.events.wait(remote_id, min(self.poll_interval_s, max(0.05, deadline - now)))
                if st is not None:
                    _merge_state(seen, st)
                    if seen.outcome == "filled" and seen.avg_price > 0:
                        return self._ok(intent, remote_id, seen, placed_ts, price_sent, chased, "event")
                    if seen.outcome in ("rejected", "cancelled"):
                        return self._bad(intent, remote_id, seen, price_sent, chased)
            else:
                await self._sleep(min(self.poll_interval_s, max(0.05, deadline - now)))
            if self._clock() - last_poll >= self.poll_interval_s:
                last_poll = self._clock()
                row = await self._poll(inst.exch, remote_id, seen)
                if row is not None:
                    if seen.outcome == "filled" and seen.avg_price > 0:
                        return self._ok(intent, remote_id, seen, placed_ts, price_sent, chased, "poll")
                    if seen.outcome in ("rejected", "cancelled"):
                        return self._bad(intent, remote_id, seen, price_sent, chased)
            if self._clock() < deadline:
                continue
            # -- the timeout: a filled order with no rate yet → the order book; an exit → chase; else cancel
            if seen.outcome == "filled" and seen.avg_price <= 0:
                found = await self._rate_from_order_book(remote_id, seen)
                if found:
                    return self._ok(intent, remote_id, seen, placed_ts, price_sent, chased, "order_book")
            if intent.purpose is Purpose.EXIT and chased < self.chase_steps and seen.exch_order_id:
                chased += 1
                self.chased += 1
                new_price = self.limit_price(intent, touch, step=chased)
                qty_left = seen.pending if seen.pending is not None else max(intent.qty - seen.traded, 0)
                try:
                    await self.rest.modify_order(seen.exch_order_id, price=new_price, qty=qty_left or intent.qty)
                    log.warning("live.chase", symbol=inst.symbol, remote_id=remote_id, step=chased, price=new_price, left=qty_left)
                    price_sent = new_price
                    deadline = self._clock() + self.fill_timeout_s
                    continue
                except VenueError as exc:
                    seen.notes.append(f"chase {chased} failed: {exc}")
                    log.error("live.chase_failed", symbol=inst.symbol, remote_id=remote_id, error=str(exc))
            return await self._give_up(intent, remote_id, seen, placed_ts, price_sent, chased)

    async def _poll(self, exch: str, remote_id: str, seen: _Seen) -> dict[str, Any] | None:
        try:
            row = await self.rest.order_status(exch, remote_id)
        except VenueError as exc:
            seen.notes.append(f"status poll failed: {exc}")
            return None
        if not row:
            return None
        p = parse_status_row(row, 0)
        seen.status = p.status or seen.status
        seen.traded = max(seen.traded, p.traded)
        seen.pending = p.pending if p.pending is not None else seen.pending
        if p.avg_price > 0:
            seen.avg_price = p.avg_price
        seen.exch_order_id = p.exch_order_id or seen.exch_order_id
        seen.broker_order_id = p.broker_order_id or seen.broker_order_id
        seen.reason = p.reason or seen.reason
        seen.outcome = classify(seen.status, traded=seen.traded, qty=0)
        return row

    async def _rate_from_order_book(self, remote_id: str, seen: _Seen) -> bool:
        try:
            rows = await self.rest.order_book()
        except VenueError as exc:
            seen.notes.append(f"order book failed: {exc}")
            return False
        for row in rows:
            if str(row.get("RemoteOrderID") or row.get("RemoteOrderId") or "") == remote_id:
                p = parse_status_row(row, 0)
                if p.avg_price > 0:
                    seen.avg_price = p.avg_price
                    seen.traded = max(seen.traded, p.traded)
                    seen.exch_order_id = p.exch_order_id or seen.exch_order_id
                    return True
        return False

    async def _give_up(self, intent: OrderIntent, remote_id: str, seen: _Seen, placed_ts: float, price_sent: float, chased: int) -> LiveResult:
        inst = intent.instrument
        if seen.exch_order_id and seen.outcome not in ("filled", "rejected", "cancelled"):
            try:
                await self.rest.cancel_order(seen.exch_order_id)
                seen.notes.append("cancelled the remainder")
            except VenueError as exc:
                seen.notes.append(f"cancel failed: {exc}")
                log.error("live.cancel_failed", symbol=inst.symbol, remote_id=remote_id, error=str(exc))
        if seen.traded > 0 and seen.avg_price > 0:
            # a partial fill is a real position: book what traded, say so loudly
            self.partial += 1
            charges = self.costs.leg(inst, intent.side, seen.avg_price, seen.traded).total
            fill = Fill(price=seen.avg_price, qty=seen.traded, ts=self._clock(), charges=charges)
            msg = f"partial: {seen.traded}/{intent.qty} filled at {seen.avg_price:.2f}; " + "; ".join(seen.notes)
            log.error("live.partial", symbol=inst.symbol, remote_id=remote_id, purpose=intent.purpose.value, detail=msg)
            if intent.purpose is Purpose.EXIT:
                raise VenueError(f"EXIT {remote_id} {msg}", raw={"traded": seen.traded, "avg_price": seen.avg_price})
            return LiveResult(True, order_id=remote_id, fill=fill, status=seen.status, partial=True, via=seen.via or "poll",
                              error=msg, price_sent=price_sent, chased=chased, fill_ms=round((self._clock() - placed_ts) * 1000, 1))
        self.unfilled += 1
        msg = (
            f"order {remote_id} not filled in {self.fill_timeout_s:.0f}s × {chased + 1} "
            f"(status {seen.status or '?'}{', ' + seen.reason if seen.reason else ''}); " + "; ".join(seen.notes)
        )
        log.error("live.unfilled", symbol=inst.symbol, remote_id=remote_id, purpose=intent.purpose.value, detail=msg)
        if intent.purpose is Purpose.EXIT:
            raise VenueError(f"EXIT {msg}", raw={"status": seen.status})
        return LiveResult(False, order_id=remote_id, status=seen.status, error=msg, price_sent=price_sent, chased=chased)

    def _ok(self, intent: OrderIntent, remote_id: str, seen: _Seen, placed_ts: float, price_sent: float, chased: int, via: str) -> LiveResult:
        inst = intent.instrument
        qty = seen.traded if seen.traded > 0 else intent.qty
        charges = self.costs.leg(inst, intent.side, seen.avg_price, qty).total
        now = self._clock()
        latency = round((seen.ack_ts - placed_ts) * 1000, 1) if seen.ack_ts else None
        fill_ms = round((now - placed_ts) * 1000, 1)
        self.filled += 1
        self.via[via] = self.via.get(via, 0) + 1
        if latency is not None:
            self._latencies.append(latency)
        self._fill_ms.append(fill_ms)
        log.info("live.filled", symbol=inst.symbol, remote_id=remote_id, price=seen.avg_price, qty=qty, via=via,
                 latency_ms=latency, fill_ms=fill_ms, chased=chased, price_sent=price_sent)
        return LiveResult(
            True, order_id=remote_id, status=seen.status, via=via, latency_ms=latency, fill_ms=fill_ms,
            price_sent=price_sent, chased=chased, partial=0 < qty < intent.qty,
            fill=Fill(price=seen.avg_price, qty=qty, ts=now, charges=charges),
        )

    def _bad(self, intent: OrderIntent, remote_id: str, seen: _Seen, price_sent: float, chased: int) -> LiveResult:
        self.rejected += 1
        reason = seen.reason or seen.status or seen.outcome
        log.error("live.rejected", symbol=intent.instrument.symbol, remote_id=remote_id, purpose=intent.purpose.value, status=seen.status, reason=reason)
        if intent.purpose is Purpose.EXIT:
            raise VenueError(f"EXIT order {remote_id} {seen.status}: {reason}", raw={"status": seen.status, "reason": reason})
        return LiveResult(False, order_id=remote_id, status=seen.status, error=reason, price_sent=price_sent, chased=chased)

    # -- the hammer --------------------------------------------------------------------------------

    async def square_off_all(self) -> None:
        """The kill switch's last resort. Uses the broker's own bulk square-off so it works even if
        our position view is wrong — which is exactly the situation in which it gets used."""
        log.warning("live.square_off_all")
        await self.rest.square_off_all()

    async def flatten(self, intent: OrderIntent) -> LiveResult:
        assert intent.purpose is Purpose.EXIT
        return await self.place(intent)

    @staticmethod
    def exit_side(entry_side: OrderSide) -> OrderSide:
        return OrderSide.SELL if entry_side is OrderSide.BUY else OrderSide.BUY

    def stats(self) -> dict[str, Any]:
        lat = sorted(self._latencies)
        fm = sorted(self._fill_ms)
        return {
            "placed": self.placed,
            "filled": self.filled,
            "partial": self.partial,
            "rejected": self.rejected,
            "unfilled": self.unfilled,
            "chased": self.chased,
            "via": dict(self.via),
            "ack_latency_ms_median": lat[len(lat) // 2] if lat else None,
            "fill_ms_median": fm[len(fm) // 2] if fm else None,
            "protect_bps": {"entry": self.protect_entry_bps, "exit": self.protect_exit_bps},
            "fill_timeout_s": self.fill_timeout_s,
            "events": self.events.stats() if self.events is not None else None,
            "throttle": self.throttle.stats() if self.throttle is not None else None,
        }
