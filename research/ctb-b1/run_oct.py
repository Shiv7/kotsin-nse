"""Replay every October B1 fade (triggers_oct.csv) on real option candles with the sheet's ladder; strict and lenient
reads, and a grid of trail widths. Run from kotsin-nse/backend:  uv run --with openpyxl python ~/Downloads/kotsin/ctb-backtest/run_oct.py"""
from __future__ import annotations
import csv, glob, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ladder import replay
from sheet import raw_pivots, to_tick
from run import charges

HERE = Path(__file__).resolve().parent
MASTER = Path("data-live/scripmaster/nse_fo-2026-10-20.csv")
BROKERAGE_PER_ORDER = 20.0

def master():
    m = {}
    for r in csv.DictReader(open(MASTER)):
        if r["ScripType"] in ("CE", "PE") and r["Expiry"].startswith("2026-10-27"):
            m[(r["SymbolRoot"], float(r["StrikeRate"]), r["ScripType"])] = (r["ScripCode"], int(r["LotSize"]))
    return m

def load(tag, interval):
    fs = glob.glob(str(HERE / "data" / f"{tag}_*_{interval}.parquet"))
    if not fs: return None
    d = pd.read_parquet(fs[0]); d["day"] = d["dt"].str[:10]; return d

def one(t, m, lenient, dip_close, dip_hard):
    k = float(t["fade_strike"]); code, lot = m[(t["symbol"], k, t["fade_side"])]
    tag = f"{t['symbol'].replace('&','and')}_{k:g}{t['fade_side']}"
    d1, m1 = load(tag, "1d"), load(tag, "1m")
    if d1 is None or m1 is None: return {"status": "no data"}
    day = m1[m1["day"] == t["day"]].assign(hm=lambda d: d["dt"].str[11:16])
    at = day[day["hm"] >= "09:45"]
    if at.empty: return {"status": "no 09:45 candle"}
    entry = float(at["o"].iloc[0]); entry_hm = str(at["hm"].iloc[0])
    piv = [to_tick(p) for p, _ in raw_pivots(d1, t["day"]) if to_tick(p) > entry]
    res = replay(day, entry, piv, entry_hm, lenient=lenient, dip_close=dip_close, dip_hard=dip_hard)
    buy = entry * lot * 4; sell = res.revenue(lot)
    ch = charges(buy, sell, BROKERAGE_PER_ORDER * (1 + len(res.fills)))
    return {"status": "ok", "option": f"{t['symbol']} {k:g} {t['fade_side']}", "lot": lot, "entry": entry, "entry_hm": entry_hm,
            "targets": ", ".join(f"{x:g}" for x in res.targets), "armed": res.armed_hm or "never", "peak": res.peak if res.armed_hm else None,
            "day_high": float(at["h"].max()), "day_low": float(at["l"].min()),
            "fills": "; ".join(f"{f.lots}L @{f.price:g} {f.hm} ({f.why})" for f in res.fills),
            "invested": buy, "revenue": sell, "charges": ch["total"], "net": sell - buy - ch["total"], "net_pct": (sell - buy - ch["total"]) / buy * 100}

def main():
    m = master()
    fades = [t for t in csv.DictReader(open(HERE / "triggers_oct.csv")) if t["b1_take"] == "True" and t["fade_strike"]]
    base = ["day", "symbol", "trigger", "grade", "gap_official", "conviction"]
    tabs = {}
    for name, lenient, dc, dh in [("strict 3-5", False, .03, .05), ("lenient 3-5", True, .03, .05), ("strict 5-8", False, .05, .08),
                                  ("strict 8-12", False, .08, .12), ("strict 10-15", False, .10, .15), ("strict 15-20", False, .15, .20)]:
        rows = [{**{k: t[k] for k in base}, **one(t, m, lenient, dc, dh)} for t in fades]
        tabs[name] = pd.DataFrame(rows)
    summ = []
    for name, df in tabs.items():
        ok = df[df.status == "ok"]
        w = (ok.net > 0).sum()
        summ.append({"variant": name, "trades": len(ok), "wins": int(w), "win_rate": w / len(ok), "invested": ok.invested.sum(), "net": ok.net.sum(),
                     "net_pct": ok.net.sum() / ok.invested.sum() * 100, "avg_net": ok.net.mean(), "best": ok.net.max(), "worst": ok.net.min(),
                     "profit_factor": ok.net[ok.net > 0].sum() / max(1e-9, -ok.net[ok.net < 0].sum()),
                     "largest_share_of_profit": (ok.net.max() / ok.net.sum()) if ok.net.sum() > 0 else None})
    out = HERE / "CTB_B1_october.xlsx"
    with pd.ExcelWriter(out) as xw:
        pd.DataFrame(summ).to_excel(xw, sheet_name="summary", index=False)
        for name, df in tabs.items(): df.to_excel(xw, sheet_name=name, index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 60)
    print(pd.DataFrame(summ).to_string(index=False))
    s = tabs["strict 3-5"]
    print("\nSTRICT 3-5 per trade"); print(s[["day","symbol","grade","gap_official","conviction","option","entry","armed","peak","day_high","net","net_pct","status"]].to_string(index=False))
    print("\nby conviction (strict 3-5):"); ok = s[s.status=="ok"].copy(); ok["conv3"] = ok.conviction.astype(float) >= 3
    print(ok.groupby("conv3").agg(trades=("net","count"), net=("net","sum"), wins=("net", lambda x: (x>0).sum())).to_string())
    print("wrote", out)

main()
