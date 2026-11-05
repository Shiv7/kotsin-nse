"""The paper books: the pre-registered option structures that run forward one bhavcopy at a time, their state in
``data-live/paper/<book>.json``, and the portfolio report around them. The registry IS the pre-registration — a
book's rules are fixed here before its first forward day, and the backtest of the same rules over the bhavcopy
history (``--backtest``) is the number it has to beat going forward.

Books (NIFTY, one unit each; rupees at lot 65; slippage 0.5 % of premium each way, ₹20 an order):
* ``batman_hold`` — the Batman held to expiry, no stop (the owner, 6 Nov 2026: "save it, the win rate is incredible").
* ``batman_stop`` — the video's own rule, stop at a mark-to-market loss equal to the credit.
* ``batman_winged`` — held to expiry with a 5-delta wing bought beyond each sold strike: the tail capped.
* ``condor_25_17`` — the monthly iron condor, 25-delta shorts, 17-delta hedges, opened in the first five days of the
  month on the nearest monthly with ≥ 15 days, no adjustment, 4 % loss budget as a multiple of credit is NOT used —
  the transcript's ₹20k on ₹5 lakh is 4 % of capital, here ``loss_budget_pts`` = 60 points (≈ ₹3,900 a lot).
* ``condor_to_fly`` — the same condor, converted to a fly when the future closes beyond a short strike.
* ``strangle_weekly`` — the weekly 10-delta strangle opened on Tuesday for the following week's expiry, premium
  balanced at 70 %, closed two days before expiry, loss budget 2× credit.
* ``strangle_weekly_hedged`` — the same with 3-delta wings.

Usage: python -m kotsin_nse.optsim.paper --backtest [--start 2024-01-01] | --advance | --report | --regime
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import Settings
from . import regime, risk
from .chain import Store
from .sim import Book, Costs, Entry, Exit, backtest, new_state, step, summary

COSTS = Costs(slippage_pct=0.5, brokerage_inr=20.0, lot=65)

BOOKS: dict[str, Book] = {
    "batman_hold": Book("batman_hold", "batman", {"sell_delta": 0.16, "buy_delta": 0.20, "ratio": 2}, Entry("monthly", 30, 45), Exit(0), "none", costs=COSTS,
                        note="the video's structure held to expiry, no stop", max_open=2),
    "batman_stop": Book("batman_stop", "batman", {"sell_delta": 0.16, "buy_delta": 0.20, "ratio": 2}, Entry("monthly", 30, 45), Exit(0, loss_budget_credit_mult=1.0), "none",
                        costs=COSTS, note="the video's rule: stop at −credit", max_open=2),
    "batman_winged": Book("batman_winged", "batman_winged", {"sell_delta": 0.16, "buy_delta": 0.20, "ratio": 2, "wing_delta": 0.05}, Entry("monthly", 30, 45), Exit(0), "none",
                          costs=COSTS, note="held to expiry, the tail capped by a 5-delta wing each side", max_open=2),
    "condor_25_17": Book("condor_25_17", "iron_condor", {"short_delta": 0.25, "hedge_delta": 0.17}, Entry("monthly", 15, 40, first_days_of_month=5), Exit(0, loss_budget_pts=60.0),
                         "none", costs=COSTS, note="monthly condor, no adjustment, 60-point loss budget"),
    "condor_to_fly": Book("condor_to_fly", "iron_condor", {"short_delta": 0.25, "hedge_delta": 0.17}, Entry("monthly", 15, 40, first_days_of_month=5), Exit(0, loss_budget_pts=60.0),
                          "convert_to_fly", costs=COSTS, note="the condor converted to a fly when a short strike is closed beyond"),
    "strangle_weekly": Book("strangle_weekly", "strangle", {"delta": 0.10}, Entry("weekly", 5, 12, weekday=1), Exit(2, loss_budget_credit_mult=2.0), "balance_premium", 0.7, 3,
                            costs=COSTS, note="Tuesday entry for the following week, 10-delta, balanced at 70 %, out two days before expiry"),
    "strangle_weekly_hedged": Book("strangle_weekly_hedged", "strangle_hedged", {"delta": 0.10, "wing_delta": 0.03}, Entry("weekly", 5, 12, weekday=1), Exit(2, loss_budget_credit_mult=2.0),
                                   "balance_premium", 0.7, 3, costs=COSTS, note="the weekly strangle with 3-delta wings"),
}

#: the portfolio the risk report is drawn for: the owner's ₹20 lakh, the shares a first allocation would give
PORTFOLIO = [
    {"name": "batman_winged", "structure": "batman_winged", "share": 0.20},
    {"name": "condor_to_fly", "structure": "iron_condor", "share": 0.15},
    {"name": "strangle_weekly_hedged", "structure": "strangle_hedged", "share": 0.15},
    {"name": "batman_hold", "structure": "batman", "share": 0.10},
    {"name": "equity_book", "structure": "future", "share": 0.20},
    {"name": "reserve", "structure": "future", "share": 0.20},
]


def paths(settings: Settings) -> tuple[Store, Path, Path]:
    return Store(settings.data_dir / "oi_daily"), settings.data_dir / "paper", settings.data_dir / "regime"


def regime_pct(store: Store, rroot: Path) -> dict[str, float]:
    """day → the monthly ATM IV's percentile against the trailing year, for the books that filter on it."""
    H = regime.with_percentiles(regime.history(store, rroot))
    if H.empty or "iv_m_pct" not in H:
        return {}
    return {str(r.day): float(r.iv_m_pct) for r in H.itertuples() if r.iv_m_pct == r.iv_m_pct}


def load_state(root: Path, book: Book) -> dict[str, Any]:
    p = root / f"{book.name}.json"
    return json.loads(p.read_text()) if p.exists() else new_state(book)


def save_state(root: Path, book: Book, state: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{book.name}.json.tmp"
    tmp.write_text(json.dumps({"rules": book.to_json(), **state}, default=str))
    tmp.replace(root / f"{book.name}.json")


def advance(store: Store, root: Path, books: dict[str, Book] | None = None, *, upto: str | None = None, start: str | None = None,
            regime_by_day: dict[str, float] | None = None) -> dict[str, list[str]]:
    """Step every book over the bhavcopy days it has not seen (from ``start`` for a fresh book)."""
    out: dict[str, list[str]] = {}
    if regime_by_day is None and any(b.min_iv_pct is not None for b in (books or BOOKS).values()):
        regime_by_day = regime_pct(store, root.parent / "regime")
    for book in (books or BOOKS).values():
        state = load_state(root, book)
        if "rules" in state:
            state.pop("rules")
        last = state.get("last_day")
        events: list[str] = []
        for day in store.days:
            if (last and day <= last) or (not last and start and day < start) or (upto and day > upto):
                continue
            events += step(state, store, day, book, regime_by_day)
        save_state(root, book, state)
        out[book.name] = events
    return out


def run_backtests(store: Store, start: str | None, end: str | None, books: dict[str, Book] | None = None, regime_by_day: dict[str, float] | None = None) -> pd.DataFrame:
    rows = []
    results: dict[str, dict[str, Any]] = {}
    for book in (books or BOOKS).values():
        st = backtest(store, book, start, end, regime_by_day)
        results[book.name] = st
        s = summary(st, book.costs.lot)
        rows.append({"book": book.name, **s})
    D = pd.DataFrame(rows).set_index("book")
    D.attrs["states"] = results
    return D


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--advance", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--regime", action="store_true")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None, help="write the backtest's cycle lists here (one csv a book)")
    ap.add_argument("--capital", type=float, default=2_000_000.0)
    a = ap.parse_args()
    settings = Settings(engine_enabled=False)
    store, proot, rroot = paths(settings)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    if a.backtest:
        D = run_backtests(store, a.start, a.end, regime_by_day=regime_pct(store, rroot))
        print(f"== backtest {a.start or store.days[0]} → {a.end or store.days[-1]} on {store.symbol} bhavcopy closes; points per unit, ₹ at lot {COSTS.lot} after slippage and charges")
        print(D.to_string())
        if a.out:
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            for name, st in D.attrs["states"].items():
                pd.DataFrame(st["cycles"]).to_csv(out / f"cycles-{name}.csv", index=False)
            D.to_csv(out / "summary.csv")
        cycles = {name: st["cycles"] for name, st in D.attrs["states"].items()}
        rep = risk.report(a.capital, PORTFOLIO, cycles)
        print(f"\n== risk report on ₹{a.capital:,.0f} (margins are estimates; p99 drawdowns from a 10,000-path bootstrap of the recorded cycles)")
        print(pd.DataFrame(rep["books"]).to_string(index=False))
        print(f"   sum of p99 drawdowns {rep['sum_p99_dd_inr']:+,} = {rep['sum_p99_dd_pct']} % of capital — {rep['note']}")
    if a.advance:
        ev = advance(store, proot, start=a.start, upto=a.end)
        for name, events in ev.items():
            print(f"== {name}: {len(events)} events")
            for e in events[-6:]:
                print("  ", e)
            st = load_state(proot, BOOKS[name])
            print("   summary:", summary(st))
    if a.report:
        cycles = {name: load_state(proot, b).get("cycles", []) for name, b in BOOKS.items()}
        rep = risk.report(a.capital, PORTFOLIO, cycles)
        print(pd.DataFrame(rep["books"]).to_string(index=False))
        print(f"   sum of p99 drawdowns {rep['sum_p99_dd_inr']:+,} = {rep['sum_p99_dd_pct']} % of capital")
    if a.regime:
        r = regime.latest(store, rroot)
        print(json.dumps(r, indent=1, default=str))


if __name__ == "__main__":
    main()
