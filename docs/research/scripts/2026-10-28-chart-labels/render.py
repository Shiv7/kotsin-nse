"""Blind charts for the held CPR retests: one 5m candlestick chart per instance, cut at the entry bar.

No symbol, no date, prices as % of the previous close, the day's levels drawn (CPR band, R1, S1, PDH, PDL),
the retest bar marked. Calibration set: 50 instances, seeded, 25 long / 25 short.
"""
import sys, json, random
from pathlib import Path
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from datetime import datetime, timezone, timedelta

S = Path(sys.argv[1]); OUT = S / "chart_labels"; PNG = OUT / "png"; PNG.mkdir(parents=True, exist_ok=True)
HIST = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/history/5m")
IST = timezone(timedelta(hours=5, minutes=30))
N = int(sys.argv[2]) if len(sys.argv) > 2 else 50
SEED = 20261028

tr = pd.read_parquet(S / "sweep/retest_trail.parquet")
inst = tr[tr.rule == "wall_then_trail"].drop_duplicates(["sym", "day", "side"]).reset_index(drop=True)
rng = np.random.default_rng(SEED)
longs = inst[inst.side > 0].sample(N // 2, random_state=SEED)
shorts = inst[inst.side < 0].sample(N - N // 2, random_state=SEED)
calib = pd.concat([longs, shorts]).sample(frac=1, random_state=SEED).reset_index(drop=True)
calib["blind_id"] = [f"c{i+1:02d}" for i in range(len(calib))]

def day_bars(sym, day):
    df = pd.read_parquet(HIST / f"{sym}.parquet")
    d0 = int(datetime.fromisoformat(f"{day} 09:15").replace(tzinfo=IST).timestamp())
    x = df[(df.ts >= d0) & (df.ts < d0 + 375 * 60)].sort_values("ts").reset_index(drop=True)
    return x, d0

rows = []
for _, r in calib.iterrows():
    bars, d0 = day_bars(r.sym, r.day)
    days = pd.read_parquet(S / f"sweep/hist/{r.sym}.days.parquet")
    lv = days[days.day == r.day].iloc[0]
    j, i_rt = int(r.j), int(r.i_rt)
    b = bars.iloc[: j + 1]
    assert len(b) == j + 1, (r.sym, r.day, len(bars), j)
    pdc = float(lv.pdc)
    pct = lambda p: (p / pdc - 1) * 100
    fig, ax = plt.subplots(figsize=(9, 5), dpi=100)
    for i, (o, h, l, c) in enumerate(zip(b.o, b.h, b.l, b.c)):
        col = "#2e9e5b" if c >= o else "#d9483b"
        ax.plot([i, i], [pct(l), pct(h)], color=col, lw=0.8)
        ax.add_patch(Rectangle((i - 0.35, pct(min(o, c))), 0.7, max(abs(pct(c) - pct(o)), 0.005), color=col))
    # levels
    ax.axhspan(pct(lv.BC), pct(lv.TC), color="#f0c0d0", alpha=0.35)
    for name, v, col in (("TC", lv.TC, "#b03060"), ("P", lv.P, "#b03060"), ("BC", lv.BC, "#b03060"),
                         ("R1", lv.R1, "#1f77b4"), ("S1", lv.S1, "#1f77b4"), ("PDH", lv.PDH, "#555"), ("PDL", lv.PDL, "#555")):
        ax.axhline(pct(v), color=col, lw=0.8, ls="--" if name in ("PDH", "PDL") else "-")
        ax.text(len(b) + 0.5, pct(v), name, fontsize=8, color=col, va="center")
    ax.annotate("retest", (i_rt, pct(b.l.iloc[i_rt]) if r.side > 0 else pct(b.h.iloc[i_rt])),
                xytext=(0, -18 if r.side > 0 else 18), textcoords="offset points", ha="center", fontsize=8,
                arrowprops=dict(arrowstyle="->", lw=0.8))
    ax.set_xlim(-1, max(len(b) + 6, 20))
    lo, hi = min(pct(b.l.min()), pct(lv.S1), pct(lv.PDL)), max(pct(b.h.max()), pct(lv.R1), pct(lv.PDH))
    pad = (hi - lo) * 0.08
    ax.set_ylim(lo - pad, hi + pad)
    ticks = list(range(0, len(b), 12))
    ax.set_xticks(ticks); ax.set_xticklabels([f"{9 + (15 + 5 * t) // 60:02d}:{(15 + 5 * t) % 60:02d}" for t in ticks], fontsize=8)
    ax.set_ylabel("% vs previous close", fontsize=8); ax.tick_params(axis="y", labelsize=8)
    ax.text(0.01, 0.97, "LONG setup" if r.side > 0 else "SHORT setup", transform=ax.transAxes, fontsize=10, va="top",
            color="#2e9e5b" if r.side > 0 else "#d9483b", weight="bold")
    ax.text(0.99, 0.97, "last bar = the bar after the retest", transform=ax.transAxes, fontsize=8, va="top", ha="right", color="#666")
    ax.grid(alpha=0.2)
    fig.tight_layout(); fig.savefig(PNG / f"{r.blind_id}.png"); plt.close(fig)
    rows.append(dict(blind_id=r.blind_id, sym=r.sym, day=r.day, side=int(r.side), i_rt=i_rt, j=j, net_bps=float(r.net_bps),
                     pnl_bps=float(r.pnl_bps), risk_bps=float(r.risk_bps), w1_bps=float(r.w1_bps), mfe_bps=float(r.mfe_bps), how=r.how))
key = pd.DataFrame(rows); key.to_parquet(OUT / "calib_key.parquet"); key.to_csv(OUT / "calib_key.csv", index=False)
print(len(rows), "charts ->", PNG); print(key[["blind_id", "side", "net_bps", "how"]].head(8).to_string())
import os; print("png size kB (first):", round(os.path.getsize(PNG / "c01.png") / 1024))
