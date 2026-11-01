"""T5 cumulative delta (slope, divergence) and T6 absorption — cross-sectional rank IC per minute, 3 days.

Readings are built on the 1-minute grid per stock from the flow archive; forward returns from the 1m close
grid (micro_ic.day_frame). Placebo: the reading shuffled within its minute. Pre-registration:
docs/research/2026-11-01-prereg-orderflow-checklist.md.
"""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.market.session import ist_at
from kotsin_nse.research.micro_ic import day_frame

ARCHIVE = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive")
DAYS = (date(2026, 10, 27), date(2026, 10, 29), date(2026, 10, 30))
H = (5, 15, 30)
rng = np.random.default_rng(11)


def grid(f: pd.DataFrame, col: str) -> pd.DataFrame:
    g = f.pivot_table(index="ts", columns="code", values=col, aggfunc="last").sort_index()
    return g.reindex(range(int(g.index.min()), int(g.index.max()) + 60, 60))


def ic_rows(f: pd.DataFrame, sig: str, day: str) -> list[dict]:
    out = []
    for h in H:
        real, fake = [], []
        for _, g in f.dropna(subset=[sig, f"fwd_{h}"]).groupby("ts"):
            if len(g) < 30 or g[sig].nunique() < 2:
                continue
            real.append(g[sig].rank().corr(g[f"fwd_{h}"].rank()))
            fake.append(pd.Series(rng.permutation(g[sig].values)).rank().corr(g[f"fwd_{h}"].rank().reset_index(drop=True)))
        if not real:
            continue
        q = f.dropna(subset=[sig, f"fwd_{h}"]).copy()
        q["pct"] = q.groupby("ts")[sig].rank(pct=True)
        spread = (q.loc[q["pct"] >= 0.8, f"fwd_{h}"].mean() - q.loc[q["pct"] <= 0.2, f"fwd_{h}"].mean()) * 1e4
        out.append({"day": day, "sig": sig, "h": h, "n_cs": len(real), "ic": np.nanmean(real), "t": np.nanmean(real) / (np.nanstd(real) / np.sqrt(len(real))),
                    "placebo_t": np.nanmean(fake) / (np.nanstd(fake) / np.sqrt(len(fake))), "q5_minus_q1_bps": spread})
    return out


rows, cond = [], []
for d in DAYS:
    f = day_frame(ARCHIVE, d)
    if f is None:
        print(d, "missing")
        continue
    lo = ist_at(d, "09:15")
    f = f[f["ts"] >= lo].copy()
    delta, vol = grid(f, "delta").fillna(0.0), grid(f, "vol").fillna(0.0)
    bars = pd.read_parquet(ARCHIVE / "bars" / f"{d}.parquet", columns=["scrip_code", "ts", "c", "source"])
    bars = bars[bars["source"] == "live"].drop_duplicates(["scrip_code", "ts"], keep="last")
    bars["scrip_code"] = bars["scrip_code"].astype(str)
    close = bars.pivot(index="ts", columns="scrip_code", values="c").sort_index()
    close = close.reindex(delta.index).reindex(columns=delta.columns).ffill()
    # T5
    cvd30 = delta.rolling(30, min_periods=20).sum() / vol.rolling(30, min_periods=20).sum().replace(0, np.nan)
    ret30 = close / close.shift(30) - 1
    # T6
    ret1 = (close / close.shift(1) - 1).abs()
    dshare = (delta / vol.replace(0, np.nan)).abs()
    vol_hi = vol.rolling(60, min_periods=30).quantile(0.8).shift(1)
    move_med = ret1.rolling(60, min_periods=30).median().shift(1)
    strong = (vol >= vol_hi) & (vol > 0) & (dshare >= 0.5)
    quiet = ret1 <= move_med
    absorb = (-np.sign(delta)).where(strong & quiet, 0.0)
    plain = (-np.sign(delta)).where(strong & ~quiet, 0.0)

    def stack(g: pd.DataFrame, name: str) -> pd.DataFrame:
        s = g.stack(future_stack=True).reset_index()
        s.columns = ["ts", "code", name]
        return s

    f = f.merge(stack(cvd30, "cvd_30"), on=["ts", "code"], how="left").merge(stack(ret30, "ret_30"), on=["ts", "code"], how="left")
    f = f.merge(stack(absorb, "absorb"), on=["ts", "code"], how="left").merge(stack(plain, "plain"), on=["ts", "code"], how="left")
    k = f.dropna(subset=["cvd_30", "ret_30"]).copy()
    k["div"] = k.groupby("ts")["cvd_30"].rank(pct=True) - k.groupby("ts")["ret_30"].rank(pct=True)
    f = f.merge(k[["ts", "code", "div"]], on=["ts", "code"], how="left")
    print(d, "rows", len(f), "absorb minutes", int((f["absorb"] != 0).sum()), "plain-strong minutes", int((f["plain"] != 0).sum()))
    for sig in ("cvd_30", "div", "absorb"):
        rows += ic_rows(f, sig, str(d))
    # T5 divergence cases: opposite signs, both beyond the quartiles
    k["cq"], k["rq"] = k.groupby("ts")["cvd_30"].rank(pct=True), k.groupby("ts")["ret_30"].rank(pct=True)
    dv = k[((k["cq"] >= 0.75) & (k["rq"] <= 0.25)) | ((k["cq"] <= 0.25) & (k["rq"] >= 0.75))]
    for h in H:
        y = dv[f"fwd_{h}"] * np.sign(dv["cvd_30"])
        y = y.dropna()
        cond.append({"day": str(d), "what": "T5 divergence, fwd in the CVD's direction", "h": h, "n": len(y), "mean_bps": y.mean() * 1e4, "t": y.mean() / (y.std() / np.sqrt(len(y)))})
    # T6 conditional: absorbed vs strong-but-moving, forward return in the absorbed (−sign delta) direction
    for name, col in (("T6 absorbed (strong & quiet)", "absorb"), ("T6 strong & moving", "plain")):
        s = f[f[col] != 0]
        for h in H:
            y = (s[f"fwd_{h}"] * s[col]).dropna()
            cond.append({"day": str(d), "what": name, "h": h, "n": len(y), "mean_bps": y.mean() * 1e4, "t": y.mean() / (y.std() / np.sqrt(len(y)))})

r = pd.DataFrame(rows)
pd.set_option("display.width", 220)
print("\n== IC per day")
print(r.round(4).to_string(index=False))
print("\n== pooled")
print(r.groupby(["sig", "h"]).agg(ic=("ic", "mean"), min_t=("t", "min"), max_t=("t", "max"), days_pos=("ic", lambda x: int((x > 0).sum())), days=("ic", "size"),
                                  max_placebo_t=("placebo_t", lambda x: x.abs().max()), q5_minus_q1_bps=("q5_minus_q1_bps", "mean")).round(4).to_string())
c = pd.DataFrame(cond)
print("\n== conditional means per day")
print(c.round(2).to_string(index=False))
print("\n== conditional means pooled (n-weighted)")
c["w"] = c["mean_bps"] * c["n"]
print(c.groupby(["what", "h"]).apply(lambda g: pd.Series({"n": g["n"].sum(), "mean_bps": g["w"].sum() / g["n"].sum(), "days_pos": int((g["mean_bps"] > 0).sum()), "min_t": g["t"].min(), "max_t": g["t"].max()}), include_groups=False).round(2).to_string())
