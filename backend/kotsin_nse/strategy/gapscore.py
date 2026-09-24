"""The reference stack's two gap readings, ported for the day book — advisory, never routed on.

Nothing here reaches a decision. ``strategy/counter.py`` is what actually routes a FUDKII trigger
in-trend or counter; these two are the streamingcandle originals, ported so a session can be read
against what the old stack would have said, and so the gap — which this engine's router does not
look at *at all* — is at least visible.

* ``gap_open_class`` — ``pivotboss/classifier/GapOpenClassifier.java``. Labels the 09:15 open
  against yesterday's close and today's daily pivots.
* ``f14_score`` — ``signal/flip/F14CounterTrendScorer.java``. Nine components, threshold 50, and a
  gate requiring at least one candle-anchored component before a flip counts.

Two departures from the original, both because the input does not exist here, and both stated on
the result rather than hidden: the ``OriginalSpec`` component needs ``flipGapRatio`` from
FortressFlipper, so it is omitted and a score is a floor; and the ATR-tier bonus is reported but
not added unless asked for, matching ``gap.atr.tier.enabled=false`` as that stack ships it.

**A defect worth knowing about, carried faithfully.** The classifier tests R1 and S1 before it
measures the gap against ATR, and returns early, so a gap below S1 can never be labelled
GAP_FILL_LIKELY however violent it is. On 2026-09-24 the four largest gaps of the session — NAUKRI
2.12x the daily ATR, BANKNIFTY 1.33x, AUBANK 1.24x, SBILIFE 1.09x — were all past the 0.8 fill
threshold and all labelled GAP_DOWN_S1 instead. ``gap_fill_hidden`` on the result says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: GapOpenClassifier.GAP_FILL_ATR_FRAC
GAP_FILL_ATR_FRAC = 0.8
#: gap.atr.tier.thresholds / gap.atr.tier.points
TIER_THRESHOLDS = (0.8, 1.2, 1.5, 2.5, 4.0)
TIER_POINTS = (0, 5, 10, 15, 20, 25)
#: f14.threshold / f14.vix.elevated.min / f14.require.candle.anchor
F14_THRESHOLD = 50
VIX_ELEVATED_MIN = 22.0
REQUIRE_CANDLE_ANCHOR = True
#: the scorer's own fallback when indiaVix is absent from the payload
VIX_DEFAULT = 15.0


@dataclass(frozen=True, slots=True)
class GapRead:
    label: str
    gap_pct: float
    gap_atr1d: float
    #: big enough for GAP_FILL_LIKELY, but labelled GAP_UP_R1/GAP_DOWN_S1 by the early return
    gap_fill_hidden: bool

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "gap_pct": round(self.gap_pct, 2),
                "gap_atr1d": round(self.gap_atr1d, 2), "fill_hidden": self.gap_fill_hidden}


def gap_open_class(
    *, open_px: float, prev_close: float, atr1d: float, r1: float | None, s1: float | None
) -> GapRead:
    """``GapOpenClassifier.classify``, plus the fill test the early return would have skipped."""
    gap_pct = (open_px - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
    gap_atr = abs(open_px - prev_close) / atr1d if atr1d > 0 and prev_close > 0 else 0.0
    fill = gap_atr > GAP_FILL_ATR_FRAC
    if open_px <= 0 or r1 is None or s1 is None:
        return GapRead("UNKNOWN", gap_pct, gap_atr, False)
    if r1 > 0 and open_px > r1:
        return GapRead("GAP_UP_R1", gap_pct, gap_atr, fill)
    if s1 > 0 and open_px < s1:
        return GapRead("GAP_DOWN_S1", gap_pct, gap_atr, fill)
    if fill:
        return GapRead("GAP_FILL_LIKELY", gap_pct, gap_atr, False)
    return GapRead("NO_GAP", gap_pct, gap_atr, False)


def gap_atr_tier(gap_atr: float) -> int:
    """0-based tier index; tier 6 (index 5) is a gap over 4x the 30m ATR."""
    for i, t in enumerate(TIER_THRESHOLDS):
        if gap_atr < t:
            return i
    return len(TIER_THRESHOLDS)


@dataclass(frozen=True, slots=True)
class F14Read:
    score: int
    would_flip: bool
    blocked: str
    tier: int
    tier_points: int
    gap_atr30: float
    range_atr: float
    wick_ratio: float
    closed_opposite: bool
    candle_anchored: bool
    components: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verdict(self) -> str:
        """What the reference stack would have done with this trigger."""
        if self.blocked:
            return "SKIP"
        return "COUNTER" if self.would_flip else "IN_TREND"

    def to_json(self) -> dict[str, Any]:
        return {"score": self.score, "verdict": self.verdict, "blocked": self.blocked,
                "tier": self.tier, "tier_points": self.tier_points,
                "gap_atr30": round(self.gap_atr30, 2), "range_atr": round(self.range_atr, 2),
                "wick": round(self.wick_ratio, 2), "closed_opposite": self.closed_opposite,
                "anchored": self.candle_anchored, "components": list(self.components)}


def f14_score(
    *,
    bullish: bool,
    grade: str,
    rr: float,
    fortress: float,
    atr30: float,
    bar: tuple[float, float, float, float],
    gap_pct: float,
    phase: str,
    vix: float | None = None,
    tiers_enabled: bool = False,
) -> F14Read:
    """``F14CounterTrendScorer.score``. ``bar`` is (open, high, low, close) of the trigger candle."""
    o, h, low, c = bar
    v = VIX_DEFAULT if vix is None else vix
    rng = h - low if h > low else 0.0
    range_atr = rng / atr30 if atr30 > 0 else 0.0
    if bullish:
        closed_opposite = c < o
        wick = max(min(o, c) - low, 0.0)
    else:
        closed_opposite = c > o
        wick = max(h - max(o, c), 0.0)
    wick_ratio = wick / rng if rng > 0 else 0.0
    gap_atr = abs(gap_pct * c / 100.0) / atr30 if atr30 > 0 else 0.0
    tier = gap_atr_tier(gap_atr)
    pts = TIER_POINTS[tier]

    blank = dict(tier=tier + 1, tier_points=pts, gap_atr30=gap_atr, range_atr=range_atr,
                 wick_ratio=wick_ratio, closed_opposite=closed_opposite, candle_anchored=False)
    if v >= 30:
        return F14Read(0, False, "CRISIS_VIX", **blank)
    if grade in ("A", "B") and fortress < 5:
        return F14Read(0, False, "HIGH_QUALITY_NO_FORTRESS", **blank)

    score, why, anchored = 0, [], False
    if wick_ratio >= 0.4 and closed_opposite and range_atr > 0.5:
        score += 30
        anchored = True
        why.append(f"StrongRejection(wick {wick_ratio:.2f}, range {range_atr:.2f} ATR) +30")
    if grade in ("C", "D", "F") and 0.3 <= rr < 1.0 and 5 <= fortress < 20:
        score += 25
        why.append(f"WeakSetup(grade {grade}, RR {rr:.2f}) +25")
    if fortress >= 20:
        score += 30
        anchored = True
        why.append(f"ExtremeFortress({fortress:.1f}) +30")
    if abs(gap_pct) > 1.0 and ((gap_pct < 0 and c > o) or (gap_pct > 0 and c < o)):
        score += 20
        anchored = True
        why.append(f"GapFade({gap_pct:+.2f}%, candle opposes) +20")
    if range_atr > 1.5:
        score += 15
        why.append(f"RangeExhaustion({range_atr:.2f} ATR) +15")
    if tiers_enabled and pts:
        score += pts
        why.append(f"GapAtrTier(T{tier + 1}, {gap_atr:.1f}x) +{pts}")
    if VIX_ELEVATED_MIN <= v < 25:
        score += 10
        why.append(f"VixRegime({v:.1f}, elevated) +10")
    elif 25 <= v < 30:
        score += 15
        why.append(f"VixRegime({v:.1f}, very high) +15")
    if phase in ("OPEN", "OPENING_RANGE", "OPENING_AUCTION"):
        score += 10
        why.append(f"TimePhase({phase}) +10")
    elif phase in ("EOD", "PRE_CLOSE", "CLOSE"):
        score += 8
        why.append(f"TimePhase({phase}) +8")

    flip = score >= F14_THRESHOLD
    if flip and REQUIRE_CANDLE_ANCHOR and not anchored:
        flip = False
        why.append("BlockedNoCandleAnchor")
    blank["candle_anchored"] = anchored
    return F14Read(score, flip, "", components=tuple(why), **blank)


__all__ = ["F14_THRESHOLD", "GAP_FILL_ATR_FRAC", "F14Read", "GapRead",
           "f14_score", "gap_atr_tier", "gap_open_class"]
