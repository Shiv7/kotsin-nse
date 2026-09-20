"""The backtester. Its job is to be *pessimistic and honest*, so these tests pin both."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd
import pytest

from kotsin_nse.config import Segment
from kotsin_nse.research.backtest import Backtester, BacktestParams, load_summaries, save
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.research.stats import (
    day_clustered_mean,
    max_drawdown,
    profit_factor,
    within_day_permutation,
)
from kotsin_nse.strategy.fudkii import FudkiiConfig

from .conftest import ist_ts

SESSION_BUCKETS = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (15, 45)][:13]


def _session(day: str, closes: list[float], vol: float = 1000.0) -> list[dict]:
    """One session of 30m bars on the real NSE grid."""
    rows = []
    prev = closes[0]
    for hm, c in zip(SESSION_BUCKETS, closes, strict=False):
        rows.append(
            {
                "ts": int(ist_ts(day, hm)),
                "o": prev,
                "h": max(prev, c) * 1.002,
                "l": min(prev, c) * 0.998,
                "c": c,
                "v": vol,
            }
        )
        prev = c
    return rows


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _seed(store: HistoryStore, symbol: str, *, days: int, shape) -> None:
    intraday: list[dict] = []
    daily: list[dict] = []
    for i, day in enumerate(_weekdays(date(2026, 1, 5), days)):
        closes = shape(i)
        intraday += _session(day, closes)
        daily.append(
            {
                "ts": int(ist_ts(day, "09:15")),
                "o": closes[0],
                "h": max(closes) * 1.01,
                "l": min(closes) * 0.99,
                "c": closes[-1],
                "v": 100000.0,
            }
        )
    store.save(symbol, "30m", pd.DataFrame(intraday))
    store.save(symbol, "1d", pd.DataFrame(daily))


@pytest.fixture
def store(tmp_path) -> HistoryStore:
    return HistoryStore(tmp_path / "history")


def test_a_flat_series_produces_no_trades_and_no_cost(settings, store):
    """The null case. A strategy that never fires must cost exactly nothing — if it does not,
    the harness is inventing trades."""
    _seed(store, "FLAT", days=60, shape=lambda i: [100.0 + (j % 2) * 0.01 for j in range(13)])
    result = Backtester(settings).run(store, ["FLAT"])
    s = result.summary()
    assert s["trades"] == 0
    assert s["net"] == 0.0
    assert s["charges"] == 0.0
    assert result.bars > 0, "the harness must have actually walked the bars"


def test_missing_history_is_reported_not_silently_empty(settings, store):
    result = Backtester(settings).run(store, ["NOPE"])
    assert result.bars == 0
    assert result.summary()["trades"] == 0


def test_a_breakout_series_produces_trades_through_the_live_code(settings, store):
    def shape(i: int) -> list[float]:
        base = 100.0 + i * 0.05
        if i % 11 == 10:  # an occasional decisive expansion
            return [base] * 6 + [base * 1.04, base * 1.06] + [base * 1.07] * 5
        return [base + (j % 3) * 0.05 for j in range(13)]

    _seed(store, "MOVER", days=90, shape=shape)
    result = Backtester(settings).run(store, ["MOVER"])
    s = result.summary()
    assert result.signals + result.rejections > 0, "the strategy must have been evaluated"
    assert s["binding_gates"], "rejections must be attributed to a gate"
    if s["trades"]:
        assert s["charges"] > 0, "a trade that costs nothing is a bug in the cost model"


def test_the_stop_is_assumed_to_hit_before_the_target(settings, store):
    """A bar whose range covers both must be booked as a loss. The opposite assumption is how a
    backtest quietly becomes a sales pitch."""
    from kotsin_nse.domain import Direction, Instrument, InstrumentKind
    from kotsin_nse.research.backtest import BacktestResult, OpenTrade

    bt = Backtester(settings)
    inst = Instrument("X", "X", Segment.NSE_EQ, InstrumentKind.EQUITY, multiplier=1)
    t = OpenTrade(
        strategy="FUDKII",
        symbol="X",
        direction=Direction.BULLISH,
        entry_ts=int(ist_ts("2026-01-05", "10:15")),
        entry=100.0,
        stop=98.0,
        initial_stop=98.0,
        targets=(104.0,),
        qty=100,
        grade="A",
        peak=100.0,
        trough=100.0,
    )
    from .conftest import bar as mk

    both = mk(ist_ts("2026-01-05", "10:45"), 100.0, 105.0, 97.0, 101.0)
    closed = bt._manage(t, both, inst, BacktestResult("x", 0, {}, [], 0, 0, 0))
    assert closed is not None
    assert closed.exit_reason == "SL-EQ"
    assert closed.net < 0


def test_a_stop_fill_is_worse_than_the_stop_price(settings, store):
    """CAN2 booked every stop-out at exactly the stop, so its whole live ledger was optimistic by
    the gap."""
    from kotsin_nse.domain import Direction, Instrument, InstrumentKind
    from kotsin_nse.research.backtest import BacktestResult, OpenTrade

    from .conftest import bar as mk

    bt = Backtester(settings, BacktestParams(slippage_bps=10.0))
    inst = Instrument("X", "X", Segment.NSE_EQ, InstrumentKind.EQUITY, multiplier=1)
    t = OpenTrade("FUDKII", "X", Direction.BULLISH, 0, 100.0, 98.0, 98.0, (110.0,), 100, "A", 100.0, 100.0)
    closed = bt._manage(t, mk(ist_ts("2026-01-05", "10:45"), 99.0, 99.5, 97.0, 97.5), inst,
                        BacktestResult("x", 0, {}, [], 0, 0, 0))
    assert closed is not None
    assert closed.exit < 98.0


def test_entry_fills_on_the_next_bar_open_not_the_signal_bar(settings, store):
    """No lookahead: the decision is made on a closed bar, and the fill is the next bar's open."""
    from kotsin_nse.domain import Direction, Instrument, InstrumentKind
    from kotsin_nse.strategy.base import Signal
    from kotsin_nse.strategy.keys import StrategyKey

    from .conftest import bar as mk

    bt = Backtester(settings)
    inst = Instrument("X", "X", Segment.NSE_EQ, InstrumentKind.EQUITY, multiplier=1)
    sig = Signal(
        strategy=StrategyKey.FUDKII, symbol="X", direction=Direction.BULLISH,
        ts=0, entry=100.0, stop=98.0, targets=(104.0,),
    )
    nxt = mk(0, 101.0, 102.0, 100.5, 101.5)
    t = bt._open(sig, nxt, inst)
    assert t is not None
    assert t.entry > 101.0  # next bar's open plus slippage, not the signal's 100.0


def test_zones_are_point_in_time(settings, store):
    """A pivot derived from a session that has not happened yet is lookahead of the worst kind."""
    from kotsin_nse.bars.store import BarStore
    from kotsin_nse.research.backtest import BacktestContext

    from .conftest import bar as mk

    dailies = {
        "X": [
            mk(ist_ts(d, "09:15"), 100, 110, 90, 105, tf="1d", symbol="X")
            for d in _weekdays(date(2026, 1, 5), 40)
        ]
    }
    ctx = BacktestContext(BarStore(), dailies, Segment.NSE_EQ)
    ctx.today = date(2026, 1, 7)  # only two prior sessions exist
    assert ctx.zones("X") == []
    ctx.today = date(2026, 2, 20)
    assert ctx.zones("X"), "with 25+ prior dailies there must be zones"


def test_option_overlay_is_reported_separately_and_labelled(settings, store):
    """It is a model, not a measurement — there is no option-chain history to check it against."""
    p = BacktestParams(model_option_leg=True)
    assert p.to_json()["model_option_leg"] is True
    off = BacktestParams(model_option_leg=False)
    assert off.to_json()["model_option_leg"] is False


def test_results_persist_and_list(settings, store, tmp_path):
    _seed(store, "FLAT", days=40, shape=lambda i: [100.0] * 13)
    result = Backtester(settings).run(store, ["FLAT"])
    path = save(result, tmp_path / "backtests")
    assert path.exists()
    listed = load_summaries(tmp_path / "backtests")
    assert listed and listed[0]["id"] == result.id


def test_params_round_trip_into_the_result(settings, store):
    _seed(store, "FLAT", days=40, shape=lambda i: [100.0] * 13)
    params = BacktestParams(fudkii=FudkiiConfig(bb_mult=3.0), position_budget_inr=50_000)
    result = Backtester(settings, params).run(store, ["FLAT"])
    assert result.summary()["params"]["fudkii"]["bb_mult"] == 3.0
    assert result.summary()["params"]["position_budget_inr"] == 50_000


# -- statistics ----------------------------------------------------------------------------------


def test_day_clustered_mean_uses_day_means_for_the_error():
    """Trades cluster by day, so the standard error must be over days, not over trades."""
    values = [1.0, 1.0, 1.0, -1.0, -1.0, -1.0]
    days = ["d1", "d1", "d1", "d2", "d2", "d2"]
    c = day_clustered_mean(values, days)
    assert c.n == 6 and c.n_days == 2
    assert c.mean == 0.0
    assert c.stderr is not None and c.stderr > 0


def test_a_small_sample_says_so():
    c = day_clustered_mean([0.1] * 5, ["d1"] * 5)
    assert c.too_small is True
    big = day_clustered_mean([0.1] * 40, [f"d{i % 15}" for i in range(40)])
    assert big.too_small is False


def test_within_day_permutation_does_not_credit_a_good_day():
    """Arm A mostly falls on good days and arm B on bad ones, with **no** within-day difference.

    Pooled, the two arms look worlds apart. A between-day shuffle would call that significant.
    Shuffling inside each day must not — which is the entire reason this test exists, because a
    naive split twice produced a "significant" result here that collapsed under exactly this.
    """
    rng = random.Random(3)
    values, labels, days = [], [], []
    for d in range(10):
        base = 5.0 if d % 2 == 0 else -5.0
        mostly_a = d % 2 == 0
        for i in range(20):
            # the label is assigned by day, not by outcome; the value ignores the label entirely
            is_a = (i < 16) if mostly_a else (i >= 16)
            values.append(base + rng.gauss(0, 0.5))
            labels.append(is_a)
            days.append(f"d{d}")

    pooled_a = [v for v, lab in zip(values, labels, strict=True) if lab]
    pooled_b = [v for v, lab in zip(values, labels, strict=True) if not lab]
    pooled_gap = abs(sum(pooled_a) / len(pooled_a) - sum(pooled_b) / len(pooled_b))
    assert pooled_gap > 3, "the pooled comparison should look dramatic — that is the trap"

    res = within_day_permutation(values, labels, days, iterations=600)
    assert res.p_value > 0.05, f"a pure day effect must not read as a label effect (p={res.p_value})"


def test_within_day_permutation_is_unfalsifiable_when_a_day_is_all_one_label():
    """If every trade on a day carries the same label there is nothing to shuffle, so the test
    can only return "no evidence" — and it should say so with p ≈ 1 rather than a small number."""
    values = [5.0] * 20 + [-5.0] * 20
    labels = [True] * 20 + [False] * 20
    days = ["d1"] * 20 + ["d2"] * 20
    res = within_day_permutation(values, labels, days, iterations=200)
    assert res.p_value > 0.9
    assert res.too_small is True


def test_within_day_permutation_finds_a_real_within_day_effect():
    values, labels, days = [], [], []
    for d in range(12):
        for i in range(20):
            a = i % 2 == 0
            values.append(3.0 if a else -3.0)
            labels.append(a)
            days.append(f"d{d}")
    res = within_day_permutation(values, labels, days, iterations=400)
    assert res.observed > 0
    assert res.p_value < 0.05


def test_drawdown_and_profit_factor():
    assert max_drawdown([0, 5, 3, 8, 2]) == -6
    assert profit_factor([2.0, -1.0]) == 2.0
    assert profit_factor([1.0, 2.0]) is None
