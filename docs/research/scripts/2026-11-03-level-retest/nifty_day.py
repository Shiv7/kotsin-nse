"""NIFTY, one session, everything the archive holds — the owner's study (3 Nov 2026: "learn one stock … nifty only …
future, option, index tick by tick … volume, market profile, ask bid, delta, PCR, will study together").
Index ticks → 1m/5m; the front future's tape (volume, spread), book (5-level imbalance), delta/OFI, OI, basis; the option
chains per strike (OI through the day, walls, PCR, straddle, IV, max pain); the future's market profile (30-min TPO
letters, IB, POC, value area, single prints) and volume profile; the index's 5m structure (swings, BOS, order blocks, gaps).
Usage: nifty_day.py OUT_DIR 2026-11-03
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.structure.engine import Bar5, analyze

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IDX = "999920000"


def hm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def master_for(day: str) -> pd.DataFrame:
    fs = sorted(p for p in (DD / "scripmaster").glob("nse_fo-*.csv") if p.name[7:17] <= day)
    m = pd.read_csv(fs[-1], dtype={"ScripCode": str}, low_memory=False)
    return m[m.SymbolRoot == "NIFTY"]


def ohlc(t: pd.DataFrame, sec: int) -> pd.DataFrame:
    d = t.copy()
    d["s"] = ((d.ts + 19800) // sec) * sec - 19800
    g = d.groupby("s").agg(o=("ltp", "first"), h=("ltp", "max"), l=("ltp", "min"), c=("ltp", "last"), n=("ltp", "size"))
    g.index = [hm(x) for x in g.index]
    return g


def main() -> None:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    D = sys.argv[2]
    m = master_for(D)
    fut = m[(m.ScripType == "XX") & (m.Expiry >= D)].sort_values("Expiry").iloc[0]
    FC, LOT = str(fut.ScripCode), int(fut.LotSize)
    opts = m[m.ScripType.isin(["CE", "PE"])]
    tk = pd.read_parquet(DD / "archive" / "ticks" / f"{D}.parquet")
    tk["hm"] = [hm(x) for x in tk.ts]
    sess = tk[(tk.hm >= "09:15") & (tk.hm < "15:30")]
    ix = sess[sess.scrip_code == IDX].sort_values("ts")
    ft = sess[sess.scrip_code == FC].sort_values("ts").copy()
    print(f"== NIFTY {D}: index frames {len(ix):,}; front future {fut.Name} ({FC}, lot {LOT}) frames {len(ft):,}")
    i5 = ohlc(ix, 300)
    print(f"index: open {i5.o.iloc[0]:.1f} high {i5.h.max():.1f} low {i5.l.min():.1f} close {i5.c.iloc[-1]:.1f} ({(i5.c.iloc[-1] / i5.o.iloc[0] - 1) * 100:+.2f} %)")
    i30 = ohlc(ix, 1800)
    print("index 30m:"); print(i30[["o", "h", "l", "c"]].round(1).to_string())
    # the future's tape: volume per minute from total_qty increments, spread, basis, book imbalance, delta
    ft["vol"] = ft.total_qty.diff().clip(lower=0).fillna(0)
    ft["spread"] = ft.ask - ft.bid
    ft["m"] = ((ft.ts + 19800) // 60) * 60 - 19800
    fm = ft.groupby("m").agg(px=("ltp", "last"), vol=("vol", "sum"), spread=("spread", "median"), frames=("ltp", "size"))
    ixm = ix.assign(m=((ix.ts + 19800) // 60) * 60 - 19800).groupby("m").ltp.last()
    fm["basis"] = fm.px - ixm.reindex(fm.index)
    dp = pd.read_parquet(DD / "archive" / "depth" / f"{D}.parquet")
    dp = dp[dp.scrip_code == FC].copy()
    dp["m"] = ((dp.ts + 19800) // 60) * 60 - 19800
    dp["bid"] = dp[["b1q", "b2q", "b3q", "b4q", "b5q"]].sum(axis=1); dp["ask"] = dp[["a1q", "a2q", "a3q", "a4q", "a5q"]].sum(axis=1)
    bk = dp.groupby("m").agg(bid=("bid", "mean"), ask=("ask", "mean"), b1=("b1q", "mean"), a1=("a1q", "mean"))
    fm = fm.join(bk)
    fm["imb"] = (fm.bid - fm.ask) / (fm.bid + fm.ask)
    fl = pd.read_parquet(DD / "archive" / "flow" / f"{D}.parquet")
    fl = fl[fl.code == FC].set_index("ts")[["delta", "cum_delta", "ofi", "buy", "sell"]]
    fm = fm.join(fl, how="left")
    oi = pd.read_parquet(DD / "archive" / "oi" / f"{D}.parquet"); oi["hm"] = [hm(x) for x in oi.ts]
    fo = oi[oi.scrip_code == FC].sort_values("ts")
    fo["m"] = ((fo.ts + 19800) // 60) * 60 - 19800
    fm = fm.join(fo.groupby("m").oi.last().rename("fut_oi"))
    fm["fut_oi"] = fm.fut_oi.ffill()
    fm["hm"] = [hm(x) for x in fm.index]
    fm.to_csv(out / f"nifty-{D}-minutes.csv")
    h = fm.copy(); h["s"] = h.hm.str[:2] + ":" + np.where(h.hm.str[3:] < "30", "00", "30")
    hh = h.groupby("s").agg(px=("px", "last"), vol=("vol", "sum"), delta=("delta", "sum"), ofi=("ofi", "sum"), imb=("imb", "mean"), spread=("spread", "median"), basis=("basis", "median"), fut_oi=("fut_oi", "last"), frames=("frames", "sum"))
    hh["oi_chg_pct"] = (hh.fut_oi / hh.fut_oi.iloc[0] - 1) * 100
    print("\n== the future by half hour: price, volume (lots×65), delta, OFI, book imbalance, spread, basis, OI change"); print(hh.round(2).to_string())
    # the chains
    print("\n== option chains")
    snaps = ["09:16", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "15:29"]
    for e in sorted(opts.Expiry.unique()):
        oe = opts[opts.Expiry == e]
        codes = set(oe.ScripCode)
        seen = oi[oi.scrip_code.isin(codes)]
        if seen.empty:
            continue
        rows = []
        for r in oe.itertuples():
            o = seen[seen.scrip_code == r.ScripCode].sort_values("ts")
            if o.empty:
                continue
            rec = {"strike": float(r.StrikeRate), "type": r.ScripType}
            for t in snaps:
                x = o[o.hm <= t]
                rec[t] = float(x.oi.iloc[-1]) / 1e5 if len(x) else np.nan
            tq = sess[sess.scrip_code == r.ScripCode]
            rec["frames"] = len(tq); rec["ltp_close"] = float(tq.ltp.iloc[-1]) if len(tq) else np.nan
            rows.append(rec)
        C = pd.DataFrame(rows).sort_values(["type", "strike"])
        print(f"\n-- expiry {e}: {C.strike.nunique()} strikes seen, OI in lakh contracts at", snaps)
        ce = C[C.type == "CE"].set_index("strike"); pe = C[C.type == "PE"].set_index("strike")
        tab = pd.DataFrame({"CE 09:16": ce["09:16"], "CE 12:00": ce["12:00"], "CE 15:29": ce["15:29"], "PE 09:16": pe["09:16"], "PE 12:00": pe["12:00"], "PE 15:29": pe["15:29"], "CE ltp": ce.ltp_close, "PE ltp": pe.ltp_close})
        print(tab.round(1).to_string())
        for t in snaps:
            c_, p_ = ce[t].dropna(), pe[t].dropna()
            if len(c_) and len(p_):
                strikes = sorted(set(c_.index) | set(p_.index))
                pain = min(strikes, key=lambda k: sum(max(0.0, k2 - k) * c_.get(k2, 0) for k2 in c_.index) + sum(max(0.0, k - k2) * p_.get(k2, 0) for k2 in p_.index))
                print(f"   {t}: call wall {c_.idxmax():.0f} ({c_.max():.0f}L)  put wall {p_.idxmax():.0f} ({p_.max():.0f}L)  PCR {p_.sum() / c_.sum():.2f}  max pain {pain:.0f}")
        C.to_csv(out / f"nifty-{D}-chain-{e}.csv", index=False)
    fn = pd.read_parquet(DD / "archive" / "fno" / f"{D}.parquet"); fn = fn[fn.symbol == "NIFTY"].sort_values("ts"); fn["hm"] = [hm(x) for x in fn.ts]
    print("\n== the board (tracked expiry): time, bias, futures state, IV, straddle, implied move, PCR, walls")
    for t in snaps:
        x = fn[fn.hm <= t]
        if len(x):
            x = x.iloc[-1]
            print(f"   {t}: {x.bias} fut {x.futures_state} ({x.futures_oi_chg_pct:+.2f} %) IV {x.x_iv:.1f} straddle {x.x_straddle:.0f} ({x.x_implied_move_pct:.2f} %) PCR {x.x_pcr_oi:.2f} cw {x.x_call_wall:.0f} pw {x.x_put_wall:.0f} calls {x.calls_state} puts {x.puts_state}")
    # market profile of the future: 30-min TPO letters over 5-point bins
    step = 5.0
    ft["letter"] = [chr(65 + int(((t + 19800) % 86400 - (9 * 3600 + 15 * 60)) // 1800)) for t in ft.ts]
    ft["bin"] = (ft.ltp // step) * step
    tpo = ft.groupby("bin").letter.apply(lambda s: "".join(sorted(set(s))))
    vol = ft.groupby("bin").vol.sum()
    prof = pd.DataFrame({"tpo": tpo, "n": tpo.str.len(), "vol": vol}).sort_index(ascending=False)
    poc = prof.n.idxmax() if len(prof) else np.nan
    tot = prof.n.sum(); cum = 0; va = []
    for b, r in prof.sort_values("n", ascending=False).iterrows():
        va.append(b); cum += r.n
        if cum >= 0.7 * tot:
            break
    ib = ft[ft.letter.isin(["A", "B"])]
    print(f"\n== market profile of the future (5-pt bins, letters A=09:15 … M=15:15): IB {ib.ltp.min():.0f}–{ib.ltp.max():.0f}, POC {poc:.0f}, value area {min(va):.0f}–{max(va):.0f}, "
          f"single prints {[int(b) for b, r in prof.iterrows() if r.n == 1]}")
    print(prof.to_string())
    # structure on the index 5m
    bars = [Bar5(int(ts), r.o, r.h, r.l, r.c, 0.0) for ts, r in ix.assign(s=((ix.ts + 19800) // 300) * 300 - 19800).groupby("s").agg(o=("ltp", "first"), h=("ltp", "max"), l=("ltp", "min"), c=("ltp", "last")).iterrows()]
    st = analyze(bars)
    print("\n== index 5m structure: swings", [(hm(s.ts), s.kind, round(float(s.price), 1), "taken" if s.taken_at else "") for s in st.swings])
    print("   zones:", [(z.side, hm(z.ob_ts), round(float(z.low), 1), round(float(z.high), 1), z.state) for z in st.zones])
    print("   gaps unfilled:", [(hm(g.ts), g.side, round(float(g.low), 1), round(float(g.high), 1)) for g in st.gaps if g.filled_at is None])


if __name__ == "__main__":
    main()
