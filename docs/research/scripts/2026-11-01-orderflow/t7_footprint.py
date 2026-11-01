"""T7 footprint diagonal imbalance (300 %) per 5-minute bar from the tick archive — cross-sectional IC, 3 days.

Each snapshot's new volume (Δ total_qty) is booked at its last price with the side by the quote rule; per
(stock, 5-minute bar, price): buy and sell volume. Buy imbalance at p: buy(p) ≥ 3 × sell(level below) > 0;
sell imbalance at p: sell(p) ≥ 3 × buy(level above) > 0. net_imb and stacked (≥ 3 consecutive one way).
Forward returns from the bar's last 1m close. Pre-registration: docs/research/2026-11-01-prereg-orderflow-checklist.md.
"""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.market.session import ist_at

ARCHIVE = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive")
DAYS = (date(2026, 10, 27), date(2026, 10, 29), date(2026, 10, 30))
H = (5, 15, 30)
rng = np.random.default_rng(13)


def footprint_day(d: date) -> pd.DataFrame:
    bars = pd.read_parquet(ARCHIVE / "bars" / f"{d}.parquet", columns=["symbol", "scrip_code", "ts", "c", "source"])
    bars = bars[bars["source"] == "live"].drop_duplicates(["scrip_code", "ts"], keep="last")
    bars["scrip_code"] = bars["scrip_code"].astype(str)
    cash = set(bars["scrip_code"])
    close = bars.pivot(index="ts", columns="scrip_code", values="c").sort_index()
    close = close.reindex(range(int(close.index.min()), int(close.index.max()) + 60, 60))
    t = pd.read_parquet(ARCHIVE / "ticks" / f"{d}.parquet", columns=["scrip_code", "ts", "recv_ts", "ltp", "total_qty", "bid", "ask"])
    t["scrip_code"] = t["scrip_code"].astype(str)
    t = t[t["scrip_code"].isin(cash)]
    lo, hi = ist_at(d, "09:15"), ist_at(d, "15:30")
    lag = (t["recv_ts"] - t["ts"])
    print(d, "ticks (cash)", len(t), "codes", t["scrip_code"].nunique(), "recv−ts s: median", round(float(lag.median()), 1), "p10/p90", round(float(lag.quantile(0.1)), 1), round(float(lag.quantile(0.9)), 1))
    t = t[(t["recv_ts"] >= lo) & (t["recv_ts"] < hi)].sort_values(["scrip_code", "recv_ts"])
    t["dq"] = t.groupby("scrip_code")["total_qty"].diff()
    t = t[t["dq"] > 0]
    side = np.where(t["ltp"] >= t["ask"], 1, np.where(t["ltp"] <= t["bid"], -1, 0))
    t["side"] = side
    t = t[t["side"] != 0]
    t["bar"] = ((t["recv_ts"] - lo) // 300).astype(int)
    fp = t.groupby(["scrip_code", "bar", "ltp", "side"])["dq"].sum().unstack("side").fillna(0.0).rename(columns={1: "buy", -1: "sell"})
    for c in ("buy", "sell"):
        if c not in fp.columns:
            fp[c] = 0.0
    out = []
    for (code, bar), g in fp.groupby(level=[0, 1]):
        g = g.sort_index(level="ltp")
        b, s = g["buy"].to_numpy(), g["sell"].to_numpy()
        n = len(b)
        buy_imb = np.zeros(n, bool)
        sell_imb = np.zeros(n, bool)
        if n >= 2:
            buy_imb[1:] = (s[:-1] > 0) & (b[1:] >= 3 * s[:-1])      # buy at p vs sell one level below
            sell_imb[:-1] = (b[1:] > 0) & (s[:-1] >= 3 * b[1:])    # sell at p vs buy one level above
        def stacked(m: np.ndarray) -> bool:
            run = 0
            for v in m:
                run = run + 1 if v else 0
                if run >= 3:
                    return True
            return False
        st = (1 if stacked(buy_imb) else 0) - (1 if stacked(sell_imb) else 0)
        out.append({"code": code, "bar": bar, "levels": n, "n_buy_imb": int(buy_imb.sum()), "n_sell_imb": int(sell_imb.sum()), "stacked": st,
                    "bar_delta": float(b.sum() - s.sum()), "bar_vol": float(b.sum() + s.sum())})
    r = pd.DataFrame(out)
    r["net_imb"] = r["n_buy_imb"] - r["n_sell_imb"]
    r["delta_share"] = r["bar_delta"] / r["bar_vol"].replace(0, np.nan)
    r["ts_close"] = lo + r["bar"] * 300 + 240   # the bar's last 1m bar
    for h in H:
        fwd = (close.shift(-h) / close - 1).stack(future_stack=True).reset_index()
        fwd.columns = ["ts_close", "code", f"fwd_{h}"]
        r = r.merge(fwd, on=["ts_close", "code"], how="left")
    r["day"] = str(d)
    print(d, "bars", len(r), "with any imbalance", int(((r["n_buy_imb"] + r["n_sell_imb"]) > 0).sum()), "stacked +", int((r["stacked"] > 0).sum()), "stacked −", int((r["stacked"] < 0).sum()))
    return r


rows, cond = [], []
for d in DAYS:
    r = footprint_day(d)
    for sig in ("net_imb", "stacked", "delta_share"):
        for h in H:
            real, fake = [], []
            for _, g in r.dropna(subset=[sig, f"fwd_{h}"]).groupby("bar"):
                if len(g) < 30 or g[sig].nunique() < 2:
                    continue
                real.append(g[sig].rank().corr(g[f"fwd_{h}"].rank()))
                fake.append(pd.Series(rng.permutation(g[sig].values)).rank().corr(g[f"fwd_{h}"].rank().reset_index(drop=True)))
            q = r.dropna(subset=[sig, f"fwd_{h}"]).copy()
            q["pct"] = q.groupby("bar")[sig].rank(pct=True)
            spread = (q.loc[q["pct"] >= 0.8, f"fwd_{h}"].mean() - q.loc[q["pct"] <= 0.2, f"fwd_{h}"].mean()) * 1e4
            rows.append({"day": str(d), "sig": sig, "h": h, "n_cs": len(real), "ic": np.nanmean(real), "t": np.nanmean(real) / (np.nanstd(real) / np.sqrt(len(real))),
                         "placebo_t": np.nanmean(fake) / (np.nanstd(fake) / np.sqrt(len(fake))), "q5_minus_q1_bps": spread})
    for h in H:
        for name, m in (("stacked +1", r["stacked"] > 0), ("stacked −1", r["stacked"] < 0), ("no stack", r["stacked"] == 0)):
            y = r.loc[m, f"fwd_{h}"].dropna()
            cond.append({"day": str(d), "what": name, "h": h, "n": len(y), "mean_bps": y.mean() * 1e4, "t": y.mean() / (y.std() / np.sqrt(len(y))) if len(y) > 1 else np.nan})

pd.set_option("display.width", 220)
x = pd.DataFrame(rows)
print("\n== IC per day (per 5-minute-bar cross-section)")
print(x.round(4).to_string(index=False))
print("\n== pooled")
print(x.groupby(["sig", "h"]).agg(ic=("ic", "mean"), min_t=("t", "min"), max_t=("t", "max"), days_pos=("ic", lambda v: int((v > 0).sum())), days=("ic", "size"),
                                  max_placebo_t=("placebo_t", lambda v: v.abs().max()), q5_minus_q1_bps=("q5_minus_q1_bps", "mean")).round(4).to_string())
c = pd.DataFrame(cond)
print("\n== stacked bars, forward return (raw, bps)")
print(c.round(2).to_string(index=False))
c["w"] = c["mean_bps"] * c["n"]
print(c.groupby(["what", "h"]).apply(lambda g: pd.Series({"n": g["n"].sum(), "mean_bps": g["w"].sum() / g["n"].sum(), "days_pos": int((g["mean_bps"] > 0).sum())}), include_groups=False).round(2).to_string())
