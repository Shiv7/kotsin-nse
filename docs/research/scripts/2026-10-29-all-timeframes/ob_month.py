"""The daily order block (limit at Z on the first return, stop the zone's far side, target 2R, out after 20 sessions)
over one calendar month, every week scanned, as a shadow book would have seen it. Conservative fill (price must trade
10 bps through Z), 15 bps + 5 on stops + 10 on a month-end roll; members only; open trades marked at the last close."""
import sys, importlib.util
from datetime import date, timedelta
from pathlib import Path
import numpy as np, pandas as pd
from kotsin_nse.market.fo_universe import load as load_members
from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.structure.engine import Bar5, analyze
ARGS = sys.argv[1:]
base = "/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants"
sys.argv = ["mtf_all.py", base, "1d"]
spec = importlib.util.spec_from_file_location("mtf_all", base + "/mtf_all.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
Y, MO = int(ARGS[0]) if len(ARGS) > 0 else 2026, int(ARGS[1]) if len(ARGS) > 1 else 10
FILL = ARGS[2] if len(ARGS) > 2 else "through"
M0, M1 = date(Y, MO, 1), (date(Y + (MO == 12), MO % 12 + 1, 1) - timedelta(days=1))
fm, _ = load_members(Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"))
syms = sorted(p.stem for p in (m.DATA / "history" / "1d").glob("*.parquet") if p.stem not in m.INDEX_ROOTS and p.stem != "CRUDEOIL")
rows = []
for sym in syms:
    d1 = m.load_1d(sym)
    if d1 is None or len(d1) < 80: continue
    d1 = d1[(d1["day"] >= M0 - timedelta(days=400)) & (d1["day"] <= M1)].reset_index(drop=True)
    if d1.empty or d1["day"].iloc[-1] < M0: continue
    dbars = [Bar5(int(r.ts), r.o, r.h, r.l, r.c, r.v) for r in d1.itertuples()]
    days = d1["day"].tolist()
    wk = d1["day"].map(lambda x: x.isocalendar()[:2]).tolist(); weeks = sorted(set(wk)); wstart = {w: wk.index(w) for w in weeks}
    seen = set()
    for wi, w in enumerate(weeks):
        s0 = wstart[w]
        wend = wstart[weeks[wi + 1]] if wi + 1 < len(weeks) else len(dbars)
        if days[wend - 1] < M0 - timedelta(days=30) or days[s0] > M1: continue
        hist = d1.iloc[max(0, s0 - 60):s0]
        if len(hist) < 21: continue
        adr = guarded_adr(hist["h"].tolist(), hist["l"].tolist(), hist["c"].tolist())
        lo_i = max(0, s0 - 60); wb = dbars[lo_i:len(dbars)]; min_k = s0 - lo_i; max_k = wend - 1 - lo_i
        st = analyze(wb, prev_day=None, adr=adr)
        for zn in st.zones:
            key = (zn.side, zn.ob_ts)
            if key in seen or not zn.retest_ts: continue
            k = next(i for i, b in enumerate(wb) if b.ts == zn.retest_ts[0])
            if k > max_k: continue  # the return has not come yet: a later window records it (not seen)
            seen.add(key)  # returned inside this window's week(s), or already in the past
            if k < min_k: continue
            side = 1 if zn.side == "BULLISH" else -1
            dk = days[lo_i + k]
            if not (M0 <= dk <= M1): continue
            try:
                if not fm.member(sym, dk): continue
            except ValueError:
                continue
            b = wb[k]; z, s1 = zn.z, zn.s1
            if FILL == "through":
                ok = (b.l <= z * (1 - 1e-3)) if side > 0 else (b.h >= z * (1 + 1e-3))
            else:
                ok = (b.l <= z) if side > 0 else (b.h >= z)
            if not ok: continue
            entry = z; risk = (entry - s1) * side
            if risk <= 0: continue
            target = entry + side * 2 * risk
            last = min(len(wb) - 1, k + 20); res = None
            for q in range(k, last + 1):
                bb = wb[q]
                if q > k and ((side > 0 and bb.o <= s1) or (side < 0 and bb.o >= s1)):
                    res = ((bb.o - entry) * side / entry * 1e4 - 5, "gap_stop", q); break
                if (side > 0 and bb.l <= s1) or (side < 0 and bb.h >= s1):
                    res = ((s1 - entry) * side / entry * 1e4 - 5, "stop", q); break
                if (side > 0 and bb.h >= target) or (side < 0 and bb.l <= target):
                    res = ((target - entry) * side / entry * 1e4, "target", q); break
            if res is None:
                q = last; how = "close" if last == k + 20 else "open"
                res = ((wb[q].c - entry) * side / entry * 1e4, how, q)
            pnl, how, q = res
            dx = days[lo_i + q]
            cost = 15 + (10 if (dx.month != dk.month or dx.year != dk.year) else 0)
            rows.append({"day": dk, "sym": sym, "side": "L" if side > 0 else "S", "entry": round(entry, 2), "stop": round(s1, 2), "target": round(target, 2),
                         "risk_bps": round(risk / entry * 1e4), "exit_day": dx, "exit": round(wb[q].c if how == "open" else (s1 if how == "stop" else target if how == "target" else wb[q].o if how == "gap_stop" else wb[q].c), 2),
                         "how": how, "net_bps": round(pnl - cost, 1), "R": round((pnl - cost) / (risk / entry * 1e4), 2)})
d = pd.DataFrame(rows).sort_values(["day", "sym"]).reset_index(drop=True)
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 500)
print(f"== {M0:%B %Y}, fill={FILL}: {len(d)} entries, {d.sym.nunique()} stocks | closed {int((d.how != 'open').sum())}, still open on {M1 if M1 < date.today() else d.exit_day.max()}: {int((d.how == 'open').sum())}")
print(d.to_string(index=False))
c = d[d.how != "open"]
print(f"\nclosed: n={len(c)} net/trade {c.net_bps.mean():+.1f} bps ({c.R.mean():+.2f} R), win {100*(c.net_bps>0).mean():.0f}%, exits {c.how.value_counts().to_dict()}, sum {c.R.sum():+.1f} R")
print(f"all incl. open marked at the last close: n={len(d)} net/trade {d.net_bps.mean():+.1f} bps, sum {d.R.sum():+.1f} R | long {d[d.side=='L'].net_bps.mean():+.1f} ({(d.side=='L').sum()}) short {d[d.side=='S'].net_bps.mean():+.1f} ({(d.side=='S').sum()})")
g = d.groupby("day").agg(n=("sym", "size"), longs=("side", lambda s: int((s == "L").sum())), net=("net_bps", "mean"), R=("R", "sum"), open=("how", lambda s: int((s == "open").sum())))
print("\nday-wise (entries that day; net per trade incl. open marked):"); print(g.round(1).to_string())
d.to_csv(base + f"/ob_{Y}-{MO:02d}_{FILL}.csv", index=False)
