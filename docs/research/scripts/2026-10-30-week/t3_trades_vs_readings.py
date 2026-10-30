"""T3 — the week's live shadow trades against the engine's readings at the entry (descriptive, n = 14).

For each trade: the board row at the entry minute (futures state, OI change, PCR, IV change, bias), the last
15 minutes of the underlying's flow (delta, OFI, queue imbalance), NIFTY's position in its day range so far,
and the previous session's official build-up from the bhavcopy.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

B = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
D = B / "archive"
IST = timezone(timedelta(hours=5, minutes=30))
FILES = {"2026-10-27": ["2026-10-27.json"], "2026-10-29": ["2026-10-29.json"], "2026-10-30": ["2026-10-30-before-1129-restart.json", "2026-10-30.json"]}


def hm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), IST).strftime("%H:%M")


def official_label(sym: str, day: date) -> str:
    """the previous session's build-up from the bhavcopy: OI and near-future close against the session before"""
    days = sorted(p.stem for p in (B / "oi_daily").glob("2*.parquet"))
    prev = [d for d in days if d < day.isoformat()]
    if len(prev) < 2:
        return "?"
    def fut(d: str) -> pd.DataFrame:
        t = pd.read_parquet(B / "oi_daily" / f"{d}.parquet", columns=["symbol", "kind", "expiry", "close", "oi"])
        return t[(t["symbol"] == sym) & (t["kind"] == "FUTSTK")].sort_values("expiry")
    f, fp = fut(prev[-1]), fut(prev[-2])
    if f.empty or fp.empty:
        return "no future"
    oi, oip = float(f["oi"].sum()), float(fp["oi"].sum())
    px, pxp = float(f["close"].iloc[0]), float(fp[fp["expiry"] == f["expiry"].iloc[0]]["close"].iloc[0]) if (fp["expiry"] == f["expiry"].iloc[0]).any() else float("nan")
    d_oi, d_px = (oi / oip - 1) * 100, (px / pxp - 1) * 100
    if d_oi > 1:
        lab = "long build-up" if d_px > 0 else "short build-up"
    elif d_oi < -1:
        lab = "short covering" if d_px > 0 else "long unwinding"
    else:
        lab = "flat OI"
    return f"{lab} ({prev[-1][5:]}: OI {d_oi:+.1f}%, px {d_px:+.1f}%)"


rows = []
for day, files in FILES.items():
    fno = pd.read_parquet(D / "fno" / f"{day}.parquet")
    flow = pd.read_parquet(D / "flow" / f"{day}.parquet", columns=["code", "ts", "delta", "ofi", "ofi_deep", "qi", "buy", "sell"])
    bars = pd.read_parquet(D / "bars" / f"{day}.parquet", columns=["symbol", "scrip_code", "ts", "h", "l", "c", "source"])
    bars = bars[bars["source"] != "partial"]
    codes = bars.drop_duplicates("symbol").set_index("symbol")["scrip_code"].astype(str)
    nifty = bars[bars["symbol"] == "NIFTY"].sort_values("ts")
    seen = set()
    for fn in files:
        d = json.load(open(B / "strategy" / fn))
        for t in d["trades"]:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            ts = int(t["opened_ts"])
            sym = t["symbol"]
            k = fno[(fno["symbol"] == sym) & (fno["ts"] <= ts)].sort_values("ts")
            r = k.iloc[-1] if len(k) else None
            code = str(codes.get(sym, "-1"))
            fl = flow[(flow["code"].astype(str) == code) & (flow["ts"] > ts - 900) & (flow["ts"] <= ts)]
            n = nifty[nifty["ts"] < ts]
            npos = (n["c"].iloc[-1] - n["l"].min()) / max(n["h"].max() - n["l"].min(), 1e-9) if len(n) else float("nan")
            rows.append({
                "day": day[5:], "sym": sym, "side": t["side"], "entry": t.get("entry_bar") or hm(ts - 300), "exit": t["exit_reason"], "pnl": t["pnl_bps"],
                "fut_state": r["futures_state"] if r is not None else None, "fut_oi%": round(float(r["futures_oi_chg_pct"]), 1) if r is not None and pd.notna(r["futures_oi_chg_pct"]) else None,
                "calls": r["calls_state"] if r is not None else None, "puts": r["puts_state"] if r is not None else None,
                "pcr": round(float(r["x_pcr_oi"]), 2) if r is not None and pd.notna(r["x_pcr_oi"]) else None,
                "iv_chg": round(float(r["x_iv"] - r["x_iv_prev"]), 1) if r is not None and pd.notna(r["x_iv_prev"]) else None,
                "bias": r["bias"] if r is not None else None,
                "delta15": round(float(fl["delta"].sum())) if len(fl) else None, "ofi15": round(float(fl["ofi"].sum())) if len(fl) else None,
                "qi15": round(float(fl["qi"].mean()), 2) if len(fl) else None, "buy/sell": round(float(fl["buy"].sum() / max(fl["sell"].sum(), 1)), 2) if len(fl) else None,
                "nifty_pos": round(npos, 2), "official_prev": official_label(sym, date.fromisoformat(day)),
            })
df = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print(df.to_string(index=False))
print("\n-- wins vs losses by the readings")
df["win"] = df["pnl"] > 0
for col in ("fut_state", "calls", "puts", "bias"):
    print(col, df.groupby(col)["pnl"].agg(["size", "mean"]).round(1).to_dict("index"))
for col in ("delta15", "ofi15", "qi15", "nifty_pos", "iv_chg", "fut_oi%"):
    k = df.dropna(subset=[col])
    print(f"{col}: winners mean {k[k.win][col].mean():.2f} (n {k.win.sum()}), losers mean {k[~k.win][col].mean():.2f} (n {(~k.win).sum()})")
print("official build-up before the trade:", df.groupby(df["official_prev"].str.split(" (", regex=False).str[0])["pnl"].agg(["size", "mean"]).round(1).to_dict("index"))
