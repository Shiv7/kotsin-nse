"""The 16-state study: next-day and 3-day market-neutral moves after each state, on the exploration
period only (to 2025-09-30). Run: uv run python -m kotsin_nse.research.oi16_study (KN_DATA_DIR from .env)."""
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.market.fo_bhavcopy import OiDailyStore
from kotsin_nse.market.session import ist_day
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.research.oi16 import STATES, state_table

root = Path('data-live/oi_daily')
hs = HistoryStore(Path('data-live/history'))
t=state_table(OiDailyStore(root), root)
EXPLORE_END='2025-09-30'
rows=[]
for sym in t.symbol.unique():
    d1=hs.load(sym,'1d')
    if d1.empty:
        continue
    d1 = d1.sort_values('ts').copy()
    d1['day'] = [ist_day(int(x)).isoformat() for x in d1.ts]
    d1=d1.drop_duplicates('day',keep='last')
    adr=(d1.h-d1.l).rolling(50,min_periods=25).mean()
    d1['adr_pct']=adr/d1.c
    d1['r1']=(d1.c.shift(-1)/d1.c-1)/d1.adr_pct
    d1['r3']=(d1.c.shift(-3)/d1.c-1)/d1.adr_pct
    d1['symbol']=sym
    rows.append(d1[['symbol','day','r1','r3']])
fw=pd.concat(rows)
x=t.merge(fw,on=['symbol','day']).dropna(subset=['r1'])
x=x[x.day<=EXPLORE_END]
for c in ('r1','r3'):
    x[c+'x']=x[c]-x.groupby('day')[c].transform('mean')   # market-neutral
name={v[0]:(k[0],k[1],k[2],v[1]) for k,v in STATES.items()}
print(f"explore {x.day.min()}..{x.day.max()}: {len(x):,} stock-days, {x.symbol.nunique()} stocks, {x.day.nunique()} sessions\n")
print(f"{'#':>2} {'futures':15s} {'call':4s} {'put':4s} {'n':>6} | {'next day':>8} {'t':>5} {'up%':>5} | {'3 days':>7} {'t':>5} | reading")
out=[]
for s in range(1,17):
    g=x[x.state==s]
    def tstat(col, g=g):
        per = g.groupby('day')[col].mean()  # one number per session: days are the independent unit
        return per.mean()/(per.std()/np.sqrt(len(per))) if len(per)>2 and per.std()>0 else np.nan
    f,cu,pu,rd=name[s]
    out.append((s,f,cu,pu,len(g),g.r1x.mean(),tstat('r1x'),100*(g.r1x>0).mean(),g.r3x.mean(),tstat('r3x'),rd))
for s,f,cu,pu,n,m1,t1,up,m3,t3,rd in out:
    print(f"{s:>2} {f:15s} {'↑' if cu else '↓':4s} {'↑' if pu else '↓':4s} {n:>6} | {m1:+8.3f} {t1:+5.1f} {up:5.1f} | {m3:+7.3f} {t3:+5.1f} | {rd}")
pd.DataFrame(out,columns=['state','futures','call_up','put_up','n','next_day_adr','t1','up_pct','three_day_adr','t3','reading']).to_csv('data-live/oi_daily/_16state_explore.csv',index=False)
