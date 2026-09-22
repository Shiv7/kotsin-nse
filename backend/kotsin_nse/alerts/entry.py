"""When the trade would actually be entered, and at what the option would actually cost.

A signal time is not an entry time, and a last-traded price is not a fill. On a 30m option book
those two gaps are the difference between a modelled trade and a fictional one:

* the bar closes, the exchange candle is reconciled, the book decides — measured at 0.7-0.8s;
* a buy lifts the **ask**, not the LTP, and walks the ladder if one lot is bigger than the touch;
* the quote itself has an age, and a premium from thirty seconds ago is not the premium now.

Reporting the LTP at signal time as the entry understates cost on every trade in the same
direction, which is precisely the kind of error a cost model cannot recover from — the measured
round trip is already 0.299% with charges at 77-209% of gross.

So the entry is modelled at the moment it is computed, off the live ask ladder, and it carries its
own staleness. A quote too old to trust yields no entry price at all rather than a confident wrong
one: ``position_quote_max_age_s`` already encodes that judgement for open positions, and an entry
deserves the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A quote older than this is not an entry price. Matches the engine's own rule for pricing an
#: open position's stop (``Settings.position_quote_max_age_s``).
MAX_QUOTE_AGE_S = 60.0
#: How far up the ladder a market buy is allowed to walk before it is called capped, as a percent
#: of the touch. The paper matcher's own ceiling.
LADDER_CEILING_PCT = 5.0


@dataclass(slots=True)
class Entry:
    ts: float
    lag_from_bar_close_s: float
    lag_from_fired_s: float
    underlying: float | None
    lots: int
    qty: int
    ltp: float | None
    bid: float | None
    ask: float | None
    quote_ts: float | None
    quote_age_s: float | None
    fill: float | None
    fill_source: str
    levels_walked: int
    capped: bool
    slippage_vs_ltp_pct: float | None
    notional: float | None
    stale: bool
    note: str

    def to_json(self) -> dict[str, Any]:
        r = lambda v, d=2: None if v is None else round(v, d)  # noqa: E731
        return {
            "ts": self.ts,
            "lagFromBarCloseS": round(self.lag_from_bar_close_s, 3),
            "lagFromFiredS": round(self.lag_from_fired_s, 3),
            "underlying": r(self.underlying),
            "lots": self.lots,
            "qty": self.qty,
            "ltp": r(self.ltp),
            "bid": r(self.bid),
            "ask": r(self.ask),
            "quoteTs": self.quote_ts,
            "quoteAgeS": r(self.quote_age_s, 1),
            "fill": r(self.fill),
            "fillSource": self.fill_source,
            "levelsWalked": self.levels_walked,
            "capped": self.capped,
            "slippageVsLtpPct": r(self.slippage_vs_ltp_pct, 3),
            "notional": r(self.notional, 0),
            "stale": self.stale,
            "note": self.note,
        }


def model(
    *,
    now: float,
    bar_close: float,
    fired_at: float,
    underlying_ltp: float | None,
    quote: Any | None,
    book: Any | None,
    lot_size: int,
    lots: int = 1,
) -> Entry:
    """The entry as it would actually happen, priced off the ask ladder at this instant."""
    from ..exec.paper import walk_book

    qty = max(lot_size, 1) * max(lots, 1)
    ltp = getattr(quote, "ltp", None) if quote else None
    bid = getattr(quote, "bid", None) if quote else None
    ask = getattr(quote, "ask", None) if quote else None
    quote_ts = getattr(quote, "ts", None) if quote else None
    age = None if quote_ts is None else now - quote_ts
    stale = age is None or age > MAX_QUOTE_AGE_S

    fill: float | None = None
    source = "none"
    levels = 0
    capped = False

    if stale:
        note = (
            "no quote for this contract" if quote_ts is None
            else f"last quote {age:.0f}s old — older than the {MAX_QUOTE_AGE_S:.0f}s an entry may trust"
        )
    else:
        asks = getattr(book, "asks", None) if book else None
        book_age = now - getattr(book, "ts", now) if book else None
        if asks and book_age is not None and book_age <= MAX_QUOTE_AGE_S:
            touch = asks[0][0]
            walk = walk_book(asks, qty, touch=touch, ceiling_pct=LADDER_CEILING_PCT, buy=True)
            if walk.filled >= qty and walk.avg_price > 0:
                fill, source, levels = walk.avg_price, "ladder", walk.levels
                capped = walk.capped
            elif ask and ask > 0:
                fill, source = ask, "touch (ladder too thin for one lot)"
        elif ask and ask > 0:
            fill, source = ask, "touch (no live depth)"
        elif ltp and ltp > 0:
            fill, source = ltp, "last trade (no book, no ask)"
        note = "" if fill else "no ask and no last trade — nothing to price against"

    slip = None
    if fill and ltp and ltp > 0:
        slip = (fill - ltp) / ltp * 100

    return Entry(
        ts=now,
        lag_from_bar_close_s=now - bar_close if bar_close else 0.0,
        lag_from_fired_s=now - fired_at if fired_at else 0.0,
        underlying=underlying_ltp,
        lots=max(lots, 1),
        qty=qty,
        ltp=ltp,
        bid=bid,
        ask=ask,
        quote_ts=quote_ts,
        quote_age_s=age,
        fill=fill,
        fill_source=source,
        levels_walked=levels,
        capped=capped,
        slippage_vs_ltp_pct=slip,
        notional=fill * qty if fill else None,
        stale=stale,
        note=note,
    )
