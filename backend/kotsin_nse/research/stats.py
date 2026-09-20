"""Statistics that survive contact with trading data.

One rule, learned expensively: **trades cluster by day.** A naive split has twice produced a
"significant" result here that collapsed under a within-day permutation test. So:

* :func:`day_clustered_mean` reports the mean with a standard error computed over **day** means,
  not over trades, because trades within a day are not independent draws;
* :func:`within_day_permutation` is the test to run before believing any split. It shuffles labels
  *inside each day*, so a difference that is really "Tuesday was a good day" cannot survive it;
* every function reports `n` and refuses to pretend a tiny sample is informative.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClusteredMean:
    mean: float
    stderr: float | None
    n: int
    n_days: int

    @property
    def t_stat(self) -> float | None:
        return None if not self.stderr else self.mean / self.stderr

    @property
    def too_small(self) -> bool:
        """Below 30 trades or 10 days, say so rather than quoting a t-statistic."""
        return self.n < 30 or self.n_days < 10


def day_clustered_mean(values: Sequence[float], days: Sequence[str]) -> ClusteredMean:
    if not values:
        return ClusteredMean(0.0, None, 0, 0)
    by_day: dict[str, list[float]] = defaultdict(list)
    for v, d in zip(values, days, strict=True):
        by_day[d].append(v)
    day_means = [sum(v) / len(v) for v in by_day.values()]
    mean = sum(values) / len(values)
    k = len(day_means)
    if k < 2:
        return ClusteredMean(mean, None, len(values), k)
    grand = sum(day_means) / k
    var = sum((m - grand) ** 2 for m in day_means) / (k - 1)
    return ClusteredMean(mean, math.sqrt(var / k), len(values), k)


@dataclass(frozen=True, slots=True)
class PermutationResult:
    observed: float
    p_value: float
    iterations: int
    n_a: int
    n_b: int
    n_days: int

    @property
    def too_small(self) -> bool:
        return min(self.n_a, self.n_b) < 30 or self.n_days < 10


def within_day_permutation(
    values: Sequence[float],
    labels: Sequence[bool],
    days: Sequence[str],
    *,
    iterations: int = 5000,
    seed: int = 7,
) -> PermutationResult:
    """Two-sided p for "label A beats label B", shuffling labels **within each day**.

    A between-day shuffle would let a difference in which days each arm happened to fall on leak
    into the result. That is precisely the mistake this exists to prevent.
    """
    rng = random.Random(seed)
    by_day: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for v, lab, d in zip(values, labels, days, strict=True):
        by_day[d].append((v, lab))

    def diff(assignment: dict[str, list[bool]]) -> float | None:
        a_sum = a_n = b_sum = b_n = 0.0
        for d, rows in by_day.items():
            for (v, _), lab in zip(rows, assignment[d], strict=True):
                if lab:
                    a_sum += v
                    a_n += 1
                else:
                    b_sum += v
                    b_n += 1
        if a_n == 0 or b_n == 0:
            return None
        return a_sum / a_n - b_sum / b_n

    actual = {d: [lab for _, lab in rows] for d, rows in by_day.items()}
    observed = diff(actual)
    n_a = sum(1 for lab in labels if lab)
    n_b = len(labels) - n_a
    if observed is None:
        return PermutationResult(0.0, 1.0, 0, n_a, n_b, len(by_day))

    hits = 0
    for _ in range(iterations):
        shuffled = {}
        for d, rows in by_day.items():
            labs = [lab for _, lab in rows]
            rng.shuffle(labs)
            shuffled[d] = labs
        d_perm = diff(shuffled)
        if d_perm is not None and abs(d_perm) >= abs(observed):
            hits += 1
    return PermutationResult(observed, (hits + 1) / (iterations + 1), iterations, n_a, n_b, len(by_day))


def max_drawdown(equity: Sequence[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for v in equity:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return worst


def profit_factor(pnls: Sequence[float]) -> float | None:
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    return None if losses == 0 else wins / losses
