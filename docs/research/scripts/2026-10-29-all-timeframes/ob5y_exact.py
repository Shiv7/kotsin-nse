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

# ---- 2. the fill session ordered exactly on 5m bars (2024-01 → 2025-09, where the 5m cache exists) ----------
from functools import lru_cache
@lru_cache(maxsize=None)
def days5(sym):
    try: return m.load_days(sym, "5m")
    except Exception: return {}

def walk_exact(side, bars, k, z, stop, tgt_r, hold, stop_slip_bps, fill_day, d5):
    """Fill session on 5m bars: fill at the first 5m bar through Z; from then on stop before target; within the
    fill bar the target counts only on a close beyond it. Later sessions on daily bars as before."""
    entry = z; risk = (entry - stop) * side; target = entry + side * tgt_r * risk
    if risk <= 0: return None
    thr = z * (1 - 1e-3) if side > 0 else z * (1 + 1e-3)
    fi = next((i for i, b in enumerate(d5) if (b.l <= thr if side > 0 else b.h >= thr)), None)
    if fi is None: return "nofill"
    for i in range(fi, len(d5)):
        b = d5[i]
        if (side > 0 and b.l <= stop) or (side < 0 and b.h >= stop):
            return (stop - entry) * side / entry * 1e4 - stop_slip_bps, "stop", k
        hit = ((b.c >= target) if side > 0 else (b.c <= target)) if i == fi else ((b.h >= target) if side > 0 else (b.l <= target))
        if hit:
            return (target - entry) * side / entry * 1e4, "target", k
    last = min(len(bars) - 1, k + hold)
    for q in range(k + 1, last + 1):
        b = bars[q]
        if (side > 0 and b.o <= stop) or (side < 0 and b.o >= stop):
            return (b.o - entry) * side / entry * 1e4 - stop_slip_bps, "gap_stop", q
        if (side > 0 and b.l <= stop) or (side < 0 and b.h >= stop):
            return (stop - entry) * side / entry * 1e4 - stop_slip_bps, "stop", q
        if (side > 0 and b.h >= target) or (side < 0 and b.l <= target):
            return (target - entry) * side / entry * 1e4, "target", q
    return (bars[last].c - entry) * side / entry * 1e4, "close", last

def walk_daily(side, bars, k, entry, stop, tgt_r, hold, stop_slip_bps, fillday):
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
        if q == k and fillday == "close":
            if (side > 0 and b.c >= target) or (side < 0 and b.c <= target):
                return (target - entry) * side / entry * 1e4, "target", q
            continue
        if (side > 0 and b.h >= target) or (side < 0 and b.l <= target):
            return (target - entry) * side / entry * 1e4, "target", q
    return (bars[last].c - entry) * side / entry * 1e4, "close", last

rows = []; nofill = 0; no5m = 0
for sym, side, k, wb, z, s1, dk, member, lo_i in cands:
    if not member or dk < date(2024, 1, 1): continue
    b = wb[k]
    ok = (b.l <= z * (1 - 1e-3)) if side > 0 else (b.h >= z * (1 + 1e-3))
    if not ok: continue
    d5 = days5(sym).get(dk)
    if not d5: no5m += 1; continue
    ex = walk_exact(side, wb, k, z, s1, 2.0, 20, 5.0, dk, d5)
    if ex == "nofill": nofill += 1; continue
    if ex is None: continue
    row = {"sym": sym, "side": side, "day": dk, "year": dk.year}
    for nm, r in (("exact", ex), ("range", walk_daily(side, wb, k, z, s1, 2.0, 20, 5.0, "range")), ("close", walk_daily(side, wb, k, z, s1, 2.0, 20, 5.0, "close"))):
        pnl, how, q = r; dx = dk + timedelta(days=int((wb[q].ts - wb[k].ts) // 86400))
        row[f"{nm}_pnl"] = pnl - (15 + 10 * ((dk.month != dx.month) or (dk.year != dx.year))); row[f"{nm}_how"] = how; row[f"{nm}_held"] = q - k
    rows.append(row)
d = pd.DataFrame(rows)
print(f"\n== 2024-01 → 2025-09 members with 5m bars on the fill day: n={len(d)} (no 5m day {no5m}, daily said through but 5m never did {nofill})")
for nm in ("range", "close", "exact"):
    net = d[f"{nm}_pnl"]; dm = net.groupby(d.day).mean(); t = dm.mean() / dm.std() * np.sqrt(len(dm))
    yrs = {y: round(float(g[f"{nm}_pnl"].mean()), 1) for y, g in d.groupby("year")}
    print(f"  {nm:6s} net={net.mean():6.1f} win={100*(net>0).mean():3.0f}% t={t:5.2f} years={yrs} | long {d[d.side>0][f'{nm}_pnl'].mean():6.1f} short {d[d.side<0][f'{nm}_pnl'].mean():6.1f} | exits {d[f'{nm}_how'].value_counts().to_dict()}")
f0 = d[d.range_held == 0]
print(f"\n== the fill-session exits under 'range' (n={len(f0)}), what the 5m bars say they really were")
print(pd.crosstab(f0.range_how, f0.exact_how, margins=True).to_string())
print(f"  net of those trades: range {f0.range_pnl.mean():6.1f} | exact {f0.exact_pnl.mean():6.1f} | close-rule {f0.close_pnl.mean():6.1f}")
d.to_parquet(base + "/daily_ob_exact_2425.parquet", index=False)
print("done")
