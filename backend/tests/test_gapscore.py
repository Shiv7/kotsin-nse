"""The two reference-stack gap readings, ported. Advisory only — nothing routes on them.

Every expected value below is a real 2026-09-24 signal, recomputed by hand from the session's own
numbers, so the port is pinned to the day it was checked against rather than to itself.
"""

from __future__ import annotations

from kotsin_nse.strategy.gapscore import (
    F14_THRESHOLD,
    f14_score,
    gap_atr_tier,
    gap_open_class,
)


def test_the_classifier_labels_the_open_against_the_pivots_then_the_gap():
    # RELIANCE: open 1236.60, prev close 1248.00, daily S1 1240.33 -> below S1
    g = gap_open_class(open_px=1236.60, prev_close=1248.00, atr1d=20.10, r1=1300.0, s1=1240.33)
    assert g.label == "GAP_DOWN_S1" and round(g.gap_pct, 2) == -0.91
    assert round(g.gap_atr1d, 2) == 0.57 and not g.gap_fill_hidden

    # a gap inside the pivots but past 0.8x the daily ATR is the fill case
    g = gap_open_class(open_px=980.0, prev_close=1000.0, atr1d=20.0, r1=1050.0, s1=950.0)
    assert g.label == "GAP_FILL_LIKELY" and round(g.gap_atr1d, 2) == 1.0

    # and a quiet open is neither
    assert gap_open_class(open_px=999.0, prev_close=1000.0, atr1d=20.0, r1=1050.0, s1=950.0).label == "NO_GAP"
    assert gap_open_class(open_px=1060.0, prev_close=1000.0, atr1d=20.0, r1=1050.0, s1=950.0).label == "GAP_UP_R1"
    assert gap_open_class(open_px=0.0, prev_close=1000.0, atr1d=20.0, r1=None, s1=None).label == "UNKNOWN"


def test_a_violent_gap_below_s1_can_never_be_called_fill_likely():
    """The classifier tests direction before magnitude and returns early, so the four largest gaps
    of 2026-09-24 were all labelled GAP_DOWN_S1. `fill_hidden` is how you can tell."""
    # BANKNIFTY: open 55682.60 vs 56548.91 close, daily ATR 649.49 -> 1.33x, and below S1
    g = gap_open_class(open_px=55682.60, prev_close=56548.91, atr1d=649.49,
                       r1=56705.65, s1=56310.30)
    assert g.label == "GAP_DOWN_S1", "direction wins"
    assert round(g.gap_atr1d, 2) == 1.33 > 0.8
    assert g.gap_fill_hidden, "big enough to be fill-likely, and the label cannot say so"


def test_the_tiers_are_the_shipped_thresholds():
    assert [gap_atr_tier(x) for x in (0.5, 1.0, 1.3, 2.0, 3.0, 5.0)] == [0, 1, 2, 3, 4, 5]


def test_reliance_is_the_one_trigger_that_scores_a_flip():
    """Bar 1236.60 / 1241.60 / 1235.50 / 1238.70 on a SHORT: it closed above its open with a 0.48
    upper wick over a 1.62 ATR range — rejection, exhaustion, and the opening bar."""
    f = f14_score(bullish=False, grade="A", rr=3.85, fortress=6.0, atr30=3.76,
                  bar=(1236.60, 1241.60, 1235.50, 1238.70), gap_pct=-0.91, phase="OPEN")
    assert f.score == 55 >= F14_THRESHOLD and f.would_flip and f.verdict == "COUNTER"
    assert f.closed_opposite and round(f.wick_ratio, 2) == 0.48 and round(f.range_atr, 2) == 1.62
    assert f.candle_anchored
    assert [c.split("(")[0] for c in f.components] == ["StrongRejection", "RangeExhaustion", "TimePhase"]


def test_banknifty_gapped_hardest_and_still_reads_in_trend():
    """Its gap is tier 6, but GapFade needs the candle to close AGAINST the gap and this one
    gapped down and kept falling. So the reference stack agrees with our in-trend call."""
    f = f14_score(bullish=False, grade="C", rr=1.72, fortress=6.0, atr30=155.99,
                  bar=(55682.60, 55914.55, 55524.15, 55529.25), gap_pct=-1.53, phase="OPEN")
    assert f.tier == 6 and f.score == 25 and not f.would_flip and f.verdict == "IN_TREND"
    assert not f.closed_opposite, "it closed below its open — with the gap, not against it"
    assert "GapFade" not in " ".join(f.components)


def test_three_triggers_stop_five_points_short_and_the_tier_bonus_would_carry_them():
    """NAUKRI, SBILIFE and SHRIRAMFIN all score GapFade but not StrongRejection: 45 against 50.
    The ATR tier is what the reference stack ships disabled."""
    naukri = dict(bullish=False, grade="B", rr=2.33, fortress=6.0, atr30=12.54,
                  bar=(1212.40, 1250.00, 1208.70, 1249.60), gap_pct=-6.52, phase="OPEN")
    off = f14_score(**naukri)
    assert off.score == 45 and not off.would_flip and off.verdict == "IN_TREND"
    assert off.tier == 6 and off.tier_points == 25
    on = f14_score(**naukri, tiers_enabled=True)
    assert on.score == 70 and on.would_flip and on.verdict == "COUNTER"


def test_the_blockers_and_the_anchor_gate():
    # grade A with no fortress is refused before any component runs
    b = f14_score(bullish=False, grade="A", rr=3.0, fortress=2.0, atr30=10.0,
                  bar=(100, 101, 99, 100.5), gap_pct=0.0, phase="MID")
    assert b.blocked == "HIGH_QUALITY_NO_FORTRESS" and b.verdict == "SKIP" and b.score == 0
    # a crisis VIX blocks everything
    assert f14_score(bullish=False, grade="C", rr=0.5, fortress=10.0, atr30=10.0,
                     bar=(100, 101, 99, 100.5), gap_pct=0.0, phase="MID", vix=31.0).blocked == "CRISIS_VIX"
    # 50 points with no candle-anchored component does not flip
    weak = f14_score(bullish=False, grade="C", rr=0.5, fortress=10.0, atr30=1.0,
                     bar=(100, 102, 99, 99.5), gap_pct=0.0, phase="OPEN", vix=23.0)
    assert weak.score >= F14_THRESHOLD and not weak.would_flip
    assert "BlockedNoCandleAnchor" in weak.components and not weak.candle_anchored
