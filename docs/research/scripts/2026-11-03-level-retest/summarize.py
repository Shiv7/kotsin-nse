"""Tables for docs/research/2026-11-03-level-retest.md from level_retest.py's parquet output."""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

COST = 15.0


def dayt(df: pd.DataFrame, col: str = "net") -> float:
    """t of the mean of daily means (day-clustered)."""
    d = df.groupby("day")[col].mean()
    return float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))) if len(d) > 2 and d.std(ddof=1) > 0 else float("nan")


def block(df: pd.DataFrame, label: str) -> str:
    if len(df) == 0:
        return f"| {label} | 0 | | | | | |"
    return (f"| {label} | {len(df):,} | {df.pnl.mean():+.1f} | {df.net.mean():+.1f} | {100 * (df.pnl > 0).mean():.1f} % | "
            f"{df.risk_bps.median():.0f} | {dayt(df):+.2f} |")


def main() -> None:
    out = sys.argv[1]
    t = pd.read_parquet(f"{out}/trades.parquet")
    c = pd.read_parquet(f"{out}/control.parquet")
    e = t[t.pnl.notna()].copy()
    e["net"] = e.pnl - COST
    e["year"] = e.day.str[:4]
    e["clock"] = pd.cut(e.j, [-1, 12, 24, 36, 48, 63], labels=["≤10:15", "10:20–11:15", "11:20–12:15", "12:20–13:15", "13:20–14:30"])
    c["net"] = c.pnl - COST
    c["year"] = c.day.str[:4]
    hdr = "| set | n | gross bps | net bps | win | median risk bps | day-t (net) |\n|---|---|---|---|---|---|---|"
    print(f"retest rows {len(t):,}: held {int(t.held.sum()):,} ({100 * t.held.mean():.0f} %), confirmed within 6 bars and before 14:30 {len(e):,}")
    print(f"stock-days with a confirmed flip {e.groupby(['sym', 'day']).ngroups:,}; symbols {e.sym.nunique()}; days {e.day.nunique()}")
    prim = e[e.with_move].sort_values("j").groupby(["sym", "day", "side"]).head(1)
    against = e[~e.with_move].sort_values("j").groupby(["sym", "day", "side"]).head(1)
    print("\n## Primary: with the day's move, the first confirmed flip per stock-day-side\n" + hdr)
    print(block(prim, "primary"))
    for y, g in prim.groupby("year"):
        print(block(g, f"primary {y}"))
    for s, g in prim.groupby("side"):
        print(block(g, "primary long" if s > 0 else "primary short"))
    print(block(against, "against the move (first per stock-day-side)"))
    print(block(e, "every confirmed flip"))
    print("\n## Primary by wall type\n" + hdr)
    for w, g in prim.groupby("wtype"):
        print(block(g, w))
    print(block(prim[prim.conf >= 1], "confluence (another wall within tol)"))
    print(block(prim[prim.conf == 0], "single wall"))
    print("\n## Primary by entry clock\n" + hdr)
    for k, g in prim.groupby("clock", observed=True):
        print(block(g, str(k)))
    print("\n## Gap-and-go (gap ≥ +2 %, longs with the move) and its mirror\n" + hdr)
    print(block(prim[(prim.gap_pct >= 2) & (prim.side > 0)], "gap ≥ +2 %, long"))
    print(block(prim[(prim.gap_pct <= -2) & (prim.side < 0)], "gap ≤ −2 %, short"))
    print(block(prim[(prim.gap_pct.abs() < 1)], "|gap| < 1 %"))
    print("\n## Exits (primary)")
    print(prim.how.value_counts(normalize=True).mul(100).round(1).to_dict())
    print("first wall:", prim.w1.value_counts(normalize=True).mul(100).round(1).head(8).to_dict())
    print("median first-wall distance bps", round(prim.w1_bps.median(), 1), "median bars held", prim.bars_held.median())
    print("\n## Control: enter at the break bar's close (no retest), first break per stock-day-side with the move\n" + hdr)
    cp = c[c.with_move].sort_values("i").groupby(["sym", "day", "side"]).head(1)
    print(block(cp, "control, with the move"))
    for y, g in cp.groupby("year"):
        print(block(g, f"control {y}"))
    for w, g in cp.groupby("wtype"):
        print(block(g, f"control {w}"))
    # where the retest helps most: by risk bucket
    print("\n## Primary by risk (stop distance)\n" + hdr)
    for k, g in prim.groupby(pd.cut(prim.risk_bps, [0, 30, 50, 80, 120, 1e9], labels=["≤30", "30–50", "50–80", "80–120", ">120"]), observed=True):
        print(block(g, f"risk {k}"))
    # trend-day hindsight split (for the record, not a filter): did the trade's day close in the top/bottom 15 % of its range?
    print("\n## Per-year, long vs short (primary)\n" + hdr)
    for (y, s), g in prim.groupby(["year", "side"]):
        print(block(g, f"{y} {'long' if s > 0 else 'short'}"))


if __name__ == "__main__":
    main()
