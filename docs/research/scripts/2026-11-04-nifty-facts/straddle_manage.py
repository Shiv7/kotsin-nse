"""The 10:00 straddle's minute-by-minute path to 15:29 on every archived day (sold at the bids, marked at the asks), its
best and worst point along the way, and what a target / stop in points would have done against holding to the close —
the owner (6 Nov 2026): "lets suppose we earn 19 avg so if we getting 76 or 90 points lets book it and lose only 19".
Usage: straddle_manage.py DAY [DAY ...]"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import live_facts as lf  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
A = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive")
RULES = [(19, 76), (19, 38), (19, 57), (30, 60), (30, 90), (40, 80), (25, 50)]


def path(day: str) -> pd.Series | None:
    import pyarrow.parquet as pq
    m = lf.master_for(day)
    m = m[m["Name"].astype(str).str.match(r"^NIFTY \d\d [A-Z]{3} \d{4} (CE|PE) ")].copy()
    m["expiry"] = pd.to_datetime(m["Name"].str.extract(r"^NIFTY (\d\d [A-Z]{3} \d{4})")[0], format="%d %b %Y").dt.date
    m["kind"] = m["Name"].str.extract(r" (CE|PE) ")[0]
    m["strike"] = m["Name"].str.extract(r" (CE|PE) ([\d.]+)")[1].astype(float)
    d = datetime.date.fromisoformat(day)
    later = sorted(e for e in set(m["expiry"]) if e > d)
    if not later:
        return None
    spot = pq.read_table(A / "bars" / f"{day}.parquet", filters=[("symbol", "==", "NIFTY")]).to_pandas()
    spot = spot[spot["source"] != "partial"].sort_values("ts")
    spot["hm"] = spot["ts"].apply(lambda x: datetime.datetime.fromtimestamp(x, IST).strftime("%H:%M"))
    b = spot[spot["hm"] < "10:00"].tail(1)
    if b.empty:
        return None
    k = round(float(b["c"].iloc[0]) / 50) * 50
    legs = m[(m["expiry"] == later[0]) & (m["strike"] == k)]
    if len(legs) < 2:
        return None
    codes = [str(c) for c in legs["ScripCode"]]
    t = lf.read("ticks", day, codes, ["scrip_code", "ts", "ltp", "bid", "ask"])
    t["scrip_code"] = t["scrip_code"].astype(str)
    t = t.sort_values("ts")
    t["minute"] = (t["ts"] // 60) * 60
    # each leg's last quote per minute, forward-filled; the sale at the 10:00 bids, the mark at the asks
    out = {}
    for c in codes:
        x = t[t.scrip_code == c].groupby("minute").last()[["ltp", "bid", "ask"]]
        x["bid"] = x["bid"].where(x["bid"] > 0, x["ltp"])
        x["ask"] = x["ask"].where(x["ask"] > 0, x["ltp"])
        out[c] = x
    idx = sorted(set(out[codes[0]].index) | set(out[codes[1]].index))
    q = {c: out[c].reindex(idx).ffill() for c in codes}
    hm = pd.Series([datetime.datetime.fromtimestamp(i, IST).strftime("%H:%M") for i in idx], index=idx)
    win = (hm >= "10:00") & (hm <= "15:29")
    sold = sum(float(q[c]["bid"][win].iloc[0]) for c in codes)
    debit = sum(q[c]["ask"][win] for c in codes)
    pnl = (sold - debit).round(2)
    pnl.index = hm[win].values
    pnl.name = day
    return pnl


def main() -> None:
    rows = []
    paths = {}
    for day in sys.argv[1:]:
        try:
            p = path(day)
        except Exception as exc:  # noqa: BLE001
            print(day, "failed:", repr(exc)[:100])
            continue
        if p is None or p.empty:
            continue
        paths[day] = p
        r = {"day": day, "close": p.iloc[-1], "mfe": p.max(), "mfe_hm": p.idxmax(), "mae": p.min(), "mae_hm": p.idxmin()}
        for stop, target in RULES:
            hit_t = p[p >= target]
            hit_s = p[p <= -stop]
            ft = hit_t.index[0] if len(hit_t) else None
            fs = hit_s.index[0] if len(hit_s) else None
            if ft is not None and (fs is None or ft < fs):
                r[f"s{stop}/t{target}"] = target
            elif fs is not None:
                r[f"s{stop}/t{target}"] = -stop
            else:
                r[f"s{stop}/t{target}"] = p.iloc[-1]
        rows.append(r)
    D = pd.DataFrame(rows).set_index("day")
    pd.set_option("display.width", 250)
    print("== the 10:00 straddle: P&L at the close, the best and worst point on the way (points, before costs)")
    print(D[["close", "mfe", "mfe_hm", "mae", "mae_hm"]].to_string())
    print("\n== a stop / target in points, checked every minute, against holding to the close (sum and mean over the days)")
    cols = ["close"] + [f"s{s}/t{t}" for s, t in RULES]
    S = D[cols]
    print(S.to_string())
    print("\nsum   ", S.sum().round(1).to_dict())
    print("mean  ", S.mean().round(1).to_dict())
    print("worst ", S.min().round(1).to_dict())
    print("hits: target / stop / close per rule:", {c: (int((S[c] == int(c.split("t")[1])).sum()), int((S[c] == -int(c.split("/")[0][1:])).sum())) for c in cols[1:]})
    print("\n== the path by half hour (points)")
    P = pd.DataFrame({d: p[[h for h in p.index if h[-2:] in ("00", "30") or h == "15:29"]] for d, p in paths.items()}).T
    print(P.to_string())


if __name__ == "__main__":
    main()
