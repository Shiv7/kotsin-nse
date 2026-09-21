"""Grade a hypothesis by running the backtester on it — **out of sample**.

The committee proposes; the backtester disposes. A hypothesis is a list of parameter changes on
``BacktestParams``. What makes the grade mean something:

* **Holdout.** The cached history is split by time; the last ``holdout_frac`` (30 %) is never
  shown to the committee and is the only range the verdict is read from. The in-sample grade is
  reported alongside, labelled, so a fit to the diagnosis data is visible as such. MadEvolve
  (2026) measured 39–54 % of validation PnL surviving a held-out test even with a fixed harness;
  a harness that grades where it proposed is measuring its own optimism.
* **Multiple testing.** Every graded hypothesis is a draw. ``n_tested`` Bonferroni-adjusts the
  permutation p before the verdict is read (Bailey et al. 2014's point, in its simplest form).
* **Cost stress.** The out-of-sample comparison is repeated at ×1.5 brokerage and ×2 slippage; a
  "confirmed" that does not survive it is downgraded. The cost model, not the signal, decided the
  sign of everything this book has measured so far.
* **Regime spread.** Per-month average R of both arms on the holdout, and how many months the
  change won — one lucky month is the commonest way a delta lies.

Both arms are recomputed every time on the same cache and range: deterministic, never a
comparison against a number that came from a different symbol set. The verdict rule is the repo's
research bar (``docs/LEARNINGS.md`` R13): day-clustered mean R and a within-day permutation test.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, timedelta
from typing import Any

from ..config import Segment, Settings
from ..market.session import ist_day
from ..research.backtest import Backtester, BacktestParams, BtTrade
from ..research.history import HistoryStore
from ..research.stats import day_clustered_mean, within_day_permutation
from .schemas import ParamChange

PATCHABLE_ROOTS = ("fudkii", "fukaa", "limits", "slippage_bps", "position_budget_inr")
#: the p at which a delta is believed; the repo's research bar
P_THRESHOLD = 0.10
#: share of the cached range, by time, that the committee never sees
DEFAULT_HOLDOUT_FRAC = 0.3
STRESS_BROKERAGE_MULT = 1.5
STRESS_SLIPPAGE_MULT = 2.0


# -- parameter patches ---------------------------------------------------------------------------


def _cast(current: Any, value: float) -> Any:
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        return round(value)
    if isinstance(current, float):
        return float(value)
    if current is None:  # an optional numeric cap, e.g. fukaa.top_n
        return round(value) if float(value).is_integer() else float(value)
    raise ValueError(f"a {type(current).__name__} field cannot be set from a number")


def _set(obj: Any, parts: Sequence[str], value: float) -> Any:
    name, rest = parts[0], parts[1:]
    if not is_dataclass(obj) or name not in {f.name for f in fields(obj)}:
        raise ValueError(f"unknown parameter {name!r} on {type(obj).__name__}")
    current = getattr(obj, name)
    if rest:
        return replace(obj, **{name: _set(current, rest, value)})
    return replace(obj, **{name: _cast(current, value)})


def apply_changes(params: BacktestParams, changes: Sequence[ParamChange]) -> BacktestParams:
    """Every path must name a field that exists (R1: an unknown key is a bug, not a no-op)."""
    out = params
    for ch in changes:
        parts = ch.path.split(".")
        if parts[0] not in PATCHABLE_ROOTS:
            raise ValueError(
                f"unknown parameter path {ch.path!r}; roots: {', '.join(PATCHABLE_ROOTS)}"
            )
        out = _set(out, parts, ch.value)
    return out


def changes_key(changes: Sequence[Any]) -> tuple[tuple[str, float], ...]:
    """The identity of a hypothesis for the veto: the same paths at the same values."""
    items = []
    for c in changes:
        path = c.path if hasattr(c, "path") else c["path"]
        value = c.value if hasattr(c, "value") else c["value"]
        items.append((str(path), float(value)))
    return tuple(sorted(items))


# -- ranges ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Split:
    train_start: date
    train_end: date  # inclusive
    test_start: date
    test_end: date  # inclusive

    def to_json(self) -> dict[str, str]:
        return {
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
        }


def holdout_split(
    store: HistoryStore,
    symbols: Sequence[str],
    *,
    tf: str = "30m",
    frac: float = DEFAULT_HOLDOUT_FRAC,
) -> Split:
    """Cut the cached range by time: the last ``frac`` is the holdout."""
    covs = [c for s in symbols if (c := store.coverage(s, tf)) is not None]
    if not covs:
        raise RuntimeError("no cached history — run `kotsin-nse fetch-history --symbols …` first")
    lo = min(c[0] for c in covs)
    hi = max(c[1] for c in covs)
    if not 0.0 < frac < 1.0:
        raise ValueError("holdout fraction must be between 0 and 1")
    cut = ist_day(lo + (hi - lo) * (1.0 - frac))
    return Split(ist_day(lo), cut - timedelta(days=1), cut, ist_day(hi))


def time_blocks(start: date, end: date, n: int) -> list[tuple[date, date]]:
    """``n`` consecutive, equal, inclusive date blocks covering [start, end]."""
    if n < 1 or end < start:
        raise ValueError("need n ≥ 1 and end ≥ start")
    days = (end - start).days + 1
    edges = [start + timedelta(days=days * i // n) for i in range(n + 1)]
    return [(edges[i], edges[i + 1] - timedelta(days=1)) for i in range(n)]


# -- statistics --------------------------------------------------------------------------------------


def arm_stats(trades: Sequence[BtTrade]) -> dict[str, Any]:
    cm = day_clustered_mean([t.r_multiple for t in trades], [t.day for t in trades])
    return {
        "n": cm.n,
        "n_days": cm.n_days,
        "avg_r": round(cm.mean, 3),
        "avg_r_t": round(cm.t_stat, 2) if cm.t_stat is not None else None,
        "net": round(sum(t.net for t in trades), 2),
        "win_rate": round(sum(1 for t in trades if t.net > 0) / len(trades) * 100, 1)
        if trades
        else None,
    }


def grade(
    baseline: Sequence[BtTrade], patched: Sequence[BtTrade], *, n_tested: int = 1
) -> dict[str, Any]:
    """``confirmed`` when the patched arm's average R is higher and the within-day permutation p —
    Bonferroni-adjusted for the number of hypotheses graded so far — is at most 0.10 on a sample
    the repo trusts; ``refuted`` when it is not higher with the same confidence; ``inconclusive``
    otherwise, including when the change trades too little to say."""
    a, b = arm_stats(baseline), arm_stats(patched)
    out: dict[str, Any] = {"baseline": a, "patched": b, "n_tested": max(1, n_tested)}
    if not patched or not baseline:
        out.update(
            verdict="inconclusive",
            delta_avg_r=None,
            p_value=None,
            p_adjusted=None,
            note="one arm produced no trades",
        )
        return out
    perm = within_day_permutation(
        [t.r_multiple for t in baseline] + [t.r_multiple for t in patched],
        [False] * len(baseline) + [True] * len(patched),
        [t.day for t in baseline] + [t.day for t in patched],
    )
    delta = round(b["avg_r"] - a["avg_r"], 3)
    p_adj = min(1.0, perm.p_value * max(1, n_tested))
    out.update(
        delta_avg_r=delta,
        p_value=round(perm.p_value, 4),
        p_adjusted=round(p_adj, 4),
        n_days=perm.n_days,
    )
    adj = f" (p {perm.p_value:.3f} × {n_tested} tested = {p_adj:.3f})" if n_tested > 1 else f" (p {perm.p_value:.3f})"
    if perm.too_small:
        out.update(
            verdict="inconclusive",
            note=f"sample too small ({b['n']} patched trades over {perm.n_days} days)",
        )
    elif p_adj <= P_THRESHOLD:
        out.update(
            verdict="confirmed" if delta > 0 else "refuted",
            note=f"Δ avg R {delta:+.3f}, within-day permutation{adj}",
        )
    else:
        out.update(
            verdict="inconclusive",
            note=f"Δ avg R {delta:+.3f} is within noise{adj}",
        )
    return out


def monthly(baseline: Sequence[BtTrade], patched: Sequence[BtTrade]) -> dict[str, Any]:
    """Average R per calendar month for both arms, and how many months the change won."""
    by: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"baseline": [], "patched": []})
    for t in baseline:
        by[t.day[:7]]["baseline"].append(t.r_multiple)
    for t in patched:
        by[t.day[:7]]["patched"].append(t.r_multiple)
    rows = []
    beats = both = 0
    for month in sorted(by):
        b, p = by[month]["baseline"], by[month]["patched"]
        row = {
            "month": month,
            "baseline_n": len(b),
            "baseline_avg_r": round(sum(b) / len(b), 3) if b else None,
            "patched_n": len(p),
            "patched_avg_r": round(sum(p) / len(p), 3) if p else None,
        }
        if b and p:
            both += 1
            beats += row["patched_avg_r"] > row["baseline_avg_r"]
        rows.append(row)
    return {"rows": rows, "months": both, "patched_beats_baseline": beats}


# -- the experiment ---------------------------------------------------------------------------------


def run_experiment(
    settings: Settings,
    *,
    changes: Sequence[ParamChange],
    segment: Segment = Segment.NSE_EQ,
    symbols: Sequence[str] | None = None,
    max_symbols: int | None = None,
    holdout_frac: float = DEFAULT_HOLDOUT_FRAC,
    n_tested: int = 1,
    cost_stress: bool = True,
) -> dict[str, Any]:
    """Synchronous and CPU-bound — call it in a worker thread. Six backtests: both arms in sample,
    both arms out of sample, both arms out of sample under cost stress."""
    store = HistoryStore(settings.data_dir / "history")
    syms = list(symbols) if symbols else store.symbols("30m")
    if max_symbols:
        syms = syms[:max_symbols]
    if not syms:
        raise RuntimeError("no cached history — run `kotsin-nse fetch-history --symbols …` first")
    split = holdout_split(store, syms, frac=holdout_frac)
    base = BacktestParams(segment=segment)
    patched = apply_changes(base, changes)
    t0 = time.time()

    def arm(params: BacktestParams, s: Settings, start: date, end: date) -> list[BtTrade]:
        return Backtester(s, params).run(store, syms, start=start, end=end).trades

    is_base = arm(base, settings, split.train_start, split.train_end)
    is_pat = arm(patched, settings, split.train_start, split.train_end)
    oos_base = arm(base, settings, split.test_start, split.test_end)
    oos_pat = arm(patched, settings, split.test_start, split.test_end)

    in_sample = grade(is_base, is_pat)
    oos = grade(oos_base, oos_pat, n_tested=n_tested)
    result: dict[str, Any] = {
        **oos,  # baseline / patched / verdict / delta / p — the out-of-sample reading is the reading
        "in_sample": {k: in_sample[k] for k in ("baseline", "patched", "delta_avg_r", "p_value", "verdict", "note")},
        "out_of_sample": {k: oos[k] for k in ("baseline", "patched", "delta_avg_r", "p_value", "p_adjusted", "verdict", "note")},
        "split": split.to_json(),
        "holdout_frac": holdout_frac,
        "monthly": monthly(oos_base, oos_pat),
        "changes": [c.model_dump() for c in changes],
        "segment": segment.value,
        "symbols": len(syms),
        "params_patched": patched.to_json(),
    }
    if cost_stress:
        stressed = settings.model_copy(
            update={
                "cost_brokerage_per_order_inr": settings.cost_brokerage_per_order_inr
                * STRESS_BROKERAGE_MULT
            }
        )
        sb = arm(
            replace(base, slippage_bps=base.slippage_bps * STRESS_SLIPPAGE_MULT),
            stressed, split.test_start, split.test_end,
        )
        sp = arm(
            replace(patched, slippage_bps=patched.slippage_bps * STRESS_SLIPPAGE_MULT),
            stressed, split.test_start, split.test_end,
        )
        cs = grade(sb, sp)
        result["cost_stress"] = {
            "brokerage_mult": STRESS_BROKERAGE_MULT,
            "slippage_mult": STRESS_SLIPPAGE_MULT,
            "baseline": cs["baseline"],
            "patched": cs["patched"],
            "delta_avg_r": cs["delta_avg_r"],
            "p_value": cs["p_value"],
        }
        survives = cs["delta_avg_r"] is not None and cs["delta_avg_r"] > 0
        result["survives_cost_stress"] = survives
        if result["verdict"] == "confirmed" and not survives:
            result["verdict"] = "inconclusive"
            result["note"] += "; the improvement does not survive ×1.5 brokerage / ×2 slippage"
    result["seconds"] = round(time.time() - t0, 1)
    return result


def walk_forward_score(
    settings: Settings,
    params: BacktestParams,
    store: HistoryStore,
    symbols: Sequence[str],
    *,
    start: date,
    end: date,
    n_blocks: int = 4,
    min_trades: int = 60,
) -> dict[str, Any]:
    """The fitness a program-evolution harness may see: the mean over consecutive time blocks of
    the day-clustered average R, net of costs, with a penalty for trading too little. Never point
    this at the holdout — that is what ``run_experiment`` is for, once, on the winner."""
    blocks = []
    for a, b in time_blocks(start, end, n_blocks):
        trades = Backtester(settings, params).run(store, list(symbols), start=a, end=b).trades
        cm = day_clustered_mean([t.r_multiple for t in trades], [t.day for t in trades])
        blocks.append(
            {
                "start": a.isoformat(),
                "end": b.isoformat(),
                "n": cm.n,
                "avg_r": round(cm.mean, 3) if cm.n else None,
                "net": round(sum(t.net for t in trades), 2),
            }
        )
    means = [x["avg_r"] for x in blocks if x["avg_r"] is not None]
    total = sum(x["n"] for x in blocks)
    score = sum(means) / len(means) if means else -9.0
    if total < min_trades:
        score -= (min_trades - total) / min_trades  # a strategy that stops trading is not improved
    return {
        "score": round(score, 4),
        "trades": total,
        "blocks": blocks,
        "blocks_positive": sum(1 for m in means if m > 0),
        "blocks_total": len(means),
    }


__all__ = [
    "DEFAULT_HOLDOUT_FRAC",
    "PATCHABLE_ROOTS",
    "Split",
    "apply_changes",
    "arm_stats",
    "changes_key",
    "grade",
    "holdout_split",
    "monthly",
    "run_experiment",
    "time_blocks",
    "walk_forward_score",
]
