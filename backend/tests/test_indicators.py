"""One Supertrend for research and live: the vectorised ``indicators.supertrend`` reads bar for bar as
``strategy/cpr_retest.py::supertrend_dir``, the function the live CPR retest trades."""

from __future__ import annotations

import numpy as np
import pytest

from kotsin_nse.indicators import supertrend, wilder_atr
from kotsin_nse.strategy.cpr_retest import supertrend_dir


def _ohlc(seed: int, n: int = 150) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rs = np.random.RandomState(seed)
    c = 100 + np.cumsum(rs.normal(0, 1.0, n))
    prev = np.r_[c[0], c[:-1]]
    h = np.maximum(c, prev) + rs.uniform(0.05, 1.5, n)
    lo = np.minimum(c, prev) - rs.uniform(0.05, 1.5, n)
    return h, lo, c


def test_every_prefix_reads_as_the_live_supertrend_dir():
    flips = 0
    for seed in range(6):
        h, lo, c = _ohlc(seed)
        d, _fu, _fl = supertrend(h, lo, c)
        assert d.shape == c.shape
        for k in range(1, len(c) + 1):
            live = supertrend_dir(list(h[:k]), list(lo[:k]), list(c[:k]))
            assert d[k - 1] == live, f"seed {seed}, {k} bars: research {d[k - 1]}, live {live}"
        flips += int((np.diff(d[11:]) != 0).sum())
    assert flips > 10, "the random walks never turned: the parity proved nothing"


def test_the_flip_is_judged_on_the_current_bars_band_not_the_previous_bars():
    # a decline, then one bar that crashes and closes back near where it opened: its own upper band
    # (its huge range widens the ATR, its midpoint sits far below) is under the close, the previous
    # bar's band is not — the standard rule turns up here, a previous-band rule would not
    c = np.r_[np.linspace(130, 100, 30), 101.0]
    h, lo = c + 0.5, c - 0.5
    lo[-1], h[-1] = 60.0, 101.0
    d, fu, _fl = supertrend(h, lo, c)
    assert fu[-1] < c[-1] <= fu[-2]
    assert d[-2] == -1 and d[-1] == 1 == supertrend_dir(list(h), list(lo), list(c))


def test_too_few_bars_read_0_as_live_does():
    h, lo, c = _ohlc(3, 40)
    d = supertrend(h, lo, c, n=10)[0]
    assert (d[:11] == 0).all() and (np.abs(d[11:]) == 1).all()
    assert supertrend_dir(list(h[:11]), list(lo[:11]), list(c[:11])) == 0


def test_wilder_atr_is_seeded_with_the_first_bars_range_and_smoothed_by_1_over_n():
    h, lo, c = _ohlc(7, 40)
    a = wilder_atr(h, lo, c, 10)
    atr = h[0] - lo[0]
    assert a[0] == pytest.approx(atr)
    for i in range(1, len(c)):
        atr += (max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) - atr) / 10
        assert a[i] == pytest.approx(atr)
