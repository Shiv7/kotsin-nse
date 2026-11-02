"""H3 (prereg addendum, 3 Nov 03:45): can a ranker trained on entry-time features pick two trades a day better than random?
Row-23 dev-window trades (first flips with the move): fit on one year, pick two a day in the other. Usage: ranker.py OUT_DIR"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

COST = 15.0
FEATS = ["sd", "conf", "risk_bps", "w1_bps", "room_R", "j", "gap_pct", "tol_bps", "w_cpr", "w_pivot", "w_pd", "w_weekly", "w_or", "w_round"]


def dayt(x: pd.Series) -> float:
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else float("nan")


def main() -> None:
    out = sys.argv[1]
    t = pd.read_parquet(f"{out}/trades.parquet")
    e = t[t.pnl.notna() & t.with_move].sort_values("j").groupby(["sym", "day", "side"]).head(1).copy()
    e["net"] = e.pnl - COST
    e["year"] = e.day.str[:4]
    e["sd"] = e.side
    e["room_R"] = e.w1_bps / e.risk_bps
    for w in ("cpr", "pivot", "pd", "weekly", "or", "round"):
        e[f"w_{w}"] = (e.wtype == w).astype(int)
    e["y"] = (e.net > 0).astype(int)
    rng = np.random.default_rng(23)
    print(f"instances {len(e):,} days {e.day.nunique()} | all-trades mean net {e.net.mean():+.1f} bps")
    for train_y, test_y in (("2024", "2025"), ("2025", "2024")):
        tr, te = e[e.year == train_y], e[e.year == test_y].copy()
        m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200, random_state=7)
        m.fit(tr[FEATS], tr.y)
        te["p"] = m.predict_proba(te[FEATS])[:, 1]
        top2 = te.sort_values("p", ascending=False).groupby("day").head(2)
        per_day = top2.groupby("day").net.sum()
        rand = []
        for _ in range(200):
            idx = te.groupby("day").sample(n=2, replace=True, random_state=int(rng.integers(0, 10**6))).groupby("day").net.sum()
            rand.append(idx)
        R = pd.concat(rand, axis=1)
        rand_mean = R.mean(axis=1)
        diff = per_day - rand_mean
        dec = te.assign(d=pd.qcut(te.p, 10, labels=False, duplicates="drop")).groupby("d").net.agg(["size", "mean"])
        print(f"\nfit {train_y} → pick in {test_y}: {te.day.nunique()} days, {len(te):,} candidates (mean {te.p.mean():.3f} predicted win)")
        print(f"  top two a day: mean net {per_day.mean():+.1f} bps/day (day-t {dayt(per_day):+.2f}), win days {100 * (per_day > 0).mean():.0f} %")
        print(f"  random two a day: mean net {rand_mean.mean():+.1f} bps/day | top-two − random {diff.mean():+.1f} bps/day, day-t {dayt(diff):+.2f}")
        print(f"  all trades in {test_y}: mean net {te.net.mean():+.1f} bps a trade | top decile by score {dec['mean'].iloc[-1]:+.1f} on {int(dec['size'].iloc[-1]):,}, bottom decile {dec['mean'].iloc[0]:+.1f}")
        imp = pd.Series(m.feature_importances_ if hasattr(m, "feature_importances_") else np.nan, index=FEATS) if hasattr(m, "feature_importances_") else None
        if imp is not None:
            print("  importances:", imp.sort_values(ascending=False).round(3).head(6).to_dict())
        top = te.sort_values("p", ascending=False).head(3000)
        print(f"  what the model likes (top 3,000 by score): risk median {top.risk_bps.median():.0f} bps (all {te.risk_bps.median():.0f}), room {top.room_R.median():.2f}R (all {te.room_R.median():.2f}), "
              f"clock bar median {top.j.median():.0f} (all {te.j.median():.0f}), long share {100 * (top.sd > 0).mean():.0f} % (all {100 * (te.sd > 0).mean():.0f}), walls {top.wtype.value_counts(normalize=True).round(2).head(3).to_dict()}")


if __name__ == "__main__":
    main()
