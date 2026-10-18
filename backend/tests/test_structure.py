"""The structure engine on a hand-built morning: a swing high, a pullback, a gapping break, the
retest, the targets — and the break that ends the zone."""

from __future__ import annotations

import pytest

from kotsin_nse.structure.engine import Bar5, analyze, resample_5m

T0 = 1_790_000_000

ROWS = [  # o, h, l, c
    (100.0, 101.0, 99.5, 100.8),
    (100.8, 102.0, 100.5, 101.8),
    (101.8, 103.0, 101.5, 102.5),  # swing high 103
    (102.5, 102.6, 101.0, 101.2),  # red
    (101.2, 101.5, 100.2, 100.4),  # red — the last before the move: the order block
    (100.4, 102.8, 100.4, 102.6),
    (102.6, 104.5, 102.9, 104.3),  # closes over 103 and leaves a gap over bar 4's high
    (104.3, 104.6, 103.5, 103.8),  # swing high 104.6
    (103.8, 103.9, 101.3, 101.6),  # back into the zone: first retest
    (101.6, 102.5, 101.4, 102.4),  # still in it — the same retest
    (102.4, 103.0, 102.0, 102.9),
]


def bars(rows=ROWS):
    return [Bar5(T0 + 300 * i, o, h, lo, c, 1000) for i, (o, h, lo, c) in enumerate(rows)]


def test_a_gapping_close_over_the_swing_high_makes_the_last_red_candle_a_demand_zone():
    s = analyze(bars(), adr=4.0)
    z = [z for z in s.zones if z.side == "BULLISH" and z.bos_kind == "SWING"][0]
    assert (z.ob_i, z.bos_i) == (4, 6)
    assert (z.low, z.high, z.z) == (100.2, 101.5, 101.2)
    assert z.s1 < z.low and z.impulse == "GAP"
    assert z.retests == 1 and z.state == "RETESTED_1"


def test_the_setup_targets_the_nearest_untaken_liquidity_ahead():
    s = analyze(bars(), adr=4.0)
    st = s.setup
    assert st is not None and st.side == "BULLISH"
    assert st.r1 == 103.0  # the opening range high
    assert st.r2 == 104.6  # the untaken swing high / day high
    assert st.rr == pytest.approx((104.6 - st.z) / (st.z - st.s1), rel=1e-3)


def test_a_close_through_the_far_side_breaks_the_zone():
    rows = [*ROWS, (102.9, 103.0, 99.8, 100.0)]
    z = [z for z in analyze(bars(rows), adr=4.0).zones if z.ob_i == 4][0]
    assert z.state == "BROKEN" and z.broken_ts == T0 + 300 * 11


def test_a_wick_through_the_zone_is_not_a_break():
    rows = [*ROWS, (102.9, 103.0, 99.9, 100.6)]
    z = [z for z in analyze(bars(rows), adr=4.0).zones if z.ob_i == 4][0]
    assert z.state != "BROKEN"


def test_no_break_without_a_close_beyond_the_swing():
    rows = [r if i != 6 else (102.6, 104.5, 102.9, 102.95) for i, r in enumerate(ROWS)]
    assert not [z for z in analyze(bars(rows[:7]), adr=4.0).zones if z.bos_kind == "SWING"]


def test_a_wick_under_a_swing_low_that_closes_back_is_a_bullish_sweep():
    rows = [
        (100.2, 100.8, 99.6, 100.0),
        (100.0, 100.5, 99.0, 99.5),
        (99.5, 100.0, 98.5, 99.8),  # swing low 98.5 (two higher lows each side)
        (99.8, 100.6, 99.2, 100.4),
        (100.4, 100.9, 99.9, 100.6),
        (100.6, 100.7, 98.0, 99.4),  # trades under 98.5, closes back above it
    ]
    s = analyze(bars(rows), adr=4.0)
    assert any(sw.side == "BULLISH" and sw.level == 98.5 for sw in s.sweeps)


def test_bars_are_only_ever_read_closed_left_to_right():
    """Truncating the day never changes what was known about its first half."""
    full = analyze(bars(), adr=4.0)
    part = analyze(bars()[:8], adr=4.0)
    assert [(z.ob_i, z.bos_i) for z in part.zones] == [(z.ob_i, z.bos_i) for z in full.zones if z.bos_i < 8]


def test_resample_rolls_minutes_onto_the_session_grid():
    rows = [(T0 + 60 * k, 100.0 + k, 101.0 + k, 99.0 + k, 100.5 + k, 10.0) for k in range(10)]
    b = resample_5m(rows, T0)
    assert len(b) == 2 and b[0].o == 100.0 and b[0].c == 104.5 and b[0].h == 105.0 and b[0].v == 50.0
