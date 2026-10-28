"""Copy the running engine's REST-seeded bars for a day into the research history cache (data-live/history/<tf>/<SYM>.parquet),
the same merge semantics as research/history.py (sort by ts, last duplicate wins). The archive (what the socket delivered) is left alone."""
import json, sys, urllib.request
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
from kotsin_nse.config import Segment
from kotsin_nse.market.session import session_open_ts

DAY = sys.argv[1]; API = "http://localhost:8500"
H = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/history")
d = date.fromisoformat(DAY); day_start = session_open_ts(Segment.NSE_EQ, d) - 9.25 * 3600; day_end = day_start + 86400
codes = pd.read_parquet("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive/bars/2026-10-27.parquet", columns=["symbol"]).symbol.unique()
def get(sym, tf, n):
    with urllib.request.urlopen(f"{API}/api/bars/{sym}?tf={tf}&n={n}", timeout=30) as r:
        return json.load(r)["bars"]
added = {"1m": 0, "5m": 0, "30m": 0, "1d": 0}; syms = 0
for sym in sorted(codes):
    try:
        pulls = {"1m": get(sym, "1m", 400), "5m": get(sym, "5m", 100), "30m": get(sym, "30m", 40), "1d": get(sym, "1d", 10)}
    except Exception as e:  # noqa: BLE001
        print("skip", sym, str(e)[:60]); continue
    syms += 1
    for tf, bars in pulls.items():
        rows = [b for b in bars if b["source"] == "rest" and (tf in ("30m", "1d") or day_start <= b["ts"] < day_end)]
        if not rows:
            continue
        new = pd.DataFrame([{"ts": int(b["ts"]), "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"]), "v": float(b["v"])} for b in rows])
        p = H / tf / f"{sym}.parquet"; p.parent.mkdir(parents=True, exist_ok=True)
        old = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=new.columns)
        before = len(old)
        merged = pd.concat([old, new], ignore_index=True).sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
        merged.to_parquet(p, index=False)
        added[tf] += len(merged) - before
print("symbols", syms, "| rows added by tf:", added)
