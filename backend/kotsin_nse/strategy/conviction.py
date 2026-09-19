"""The FUDKII family's conviction matrix — five factors, one tier.

Ported from ``FudkiiConvictionMatrix`` with its per-exchange thresholds intact, because those were
fitted separately and collapsing them would silently re-tune two books:

=====================  =====  =====  =====
factor                   NSE    MCX    CDS
=====================  =====  =====  =====
volume STRONG / BASE   3.0/1.5 2.0/1.0 2.5/1.5
OI VERY_HIGH / HIGH    300/150 200/100 200/100
ΔPrice/ATR STR / EXT   0.8/1.2 1.2/2.0 1.0/1.5
=====================  =====  =====  =====

Two notes carried over verbatim from the original because they are load-bearing:

* the matrix's volume gate is ``0.9×``, which is **not** FUKAA's ``4×`` entry filter — they are
  different bars serving different purposes, and conflating them was an easy mistake to make;
* ``OI_HIGH`` on NSE is 150, the same number FUDKOI used as its threshold. One calibration point,
  two consumers.

Tiers S1–S4 are tradeable, S5–S6 are skips. Option volume is multiplied by 1.75 to compensate for
liquidity fragmented across strikes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Tier(StrEnum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"

    @property
    def tradeable(self) -> bool:
        return self in (Tier.S1, Tier.S2, Tier.S3, Tier.S4)


@dataclass(frozen=True, slots=True)
class ExchangeThresholds:
    volume_strong: float
    volume_base: float
    volume_gate: float
    oi_very_high: float
    oi_high: float
    price_atr_stretched: float
    price_atr_extended: float
    ref_oi_floor: float


THRESHOLDS: dict[str, ExchangeThresholds] = {
    "N": ExchangeThresholds(3.0, 1.5, 0.9, 300.0, 150.0, 0.8, 1.2, 5.0),
    "M": ExchangeThresholds(2.0, 1.0, 0.9, 200.0, 100.0, 1.2, 2.0, 8.0),
    "C": ExchangeThresholds(2.5, 1.5, 0.9, 200.0, 100.0, 1.0, 1.5, 3.0),
}

#: Applied to an option's own volume before comparison. Not applied to the underlying's.
OPTION_VOLUME_ADJUSTMENT = 1.75


def thresholds_for(exchange: str) -> ExchangeThresholds:
    """Unknown exchange codes fall back to the **strictest** table rather than to a permissive
    default. FUDKOI's ``default -> false`` silently dropped anything that was not N/M/C with no log
    line at all; failing strict and saying so is the same safety with an explanation."""
    return THRESHOLDS.get(exchange.upper(), THRESHOLDS["N"])


@dataclass(frozen=True, slots=True)
class ConvictionInput:
    exchange: str
    volume_surge: float | None
    oi_change_pct: float | None
    oi_buildup_pct: float | None
    price_change_over_atr: float | None
    rr: float
    is_option_volume: bool = False


@dataclass(frozen=True, slots=True)
class Conviction:
    composite: float  # 0–100
    tier: Tier
    volume_score: float
    oi_score: float
    momentum_score: float
    rr_score: float
    missing: tuple[str, ...]

    @property
    def tradeable(self) -> bool:
        return self.tier.tradeable


def _volume_score(surge: float | None, t: ExchangeThresholds, is_option: bool) -> float:
    if surge is None:
        return 0.0
    v = surge * (OPTION_VOLUME_ADJUSTMENT if is_option else 1.0)
    if v >= t.volume_strong:
        return 30.0
    if v >= t.volume_base:
        return 20.0
    if v >= t.volume_gate:
        return 10.0
    return 0.0


def _oi_score(change_pct: float | None, buildup_pct: float | None, t: ExchangeThresholds) -> float:
    if change_pct is None:
        return 0.0
    magnitude = abs(change_pct)
    score = 30.0 if magnitude >= t.oi_very_high else 20.0 if magnitude >= t.oi_high else 5.0
    if buildup_pct is not None and buildup_pct > 0:
        score += 5.0
    return min(score, 30.0)


def _momentum_score(price_over_atr: float | None, t: ExchangeThresholds) -> float:
    """Extension is not linearly good. A move already ``extended`` ATRs from its base has spent
    most of the range the target needs, so it scores *lower* than a merely ``stretched`` one."""
    if price_over_atr is None:
        return 0.0
    m = abs(price_over_atr)
    if m >= t.price_atr_extended:
        return 10.0
    if m >= t.price_atr_stretched:
        return 25.0
    return 15.0


def _rr_score(rr: float) -> float:
    if rr >= 2.5:
        return 15.0
    if rr >= 2.0:
        return 12.0
    if rr >= 1.5:
        return 9.0
    if rr >= 1.0:
        return 6.0
    return 3.0


def score(inp: ConvictionInput) -> Conviction:
    t = thresholds_for(inp.exchange)
    missing = tuple(
        name
        for name, value in (
            ("volume_surge", inp.volume_surge),
            ("oi_change_pct", inp.oi_change_pct),
            ("price_change_over_atr", inp.price_change_over_atr),
        )
        if value is None
    )
    vol = _volume_score(inp.volume_surge, t, inp.is_option_volume)
    oi = _oi_score(inp.oi_change_pct, inp.oi_buildup_pct, t)
    mom = _momentum_score(inp.price_change_over_atr, t)
    rr = _rr_score(inp.rr)
    composite = vol + oi + mom + rr  # max 100
    if composite >= 80:
        tier = Tier.S1
    elif composite >= 70:
        tier = Tier.S2
    elif composite >= 60:
        tier = Tier.S3
    elif composite >= 50:
        tier = Tier.S4
    elif composite >= 35:
        tier = Tier.S5
    else:
        tier = Tier.S6
    return Conviction(
        composite=round(composite, 2),
        tier=tier,
        volume_score=vol,
        oi_score=oi,
        momentum_score=mom,
        rr_score=rr,
        missing=missing,
    )
