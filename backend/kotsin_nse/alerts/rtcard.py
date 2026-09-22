"""The FUDKII-RT card: walls on both sides, a dual-trigger stop, and honest odds.

Ported from the dashboard's ``FudkiiRtTabContent``, with two deliberate departures.

**Both walls, not one.** The reference scores only the wall *ahead* — the pivot cluster standing in
front of the breakout, which is what decides whether RT fades. A wall *behind* is the other half of
the same fact: structure under the stop. The two are scored identically, on
:data:`~kotsin_nse.bars.pivots.WALL_MIN_STRENGTH` (5.2, already this repo's live value, meaning one
daily level is not a wall but a daily plus a weekly is), so "strong wall ahead" and "strong wall
behind" are comparable statements rather than two different scales.

**A stop that fires on whichever side hits first.** The stop is defined on the equity — from the
pivot ladder, so it is fixed for the session — and mirrored onto the option through delta. Either
can trigger: the equity reaching its level, or the option's own LTP reaching the delta-adjusted
one. Delta is not a constant, so the option-side level is restamped on every read (``deltaTs``) and
drifts as spot moves; the equity level does not move all day, which is the point of using pivots
for it.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..bars.pivots import WALL_MIN_STRENGTH, Zone
from ..bars.unified import UnifiedBar
from ..instrument.select import map_levels_to_option

#: Delta is restamped at least this often. Spot moves, so a level computed off a stale delta is a
#: level in the wrong place.
DELTA_REFRESH_S = 10.0


@dataclass(slots=True)
class Wall:
    price: float
    strength: float
    members: list[str]
    timeframes: list[str]
    dist_atr: float
    dist_pct: float
    grade: str
    qualifies: bool
    side: str  # AHEAD | BEHIND

    def to_json(self) -> dict[str, Any]:
        return {
            "price": round(self.price, 2),
            "strength": round(self.strength, 2),
            "members": self.members,
            "timeframes": self.timeframes,
            "levels": len(self.members),
            "distAtr": round(self.dist_atr, 2),
            "distPct": round(self.dist_pct, 2),
            "grade": self.grade,
            "qualifies": self.qualifies,
            "min": WALL_MIN_STRENGTH,
            "side": self.side,
        }


def _grade(strength: float) -> str:
    if strength >= WALL_MIN_STRENGTH * 2:
        return "FORTRESS"
    if strength >= WALL_MIN_STRENGTH:
        return "STRONG"
    if strength >= WALL_MIN_STRENGTH * 0.6:
        return "AVERAGE"
    return "WEAK"


def find_wall(zones: list[Zone], price: float, atr: float, *, ahead: bool, bullish: bool) -> Wall | None:
    """Nearest zone on one side. ``ahead`` means in the direction of the trade."""
    if not zones or atr <= 0 or price <= 0:
        return None
    want_above = bullish if ahead else not bullish
    side = [z for z in zones if (z.price > price) == want_above and z.price != price]
    if not side:
        return None
    z = min(side, key=lambda x: abs(x.price - price))
    tfs = sorted({m.split(".")[0] for m in z.members})
    return Wall(
        price=z.price,
        strength=z.strength,
        members=z.members,
        timeframes=tfs,
        dist_atr=abs(z.price - price) / atr,
        dist_pct=abs(z.price - price) / price * 100,
        grade=_grade(z.strength),
        qualifies=z.strength >= WALL_MIN_STRENGTH,
        side="AHEAD" if ahead else "BEHIND",
    )


def hit_probability(entry: float, stop: float, target: float) -> dict[str, Any]:
    """Odds of reaching the target before the stop, from geometry alone.

    For a driftless walk the probability of touching one barrier before the other is simply the
    *other* barrier's distance over the total — gambler's ruin. So a 7R setup is not a good trade
    with a high chance of paying; it is a 12% chance of paying seven times.

    That is the whole point of putting it on the card. It assumes no drift and ignores theta, which
    means it is the **ceiling** for an option: time decay only takes from it. Any real edge has to
    come from the trigger being better than a coin, and this is the number it has to beat.
    """
    risk = abs(entry - stop)
    reward = abs(target - entry)
    total = risk + reward
    if total <= 0:
        return {"pT1": None, "note": "no distance between the levels"}
    p = risk / total
    return {
        "pT1": round(p * 100, 1),
        "risk": round(risk, 2),
        "reward": round(reward, 2),
        "model": "driftless barrier (gambler's ruin)",
        "note": (
            "geometry only — assumes no drift and ignores theta, so it is an upper bound for the "
            "option leg. The trigger has to beat this, not inherit it."
        ),
    }


def same_slot_volume(history: list[UnifiedBar], bar: UnifiedBar) -> dict[str, Any] | None:
    """This bar's volume against the same time-of-day slot in earlier sessions.

    The reference calls it ``rtVolVsBaselineRatio``. A 30m bar's volume is meaningless against the
    day's average — 09:15 and 14:15 are different animals — so the comparison is slot against slot.
    Median, not mean: one event day in the lookback would otherwise move the baseline more than the
    bar being judged.
    """
    slot = bar.ist[-5:] if getattr(bar, "ist", None) else None
    if not slot:
        return None
    prior = [b.volume for b in history if b.ts != bar.ts and getattr(b, "ist", "")[-5:] == slot]
    if len(prior) < 3:
        return None
    med = statistics.median(prior)
    if med <= 0:
        return None
    return {
        "ratio": round(bar.volume / med, 2),
        "median": round(med, 0),
        "sessions": len(prior),
        "slot": slot,
    }


def dual_stop(
    *,
    bullish: bool,
    equity_entry: float,
    equity_stop: float,
    option_entry: float | None,
    option_ltp: float | None,
    equity_ltp: float | None,
    delta: float,
    basis: str,
) -> dict[str, Any]:
    """One stop, two ways to reach it, whichever comes first.

    The equity level is the authority — it comes from the pivot ladder and does not move all
    session. The option level is that same stop expressed through delta, and it *does* move,
    because delta does. Either triggering is the trade being over.
    """
    move = abs(equity_entry - equity_stop)
    opt_stop = None
    if option_entry is not None and delta > 0:
        # Projected by the engine's own map_levels_to_option, not by a second copy of the formula.
        # The card has to name the level the engine would actually set on the position; two
        # implementations of one projection drift the moment either is retuned.
        opt_stop, _ = map_levels_to_option(
            equity_entry=equity_entry,
            equity_stop=equity_stop,
            equity_targets=(),
            option_premium=option_entry,
            delta=delta,
        )

    eq_dist_pct = (
        (equity_ltp - equity_stop) / equity_ltp * 100 * (1 if bullish else -1)
        if equity_ltp
        else None
    )
    opt_dist_pct = (
        (option_ltp - opt_stop) / option_ltp * 100
        if option_ltp and opt_stop and option_ltp > 0
        else None
    )

    # Has either side already gone? The option level is reached when the premium falls to it,
    # which is what happens when the underlying moves *against* the trade — a CE bleeds as spot
    # falls, a PE bleeds as spot rises. So the option leg stops the trade out on adverse movement
    # even when the equity has not yet reached its own level, which is the case worth flagging.
    eq_hit = eq_dist_pct is not None and eq_dist_pct <= 0
    opt_hit = opt_dist_pct is not None and opt_dist_pct <= 0

    nearer = None
    if eq_dist_pct is not None and opt_dist_pct is not None:
        nearer = "OPTION" if opt_dist_pct < eq_dist_pct else "EQUITY"
    elif eq_dist_pct is not None:
        nearer = "EQUITY"
    elif opt_dist_pct is not None:
        nearer = "OPTION"

    return {
        "basis": basis,
        "constantForSession": basis == "pivot",
        "equityStop": round(equity_stop, 2),
        "equityMove": round(move, 2),
        "equityDistPct": None if eq_dist_pct is None else round(eq_dist_pct, 2),
        "optionStop": opt_stop,
        "optionDistPct": None if opt_dist_pct is None else round(opt_dist_pct, 2),
        "delta": round(delta, 3),
        "deltaTs": time.time(),
        "deltaRefreshS": DELTA_REFRESH_S,
        "triggersFirst": nearer,
        "equityHit": eq_hit,
        "optionHit": opt_hit,
        "triggered": eq_hit or opt_hit,
        "triggeredBy": "EQUITY" if eq_hit else ("OPTION" if opt_hit else None),
        "rule": "exit on whichever is reached first — the equity level or the delta-adjusted option level",
    }


def option_ladder(
    *, option_entry: float, equity_entry: float, targets: list[float], delta: float
) -> list[dict[str, Any]]:
    """Equity targets mirrored onto the option through delta.

    Linear in delta, so it *understates* a large favourable move — gamma lifts delta as the option
    goes in the money and the real premium runs further than this says. Labelled modelled for that
    reason; it is a floor on the upside, not a forecast.
    """
    _, projected = map_levels_to_option(
        equity_entry=equity_entry,
        equity_stop=equity_entry,  # unused for the target side
        equity_targets=tuple(targets),
        option_premium=option_entry,
        delta=delta,
    )
    out = []
    for i, (t, opt) in enumerate(zip(targets, projected, strict=False), start=1):
        move = abs(t - equity_entry)
        out.append(
            {
                "n": i,
                "equity": round(t, 2),
                "option": opt,
                "equityMove": round(move, 2),
                "optionGainPct": round((opt - option_entry) / option_entry * 100, 1)
                if option_entry > 0
                else None,
                "source": "pivot confluence wall (strength >= 5.2)",
            }
        )
    return out


def confidence(
    *, wall_ahead: Wall | None, wall_behind: Wall | None, surge: float | None, p_t1: float | None
) -> dict[str, Any]:
    """0-100, and every component is named so a number can be argued with.

    A wall *behind* is support under the stop and adds; a wall *ahead* is what price must pay
    through and subtracts. That asymmetry is the whole reason both are computed — the reference
    scored only the obstacle, which makes a trade with nothing beneath it look identical to one
    standing on a monthly pivot.
    """
    parts: list[dict[str, Any]] = []
    score = 50.0

    if wall_behind is not None:
        pts = min(20.0, wall_behind.strength / WALL_MIN_STRENGTH * 12)
        if wall_behind.dist_atr > 3:
            pts *= 0.4  # too far to be support for this trade
        score += pts
        parts.append(
            {
                "factor": "wall behind (support)",
                "points": round(pts, 1),
                "detail": f"{wall_behind.grade} {wall_behind.strength:.1f} at {wall_behind.dist_atr:.2f} ATR",
            }
        )
    else:
        score -= 10
        parts.append({"factor": "no wall behind", "points": -10.0, "detail": "nothing under the stop"})

    if wall_ahead is not None:
        pen = -min(25.0, wall_ahead.strength / WALL_MIN_STRENGTH * 15)
        if wall_ahead.dist_atr > 2:
            pen *= 0.4  # far enough that T1 is reached before it matters
        score += pen
        parts.append(
            {
                "factor": "wall ahead (obstacle)",
                "points": round(pen, 1),
                "detail": f"{wall_ahead.grade} {wall_ahead.strength:.1f} at {wall_ahead.dist_atr:.2f} ATR",
            }
        )
    else:
        score += 8
        parts.append({"factor": "clear ahead", "points": 8.0, "detail": "no zone between price and open air"})

    if surge is not None:
        pts = max(-8.0, min(12.0, (surge - 1.0) * 8))
        score += pts
        parts.append({"factor": "volume surge", "points": round(pts, 1), "detail": f"{surge:.2f}x"})

    if p_t1 is not None:
        pts = (p_t1 - 50) * 0.2
        score += pts
        parts.append(
            {"factor": "geometric odds", "points": round(pts, 1), "detail": f"{p_t1:.0f}% to T1 first"}
        )

    return {
        "score": round(max(0.0, min(100.0, score)), 1),
        "components": parts,
        "note": "50 is neutral; a wall behind adds, a wall ahead subtracts.",
    }


def dte(expiry: str, today: date | None = None) -> int | None:
    try:
        d = date.fromisoformat(expiry[:10])
    except (ValueError, TypeError):
        return None
    return (d - (today or date.today())).days


# ── FUDKII-RT position sizing ────────────────────────────────────────────────────────────────────

#: Per trade, inclusive of margin. Whatever is not spent goes back to the wallet rather than being
#: reserved — an unspent slot is capital that can take the next signal.
RT_MAX_CAPITAL_INR = 100_000.0
#: Hard lot cap. The binding constraint is whichever of the two is *lower*, so a contract whose
#: single lot already costs more than the cap is declined rather than part-filled.
RT_MAX_LOTS = 4
#: Concurrency ceiling. A trade occupies exactly one slot from entry to final exit — scaling out in
#: tranches at T1-T4 is one trade leaving in pieces, not four trades.
RT_MAX_CONCURRENT = 30


def size(
    *,
    option_premium: float | None,
    lot_size: int,
    multiplier: int = 1,
    open_trades: int = 0,
    max_capital: float = RT_MAX_CAPITAL_INR,
    max_lots: int = RT_MAX_LOTS,
    max_concurrent: int = RT_MAX_CONCURRENT,
) -> dict[str, Any]:
    """Lots for one FUDKII-RT entry, and what the caps did to it.

    Two ceilings, and the lower one wins: ``max_capital`` rupees, or ``max_lots`` lots. Reporting
    which one bound matters — a trade cut to one lot by a rich premium is a different fact from one
    cut to four by the lot cap, and only the first is a liquidity problem.
    """
    cost_per_lot = (option_premium or 0) * max(lot_size, 1) * max(multiplier, 1)
    slots_left = max(0, max_concurrent - open_trades)

    if cost_per_lot <= 0:
        return {
            "lots": 0, "qty": 0, "capital": 0.0, "costPerLot": 0.0,
            "binding": "no premium", "rejected": True,
            "reason": "the contract is not quoting, so it cannot be sized",
            "slotsLeft": slots_left, "maxConcurrent": max_concurrent,
            "unusedReturnedToWallet": 0.0, "capPerTrade": max_capital, "maxLots": max_lots,
        }

    by_capital = int(max_capital // cost_per_lot)
    lots = min(max_lots, by_capital)
    binding = "lot cap" if by_capital >= max_lots else "capital cap"

    if slots_left <= 0:
        return {
            "lots": 0, "qty": 0, "capital": 0.0, "costPerLot": round(cost_per_lot, 2),
            "binding": "concurrency", "rejected": True,
            "reason": f"{open_trades} trades already open against a {max_concurrent} ceiling",
            "slotsLeft": 0, "maxConcurrent": max_concurrent,
            "unusedReturnedToWallet": 0.0, "capPerTrade": max_capital, "maxLots": max_lots,
        }

    if lots < 1:
        return {
            "lots": 0, "qty": 0, "capital": 0.0, "costPerLot": round(cost_per_lot, 2),
            "binding": "capital cap", "rejected": True,
            "reason": (
                f"one lot costs Rs {cost_per_lot:,.0f}, above the Rs {max_capital:,.0f} a trade may "
                f"take — declined rather than part-filled"
            ),
            "slotsLeft": slots_left, "maxConcurrent": max_concurrent,
            "unusedReturnedToWallet": 0.0, "capPerTrade": max_capital, "maxLots": max_lots,
        }

    capital = lots * cost_per_lot
    return {
        "lots": lots,
        "qty": lots * max(lot_size, 1),
        "capital": round(capital, 2),
        "costPerLot": round(cost_per_lot, 2),
        "binding": binding,
        "rejected": False,
        "reason": "",
        "slotsLeft": slots_left,
        "maxConcurrent": max_concurrent,
        # Not reserved: the cap is a ceiling on what a trade may take, not an allocation it holds.
        "unusedReturnedToWallet": round(max_capital - capital, 2),
        "capPerTrade": max_capital,
        "maxLots": max_lots,
    }
