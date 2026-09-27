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

from ..domain import Fill, Order, OrderIntent, OrderSide, Purpose, new_id
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
    #: the engine is not set up to send it (not in a live mode, no live executor): says nothing
    #: about the broker, so it never counts toward a book's breaker
    REJECTED_CONFIG = "REJECTED_CONFIG"
    DUP_BLOCKED = "DUP_BLOCKED"
    #: a paper limit order resting at the venue's touch, not yet filled
    RESTING = "RESTING"
    #: a paper limit entry that was never filled by its deadline — the trigger is missed
    LIMIT_UNFILLED = "LIMIT_UNFILLED"


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
        #: The breaker is PER BOOK (operator, 2026-09-26: "each fudkii variant, parent or twins
        #: required 12 consecutive rejection to trigger a halt. not total 12"). Since every book
        #: places its own order, one shared count reached 12 after two or three triggers. Only a
        #: real failure counts (a book with no depth, the broker refusing): not an exit, a duplicate,
        #: a halt or a cap — those say nothing about the gateway. A tripped book places no entry
        #: until it is reset; its open positions keep their exits.
        self.rejects_by_book: dict[str, int] = {}
        self.tripped_books: set[str] = set()
        #: books tripped since the engine last read them (``take_new_trips``), for the alert
        self._new_trips: list[str] = []
        #: live orders sent today, per book — each book's own LIVE_CAPPED order cap
        self.live_orders_by_book: dict[str, int] = {}
        self._day = ist_day(time.time()).isoformat()

    #: the rejections that count toward a book's breaker
    COUNTED_REJECTS = frozenset({Decision.REJECTED_BOOK, Decision.REJECTED_BROKER})

    @property
    def consecutive_rejects(self) -> int:
        """The worst book's run of consecutive rejections (the health page's number)."""
        return max(self.rejects_by_book.values(), default=0)

    @property
    def breaker_tripped(self) -> bool:
        return bool(self.tripped_books)

    def book_tripped(self, book: str) -> bool:
        return book in self.tripped_books

    def take_new_trips(self) -> list[str]:
        out, self._new_trips = self._new_trips, []
        return out

    def _ok(self, order: Order) -> None:
        """An ENTRY that went through ends its book's run of rejected entries. An exit or a target
        fill says nothing about whether the book's entries get through (review, 2026-09-26)."""
        if order.purpose is Purpose.ENTRY:
            self.rejects_by_book[order.strategy] = 0

    # -- bookkeeping -----------------------------------------------------------------------------

    def remember(self, client_order_id: str) -> None:
        """Seed idempotency from the ledger at boot, so a restart cannot re-send an order that
        already went to the broker."""
        self._seen.add(client_order_id)

    def _rollover(self) -> None:
        d = ist_day(time.time()).isoformat()
        if d != self._day:
            self._day, self.orders_today = d, 0
            self.live_orders_by_book.clear()

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
        # An EXIT refused is a position that needs closing, not a gateway misbehaving: counting it
        # let one quiet contract's retries run the engine-wide breaker to 10 of 12 on 2026-09-25
        # (14:45-14:46), which would have force-closed every book.
        if decision in self.COUNTED_REJECTS and order.purpose is not Purpose.EXIT:
            n = self.rejects_by_book.get(order.strategy, 0) + 1
            self.rejects_by_book[order.strategy] = n
            if n >= self.caps.breaker_consecutive_rejects and order.strategy not in self.tripped_books:
                self.tripped_books.add(order.strategy)
                self._new_trips.append(order.strategy)
        return OrderResult(decision, order)

    def _precheck(self, intent: OrderIntent, order: Order) -> OrderResult | None:
        if intent.client_order_id in self._seen:
            return self._reject(order, Decision.DUP_BLOCKED, "duplicate client_order_id")
        self._seen.add(intent.client_order_id)
        halted, why = self._halted()
        if halted and intent.purpose is Purpose.ENTRY:
            return self._reject(order, Decision.REJECTED_HALT, why or "halted")
        if intent.purpose is Purpose.ENTRY and intent.strategy in self.tripped_books:
            return self._reject(order, Decision.REJECTED_HALT, f"{intent.strategy}'s order breaker is tripped — reset it")
        return None

    # -- SHADOW / PAPER ----------------------------------------------------------------------------

    def submit_paper(self, intent: OrderIntent, *, now: float | None = None) -> OrderResult:
        """A paper fill whatever the engine's mode — for a book that never trades at the venue, so a
        LIVE session fills its entries and exits against the live book without sending them."""
        self._rollover()
        order = self._order_for(intent, Mode.PAPER)
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre
        return self._paper_fill(intent, order, now)

    def submit(self, intent: OrderIntent, *, now: float | None = None) -> OrderResult:
        self._rollover()
        mode = self._mode()
        order = self._order_for(intent, mode)
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre

        if mode is Mode.SHADOW:
            order.status = "SHADOW"
            self._ok(order)
            return OrderResult(Decision.SHADOW_OK, order)

        if mode is Mode.PAPER:
            return self._paper_fill(intent, order, now)

        return self._reject(
            order, Decision.REJECTED_BROKER, f"{mode.value} orders must go through submit_live()"
        )

    def _paper_fill(self, intent: OrderIntent, order: Order, now: float | None) -> OrderResult:
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
        self._ok(order)
        self.orders_today += 1
        return OrderResult(Decision.PAPER_FILLED, order, fill)

    # -- PAPER limit orders (exec/resting.py) -------------------------------------------------------

    def place_limit(self, intent: OrderIntent) -> OrderResult:
        """A PAPER limit order goes to rest: the same idempotency and halt gates as any order, no
        fill yet. The engine advances it (``fill_resting`` / ``cancel_resting``)."""
        self._rollover()
        order = self._order_for(intent, Mode.PAPER)
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre
        order.status, order.note = "RESTING", f"limit {intent.limit_price}"
        return OrderResult(Decision.RESTING, order)

    def fill_resting(
        self, order: Order, intent: OrderIntent, *, price: float, mid: float | None, book_age_ms: float | None, now: float
    ) -> OrderResult:
        """A resting limit was hit: filled whole at its limit (partials are not modelled)."""
        charges = self._matcher.costs.leg(intent.instrument, intent.side, price, intent.qty).total
        buy = intent.side is OrderSide.BUY
        slip = (price - mid) / mid * 1e4 * (1 if buy else -1) if mid else None
        fill = Fill(price=round(price, 2), qty=intent.qty, ts=now, charges=charges,
                    slippage_bps=round(slip, 2) if slip is not None else None,
                    book_age_ms=round(book_age_ms) if book_age_ms is not None else None, levels=0)
        order.status = "FILLED"
        order.avg_price, order.filled = fill.price, fill.qty
        order.charges, order.slippage_bps = fill.charges, fill.slippage_bps
        self._ok(order)
        self.orders_today += 1
        self._matcher.fills += 1
        return OrderResult(Decision.PAPER_FILLED, order, fill)

    def cancel_resting(self, order: Order, note: str) -> OrderResult:
        """A resting entry that ran out of time. Not a reject: nothing was refused, the market just
        never came to the limit — so it does not count toward the breaker."""
        order.status, order.note = "CANCELLED", note
        return OrderResult(Decision.LIMIT_UNFILLED, order)

    # -- LIVE ---------------------------------------------------------------------------------------

    def check_live_caps(self, intent: OrderIntent, ctx: LiveContext) -> str | None:
        """Reason the intent breaks a cap, or ``None``. **Exits are never capped.**"""
        if intent.purpose is not Purpose.ENTRY:
            return None
        c = self.caps
        inst = intent.instrument
        if intent.strategy in self.tripped_books:
            return f"{intent.strategy}'s circuit breaker tripped — reset it explicitly"
        if ctx.segment not in c.segments:
            return f"{ctx.segment} not in live whitelist {list(c.segments)}"
        if c.skip_index and inst.scrip_code.startswith("999920"):
            return "index instrument excluded from live"
        if not ctx.exposure_ok:
            return ctx.exposure_reason or "exposure cap"
        if ctx.open_positions >= c.max_positions:
            return f"{ctx.open_positions} positions open ≥ cap {c.max_positions}"
        sent = self.live_orders_by_book.get(intent.strategy, 0)
        if sent >= c.max_orders_per_day:
            return f"{intent.strategy}: {sent} live orders today ≥ its cap {c.max_orders_per_day}"
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
            return self._reject(order, Decision.REJECTED_CONFIG, f"not in a live mode ({mode.value})")
        if self.live is None:
            return self._reject(
                order, Decision.REJECTED_CONFIG, "live executor not configured (no credentials)"
            )
        pre = self._precheck(intent, order)
        if pre is not None:
            return pre
        if mode is Mode.LIVE_CAPPED:
            why = self.check_live_caps(intent, ctx)
            if why:
                return self._reject(order, Decision.REJECTED_CAP, why)

        # counted when SENT: a cap on the orders a book sends a day counts the ones the broker
        # refused or never filled too, not only the fills (review, 2026-09-26)
        self.live_orders_by_book[order.strategy] = self.live_orders_by_book.get(order.strategy, 0) + 1
        res = await self.live.place(intent)
        if not res.ok or res.fill is None:
            return self._reject(order, Decision.REJECTED_BROKER, res.error or "no fill")
        order.status = "FILLED"
        order.avg_price, order.filled = res.fill.price, res.fill.qty
        order.charges = res.fill.charges
        order.broker_order_id = res.order_id
        self._ok(order)
        self.orders_today += 1
        return OrderResult(Decision.SUBMITTED, order, res.fill)

    def refused(self, intent: OrderIntent, decision: Decision, note: str) -> OrderResult:
        """An order the engine refuses before it reaches the gateway (a live entry the broker account
        has no money for): recorded like any refusal, not counted toward the breaker."""
        order = self._order_for(intent, self._mode())
        order.status, order.note = "REJECTED", note
        return OrderResult(decision, order)

    def reset_breaker(self, book: str | None = None) -> None:
        """Reset one book's breaker, or every book's."""
        books = [book] if book else list(set(self.rejects_by_book) | self.tripped_books)
        for b in books:
            self.rejects_by_book[b] = 0
            self.tripped_books.discard(b)

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self._mode().value,
            "orders_today": self.orders_today,
            "consecutive_rejects": self.consecutive_rejects,
            "breaker_tripped": self.breaker_tripped,
            "rejects_by_book": dict(self.rejects_by_book),
            "tripped_books": sorted(self.tripped_books),
            "live_orders_by_book": dict(self.live_orders_by_book),
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
                "breaker_consecutive_rejects": self.caps.breaker_consecutive_rejects,
            },
        }
