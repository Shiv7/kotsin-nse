"""Indicator maths. These pin the *deviations* from the textbook, because those are what the live
thresholds were fitted against."""

from __future__ import annotations

import math
from itertools import pairwise

from kotsin_nse.bars.indicators import (
    atr,
    atr_series,
    bars_in_trend,
    body_outside_ratio,
    bollinger,
    close_position_in_range,
    pstdev,
    supertrend,
    volume_baseline,
    volume_surge_median,
    volume_surges,
    vwap,
)

from .conftest import bar, series


def test_bollinger_uses_population_sigma_not_sample():
    """``BBSuperTrendCalculator`` divided by n. Matching it matters more than being fastidious:
    the live 2.0-sigma thresholds were fitted against this divisor."""
    closes = [float(i) for i in range(1, 21)]
    sd_pop = pstdev(closes, 20)
    mean = sum(closes) / 20
    expected = math.sqrt(sum((c - mean) ** 2 for c in closes) / 20)
    assert sd_pop == expected
    bb = bollinger(closes, 20, 2.0)
    assert bb is not None
    assert bb.middle == mean
    assert bb.upper == mean + 2 * expected
    assert bb.width == (bb.upper - bb.lower) / bb.middle


def test_bollinger_needs_a_full_window():
    assert bollinger([1.0, 2.0, 3.0], 20, 2.0) is None


def test_atr_is_wilder_rma_seeded_with_a_simple_mean():
    bars = series([100, 102, 101, 103, 105, 104, 106, 108])
    period = 3
    got = atr_series(bars, period)
    # Manual Wilder over the same true ranges.
    trs = []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        trs.append(
            max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        )
    want = sum(trs[:period]) / period
    assert got[period] == want
    for i in range(period, len(trs)):
        want = (want * (period - 1) + trs[i]) / period
        assert math.isclose(got[i + 1], want, rel_tol=1e-12)
    assert atr(bars, period) == got[-1]


def test_atr_is_none_before_warm():
    assert atr(series([100, 101]), 14) is None


def test_supertrend_flips_and_bands_only_ratchet():
    up = series([100 + i for i in range(30)])
    pts = supertrend(up, 7, 3.0)
    assert pts[-1] is not None and pts[-1].trend == 1
    # In an uptrend the lower band never falls.
    lows = [p.lower for p in pts if p is not None]
    assert all(b >= a - 1e-9 for a, b in pairwise(lows))

    down = series([100 + i for i in range(30)] + [130 - 4 * i for i in range(1, 20)])
    pts2 = supertrend(down, 7, 3.0)
    assert pts2[-1] is not None and pts2[-1].trend == -1
    trends = [p.trend for p in pts2 if p is not None]
    assert 1 in trends and -1 in trends  # it actually flipped


def test_bars_in_trend_counts_the_unchanged_run():
    up = series([100 + i for i in range(40)])
    pts = supertrend(up, 7, 3.0)
    n = bars_in_trend(pts)
    assert n >= 2
    assert all(p is None or p.trend == pts[-1].trend for p in pts[-n:])


def test_fukaa_baseline_excludes_both_T_and_T_minus_1():
    """Baseline is T-2..T-(N+1). Neither the bar under test nor the one before it may inflate the
    average they are judged against — off by one here silently retunes the gate."""
    vols = [100.0] * 6 + [500.0, 1000.0]  # six baseline bars, then T-1 = 500, T = 1000
    surge_t, surge_t1, base = volume_surges(vols, window=6, floor=0.0)
    assert base == 100.0
    assert surge_t == 10.0
    assert surge_t1 == 5.0


def test_both_surges_share_one_baseline():
    """"The higher of the two" only means something if both are over the same denominator."""
    vols = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 900.0, 100.0]
    surge_t, surge_t1, base = volume_surges(vols, window=6, floor=0.0)
    assert base == sum(vols[:6]) / 6
    assert surge_t == 100.0 / base
    assert surge_t1 == 900.0 / base


def test_volume_baseline_has_a_floor():
    """A dead scrip averaging 3 shares must not manufacture a 400x surge."""
    vols = [3.0] * 6 + [0.0, 1200.0]
    surge_t, _, base = volume_surges(vols, window=6, floor=1000.0)
    assert base == 1000.0
    assert surge_t == 1.2


def test_surge_is_none_not_zero_when_history_is_short():
    """A gate that cannot see its input must say so. Returning 0 would read as 'fails the bar'
    and returning 1 as 'passes'; both are guesses."""
    assert volume_surges([1.0, 2.0], window=6) == (None, None, None)
    assert volume_baseline([1.0, 2.0], window=6, skip_last=2) is None
    assert volume_surge_median([1.0, 2.0], 20) is None


def test_volume_surge_median_form_is_can2s():
    vols = [10.0] * 20 + [50.0]
    assert volume_surge_median(vols, 20) == 5.0


def test_vwap_is_over_exactly_the_bars_given():
    bars = [bar(0, 10, 12, 8, 10, 100), bar(1800, 10, 14, 10, 12, 300)]
    want = sum(b.typical * b.volume for b in bars) / 400
    assert vwap(bars) == want
    assert vwap([bar(0, 10, 10, 10, 10, 0)]) is None


def test_body_outside_ratio_ignores_the_wick():
    """The BB-squeeze family's 0.50 bar. A doji spearing the band scores ~0."""
    doji = bar(0, 100.0, 120.0, 99.0, 100.2)
    assert body_outside_ratio(doji, 110.0, above=True) == 0.0
    strong = bar(0, 100.0, 121.0, 99.0, 120.0)
    assert body_outside_ratio(strong, 110.0, above=True) == 0.5
    assert body_outside_ratio(strong, 100.0, above=True) == 1.0


def test_close_position_in_range():
    assert close_position_in_range(bar(0, 10, 20, 10, 20)) == 1.0
    assert close_position_in_range(bar(0, 10, 20, 10, 10)) == 0.0
    assert close_position_in_range(bar(0, 10, 10, 10, 10)) is None
