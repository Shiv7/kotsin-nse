import sys
import numpy as np, pandas as pd
COST = 15.0
d = pd.read_parquet(sys.argv[1] + "/instances.parquet")
def stat(x, days):
    x = x.dropna(); days = days[x.index]
    if len(x) == 0: return "n=0"
    net = x - COST; dm = net.groupby(days).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 and dm.std() > 0 else float("nan")
    return f"n={len(x):5d} gross={x.mean():7.1f} net={net.mean():7.1f} win%={100*(x>0).mean():4.0f} t_day={t:5.2f}"
print("== sides that reached the through bar:", len(d), "| by stage:", d["stage"].value_counts().to_dict())
conf = d["stage"] == "confirmed"; held = d["stage"].isin(["retest_held", "confirmed"])
print("base (live entry, confirmed only)              ", stat(d.loc[conf, "base_pnl"], d["day"]))
print("-- through-bar entry, scored on EVERY through bar (what a trader at that bar can take)")
for name, m in {"brk_bar all": d["brk_bar_pnl"].notna(), "brk_bar rej>=2": d["rej_n"] >= 2, "brk_bar rej>=2 room>=1R": (d["rej_n"] >= 2) & (d["brk_bar_room"] >= 1),
                "brk_bar room>=1R": d["brk_bar_room"] >= 1, "brk_bar not late": ~d["late_brk"], "brk_bar range<=2tol": d["brk_range_tol"] <= 2,
                "brk_bar rej>=2 & room>=1R & not late & range<=2tol": (d["rej_n"] >= 2) & (d["brk_bar_room"] >= 1) & ~d["late_brk"] & (d["brk_range_tol"] <= 2)}.items():
    print(f"  {name:52s}", stat(d.loc[m, "brk_bar_pnl"], d["day"]))
for name, m in {"brk_cpr all": d["brk_cpr_pnl"].notna(), "brk_cpr rej>=2": d["rej_n"] >= 2, "brk_cpr room>=1R": d["brk_cpr_room"] >= 1,
                "brk_cpr rej>=2 & room>=1R & not late": (d["rej_n"] >= 2) & (d["brk_cpr_room"] >= 1) & ~d["late_brk"]}.items():
    print(f"  {name:52s}", stat(d.loc[m, "brk_cpr_pnl"], d["day"]))
print("-- the same entry on the confirmed subset only (the look-ahead number, for the record)")
print(f"  {'brk_bar, confirmed only':52s}", stat(d.loc[conf, "brk_bar_pnl"], d["day"]))
print(f"  {'brk_cpr, confirmed only':52s}", stat(d.loc[conf, "brk_cpr_pnl"], d["day"]))
print("-- what happened after the through bar, by what came next")
for stg in ["through", "retest_failed", "retest_held", "confirmed"]:
    m = d["stage"] == stg
    print(f"  {stg:16s} brk_bar", stat(d.loc[m, "brk_bar_pnl"], d["day"]), "| brk_cpr", stat(d.loc[m, "brk_cpr_pnl"], d["day"]))
print("-- stop-order entry at the retest extreme, scored on EVERY held retest (filled or not within 6 bars)")
filled = held & (d["stop_in_k"] >= 0)
print(f"  {'held retests':52s} n_held={int(held.sum())} filled={int(filled.sum())}")
print(f"  {'stop_in, all fills':52s}", stat(d.loc[filled, "stop_in_pnl"], d["day"]))
print(f"  {'stop_in, confirmed only (look-ahead)':52s}", stat(d.loc[conf, "stop_in_pnl"], d["day"]))
print(f"  {'stop_in, filled but never confirmed':52s}", stat(d.loc[filled & ~conf, "stop_in_pnl"], d["day"]))
