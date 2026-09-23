"""Offline RL for exits: the environment mirrors the backtester (gap, stop, last target,
force-flat, time stop, tighten-only actions), the ladder reproduces the hand rules, FQI learns
from transitions, and the whole experiment runs on synthetic history one symbol at a time."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction
from kotsin_nse.research.backtest import Backtester, BacktestParams
from kotsin_nse.research.rl.env import (
    ACTION_INDEX,
    ACTIONS,
    OBS_COLUMNS,
    ExitEnv,
    ExitEpisode,
    stop_after_action,
)
from kotsin_nse.research.rl.exit_policy import (
    Transitions,
    collect_episodes,
    experiment,
    fqi,
    run_episode,
)
from kotsin_nse.research.rl.policies import LadderPolicy, LearnedExitPolicy
from kotsin_nse.research.stats import sign_flip_test, walk_forward_folds
from kotsin_nse.strategy.base import Outcome, Signal
from kotsin_nse.strategy.keys import StrategyKey

from .conftest import bar, ist_ts
from .test_backtest_debug import _history

DAY0 = "2026-01-05"


def _bars(specs: list[tuple[float, float, float, float]], *, day: str = DAY0, start: str = "09:15") -> list[Any]:
    t0 = int(ist_ts(day, start))
    return [bar(t0 + i * 1800, o, h, lo, c, symbol="AAA", tf="30m") for i, (o, h, lo, c) in enumerate(specs)]


def _ep(entry_index: int = 1, side: int = 1, entry: float = 100.0, r_unit: float = 1.0, targets_r=(2.0, 4.0)) -> ExitEpisode:
    return ExitEpisode(
        symbol="AAA", segment=Segment.NSE_EQ, side=side, entry=entry, r_unit=r_unit, entry_index=entry_index,
        signal_ts=1, targets_r=targets_r, qty=10, multiplier=1, charges=lambda a, b: 8.0, slip_bps=0.0,
    )


# -- the environment ------------------------------------------------------------------------------


def test_env_mirrors_the_backtester_on_stops_targets_and_the_session():
    # bars: fill bar, quiet bar, gap through the stop at the open
    bars = _bars([(100, 100.5, 99.8, 100.2), (100.2, 100.6, 99.7, 100.1), (98.5, 99.0, 98.0, 98.4)])
    env = ExitEnv(bars, _ep(entry_index=0), max_bars=8, giveback_pct=40)
    assert not env.done
    res = env.step(ACTION_INDEX["hold"])  # to bar 1
    assert not res.done
    res = env.step(ACTION_INDEX["hold"])  # bar 2 opens below the stop
    assert res.done and res.info["reason"] == "stop_gap" and res.info["exit"] == 98.5
    assert env.net_r == pytest.approx((98.5 - 100) - 8.0 / 10, abs=1e-9)

    # one rung per bar, as the backtester takes them: a bar through T1 and T2 only registers T1;
    # the last rung closes the trade at its price
    bars = _bars([(100, 100.5, 99.8, 100.2), (100.2, 105.0, 100.0, 104.0), (104.0, 105.0, 103.5, 104.5)])
    env = ExitEnv(bars, _ep(entry_index=0, targets_r=(2.0, 4.0)), max_bars=8, giveback_pct=40)
    res = env.step(ACTION_INDEX["hold"])
    assert not res.done and env.targets_hit == 1
    res = env.step(ACTION_INDEX["hold"])
    assert res.done and res.info["reason"] == "target" and res.info["exit"] == pytest.approx(104.0)

    # the bar containing 15:20 IST flattens the position at its close
    bars = _bars([(100, 100.5, 99.8, 100.2), (100.2, 100.8, 100.0, 100.5), (100.5, 100.9, 100.1, 100.7)], start="14:15")
    env = ExitEnv(bars, _ep(entry_index=0), max_bars=8, giveback_pct=40)
    env.step(ACTION_INDEX["hold"])  # 14:45
    res = env.step(ACTION_INDEX["hold"])  # 15:15 contains 15:20
    assert res.done and res.info["reason"] == "force_flat"

    # the time stop counts the fill bar as bar 1 (the backtester's bars_held), and a stop on the
    # fill bar ends the episode before any action
    bars = _bars([(100, 100.5, 99.8, 100.2)] * 6)
    env = ExitEnv(bars, _ep(entry_index=0), max_bars=2, giveback_pct=40)
    assert env.bars_held == 1 and not env.done
    res = env.step(ACTION_INDEX["hold"])
    assert res.done and res.info["reason"] == "time_stop" and res.info["bars_held"] == 2
    env = ExitEnv(_bars([(100, 100.5, 98.5, 99.0)]), _ep(entry_index=0), max_bars=8, giveback_pct=40)
    assert env.done and env.exit_reason == "stop"


def test_actions_only_tighten_and_the_ladder_trail_is_proportional():
    kw = dict(side=1, entry=100.0, r_unit=1.0, stop=99.0, peak_r=3.0, giveback_pct=40.0)
    assert stop_after_action("lock_1r", **kw) == 101.0
    assert stop_after_action("lock_1r", **{**kw, "stop": 102.0}) == 102.0  # never loosens
    assert stop_after_action("trail_1r", **kw) == 102.0
    assert stop_after_action("trail_ladder", **kw) == pytest.approx(100 + 3.0 * 0.6)
    assert stop_after_action("hold", **kw) == 99.0
    short = dict(side=-1, entry=100.0, r_unit=1.0, stop=101.0, peak_r=2.0, giveback_pct=40.0)
    assert stop_after_action("lock_0r", **short) == 100.0
    assert stop_after_action("lock_0r", **{**short, "stop": 99.5}) == 99.5

    obs = np.zeros(len(OBS_COLUMNS))
    ladder = LadderPolicy(trail_arm_pct=3.0)
    assert ACTIONS[ladder.act(obs)] == "hold"
    obs[OBS_COLUMNS.index("t1_r")] = 2.0
    obs[OBS_COLUMNS.index("peak_r")] = 2.5
    assert ACTIONS[ladder.act(obs)] == "lock_0r"
    obs[OBS_COLUMNS.index("peak_gain_pct")] = 3.5
    assert ACTIONS[ladder.act(obs)] == "trail_ladder"


def test_fqi_prefers_the_action_the_data_rewards():
    rng = np.random.default_rng(0)
    n, d = 600, len(OBS_COLUMNS)
    obs = rng.normal(size=(n, d))
    act = rng.integers(0, len(ACTIONS), size=n)
    rew = np.where(act == ACTION_INDEX["exit_now"], 1.0, -1.0) + rng.normal(scale=0.05, size=n)
    tr = {"obs": obs, "action": act, "reward": rew, "next_obs": obs + 0.01, "done": np.ones(n, dtype=bool)}
    learned = fqi(tr, n_iter=5, min_support=10)
    picks = [ACTIONS[learned.act(o)] for o in obs[:50]]
    assert picks.count("exit_now") >= 45
    round_trip = LearnedExitPolicy.from_artefact(learned.to_artefact())
    assert ACTIONS[round_trip.act(obs[0])] == picks[0]
    with pytest.raises(ValueError):
        LearnedExitPolicy.from_artefact({**learned.to_artefact(), "actions": ["hold"]})


def test_folds_and_sign_flip():
    folds = walk_forward_folds(0, 600, n_folds=4, min_train_blocks=2)
    assert [f.train_start for f in folds] == [0, 0, 0, 0] and folds[0].test_end == folds[1].test_start
    assert folds[-1].test_end == 600 and all(f.train_end == f.test_start for f in folds)
    p = sign_flip_test([1.0] * 40, [f"d{i % 10}" for i in range(40)], n_perm=500)
    assert p["p_value"] < 0.05 and p["n_days"] == 10
    assert sign_flip_test([0.5], ["d"])["p_value"] is None


# -- end to end on synthetic history, one symbol -----------------------------------------------------


def _always_long(fudkii, fukaa, ctx, fukaa_ctx, b) -> Outcome:
    return Outcome(signals=[Signal(strategy=StrategyKey.FUDKII, symbol=b.symbol, direction=Direction.BULLISH, ts=b.ts,
                                   entry=b.close, stop=b.close * 0.99, targets=(b.close * 1.02, b.close * 1.04), grade="A", rr=2.0)])


def test_episodes_come_from_the_backtester_and_the_experiment_runs(settings, tmp_path, monkeypatch):
    store = _history(tmp_path / "history", "AAA", trading_days=120)
    monkeypatch.setattr(Backtester, "_decide", staticmethod(_always_long))
    bars, eps = collect_episodes(settings, BacktestParams(), store, "AAA")
    assert bars and len(eps) >= 30
    e = eps[0]
    # R is measured from the FILL (next open + slippage), not the signal close, so a target set at
    # 2 R from the close sits a little under 2 R from the fill
    assert e.side == 1 and e.r_unit > 0 and e.charges(100.0, 101.0) > 0
    assert len(e.targets_r) == 2 and 0 < e.targets_r[0] < e.targets_r[1]
    assert bars[e.entry_index].ts > e.signal_ts  # filled on the bar after the decision
    r = run_episode(bars, e, LadderPolicy(), max_bars=8, giveback_pct=40.0, log_to=(tr := Transitions()))
    assert r.policy == "ladder" and r.reason in ("stop", "stop_gap", "target", "force_flat", "time_stop", "data_end")
    assert len(tr) in (r.bars_held - 1, r.bars_held)  # one action per bar after the fill bar

    art = experiment(settings, ["AAA"], out_dir=tmp_path / "rl", n_folds=2, seed=1)
    assert art["kind"] == "exit_policy" and art["summary"]["episodes"] == len(eps)
    assert art["config"]["actions"] == list(ACTIONS) and (tmp_path / "rl" / f"{art['name']}.json").exists()
    evaluated = [f for f in art["folds"] if "skipped" not in f]
    assert evaluated, art["folds"]
    f0 = evaluated[0]
    assert set(f0) >= {"mean_net_r_policy", "mean_net_r_baseline", "paired_p_value", "cost_stress", "action_support", "exits_policy"}
    assert art["policy"] is not None and len(art["policy"]["weights"]) == len(ACTIONS)
