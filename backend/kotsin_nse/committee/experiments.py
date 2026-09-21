"""Grade a hypothesis by running the backtester on it.

The committee proposes; the backtester disposes. A hypothesis is a list of parameter changes on
``BacktestParams``. The experiment runs the baseline (the code's current defaults) and the patched
parameters over the same cached history and compares the day-clustered average R with the repo's
within-day permutation test — the statistic ``docs/LEARNINGS.md`` R13 demands of any claim. Both
arms are recomputed every time: deterministic, and never a comparison against a number that came
from a different symbol set or date range.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import fields, is_dataclass, replace
from datetime import date
from typing import Any

from ..config import Segment, Settings
from ..research.backtest import Backtester, BacktestParams, BtTrade
from ..research.history import HistoryStore
from ..research.stats import day_clustered_mean, within_day_permutation
from .schemas import ParamChange

PATCHABLE_ROOTS = ("fudkii", "fukaa", "limits", "slippage_bps", "position_budget_inr")
#: the p at which a delta is believed; the repo's research bar
P_THRESHOLD = 0.10


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


def grade(baseline: Sequence[BtTrade], patched: Sequence[BtTrade]) -> dict[str, Any]:
    """``confirmed`` when the patched arm's average R is higher and the within-day permutation
    p is at most 0.10 with a sample the repo trusts; ``refuted`` when it is not higher with the
    same confidence; ``inconclusive`` otherwise — including when the change trades too little."""
    a, b = arm_stats(baseline), arm_stats(patched)
    out: dict[str, Any] = {"baseline": a, "patched": b}
    if not patched or not baseline:
        out.update(
            verdict="inconclusive",
            delta_avg_r=None,
            p_value=None,
            note="one arm produced no trades",
        )
        return out
    perm = within_day_permutation(
        [t.r_multiple for t in baseline] + [t.r_multiple for t in patched],
        [False] * len(baseline) + [True] * len(patched),
        [t.day for t in baseline] + [t.day for t in patched],
    )
    delta = round(b["avg_r"] - a["avg_r"], 3)
    out.update(delta_avg_r=delta, p_value=round(perm.p_value, 4), n_days=perm.n_days)
    if perm.too_small:
        out.update(
            verdict="inconclusive",
            note=f"sample too small ({b['n']} patched trades over {perm.n_days} days)",
        )
    elif perm.p_value <= P_THRESHOLD:
        out.update(
            verdict="confirmed" if delta > 0 else "refuted",
            note=f"Δ avg R {delta:+.3f}, within-day permutation p {perm.p_value:.3f}",
        )
    else:
        out.update(
            verdict="inconclusive",
            note=f"Δ avg R {delta:+.3f} is within noise (p {perm.p_value:.3f})",
        )
    return out


def run_experiment(
    settings: Settings,
    *,
    changes: Sequence[ParamChange],
    segment: Segment = Segment.NSE_EQ,
    symbols: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    max_symbols: int | None = None,
) -> dict[str, Any]:
    """Synchronous and CPU-bound — call it in a worker thread."""
    store = HistoryStore(settings.data_dir / "history")
    syms = list(symbols) if symbols else store.symbols("30m")
    if max_symbols:
        syms = syms[:max_symbols]
    if not syms:
        raise RuntimeError("no cached history — run `kotsin-nse fetch-history --symbols …` first")
    base = BacktestParams(segment=segment)
    patched = apply_changes(base, changes)
    t0 = time.time()
    a = Backtester(settings, base).run(store, syms, start=start, end=end)
    b = Backtester(settings, patched).run(store, syms, start=start, end=end)
    result = grade(a.trades, b.trades)
    result.update(
        changes=[c.model_dump() for c in changes],
        segment=segment.value,
        symbols=len(syms),
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
        baseline_run=a.id,
        patched_run=b.id,
        seconds=round(time.time() - t0, 1),
        params_patched=patched.to_json(),
    )
    return result


__all__ = ["PATCHABLE_ROOTS", "apply_changes", "arm_stats", "grade", "run_experiment"]
