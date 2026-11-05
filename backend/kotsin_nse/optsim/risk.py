"""The risk layer the playbooks run on and we did not have: capital allocation across books, a margin estimate per
structure, the monthly loss budget, and the drawdown distribution (a bootstrap over the recorded cycles — the
practitioners' Monte Carlo) so a book's "4 % a month" comes with the quarter that goes wrong.

Margins are ESTIMATES for one NIFTY lot (65) in Nov 2026 — SPAN plus exposure as 5paisa levies it changes with
volatility and must be read off the broker's calculator before any size is believed.
"""

from __future__ import annotations

import random
from typing import Any

#: approximate margin per lot, ₹, NIFTY, Nov 2026 — verify on 5paisa's margin calculator
MARGIN_INR: dict[str, float] = {
    "batman": 225_000.0,  # net one naked lot per side (the video's own figure for the structure)
    "batman_winged": 140_000.0,  # the far wing turns each side into a defined-risk spread
    "iron_condor": 45_000.0,
    "iron_fly": 40_000.0,
    "strangle": 170_000.0,
    "strangle_hedged": 55_000.0,
    "straddle": 160_000.0,
    "future": 190_000.0,
    "covered_call": 220_000.0,
}


def margin_for(structure: str, lots: int = 1) -> float:
    return MARGIN_INR.get(structure, 150_000.0) * lots


def monte_carlo_drawdown(pnls: list[float], *, n_paths: int = 10_000, horizon: int | None = None, seed: int = 7) -> dict[str, float]:
    """Bootstrap the per-cycle P&L into ``n_paths`` paths of ``horizon`` cycles (default: as many as recorded) and
    read the maximum drawdown of each: the median, the 90th and 99th percentiles, and the worst. The recorded
    sequence is one path; the distribution is what the same cycles could have done in another order."""
    if not pnls:
        return {}
    rng = random.Random(seed)
    h = horizon or len(pnls)
    dds = []
    for _ in range(n_paths):
        eq = peak = 0.0
        dd = 0.0
        for _ in range(h):
            eq += rng.choice(pnls)
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        dds.append(dd)
    dds.sort()
    q = lambda p: dds[min(len(dds) - 1, int(p * len(dds)))]  # noqa: E731
    return {"paths": n_paths, "horizon": h, "median_dd": round(q(0.5), 1), "p90_dd": round(q(0.1), 1), "p99_dd": round(q(0.01), 1), "worst_dd": round(dds[0], 1),
            "mean_path_pnl": round(sum(pnls) / len(pnls) * h, 1)}


def recorded_drawdown(pnls: list[float]) -> float:
    eq = peak = 0.0
    dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return round(dd, 1)


def budget_check(net_inr_this_month: float, budget_inr: float) -> dict[str, Any]:
    """The monthly loss budget: how much of it a book has spent and whether it is halted for the month."""
    used = max(-net_inr_this_month, 0.0)
    return {"net_inr": round(net_inr_this_month), "budget_inr": round(budget_inr), "used_pct": round(used / budget_inr * 100, 1) if budget_inr else None,
            "halted": budget_inr > 0 and used >= budget_inr}


def allocation(capital_inr: float, books: list[dict[str, Any]], *, margin_use: float = 0.5, budget_pct: float = 4.0) -> list[dict[str, Any]]:
    """Each book's allocation (its share of the capital), the lots that share affords at ``margin_use`` of it (the
    playbooks use half their margin), and the monthly loss budget as ``budget_pct`` of the allocation."""
    out = []
    for b in books:
        alloc = capital_inr * b["share"]
        m = margin_for(b["structure"])
        lots = int(alloc * margin_use // m) if m > 0 else 0
        out.append({"book": b["name"], "structure": b["structure"], "share_pct": round(b["share"] * 100, 1), "allocation_inr": round(alloc),
                    "margin_per_lot_inr": round(m), "lots": lots, "monthly_budget_inr": round(alloc * budget_pct / 100)})
    return out


def report(capital_inr: float, books: list[dict[str, Any]], cycles_by_book: dict[str, list[dict[str, Any]]], *, lot: int = 65) -> dict[str, Any]:
    """The portfolio view: per book the allocation, the recorded cycles' mean and worst, the drawdown distribution
    scaled to the lots the allocation affords, and what the 99th-percentile drawdown is as a share of the capital."""
    alloc = {a["book"]: a for a in allocation(capital_inr, books)}
    rows = []
    for name, a in alloc.items():
        c = cycles_by_book.get(name, [])
        net = [x["net_inr"] for x in c]
        mc = monte_carlo_drawdown(net) if net else {}
        lots = max(a["lots"], 1)
        rows.append({**a, "cycles": len(c), "mean_net_inr": round(sum(net) / len(net)) if net else None, "worst_cycle_inr": round(min(net)) if net else None,
                     "recorded_dd_inr": recorded_drawdown(net) if net else None,
                     "p99_dd_inr_at_lots": round(mc["p99_dd"] * lots) if mc else None, "p99_dd_pct_capital": round(mc["p99_dd"] * lots / capital_inr * 100, 1) if mc else None,
                     "mean_month_inr_at_lots": round(sum(net) / len(net) * lots) if net else None})
    total_p99 = sum(r["p99_dd_inr_at_lots"] or 0 for r in rows)
    return {"capital_inr": capital_inr, "books": rows, "sum_p99_dd_inr": round(total_p99), "sum_p99_dd_pct": round(total_p99 / capital_inr * 100, 1),
            "note": "p99 drawdowns summed assume the books lose together — the conservative reading; margins are estimates"}


__all__ = ["MARGIN_INR", "allocation", "budget_check", "margin_for", "monte_carlo_drawdown", "recorded_drawdown", "report"]
