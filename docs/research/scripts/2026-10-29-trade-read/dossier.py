"""One dossier per shadow trade: the day's levels, the stock's 5m path around the sequence, what happened after the
entry, NIFTY at the same hours, the stock's daily and hourly context."""
import json, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import numpy as np, pandas as pd

D = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
IST = timezone(timedelta(hours=5, minutes=30))
def hm(ts): return datetime.fromtimestamp(int(ts), IST).strftime("%H:%M")
def day_of(ts): return datetime.fromtimestamp(int(ts), IST).date()

def bars5_archive(day):
    a = pd.read_parquet(D / "archive" / "bars" / f"{day}.parquet")
    a = a[a["source"] != "partial"].copy(); a["b"] = (a["ts"] // 300) * 300
    g = a.sort_values("ts").groupby(["symbol", "b"]).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index().rename(columns={"b": "ts"})
    return {s: d.drop(columns="symbol").reset_index(drop=True) for s, d in g.groupby("symbol")}

def bars5_cache(sym, day):
    f = pd.read_parquet(D / "history" / "5m" / f"{sym}.parquet")
    f = f[f["ts"].map(day_of) == day].sort_values("ts").reset_index(drop=True)
    return f[f["ts"].map(lambda t: hm(t) < "15:30")]

def daily(sym):
    d = pd.read_parquet(D / "history" / "1d" / f"{sym}.parquet").drop_duplicates("ts", keep="last").sort_values("ts")
    d["day"] = d["ts"].map(day_of); return d.drop_duplicates("day", keep="last").reset_index(drop=True)

def h30(sym):
    d = pd.read_parquet(D / "history" / "30m" / f"{sym}.parquet").drop_duplicates("ts", keep="last").sort_values("ts")
    d["day"] = d["ts"].map(day_of); return d

def pct(a, b): return (a / b - 1) * 100

def describe_path(b, i0, i1, ref):
    """bars i0..i1 as a compact string: time o/h/l/c (% vs ref)."""
    out = []
    for i in range(max(0, i0), min(len(b), i1 + 1)):
        r = b.iloc[i]; out.append(f"{hm(r.ts)} {r.o:.2f}/{r.h:.2f}/{r.l:.2f}/{r.c:.2f} ({pct(r.c, ref):+.2f}%)")
    return "; ".join(out)

def nifty_at(nb, ts):
    k = nb[nb["ts"] <= ts]
    return float(k["c"].iloc[-1]) if len(k) else float("nan")

def run(day, snap, bars_for, nifty_bars):
    d = json.load(open(snap))
    stages = {(s["symbol"], int(s["side"])): s for s in d["stages"]}
    nb = nifty_bars
    n_open = float(nb["o"].iloc[0]); n_pdc = None
    nd = daily("NIFTY"); npv = nd[nd["day"] < day].iloc[-1]; n_pdc = float(npv["c"])
    n_lv = stages.get(("NIFTY", 1), stages.get(("NIFTY", -1), {})).get("levels")
    n_ib_h, n_ib_l = float(nb["h"].iloc[:12].max()), float(nb["l"].iloc[:12].min())
    print(f"\n######## {day}  NIFTY: pdc {n_pdc:.0f}, open {n_open:.0f} ({pct(n_open, n_pdc):+.2f}%), first hour {n_ib_l:.0f}–{n_ib_h:.0f}, 10:15 close {nifty_at(nb, nb['ts'].iloc[11]):.0f} ({pct(nifty_at(nb, nb['ts'].iloc[11]), n_pdc):+.2f}%), close {float(nb['c'].iloc[-1]):.0f} ({pct(float(nb['c'].iloc[-1]), n_pdc):+.2f}%); levels {({k: round(v) for k, v in n_lv.items()} if n_lv else None)}")
    for t in reversed(d["trades"]):
        sym, side = t["symbol"], t["side"]
        sg = 1 if side == "LONG" else -1
        st = stages.get((sym, sg), {})
        lv, closes = st.get("levels", {}), st.get("closes", {})
        b = bars_for(sym)
        if b is None or len(b) < 20:
            print(f"\n=== {sym} {side}: no bars"); continue
        pdc = (closes.get("last") if closes.get("used") == "last" and closes.get("last") else closes.get("official"))
        if not pdc:
            dd = daily(sym); pdc = float(dd[dd["day"] < day].iloc[-1]["c"])
        o0 = float(b["o"].iloc[0]); ibh, ibl = float(b["h"].iloc[:12].max()), float(b["l"].iloc[:12].min())
        i_os, i_bos, i_brk, i_rt, j = (st.get(k) for k in ("i_os", "i_bos", "i_brk", "i_rt", "j"))
        tags = t.get("tags", {})
        e_ts = t["opened_ts"] - 300  # the entry bar's start
        je = int(b.index[b["ts"] == e_ts][0]) if (b["ts"] == e_ts).any() else None
        x_ts = t.get("closed_ts"); jx = int(b.index[b["ts"] == (x_ts - 300)][0]) if x_ts and (b["ts"] == (x_ts - 300)).any() else None
        print(f"\n=== {sym} {side}  entry bar {t.get('entry_bar') or hm(e_ts)} @ {t['entry']}  stop {t['initial_stop']:.2f} ({t['risk_bps']} bps)  first wall {t['target_name']} {t['walls'][0]['price']:.2f} ({t['walls'][0]['bps']} bps, {t['walls'][0]['bps']/t['risk_bps']:.1f}R)  exit {t.get('exit')} {t.get('exit_reason')} {t.get('pnl_bps'):+.1f} bps  MFE {t.get('mfe_bps'):+.0f} MAE {t.get('mae_bps'):+.0f} bars {t.get('bars_held')}")
        print(f"  levels: P {lv.get('P')} TC {lv.get('TC')} BC {lv.get('BC')} | R1 {lv.get('R1')} S1 {lv.get('S1')} | PDH {lv.get('PDH')} PDL {lv.get('PDL')} | pdc used {pdc} ({closes.get('used')}; official {closes.get('official')})")
        dd = daily(sym); pv = dd[dd["day"] < day].tail(60)
        p1, p2 = pv.iloc[-1], pv.iloc[-2]
        print(f"  daily: prev day {pct(p1.c, p2.c):+.2f}% closed at {((p1.c - p1.l) / max(p1.h - p1.l, 1e-9)):.0%} of its range; 5d {pct(p1.c, pv.iloc[-6].c):+.1f}%  20d {pct(p1.c, pv.iloc[-21].c):+.1f}%; vs DMA20 {tags.get('vs_dma20_pct')}% DMA50 {tags.get('vs_dma50_pct')}% ema stack {tags.get('ema_stack_al')} rs20 {tags.get('rs20_pct')} | 1H supertrend {tags.get('st60')} ({'with' if tags.get('st60_al') else 'against'})")
        try:
            h = h30(sym); hh = h[h["day"] < day].tail(26)
            sess = hh.groupby("day").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))
            print("  last sessions (30m cache): " + " | ".join(f"{k.strftime('%d%b')} {r.o:.1f}→{r.c:.1f} ({pct(r.c, r.o):+.1f}%) H{r.h:.1f} L{r.l:.1f}" for k, r in sess.tail(3).iterrows()))
        except Exception as e:
            print("  30m cache:", e)
        print(f"  open {o0:.2f} ({pct(o0, pdc):+.2f}% vs pdc), first hour {ibl:.2f}–{ibh:.2f}, 10:15 close {b['c'].iloc[11]:.2f}; day H {b['h'].max():.2f} L {b['l'].min():.2f} close {b['c'].iloc[-1]:.2f} ({pct(b['c'].iloc[-1], pdc):+.2f}%)")
        seq = []
        for name, i in (("open test", i_os), ("BOS", i_bos), ("through", i_brk), ("retest", i_rt), ("entry", je if je is not None else j)):
            if i is not None and i < len(b):
                r = b.iloc[int(i)]; seq.append(f"{name} {hm(r.ts)} o{r.o:.2f} h{r.h:.2f} l{r.l:.2f} c{r.c:.2f}")
        print("  sequence: " + " → ".join(seq))
        if je is not None:
            print("  after entry: " + describe_path(b, je + 1, je + 8, t["entry"]))
            seg = b.iloc[je + 1: (jx + 1) if jx is not None else len(b)]
            if len(seg):
                imax, imin = seg["h"].idxmax(), seg["l"].idxmin()
                print(f"  during trade: high {seg['h'].max():.2f} at {hm(b.loc[imax, 'ts'])}, low {seg['l'].min():.2f} at {hm(b.loc[imin, 'ts'])}; exit bar {hm(b.loc[jx, 'ts']) if jx is not None else '?'}; rest of day after exit: {describe_path(b, (jx + 1) if jx is not None else len(b), (jx + 3) if jx is not None else len(b), t['entry'])}")
            n_e = nifty_at(nb, e_ts + 300); n_x = nifty_at(nb, x_ts) if x_ts else float("nan")
            n_lvl = n_lv or {}
            pos = "above TC" if n_lvl and n_e > n_lvl.get("TC", 0) else ("below BC" if n_lvl and n_e < n_lvl.get("BC", 0) else "inside CPR")
            print(f"  NIFTY: at entry {n_e:.0f} ({pct(n_e, n_pdc):+.2f}% vs pdc, {pct(n_e, n_open):+.2f}% from open, {pos}); entry→exit {pct(n_x, n_e):+.2f}%; stock entry→exit {pct(t.get('exit') or t['entry'], t['entry']):+.2f}% | stock vs NIFTY from open to entry: {pct(t['entry'], o0) - pct(n_e, n_open):+.2f}% | tags move_adr {tags.get('move_adr')} range_pos {tags.get('range_pos')} ib_al {tags.get('ib_al')} nifty_move {tags.get('nifty_move_pct')} xs_pct {tags.get('xs_pct')}")

a27 = bars5_archive("2026-10-27")
run(date(2026, 10, 27), D / "strategy" / "2026-10-27.json", lambda s: a27.get(s), a27["NIFTY"])
run(date(2026, 10, 28), D / "strategy" / "2026-10-28-replay.json", lambda s: bars5_cache(s, date(2026, 10, 28)), bars5_cache("NIFTY", date(2026, 10, 28)))
