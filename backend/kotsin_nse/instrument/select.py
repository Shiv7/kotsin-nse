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

from collections.abc import Mapping
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
    #: a strike under this delta barely responds to the move being predicted, whatever its open
    #: interest. ETERNAL on 2026-09-24 had MORE open interest at its target strike than one ATR
    #: out, on 0.15 delta; buying that is buying where positions are parked, not where the thesis
    #: pays. Applied only to the two candidates, never to the fallback walk.
    min_delta: float = 0.20
    #: how far the further strike must beat the nearer one on every liquidity measure before its
    #: lower delta is accepted. HAVELLS the same day: 10 % more open interest for a quarter less
    #: delta is a bad trade; 2-3x more is not.
    oi_margin: float = 1.5


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


def beats_on_liquidity(
    b: tuple[float, float], a: tuple[float, float], margin: float
) -> bool:
    """Whether ``b`` beats ``a`` by ``margin`` on **every** liquidity measure both of them have.

    Deliberately conservative, and the reason is a real trade. HAVELLS on 2026-09-24: the target
    strike carried 549k open interest against 499k at the nearer one — a 10 % edge — and 0.36 delta
    against 0.50. Paying a quarter of the option's responsiveness for a tenth more open interest is
    a bad trade, so a clear margin is required before the further strike is taken. Where neither
    side has data the answer is False: the nearer strike stands.
    """
    ratios = []
    for vb, va in zip(b, a, strict=True):
        if va > 0:
            ratios.append(vb / va)
        elif vb > 0:
            ratios.append(float("inf"))
    return bool(ratios) and min(ratios) >= margin


def strike_candidates(
    *,
    otm: list[Instrument],
    spot: float,
    direction: Direction,
    atr: float,
    target1: float | None,
    liquidity: Mapping[str, tuple[float, float]],
    delta_floor: float,
    oi_margin: float,
) -> tuple[list[Instrument], str]:
    """The two strikes worth considering, best first, and why.

    **A** is the strike one ATR beyond spot — where the move the trigger predicts actually gets to.
    **B** is the strike at the confluence target — where the move is expected to stop. Open interest
    is lumpy rather than decaying with distance (RELIANCE 2026-09-24 held 12.5 m lots at the 1300
    call against 3.5 m one strike from spot), so which of the two is the tradeable contract is a
    real question and liquidity is the right arbiter — but only between strikes the thesis
    supports. A strike under ``delta_floor`` barely responds to the move being predicted, however
    much open interest is parked on it: ETERNAL that day had MORE open interest at its target strike
    than at the nearer one, on 0.15 delta.

    Returns the preferred order; the caller walks outward from there if neither is tradeable.
    """
    if not otm or spot <= 0 or atr <= 0:
        return [], ""
    bullish = direction is Direction.BULLISH
    ot = direction.option_type

    def nearest(level: float) -> Instrument | None:
        return min(otm, key=lambda i: abs(i.strike - level), default=None)

    a = nearest(spot + atr if bullish else spot - atr)
    b = nearest(target1) if target1 else None
    picks = [i for i in (a, b) if i is not None]
    if not picks:
        return [], ""
    if b is not None and a is not None and b.scrip_code == a.scrip_code:
        return [a], "one strike serves both"

    live = [i for i in picks if abs(estimate_delta(spot=spot, strike=i.strike, option_type=ot)) >= delta_floor]
    blocked = [i for i in picks if i not in live]
    low = ",".join(f"{i.strike:g}" for i in blocked)
    note = f"delta<{delta_floor:.2f}: {low}" if blocked else ""
    if not live:
        return [], note or "both candidates under the delta floor"
    if len(live) == 1:
        return live, note
    la, lb = liquidity.get(a.scrip_code, (0.0, 0.0)), liquidity.get(b.scrip_code, (0.0, 0.0))
    if beats_on_liquidity(lb, la, oi_margin):
        return [b, a], f"{b.strike:g} clears {a.strike:g} by {oi_margin:g}x on liquidity"
    return [a, b], note


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
    atr: float = 0.0,
    liquidity: Mapping[str, tuple[float, float]] | None = None,
) -> Selection:
    """The tradeable OTM strike this move should be expressed in.

    With ``atr`` and ``liquidity`` the two-candidate rule applies (``strike_candidates``): one ATR
    out against the confluence target, decided on liquidity, floored on delta. Without them this is
    the old behaviour — nearest to the target — which is what the trigger-card preview uses.
    """
    pol = policy or SelectionPolicy()
    want = direction.option_type
    anchor = otm_strike_anchor(spot=spot, target1=target1, direction=direction)
    candidates = [i for i in chain if i.option_type is want and i.strike > 0]
    if not candidates:
        return Selection(None, reason=f"no {want.value} strikes in the chain", anchor=anchor)

    # OTM at entry: calls above spot, puts below.
    if want is OptionType.CE:
        otm = [i for i in candidates if i.strike > spot]
    else:
        otm = [i for i in candidates if i.strike < spot]
    pool = otm or candidates  # a chain with no OTM strike is odd but not a reason to skip the trade
    note = ""
    if liquidity is not None and atr > 0:
        # The two candidates first, best first; then everything else, most-traded first. The
        # candidates are a preference, not a cage — "the best strike is one-sided" must fall
        # through to the next suitable OTM rather than abandoning the trigger (found live
        # 2026-09-24: INDUSTOWER and RADICO were lost to a single one-sided quote).
        picks, note = strike_candidates(
            otm=pool, spot=spot, direction=direction, atr=atr, target1=target1,
            liquidity=liquidity, delta_floor=pol.min_delta, oi_margin=pol.oi_margin,
        )
        chosen = {i.scrip_code for i in picks}
        rest = rank_by_liquidity([i for i in pool if i.scrip_code not in chosen], liquidity, anchor)
        pool = picks + rest
        if picks:
            anchor = picks[0].strike
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
        return Selection(inst, premium=premium, anchor=anchor, spread_pct=spread,
                         reason=f"ok ({note})" if note else "ok")
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
