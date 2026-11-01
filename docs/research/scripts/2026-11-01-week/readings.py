"""readings at the entry for any day file (live or replay): board row, last-15-min flow, NIFTY position, official prev build-up"""
import json, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
B = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"); D = B / "archive"
IST = timezone(timedelta(hours=5, minutes=30))
def hm(ts): return datetime.fromtimestamp(int(ts), IST).strftime("%H:%M")
def official_label(sym, day):
    days = sorted(p.stem for p in (B / "oi_daily").glob("2*.parquet")); prev = [d for d in days if d < day]
    if len(prev) < 2: return "?"
    def fut(d):
        t = pd.read_parquet(B / "oi_daily" / f"{d}.parquet", columns=["symbol", "kind", "expiry", "close", "oi"])
        return t[(t["symbol"] == sym) & (t["kind"] == "FUTSTK")].sort_values("expiry")
    f, fp = fut(prev[-1]), fut(prev[-2])
    if f.empty or fp.empty: return "no future"
    oi, oip = float(f["oi"].sum()), float(fp["oi"].sum()); e = f["expiry"].iloc[0]
    px = float(f["close"].iloc[0]); m = fp[fp["expiry"] == e]
    pxp = float(m["close"].iloc[0]) if len(m) else float("nan")
    d_oi, d_px = (oi / oip - 1) * 100, (px / pxp - 1) * 100
    lab = ("long build-up" if d_px > 0 else "short build-up") if d_oi > 1 else (("short covering" if d_px > 0 else "long unwinding") if d_oi < -1 else "flat OI")
    return f"{lab} ({prev[-1][5:]}: OI {d_oi:+.1f}%, px {d_px:+.1f}%)"
rows = []
for spec in sys.argv[1:]:
    day, fn = spec.split("=")
    have = (D / "fno" / f"{day}.parquet").exists()
    if have:
        fno = pd.read_parquet(D / "fno" / f"{day}.parquet")
        flow = pd.read_parquet(D / "flow" / f"{day}.parquet", columns=["code", "ts", "delta", "ofi", "qi", "buy", "sell"])
        bars = pd.read_parquet(D / "bars" / f"{day}.parquet", columns=["symbol", "scrip_code", "ts", "h", "l", "c", "source"]); bars = bars[bars.source != "partial"]
        codes = bars.drop_duplicates("symbol").set_index("symbol")["scrip_code"].astype(str); nifty = bars[bars.symbol == "NIFTY"].sort_values("ts")
    d = json.load(open(B / "strategy" / fn))
    for t in d["trades"]:
        ts = int(t["opened_ts"]); sym = t["symbol"]; r = None; fl = pd.DataFrame(); npos = float("nan")
        if have:
            k = fno[(fno.symbol == sym) & (fno.ts <= ts)].sort_values("ts"); r = k.iloc[-1] if len(k) else None
            code = str(codes.get(sym, "-1")); fl = flow[(flow.code.astype(str) == code) & (flow.ts > ts - 900) & (flow.ts <= ts)]
            n = nifty[nifty.ts < ts]; npos = (n.c.iloc[-1] - n.l.min()) / max(n.h.max() - n.l.min(), 1e-9) if len(n) else float("nan")
        g = lambda key, f=lambda x: x: (f(r[key]) if r is not None and pd.notna(r[key]) else None)
        rows.append({"day": day[5:], "src": "replay" if "replay" in fn else "live", "sym": sym, "side": t["side"], "entry": hm(ts - 300), "exit": t["exit_reason"], "bps": t["pnl_bps"],
                     "fut": g("futures_state"), "fut_oi%": g("futures_oi_chg_pct", lambda x: round(float(x), 1)), "calls": g("calls_state"), "puts": g("puts_state"), "bias": g("bias"),
                     "delta15": round(float(fl.delta.sum())) if len(fl) else None, "b/s15": round(float(fl.buy.sum() / max(fl.sell.sum(), 1)), 2) if len(fl) else None,
                     "nifty_pos": round(npos, 2) if npos == npos else None, "official_prev": official_label(sym, day)})
df = pd.DataFrame(rows); pd.set_option("display.width", 250); print(df.to_string(index=False))
