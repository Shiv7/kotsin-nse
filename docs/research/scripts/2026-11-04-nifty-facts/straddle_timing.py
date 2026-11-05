"""The NIFTY ATM straddle sold at time T and bought back at 15:29, from the option TICKS, for every archived
session and several T — the owner's question (6 Nov 2026): "sell at the best time". The strike is the spot at T
rounded to 50; the expiry the nearest strictly after the day (on expiry day the following week's — the owner's
rule). Sold at the bid, bought at the ask when the quote is fresh, else the last trade. A description over the
archived days, not a test. Usage: straddle_timing.py DAY [DAY ...]"""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
import live_facts as lf  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TIMES = ["09:20", "09:30", "09:45", "10:00", "10:30", "11:00", "12:00", "13:00", "14:00"]
CLOSE = "15:29:59"
A = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive")


def hms(ts: float) -> str:
    return datetime.datetime.fromtimestamp(float(ts), IST).strftime("%H:%M:%S")


def one(day: str) -> list[dict]:
    m = lf.master_for(day)
    m = m[m["Name"].astype(str).str.match(r"^NIFTY \d\d [A-Z]{3} \d{4} (CE|PE) ")].copy()
    m["expiry"] = pd.to_datetime(m["Name"].str.extract(r"^NIFTY (\d\d [A-Z]{3} \d{4})")[0], format="%d %b %Y").dt.date
    m["kind"] = m["Name"].str.extract(r" (CE|PE) ")[0]
    m["strike"] = m["Name"].str.extract(r" (CE|PE) ([\d.]+)")[1].astype(float)
    d = datetime.date.fromisoformat(day)
    later = sorted(e for e in set(m["expiry"]) if e > d)
    if not later:
        return []
    exp = later[0]
    spot = pq.read_table(A / "bars" / f"{day}.parquet", filters=[("symbol", "==", "NIFTY")]).to_pandas()
    spot = spot[spot["source"] != "partial"].sort_values("ts")
    spot["hm"] = spot["ts"].apply(lambda x: datetime.datetime.fromtimestamp(x, IST).strftime("%H:%M"))
    rows = []
    want: dict[str, tuple[float, str, str]] = {}
    for T in TIMES:
        b = spot[spot["hm"] < T].tail(1)  # the bar that closed at T
        if b.empty:
            continue
        px = float(b["c"].iloc[0])
        k = round(px / 50) * 50
        legs = m[(m["expiry"] == exp) & (m["strike"] == k)]
        if len(legs) < 2:
            continue
        ce = str(legs[legs["kind"] == "CE"]["ScripCode"].iloc[0]); pe = str(legs[legs["kind"] == "PE"]["ScripCode"].iloc[0])
        want[T] = (px, ce, pe)
    codes = sorted({c for _, ce, pe in want.values() for c in (ce, pe)})
    t = lf.read("ticks", day, codes, ["scrip_code", "ts", "ltp", "bid", "ask"])
    t["scrip_code"] = t["scrip_code"].astype(str)
    t["hms"] = t["ts"].apply(hms)
    t = t.sort_values("ts")

    def quote(code: str, upto: str, side: str) -> float | None:
        x = t[(t["scrip_code"] == code) & (t["hms"] <= upto)].tail(1)
        if x.empty:
            return None
        v = x[side].iloc[0]
        return float(v) if v and v > 0 else float(x["ltp"].iloc[0])

    for T, (px, ce, pe) in want.items():
        s = [quote(ce, T + ":59", "bid"), quote(pe, T + ":59", "bid")]
        c = [quote(ce, CLOSE, "ask"), quote(pe, CLOSE, "ask")]
        if None in s or None in c:
            continue
        rows.append({"day": day, "T": T, "spot": px, "strike": round(px / 50) * 50, "expiry": str(exp), "sold": round(s[0] + s[1], 2),
                     "bought": round(c[0] + c[1], 2), "pnl": round(s[0] + s[1] - c[0] - c[1], 2)})
    return rows


def main() -> None:
    out = []
    for day in sys.argv[1:]:
        try:
            out += one(day)
        except Exception as exc:  # noqa: BLE001
            print(day, "failed:", repr(exc)[:120])
    D = pd.DataFrame(out)
    if D.empty:
        print("nothing"); return
    print("== straddle sold at T, bought back 15:29 (points; the 65-lot ₹ = ×65)")
    P = D.pivot(index="day", columns="T", values="pnl")
    print(P.to_string())
    print("\nmean   ", P.mean().round(1).to_dict())
    print("median ", P.median().round(1).to_dict())
    print("positive", {c: f"{int((P[c] > 0).sum())}/{int(P[c].notna().sum())}" for c in P.columns})
    print("\n== what was sold (points) by T")
    print(D.pivot(index="day", columns="T", values="sold").to_string())


if __name__ == "__main__":
    main()
