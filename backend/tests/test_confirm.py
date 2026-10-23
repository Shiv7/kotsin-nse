"""Confirmed entries: a double bottom enters on the first close above the neckline (never on the rally
that makes it, never after a shallow pullback); the CPR retest enters on the bar that comes back to TC
and closes above it; the holdout is refused."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from kotsin_nse.config import Settings
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.confirm import ConfirmParams, doubles, run, supertrend, trades_for
from kotsin_nse.risk.costs import CostModel


def _bars(closes: list[float], lows: list[float] | None = None, highs: list[float] | None = None) -> tuple[np.ndarray, ...]:
    c = np.asarray(closes, float)
    lo = np.asarray(lows, float) if lows else c - 0.2
    h = np.asarray(highs, float) if highs else c + 0.2
    return h, lo, c, np.full(len(c), 1.0)


W = [104, 103, 102, 101, 100, 101, 102, 103, 102.5, 101.5, 100.6, 100.3, 101, 102, 103.5, 104]


def test_a_double_bottom_enters_on_the_first_close_above_the_neckline_not_on_the_rally():
    h, lo, c, a = _bars(W)
    (s,) = doubles(h, lo, c, a, ConfirmParams(), np.ones(len(c), bool))
    assert (s["i1"], s["i2"], s["k"]) == (4, 11, 14), "L1 at 100, L2 at 100.3, the close of 103.5 above the 103.2 high"
    assert s["neck"] == pytest.approx(103.2)


def test_a_shallow_pullback_is_not_a_double_bottom():
    shallow = [104, 103, 102, 101, 100, 101, 102, 103, 102.5, 102.2, 102.4, 103.5, 104]
    h, lo, c, a = _bars(shallow)
    assert doubles(h, lo, c, a, ConfirmParams(), np.ones(len(c), bool)) == []


def test_a_broken_first_low_cancels_the_pattern():
    broken = [*W[:11], 99.0, 101, 102, 103.5, 104]
    h, lo, c, a = _bars(broken)
    assert doubles(h, lo, c, a, ConfirmParams(), np.ones(len(c), bool)) == []


def test_supertrend_follows_a_trend():
    up = np.linspace(100, 130, 200)
    assert supertrend(up + 0.5, up - 0.5, up)[-1] == 1
    assert supertrend(up[::-1] + 0.5, up[::-1] - 0.5, up[::-1])[-1] == -1


def _session(day: date, closes: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    t0 = ist_at(day, "09:15")
    c = np.asarray(closes, float)
    lo = np.asarray(lows, float) if lows else c - 0.2
    return pd.DataFrame({"ts": [t0 + 300 * i for i in range(len(c))], "o": c, "h": c + 0.2, "l": lo, "c": c, "v": 1000.0})


def test_the_cpr_retest_enters_on_the_bar_that_holds_tc_with_the_15m_trend_up():
    # yesterday a steady rise, H 110.2 L 99.8 C 110: P 106.67, BC 105, TC 108.33, R1 113.53
    d1 = _session(date(2024, 6, 3), list(np.linspace(100, 110, 75)))
    today = [109.0 + 0.05 * i for i in range(20)] + [108.6, 109.4] + [109.6 + 0.25 * i for i in range(30)]
    lows = [x - 0.2 for x in today]
    lows[20] = 108.30  # back to TC, and the bar closes above it
    d2 = _session(date(2024, 6, 4), today, lows)
    p = ConfirmParams(setup="cpr", first_entry="09:15")
    (t,) = [x for x in trades_for("X", pd.concat([d1, d2]), p, date(2024, 6, 4), date(2024, 6, 4), CostModel(Settings(_env_file=None))) if x.side == "LONG"]
    assert t.signal_ts == ist_at(date(2024, 6, 4), "09:15") + 300 * 20 + 300
    assert t.target == pytest.approx(113.533, abs=0.01) and t.exit_reason == "TARGET" and t.r > 0
    assert 0 < t.p_random < 1


def test_the_holdout_is_refused():
    with pytest.raises(ValueError, match="holdout"):
        run(Settings(_env_file=None), date(2025, 9, 1), date(2025, 10, 15), ConfirmParams())
