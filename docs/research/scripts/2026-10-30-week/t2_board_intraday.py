"""T2 — the morning F&O board against the rest of the day (pre-registered 2026-10-30).

For every root with a board row at 10:15 and at 12:00: futures state, calls'/puts' states, PCR change, ATM IV
change vs the previous day → the stock's return from that minute's 1m close to the 15:14 close, demeaned across
stocks per day. Reads only the engine's archives.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

D = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive")
IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ["2026-10-27", "2026-10-29", "2026-10-30"]
MARKS = ["10:15", "12:00"]


def ts_of(day: str, hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return int(datetime(*map(int, day.split("-")), h, m, tzinfo=IST).timestamp())


def closes(day: str) -> pd.DataFrame:
    b = pd.read_parquet(D / "bars" / f"{day}.parquet", columns=["symbol", "ts", "c", "source"])
    return b[b["source"] != "partial"].sort_values("ts")


def close_at(b: pd.DataFrame, ts: int) -> pd.Series:
    """last 1m close at or before ts, per symbol"""
    k = b[(b["ts"] <= ts) & (b["ts"] > ts - 900)]
    return k.groupby("symbol")["c"].last()


rows = []
for day in DAYS:
    fno = pd.read_parquet(D / "fno" / f"{day}.parquet")
    b = closes(day)
    end = close_at(b, ts_of(day, "15:14") + 59)
    for mark in MARKS:
        t = ts_of(day, mark)
        k = fno[(fno["ts"] <= t + 59) & (fno["ts"] > t - 600)].sort_values("ts").groupby("symbol").last()
        px = close_at(b, t + 59)
        for sym, r in k.iterrows():
            if sym not in px.index or sym not in end.index or px[sym] <= 0:
                continue
            rows.append({"day": day, "mark": mark, "symbol": sym, "ret": np.log(end[sym] / px[sym]) * 1e4,
                         "fut": r["futures_state"], "calls": r["calls_state"], "puts": r["puts_state"], "bias": r["bias"],
                         "pcr_chg": r["x_pcr_oi_chg"], "iv_chg": (r["x_iv"] - r["x_iv_prev"]) if pd.notna(r["x_iv_prev"]) else np.nan,
                         "fut_oi_chg": r["futures_oi_chg_pct"], "score": r["score"]})
df = pd.DataFrame(rows)
df["dret"] = df["ret"] - df.groupby(["day", "mark"])["ret"].transform("mean")
print(f"stock-marks {len(df)}; by day {df.groupby('day').size().to_dict()}; market (mean raw ret to close, bps) {df.groupby(['day','mark'])['ret'].mean().round(1).to_dict()}")


def table(col: str, title: str) -> None:
    print(f"\n== {title}: demeaned return to 15:14 (bps), by {col}")
    for mark in MARKS:
        d = df[df["mark"] == mark]
        g = d.groupby(col)["dret"]
        pooled = pd.DataFrame({"n": g.size(), "mean": g.mean().round(1), "se": (g.std() / np.sqrt(g.size())).round(1)})
        pooled["t"] = (pooled["mean"] / pooled["se"]).round(2)
        byday = d.groupby([col, "day"])["dret"].mean().unstack("day").round(1)
        out = pooled.join(byday)
        out["same_sign_3d"] = (np.sign(byday).nunique(axis=1) == 1) & byday.notna().all(axis=1)
        print(f"-- at {mark}")
        print(out.sort_values("n", ascending=False).to_string())


table("fut", "futures state")
table("calls", "calls state")
table("puts", "puts state")
table("bias", "board bias")
for col, title in (("pcr_chg", "PCR change"), ("iv_chg", "ATM IV change vs previous day"), ("fut_oi_chg", "futures OI change %")):
    d = df.dropna(subset=[col]).copy()
    d["q"] = d.groupby(["day", "mark"])[col].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=["q1", "q2", "q3", "q4", "q5"]))
    table.__wrapped__ = None
    print(f"\n== {title} quintiles (q5 = highest), demeaned return to 15:14 (bps)")
    for mark in MARKS:
        k = d[d["mark"] == mark]
        g = k.groupby("q", observed=True)["dret"]
        pooled = pd.DataFrame({"n": g.size(), "mean": g.mean().round(1), "t": (g.mean() / (g.std() / np.sqrt(g.size()))).round(2)})
        byday = k.groupby(["q", "day"], observed=True)["dret"].mean().unstack("day").round(1)
        print(f"-- at {mark}")
        print(pooled.join(byday).to_string())
        q5, q1 = k[k["q"] == "q5"]["dret"], k[k["q"] == "q1"]["dret"]
        print(f"   q5 − q1 = {q5.mean() - q1.mean():+.1f} bps, t {((q5.mean() - q1.mean()) / np.sqrt(q5.var() / len(q5) + q1.var() / len(q1))):+.2f}")
print("done", file=sys.stderr)
