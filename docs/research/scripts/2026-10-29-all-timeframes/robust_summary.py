import sys
from datetime import date
import numpy as np, pandas as pd
COST = 15.0
EPOCH_ORD = date(1970, 1, 1).toordinal()
frames = [pd.read_parquet(f"{sys.argv[1]}/mtf_{tf}.parquet") for tf in ("1h", "4h", "1d")]
d = pd.concat(frames, ignore_index=True)
d["eday"] = ((d["entry_ts"] + 19800) // 86400).astype(int)
d = d[(d["eday"] >= date(2024, 1, 1).toordinal() - EPOCH_ORD) & (d["eday"] <= date(2025, 9, 30).toordinal() - EPOCH_ORD)].copy()
d["year"] = d["eday"].map(lambda x: date.fromordinal(EPOCH_ORD + x).year)
d["hour"] = ((d["entry_ts"] + 19800) % 86400 - 33300) // 3600
def stat(x, days):
    x = x.dropna(); days = days[x.index]
    if len(x) == 0: return {"n": 0}
    net = x - COST; dm = net.groupby(days).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 and dm.std() > 0 else float("nan")
    return {"n": len(x), "gross": round(x.mean(), 1), "net": round(net.mean(), 1), "win": round(100 * (x > 0).mean()), "t_day": round(t, 1)}
rows = []
for tf in ("1h", "4h", "1d"):
    for c in ["level_retest", "through_bar", "range_break", "order_block", "sweep_reversal", "placebo_order_block", "placebo_sweep_reversal"]:
        m = (d.tf == tf) & (d.concept == c)
        if not m.any(): continue
        for rule in ("wall_then_trail", "fixed_1to2"):
            s = stat(d.loc[m, rule], d["eday"])
            s24 = stat(d.loc[m & (d.year == 2024), rule], d["eday"]); s25 = stat(d.loc[m & (d.year == 2025), rule], d["eday"])
            sl = stat(d.loc[m & (d.side > 0), rule], d["eday"]); ss = stat(d.loc[m & (d.side < 0), rule], d["eday"])
            rows.append({"tf": tf, "concept": c, "exit": rule, **s, "net24": s24.get("net"), "net25": s25.get("net"), "long": sl.get("net"), "short": ss.get("net"),
                         "bars": round(d.loc[m, rule + "_bars"].mean(), 1), "risk_med": round(d.loc[m, "risk_bps"].median(), 0)})
t = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print(t.to_string(index=False))
t.to_csv(f"{sys.argv[1]}/robust_results.csv", index=False)
print("\n== exit mix (wall_then_trail), passing cells")
for tf, c in (("1h", "sweep_reversal"), ("4h", "sweep_reversal"), ("1h", "order_block"), ("4h", "order_block"), ("1d", "order_block")):
    m = (d.tf == tf) & (d.concept == c)
    mix = d.loc[m, "wall_then_trail_how"].value_counts(normalize=True).round(2).to_dict()
    pnl = d.loc[m].groupby("wall_then_trail_how")["wall_then_trail"].mean().round(1).to_dict()
    print(f"  {tf} {c}: mix {mix} | gross by exit {pnl}")
print("\n== 1h sweep reversal by entry hour (0 = 09:15 bar)")
m = (d.tf == "1h") & (d.concept == "sweep_reversal")
print(d.loc[m].groupby("hour")["wall_then_trail"].agg(["size", "mean"]).round(1).to_string())
print("\n== verdict vs placebo (wall_then_trail): cell net − placebo net")
for tf, c in (("1h", "sweep_reversal"), ("4h", "sweep_reversal"), ("1h", "order_block"), ("4h", "order_block"), ("1d", "order_block")):
    a = t[(t.tf == tf) & (t.concept == c) & (t.exit == "wall_then_trail")].iloc[0]
    b = t[(t.tf == tf) & (t.concept == "placebo_" + c) & (t.exit == "wall_then_trail")]
    bn = b.iloc[0]["net"] if len(b) else float("nan")
    ok = (a["net"] > 0) and (a["t_day"] >= 2.5) and (a["n"] >= 300) and (a["net24"] > 0) and (a["net25"] > 0) and (a["net"] - bn >= 10)
    print(f"  {tf} {c:16s} net {a['net']:+6.1f} (t {a['t_day']}) placebo {bn:+6.1f} → {'PASS' if ok else 'fail'}")
