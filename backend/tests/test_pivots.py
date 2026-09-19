"""Pivots and the confluence ladder.

The golden case is the one the old repo verified against authoritative NSE bhavcopy: DABUR monthly
matched P=458.0, R1=475.9, R2=508.4, S1=425.5, S2=407.6 exactly. Those five numbers pin the
formula; the R3 test pins the *convention*, which is the part that actually differed between the
four implementations the old codebase used to carry.
"""

from __future__ import annotations

import math

from kotsin_nse.bars.pivots import (
    TF_WEIGHT,
    GradePolicy,
    Zone,
    classic_pivots,
    cluster_zones,
    compute_confluence,
    pivot_points,
    round_figure_snap,
)

# Derived from the published DABUR levels: L = 2P - R1, H = 2P - S1, C = 3P - H - L.
DABUR_H, DABUR_L, DABUR_C = 490.5, 440.1, 443.4


def test_classic_pivots_match_the_verified_dabur_levels():
    p = classic_pivots(DABUR_H, DABUR_L, DABUR_C)
    assert p is not None
    assert math.isclose(p.pivot, 458.0, abs_tol=0.01)
    assert math.isclose(p.r1, 475.9, abs_tol=0.01)
    assert math.isclose(p.r2, 508.4, abs_tol=0.01)
    assert math.isclose(p.s1, 425.5, abs_tol=0.01)
    assert math.isclose(p.s2, 407.6, abs_tol=0.01)


def test_r3_uses_the_kite_convention_not_the_floor_trader_one():
    """R3 = P + 2(H-L), not H + 2(P-L). The two differ by |P-H| and the gap scales with range.
    A pivot has no mechanism except coordination: the number other participants are watching is
    the correct one."""
    p = classic_pivots(110, 90, 100)
    assert p is not None
    assert p.r3 == 100 + 2 * 20  # Kite: 140
    assert p.r3 != 110 + 2 * (100 - 90)  # floor-trader: 130
    assert p.s3 == 100 - 2 * 20


def test_cpr_is_sorted_so_tc_is_always_the_upper_edge():
    """On a strongly directional candle the raw TC lands below BC. The trade logic cares about the
    band edges, not which formula produced which name."""
    p = classic_pivots(110, 90, 91)
    assert p is not None
    assert p.tc >= p.bc
    assert p.cpr_width >= 0


def test_bad_input_returns_none_rather_than_a_pivot_at_zero():
    assert classic_pivots(0, 0, 0) is None
    assert classic_pivots(110, 90, 0) is None


def test_camarilla_levels_are_populated_but_carry_no_weight():
    """Disabled 2026-04-13. Keeping them at zero weight makes that a reversible decision rather
    than a silent omission."""
    p = classic_pivots(110, 90, 100)
    assert p is not None and p.cam_r1 > 0
    labels = {pt.label for pt in pivot_points(p, "1d")}
    assert not any("CAM" in lab for lab in labels)


def test_timeframe_weights_order_daily_over_weekly_over_monthly():
    assert TF_WEIGHT["1d"] > TF_WEIGHT["1wk"] > TF_WEIGHT["1mo"]
    p = classic_pivots(110, 90, 100)
    assert p is not None
    daily = [pt for pt in pivot_points(p, "1d") if pt.label == "1d.R1"][0]
    weekly = [pt for pt in pivot_points(p, "1wk") if pt.label == "1wk.R1"][0]
    assert daily.weight > weekly.weight


def test_cluster_merges_nearby_levels_and_sums_their_weight():
    p1 = classic_pivots(110, 90, 100)
    p2 = classic_pivots(110.1, 90.1, 100.05)
    assert p1 and p2
    zones = cluster_zones(pivot_points(p1, "1d") + pivot_points(p2, "1wk"), tolerance_pct=0.5)
    assert zones
    # A merged zone carries members from both timeframes and is therefore a wall.
    merged = [z for z in zones if len({m.split(".")[0] for m in z.members}) > 1]
    assert merged
    assert any(z.is_wall for z in merged)


def test_a_lone_daily_level_is_not_a_wall():
    """wall.min = 5.2 means one daily level (4.0) is not a wall; a daily plus a weekly is."""
    assert Zone(price=100, strength=TF_WEIGHT["1d"], members=["1d.R1"]).is_wall is False
    assert (
        Zone(
            price=100,
            strength=TF_WEIGHT["1d"] + TF_WEIGHT["1wk"],
            members=["1d.R1", "1wk.R1"],
        ).is_wall
        is True
    )


def test_round_figure_snap_only_ever_moves_away_from_entry():
    """The snap can make a target harder to reach, never easier — so it cannot flatter a backtest."""
    assert round_figure_snap(1247, up=True) >= 1247
    assert round_figure_snap(1247, up=False) <= 1247
    assert round_figure_snap(2510, up=True) == 2600
    assert round_figure_snap(2510, up=False) == 2500


def test_snap_is_capped_so_rounding_cannot_change_the_reward_risk():
    """A target 0.5 away must not be snapped 9.5 further out just because 110 is a round number:
    that would turn a 1:1 trade into a fictional 10:1 one."""
    assert round_figure_snap(100.5, up=True, anchor=100.0) == 100.5
    # A proportionate nudge is still applied.
    assert round_figure_snap(1247, up=True, anchor=1000.0) == 1250


def _walls(prices: list[float]) -> list[Zone]:
    return [Zone(price=p, strength=8.0, members=["1d.R1", "1wk.R1"]) for p in prices]


def test_confluence_stop_is_the_nearest_zone_behind():
    zones = _walls([95.0, 90.0, 110.0, 120.0])
    c = compute_confluence(close=100.0, bullish=True, zones=zones, atr_value=2.0)
    assert c.stop == 95.0
    assert c.targets[0] >= 110.0  # snapped outward
    assert c.grade in ("A", "B", "C")


def test_confluence_falls_back_to_one_atr_and_says_so_when_nothing_is_behind():
    """The old engine produced a stop of zero here, which the executor read as 'no stop'."""
    c = compute_confluence(close=100.0, bullish=True, zones=_walls([110.0, 120.0]), atr_value=2.0)
    assert c.stop == 98.0
    assert "fell back" in c.note


def test_no_wall_ahead_is_grade_f_not_a_target_of_zero():
    c = compute_confluence(close=100.0, bullish=True, zones=_walls([95.0]), atr_value=2.0)
    assert c.grade == "F"
    assert c.blocked is True
    assert c.targets == ()


def test_grade_f_below_the_rr_hard_floor():
    """Risk 5, reward 0.5 → rr 0.1, well under the 1.0 publish floor."""
    zones = _walls([95.0, 100.5])
    c = compute_confluence(close=100.0, bullish=True, zones=zones, atr_value=2.0)
    assert c.rr < GradePolicy().rr_hard_floor
    assert c.grade == "F"
    assert c.blocked is True


def test_room_ratio_caps_an_otherwise_good_grade_at_c():
    """Plenty of reward but the next wall is right behind the first: no room to run."""
    zones = _walls([90.0, 130.0, 130.5])
    c = compute_confluence(
        close=100.0,
        bullish=True,
        zones=zones,
        atr_value=100.0,  # a huge ATR makes room_ratio tiny
    )
    assert c.grade == "C"
    assert "room" in c.note
