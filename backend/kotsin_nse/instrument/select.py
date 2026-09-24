"""Which instrument a signal is actually traded in.

A FUDKII/FUKAA signal is a statement about the **underlying**. Turning it into an order means
picking a contract, and that is where the old stack lost the most time:

* the strike rule is *"OTM at entry, roughly ATM at the confluence T1"* — the option is bought far
  enough out to be cheap and close enough in to be worth something when the target prints. The
  anchor is T1, falling back to ATM when no target resolved;
* **this step used to block publication.** The enricher fetched a live option LTP inline and took
  3–23 seconds on a cache miss, logging *"CTA price may be stale at publish time"* past 10 s. Here
  selection is a pure function over a chain snapshot the engine already holds, so it costs
  microseconds and a stale chain is a *gate*, not a silent delay;
* liquidity is checked before the strike is accepted. A strike with no bid is not a trade.

MCX signals are expressed in the **front-month future**, not an option: MCX option liquidity away
from gold and silver does not support a stop.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from ..config import Segment
from ..domain import Direction, Instrument, InstrumentKind, OptionType


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    #: minimum days to expiry — a contract expiring tomorrow is all theta and no delta
    min_days_to_expiry: int = 2
    #: prefer the nearest expiry that clears the floor; 0 = nearest available
    expiry_index: int = 0
    #: reject a strike whose quoted premium is outside this band (₹). The QUANT book banded premium
    #: deliberately: enough to move, not so much that a stop is expensive. Applied here to every
    #: option order because the reasoning is not strategy-specific.
    min_premium: float = 5.0
    max_premium: float | None = 400.0
    #: reject a strike with no two-sided quote or a spread wider than this fraction of the mid
    max_spread_pct: float = 8.0
    #: how stale a chain snapshot may be before selection refuses to choose
    max_quote_age_s: float = 30.0


@dataclass(frozen=True, slots=True)
class Selection:
    instrument: Instrument | None
    premium: float = 0.0
    reason: str = ""
    anchor: float = 0.0
    spread_pct: float | None = None

    @property
    def ok(self) -> bool:
        return self.instrument is not None


@dataclass(frozen=True, slots=True)
class Quote:
    ltp: float
    bid: float
    ask: float
    ts: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.ltp

    @property
    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100 if mid > 0 else None


def choose_expiry(expiries: list[str], today: date, policy: SelectionPolicy) -> str | None:
    live = [e for e in sorted(expiries) if (date.fromisoformat(e) - today).days >= policy.min_days_to_expiry]
    if not live:
        return None
    return live[min(policy.expiry_index, len(live) - 1)]


def otm_strike_anchor(
    *, spot: float, target1: float | None, direction: Direction
) -> float:
    """The price the chosen strike should sit at.

    *"OTM at entry → ATM at target"*: anchor on T1 so the option is in the money exactly when the
    trade works. With no target the anchor is the spot, i.e. plain ATM — the original's ``0`` return
    meant the same thing but forced every caller to know that.
    """
    if target1 is None or target1 <= 0:
        return spot
    # Never anchor behind spot: a "target" on the wrong side means the confluence is stale.
    if direction is Direction.BULLISH and target1 < spot:
        return spot
    if direction is Direction.BEARISH and target1 > spot:
        return spot
    return target1


def strike_anchor_from_pivots(
    *, prices: Iterable[float], spot: float, direction: Direction, min_distance: float
) -> float | None:
    """The nearest **raw classic pivot** ahead of the trade, skipping any too close to aim at.

    Deliberately not the confluence target. T1 is a *cluster* of levels strong enough to be a wall,
    and it is what the trade exits on; borrowing it to place the strike coupled two decisions that
    are not the same question, and on a distant wall it put the strike five to seven strikes out
    where nothing trades (KAYNES, 2026-09-24: anchor 3800 against a 3523 spot, every candidate
    one-sided). This answers only "which strike", on the nearest level price would actually have to
    reach — and a level sitting almost on top of spot is no target at all, so the next one is used.

    ``min_distance`` is in price, normally a fraction of ATR. Returns None when nothing is ahead.
    """
    ahead = sorted(
        (p for p in prices if (p > spot if direction is Direction.BULLISH else p < spot)),
        key=lambda p: abs(p - spot),
    )
    if not ahead:
        return None
    for p in ahead:
        if abs(p - spot) >= min_distance:
            return p
    return ahead[-1]  # every level is inside the floor: the furthest is the best of them


def rank_by_liquidity(
    candidates: list[Instrument], liquidity: Mapping[str, tuple[float, float]], anchor: float
) -> list[Instrument]:
    """Best combined rank of traded volume and open interest, ties broken toward the anchor.

    Neither number decides alone: volume is what you can be filled against today, open interest is
    where the positions actually are, and a strike that is merely extreme on one of them is not the
    one to trade. A strike with no data ranks last on that metric rather than being dropped.
    """
    def ranks(which: int) -> dict[str, int]:
        # ranked by VALUE, not by position: two strikes with the same volume must tie, or the
        # tie-break below never runs and the arbitrary input order decides the trade.
        vals = sorted({liquidity.get(i.scrip_code, (0.0, 0.0))[which] for i in candidates}, reverse=True)
        at = {v: n for n, v in enumerate(vals)}
        return {i.scrip_code: at[liquidity.get(i.scrip_code, (0.0, 0.0))[which]] for i in candidates}

    vr, orr = ranks(0), ranks(1)
    return sorted(candidates, key=lambda i: (vr[i.scrip_code] + orr[i.scrip_code], abs(i.strike - anchor)))


def select_option(
    *,
    chain: list[Instrument],
    quotes: dict[str, Quote],
    spot: float,
    target1: float | None,
    direction: Direction,
    now: float,
    policy: SelectionPolicy | None = None,
    strike_anchor: float | None = None,
    liquidity: Mapping[str, tuple[float, float]] | None = None,
) -> Selection:
    """The most liquid OTM strike between spot and the anchor that is genuinely tradeable.

    ``strike_anchor`` is the pivot the strike is placed against (``strike_anchor_from_pivots``);
    without one this falls back to the old behaviour, the confluence target. ``liquidity`` maps a
    scrip code to ``(volume, open interest)``.
    """
    pol = policy or SelectionPolicy()
    want = direction.option_type
    anchor = strike_anchor or otm_strike_anchor(spot=spot, target1=target1, direction=direction)
    candidates = [i for i in chain if i.option_type is want and i.strike > 0]
    if not candidates:
        return Selection(None, reason=f"no {want.value} strikes in the chain", anchor=anchor)

    # OTM at entry: calls above spot, puts below.
    if want is OptionType.CE:
        otm = [i for i in candidates if i.strike > spot]
    else:
        otm = [i for i in candidates if i.strike < spot]
    pool = otm or candidates  # a chain with no OTM strike is odd but not a reason to skip the trade
    if liquidity is not None:
        # The strikes the move would travel through, most-traded first — then everything else,
        # nearest the anchor first. The span is a preference, not a cage: it is often one or two
        # strikes wide, and "the best strike is one-sided" must fall through to the next suitable
        # OTM rather than abandoning the trigger (found live 2026-09-24: INDUSTOWER and RADICO
        # were lost to a single one-sided quote).
        lo, hi = (spot, anchor) if want is OptionType.CE else (anchor, spot)
        span = [i for i in pool if lo <= i.strike <= hi]
        rest = sorted((i for i in pool if i not in span), key=lambda i: abs(i.strike - anchor))
        pool = rank_by_liquidity(span, liquidity, anchor) + rest
    else:
        pool = sorted(pool, key=lambda i: abs(i.strike - anchor))

    skipped: list[str] = []
    for inst in pool:
        q = quotes.get(inst.scrip_code)
        if q is None:
            skipped.append(f"{inst.strike:g}:no-quote")
            continue
        if now - q.ts > pol.max_quote_age_s:
            skipped.append(f"{inst.strike:g}:stale-{int(now - q.ts)}s")
            continue
        premium = q.mid
        if premium < pol.min_premium:
            skipped.append(f"{inst.strike:g}:premium-{premium:.1f}<{pol.min_premium:g}")
            continue
        if pol.max_premium is not None and premium > pol.max_premium:
            skipped.append(f"{inst.strike:g}:premium-{premium:.1f}>{pol.max_premium:g}")
            continue
        spread = q.spread_pct
        if spread is None:
            skipped.append(f"{inst.strike:g}:one-sided")
            continue
        if spread > pol.max_spread_pct:
            skipped.append(f"{inst.strike:g}:spread-{spread:.1f}%")
            continue
        return Selection(inst, premium=premium, anchor=anchor, spread_pct=spread, reason="ok")
    return Selection(
        None,
        reason="no tradeable strike: " + ", ".join(skipped[:6]),
        anchor=anchor,
    )


def select_future(
    *, front: Instrument | None, quote: Quote | None, now: float, policy: SelectionPolicy | None = None
) -> Selection:
    pol = policy or SelectionPolicy()
    if front is None:
        return Selection(None, reason="no unexpired front-month future")
    if quote is None or now - quote.ts > pol.max_quote_age_s:
        return Selection(None, reason="no fresh quote for the front future")
    return Selection(front, premium=quote.mid, reason="ok", spread_pct=quote.spread_pct)


def map_levels_to_option(
    *,
    equity_entry: float,
    equity_stop: float,
    equity_targets: tuple[float, ...],
    option_premium: float,
    delta: float,
) -> tuple[float, tuple[float, ...]]:
    """Project the underlying's stop/target ladder onto the option premium via delta.

    Crude on purpose. A full Black-Scholes re-price needs an IV surface we do not have live, and the
    old stack's option ladder was itself delta-scaled. What matters is that the mapping is stated in
    one place and is monotone: the option stop is never at or below zero, and the targets keep their
    order. Gamma makes this conservative on the target side and slightly tight on the stop side.
    """
    d = max(0.05, min(1.0, abs(delta)))
    move_stop = abs(equity_entry - equity_stop) * d
    stop = max(0.05, round(option_premium - move_stop, 2))
    targets = tuple(
        round(option_premium + abs(t - equity_entry) * d, 2) for t in equity_targets if t > 0
    )
    return stop, targets


def estimate_delta(*, spot: float, strike: float, option_type: OptionType) -> float:
    """A stand-in for a Greek we do not receive on the feed.

    0.5 at the money, decaying towards 0.15 as the strike moves away — roughly the shape of a
    2-4 week NSE option. Marked as an estimate everywhere it is used; a real delta would come from
    the chain if the broker published one.
    """
    if spot <= 0 or strike <= 0:
        return 0.5
    moneyness = (strike - spot) / spot if option_type is OptionType.CE else (spot - strike) / spot
    if moneyness <= 0:
        return min(0.85, 0.5 + abs(moneyness) * 6)
    return max(0.15, 0.5 - moneyness * 8)


def qty_for_budget(instrument: Instrument, price: float, budget: float, *, max_lots: int | None) -> int:
    """Lots that fit the budget, as a quantity in the broker's own units.

    ``price × qty`` is **not** the notional on MCX — ALUMINI is quoted per kg on a 1,000 kg
    contract, so a 286-qty entry that logged ₹99,943 was really ₹99.9 million. The multiplier is
    always applied, and an unknown multiplier declines the trade rather than guessing 1.
    """
    if price <= 0 or budget <= 0 or instrument.multiplier <= 0:
        return 0
    if instrument.kind is InstrumentKind.EQUITY:
        return max(0, int(budget // (price * instrument.multiplier)))
    per_lot = price * instrument.lot_size * instrument.multiplier
    if per_lot <= 0:
        return 0
    lots = int(budget // per_lot)
    if max_lots is not None:
        lots = min(lots, max_lots)
    return lots * instrument.lot_size if lots >= 1 else 0


def segment_for_signal(underlying_segment: Segment) -> Segment:
    """Where the order goes for a signal computed on ``underlying_segment``."""
    return Segment.MCX_FO if underlying_segment is Segment.MCX_FO else Segment.NSE_FO


def kind_traded(segment: Segment) -> InstrumentKind:
    return InstrumentKind.FUTURE if segment is Segment.MCX_FO else InstrumentKind.OPTION
