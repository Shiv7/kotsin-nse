import sys, glob
from datetime import date
import numpy as np, pandas as pd
COST = 15.0
EPOCH_ORD = date(1970, 1, 1).toordinal()
ORDER = ["5m", "15m", "30m", "1h", "4h", "1d"]
CONCEPTS = ["level_retest", "through_bar", "range_break", "order_block", "sweep_reversal"]
frames = []
for tf in ORDER:
    try:
        d = pd.read_parquet(f"{sys.argv[1]}/mtf_{tf}.parquet"); frames.append(d)
    except Exception:
        pass
d = pd.concat(frames, ignore_index=True)
d["eday"] = ((d["entry_ts"] + 19800) // 86400).astype(int)
d["year"] = d["eday"].map(lambda x: date.fromordinal(EPOCH_ORD + x).year)
def stat(x, days):
    x = x.dropna(); days = days[x.index]
    if len(x) == 0: return None
    net = x - COST; dm = net.groupby(days).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 and dm.std() > 0 else float("nan")
    return {"n": len(x), "gross": x.mean(), "net": net.mean(), "win": 100 * (x > 0).mean(), "t_day": t}
rows = []
for tf in ORDER:
    for c in CONCEPTS:
        m = (d["tf"] == tf) & (d["concept"] == c)
        if not m.any(): continue
        for rule in ("wall_then_trail", "fixed_1to2"):
            s = stat(d.loc[m, rule], d["eday"])
            h1 = stat(d.loc[m & (d.year == 2024), rule], d["eday"]); h2 = stat(d.loc[m & (d.year == 2025), rule], d["eday"])
            rows.append({"tf": tf, "concept": c, "exit": rule, **s, "risk_med": d.loc[m, "risk_bps"].median(), "net_2024": h1["net"] if h1 else np.nan, "net_2025": h2["net"] if h2 else np.nan,
                         "long_net": (d.loc[m & (d.side > 0), rule].mean() - COST), "short_net": (d.loc[m & (d.side < 0), rule].mean() - COST)})
t = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print(t.round(1).to_string(index=False))
t.to_csv(f"{sys.argv[1]}/mtf_results.csv", index=False)
print("\n== cells passing the bar (net > 0, t >= 2.5, n >= 300, both halves > 0):")
ok = t[(t.net > 0) & (t.t_day >= 2.5) & (t.n >= 300) & (t.net_2024 > 0) & (t.net_2025 > 0)]
print(ok.round(1).to_string(index=False) if len(ok) else "  none")
