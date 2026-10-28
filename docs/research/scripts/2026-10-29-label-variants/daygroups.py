"""Day groups from NIFTY's previous day and first hour against fixed levels; the algo per group learned on the first
80 % of days (a contextual bandit over our algos), tested frozen on the last 20 %. See the pre-registration."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

from kotsin_nse.analysis.dayread import TOL_ADR, bands, cpr, level_events
from kotsin_nse.instrument.universe import INDEX_ROOTS
from kotsin_nse.structure.engine import Bar5

DATA = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
OUT = Path(sys.argv[1])
START, END = date(2024, 1, 1), date(2025, 9, 30)
EPOCH_ORD = date(1970, 1, 1).toordinal()
OPEN_S, CLOSE_S = 33300, 55800
FH = 12
COST_STOCK, COST_NIFTY = 15.0, 5.0
K = 5
MIN_TRAIN_DAYS, MIN_T = 20, 2.0


def load_5m(sym: str) -> dict[date, dict[str, np.ndarray]]:
    df = pd.read_parquet(DATA / "history" / "5m" / f"{sym}.parquet").drop_duplicates("ts", keep="last").sort_values("ts")
    ts = df["ts"].to_numpy(dtype=np.int64)
    ist = ts + 19800
    dayn, secs = ist // 86400, ist % 86400
    keep = (secs >= OPEN_S) & (secs < CLOSE_S)
    df, ts, dayn = df[keep], ts[keep], dayn[keep]
    arr = {k: df[k].to_numpy(dtype=float) for k in ("o", "h", "l", "c")}
    out = {}
    uniq, starts, counts = np.unique(dayn, return_index=True, return_counts=True)
    for dn, s, cnt in zip(uniq, starts, counts, strict=True):
        d = date.fromordinal(EPOCH_ORD + int(dn))
        if START <= d <= END and cnt >= 70:
            out[d] = {k: v[s:s + cnt] for k, v in arr.items()} | {"ts": ts[s:s + cnt]}
    return out


def load_1d(sym: str) -> pd.DataFrame:
    d = pd.read_parquet(DATA / "history" / "1d" / f"{sym}.parquet").drop_duplicates("ts", keep="last").sort_values("ts")
    d = d[(d["h"] > 0) & (d["l"] > 0)].copy()
    d["day"] = ((d["ts"] + 19800) // 86400).astype(int).map(lambda x: date.fromordinal(EPOCH_ORD + x))
    return d.drop_duplicates("day", keep="last").reset_index(drop=True)


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / x.std() * np.sqrt(len(x))) if len(x) > 2 and x.std() > 0 else float("nan")


# ---------------------------------------------------------------- NIFTY context at 10:15
n1, n5 = load_1d("NIFTY"), load_5m("NIFTY")
rows = []
for day, b in n5.items():
    prev = n1[n1["day"] < day]
    if len(prev) < 25:
        continue
    p, pp = prev.iloc[-1], prev.iloc[-2]
    adr = float((prev["h"] - prev["l"]).tail(20).mean())
    lv = cpr(float(p["h"]), float(p["l"]), float(p["c"]))
    lvp = cpr(float(pp["h"]), float(pp["l"]), float(pp["c"]))
    tol = adr * TOL_ADR
    o, h, l, c = b["o"], b["h"], b["l"], b["c"]
    o0, c11 = o[0], c[FH - 1]
    ibh, ibl = h[:FH].max(), l[:FH].min()
    pdh, pdl = lv["PDH"], lv["PDL"]
    rng = max(pdh - pdl, 1e-9)
    fib = {k: pdl + f * rng for k, f in (("f382", 0.382), ("f500", 0.5), ("f618", 0.618))}
    wk = prev.copy()
    wk["wk"] = wk["day"].map(lambda x: x.isocalendar()[:2])
    done = [w for w in wk["wk"].unique() if w < day.isocalendar()[:2]]
    g = wk[wk["wk"] == max(done)] if done else None
    wp = (g["h"].max() + g["l"].min() + g["c"].iloc[-1]) / 3 if g is not None else lv["P"]
    def loc(px: float) -> int:
        return 1 if px > lv["TC"] else (-1 if px < lv["BC"] else 0)
    bars = [Bar5(int(b["ts"][i]), o[i], h[i], l[i], c[i], 0.0) for i in range(FH)]
    ev = level_events(bars, bands(lv), tol)
    rows.append({
        "day": day, "adr": adr,
        "prev_ret_adr": (p["c"] - pp["c"]) / adr, "prev_loc": (p["c"] - p["l"]) / max(p["h"] - p["l"], 1e-9), "prev_range_adr": (p["h"] - p["l"]) / adr,
        "ret5_adr": (p["c"] - prev["c"].iloc[-6]) / adr, "prev_vs_cpr": 1 if p["c"] > lvp["TC"] else (-1 if p["c"] < lvp["BC"] else 0),
        "gap_adr": (o0 - p["c"]) / adr, "open_loc": loc(o0), "open_vs_pdhl": 1 if o0 > pdh else (-1 if o0 < pdl else 0),
        "ib_range_adr": (ibh - ibl) / adr, "fh_move_adr": (c11 - o0) / adr, "fh_loc": (c11 - ibl) / max(ibh - ibl, 1e-9),
        "ib_broke_up": int(ibh >= min(pdh, lv["R1"])), "ib_broke_dn": int(ibl <= max(pdl, lv["S1"])), "gap_filled": int(ibl <= p["c"] <= ibh),
        "c11_loc": loc(c11), "fib_pos": (c11 - pdl) / rng, "c11_vs_wp_adr": (c11 - wp) / adr, "cpr_w_adr": (lv["TC"] - lv["BC"]) / adr,
        "n_support": sum(e.kind == "SUPPORT" for e in ev), "n_resist": sum(e.kind == "RESISTANCE" for e in ev), "n_break": sum(e.kind.startswith("BREAK") for e in ev),
        "c11": c11, "c_last": c[-1], "fh_up": int((c11 - o0) / adr >= 0.25), "fh_dn": int((c11 - o0) / adr <= -0.25),
    })
ctx = pd.DataFrame(rows).sort_values("day").reset_index(drop=True)
FEATS = ["prev_ret_adr", "prev_loc", "prev_range_adr", "ret5_adr", "prev_vs_cpr", "gap_adr", "open_loc", "open_vs_pdhl", "ib_range_adr", "fh_move_adr",
         "fh_loc", "ib_broke_up", "ib_broke_dn", "gap_filled", "c11_loc", "fib_pos", "c11_vs_wp_adr", "cpr_w_adr", "n_support", "n_resist", "n_break", "breadth_up"]

# ---------------------------------------------------------------- the stocks at 10:15: breadth and the movers basket
syms = sorted(p.stem for p in (DATA / "history" / "5m").glob("*.parquet") if p.stem not in INDEX_ROOTS and p.stem != "CRUDEOIL")
mv: dict[date, list[tuple[float, float]]] = {}
for sym in syms:
    d1 = load_1d(sym).set_index("day")["c"]
    for day, b in load_5m(sym).items():
        pv = d1[d1.index < day]
        if pv.empty or b["c"][FH - 1] <= 0:
            continue
        pdc = float(pv.iloc[-1])
        mv.setdefault(day, []).append(((b["c"][FH - 1] / pdc - 1) * 1e4, (b["c"][-1] / b["c"][FH - 1] - 1) * 1e4))
mrows = []
for day, xs in mv.items():
    a = np.array(xs)
    if len(a) < 30:
        continue
    k = max(3, len(a) // 10)
    order = np.argsort(a[:, 0])
    mrows.append({"day": day, "breadth_up": float((a[:, 0] > 0).mean()), "mom_long": float(a[order[-k:], 1].mean()) - COST_STOCK, "mom_short": float(-a[order[:k], 1].mean()) - COST_STOCK})
ctx = ctx.merge(pd.DataFrame(mrows), on="day", how="inner")

# ---------------------------------------------------------------- rewards per day per action
inst = pd.read_parquet(OUT / "dev" / "instances.parquet")
inst["day"] = pd.to_datetime(inst["day"]).dt.date
ib = pd.read_parquet(OUT / "dev" / "ib15.parquet")
ib["day"] = pd.to_datetime(ib["day"]).dt.date
R = pd.DataFrame({"day": ctx["day"]}).set_index("day")
for name, df, col in (("retest", inst[inst["stage"] == "confirmed"], "base_pnl"), ("through", inst, "brk_bar_pnl"), ("ib15", ib, "pnl")):
    for side, tag in ((1, "L"), (-1, "S")):
        s = df[df["side"] == side].groupby("day")[col].mean() - COST_STOCK
        R[f"{name}_{tag}"] = s
R["mom_L"], R["mom_S"] = ctx.set_index("day")["mom_long"], ctx.set_index("day")["mom_short"]
sgn = np.sign(ctx.set_index("day")["fh_move_adr"])
nmove = (ctx.set_index("day")["c_last"] / ctx.set_index("day")["c11"] - 1) * 1e4
R["nifty_cont"] = (sgn * nmove - COST_NIFTY).where(sgn != 0)
R["nifty_fade"] = (-sgn * nmove - COST_NIFTY).where(sgn != 0)
R["flat"] = 0.0
ACTIONS = list(R.columns)

# ---------------------------------------------------------------- learn on 80 %, test on 20 %
days = list(ctx["day"])
cut = int(len(days) * 0.8)


def run(train_days: list[date], test_days: list[date], label: str, grouping: str) -> dict:
    tr = ctx[ctx["day"].isin(train_days)]
    if grouping == "kmeans":
        sc = StandardScaler().fit(tr[FEATS])
        km = KMeans(n_clusters=K, n_init=10, random_state=20261029).fit(sc.transform(tr[FEATS]))
        grp = pd.Series(km.predict(sc.transform(ctx[FEATS])), index=ctx["day"])
    else:
        grp = pd.Series([f"open{'+' if r.open_loc > 0 else '-' if r.open_loc < 0 else '0'}/fh{'+' if r.fh_up else '-' if r.fh_dn else '0'}" for r in ctx.itertuples()], index=ctx["day"])
    policy, table = {}, []
    for g in sorted(grp.unique()):
        gdays_tr = [d for d in train_days if grp[d] == g]
        gdays_te = [d for d in test_days if grp[d] == g]
        best, best_v = "flat", 0.0
        for a in ACTIONS:
            r_tr = R.loc[gdays_tr, a].dropna()
            r_te = R.loc[gdays_te, a].dropna()
            t_tr = tstat(r_tr)
            table.append({"split": label, "grouping": grouping, "group": str(g), "action": a, "train_days": len(gdays_tr), "train_traded": len(r_tr), "train_net": r_tr.mean() if len(r_tr) else np.nan,
                          "train_t": t_tr, "test_days": len(gdays_te), "test_traded": len(r_te), "test_net": r_te.mean() if len(r_te) else np.nan, "test_t": tstat(r_te)})
            if a != "flat" and len(r_tr) >= MIN_TRAIN_DAYS and t_tr >= MIN_T and r_tr.mean() > best_v:
                best, best_v = a, float(r_tr.mean())
        policy[str(g)] = best
    # the frozen policy on the test days: reward of the chosen action, 0 when it has no trade that day
    real = pd.Series([R.loc[d, policy[str(grp[d])]] if policy[str(grp[d])] != "flat" else 0.0 for d in test_days], index=test_days).fillna(0.0)
    traded = [d for d in test_days if policy[str(grp[d])] != "flat"]
    singles = {a: R.loc[test_days, a].fillna(0.0) for a in ACTIONS if a != "flat"}
    best_single = max(singles, key=lambda a: singles[a].mean())
    res = {"split": label, "grouping": grouping, "policy": policy, "groups_train": {str(g): int(sum(grp[d] == g for d in train_days)) for g in sorted(grp.unique())},
           "test_days": len(test_days), "test_days_traded": len(traded), "policy_test_mean_per_day": float(real.mean()), "policy_test_t": tstat(real),
           "policy_test_mean_on_traded_days": float(real[traded].mean()) if traded else 0.0,
           "best_single_on_test": best_single, "best_single_test_mean": float(singles[best_single].mean()), "best_single_test_t": tstat(singles[best_single]),
           "train_window": f"{train_days[0]} → {train_days[-1]}", "test_window": f"{test_days[0]} → {test_days[-1]}"}
    if grouping == "kmeans":
        prof = ctx.assign(g=grp.values).groupby("g")[["gap_adr", "open_loc", "fh_move_adr", "ib_range_adr", "ib_broke_up", "ib_broke_dn", "c11_loc", "fib_pos", "breadth_up", "prev_ret_adr"]].mean().round(2)
        res["profiles"] = prof.to_dict(orient="index")
    return res, pd.DataFrame(table)


results, tables = [], []
for label, trd, ted in (("forward", days[:cut], days[cut:]), ("reverse", days[-cut:], days[:-cut])):
    for grouping in ("kmeans", "rules"):
        r, t = run(trd, ted, label, grouping)
        results.append(r)
        tables.append(t)
tab = pd.concat(tables)
tab.to_parquet(OUT / "daygroups_table.parquet", index=False)
ctx.to_parquet(OUT / "daygroups_ctx.parquet", index=False)
R.reset_index().to_parquet(OUT / "daygroups_rewards.parquet", index=False)
json.dump(results, open(OUT / "daygroups_results.json", "w"), indent=1, default=str)
print("days", len(days), "train", cut, "test", len(days) - cut)
print("== single actions over all days (per day with a trade): ")
for a in ACTIONS:
    x = R[a].dropna()
    print(f"  {a:12s} traded_days={len(x):4d} net/day={x.mean():7.1f} t={tstat(x):5.2f}")
for r in results:
    print(f"\n== {r['split']} / {r['grouping']}: train {r['train_window']} test {r['test_window']}")
    print("  policy:", r["policy"], "| groups (train days):", r["groups_train"])
    print(f"  policy on test: {r['policy_test_mean_per_day']:+.1f} bps/day (t {r['policy_test_t']:.2f}), traded {r['test_days_traded']}/{r['test_days']} days, {r['policy_test_mean_on_traded_days']:+.1f} on traded days")
    print(f"  best single action on test: {r['best_single_on_test']} {r['best_single_test_mean']:+.1f}/day (t {r['best_single_test_t']:.2f})")
    if "profiles" in r:
        print("  group profiles:", json.dumps(r["profiles"]))
pd.set_option("display.width", 250)
fw = tab[(tab["split"] == "forward") & (tab["grouping"] == "kmeans")].copy()
print("\n== forward / kmeans: group × action (train net, t | test net, t)")
print(fw.round(1).to_string(index=False))
