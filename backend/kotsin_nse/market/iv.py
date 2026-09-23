"""Per-name implied volatility — a stock's own VIX — from its ATM option chain.

India VIX is the index's expected move. A single stock's option chain carries the same information
for that stock, and it is a different number: on 2026-09-23 09:45 CANBK's ATM implied vol was
21.8 % against an India VIX of 10.7. Banding a stock on the index therefore misreads it twice
over — the level is wrong, and so is the regime. An option's price depends on its parent, not on
the index, so the option-side ladder is clustered at a tolerance derived from the parent's own
implied regime and ATR, projected through delta:

    tolerance = k(name) × ATR30(parent) × δ / premium

**Where the band comes from.** ``k`` follows the same ladder as everywhere else, but the ratio is
today's ATM IV against the name's *own* median IV over its last sessions (``MIN_HISTORY`` before
the name is trusted; India VIX is the fallback until then). Not IV against realised vol: a single
stock's implied vol always sits above its realised (the variance premium, and expiry week
inflates it further), so that ratio reads EXTREME on a perfectly ordinary day.

**History.** Seeded from the daily candles the leg loader already fetches — the option's close on
each past session against the parent's close that day — and replaced by live-logged IV as it
accumulates, one point per session, on disk under ``data/iv/<SYMBOL>.json``.

Black-Scholes here is the standard European price with a flat rate and no dividend, inverted by
bisection. Crude but stated; the ratio-to-own-median is what the band uses, and a constant bias
cancels out of a ratio.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from .volatility import CLUSTER_K, Regime, band_from_realised

RISK_FREE = 0.065
IV_MIN, IV_MAX = 0.01, 5.0
#: sessions of a name's own IV before it bands on its own median rather than on India VIX
MIN_HISTORY = 10
MEDIAN_SESSIONS = 20
KEEP_SESSIONS = 60


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float, *, call: bool, r: float = RISK_FREE) -> float:
    if t_years <= 0 or sigma <= 0:
        return max(0.0, (spot - strike) if call else (strike - spot))
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + sigma * sigma / 2) * t_years) / sq
    d2 = d1 - sq
    disc = strike * math.exp(-r * t_years)
    if call:
        return spot * _norm_cdf(d1) - disc * _norm_cdf(d2)
    return disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t_years: float, *, call: bool, r: float = RISK_FREE) -> float | None:
    """The sigma that prices the option, or None when no sigma does (at or under intrinsic, expired,
    or above the price a 500 % vol would give — a bad print, not a volatility)."""
    if price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None
    disc = strike * math.exp(-r * t_years)
    intrinsic = max(0.0, spot - disc) if call else max(0.0, disc - spot)
    if price <= intrinsic + 1e-9 or price >= bs_price(spot, strike, t_years, IV_MAX, call=call, r=r):
        return None
    lo, hi = IV_MIN, IV_MAX
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t_years, mid, call=call, r=r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to_expiry(expiry: str, on: date) -> float:
    return max(0.0, (date.fromisoformat(expiry) - on).days) / 365.0


def atm_iv(
    spot: float, strike: float, t_years: float, call_mid: float | None, put_mid: float | None, *, r: float = RISK_FREE
) -> float | None:
    """The ATM implied vol: the mean of the call's and the put's, from whichever solve."""
    ivs = [
        v
        for v in (
            implied_vol(call_mid, spot, strike, t_years, call=True, r=r) if call_mid else None,
            implied_vol(put_mid, spot, strike, t_years, call=False, r=r) if put_mid else None,
        )
        if v is not None
    ]
    return sum(ivs) / len(ivs) if ivs else None


def ladder_tolerance_pct(k: float, atr30: float, delta: float, premium: float) -> float:
    """The option ladder's merge tolerance, in percent of the premium: the parent's expected noise
    (k × its ATR) carried onto the option through delta. Not the option's own ATR — that is
    dominated by gap and crush and runs to three quarters of the premium."""
    if premium <= 0 or atr30 <= 0:
        return 0.0
    return k * atr30 * abs(delta) / premium * 100.0


@dataclass(frozen=True, slots=True)
class IvPoint:
    day: date
    iv: float


def seed_points(
    rows: list[dict[str, Any]], equity_close_by_day: dict[str, float], *, strike: float, call: bool, expiry: str
) -> list[IvPoint]:
    """IV per past session from an option's daily closes against the parent's daily closes."""
    out: list[IvPoint] = []
    for r in rows:
        day = str(r.get("dt", ""))[:10]
        spot = equity_close_by_day.get(day)
        if not spot or not day:
            continue
        d = date.fromisoformat(day)
        v = implied_vol(float(r["c"]), spot, strike, years_to_expiry(expiry, d), call=call)
        if v is not None:
            out.append(IvPoint(d, v))
    return out


def merge_points(*series: list[IvPoint]) -> list[IvPoint]:
    """Average the call's and the put's IV on each day they both have one; keep either alone."""
    by: dict[date, list[float]] = {}
    for s in series:
        for p in s:
            by.setdefault(p.day, []).append(p.iv)
    return [IvPoint(d, sum(v) / len(v)) for d, v in sorted(by.items())]


class IvHistory:
    """One IV point per session per name, on disk. Live points win over seeded ones."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._by: dict[str, list[IvPoint]] = {}
        self._live_days: dict[str, set[date]] = {}

    def path(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.json"

    def points(self, symbol: str) -> list[IvPoint]:
        sym = symbol.upper()
        if sym not in self._by:
            self._by[sym] = self._load(sym)
        return self._by[sym]

    def _load(self, sym: str) -> list[IvPoint]:
        try:
            raw = json.loads(self.path(sym).read_text())
        except (OSError, ValueError):
            return []
        out = []
        for r in raw if isinstance(raw, list) else []:
            try:
                out.append(IvPoint(date.fromisoformat(r[0]), float(r[1])))
            except (ValueError, TypeError, IndexError):
                continue
        return sorted(out, key=lambda p: p.day)

    def save(self, symbol: str) -> None:
        sym = symbol.upper()
        pts = self.points(sym)[-KEEP_SESSIONS:]
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path(sym).with_suffix(".json.tmp")
        tmp.write_text(json.dumps([[p.day.isoformat(), round(p.iv, 5)] for p in pts]))
        tmp.replace(self.path(sym))

    def save_all(self) -> int:
        for sym in list(self._by):
            self.save(sym)
        return len(self._by)

    def record(self, symbol: str, day: date, iv: float) -> None:
        """Today's live point replaces today's earlier one; a live day is never re-seeded."""
        sym = symbol.upper()
        pts = [p for p in self.points(sym) if p.day != day]
        pts.append(IvPoint(day, iv))
        self._by[sym] = sorted(pts, key=lambda p: p.day)[-KEEP_SESSIONS:]
        self._live_days.setdefault(sym, set()).add(day)

    def seed(self, symbol: str, points: list[IvPoint]) -> int:
        """Fill sessions the name has no point for. Returns how many were added."""
        sym = symbol.upper()
        have = {p.day for p in self.points(sym)}
        added = [p for p in points if p.day not in have]
        if added:
            self._by[sym] = sorted([*self.points(sym), *added], key=lambda p: p.day)[-KEEP_SESSIONS:]
        return len(added)

    def median_before(self, symbol: str, day: date, *, sessions: int = MEDIAN_SESSIONS) -> tuple[float | None, int]:
        """The median of the last ``sessions`` points strictly before ``day``, and how many."""
        prior = [p.iv for p in self.points(symbol) if p.day < day][-sessions:]
        if len(prior) < MIN_HISTORY:
            return None, len(prior)
        return median(prior), len(prior)


def regime_for_name(iv_now: float | None, median_iv: float | None, sessions: int, *, fallback: Regime) -> Regime:
    """The name's own regime — today's ATM IV against its own median — or the fallback."""
    if iv_now is None or median_iv is None or median_iv <= 0:
        return fallback
    ratio = iv_now / median_iv
    band = band_from_realised(ratio)
    return Regime(
        band=band,
        k=CLUSTER_K[band],
        source="stock_iv",
        value=ratio,
        detail=(
            f"own ATM IV {iv_now * 100:.1f}% is {ratio:.2f}x its {sessions}-session median "
            f"{median_iv * 100:.1f}% -> {band.value}, k {CLUSTER_K[band]:.2f}"
        ),
    )
