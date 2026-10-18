"""The 64-state study (operator's futures × call × put table): next-day and 3-day market-neutral moves
by score, bias and premium signal, exploration period only (to 2025-09-30), plus the strategy trades
split by the previous day's bias. Run: uv run python -m kotsin_nse.research.oi64_study"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.market.fo_bhavcopy import OiDailyStore
from kotsin_nse.market.session import ist_day
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.research.oi16 import state64_table

EXPLORE_END = "2025-09-30"
root = Path("data-live/oi_daily")
hs = HistoryStore(Path("data-live/history"))
t = state64_table(OiDailyStore(root), root)
fw = []
for sym in t.symbol.unique():
    d1 = hs.load(sym, "1d")
    if d1.empty:
        continue
    d1 = d1.sort_values("ts").copy()
    d1["day"] = [ist_day(int(x)).isoformat() for x in d1.ts]
    d1 = d1.drop_duplicates("day", keep="last")
    adr = (d1.h - d1.l).rolling(50, min_periods=25).mean() / d1.c
    d1["r1"] = (d1.c.shift(-1) / d1.c - 1) / adr
    d1["r3"] = (d1.c.shift(-3) / d1.c - 1) / adr
    fw.append(d1.assign(symbol=sym)[["symbol", "day", "r1", "r3"]])
x = t.merge(pd.concat(fw), on=["symbol", "day"]).dropna(subset=["r1"])
x = x[x.day <= EXPLORE_END]
for c in ("r1", "r3"):
    x[c + "x"] = x[c] - x.groupby("day")[c].transform("mean")


def table(col: str, order: list) -> None:
    print(f"\n{'':14s} {'n':>6} | {'next day':>8} {'t':>5} {'up%':>5} | {'3 days':>7} {'t':>5}")
    for k in order:
        g = x[x[col] == k]
        if g.empty:
            continue
        row = [f"{k!s:14s} {len(g):>6} |"]
        for r in ("r1x", "r3x"):
            per = g.groupby("day")[r].mean()
            tt = per.mean() / (per.std() / np.sqrt(len(per))) if len(per) > 2 and per.std() > 0 else np.nan
            row.append(f"{g[r].mean():+8.3f} {tt:+5.1f}" + (f" {100 * (g[r] > 0).mean():5.1f} |" if r == "r1x" else ""))
        print(" ".join(row))


print(f"explore {x.day.min()}..{x.day.max()}: {len(x):,} stock-days, {x.symbol.nunique()} stocks")
print("\nBY BIAS (market-neutral, in normal days)")
table("bias", ["Strong Bull", "Bull", "Mild Bull", "Neutral", "Mild Bear", "Bear", "Strong Bear"])
print("\nBY SCORE")
table("score", sorted(x.score.unique(), reverse=True))
print("\nBY PREMIUM SIGNAL")
table("premium", ["Normal", "IV↑ big move", "IV↓ range", "Divergence"])
print("\nDIVERGENCE: the futures direction vs what followed")
for f in ("LB", "SC", "SB", "LU"):
    g = x[(x.premium == "Divergence") & (x.fut == f)]
    if len(g):
        print(f"  futures {f}: n {len(g):>4}  next day {g.r1x.mean():+.3f}  3 days {g.r3x.mean():+.3f}")

ids = ["fib-85ee299414", "fib-95ae3b720b", "fib-0781e1dca1", "fib-8c31c1993e", "mtf-29fe72e400", "mtf-930e9f7b2b",
       "mtf-004ccb5d7d", "mtf-c6abe0295e", "mtf-f239c60bc3", "mtf-46788dfbf0", "mtf-1a79720739"]
seen, rows = set(), []
for rid in ids:
    for tr in json.load(open(f"data-live/obtests/{rid}.json"))["trades"]:
        k = (tr["symbol"], tr["signal_ts"], tr["side"])
        if k not in seen:
            seen.add(k)
            rows.append({"symbol": tr["symbol"], "day": tr["day"], "side": tr["side"], "r": tr["r"]})
tr = pd.DataFrame(rows).assign(d=lambda z: pd.to_datetime(z.day)).sort_values("d")
st = t[["symbol", "day", "score", "bias"]].assign(d=lambda z: pd.to_datetime(z.day) + pd.Timedelta(days=1)).drop(columns="day").sort_values("d")
tr = pd.merge_asof(tr, st, on="d", by="symbol", direction="backward").dropna(subset=["score"])
print(f"\nSTRATEGY TRADES by the previous day's bias ({len(tr)} distinct trades)")
print(f"{'':14s} | {'LONG n':>6} {'avg R':>6} | {'SHORT n':>7} {'avg R':>6}")
for b in ["Strong Bull", "Bull", "Mild Bull", "Neutral", "Mild Bear", "Bear", "Strong Bear"]:
    L = tr[(tr.bias == b) & (tr.side == "BULLISH")].r
    S = tr[(tr.bias == b) & (tr.side == "BEARISH")].r
    print(f"{b:14s} | {len(L):>6} {L.mean() if len(L) else float('nan'):+6.2f} | {len(S):>7} {S.mean() if len(S) else float('nan'):+6.2f}")
