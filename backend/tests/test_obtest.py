"""The playbook backtester's arithmetic, and a day that runs end to end on a synthetic cache."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kotsin_nse.config import Settings
from kotsin_nse.domain import OrderSide
from kotsin_nse.market.session import ist_at
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.research.obtest import Params, Trade, _fut, run, run_day, summarize
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.scan.board import Board, Row
from kotsin_nse.scan.replay import DayReplay

from .test_scan import _store

DAY = date(2025, 6, 2)


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


def _x_store(tmp_path, fill_bar: tuple[float, float, float, float]) -> HistoryStore:
    """One stock, X: five flat minutes, ``fill_bar`` at 09:20, then a rally through 103."""
    store = HistoryStore(tmp_path)
    t0 = ist_at(DAY, "09:15")
    store.save("X", "1d", pd.DataFrame([{"ts": t0 - (60 - i) * 86400, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1e6} for i in range(60)]))
    path = [(100.0, 100.05, 99.95, 100.0)] * 5 + [fill_bar] + [(99.2 + 0.2 * k, 99.5 + 0.2 * k, 99.1 + 0.2 * k, 99.4 + 0.2 * k) for k in range(30)]
    store.save("X", "1m", pd.DataFrame([{"ts": t0 + 60 * i, "o": o, "h": h, "l": lo, "c": c, "v": 1000.0} for i, (o, h, lo, c) in enumerate(path)]))
    return store


def _ready_board(monkeypatch) -> None:
    """Every board shows X as a bullish pick with a READY setup: a limit at 99, S1 98, R1 101, R2 103."""
    setup = {"z": 99.0, "s1": 98.0, "r1": 101.0, "r2": 103.0, "rr": 2.0, "state": "READY"}

    def board_at(self, ts: int, *, setups: bool = True) -> Board:
        row = Row("X", "S", 100.0, None, 1.0, "BULLISH", rank=1, pick=True, align=4, setup=setup)
        return Board(ts, [row], ["X"], [], ["X"], {"bullish": 1, "bearish": 0})

    monkeypatch.setattr(DayReplay, "board_at", board_at)


def test_a_fill_bar_that_runs_on_through_the_hard_stop_is_stopped_on_that_bar(tmp_path, monkeypatch):
    _ready_board(monkeypatch)
    store = _x_store(tmp_path, (99.5, 99.7, 97.5, 99.2))  # trades to Z = 99 and on through the backstop 98.5
    (t,) = run_day(store, DAY, Params(hard_stop_r=0.5), CostModel(Settings(_env_file=None)))
    assert t.entry == 99.0 and t.entry_ts == ist_at(DAY, "09:21")
    assert [e["why"] for e in t.exits] == ["HARD_STOP"] and t.exits[0]["ts"] == t.entry_ts
    assert t.exits[0]["price"] == pytest.approx(98.5 * (1 - 5 / 1e4)) and t.r < -0.5


def test_a_trade_that_exits_in_two_pieces_pays_three_orders_not_two(tmp_path, monkeypatch):
    _ready_board(monkeypatch)
    store = _x_store(tmp_path, (99.5, 99.7, 98.9, 99.2))  # to Z, short of the backstop; then the rally
    settings = Settings(_env_file=None)
    costs = CostModel(settings)
    (t,) = run_day(store, DAY, Params(hard_stop_r=0.5), costs)
    assert [e["why"] for e in t.exits] == ["R1", "R2"]
    fut = _fut("X")
    legs = [costs.leg(fut, OrderSide.BUY, 99.0, t.qty), costs.leg(fut, OrderSide.SELL, 101.0, t.qty / 2), costs.leg(fut, OrderSide.SELL, 103.0, t.qty / 2)]
    assert t.costs == pytest.approx(sum(x.total for x in legs))
    assert sum(x.brokerage for x in legs) == 3 * settings.cost_brokerage_per_order_inr
    assert t.costs > costs.round_trip(fut, 99.0, 102.0, t.qty).total, "two legs at the average exit under-charge"


def test_the_holdout_is_refused():
    with pytest.raises(ValueError, match="holdout"):
        run(Settings(_env_file=None), [date(2025, 10, 1)], Params())
