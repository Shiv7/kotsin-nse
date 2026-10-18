"""The playbook backtester's arithmetic, and a day that runs end to end on a synthetic cache."""

from __future__ import annotations

from datetime import date

import pytest

from kotsin_nse.config import Settings
from kotsin_nse.research.obtest import Params, Trade, run_day, summarize
from kotsin_nse.risk.costs import CostModel

from .test_scan import _store


def _t(day: str, r: float, why: str = "R2", side: str = "BULLISH") -> Trade:
    t = Trade(day, "X", side, 0, 0, 100.0, 100.0, 99.0, 101.0, 102.0, 2.0, 3, 1, 100)
    t.r, t.exit_reason, t.net = r, why, r * 100
    return t


def test_summary_counts_by_day_including_the_days_without_a_trade():
    s = summarize([_t("d1", 2.0), _t("d1", -1.0, "S1_CLOSE"), _t("d2", -1.0, "S1_CLOSE")], ["d1", "d2", "d3"])
    assert s["trades"] == 3 and s["days"] == 3 and s["days_traded"] == 2
    assert s["total_r"] == 0.0 and s["avg_r"] == 0.0 and s["win_rate"] == pytest.approx(33.3)
    assert s["by_day"] == {"d1": 1.0, "d2": -1.0, "d3": 0.0}
    assert s["by_exit"]["S1_CLOSE"] == {"n": 2, "r": -2.0}
    assert s["days_positive"] == 1 and s["days_negative"] == 1


def test_a_quiet_synthetic_day_runs_end_to_end_without_a_trade(tmp_path):
    day = date(2026, 10, 16)
    store = _store(tmp_path, day)
    assert run_day(store, day, Params(), CostModel(Settings(_env_file=None))) == []
