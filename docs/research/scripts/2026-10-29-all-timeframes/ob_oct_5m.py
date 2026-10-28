"""The same order block on 5m bars for the October days with intraday data (20, 23, 26, 27 Oct from the feed archive,
28 Oct the exchange's), intraday, flat at the close, conservative fill, 15 bps."""
import sys, importlib.util
from datetime import date
import numpy as np, pandas as pd
base = "/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants"
sys.argv = ["x", base, "5m"]
spec = importlib.util.spec_from_file_location("mtf_all", base + "/mtf_all.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
specv = importlib.util.spec_from_file_location("variants", base + "/variants.py"); V = importlib.util.module_from_spec(specv); specv.loader.exec_module(V)
from kotsin_nse.structure.engine import Bar5, analyze
from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.market.fo_universe import load as load_members
from pathlib import Path
fm, _ = load_members(Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"))
DAYS = [date(2026, 10, 20), date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)]
arch = {d: V.archive_5m(m.DATA, d) for d in DAYS[:-1]}
rows = []
syms = sorted(p.stem for p in (m.DATA / "history" / "5m").glob("*.parquet") if p.stem not in m.INDEX_ROOTS and p.stem != "CRUDEOIL")
for sym in syms:
    d1 = m.load_1d(sym)
    if d1 is None: continue
    days = {}
    for d in DAYS[:-1]:
        if sym in arch[d]:
            b = V.bars_by_day(arch[d][sym]); 
            if d in b and len(b[d]) >= 60: days[d] = b[d]
    c = m.load_days(sym, "5m"); 
    if DAYS[-1] in c: days[DAYS[-1]] = c[DAYS[-1]]
    for d, bars in days.items():
        prev = d1[d1["day"] < d]
        if len(prev) < 25: continue
        p = prev.iloc[-1]; hist = prev.tail(60)
        adr = guarded_adr(hist["h"].tolist(), hist["l"].tolist(), hist["c"].tolist()); tol = cr.tolerance(adr, float(p["c"]))
        try:
            if not fm.member(sym, d): continue
        except ValueError: continue
        st = analyze(bars, prev_day=(float(p["h"]), float(p["l"]), float(p["c"])), adr=adr)
        idx = {b.ts: i for i, b in enumerate(bars)}
        for zn in st.zones:
            if not zn.retest_ts: continue
            k = idx[zn.retest_ts[0]]; side = 1 if zn.side == "BULLISH" else -1; b = bars[k]; z, s1 = zn.z, zn.s1
            if k >= len(bars) - 1 or not ((b.l <= z * (1 - 1e-3)) if side > 0 else (b.h >= z * (1 + 1e-3))): continue
            risk = (z - s1) * side
            if risk <= 0: continue
            target = z + side * 2 * risk; res = None
            for q in range(k, len(bars)):
                bb = bars[q]
                if (side > 0 and bb.l <= s1) or (side < 0 and bb.h >= s1): res = ((s1 - z) * side / z * 1e4 - 5, "stop"); break
                if (side > 0 and bb.h >= target) or (side < 0 and bb.l <= target): res = ((target - z) * side / z * 1e4, "target"); break
            if res is None: res = ((bars[-1].c - z) * side / z * 1e4, "close")
            rows.append({"day": d, "sym": sym, "side": side, "time": m.hm(k) if hasattr(m, "hm") else k, "risk_bps": risk / z * 1e4, "pnl": res[0], "how": res[1]})
x = pd.DataFrame(rows); x["net"] = x.pnl - 15
print(f"5m order block, five October days, members only: n={len(x)} gross={x.pnl.mean():.1f} net={x.net.mean():.1f} win={100*(x.pnl>0).mean():.0f}% | long {x[x.side>0].net.mean():.1f} ({(x.side>0).sum()}) short {x[x.side<0].net.mean():.1f} ({(x.side<0).sum()})")
print(x.groupby("day")["net"].agg(["size", "mean"]).round(1).to_string())
print("exits:", x.how.value_counts().to_dict(), "| risk median", round(x.risk_bps.median()))
