"""Real orders. The only file in the package that can lose money.

Deliberately small and deliberately synchronous-in-spirit: place, then poll the broker until the
order is terminal, then report the **actual** fill. Never assume a placement is a fill — a 200 from
``PlaceOrderRequest`` means the broker accepted the request, and RMS can still reject it a second
later.

Two rules that came out of the old executor's incident log:

* **The broker is the source of truth.** Local state is a cache. Everything here reports what the
  order book says, not what we hoped.
* **A failed exit must be loud.** CAN2 swallowed ``te-CLOSE failed`` as a warning and carried on,
  so its own view of a position and the executor's silently diverged. An exit that does not confirm
  raises.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from ..domain import Fill, OrderIntent, OrderSide, Purpose
from ..risk.costs import CostModel
from ..venue.base import VenueError
from ..venue.fivepaisa.rest import FivePaisaREST

log = structlog.get_logger(__name__)

TERMINAL_OK = {"FULLY EXECUTED", "FULLY_EXECUTED", "EXECUTED", "FILLED"}
TERMINAL_BAD = {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}


@dataclass(slots=True)
class LiveResult:
    ok: bool
    order_id: str = ""
    fill: Fill | None = None
    error: str = ""
    status: str = ""


class LiveExecutor:
    def __init__(
        self,
        rest: FivePaisaREST,
        costs: CostModel,
        *,
        poll_interval_s: float = 1.0,
        poll_timeout_s: float = 20.0,
        intraday: bool = True,
    ) -> None:
        self.rest = rest
        self.costs = costs
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self.intraday = intraday
        self.placed = 0
        self.rejected = 0

    async def place(self, intent: OrderIntent) -> LiveResult:
        inst = intent.instrument
        try:
            remote_id = await self.rest.place_order(
                inst,
                intent.side,
                intent.qty,
                price=intent.limit_price or 0.0,
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
                error=str(exc),
            )
            if intent.purpose is Purpose.EXIT:
                raise
            return LiveResult(False, error=str(exc))

        self.placed += 1
        log.info(
            "live.placed",
            strategy=intent.strategy,
            symbol=inst.symbol,
            scrip=inst.scrip_code,
            side=intent.side.value,
            qty=intent.qty,
            remote_id=remote_id,
        )
        return await self._await_fill(intent, remote_id)

    async def _await_fill(self, intent: OrderIntent, remote_id: str) -> LiveResult:
        inst = intent.instrument
        deadline = time.time() + self.poll_timeout_s
        last_status = ""
        while time.time() < deadline:
            await asyncio.sleep(self.poll_interval_s)
            try:
                row = await self.rest.order_status(inst.exch, remote_id)
            except VenueError as exc:
                last_status = f"status poll failed: {exc}"
                continue
            status = str(row.get("Status") or row.get("OrderStatus") or "").upper().strip()
            last_status = status or last_status
            if status in TERMINAL_OK:
                price = float(row.get("Rate") or row.get("AvgRate") or intent.ref_price or 0)
                qty = int(row.get("TradedQty") or row.get("Qty") or intent.qty)
                charges = self.costs.leg(inst, intent.side, price, qty).total
                return LiveResult(
                    True,
                    order_id=remote_id,
                    status=status,
                    fill=Fill(price=price, qty=qty, ts=time.time(), charges=charges),
                )
            if status in TERMINAL_BAD:
                self.rejected += 1
                reason = str(row.get("Reason") or row.get("Message") or status)
                if intent.purpose is Purpose.EXIT:
                    raise VenueError(f"EXIT order {remote_id} {status}: {reason}", raw=row)
                return LiveResult(False, order_id=remote_id, status=status, error=reason)
        msg = f"order {remote_id} not terminal after {self.poll_timeout_s:.0f}s (last: {last_status})"
        if intent.purpose is Purpose.EXIT:
            raise VenueError(msg)
        return LiveResult(False, order_id=remote_id, status=last_status, error=msg)

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

    def stats(self) -> dict[str, float]:
        return {"placed": self.placed, "rejected": self.rejected}
