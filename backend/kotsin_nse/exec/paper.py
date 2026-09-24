"""Paper fills against the **live** order book.

Not "fill at the last price". An option's last trade can be minutes old and three ticks away from
where you could actually buy, and a paper book that assumes otherwise reports an edge that does not
survive contact with the ask ladder. The old stack's ``VirtualOrderMatchingService`` walked the real
ladder for exactly this reason, and its rules are kept:

* **lots first, then price** — take everything available at each level before moving up;
* **a 10% ceiling, absolute** — if filling the whole order would require paying more than 10% above
  the touch, the fill is truncated rather than chasing. A synthetic fill 15% up the ladder is not a
  fill, it is an excuse;
* **stale books do not fill.** A quote older than the freshness budget raises rather than filling at
  a price nobody is showing.

A CAN2 post-mortem is the cautionary tale here: its stop exits were booked *at exactly the stop
price*, so every recorded stop-out was optimistic by the gap, and the whole live ledger carried the
bias. A stop fill here walks the book like any other market order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..domain import Fill, Instrument, OrderIntent, OrderSide
from ..market.session import ist_hm
from ..risk.costs import CostModel


class NoBook(Exception):
    """No tradeable book. The caller must reject the order, not invent a price."""


@dataclass(slots=True)
class BookSnapshot:
    scrip_code: str
    bids: list[tuple[float, int]]
    asks: list[tuple[float, int]]
    ts: float

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return (b + a) / 2 if b and a else (b or a)

    def age_ms(self, now: float | None = None) -> float:
        return ((now or time.time()) - self.ts) * 1000


@dataclass(frozen=True, slots=True)
class Walk:
    avg_price: float
    filled: int
    levels: int
    capped: bool


def walk_book(
    levels: list[tuple[float, int]], qty: int, *, touch: float, ceiling_pct: float, buy: bool
) -> Walk:
    taken = 0
    cost = 0.0
    used = 0
    capped = False
    limit = touch * (1 + ceiling_pct / 100) if buy else touch * (1 - ceiling_pct / 100)
    for price, available in levels:
        if taken >= qty:
            break
        if (buy and price > limit) or (not buy and price < limit):
            capped = True
            break
        take = min(available, qty - taken)
        if take <= 0:
            continue
        taken += take
        cost += take * price
        used += 1
    return Walk(cost / taken if taken else 0.0, taken, used, capped)


class PaperMatcher:
    def __init__(
        self,
        costs: CostModel,
        *,
        max_book_age_ms: float = 6_000.0,
        open_max_book_age_ms: float = 25_000.0,
        open_window_ist: tuple[str, str] = ("09:00", "09:55"),
        ceiling_pct: float = 10.0,
    ) -> None:
        self.costs = costs
        #: the depth a fill may be priced on outside the opening window
        self.max_book_age_ms = max_book_age_ms
        #: …and inside it. The first minutes of a session deliver depth in bursts: on 2026-09-24
        #: three NSE names came back 13–15 s stale at the 09:45 decision, each order was rejected,
        #: and three consecutive rejects tripped the gateway breaker — which halts the ENGINE and
        #: force-flattened live positions in every book. A wider window through the opens is the
        #: operator's answer (2026-09-24); the tight one governs the rest of the day.
        self.open_max_book_age_ms = open_max_book_age_ms
        self.open_window_ist = open_window_ist
        self.ceiling_pct = ceiling_pct
        self.fills = 0
        self.truncated = 0
        self.rejected_stale = 0

    def fill(
        self,
        intent: OrderIntent,
        book: BookSnapshot | None,
        *,
        fallback_ltp: float | None = None,
        now: float | None = None,
    ) -> Fill:
        now = now or time.time()
        inst: Instrument = intent.instrument
        buy = intent.side is OrderSide.BUY

        if book is None or not (book.asks if buy else book.bids):
            if fallback_ltp is None or fallback_ltp <= 0:
                raise NoBook(f"no book and no LTP for {inst.symbol} ({inst.scrip_code})")
            # Degraded path: fill at LTP plus the configured slippage allowance, and SAY so on the
            # fill so the audit can separate these from real book fills.
            slip_bps = self.costs.s.slippage_bps_default
            price = fallback_ltp * (1 + slip_bps / 1e4 * (1 if buy else -1))
            charges = self.costs.leg(inst, intent.side, price, intent.qty).total
            self.fills += 1
            return Fill(
                price=round(price, 2),
                qty=intent.qty,
                ts=now,
                charges=charges,
                slippage_bps=slip_bps,
                book_age_ms=None,
                levels=0,
            )

        age = book.age_ms(now)
        limit = self.age_limit_ms(now)
        if age > limit:
            self.rejected_stale += 1
            raise NoBook(f"book for {inst.symbol} is {age:.0f} ms old (limit {limit:.0f})")

        levels = book.asks if buy else book.bids
        touch = levels[0][0]
        walk = walk_book(levels, intent.qty, touch=touch, ceiling_pct=self.ceiling_pct, buy=buy)
        if walk.filled == 0:
            raise NoBook(f"{inst.symbol}: nothing within {self.ceiling_pct:g}% of the touch")
        if walk.filled < intent.qty:
            self.truncated += 1

        mid = book.mid or touch
        slip = (walk.avg_price - mid) / mid * 1e4 * (1 if buy else -1) if mid else 0.0
        charges = self.costs.leg(inst, intent.side, walk.avg_price, walk.filled).total
        self.fills += 1
        return Fill(
            price=round(walk.avg_price, 2),
            qty=walk.filled,
            ts=now,
            charges=charges,
            slippage_bps=round(slip, 2),
            book_age_ms=round(age),
            levels=walk.levels,
        )

    def age_limit_ms(self, now: float | None = None) -> float:
        """How stale the depth may be right now. Wider inside the opening window, tight after."""
        lo, hi = self.open_window_ist
        return self.open_max_book_age_ms if lo <= ist_hm(now or time.time()) < hi else self.max_book_age_ms

    def stats(self) -> dict[str, float]:
        return {
            "fills": self.fills,
            "truncated": self.truncated,
            "rejected_stale": self.rejected_stale,
            "ceiling_pct": self.ceiling_pct,
            "max_book_age_ms": self.max_book_age_ms,
            "open_max_book_age_ms": self.open_max_book_age_ms,
            "open_window_ist": f"{self.open_window_ist[0]}-{self.open_window_ist[1]}",
            "age_limit_now_ms": self.age_limit_ms(),
        }
