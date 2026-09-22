"""Volatility regime, and the pivot-cluster width it implies.

Pivot levels are merged into zones when they sit within ``k x ATR`` of each other. This module
decides ``k``.

**Why ``k`` moves at all, when ATR already scales with volatility.** A 3.6x range in India VIX
produces a 3.6x range in the 30m expected move, so ``k x ATR`` already widens in rupees without
touching ``k``. ``k`` is therefore a *second-order* correction, and it exists for one reason: VIX is
30-day **implied** vol while ATR(14) on 30m bars is a trailing seven hours of **realised** tape.
When vol spikes, ATR has not seen it yet and under-measures the noise the next trade must survive;
when vol collapses, ATR still carries the decayed spike and over-measures it. ``k`` closes that gap.

Scaling ``k`` linearly with VIX as well would double-count and produce absurd zones at the top,
which is why the ladder spans roughly 3x and not 10x.

**Direction: more volatility means a wider ``k``.** In a calm tape price respects a pivot to a few
ticks and the fine distinction between ``1d.S1`` and ``1d.FIB_S1`` is real. In stress the market
trades zones, not lines, and two levels fifteen paise apart are one level that a single bar covers.
Merging harder yields fewer, stronger zones — and pushes the nearest zone further from price, which
is the same direction as the only repair that has ever worked on this book: ``FUDKII.md`` §8 measured
a median stop 0.23% from entry, 89% inside 0.5%, a median hold of one bar and 80% of exits at the
stop, and lifting -1.40R to -0.27R took a 1.0-1.5 ATR stop floor.

**The anchor.** The old stack ran a flat ``CLUSTER_ATR_MULTIPLE = 0.3``. India VIX spends most of
its life between 12 and 16, so 0.3 was calibrated in NEUTRAL, and NEUTRAL is where 0.3 sits here.

**Commodities do not get India VIX.** An equity-index implied vol says nothing about crude, so MCX
bands on the contract's *own* realised vol — its current 30m ATR% against its 20-session median.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum

#: India VIX on the NSE cash feed. Not a tradeable root, so it never enters the universe — it is
#: subscribed for its price alone.
INDIA_VIX_SCRIP = "999920019"
INDIA_VIX_NAME = "INDIA VIX"


class Band(StrEnum):
    COMPLACENT = "COMPLACENT"
    LOW = "LOW"
    NEUTRAL = "NEUTRAL"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


#: Thresholds transcribed from the old stack's ``VixRegimeService`` (vix.regime.threshold.*), so a
#: regime named here is the regime it named.
VIX_THRESHOLDS: tuple[tuple[float, Band], ...] = (
    (28.0, Band.EXTREME),
    (22.0, Band.HIGH),
    (18.0, Band.ELEVATED),
    (14.0, Band.NEUTRAL),
    (11.0, Band.LOW),
)

#: Cluster width in ATRs, per band. Convex on purpose: the implied-realised gap widens
#: super-linearly in stress, so HIGH -> EXTREME is a bigger step than COMPLACENT -> LOW.
CLUSTER_K: dict[Band, float] = {
    Band.COMPLACENT: 0.20,
    Band.LOW: 0.25,
    Band.NEUTRAL: 0.30,  # the old stack's calibrated value, in the regime it was calibrated in
    Band.ELEVATED: 0.40,
    Band.HIGH: 0.55,
    Band.EXTREME: 0.75,
}

#: Realised-vol banding for commodities: today's 30m ATR% against its own 20-session median.
#: 1.0 is a contract behaving normally for itself, which is what NEUTRAL means on the VIX side.
REALISED_THRESHOLDS: tuple[tuple[float, Band], ...] = (
    (1.80, Band.EXTREME),
    (1.40, Band.HIGH),
    (1.15, Band.ELEVATED),
    (0.85, Band.NEUTRAL),
    (0.70, Band.LOW),
)

#: Sizing multipliers, also from VixRegimeService. Carried because the old stack stamped them on
#: every signal as ``vixSizingMultiplier``; this module does not apply them, it reports them.
SIZING_MULTIPLIER: dict[Band, float] = {
    Band.EXTREME: 0.20,
    Band.HIGH: 0.50,
    Band.ELEVATED: 0.75,
    Band.NEUTRAL: 1.00,
    Band.LOW: 1.10,
    Band.COMPLACENT: 1.20,
}

ACTION: dict[Band, str] = {
    Band.EXTREME: "halt new entries; manage existing only",
    Band.HIGH: "size x 0.5; skip counter-trend flips",
    Band.ELEVATED: "size x 0.75; favour mean-reversion",
    Band.NEUTRAL: "standard sizing",
    Band.LOW: "size x 1.1; favour breakouts",
    Band.COMPLACENT: "size x 1.2; breakouts preferred, vol expansion likely",
}

#: Used when neither an India VIX print nor enough history exists. The old stack's flat value —
#: a known number rather than a guess dressed as a measurement.
FALLBACK_K = 0.30


@dataclass(frozen=True, slots=True)
class Regime:
    band: Band
    k: float
    source: str  # "india_vix" | "realised" | "fallback"
    value: float | None  # the VIX print, or the ATR%/median ratio
    detail: str

    def to_json(self) -> dict[str, object]:
        return {
            "band": self.band.value,
            "clusterK": self.k,
            "source": self.source,
            "value": None if self.value is None else round(self.value, 2),
            "sizingMultiplier": SIZING_MULTIPLIER[self.band],
            "action": ACTION[self.band],
            "detail": self.detail,
        }


def band_from_vix(vix: float) -> Band:
    for threshold, band in VIX_THRESHOLDS:
        if vix >= threshold:
            return band
    return Band.COMPLACENT


def band_from_realised(ratio: float) -> Band:
    for threshold, band in REALISED_THRESHOLDS:
        if ratio >= threshold:
            return band
    return Band.COMPLACENT


def atr_pct_ratio(recent: list[float], sessions: int = 20) -> float | None:
    """Latest ATR% against the median of the prior ``sessions``.

    Median, not mean: one event day would otherwise move the baseline more than the reading being
    judged against it.
    """
    if len(recent) < 6:
        return None
    latest, prior = recent[-1], recent[-1 - sessions : -1]
    if not prior:
        return None
    med = statistics.median(prior)
    return None if med <= 0 else latest / med


def regime_for_equity(vix: float | None) -> Regime:
    """NSE equity and its derivatives band on India VIX."""
    if vix is None or vix <= 0:
        return Regime(
            band=Band.NEUTRAL,
            k=FALLBACK_K,
            source="fallback",
            value=None,
            detail="no India VIX print — falling back to the old stack's flat 0.30",
        )
    band = band_from_vix(vix)
    return Regime(
        band=band,
        k=CLUSTER_K[band],
        source="india_vix",
        value=vix,
        detail=f"India VIX {vix:.2f} -> {band.value}, clustering at {CLUSTER_K[band]:.2f} x ATR",
    )


def regime_for_commodity(atr_pcts: list[float]) -> Regime:
    """MCX bands on the contract's own realised vol.

    India VIX is an equity-index implied vol and says nothing about crude, so borrowing it here
    would be worse than having no regime at all — it would be a confident wrong one.
    """
    ratio = atr_pct_ratio(atr_pcts)
    if ratio is None:
        return Regime(
            band=Band.NEUTRAL,
            k=FALLBACK_K,
            source="fallback",
            value=None,
            detail="not enough sessions to measure this contract's own volatility",
        )
    band = band_from_realised(ratio)
    return Regime(
        band=band,
        k=CLUSTER_K[band],
        source="realised",
        value=ratio,
        detail=(
            f"30m ATR is {ratio:.2f}x this contract's own 20-session median -> {band.value}, "
            f"clustering at {CLUSTER_K[band]:.2f} x ATR"
        ),
    )
