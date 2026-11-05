"""The combined book: the good points of the five playbooks, each with data behind it, crossed in a small grid on the
NIFTY bhavcopy history 2024-01 → 2026-11 (the owner, 6 Nov 2026: "take good points and use over and then finally build
one good one"). Pre-stated selection: among configurations with at least 25 cycles and a worst cycle no worse than
three times the mean credit, the highest mean net rupees a cycle; ties to fewer stops. The pick is IN-SAMPLE (chosen on
the same 2.8 years); its test is the paper book that runs it forward from here.

Axes: structure (iron condor at 16/20/25-delta shorts with hedges 8 deltas further out; the winged Batman with 8- or
10-delta wings), entry 35–50 days out with two positions overlapping, exit at expiry or three days before, loss budget
none or twice the credit, regime filter none or the monthly IV above its 40th percentile.
Usage: grid.py OUT_DIR"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd

from kotsin_nse.config import Settings
from kotsin_nse.optsim import paper
from kotsin_nse.optsim.sim import Book, Entry, Exit


def main() -> None:
    out = Path(sys.argv[1])
    settings = Settings(engine_enabled=False)
    store, _, rroot = paper.paths(settings)
    reg = paper.regime_pct(store, rroot)
    books: dict[str, Book] = {}
    for sd, exit_dte, budget, regime in itertools.product((0.16, 0.20, 0.25), (0, 3), (None, 2.0), (None, 40.0)):
        name = f"condor{int(sd*100)}_x{exit_dte}_b{'n' if budget is None else int(budget)}_r{'n' if regime is None else int(regime)}"
        books[name] = Book(name, "iron_condor", {"short_delta": sd, "hedge_delta": round(sd - 0.08, 2)}, Entry("monthly", 35, 50), Exit(exit_dte, loss_budget_credit_mult=budget),
                           "none", costs=paper.COSTS, max_open=2, min_iv_pct=regime)
    for wd, exit_dte, budget, regime in itertools.product((0.08, 0.10), (0, 3), (None, 2.0), (None, 40.0)):
        name = f"batmanw{int(wd*100)}_x{exit_dte}_b{'n' if budget is None else int(budget)}_r{'n' if regime is None else int(regime)}"
        books[name] = Book(name, "batman_winged", {"sell_delta": 0.16, "buy_delta": 0.20, "ratio": 2, "wing_delta": wd}, Entry("monthly", 35, 50),
                           Exit(exit_dte, loss_budget_credit_mult=budget), "none", costs=paper.COSTS, max_open=2, min_iv_pct=regime)
    D = paper.run_backtests(store, "2024-01-01", None, books, reg)
    D["worst_over_credit"] = (D["worst_pts"] / D["mean_credit"]).round(2)
    D["eligible"] = (D["cycles"] >= 25) & (D["worst_over_credit"] >= -3.0)
    D = D.sort_values(["eligible", "mean_net_inr", "stops"], ascending=[False, False, True])
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print("== the grid, 2024-01 → 2026-11, ₹ at lot 65 after 0.5 % slippage and charges; eligible = ≥ 25 cycles and worst ≥ −3× the mean credit")
    print(D[["cycles", "wins", "sum_pts", "mean_pts", "worst_pts", "mean_credit", "worst_over_credit", "stops", "sum_net_inr", "mean_net_inr", "worst_net_inr", "eligible"]].to_string())
    D.to_csv(out / "grid.csv")
    el = D[D["eligible"]]
    if len(el):
        w = el.index[0]
        print(f"\n== the pick by the pre-stated rule: {w} — mean ₹{el.iloc[0]['mean_net_inr']:+,.0f} a cycle over {int(el.iloc[0]['cycles'])} cycles, worst ₹{el.iloc[0]['worst_net_inr']:+,.0f}")
        st = D.attrs["states"][w]
        pd.DataFrame(st["cycles"]).to_csv(out / f"cycles-{w}.csv", index=False)
    else:
        print("\n== nothing eligible")


if __name__ == "__main__":
    main()
