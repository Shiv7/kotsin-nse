"""The daily order block, fixed R target, held across sessions: fill models, cost stacks, market adjustment,
parameter sensitivity, members only. One pass builds the zones; each config re-walks the exits."""
import sys, importlib.util, time
from datetime import date, timedelta
from pathlib import Path
import numpy as np, pandas as pd
from kotsin_nse.market.fo_universe import load as load_members
from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.structure.engine import Bar5, analyze
base = "/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants"
sys.argv = ["mtf_all.py", base, "1d"]
spec = importlib.util.spec_from_file_location("mtf_all", base + "/mtf_all.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
START, END = date(2021, 1, 1), date(2025, 9, 30)
DATA = m.DATA
fm, _ = load_members(Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"))
nifty = m.load_1d("NIFTY").set_index("day")["c"]
syms = sorted(p.stem for p in (DATA / "history" / "1d").glob("*.parquet") if p.stem not in m.INDEX_ROOTS and p.stem != "CRUDEOIL")

# ---- 1. the zones and their first return, once -------------------------------------------------------------
cands = []  # (sym, side, k, bars (list), zone z, s1, day of bar k)
t0 = time.time()
for sym in syms:
    d1 = m.load_1d(sym)
    if d1 is None or len(d1) < 80: continue
    d1 = d1[(d1["day"] >= START - timedelta(days=400)) & (d1["day"] <= END)].reset_index(drop=True)
    dbars = [Bar5(int(r.ts), r.o, r.h, r.l, r.c, r.v) for r in d1.itertuples()]
    days = d1["day"].tolist()
    wk = d1["day"].map(lambda x: x.isocalendar()[:2]).tolist(); weeks = sorted(set(wk)); wstart = {w: wk.index(w) for w in weeks}
    seen = set()
    for wi, w in enumerate(weeks):
        if wi < 8 or wi % 4: continue
        s0 = wstart[w]
        if not (START <= days[s0] <= END): continue
        hist = d1.iloc[max(0, s0 - 60):s0]
        adr = guarded_adr(hist["h"].tolist(), hist["l"].tolist(), hist["c"].tolist()) if len(hist) >= 21 else None
        end = wstart[weeks[wi + 4]] if wi + 4 < len(weeks) else len(dbars)
        max_entry = (wstart[weeks[wi + 2]] if wi + 2 < len(weeks) else len(dbars)) - s0 - 1
        lo_i = max(0, s0 - 60); wb = dbars[lo_i:end]; min_k = s0 - lo_i; max_k = min_k + max_entry
        st = analyze(wb, prev_day=None, adr=adr)
        for zn in st.zones:
            key = (zn.side, zn.ob_ts)
            if key in seen or not zn.retest_ts: continue
            k = next(i for i, b in enumerate(wb) if b.ts == zn.retest_ts[0])
            if k > max_k: continue  # the return has not come yet: a later window records it (not seen)
            seen.add(key)  # returned inside this window's week(s), or already in the past
            if k < min_k or k >= len(wb) - 1: continue
            side = 1 if zn.side == "BULLISH" else -1
            dk = days[lo_i + k]
            try: member = fm.member(sym, dk)
            except ValueError: member = None
            cands.append((sym, side, k, wb, zn.z, zn.s1, dk, member, lo_i))
print(f"zones with a first return: {len(cands)} ({time.time()-t0:.0f}s), members {sum(1 for c in cands if c[7])}")

# ---- 2. the walk, with the fill-session rule as a parameter ---------------------------------------------------
def walk(side, bars, k, entry, stop, tgt_r, hold, stop_slip_bps, fillday):
    risk = (entry - stop) * side
    if risk <= 0: return None
    target = entry + side * tgt_r * risk
    last = min(len(bars) - 1, k + hold)
    for q in range(k, last + 1):
        b = bars[q]
        if q > k and ((side > 0 and b.o <= stop) or (side < 0 and b.o >= stop)):
            return (b.o - entry) * side / entry * 1e4 - stop_slip_bps, "gap_stop", q
        if (side > 0 and b.l <= stop) or (side < 0 and b.h >= stop):
            return (stop - entry) * side / entry * 1e4 - stop_slip_bps, "stop", q
        if q == k and fillday == "never":
            continue
        if q == k and fillday == "close":
            if (side > 0 and b.c >= target) or (side < 0 and b.c <= target):
                return (target - entry) * side / entry * 1e4, "target", q
            continue
        if (side > 0 and b.h >= target) or (side < 0 and b.l <= target):
            return (target - entry) * side / entry * 1e4, "target", q
    return (bars[last].c - entry) * side / entry * 1e4, "close", last

def run(fillday, tgt_r=2.0, hold=20, stop_slip=5.0):
    rows = []
    for sym, side, k, wb, z, s1, dk, member, lo_i in cands:
        if not member: continue
        b = wb[k]
        ok = (b.l <= z * (1 - 1e-3)) if side > 0 else (b.h >= z * (1 + 1e-3)); entry = z
        if not ok: continue
        r = walk(side, wb, k, entry, s1, tgt_r, hold, stop_slip, fillday)
        if r is None: continue
        pnl, how, q = r
        de = dk; dx = de + timedelta(days=int((wb[q].ts - wb[k].ts) // 86400))
        rows.append({"sym": sym, "side": side, "day": de, "exit_day": dx, "year": de.year, "pnl": pnl, "how": how, "held": q - k,
                     "risk_bps": (entry - s1) * side / entry * 1e4, "cross_month": (de.month != dx.month) or (de.year != dx.year)})
    d = pd.DataFrame(rows); d["net"] = d.pnl - (15 + 10 * d.cross_month.astype(float))
    return d

def st(d):
    if not len(d): return "n=0"
    dm = d.net.groupby(d.day).mean(); t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 else float("nan")
    yrs = {y: round(float(g.net.mean()), 1) for y, g in d.groupby("year")}
    return f"n={len(d):5d} gross={d.pnl.mean():6.1f} net={d.net.mean():6.1f} win={100*(d.pnl>0).mean():3.0f}% t={t:5.2f} years={yrs}"

print("\n== the conservative case (through fill, 15 + 5 + roll, 2R, hold 20, members only) by fill-session rule")
res = {}
for rule in ("range", "close", "never"):
    d = run(rule); res[rule] = d
    print(f"  {rule:6s} {st(d)}")
    print(f"         long  {st(d[d.side > 0])}")
    print(f"         short {st(d[d.side < 0])}")
    print(f"         exits {d.how.value_counts().to_dict()} | fill-session exits {int((d.held == 0).sum())} net {d[d.held == 0].net.mean() if (d.held == 0).any() else float('nan'):.1f}")
d = res["range"]; f0 = d[d.held == 0]
print("\n== under 'range': the fill-session exits by how (these are the ones the rule cannot order)")
for how, g in f0.groupby("how"):
    print(f"  {how:8s} n={len(g):5d} net={g.net.mean():6.1f} share of all trades {100*len(g)/len(d):.0f}% | contribution to the published net {g.net.sum()/len(d):+.1f} bps per trade")
print("\n== sensitivity under 'close': target R × hold sessions, net bps / t")
for tgt in (1.5, 2.0, 3.0):
    line = []
    for hold in (10, 20, 40):
        g = run("close", tgt_r=tgt, hold=hold); dm = g.net.groupby(g.day).mean(); t = dm.mean() / dm.std() * np.sqrt(len(dm))
        line.append(f"hold {hold}: {g.net.mean():+6.1f} (t {t:4.1f}, n {len(g)})")
    print(f"  target {tgt}R:  " + " | ".join(line))
res["close"].to_parquet(base + "/daily_ob_fillday_close.parquet", index=False)
print("done")
