"""Long and short of the daily order block by regime known at the entry day. The marking is chosen on 2021–23 only
(rule written first), then applied unchanged to 2024–25 and October 2026."""
import sys, importlib.util
from datetime import date, timedelta
from pathlib import Path
import numpy as np, pandas as pd
base = "/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants"
sys.argv = ["x", base, "1d"]
spec = importlib.util.spec_from_file_location("mtf_all", base + "/mtf_all.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from kotsin_nse.market.fo_universe import load as load_members
fm, _ = load_members(Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"))

# ---- trades: five years conservative (through fill, 15 + 5 + roll) and October 2026 (same)
t5 = pd.read_parquet(base + "/daily_ob_conservative.parquet"); t5["day"] = pd.to_datetime(t5.day).dt.date; t5["period"] = np.where(t5.year <= 2023, "2021-23", "2024-25")
oc = pd.read_csv(base + "/ob_2026-10_through.csv", parse_dates=["day"]); oc["day"] = oc.day.dt.date; oc["side"] = np.where(oc.side == "L", 1, -1); oc["net"] = oc.net_bps; oc["period"] = "2026-10"
T = pd.concat([t5[["sym", "side", "day", "net", "risk_bps", "period"]], oc[["sym", "side", "day", "net", "risk_bps", "period"]]], ignore_index=True)

# ---- market context at the entry day, from the daily cache (known at the close of the day before)
nd = m.load_1d("NIFTY").set_index("day")["c"].sort_index()
nifty = pd.DataFrame({"c": nd}); nifty["dma50"] = nifty.c.rolling(50).mean(); nifty["dma200"] = nifty.c.rolling(200).mean()
nifty["ret20"] = nifty.c.pct_change(20) * 100; nifty["rv20"] = np.log(nifty.c).diff().rolling(20).std() * np.sqrt(252) * 100
nifty = nifty.shift(1)  # the day before the entry
# breadth: share of the daily cache's names above their own 20-DMA, the day before
syms = sorted(p.stem for p in (m.DATA / "history" / "1d").glob("*.parquet") if p.stem not in m.INDEX_ROOTS and p.stem != "CRUDEOIL")
above = {}; stock_ctx = {}
for s in syms:
    d = m.load_1d(s)
    if d is None or len(d) < 60: continue
    c = d.set_index("day")["c"]; dma20 = c.rolling(20).mean(); dma50 = c.rolling(50).mean(); r20 = c.pct_change(20) * 100
    above[s] = (c > dma20).astype(float).shift(1)
    stock_ctx[s] = pd.DataFrame({"s_vs50": (c / dma50 - 1).shift(1) * 100, "s_ret20": r20.shift(1)})
breadth = pd.concat(above.values(), axis=1).mean(axis=1)
def ctx(r):
    d = r.day
    n = nifty.loc[:d].iloc[-1] if (nifty.index <= d).any() else None
    b = breadth.loc[:d].iloc[-1] if (breadth.index <= d).any() else np.nan
    sc = stock_ctx.get(r.sym); s = sc.loc[:d].iloc[-1] if sc is not None and (sc.index <= d).any() else None
    return {"n_vs50": n.c / n.dma50 - 1 if n is not None else np.nan, "n_vs200": n.c / n.dma200 - 1 if n is not None else np.nan, "n_ret20": n.ret20 if n is not None else np.nan,
            "n_rv20": n.rv20 if n is not None else np.nan, "breadth": b, "s_vs50": s.s_vs50 if s is not None else np.nan, "s_ret20": s.s_ret20 if s is not None else np.nan}
T = pd.concat([T, pd.DataFrame([ctx(r) for r in T.itertuples()])], axis=1)
# bins (the volatility and breadth cut points come from 2021–23 only)
tr = T[T.period == "2021-23"]
rv_cut = tr.n_rv20.quantile([1/3, 2/3]).values; br_cut = tr.breadth.quantile([1/3, 2/3]).values
T["NIFTY vs 50-DMA"] = np.where(T.n_vs50 > 0, "above", "below")
T["NIFTY vs 200-DMA"] = np.where(T.n_vs200 > 0, "above", "below")
T["NIFTY 20-day return"] = np.where(T.n_ret20 > 0, "up", "down")
T["NIFTY 20-day vol"] = pd.cut(T.n_rv20, [-1, rv_cut[0], rv_cut[1], 1e9], labels=["low", "mid", "high"]).astype(str)
T["breadth (above 20-DMA)"] = pd.cut(T.breadth, [-1, br_cut[0], br_cut[1], 2], labels=["weak", "mid", "strong"]).astype(str)
T["stock vs its 50-DMA, for the trade"] = np.where(T.side * T.s_vs50 > 0, "with trend", "against trend")
T["stock 20-day return, for the trade"] = np.where(T.side * T.s_ret20 > 0, "with", "against")
REG = ["NIFTY vs 50-DMA", "NIFTY vs 200-DMA", "NIFTY 20-day return", "NIFTY 20-day vol", "breadth (above 20-DMA)", "stock vs its 50-DMA, for the trade", "stock 20-day return, for the trade"]
def st(g):
    if len(g) < 5: return (len(g), np.nan, np.nan)
    dm = g.net.groupby(g.day).mean(); t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 else np.nan
    return (len(g), round(g.net.mean(), 1), round(t, 1))
print("== by side, by period (net bps, t by day)")
for sd, nm in ((1, "long"), (-1, "short")):
    print(f"  {nm:5s}", {p: st(T[(T.side == sd) & (T.period == p)]) for p in ("2021-23", "2024-25", "2026-10")})
print("\n== by regime at the entry day (n, net, t): 2021-23 | 2024-25 | 2026-10")
rows = []
for reg in REG:
    for val in sorted(T[reg].dropna().unique()):
        for sd, nm in ((1, "long"), (-1, "short")):
            cells = {p: st(T[(T[reg] == val) & (T.side == sd) & (T.period == p)]) for p in ("2021-23", "2024-25", "2026-10")}
            rows.append({"regime": reg, "value": val, "side": nm, **{p: f"{c[0]:4d} {c[1]:+7.1f} ({c[2]:4.1f})" if not np.isnan(c[1]) else f"{c[0]:4d}" for p, c in cells.items()},
                         "n_tr": cells["2021-23"][0], "net_tr": cells["2021-23"][1], "t_tr": cells["2021-23"][2], "net_te": cells["2024-25"][1], "net_oct": cells["2026-10"][1]})
R = pd.DataFrame(rows); pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
print(R[["regime", "value", "side", "2021-23", "2024-25", "2026-10"]].to_string(index=False))
# ---- the marking, chosen on 2021-23 only: a side is ON in a regime value if net > 0, t >= 2.5, n >= 200 there
print("\n== marking rule (2021-23 only): side ON where net > 0, t >= 2.5, n >= 200; then applied unchanged")
for reg in REG:
    sub = R[R.regime == reg]
    on = sub[(sub.net_tr > 0) & (sub.t_tr >= 2.5) & (sub.n_tr >= 200)]
    off = sub[~sub.index.isin(on.index)]
    if on.empty: continue
    def pol(p):
        keep = pd.Series(False, index=T.index)
        for r in on.itertuples(): keep |= (T[reg] == r.value) & (T.side == (1 if r.side == "long" else -1))
        g = T[keep & (T.period == p)]; a = T[T.period == p]
        return f"{p}: policy {st(g)} vs all {st(a)}"
    print(f"  {reg}: ON = {[(r.value, r.side) for r in on.itertuples()]}")
    for p in ("2021-23", "2024-25", "2026-10"): print("     ", pol(p))
T.to_parquet(base + "/daily_ob_regimes.parquet", index=False)
