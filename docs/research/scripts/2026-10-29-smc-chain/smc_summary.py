import sys
import numpy as np, pandas as pd
COST = 15.0
d = pd.read_parquet(sys.argv[1] + "/smc.parquet")
d["year"] = d["day"].str[:4]
def stat(x, days):
    x = x.dropna(); days = days[x.index]
    if len(x) == 0: return "n=0"
    net = x - COST; dm = net.groupby(days).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 and dm.std() > 0 else float("nan")
    return f"n={len(x):6d} gross={x.mean():7.1f} net={net.mean():7.1f} win%={100*(x>0).mean():4.0f} t_day={t:6.2f}"
print("== order-block first-return fills:", len(d), "days", d["day"].nunique(), "| sides", d["side"].value_counts().to_dict())
print("   risk median bps", round(d["risk_bps"].median(), 1), "| room_R median", round(d["room_R"].median(), 2), "| bars to retest median", d["bars_to_retest"].median())
gates = {
 "(a) all zones": pd.Series(True, index=d.index),
 "(b) sweep before": d["swept_before"],
 "(c) 1H bias agrees": d["h1_agree"],
 "(d) discount/premium": d["discount"],
 "(e) MTF pivot confluence": d["confluence"],
 "(f) FVG impulse": d["impulse"] == "GAP",
 "(g) entry <= 14:30": ~d["late"],
 "(b)+(c)": d["swept_before"] & d["h1_agree"],
 "(b)+(c)+(d)": d["swept_before"] & d["h1_agree"] & d["discount"],
 "(b)+(c)+(d)+(e)": d["swept_before"] & d["h1_agree"] & d["discount"] & d["confluence"],
 "(b)+(c)+(d)+(e)+(f)+(g)": d["swept_before"] & d["h1_agree"] & d["discount"] & d["confluence"] & (d["impulse"] == "GAP") & ~d["late"],
 "(c)+(d)+(g)": d["h1_agree"] & d["discount"] & ~d["late"],
 "(c)+(e)+(g)": d["h1_agree"] & d["confluence"] & ~d["late"],
 "room >= 1R": d["room_R"] >= 1,
 "(b)+(c)+(d) & room>=1R": d["swept_before"] & d["h1_agree"] & d["discount"] & (d["room_R"] >= 1),
}
for rule in ("wall_then_trail", "fixed_1to2"):
    print(f"\n-- exit {rule}")
    for name, m in gates.items():
        print(f"  {name:28s}", stat(d.loc[m, rule], d["day"]), "| 2024:", stat(d.loc[m & (d.year=='2024'), rule], d["day"]).split("net=")[1][:7], "2025:", stat(d.loc[m & (d.year=='2025'), rule], d["day"]).split("net=")[1][:7])
    for sd, nm in ((1, "long"), (-1, "short")):
        m = (d["side"] == sd) & d["swept_before"] & d["h1_agree"] & d["discount"]
        print(f"  (b)+(c)+(d) {nm:5s}             ", stat(d.loc[m, rule], d["day"]))
