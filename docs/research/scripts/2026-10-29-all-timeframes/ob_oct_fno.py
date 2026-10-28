"""October's daily order-block entries read beside the futures and options data of the entry day (NSE bhavcopy):
the stock future's OI change and price change (the build-up), the options put/call OI ratio of the nearest expiry."""
import pandas as pd, numpy as np
from datetime import date, timedelta
from pathlib import Path
base = "/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants"
OI = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/oi_daily")
t = pd.read_csv(base + "/ob_2026-10_through.csv", parse_dates=["day", "exit_day"])
t["day"] = t.day.dt.date; t["exit_day"] = t.exit_day.dt.date
cache = {}
def oi(day):
    if day not in cache:
        p = OI / f"{day}.parquet"; cache[day] = pd.read_parquet(p) if p.exists() else None
    return cache[day]
def prev_session(day):
    for k in range(1, 6):
        d = day - timedelta(days=k)
        if (OI / f"{d}.parquet").exists(): return d
    return None
rows = []
for r in t.itertuples():
    o, pd_ = oi(r.day), prev_session(r.day); op = oi(pd_) if pd_ else None
    if o is None or op is None: rows.append({}); continue
    f = o[(o.symbol == r.sym) & (o.kind == "FUTSTK")]; fp = op[(op.symbol == r.sym) & (op.kind == "FUTSTK")]
    if f.empty or fp.empty: rows.append({}); continue
    near = f.sort_values("expiry").iloc[0]; nearp = fp[fp.expiry == near.expiry]
    fut_oi, fut_oi_p = float(f.oi.sum()), float(fp.oi.sum())
    px, pxp = float(near.close), float(nearp.close.iloc[0]) if len(nearp) else np.nan
    d_oi = (fut_oi / fut_oi_p - 1) * 100 if fut_oi_p else np.nan; d_px = (px / pxp - 1) * 100 if pxp else np.nan
    lab = ("long build-up" if d_px > 0 else "short build-up") if d_oi > 1 else ("short covering" if d_px > 0 else "long unwinding") if d_oi < -1 else "flat OI"
    opt = o[(o.symbol == r.sym) & (o.kind == "OPTSTK") & (o.expiry == near.expiry)]
    pcr = float(opt[opt.option_type == "PE"].oi.sum()) / max(float(opt[opt.option_type == "CE"].oi.sum()), 1)
    rows.append({"fut_oi_chg_pct": round(d_oi, 1), "fut_px_chg_pct": round(d_px, 2), "build_up": lab, "pcr": round(pcr, 2)})
x = pd.concat([t, pd.DataFrame(rows)], axis=1)
x.to_csv(base + "/ob_2026-10_fno.csv", index=False)
pd.set_option("display.width", 220)
print("October entries with F&O data:", int(x.build_up.notna().sum()), "of", len(x))
print("\n== by the future's build-up on the entry day (net bps per trade, closed + open marked)")
print(x.groupby(["build_up", "side"])["net_bps"].agg(["size", "mean"]).round(1).to_string())
print("\n== with the build-up agreeing with the trade (long build-up for a long, short build-up for a short) vs against")
agree = ((x.side == "L") & (x.build_up == "long build-up")) | ((x.side == "S") & (x.build_up == "short build-up"))
against = ((x.side == "L") & (x.build_up == "short build-up")) | ((x.side == "S") & (x.build_up == "long build-up"))
for nm, mm in (("agree", agree), ("against", against), ("neither", ~agree & ~against & x.build_up.notna())):
    g = x[mm]; print(f"  {nm:8s} n={len(g):3d} net={g.net_bps.mean():+7.1f} win={100*(g.net_bps>0).mean():3.0f}%")
print("\n== by the options put/call OI ratio (nearest expiry) tercile")
x["pcr_t"] = pd.qcut(x.pcr, 3, labels=["low PCR (calls heavy)", "mid", "high PCR (puts heavy)"])
print(x.groupby(["pcr_t", "side"], observed=True)["net_bps"].agg(["size", "mean"]).round(1).to_string())
print("\n== the ten biggest winners and losers with their F&O read")
cols = ["day", "sym", "side", "how", "net_bps", "fut_oi_chg_pct", "fut_px_chg_pct", "build_up", "pcr"]
print(x.sort_values("net_bps").head(6)[cols].to_string(index=False)); print(x.sort_values("net_bps").tail(6)[cols].to_string(index=False))
