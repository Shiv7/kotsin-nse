"""One expiry's option chain read as a trader reads it: where the OI sits (walls), where it is being
added, the put-call ratio, the at-the-money straddle (the move the market prices in), the IV, and
the calls' and the puts' own leg for the 64-state table.

Options here are options on FUTURES (MCX, and NSE's own options settle against the same-month
future), so the pricing model is Black-76 on the underlying future of THAT expiry — GOLD's December
options sit on the February future, not on the front month.

Only the subscribed strikes are seen (±N around the money), so a wall is the largest OI *among
those*: one further out is invisible. ``walls_cover`` says how wide the window was.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .positioning import Leg

#: annual risk-free rate for Black-76 discounting — over days to weeks it moves IV by hundredths
RISK_FREE = 0.065
#: strikes within this fraction of the future count toward the calls' / puts' leg (research BAND)
LEG_BAND = 0.10
YEAR_S = 365.0 * 86400


@dataclass(frozen=True, slots=True)
class OptQuote:
    ltp: float | None
    prev_close: float | None
    oi: float | None
    oi_ref: float | None  # the OI an OI change is measured from (previous close, or first seen today)

    @property
    def chg_pct(self) -> float | None:
        if self.ltp and self.prev_close and self.prev_close > 0:
            return (self.ltp / self.prev_close - 1) * 100
        return None

    @property
    def oi_chg(self) -> float | None:
        return self.oi - self.oi_ref if self.oi is not None and self.oi_ref is not None else None


@dataclass(frozen=True, slots=True)
class StrikeRow:
    strike: float
    ce: OptQuote | None
    pe: OptQuote | None


def _n(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black76(f: float, k: float, t: float, sigma: float, call: bool, r: float = RISK_FREE) -> float:
    df = math.exp(-r * t)
    if sigma <= 0 or t <= 0:
        return df * max((f - k) if call else (k - f), 0.0)
    v = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * v * v) / v
    d2 = d1 - v
    if call:
        return df * (f * _n(d1) - k * _n(d2))
    return df * (k * _n(-d2) - f * _n(-d1))


def implied_vol(price: float | None, f: float, k: float, t: float, call: bool, r: float = RISK_FREE) -> float | None:
    """Annualised Black-76 IV by bisection, or None when the premium says nothing (at or below
    intrinsic, beyond a 500 % vol, or no time left)."""
    if not price or price <= 0 or f <= 0 or k <= 0 or t <= 0:
        return None
    if price <= black76(f, k, t, 0.0, call, r) + 1e-9:
        return None
    lo, hi = 1e-4, 5.0
    if black76(f, k, t, hi, call, r) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if black76(f, k, t, mid, call, r) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def leg_of(quotes: Sequence[OptQuote]) -> Leg | None:
    """OI-weighted premium change and summed OI change over the near-the-money strikes."""
    priced = [q for q in quotes if q.chg_pct is not None and q.oi]
    if not priced:
        return None
    w = sum(max(q.oi or 0, 1) for q in priced)
    px = sum((q.chg_pct or 0) * max(q.oi or 0, 1) for q in priced) / w
    with_ref = [q for q in quotes if q.oi_chg is not None]
    chg = sum(q.oi_chg or 0 for q in with_ref)
    ref = sum(q.oi_ref or 0 for q in with_ref)
    return Leg(price_chg_pct=px, oi_chg=chg, oi_chg_pct=(chg / ref * 100) if ref > 0 else None)


def _best(rows: Sequence[StrikeRow], side: str, key: str) -> dict[str, float] | None:
    vals = [
        (r.strike, v)
        for r in rows
        if (q := (r.ce if side == "CE" else r.pe)) is not None and (v := getattr(q, key)) is not None and v > 0
    ]
    if not vals:
        return None
    k, v = max(vals, key=lambda x: x[1])
    return {"strike": k, "value": v}


@dataclass(frozen=True, slots=True)
class ChainRead:
    view: dict[str, Any]  # what the panel shows
    calls: Leg | None  # this expiry's calls' and puts' legs
    puts: Leg | None
    #: the near-the-money quotes behind them, so a caller can read several expiries as one leg
    near_calls: tuple[OptQuote, ...] = ()
    near_puts: tuple[OptQuote, ...] = ()


def read_chain(
    rows: Sequence[StrikeRow],
    *,
    future: float,
    future_prev: float | None,
    t: float,
    t_prev: float | None,
) -> ChainRead:
    """``future`` = the live price of this expiry's underlying future; ``t`` / ``t_prev`` = years to
    expiry now / at the previous close (for the IV the previous premiums imply)."""
    rows = sorted(rows, key=lambda r: r.strike)
    out: dict[str, Any] = {"strikes": len(rows)}
    if not rows or future <= 0:
        return ChainRead(out, None, None)
    atm = min(rows, key=lambda r: abs(r.strike - future))
    out["atm"] = atm.strike
    out["walls_cover"] = [rows[0].strike, rows[-1].strike]

    if atm.ce and atm.pe and atm.ce.ltp and atm.pe.ltp:
        straddle = atm.ce.ltp + atm.pe.ltp
        out["straddle"] = straddle
        out["implied_move_pct"] = straddle / future * 100
        if atm.ce.prev_close and atm.pe.prev_close:
            prev = atm.ce.prev_close + atm.pe.prev_close
            out["straddle_prev"] = prev
            out["straddle_chg_pct"] = (straddle / prev - 1) * 100
    ivs = [
        implied_vol(q.ltp, future, atm.strike, t, call)
        for q, call in ((atm.ce, True), (atm.pe, False))
        if q is not None
    ]
    ivs = [v for v in ivs if v is not None]
    out["iv"] = sum(ivs) / len(ivs) * 100 if ivs else None
    if future_prev and t_prev:
        ivp = [
            implied_vol(q.prev_close, future_prev, atm.strike, t_prev, call)
            for q, call in ((atm.ce, True), (atm.pe, False))
            if q is not None
        ]
        ivp = [v for v in ivp if v is not None]
        out["iv_prev"] = sum(ivp) / len(ivp) * 100 if ivp else None

    ce_oi = sum(r.ce.oi or 0 for r in rows if r.ce)
    pe_oi = sum(r.pe.oi or 0 for r in rows if r.pe)
    ce_add = sum(r.ce.oi_chg or 0 for r in rows if r.ce and r.ce.oi_chg is not None)
    pe_add = sum(r.pe.oi_chg or 0 for r in rows if r.pe and r.pe.oi_chg is not None)
    out["call_oi"], out["put_oi"] = ce_oi, pe_oi
    out["call_oi_chg"], out["put_oi_chg"] = ce_add, pe_add
    out["pcr_oi"] = pe_oi / ce_oi if ce_oi > 0 else None
    # a ratio of changes only reads cleanly when calls were added: of a negative it says nothing
    out["pcr_oi_chg"] = pe_add / ce_add if ce_add > 0 else None
    out["call_wall"] = _best(rows, "CE", "oi")
    out["put_wall"] = _best(rows, "PE", "oi")
    out["call_add"] = _best(rows, "CE", "oi_chg")
    out["put_add"] = _best(rows, "PE", "oi_chg")

    near = [r for r in rows if abs(r.strike - future) <= LEG_BAND * future]
    near_calls = tuple(r.ce for r in near if r.ce)
    near_puts = tuple(r.pe for r in near if r.pe)
    calls, puts = leg_of(near_calls), leg_of(near_puts)
    out["calls"] = calls.to_json() if calls else None
    out["puts"] = puts.to_json() if puts else None

    def view(q: OptQuote | None, call: bool, k: float) -> dict[str, Any] | None:
        if q is None:
            return None
        iv = implied_vol(q.ltp, future, k, t, call)
        return {
            "ltp": q.ltp,
            "chg_pct": q.chg_pct,
            "oi": q.oi,
            "oi_chg": q.oi_chg,
            "iv": iv * 100 if iv is not None else None,
        }

    out["rows"] = [{"strike": r.strike, "CE": view(r.ce, True, r.strike), "PE": view(r.pe, False, r.strike)} for r in rows]
    return ChainRead(out, calls, puts, near_calls, near_puts)
