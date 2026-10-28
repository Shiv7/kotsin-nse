import sys
import numpy as np, pandas as pd
COST = 15.0
d = pd.read_parquet(sys.argv[1] + "/instances.parquet")
ib = pd.read_parquet(sys.argv[1] + "/ib15.parquet")
full = len(sys.argv) > 2 and sys.argv[2] == "full"

def stat(x: pd.Series, days: pd.Series) -> str:
    x = x.dropna(); days = days[x.index]
    if len(x) == 0: return "n=0"
    net = x - COST
    dm = net.groupby(days).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 and dm.std() > 0 else float("nan")
    return f"n={len(x):5d} gross={x.mean():7.1f} net={net.mean():7.1f} win%={100*(x>0).mean():4.0f} t_day={t:5.2f}"

print("== held-retest instances:", len(d), "days", d['day'].nunique())
print("base      (live entry)     ", stat(d["base_pnl"], d["day"]))
print("brk_bar   (through-bar close, stop = bar)  ", stat(d["brk_bar_pnl"], d["day"]))
print("brk_cpr   (through-bar close, stop = far CPR edge)", stat(d["brk_cpr_pnl"], d["day"]))
print("stop_in   (stop order at the retest bar's extreme)", stat(d["stop_in_pnl"], d["day"]))
print("-- filters on the base entry (skip rules read out of the labels)")
flt = {
 "room >= 1R": d["room_R"] >= 1,
 "room >= 1.5R": d["room_R"] >= 1.5,
 "not late (<= 14:30)": ~d["late"],
 "wall 1 not touched before (trend not gone)": ~d["trend_gone"],
 "entry bar range <= 2 tol (no big bar)": d["bar_range_tol"] <= 2,
 "excursion beyond edge >= 2 tol (real impulse)": d["exc_tol"] >= 2,
 "retest >= 2 bars after through (not a pause inside)": d["gap_rt"] >= 2,
 "rejections at edge >= 2 before through": d["rej_n"] >= 2,
 "rejections >= 1": d["rej_n"] >= 1,
}
for k, m in flt.items():
    print(f"  {k:52s}", stat(d.loc[m, "base_pnl"], d["day"]), "| excluded:", stat(d.loc[~m, "base_pnl"], d["day"]))
allf = (d["room_R"] >= 1) & ~d["late"] & ~d["trend_gone"] & (d["bar_range_tol"] <= 2) & (d["exc_tol"] >= 2)
print(f"  {'ALL FIVE (room, time, trend, bar, impulse)':52s}", stat(d.loc[allf, "base_pnl"], d["day"]))
print("-- the through-bar entry with the rejection precondition")
for k, m in {"brk_bar, rej>=2": d["rej_n"] >= 2, "brk_bar, rej>=2 & room>=1R": (d["rej_n"] >= 2) & (d["brk_bar_room"] >= 1), "brk_bar, room>=1R": d["brk_bar_room"] >= 1,
             "brk_cpr, rej>=2": d["rej_n"] >= 2, "brk_cpr, room>=1R": d["brk_cpr_room"] >= 1}.items():
    col = "brk_bar_pnl" if k.startswith("brk_bar") else "brk_cpr_pnl"
    print(f"  {k:52s}", stat(d.loc[m, col], d["day"]))
print("== IB15 break (first close beyond the first 15 minutes), all symbol-days:", len(ib))
print("  all                 ", stat(ib["pnl"], ib["day"]))
print("  room >= 1R          ", stat(ib.loc[ib["room_R"] >= 1, "pnl"], ib["day"]))
print("  room >= 1R, not late", stat(ib.loc[(ib["room_R"] >= 1) & ~ib["late"], "pnl"], ib["day"]))
print("  before 10:30        ", stat(ib.loc[ib["k"] <= 15, "pnl"], ib["day"]))
print("  before 10:30, room>=1R", stat(ib.loc[(ib["k"] <= 15) & (ib["room_R"] >= 1), "pnl"], ib["day"]))
print("  long / short        ", stat(ib.loc[ib["side"] > 0, "pnl"], ib["day"]), "|", stat(ib.loc[ib["side"] < 0, "pnl"], ib["day"]))
if not full:
    pd.set_option("display.width", 250)
    cols = ["day", "sym", "side", "brk_hm", "j_hm", "rej_n", "exc_tol", "room_R", "trend_gone", "bar_range_tol", "base_pnl", "base_how", "brk_bar_pnl", "brk_cpr_pnl", "stop_in_pnl"]
    print(d[cols].round(1).to_string(index=False))
