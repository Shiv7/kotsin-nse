"""Offline RL for exits, evaluated walk-forward against the backtester's own rules.

1. Episodes come from REAL trades: the backtester runs one symbol at a time exactly as
   ``kotsin-nse backtest`` does, and every fill it takes becomes an :class:`ExitEpisode` (same
   signal, same next-open fill, same CostModel charges, same targets).
2. Behaviour policies (the hand ladder, an ε-ladder, random) roll the episodes through
   :class:`ExitEnv` to log (obs, action, reward, next_obs, done) transitions.
3. Fitted Q-Iteration with a ridge-regressed quadratic approximator learns Q(s, a); the greedy
   policy is evaluated OUT OF SAMPLE per walk-forward fold against the ladder on the same episodes
   (paired, day-blocked sign-flip permutation test) and under cost stress.

CLI: ``kotsin-nse rl exit-policy --symbols RELIANCE,TCS --folds 4``
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from ...bars.unified import UnifiedBar
from ...config import Settings
from ...domain import Instrument, OrderSide
from ...market.session import ist_day
from ..backtest import Backtester, BacktestParams, OpenTrade
from ..history import HistoryStore
from ..stats import sign_flip_test, walk_forward_folds
from .env import ACTIONS, OBS_COLUMNS, ExitEnv, ExitEpisode
from .policies import (
    EpsilonLadderPolicy,
    ExitPolicy,
    LadderPolicy,
    LearnedExitPolicy,
    RandomExitPolicy,
    phi,
)

log = structlog.get_logger(__name__)


class _Recorder(Backtester):
    """The backtester with every fill kept: the episodes are exactly the trades it took."""

    def __init__(self, settings: Settings, params: BacktestParams | None = None) -> None:
        super().__init__(settings, params)
        self.opened: list[tuple[OpenTrade, int, Instrument]] = []

    def _open(self, sig: Any, bar: UnifiedBar, inst: Instrument, *, fukaa: dict[str, Any] | None = None) -> OpenTrade | None:
        t = super()._open(sig, bar, inst, fukaa=fukaa)
        if t is not None:
            self.opened.append((t, int(bar.ts), inst))
        return t


def collect_episodes(
    settings: Settings, params: BacktestParams, store: HistoryStore, symbol: str
) -> tuple[list[UnifiedBar], list[ExitEpisode]]:
    """Run the backtester on ONE symbol and turn each fill into an episode."""
    rec = _Recorder(settings, params)
    rec.run(store, [symbol])
    tf = rec.p.decision_tf
    bars = Backtester._to_bars(store.load(symbol, tf), symbol, symbol, tf)
    by_ts = {b.ts: i for i, b in enumerate(bars)}
    costs = rec.costs
    out: list[ExitEpisode] = []
    for t, ts, inst in rec.opened:
        i = by_ts.get(ts)
        if i is None or t.r_unit <= 0:
            continue
        side = t.sign()
        targets_r = tuple(
            round((x - t.entry) * side / t.r_unit, 4) for x in t.targets if (x - t.entry) * side > 0
        )

        def charges(entry_px: float, exit_px: float, *, _inst: Instrument = inst, _qty: int = t.qty, _side: int = side) -> float:
            buy = OrderSide.BUY if _side > 0 else OrderSide.SELL
            sell = OrderSide.SELL if _side > 0 else OrderSide.BUY
            return (costs.leg(_inst, buy, entry_px, _qty) + costs.leg(_inst, sell, exit_px, _qty)).total

        out.append(
            ExitEpisode(
                symbol=symbol,
                segment=rec.p.segment,
                side=side,
                entry=t.entry,
                r_unit=t.r_unit,
                entry_index=i,
                signal_ts=int(t.signal["ts"]) if t.signal else ts,
                targets_r=targets_r,
                qty=t.qty,
                multiplier=inst.multiplier,
                charges=charges,
                slip_bps=rec.p.slippage_bps,
                tf=tf,
            )
        )
    return bars, out


@dataclass(slots=True)
class EpisodeResult:
    symbol: str
    signal_ts: int
    day: str
    net_r: float
    fees_r: float
    bars_held: int
    reason: str
    policy: str


@dataclass(slots=True)
class Transitions:
    obs: list[np.ndarray] = field(default_factory=list)
    action: list[int] = field(default_factory=list)
    reward: list[float] = field(default_factory=list)
    next_obs: list[np.ndarray] = field(default_factory=list)
    done: list[bool] = field(default_factory=list)

    def arrays(self) -> dict[str, np.ndarray]:
        return {
            "obs": np.asarray(self.obs, dtype=float),
            "action": np.asarray(self.action, dtype=int),
            "reward": np.asarray(self.reward, dtype=float),
            "next_obs": np.asarray(self.next_obs, dtype=float),
            "done": np.asarray(self.done, dtype=bool),
        }

    def __len__(self) -> int:
        return len(self.action)


def run_episode(
    bars: Sequence[UnifiedBar],
    ep: ExitEpisode,
    policy: ExitPolicy,
    *,
    max_bars: int | None,
    giveback_pct: float,
    log_to: Transitions | None = None,
) -> EpisodeResult:
    env = ExitEnv(bars, ep, max_bars=max_bars, giveback_pct=giveback_pct)
    obs = env.reset()
    while not env.done:
        a = policy.act(obs)
        res = env.step(a)
        if log_to is not None:
            log_to.obs.append(obs)
            log_to.action.append(a)
            log_to.reward.append(res.reward)
            log_to.next_obs.append(res.obs)
            log_to.done.append(res.done)
        obs = res.obs
    return EpisodeResult(
        ep.symbol,
        ep.signal_ts,
        ist_day(ep.signal_ts).isoformat(),
        env.net_r,
        env.fees_r,
        env.bars_held,
        env.exit_reason,
        policy.name,
    )


def collect_transitions(
    bars_by_symbol: dict[str, Sequence[UnifiedBar]],
    episodes: Sequence[ExitEpisode],
    *,
    seed: int = 0,
    max_bars: int | None,
    giveback_pct: float,
    trail_arm_pct: float,
) -> Transitions:
    tr = Transitions()
    behaviours: list[ExitPolicy] = [
        LadderPolicy(trail_arm_pct),
        EpsilonLadderPolicy(0.3, seed, trail_arm_pct),
        RandomExitPolicy(seed + 1),
    ]
    for ep in episodes:
        for pol in behaviours:
            run_episode(bars_by_symbol[ep.symbol], ep, pol, max_bars=max_bars, giveback_pct=giveback_pct, log_to=tr)
    return tr


def fqi(
    tr: Transitions | dict[str, np.ndarray],
    *,
    n_iter: int = 40,
    gamma: float = 0.99,
    ridge: float = 1.0,
    min_support: int = 25,
) -> LearnedExitPolicy:
    d = tr.arrays() if isinstance(tr, Transitions) else tr
    obs, act, rew, nxt, done = d["obs"], d["action"], d["reward"], d["next_obs"], d["done"].astype(float)
    mu = obs.mean(axis=0)
    sigma = obs.std(axis=0)
    sigma = np.where(sigma > 1e-9, sigma, 1.0)
    P = phi((obs - mu) / sigma)
    Pn = phi((nxt - mu) / sigma)
    n_a, n_f = len(ACTIONS), P.shape[1]
    W = np.zeros((n_a, n_f))
    support = np.array([(act == a).sum() for a in range(n_a)])
    usable = support >= min_support
    reg = ridge * np.eye(n_f)
    for _ in range(n_iter):
        q_next = Pn @ W.T  # (N, A)
        q_next[:, ~usable] = -np.inf
        target = rew + gamma * (1.0 - done) * np.max(q_next, axis=1)
        target = np.where(np.isfinite(target), target, rew)
        for a in range(n_a):
            if not usable[a]:
                continue
            m = act == a
            X, y = P[m], target[m]
            W[a] = np.linalg.solve(X.T @ X + reg, X.T @ y)
    W[~usable, :] = 0.0
    W[~usable, 0] = -1e6  # never choose an action the data cannot support
    meta = {
        "n_transitions": len(act),
        "support": support.tolist(),
        "n_iter": n_iter,
        "gamma": gamma,
        "ridge": ridge,
        "trained_at": time.time(),
    }
    return LearnedExitPolicy(W, mu, sigma, meta)


def evaluate(
    bars_by_symbol: dict[str, Sequence[UnifiedBar]],
    episodes: Sequence[ExitEpisode],
    policy: ExitPolicy,
    *,
    max_bars: int | None,
    giveback_pct: float,
) -> list[EpisodeResult]:
    return [run_episode(bars_by_symbol[ep.symbol], ep, policy, max_bars=max_bars, giveback_pct=giveback_pct) for ep in episodes]


def _summ(res: Sequence[EpisodeResult]) -> dict[str, Any]:
    if not res:
        return {"n": 0, "mean_net_r": None, "median_net_r": None, "win_rate": None, "fees_r_per_trade": None, "avg_hold_bars": None}
    x = np.array([r.net_r for r in res])
    return {
        "n": len(x),
        "mean_net_r": float(x.mean()),
        "median_net_r": float(np.median(x)),
        "win_rate": float((x > 0).mean()),
        "fees_r_per_trade": float(np.mean([r.fees_r for r in res])),
        "avg_hold_bars": float(np.mean([r.bars_held for r in res])),
    }


def _reasons(res: Sequence[EpisodeResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in res:
        out[r.reason] = out.get(r.reason, 0) + 1
    return out


def experiment(
    settings: Settings,
    symbols: Sequence[str],
    *,
    out_dir: Path,
    n_folds: int = 4,
    seed: int = 0,
    params: BacktestParams | None = None,
    max_bars: int | None = None,
) -> dict[str, Any]:
    t0 = time.time()
    p = params or BacktestParams()
    max_bars = p.limits.time_stop_bars if max_bars is None else max_bars
    giveback = float(p.limits.trail_giveback_pct)
    arm = float(p.limits.trail_arm_pct)
    store = HistoryStore(settings.data_dir / "history")
    bars_by_symbol: dict[str, list[UnifiedBar]] = {}
    episodes: list[ExitEpisode] = []
    per_symbol: dict[str, int] = {}
    for sym in symbols:  # one symbol at a time, on purpose: a laptop is not a cluster
        bars, eps = collect_episodes(settings, p, store, sym)
        if not bars:
            log.warning("rl.no_history", symbol=sym)
            continue
        bars_by_symbol[sym] = bars
        episodes.extend(eps)
        per_symbol[sym] = len(eps)
        log.info("rl.episodes", symbol=sym, n=len(eps))
    episodes.sort(key=lambda e: e.signal_ts)
    if not episodes:
        raise RuntimeError("no episodes: the backtester took no trades on these symbols")

    folds = walk_forward_folds(episodes[0].signal_ts, episodes[-1].signal_ts + 1, n_folds=n_folds)
    ladder = LadderPolicy(arm)
    rows: list[dict[str, Any]] = []
    for fold in folds:
        train = [e for e in episodes if fold.in_train(e.signal_ts)]
        test = [e for e in episodes if fold.in_test(e.signal_ts)]
        row: dict[str, Any] = {
            "fold": fold.index,
            "train_episodes": len(train),
            "test_episodes": len(test),
            "test_start": ist_day(fold.test_start).isoformat(),
            "test_end": ist_day(fold.test_end).isoformat(),
        }
        if len(train) < 30 or len(test) < 5:
            row["skipped"] = "too few episodes"
            rows.append(row)
            continue
        tr = collect_transitions(bars_by_symbol, train, seed=seed + fold.index, max_bars=max_bars, giveback_pct=giveback, trail_arm_pct=arm)
        learned = fqi(tr)
        res_l = evaluate(bars_by_symbol, test, learned, max_bars=max_bars, giveback_pct=giveback)
        res_b = evaluate(bars_by_symbol, test, ladder, max_bars=max_bars, giveback_pct=giveback)
        diffs = [a.net_r - b.net_r for a, b in zip(res_l, res_b, strict=True)]
        pt = sign_flip_test(diffs, [r.day for r in res_l], seed=seed)
        ml, mb = _summ(res_l), _summ(res_b)
        row.update(
            n_transitions=len(tr),
            mean_net_r_policy=ml["mean_net_r"],
            mean_net_r_baseline=mb["mean_net_r"],
            win_rate_policy=ml["win_rate"],
            win_rate_baseline=mb["win_rate"],
            fees_r_policy=ml["fees_r_per_trade"],
            fees_r_baseline=mb["fees_r_per_trade"],
            avg_hold_bars_policy=ml["avg_hold_bars"],
            avg_hold_bars_baseline=mb["avg_hold_bars"],
            exits_policy=_reasons(res_l),
            exits_baseline=_reasons(res_b),
            paired_p_value=pt["p_value"],
            action_support=learned.meta["support"],
        )
        stressed = [
            replace(e, fee_mult=1.5, slip_bps=e.slip_bps * 2, entry=e.entry * (1 + e.side * e.slip_bps / 1e4))
            for e in test
        ]
        row["cost_stress"] = {
            "policy": _summ(evaluate(bars_by_symbol, stressed, learned, max_bars=max_bars, giveback_pct=giveback))["mean_net_r"],
            "baseline": _summ(evaluate(bars_by_symbol, stressed, ladder, max_bars=max_bars, giveback_pct=giveback))["mean_net_r"],
        }
        rows.append(row)

    final = (
        fqi(collect_transitions(bars_by_symbol, episodes, seed=seed, max_bars=max_bars, giveback_pct=giveback, trail_arm_pct=arm))
        if len(episodes) >= 30
        else None
    )
    valid = [r for r in rows if "skipped" not in r]
    summary = {
        "symbols": list(bars_by_symbol),
        "episodes": len(episodes),
        "episodes_by_symbol": per_symbol,
        "n_folds_evaluated": len(valid),
        "mean_net_r_policy": float(np.mean([r["mean_net_r_policy"] for r in valid])) if valid else None,
        "mean_net_r_baseline": float(np.mean([r["mean_net_r_baseline"] for r in valid])) if valid else None,
        "folds_policy_beats_baseline": sum(1 for r in valid if r["mean_net_r_policy"] > r["mean_net_r_baseline"]),
        "paired_p_values": [r["paired_p_value"] for r in valid],
        "wall_clock_s": round(time.time() - t0, 1),
    }
    name = f"exit_policy_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}"
    artefact = {
        "kind": "exit_policy",
        "name": name,
        "created_ts": time.time(),
        "config": {
            "symbols": list(symbols),
            "n_folds": n_folds,
            "seed": seed,
            "max_bars": max_bars,
            "giveback_pct": giveback,
            "trail_arm_pct": arm,
            "decision_tf": p.decision_tf,
            "segment": p.segment.value,
            "actions": list(ACTIONS),
            "obs_columns": OBS_COLUMNS,
        },
        "summary": summary,
        "folds": rows,
        "policy": final.to_artefact() if final else None,
        "caveats": [
            "Episodes are FUDKII/FUKAA trades that lose money on average; an exit policy can only redistribute that outcome, never create an edge the entry lacks.",
            "Actions apply at bar close with a one-bar lag versus the live ExitEngine, which acts on every tick.",
            "Fills at the next open with fixed slippage; the last target fills at its price; no book impact. REST bars carry no OI or depth, so no microstructure feature exists here.",
            "Walk-forward: later folds train on more data; fold p-values are not adjusted for the number of folds.",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(artefact, default=str))
    return artefact


def list_runs(root: Path, limit: int = 25) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("exit_policy_*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            a = json.loads(p.read_text())
            out.append({"name": a["name"], "created_ts": a["created_ts"], "config": a["config"], "summary": a["summary"]})
        except Exception:  # noqa: BLE001 - a half-written artefact must not break the listing
            continue
    return out


def load_run(root: Path, name: str) -> dict[str, Any]:
    if not name.startswith("exit_policy_") or "/" in name or ".." in name:
        raise KeyError(f"unknown run {name!r}")
    p = root / f"{name}.json"
    if not p.exists():
        raise KeyError(f"unknown run {name!r}")
    return json.loads(p.read_text())


__all__ = [
    "EpisodeResult",
    "Transitions",
    "collect_episodes",
    "collect_transitions",
    "evaluate",
    "experiment",
    "fqi",
    "list_runs",
    "load_run",
    "run_episode",
]
