"""Loop 1: the four changes from the fifteen-trade read, scored on the dev window before they ship.
(A) no round numbers as walls; (B) room to the nearest real level beyond the entry, at any distance, must be >= 1R
else skip; (C) skip when the stock has already travelled >= X ADR from the open; (D) PDH/PDL from continuous trades
(cannot be scored here: the 5m cache is continuous already)."""
import sys, importlib.util
from datetime import date
import numpy as np, pandas as pd
spec = importlib.util.spec_from_file_location("variants", "/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants/variants.py")
V = importlib.util.module_from_spec(spec); spec.loader.exec_module(V)
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.instrument.universe import INDEX_ROOTS
from pathlib import Path
import math

DATA = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
orig_walls = cr.walls

def cands(side, entry, levels, weekly, use_round):
    c = {}
    if side > 0:
        c.update({k: levels[k] for k in ("R1", "R2", "R3") if k in levels}); c["PDH"] = levels["PDH"]
        if weekly: c.update({k: weekly[k] for k in ("WP", "WR1", "WR2") if k in weekly})
        if use_round:
            step = cr.round_step(entry); c["ROUND"] = math.floor(entry / step) * step + step
        return {k: v for k, v in c.items() if v > entry}
    c.update({k: levels[k] for k in ("S1", "S2", "S3") if k in levels}); c["PDL"] = levels["PDL"]
    if weekly: c.update({k: weekly[k] for k in ("WP", "WS1", "WS2") if k in weekly})
    if use_round:
        step = cr.round_step(entry); c["ROUND"] = math.ceil(entry / step) * step - step
    return {k: v for k, v in c.items() if v < entry}

def walls_noround(side, entry, levels, weekly, tol):
    c = cands(side, entry, levels, weekly, False)
    out = [(k, v) for k, v in c.items() if abs(v - entry) >= tol]
    return sorted(out, key=lambda kv: side * kv[1])

rows = []
syms = sorted(p.stem for p in (DATA / "history" / "5m").glob("*.parquet") if p.stem not in INDEX_ROOTS and p.stem != "CRUDEOIL")
for si, sym in enumerate(syms):
    d1 = V.load_1d(DATA, sym)
    if d1 is None: continue
    h5 = pd.read_parquet(DATA / "history" / "5m" / f"{sym}.parquet")
    days = {d: b for d, b in V.bars_by_day(h5).items() if date(2024, 1, 1) <= d <= date(2025, 9, 30) and len(b) >= 70}
    for day, bars in sorted(days.items()):
        prev = d1[d1["day"] < day]
        if len(prev) < 2: continue
        p = prev.iloc[-1]
        if not (p["h"] > p["l"] > 0): continue
        levels = cr.day_levels(float(p["h"]), float(p["l"]), float(p["c"]))
        win = prev.tail(60)
        adr = guarded_adr(win["h"].tolist(), win["l"].tolist(), win["c"].tolist()) if len(win) >= 21 else None
        tol = cr.tolerance(adr, float(p["c"]))
        weekly = V.weekly_for(d1, day)
        st = cr.sequence(bars, levels, tol)
        for side, s in st.items():
            if s.j is None: continue
            j, i_rt = int(s.j), int(s.i_rt)
            entry = bars[j].c
            stop = (bars[i_rt].l - tol) if side > 0 else (bars[i_rt].h + tol)
            risk = (entry - stop) * side
            if risk <= 0: continue
            cr.walls = orig_walls
            t0 = V.trade(side, bars, j, entry, stop, levels, weekly, tol)
            cr.walls = walls_noround
            t1 = V.trade(side, bars, j, entry, stop, levels, weekly, tol)
            cr.walls = orig_walls
            near = cands(side, entry, levels, weekly, False)
            room_real = (min(abs(v - entry) for v in near.values()) / risk) if near else np.nan
            o0 = bars[0].o
            adr_pct = adr / entry * 100 if adr else None
            move_adr = side * (entry / o0 - 1) * 100 / adr_pct if adr_pct else np.nan
            rows.append({"sym": sym, "day": day.isoformat(), "side": side, "base": t0["pnl"] if t0 else np.nan, "noround": t1["pnl"] if t1 else np.nan,
                         "room_real": room_real, "move_adr": move_adr, "w1_base": t0["w1"] if t0 else "", "room_base": t0["room_R"] if t0 else np.nan})
    if (si + 1) % 50 == 0: print(si + 1, len(rows), flush=True)
d = pd.DataFrame(rows); d.to_parquet("/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants/loop1.parquet", index=False)
def st(x, days):
    x = x.dropna(); days = days[x.index]; net = x - 15; dm = net.groupby(days).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 else float("nan")
    return f"n={len(x):5d} gross={x.mean():6.1f} net={net.mean():6.1f} win={100*(x>0).mean():3.0f}% t={t:5.2f}"
print("base (live walls)                       ", st(d.base, d.day))
print("(A) no ROUND walls                      ", st(d.noround, d.day))
for r in (0.5, 1.0, 1.5):
    m = d.room_real >= r
    print(f"(A)+(B) room to nearest real level >= {r}R", st(d.loc[m, "noround"], d.day), "| skipped:", st(d.loc[~m, "noround"], d.day))
for x in (0.5, 0.6, 0.8, 1.0):
    m = d.move_adr < x
    print(f"(A)+(C) move from open < {x} ADR          ", st(d.loc[m, "noround"], d.day), "| skipped:", st(d.loc[~m, "noround"], d.day))
for r, x in ((1.0, 0.6), (1.0, 0.8), (0.5, 0.6)):
    m = (d.room_real >= r) & (d.move_adr < x)
    print(f"(A)+(B)+(C) room>={r}R & move<{x}ADR         ", st(d.loc[m, "noround"], d.day), "| skipped:", st(d.loc[~m, "noround"], d.day))
print("by first wall (base):", d.groupby("w1_base")["base"].agg(["size", "mean"]).round(1).to_dict("index"))
