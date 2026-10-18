"""Where the volume traded, and where the time was spent.

**Volume profile** — each 1m bar's volume spread evenly over the price bins its low..high covers
(the feed has no trade-by-trade prices, so within a minute volume is assumed uniform — the standard
approximation, and finer than a 30m bar). POC = the busiest bin; the value area grows from the POC
toward whichever neighbour holds more volume until it holds ``VALUE_AREA`` of the total; HVN / LVN =
local peaks / troughs of the lightly smoothed histogram.

**Market profile** — one letter per ``TPO_MINUTES`` period from the open (A = 09:15–09:45, …); a bin
gets a period's letter if that period traded through it. POC and value area by TPO count; initial
balance = the first two periods (the first hour); single prints = bins inside the range touched by
one period only; a poor high/low = the extreme bin was touched by two or more periods (no tail,
unfinished business). Day type from the initial balance, by Dalton's taxonomy, simplified.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

VALUE_AREA = 0.70
TPO_MINUTES = 30
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def bin_size(price: float, tick: float = 0.05) -> float:
    """About 0.05 % of price, rounded up to a whole number of ticks: ~0.6 on a ₹1,200 stock."""
    raw = max(tick, price * 0.0005)
    return round(math.ceil(raw / tick) * tick, 4)


def _bins(lo: float, hi: float, step: float) -> range:
    return range(math.floor(lo / step + 1e-9), math.floor(hi / step + 1e-9) + 1)


def _value_area(weights: list[float], poc_i: int, share: float) -> tuple[int, int]:
    total = sum(weights)
    lo = hi = poc_i
    held = weights[poc_i]
    while held < share * total and (lo > 0 or hi < len(weights) - 1):
        up = weights[hi + 1] if hi < len(weights) - 1 else -1.0
        dn = weights[lo - 1] if lo > 0 else -1.0
        if up >= dn:
            hi += 1
            held += up
        else:
            lo -= 1
            held += dn
    return lo, hi


@dataclass(slots=True)
class VolumeProfile:
    step: float
    prices: list[float]
    volumes: list[float]
    poc: float
    vah: float
    val: float
    hvn: list[float]
    lvn: list[float]
    total: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def volume_profile(rows: Sequence[tuple[float, float, float]], step: float | None = None) -> VolumeProfile | None:
    """``rows`` = (high, low, volume) per 1m bar."""
    rows = [r for r in rows if r[2] > 0 and r[0] >= r[1] > 0]
    if not rows:
        return None
    step = step or bin_size(rows[-1][0])
    acc: dict[int, float] = {}
    for h, lo, v in rows:
        ks = _bins(lo, h, step)
        each = v / len(ks)
        for k in ks:
            acc[k] = acc.get(k, 0.0) + each
    keys = list(range(min(acc), max(acc) + 1))
    prices = [round(k * step, 4) for k in keys]
    vols = [acc.get(k, 0.0) for k in keys]
    poc_i = max(range(len(vols)), key=lambda i: vols[i])
    lo_i, hi_i = _value_area(vols, poc_i, VALUE_AREA)
    sm = [sum(vols[max(0, i - 1) : i + 2]) / len(vols[max(0, i - 1) : i + 2]) for i in range(len(vols))]
    hvn = [prices[i] for i in range(1, len(sm) - 1) if sm[i] > sm[i - 1] and sm[i] >= sm[i + 1] and sm[i] > 1.3 * (sum(sm) / len(sm))]
    lvn = [prices[i] for i in range(1, len(sm) - 1) if sm[i] < sm[i - 1] and sm[i] <= sm[i + 1] and sm[i] < 0.5 * (sum(sm) / len(sm))]
    return VolumeProfile(step, prices, [round(v, 1) for v in vols], prices[poc_i], prices[hi_i], prices[lo_i], hvn, lvn, round(sum(vols), 1))


@dataclass(slots=True)
class MarketProfile:
    step: float
    prices: list[float]
    letters: list[str]  # per price, the periods that traded there, e.g. "ABD"
    poc: float
    vah: float
    val: float
    ib_high: float | None
    ib_low: float | None
    single_prints: list[float]
    poor_high: bool
    poor_low: bool
    day_type: str
    periods: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def market_profile(
    rows: Sequence[tuple[int, float, float]], session_open_ts: int, step: float | None = None, minutes: int = TPO_MINUTES
) -> MarketProfile | None:
    """``rows`` = (bar start ts, high, low) per 1m bar of one session."""
    rows = [r for r in rows if r[1] >= r[2] > 0]
    if not rows:
        return None
    step = step or bin_size(rows[-1][1])
    period_hl: dict[int, list[float]] = {}
    for ts, h, lo in rows:
        p = int((ts - session_open_ts) // (minutes * 60))
        cur = period_hl.get(p)
        if cur is None:
            period_hl[p] = [h, lo]
        else:
            cur[0] = max(cur[0], h)
            cur[1] = min(cur[1], lo)
    tpo: dict[int, str] = {}
    for p in sorted(period_hl):
        h, lo = period_hl[p]
        for k in _bins(lo, h, step):
            tpo[k] = tpo.get(k, "") + LETTERS[p % len(LETTERS)]
    keys = list(range(min(tpo), max(tpo) + 1))
    prices = [round(k * step, 4) for k in keys]
    letters = [tpo.get(k, "") for k in keys]
    counts = [float(len(x)) for x in letters]
    mid = (len(keys) - 1) / 2
    poc_i = max(range(len(counts)), key=lambda i: (counts[i], -abs(i - mid)))
    lo_i, hi_i = _value_area(counts, poc_i, VALUE_AREA)
    ib = [period_hl[p] for p in (0, 1) if p in period_hl]
    ib_high = max(x[0] for x in ib) if ib else None
    ib_low = min(x[1] for x in ib) if ib else None
    singles = [prices[i] for i in range(1, len(counts) - 1) if counts[i] == 1]
    day_hi, day_lo = max(x[0] for x in period_hl.values()), min(x[1] for x in period_hl.values())
    return MarketProfile(
        step, prices, letters, prices[poc_i], prices[hi_i], prices[lo_i], ib_high, ib_low, singles,
        poor_high=counts[-1] >= 2, poor_low=counts[0] >= 2,
        day_type=_day_type(ib_high, ib_low, day_hi, day_lo) if len(period_hl) > 2 else "FORMING",
        periods=len(period_hl),
    )


def _day_type(ib_high: float | None, ib_low: float | None, hi: float, lo: float) -> str:
    """Dalton, simplified: how far the day extended beyond its first hour, and on which sides."""
    if ib_high is None or ib_low is None or ib_high <= ib_low:
        return "UNKNOWN"
    ib = ib_high - ib_low
    up, down = hi > ib_high, lo < ib_low
    ext = (hi - lo) / ib
    if up and down:
        return "NEUTRAL"
    if ext >= 2.0:
        return "TREND"
    if ext >= 1.15:
        return "NORMAL_VARIATION"
    return "NORMAL"
