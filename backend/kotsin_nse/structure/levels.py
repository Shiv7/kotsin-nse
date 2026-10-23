"""Pivot levels, and where they cluster into support and resistance.

Every level is computed from sessions BEFORE the day it is drawn on, so it is known at the open:

* **Daily** (previous session H, L, C): classic floor pivots P = (H + L + C) / 3, R1 = 2P − L,
  S1 = 2P − H, R2 = P + (H − L), S2 = P − (H − L), R3 = H + 2(P − L), S3 = L − 2(H − P); the central
  pivot range BC = (H + L) / 2, TC = 2P − BC; Camarilla H3/H4/L3/L4 = C ± (H − L) × 1.1 / 4 and / 2;
  the previous day's high, low and close.
* **Weekly / monthly** (the previous complete week / month): P, R1, S1, R2, S2, and its high and low.

**Clusters:** levels sorted by price are chained while each is within ``CLUSTER_TOL_ADR`` of a normal
day (the average daily range) of the one before. A chain of two or more is a zone [lowest, highest];
its weight is the sum of its members' (monthly 3, weekly 2, daily 1), so a zone where a monthly S1,
a weekly pivot and yesterday's low meet outranks three daily levels. Above the last price it is
resistance, below it support, around it "at price". The levels are a map, not a tested signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from ..market.session import ist_day

CLUSTER_TOL_ADR = 0.10
CAMARILLA = 1.1
WEIGHT = {"D": 1, "W": 2, "M": 3}


@dataclass(frozen=True, slots=True)
class PivotLevel:
    name: str  # e.g. "S1", "WP", "MR1", "PDH", "H3"
    price: float
    frame: str  # D | W | M


@dataclass(slots=True)
class Cluster:
    low: float
    high: float
    weight: int
    members: list[str] = field(default_factory=list)
    kind: str = ""  # SUPPORT | RESISTANCE | AT_PRICE
    distance_pct: float | None = None  # from the last price to the zone's near edge

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


def floor_pivots(h: float, lo: float, c: float, prefix: str = "") -> dict[str, float]:
    p = (h + lo + c) / 3
    return {
        f"{prefix}P": p,
        f"{prefix}R1": 2 * p - lo,
        f"{prefix}S1": 2 * p - h,
        f"{prefix}R2": p + (h - lo),
        f"{prefix}S2": p - (h - lo),
    }


def daily_levels(h: float, lo: float, c: float) -> list[PivotLevel]:
    fp = floor_pivots(h, lo, c)
    p = fp["P"]
    bc = (h + lo) / 2
    tc = 2 * p - bc
    rng = h - lo
    out = {
        **fp,
        "R3": h + 2 * (p - lo),
        "S3": lo - 2 * (h - p),
        "TC": max(tc, bc),
        "BC": min(tc, bc),
        "H3": c + rng * CAMARILLA / 4,
        "L3": c - rng * CAMARILLA / 4,
        "H4": c + rng * CAMARILLA / 2,
        "L4": c - rng * CAMARILLA / 2,
        "PDH": h,
        "PDL": lo,
        "PDC": c,
    }
    return [PivotLevel(k, v, "D") for k, v in out.items()]


def _period_levels(rows: Sequence[tuple[date, float, float, float]], key: Any, prefix: str, frame: str, today: date) -> list[PivotLevel]:
    """The previous complete period (week / month) before ``today``'s."""
    groups: dict[Any, list[tuple[date, float, float, float]]] = {}
    for r in rows:
        groups.setdefault(key(r[0]), []).append(r)
    done = [k for k in groups if k < key(today)]
    if not done:
        return []
    g = groups[max(done)]
    h, lo, c = max(r[1] for r in g), min(r[2] for r in g), g[-1][3]
    out = floor_pivots(h, lo, c, prefix)
    out[f"P{prefix}H"], out[f"P{prefix}L"] = h, lo
    return [PivotLevel(k, v, frame) for k, v in out.items()]


def pivot_levels(daily: Sequence[tuple[int, float, float, float]], today: date) -> list[PivotLevel]:
    """``daily`` = (ts, high, low, close) of completed sessions; only those before ``today`` count."""
    rows = sorted((ist_day(int(ts)), h, lo, c) for ts, h, lo, c in daily if h > 0 and lo > 0 and ist_day(int(ts)) < today)
    if not rows:
        return []
    _d, h, lo, c = rows[-1]
    out = daily_levels(h, lo, c)
    out += _period_levels(rows, lambda d: d.isocalendar()[:2], "W", "W", today)
    out += _period_levels(rows, lambda d: (d.year, d.month), "M", "M", today)
    return out


def clusters(levels: Sequence[PivotLevel], adr: float | None, last: float | None, tol_adr: float = CLUSTER_TOL_ADR) -> list[Cluster]:
    """Chains of two or more levels within ``tol_adr`` × ADR of each other, strongest first."""
    if not levels or not adr or adr <= 0:
        return []
    tol = tol_adr * adr
    lv = sorted(levels, key=lambda x: x.price)
    chains: list[list[PivotLevel]] = [[lv[0]]]
    for x in lv[1:]:
        if x.price - chains[-1][-1].price <= tol:
            chains[-1].append(x)
        else:
            chains.append([x])
    out = []
    for ch in chains:
        if len(ch) < 2:
            continue
        cl = Cluster(ch[0].price, ch[-1].price, sum(WEIGHT[x.frame] for x in ch), [x.name for x in ch])
        if last:
            if cl.low > last:
                cl.kind, cl.distance_pct = "RESISTANCE", (cl.low / last - 1) * 100
            elif cl.high < last:
                cl.kind, cl.distance_pct = "SUPPORT", (cl.high / last - 1) * 100
            else:
                cl.kind, cl.distance_pct = "AT_PRICE", 0.0
        out.append(cl)
    return sorted(out, key=lambda c: -c.weight)


def to_json(levels: Sequence[PivotLevel], cl: Sequence[Cluster], adr: float | None, last: float | None) -> dict[str, Any]:
    r = lambda x: round(x, 2)  # noqa: E731
    return {
        "adr": r(adr) if adr else None,
        "last": last,
        "tolerance": r(CLUSTER_TOL_ADR * adr) if adr else None,
        "levels": [{**asdict(x), "price": r(x.price)} for x in sorted(levels, key=lambda x: -x.price)],
        "clusters": [
            {**asdict(c), "low": r(c.low), "high": r(c.high), "mid": r(c.mid),
             "distance_pct": round(c.distance_pct, 2) if c.distance_pct is not None else None}
            for c in cl
        ],
    }


__all__ = ["CLUSTER_TOL_ADR", "Cluster", "PivotLevel", "clusters", "daily_levels", "floor_pivots", "pivot_levels", "to_json"]
