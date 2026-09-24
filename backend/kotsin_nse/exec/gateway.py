"""The single chokepoint every order passes through, in every mode.

Pipeline, fail-closed at each step: **halt → idempotency → caps → exposure → place → audit**, with
a consecutive-reject circuit breaker that halts the strategy rather than machine-gunning the broker.

Modes are ``SHADOW`` (gates run, nothing is placed), ``PAPER`` (filled against the live book),
``LIVE_CAPPED`` (real orders under hard caps) and ``LIVE``. The mode is **state**, read from the
control table — never an environment variable. A restart that dropped ``CAN2_LIVE=true`` left that
book paper-trading for eight weeks while its log looked completely normal; the only tell was one
line in the boot banner and nothing alerted on it.

Exits are never blocked. A halt stops *entries*; refusing to let a position out is how a halt turns
a bad day into a disaster.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..domain import Fill, Order, OrderIntent, Purpose, new_id
from ..market.session import ist_day
from .paper import BookSnapshot, NoBook, PaperMatcher

if TYPE_CHECKING:
    from .live import LiveExecutor


class Mode(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_CAPPED = "LIVE_CAPPED"
    LIVE = "LIVE"


LIVE_MODES = (Mode.LIVE_CAPPED, Mode.LIVE)


class Decision(StrEnum):
    SUBMITTED = "SUBMITTED"
    PAPER_FILLED = "PAPER_FILLED"
    SHADOW_OK = "SHADOW_OK"
    SHADOW_WOULD_REJECT = "SHADOW_WOULD_REJECT"
    REJECTED_HALT = "REJECTED_HALT"
    REJECTED_CAP = "REJECTED_CAP"
    REJECTED_RISK = "REJECTED_RISK"
    REJECTED_BOOK = "REJECTED_BOOK"
    REJECTED_BROKER = "REJECTED_BROKER"
    DUP_BLOCKED = "DUP_BLOCKED"


@dataclass(frozen=True, slots=True)
class LiveCaps:
    """Hard bounds on an *armed* live engine. Sized for a personal account doing plumbing tests."""

    segments: tuple[str, ...] = ("NSE_EQ",)
    max_notional_inr: float = 25_000.0
    max_positions: int = 2
    max_orders_per_day: int = 6
    daily_loss_inr: float = 2_000.0
    entry_cutoff_ist: str = "15:10"
    #: Eleven consecutive rejects are tolerated; the twelfth trips the breaker (operator,
    #: 2026-09-24). It was 3, and three stale-depth rejections at the 09:45 open tripped it —
    #: which halts the whole engine and force-flattens every book, including books that were fine.
    breaker_consecutive_rejects: int = 12
    #: index options are excluded by default — they move faster than a personal risk budget likes
    skip_index: bool = True


@dataclass(slots=True)
class LiveContext:
    balance: float
    open_positions: int
    day_pnl_inr: float
    now_hm_ist: str
    segment: str
    exposure_ok: bool = True
    exposure_reason: str = ""


@dataclass(slots=True)
class OrderResult:
    decision: Decision
    order: Order
    fill: Fill | None = None

    @property
    def filled(self) -> bool:
        return self.fill is not None


class Gateway:
    def __init__(
        self,
        *,
        matcher: PaperMatcher,
        mode: Callable[[], Mode],
        halted: Callable[[], tuple[bool, str]],
        book_for: Callable[[str], BookSnapshot | None],
        ltp_for: Callable[[str], float | None],
        caps: LiveCaps | None = None,
        live: LiveExecutor | None = None,
    ) -> None:
        self._matcher = matcher
        self._mode = mode
        self._halted = halted
        self._book_for = book_for
        self._ltp_for = ltp_for
        self.caps = caps or LiveCaps()
        self.live = live
        self._seen: set[str] = set()
        self.orders_today = 0
        self.consecutive_rejects = 0
        self.breaker_tripped = False
        self._day = ist_day(time.time()).isoformat()

    # -- bookkeeping -----------------------------------------------------------------------------

    def remember(self, client_order_id: str) -> None:
        """Seed idempotency from the ledger at boot, so a restart cannot re-send an order that
        already went to the broker."""
        self._seen.add(client_order_id)

    def _rollover(self) -> None:
        d = ist_day(time.time()).isoformat()
        if d != self._day:
            self._day, self.orders_today = d, 0

    def _order_for(self, intent: OrderIntent, mode: Mode) -> Order:
        return Order(
            id=new_id("ord"),
            client_order_id=intent.client_order_id,
            strategy=intent.strategy,
            scrip_code=intent.instrument.scrip_code,
            symbol=intent.instrument.symbol,
            side=intent.side,
            purpose=intent.purpose,
            qty=intent.qty,
            mode=mode.value,
            status="REJECTED",
            signal_id=intent.signal_id,
            position_id=intent.position_id,
            reason=intent.reason,
            ts=time.time(),
        )

    def _reject(self, order: Order, decision: Decision, note: str) -> OrderResult:
        order.status, order.note = "REJECTED", note
        if decision is not Decision.DUP_BLOCKED:
            self.consecutive_rejects += 1
            if self.consecutive_rejects >= self.caps.breaker_consecutive_rejects:
                self.breaker_tripped = True
        return OrderResult(decision, order)

    def _precheck(self, intent: OrderIntent, order: Order) -> OrderResult | None:
        if intent.client_order_id in self._seen:
            return self._reject(order, Decision.DUP_BLOCKED, "duplicate client_order_id")
        self._seen.add(intent.client_order_id)
        halted, why = self._halted()
        if halted and intent.purpose is Purpose.ENTRY:
            return self._reject(order, Decision.REJECTED_HALT, why or "halted")
        return None

    # -- SHADOW / PAPER ----------------------------------------------------------------------------

    def submit(self, intent: OrderIntent, *, now: float | None = None) -> OrderResult:
        self._rollover()
        mode = self._mode()
        order = self._order_for(intent, mode)
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre

        if mode is Mode.SHADOW:
            order.status = "SHADOW"
            self.consecutive_rejects = 0
            return OrderResult(Decision.SHADOW_OK, order)

        if mode is Mode.PAPER:
            try:
                fill = self._matcher.fill(
                    intent,
                    self._book_for(intent.instrument.scrip_code),
                    fallback_ltp=self._ltp_for(intent.instrument.scrip_code),
                    now=now,
                )
            except NoBook as exc:
                return self._reject(order, Decision.REJECTED_BOOK, str(exc))
            order.status = "FILLED"
            order.avg_price, order.filled = fill.price, fill.qty
            order.charges, order.slippage_bps = fill.charges, fill.slippage_bps
            self.consecutive_rejects = 0
            self.orders_today += 1
            return OrderResult(Decision.PAPER_FILLED, order, fill)

        return self._reject(
            order, Decision.REJECTED_BROKER, f"{mode.value} orders must go through submit_live()"
        )

    # -- LIVE ---------------------------------------------------------------------------------------

    def check_live_caps(self, intent: OrderIntent, ctx: LiveContext) -> str | None:
        """Reason the intent breaks a cap, or ``None``. **Exits are never capped.**"""
        if intent.purpose is not Purpose.ENTRY:
            return None
        c = self.caps
        inst = intent.instrument
        if self.breaker_tripped:
            return "circuit breaker tripped — reset it explicitly"
        if ctx.segment not in c.segments:
            return f"{ctx.segment} not in live whitelist {list(c.segments)}"
        if c.skip_index and inst.scrip_code.startswith("999920"):
            return "index instrument excluded from live"
        if not ctx.exposure_ok:
            return ctx.exposure_reason or "exposure cap"
        if ctx.open_positions >= c.max_positions:
            return f"{ctx.open_positions} positions open ≥ cap {c.max_positions}"
        if self.orders_today >= c.max_orders_per_day:
            return f"{self.orders_today} orders today ≥ cap {c.max_orders_per_day}"
        if ctx.now_hm_ist > c.entry_cutoff_ist:
            return f"past the {c.entry_cutoff_ist} IST entry cutoff"
        if ctx.day_pnl_inr <= -c.daily_loss_inr:
            return f"day P&L ₹{ctx.day_pnl_inr:,.0f} ≤ −₹{c.daily_loss_inr:,.0f}"
        ref = intent.ref_price or self._ltp_for(inst.scrip_code) or 0.0
        notional = inst.notional(ref, intent.qty)
        if notional > c.max_notional_inr:
            return f"notional ₹{notional:,.0f} > cap ₹{c.max_notional_inr:,.0f}"
        return None

    async def submit_live(self, intent: OrderIntent, *, ctx: LiveContext) -> OrderResult:
        self._rollover()
        mode = self._mode()
        order = self._order_for(intent, mode)
        if mode not in LIVE_MODES:
            return self._reject(order, Decision.REJECTED_BROKER, f"not in a live mode ({mode.value})")
        if self.live is None:
            return self._reject(
                order, Decision.REJECTED_BROKER, "live executor not configured (no credentials)"
            )
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre
        if mode is Mode.LIVE_CAPPED:
            why = self.check_live_caps(intent, ctx)
            if why:
                return self._reject(order, Decision.REJECTED_CAP, why)

        res = await self.live.place(intent)
        if not res.ok or res.fill is None:
            return self._reject(order, Decision.REJECTED_BROKER, res.error or "no fill")
        order.status = "FILLED"
        order.avg_price, order.filled = res.fill.price, res.fill.qty
        order.charges = res.fill.charges
        order.broker_order_id = res.order_id
        self.consecutive_rejects = 0
        self.orders_today += 1
        return OrderResult(Decision.SUBMITTED, order, res.fill)

    def reset_breaker(self) -> None:
        self.consecutive_rejects = 0
        self.breaker_tripped = False

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self._mode().value,
            "orders_today": self.orders_today,
            "consecutive_rejects": self.consecutive_rejects,
            "breaker_tripped": self.breaker_tripped,
            "seen_ids": len(self._seen),
            "live_configured": self.live is not None,
            "matcher": self._matcher.stats(),
            "caps": {
                "segments": list(self.caps.segments),
                "max_notional_inr": self.caps.max_notional_inr,
                "max_positions": self.caps.max_positions,
                "max_orders_per_day": self.caps.max_orders_per_day,
                "daily_loss_inr": self.caps.daily_loss_inr,
                "entry_cutoff_ist": self.caps.entry_cutoff_ist,
            },
        }
