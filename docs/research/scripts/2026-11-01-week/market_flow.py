"""market-wide order flow (all equity codes) around each trade's entry, and by half hour per day"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd, numpy as np
B=Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"); IST=timezone(timedelta(hours=5,minutes=30))
def hm(ts): return datetime.fromtimestamp(int(ts),IST).strftime("%H:%M")
FILES={"2026-10-27":["2026-10-27.json","2026-10-27-replay-loop1.json"],"2026-10-29":["2026-10-29.json"],"2026-10-30":["2026-10-30.json"]}
for day,fns in FILES.items():
    bars=pd.read_parquet(B/"archive"/"bars"/f"{day}.parquet",columns=["symbol","scrip_code","ts","c","source"]); bars=bars[bars.source!="partial"]
    eq=set(bars.scrip_code.astype(str).unique())  # equity codes in the bars archive (the cash legs)
    fl=pd.read_parquet(B/"archive"/"flow"/f"{day}.parquet",columns=["code","ts","buy","sell","delta","ofi","qi","wimb"])
    fl["code"]=fl.code.astype(str); eqf=fl[fl.code.isin(eq)]
    m=eqf.groupby("ts").agg(buy=("buy","sum"),sell=("sell","sum")).sort_index(); m["share"]=(m.buy-m.sell)/(m.buy+m.sell).replace(0,np.nan)
    m["hh"]=[hm(t) for t in m.index]; m["slot"]=m.hh.str[:4]+("0" if True else "")
    m["slot"]=m.hh.map(lambda s: s[:3]+("00" if int(s[3:])<30 else "30"))
    g=m.groupby("slot").agg(buy=("buy","sum"),sell=("sell","sum")); g["b/s"]=(g.buy/g.sell).round(2)
    n=bars[bars.symbol=="NIFTY"].sort_values("ts"); n["slot"]=n.ts.map(lambda t: hm(t)[:3]+("00" if int(hm(t)[3:])<30 else "30"))
    nn=n.groupby("slot").c.last().round(0)
    print(f"\n== {day} market-wide equity buy/sell by half hour (NIFTY last):"); print(pd.DataFrame({"b/s":g["b/s"],"nifty":nn}).T.to_string())
    seen=set()
    for fn in fns:
        d=json.load(open(B/"strategy"/fn))
        for t in d["trades"]:
            if t["id"] in seen: continue
            seen.add(t["id"]); ts=int(t["opened_ts"])
            w=m[(m.index>ts-900)&(m.index<=ts)]; w60=m[(m.index>ts-3600)&(m.index<=ts)]
            nif=n[n.ts<ts]; npos=(nif.c.iloc[-1]-nif.c.min())/max(nif.c.max()-nif.c.min(),1e-9)
            tag="live" if "replay" not in fn else "replay"
            print(f"  {tag:6} {t['symbol']:11} {t['side'][0]} {hm(ts-300)}  bps {t['pnl_bps']:+6.1f}  market b/s last15 {w.buy.sum()/max(w.sell.sum(),1):.2f}  last60 {w60.buy.sum()/max(w60.sell.sum(),1):.2f}  NIFTY pos {npos:.2f}")
