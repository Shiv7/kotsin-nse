"""H4: the price action at the entry — features from the dev cache's 5m bars for every row-23 first flip, the one-way
splits, and the ranker of H3 refit with them (leave-one-year-out). Usage: features.py OUT_DIR"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, str(Path(__file__).parent))
from level_retest import bars_by_day  # noqa: E402

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
COST = 15.0
GEOM = ["sd", "conf", "risk_bps", "w1_bps", "room_R", "j", "gap_pct", "tol_bps", "w_cpr", "w_pivot", "w_pd", "w_weekly", "w_or", "w_round"]
PA = ["mv_adr", "mv_pct_with", "xs_rank_with", "breadth_with", "nifty_with", "day_range_adr", "range_pos_with", "vol_brk", "rng_brk_tol", "vol_rt", "rng_rt_tol", "brk_to_rt", "rt_to_j"]


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
    days = sorted(e.day.unique())
    syms = sorted(e.sym.unique())
    # the cross-section: move from the open (%) per (day, bar) for every symbol — float32, 219 × 433 × 75
    nbar = 75
    mv = {d: np.full((len(syms), nbar), np.nan, dtype=np.float32) for d in days}
    si = {s: i for i, s in enumerate(syms)}
    feats: dict[tuple, dict] = {}
    want = e.groupby("sym")
    for n_s, (sym, g) in enumerate(want):
        p = DD / "history" / "5m" / f"{sym}.parquet"
        if not p.exists():
            continue
        bd = bars_by_day(pd.read_parquet(p))
        for d in set(g.day):
            dd = date.fromisoformat(d)
            bars = bd.get(dd)
            if not bars:
                continue
            o0 = bars[0].o
            c = np.array([b.c for b in bars]); h = np.array([b.h for b in bars]); lo = np.array([b.l for b in bars]); v = np.array([b.v for b in bars])
            m = (c / o0 - 1) * 100
            k_ = min(len(m), nbar)
            mv[d][si[sym], :k_] = m[:k_]
            for r in g[g.day == d].itertuples():
                j, ib, k = int(r.j), int(r.i_brk), int(r.k)
                if j >= len(c):
                    continue
                tol = r.tol_bps / 1e4 * r.entry
                hi, lw = h[: j + 1].max(), lo[: j + 1].min()
                feats[(sym, d, r.side, j)] = {
                    "mv_pct": m[j], "day_range_pct": (hi / lw - 1) * 100, "range_pos": (c[j] - lw) / (hi - lw) if hi > lw else 0.5,
                    "vol_brk": v[ib] / v[:ib].mean() if ib > 0 and v[:ib].mean() > 0 else np.nan, "rng_brk_tol": (h[ib] - lo[ib]) / tol if tol > 0 else np.nan,
                    "vol_rt": v[k] / v[:k].mean() if k > 0 and v[:k].mean() > 0 else np.nan, "rng_rt_tol": (h[k] - lo[k]) / tol if tol > 0 else np.nan,
                    "brk_to_rt": k - ib, "rt_to_j": j - k,
                }
        if n_s % 40 == 0:
            print(f"  {n_s}/{len(syms)} symbols", file=sys.stderr)
    nifty = {}
    pn = DD / "history" / "5m" / "NIFTY.parquet"
    if pn.exists():
        for dd, bars in bars_by_day(pd.read_parquet(pn)).items():
            nifty[dd.isoformat()] = np.array([(b.c / bars[0].o - 1) * 100 for b in bars])
    rows = []
    for r in e.itertuples():
        f = feats.get((r.sym, r.day, r.side, int(r.j)))
        if f is None:
            continue
        j = int(r.j)
        col = mv[r.day][:, j] if j < nbar else np.full(len(syms), np.nan)
        ok = ~np.isnan(col)
        mine = f["mv_pct"]
        xs = (col[ok] < mine).mean() if ok.sum() > 20 else np.nan  # percentile of the stock's move among the universe
        breadth = (col[ok] > 0).mean() if ok.sum() > 20 else np.nan
        nm = nifty.get(r.day)
        nv = nm[j] if nm is not None and j < len(nm) else np.nan
        adr_pct = r.tol_bps * 10 / 100
        rows.append({**{k: getattr(r, k) for k in ("sym", "day", "side", "year", "net", "pnl", *GEOM)}, **f,
                     "mv_adr": (mine * r.side) / adr_pct if adr_pct > 0 else np.nan, "mv_pct_with": mine * r.side,
                     "xs_rank_with": xs if r.side > 0 else 1 - xs, "breadth_with": breadth if r.side > 0 else 1 - breadth, "nifty_with": nv * r.side,
                     "day_range_adr": f["day_range_pct"] / adr_pct if adr_pct > 0 else np.nan, "range_pos_with": f["range_pos"] if r.side > 0 else 1 - f["range_pos"]})
    D = pd.DataFrame(rows)
    D.to_parquet(f"{out}/features.parquet")
    print(f"\ninstances with features {len(D):,} of {len(e):,} | all mean net {D.net.mean():+.1f} bps")

    def split(col, bins, label):
        g = D.groupby(pd.cut(D[col], bins), observed=True)
        o = g.net.agg(n="size", net="mean", win=lambda x: 100 * (x > 0).mean())
        for y in ("2024", "2025"):
            o[y] = D[D.year == y].groupby(pd.cut(D[D.year == y][col], bins), observed=True).net.mean()
        print(f"\n-- {label}")
        print(o.round(1).to_string())

    split("mv_adr", [-9, 0, 0.3, 0.6, 1.0, 1.5, 99], "the stock's move from the open at entry, the trade's way (ADR)")
    split("xs_rank_with", [-0.01, 0.2, 0.4, 0.6, 0.8, 0.9, 1.0], "rank of the stock's move among the universe at the entry bar, the trade's way (1 = leading)")
    split("breadth_with", [-0.01, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0], "breadth moving the trade's way at the entry bar")
    split("nifty_with", [-9, -0.5, -0.2, 0, 0.2, 0.5, 9], "NIFTY's move from its open at entry, the trade's way (%)")
    split("day_range_adr", [0, 0.3, 0.5, 0.8, 1.2, 9], "the day's range so far at entry (ADR)")
    split("range_pos_with", [-0.01, 0.5, 0.8, 0.95, 1.0], "where the entry close sits in the day's range, the trade's way (1 = at the extreme)")
    split("vol_brk", [0, 0.7, 1.0, 1.5, 2.5, 99], "break bar's volume vs the day's earlier bars")
    split("rng_brk_tol", [0, 1, 2, 3, 5, 99], "break bar's range in tolerances")
    split("vol_rt", [0, 0.5, 0.8, 1.2, 2, 99], "retest bar's volume vs the day's earlier bars")
    split("brk_to_rt", [-1, 1, 2, 4, 8, 99], "bars from the break to the retest")
    split("rt_to_j", [0, 1, 2, 3, 6], "bars from the retest to the confirmation")
    rng = np.random.default_rng(23)
    FE = GEOM + PA
    print("\n== H4 ranker (geometry + price action), leave-one-year-out")
    for train_y, test_y in (("2024", "2025"), ("2025", "2024")):
        tr, te = D[D.year == train_y], D[D.year == test_y].copy()
        m = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200, random_state=7)
        m.fit(tr[FE], (tr.net > 0).astype(int))
        te["p"] = m.predict_proba(te[FE])[:, 1]
        top2 = te.sort_values("p", ascending=False).groupby("day").head(2).groupby("day").net.sum()
        R = pd.concat([te.groupby("day").sample(n=2, replace=True, random_state=int(rng.integers(0, 10**6))).groupby("day").net.sum() for _ in range(200)], axis=1).mean(axis=1)
        diff = top2 - R
        dec = te.assign(d=pd.qcut(te.p, 10, labels=False, duplicates="drop")).groupby("d").net.agg(["size", "mean"])
        print(f"fit {train_y} → pick in {test_y}: top two a day {top2.mean():+.1f} bps/day (day-t {dayt(top2):+.2f}) | random {R.mean():+.1f} | top − random {diff.mean():+.1f} (day-t {dayt(diff):+.2f}) "
              f"| top decile by score {dec['mean'].iloc[-1]:+.1f} on {int(dec['size'].iloc[-1]):,}, bottom {dec['mean'].iloc[0]:+.1f}, all {te.net.mean():+.1f}")
        # a single-feature ranker per price-action feature: the top two a day by that feature alone (both directions)
        best = []
        for col in PA:
            for asc in (False, True):
                tt = te.dropna(subset=[col]).sort_values(col, ascending=asc).groupby("day").head(2).groupby("day").net.sum()
                best.append((f"{col} {'low' if asc else 'high'}", tt.mean(), dayt(tt)))
        best.sort(key=lambda x: -x[1])
        print("   top two a day by one feature:", [(n, round(v, 1), round(tv, 1)) for n, v, tv in best[:4]], "… worst", [(n, round(v, 1)) for n, v, _ in best[-2:]])


if __name__ == "__main__":
    main()
