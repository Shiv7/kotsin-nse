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
            seen.add(key)
            k = next(i for i, b in enumerate(wb) if b.ts == zn.retest_ts[0])
            if k < min_k or k > max_k or k >= len(wb) - 1: continue
            side = 1 if zn.side == "BULLISH" else -1
            dk = days[lo_i + k]
            try: member = fm.member(sym, dk)
            except ValueError: member = None
            cands.append((sym, side, k, wb, zn.z, zn.s1, dk, member, lo_i))
print(f"zones with a first return: {len(cands)} ({time.time()-t0:.0f}s), members {sum(1 for c in cands if c[7])}")

# ---- 2. one walk per config ----------------------------------------------------------------------------------
def walk(side, bars, k, entry, stop, tgt_r, hold, stop_slip_bps):
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
        if (side > 0 and b.h >= target) or (side < 0 and b.l <= target):
            return (target - entry) * side / entry * 1e4, "target", q
    return (bars[last].c - entry) * side / entry * 1e4, "close", last

def run(fill="touch", tgt_r=2.0, hold=20, stop_slip=0.0, members_only=True):
    rows = []
    for sym, side, k, wb, z, s1, dk, member, lo_i in cands:
        if members_only and not member: continue
        b = wb[k]
        if fill == "touch":
            ok = (b.l <= z) if side > 0 else (b.h >= z); entry = z
        elif fill == "through":  # the limit fills only once price has traded 10 bps beyond it
            ok = (b.l <= z * (1 - 1e-3)) if side > 0 else (b.h >= z * (1 + 1e-3)); entry = z
        else:  # "close": a market order at the close of the bar that first returned to the zone
            ok = (b.l <= z) if side > 0 else (b.h >= z); entry = b.c
        if not ok: continue
        r = walk(side, wb, k, entry, s1, tgt_r, hold, stop_slip)
        if r is None: continue
        pnl, how, q = r
        de = dk; dx = de + timedelta(days=int((wb[q].ts - wb[k].ts) // 86400))
        ne = nifty.get(de); nx = nifty.get(dx)
        nret = (nx / ne - 1) * 1e4 if (ne and nx) else np.nan
        rows.append({"sym": sym, "side": side, "day": de, "exit_day": dx, "year": de.year, "pnl": pnl, "how": how, "held": q - k, "risk_bps": (entry - s1) * side / entry * 1e4,
                     "nifty_bps": nret, "cross_month": (de.month != dx.month) or (de.year != dx.year)})
    return pd.DataFrame(rows)

def st(d, cost):
    if not len(d): return "n=0"
    net = d.pnl - cost; dm = net.groupby(d.day).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 else float("nan")
    yrs = {y: round(float((g.pnl - cost).mean()), 1) for y, g in d.groupby("year")}
    return f"n={len(d):5d} gross={d.pnl.mean():6.1f} net={net.mean():6.1f} win={100*(d.pnl>0).mean():3.0f}% t={t:5.2f} years={yrs}"

print("\n== fill model × cost (target 2R, hold 20, members only)")
for fill in ("touch", "through", "close"):
    d = run(fill=fill)
    print(f"  {fill:8s} 15 bps            ", st(d, 15))
    d5 = run(fill=fill, stop_slip=5)
    cost = 15 + 10 * d5.cross_month.astype(float)  # +10 bps when the hold crosses a month end (the roll)
    print(f"  {fill:8s} 15 + 5 stop + 10 roll", st(d5.assign(pnl=d5.pnl - cost + 15), 15), f"| crosses a month end {100*d5.cross_month.mean():.0f}%")
d = run(fill="through", stop_slip=5); cost = 15 + 10 * d.cross_month.astype(float); d["net"] = d.pnl - cost
print("\n== the conservative case (through, 15 + 5 + roll): by side, market-adjusted, by NIFTY's month")
for nm, g in (("long", d[d.side > 0]), ("short", d[d.side < 0])):
    adj = g.net - g.side * g.nifty_bps
    print(f"  {nm:5s} n={len(g)} net={g.net.mean():6.1f} | minus NIFTY over the hold (beta 1): {adj.mean():6.1f} | NIFTY itself over those holds {(g.side*g.nifty_bps).mean():+.1f}")
d["nm"] = d.day.map(lambda x: (x.year, x.month)); nm_ret = nifty.groupby(nifty.index.map(lambda x: (x.year, x.month))).agg(lambda s: s.iloc[-1] / s.iloc[0] - 1)
d["nifty_month_up"] = d.nm.map(lambda k: nm_ret.get(k, np.nan) > 0)
for up in (True, False):
    g = d[d.nifty_month_up == up]; print(f"  NIFTY month {'up' if up else 'down'}: n={len(g)} net={g.net.mean():6.1f} long {g[g.side>0].net.mean():6.1f} short {g[g.side<0].net.mean():6.1f}")
q = d.copy(); q["q"] = q.day.map(lambda x: f"{x.year}Q{(x.month-1)//3+1}")
qq = q.groupby("q")["net"].agg(["size", "mean"]); print("  quarters positive:", int((qq['mean'] > 0).sum()), "/", len(qq), "| worst", round(qq['mean'].min(), 1), qq['mean'].idxmin(), "| 2025 by quarter", {k: round(v, 1) for k, v in qq['mean'].items() if k.startswith('2025')})
print("\n== sensitivity (through fill, 15 + 5 + roll): target R × hold sessions, net bps / t")
for tgt in (1.5, 2.0, 3.0):
    line = []
    for hold in (10, 20, 40):
        g = run(fill="through", tgt_r=tgt, hold=hold, stop_slip=5); c = 15 + 10 * g.cross_month.astype(float); net = g.pnl - c
        dm = net.groupby(g.day).mean(); t = dm.mean() / dm.std() * np.sqrt(len(dm))
        line.append(f"hold {hold}: {net.mean():+6.1f} (t {t:4.1f}, n {len(g)})")
    print(f"  target {tgt}R:  " + " | ".join(line))
print("\n== the book, conservative case: R per trade, concurrency, drawdown")
d["R"] = d.net / d.risk_bps
print(f"  net per trade {d.net.mean():.1f} bps = {d.R.mean():+.3f} R; risk median {d.risk_bps.median():.0f} bps; trades/year {len(d)/4.75:.0f}")
# open positions per session and the equity in R (one unit of risk per trade)
alld = sorted(set(d.day) | set(d.exit_day)); opens = pd.Series(0, index=pd.Index(alld))
for r in d.itertuples(): opens[(opens.index >= r.day) & (opens.index < r.exit_day)] += 1
print(f"  open positions per session: median {int(opens.median())}, 90th pct {int(opens.quantile(.9))}, max {int(opens.max())}")
eq = d.groupby("exit_day")["R"].sum().cumsum(); dd = (eq - eq.cummax()).min()
print(f"  equity in R (1 unit of risk per trade): end {eq.iloc[-1]:+.0f} R over {len(d)} trades, max drawdown {dd:+.0f} R; worst year {d.groupby('year')['R'].sum().round(0).to_dict()}")
d.to_parquet(base + "/daily_ob_conservative.parquet", index=False)
