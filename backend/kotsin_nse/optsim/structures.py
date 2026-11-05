"""Option structures as the playbooks build them, by delta on a ``Day``: a list of legs with a signed lot count (negative
= sold) and the settlement close they were opened at. Every builder returns ``None`` when the chain cannot supply a
leg (a strike with no close, no implied volatility), so a book skips that day rather than opening on a hole.

* ``batman`` — the double ratio spread: sell ``ratio`` lots at ``sell_delta`` and buy one at ``buy_delta`` (nearer the
  money) on each side; the video's 16/20, 2:1. Net one naked lot per side beyond the sold strike.
* ``batman_winged`` — the same with one far wing bought per side at ``wing_delta``, which caps the tail.
* ``iron_condor`` — sell ``short_delta`` call and put, buy the hedges at ``hedge_delta`` (the 25/17 of the transcript).
* ``iron_fly`` — sell the ATM straddle, buy the wings ``wing_pts`` away.
* ``strangle`` — sell the call and the put at ``delta``, naked (the weekly playbook's starting position).
* ``strangle_hedged`` — the strangle with far wings.
"""

from __future__ import annotations

from dataclasses import dataclass

from .chain import Day


@dataclass(slots=True)
class Leg:
    expiry: str
    strike: float
    kind: str  # CE / PE
    qty: int  # lots, negative = sold
    entry: float  # the close it was opened at
    role: str = ""  # sell / hedge / wing

    def to_json(self) -> dict:
        return {"expiry": self.expiry, "strike": self.strike, "kind": self.kind, "qty": self.qty, "entry": self.entry, "role": self.role}

    @staticmethod
    def from_json(d: dict) -> Leg:
        return Leg(d["expiry"], float(d["strike"]), d["kind"], int(d["qty"]), float(d["entry"]), d.get("role", ""))


def _leg(day: Day, expiry: str, strike: float | None, kind: str, qty: int, role: str) -> Leg | None:
    if strike is None:
        return None
    px = day.close(expiry, strike, kind)
    if px is None or px <= 0:
        return None
    return Leg(expiry, strike, kind, qty, px, role)


def _next_out(day: Day, expiry: str, strike: float, kind: str, steps: int = 1) -> float | None:
    """The strike ``steps`` further out of the money than ``strike``."""
    ks = day.strikes(expiry)
    if strike not in ks:
        return None
    i = ks.index(strike)
    j = i + steps if kind == "CE" else i - steps
    return ks[j] if 0 <= j < len(ks) else None


def _next_in(day: Day, expiry: str, strike: float, kind: str, steps: int = 1) -> float | None:
    return _next_out(day, expiry, strike, kind, -steps)


def batman(day: Day, expiry: str, *, sell_delta: float = 0.16, buy_delta: float = 0.20, ratio: int = 2) -> list[Leg] | None:
    legs: list[Leg] = []
    for kind in ("PE", "CE"):
        ks, kb = day.strike_by_delta(expiry, kind, sell_delta), day.strike_by_delta(expiry, kind, buy_delta)
        if ks is None or kb is None:
            return None
        if (kind == "PE" and kb <= ks) or (kind == "CE" and kb >= ks):  # the bought leg must sit nearer the money
            kb = _next_in(day, expiry, ks, kind)
        s, b = _leg(day, expiry, ks, kind, -ratio, "sell"), _leg(day, expiry, kb, kind, 1, "hedge")
        if s is None or b is None:
            return None
        legs += [s, b]
    return legs


def batman_winged(day: Day, expiry: str, *, sell_delta: float = 0.16, buy_delta: float = 0.20, ratio: int = 2, wing_delta: float = 0.05) -> list[Leg] | None:
    base = batman(day, expiry, sell_delta=sell_delta, buy_delta=buy_delta, ratio=ratio)
    if base is None:
        return None
    for kind in ("PE", "CE"):
        kw = day.strike_by_delta(expiry, kind, wing_delta)
        sold = next(x.strike for x in base if x.kind == kind and x.role == "sell")
        if kw is None or (kind == "PE" and kw >= sold) or (kind == "CE" and kw <= sold):
            kw = _next_out(day, expiry, sold, kind, 2)
        w = _leg(day, expiry, kw, kind, ratio - 1, "wing")
        if w is None:
            return None
        base.append(w)
    return base


def iron_condor(day: Day, expiry: str, *, short_delta: float = 0.25, hedge_delta: float = 0.17) -> list[Leg] | None:
    legs: list[Leg] = []
    for kind in ("PE", "CE"):
        ks, kh = day.strike_by_delta(expiry, kind, short_delta), day.strike_by_delta(expiry, kind, hedge_delta)
        if ks is None or kh is None:
            return None
        if (kind == "PE" and kh >= ks) or (kind == "CE" and kh <= ks):
            kh = _next_out(day, expiry, ks, kind)
        s, h = _leg(day, expiry, ks, kind, -1, "sell"), _leg(day, expiry, kh, kind, 1, "hedge")
        if s is None or h is None:
            return None
        legs += [s, h]
    return legs


def iron_fly(day: Day, expiry: str, *, wing_pts: float = 300.0) -> list[Leg] | None:
    k = day.atm(expiry)
    if k is None:
        return None
    ks = day.strikes(expiry)
    wc = min(ks, key=lambda x: abs(x - (k + wing_pts)))
    wp = min(ks, key=lambda x: abs(x - (k - wing_pts)))
    legs = [_leg(day, expiry, k, "CE", -1, "sell"), _leg(day, expiry, k, "PE", -1, "sell"), _leg(day, expiry, wc, "CE", 1, "wing"), _leg(day, expiry, wp, "PE", 1, "wing")]
    return None if any(x is None for x in legs) else legs  # type: ignore[return-value]


def strangle(day: Day, expiry: str, *, delta: float = 0.10) -> list[Leg] | None:
    legs = [_leg(day, expiry, day.strike_by_delta(expiry, "PE", delta), "PE", -1, "sell"), _leg(day, expiry, day.strike_by_delta(expiry, "CE", delta), "CE", -1, "sell")]
    return None if any(x is None for x in legs) else legs  # type: ignore[return-value]


def strangle_hedged(day: Day, expiry: str, *, delta: float = 0.10, wing_delta: float = 0.03) -> list[Leg] | None:
    base = strangle(day, expiry, delta=delta)
    if base is None:
        return None
    for kind in ("PE", "CE"):
        sold = next(x.strike for x in base if x.kind == kind)
        kw = day.strike_by_delta(expiry, kind, wing_delta)
        if kw is None or (kind == "PE" and kw >= sold) or (kind == "CE" and kw <= sold):
            kw = _next_out(day, expiry, sold, kind, 2)
        w = _leg(day, expiry, kw, kind, 1, "wing")
        if w is None:
            return None
        base.append(w)
    return base


BUILDERS = {"batman": batman, "batman_winged": batman_winged, "iron_condor": iron_condor, "iron_fly": iron_fly, "strangle": strangle,
            "strangle_hedged": strangle_hedged}

__all__ = ["BUILDERS", "Leg", "batman", "batman_winged", "iron_condor", "iron_fly", "strangle", "strangle_hedged"]
