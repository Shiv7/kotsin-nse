"""Post-hoc control for T5's `div` (not pre-registered, a decomposition): is the positive IC of
rank(cvd_30) − rank(ret_30) just the 30-minute price reversal? IC of −ret_30 alone, of cvd_30 alone, of
div, and of div with ret_30's rank partialled out (the residual of rank(cvd_30) on rank(ret_30) within the
minute), per day and pooled over all cross-sections (t over cross-sections)."""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.market.session import ist_at
from kotsin_nse.research.micro_ic import day_frame

ARCHIVE = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive")
DAYS = (date(2026, 10, 27), date(2026, 10, 29), date(2026, 10, 30))
H = (5, 15, 30)


def grid(f, col):
    g = f.pivot_table(index="ts", columns="code", values=col, aggfunc="last").sort_index()
    return g.reindex(range(int(g.index.min()), int(g.index.max()) + 60, 60))


allics = []
for d in DAYS:
    f = day_frame(ARCHIVE, d)
    f = f[f["ts"] >= ist_at(d, "09:15")].copy()
    delta, vol = grid(f, "delta").fillna(0.0), grid(f, "vol").fillna(0.0)
    bars = pd.read_parquet(ARCHIVE / "bars" / f"{d}.parquet", columns=["scrip_code", "ts", "c", "source"])
    bars = bars[bars["source"] == "live"].drop_duplicates(["scrip_code", "ts"], keep="last")
    bars["scrip_code"] = bars["scrip_code"].astype(str)
    close = bars.pivot(index="ts", columns="scrip_code", values="c").sort_index().reindex(delta.index).reindex(columns=delta.columns).ffill()
    cvd30 = delta.rolling(30, min_periods=20).sum() / vol.rolling(30, min_periods=20).sum().replace(0, np.nan)
    ret30 = close / close.shift(30) - 1
    for g, name in ((cvd30, "cvd_30"), (ret30, "ret_30")):
        s = g.stack(future_stack=True).reset_index()
        s.columns = ["ts", "code", name]
        f = f.merge(s, on=["ts", "code"], how="left")
    k = f.dropna(subset=["cvd_30", "ret_30"]).copy()
    k["rc"], k["rr"] = k.groupby("ts")["cvd_30"].rank(pct=True), k.groupby("ts")["ret_30"].rank(pct=True)
    k["div"] = k["rc"] - k["rr"]
    k["neg_ret_30"] = -k["ret_30"]
    # residual of rank(cvd) on rank(ret) within the minute
    def resid(g):
        x, y = g["rr"].to_numpy(), g["rc"].to_numpy()
        if len(g) < 30 or x.std() == 0:
            return pd.Series(np.nan, index=g.index)
        b = np.cov(x, y, bias=True)[0, 1] / x.var()
        return pd.Series(y - b * x, index=g.index)
    k["cvd_resid"] = k.groupby("ts", group_keys=False).apply(resid, include_groups=False)
    for sig in ("neg_ret_30", "cvd_30", "div", "cvd_resid"):
        for h in H:
            for ts, g in k.dropna(subset=[sig, f"fwd_{h}"]).groupby("ts"):
                if len(g) >= 30 and g[sig].nunique() > 1:
                    allics.append({"day": str(d), "sig": sig, "h": h, "ic": g[sig].rank().corr(g[f"fwd_{h}"].rank())})
a = pd.DataFrame(allics)
pd.set_option("display.width", 200)
print("== per day: mean IC and t over the day's cross-sections")
print(a.groupby(["sig", "h", "day"])["ic"].agg(ic="mean", t=lambda v: v.mean() / (v.std() / np.sqrt(len(v))), n="size").round(4).to_string())
print("\n== pooled over all cross-sections")
print(a.groupby(["sig", "h"])["ic"].agg(ic="mean", t=lambda v: v.mean() / (v.std() / np.sqrt(len(v))), n="size").round(4).to_string())
