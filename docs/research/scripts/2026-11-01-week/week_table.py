"""the week under the CURRENT rules: 27/28 replay-loop1, 29/30 live; per day funnel + trades at ₹1L, with NIFTY context"""
import json, collections
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd, numpy as np
B=Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"); IST=timezone(timedelta(hours=5,minutes=30))
def hm(ts): return datetime.fromtimestamp(int(ts),IST).strftime("%H:%M")
CH=127.0  # charges per round trip at ₹1L (30 Oct live: ₹124 and ₹130)
FILES=[("2026-10-27","2026-10-27-replay-loop1.json","replay"),("2026-10-28","2026-10-28-replay-loop1.json","replay"),("2026-10-29","2026-10-29.json","live"),("2026-10-30","2026-10-30.json","live")]
# NIFTY 5m per day: archive (27/29/30) or research cache (28)
def nifty(day):
    p=B/"archive"/"bars"/f"{day}.parquet"
    if p.exists():
        b=pd.read_parquet(p,columns=["symbol","ts","o","h","l","c","source"]); b=b[(b.symbol=="NIFTY")&(b.source!="partial")].sort_values("ts")
    else:
        b=pd.read_parquet(B/"history"/"5m"/"NIFTY.parquet").sort_values("ts"); b=b[b.ts.map(lambda t: datetime.fromtimestamp(int(t),IST).date().isoformat())==day]
    return b
rows=[]; funnel=[]
for day,fn,src in FILES:
    d=json.load(open(B/"strategy"/fn)); n=nifty(day)
    no,nc,nh,nl=n.o.iloc[0],n.c.iloc[-1],n.h.max(),n.l.min()
    st=d["stages"]; c=collections.Counter()
    for s in st:
        for k,lab in (("i_os","open"),("i_bos","bos"),("i_brk","through"),("i_rt","retest"),("j","confirm")): c[lab]+= s.get(k) is not None
    sig=d["signals"]; tr=d["trades"]
    funnel.append({"day":day[5:],"src":src,"nifty_o→c_bps":round(np.log(nc/no)*1e4),"nifty_close_pos":round((nc-nl)/(nh-nl),2),"symbols":len(st),**{k:c[k] for k in ("open","bos","through","retest","confirm")},"signals":len(sig),"taken":sum(1 for s in sig if s["taken"]),"skipped":[f"{s['symbol']}:{(s.get('why') or '')[:28]}" for s in sig if not s["taken"]]})
    for t in tr:
        ts=int(t["opened_ts"]); k=n[n.ts<ts]; npos=(k.c.iloc[-1]-k.l.min())/max(k.h.max()-k.l.min(),1e-9) if len(k) else np.nan
        nifty_then=k.c.iloc[-1] if len(k) else np.nan
        v=t.get("virtual") or {}
        rows.append({"day":day[5:],"src":src,"sym":t["symbol"],"side":t["side"][0],"entry_bar":t.get("entry_bar") or hm(ts-300),"entry":t["entry"],"risk_bps":t["risk_bps"],"wall":f"{t.get('target_name')} {round(t['target'],2) if t.get('target') else ''}","reward_bps":t.get("reward_bps"),
                     "exit_bar":hm(int(t["closed_ts"])-300) if t.get("closed_ts") else None,"how":t["exit_reason"],"bps":t["pnl_bps"],"mfe":t.get("mfe_bps"),"mae":t.get("mae_bps"),"held":t.get("bars_held"),
                     "fixed_wall":(v.get("fixed_wall") or {}).get("pnl_bps"),"fixed_1to2":(v.get("fixed_1to2") or {}).get("pnl_bps"),
                     "net_1L":round(t["pnl_bps"]/1e4*1e5-CH),"nifty_pos":round(npos,2),"nifty_rest_bps":round(np.log(nc/nifty_then)*1e4) if nifty_then==nifty_then else None,"with_nifty_day":(t["side"][0]=="L")==(nc>no)})
F=pd.DataFrame(funnel); T=pd.DataFrame(rows); pd.set_option("display.width",260); pd.set_option("display.max_colwidth",60)
print(F.to_string(index=False)); print(); print(T.to_string(index=False))
print("\n== week under current rules:",len(T),"trades, mean",round(T.bps.mean(),1),"bps, win",f"{(T.bps>0).mean():.0%}","sum net at ₹1L",int(T.net_1L.sum()),"| fixed_wall mean",round(T.fixed_wall.mean(),1),"| fixed_1to2 mean",round(T.fixed_1to2.mean(),1))
print("by day:",T.groupby("day").agg(n=("bps","size"),mean=("bps","mean"),net_1L=("net_1L","sum")).round(1).to_dict("index"))
print("by side:",T.groupby("side").agg(n=("bps","size"),mean=("bps","mean"),win=("bps",lambda x:(x>0).mean())).round(2).to_dict("index"))
print("with NIFTY's day direction:",T.groupby("with_nifty_day").agg(n=("bps","size"),mean=("bps","mean"),win=("bps",lambda x:(x>0).mean())).round(2).to_dict("index"))
print("by exit:",T.groupby("how").agg(n=("bps","size"),mean=("bps","mean")).round(1).to_dict("index"))
print("entry hour:",T.groupby(T.entry_bar.str[:2]).agg(n=("bps","size"),mean=("bps","mean")).round(1).to_dict("index"))
print("stopped within 6 bars:",int(((T.how=="stop")&(T.held<=6)).sum()),"of",int((T.how=="stop").sum()),"stops; mfe of stops:",T[T.how=="stop"].mfe.round(0).tolist())
print("NIFTY rest-of-day after entry, winners vs losers:",round(T[T.bps>0].nifty_rest_bps.mean(),0),round(T[T.bps<=0].nifty_rest_bps.mean(),0))
T.to_csv("/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/week/trades_current_rules.csv",index=False)
