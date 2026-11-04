"""NIFTY futures, options, flow and microstructure FACTS from the live archive (data-live/archive: ticks, depth, flow,
oi, fno, bars), every session with a full day of the front future's ticks. Descriptions with counts — nine sessions is
not a test. Per day: the front future by the scripmaster, the index from the archive's 1m bars (REST-refilled), the
chain's walls/PCR/straddle/IV from the engine's per-minute board (fno), per-strike OI from the oi archive.
Usage: live_facts.py OUT_DIR [DAY ...]
"""
from __future__ import annotations

import datetime
import gc
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IDX = "999920000"
HALF = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (15, 45) if (h, m) >= (9, 15) and (h, m) < (15, 30)]


def hm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def bucket(h: str) -> str:
    return max(b for b in HALF if b <= h)


def master_for(day: str) -> pd.DataFrame:
    fs = sorted(p for p in (DD / "scripmaster").glob("nse_fo-*.csv*") if p.name[7:17] <= day)
    m = pd.read_csv(fs[-1], dtype={"ScripCode": str}, low_memory=False)
    return m[m.SymbolRoot == "NIFTY"]


def read(kind: str, day: str, codes: list[str] | None, cols: list[str] | None, col: str = "scrip_code") -> pd.DataFrame:
    f = DD / "archive" / kind / f"{day}.parquet"
    if not f.exists():
        return pd.DataFrame()
    pf = pq.ParquetFile(f)
    want = [c for c in (cols or pf.schema_arrow.names) if c in pf.schema_arrow.names]
    typ = pf.schema_arrow.field(col).type if col in pf.schema_arrow.names else None
    flt = None
    if codes is not None and typ is not None:
        vals = [int(c) for c in codes] if "int" in str(typ) else [str(c) for c in codes]
        flt = [(col, "in", vals)]
    return pq.read_table(f, columns=want, filters=flt).to_pandas()


def m5(df: pd.DataFrame, px: str = "ltp", sec: int = 300) -> pd.DataFrame:
    d = df.copy()
    d["s"] = ((d.ts + 19800) // sec) * sec - 19800
    g = d.groupby("s").agg(o=(px, "first"), h=(px, "max"), l=(px, "min"), c=(px, "last"), n=(px, "size"))
    g["hm"] = [hm(x) for x in g.index]
    return g


def spearman(a: pd.Series, b: pd.Series) -> tuple[float, int]:
    x = pd.concat([a, b], axis=1).dropna()
    return (float(x.iloc[:, 0].rank().corr(x.iloc[:, 1].rank())), len(x)) if len(x) > 5 else (float("nan"), len(x))


def main() -> None:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    days = sys.argv[2:] or sorted(p.name[:10] for p in (DD / "archive" / "ticks").glob("2026-*.parquet") if p.stat().st_size > 5_000_000)
    DAYS, FLOW, TOUCH, SWING, VOLB = [], [], [], [], []
    for D in days:
        m = master_for(D)
        fut = m[(m.ScripType == "XX") & (m.Expiry >= D)].sort_values("Expiry").iloc[0]
        FC, LOT = str(fut.ScripCode), int(fut.LotSize)
        exps = sorted(e for e in m[m.ScripType.isin(["CE", "PE"])].Expiry.unique() if e >= D)
        tk = read("ticks", D, [IDX, FC], ["scrip_code", "ts", "ltp", "total_qty", "last_qty", "bid", "ask", "bid_qty", "ask_qty"])
        if tk.empty:
            continue
        tk["scrip_code"] = tk.scrip_code.astype(str)
        tk["hm"] = [hm(x) for x in tk.ts]
        tk = tk[(tk.hm >= "09:15") & (tk.hm < "15:30")]
        ft = tk[tk.scrip_code == FC].sort_values("ts").copy()
        ix = tk[tk.scrip_code == IDX].sort_values("ts").copy()
        fmin = ft.hm.nunique()
        if fmin < 300:
            print(f"-- {D}: future {FC} has {fmin} minutes of ticks — skipped"); continue
        ab = read("bars", D, None, ["symbol", "ts", "o", "h", "l", "c", "source"], col="symbol")
        nb = ab[ab.symbol == "NIFTY"].sort_values("ts").copy() if not ab.empty else pd.DataFrame()
        nb["hm"] = [hm(x) for x in nb.ts]; nb = nb[(nb.hm >= "09:15") & (nb.hm < "15:30")]
        i1 = nb.set_index("hm")[["o", "h", "l", "c"]]  # index 1m from bars
        i5 = nb.assign(s=((nb.ts + 19800) // 300) * 300 - 19800).groupby("s").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last")); i5["hm"] = [hm(x) for x in i5.index]
        # the future's tape per minute
        ft["vol"] = ft.total_qty.diff().clip(lower=0).fillna(0)
        ft["spread"] = (ft.ask - ft.bid).where((ft.ask > 0) & (ft.bid > 0))
        ft["m"] = ((ft.ts + 19800) // 60) * 60 - 19800
        fm = ft.groupby("m").agg(px=("ltp", "last"), vol=("vol", "sum"), spread=("spread", "median"), frames=("ltp", "size"), big=("last_qty", lambda s: float((s >= 10 * LOT).sum())))
        fm["hm"] = [hm(x) for x in fm.index]
        fm["ix"] = fm.hm.map(i1.c)
        fm["basis"] = fm.px - fm.ix
        dte = (datetime.date.fromisoformat(fut.Expiry) - datetime.date.fromisoformat(D)).days
        # delta from the ticks themselves (the engine's flow archive classified the NOV future's trades only from 4 Nov):
        # a frame at or above the ask is a buy, at or below the bid a sell, inside the quote the tick rule (vs the previous print)
        up = (ft.ltp >= ft.ask) & (ft.ask > 0); dn = (ft.ltp <= ft.bid) & (ft.bid > 0)
        sign = np.where(up, 1, np.where(dn, -1, 0)).astype(float)
        tick = np.sign(ft.ltp.diff().fillna(0)).to_numpy()
        sign = np.where(sign == 0, tick, sign)
        sign = pd.Series(sign, index=ft.index).replace(0, np.nan).ffill().fillna(0)
        ft["delta"] = sign.to_numpy() * ft.vol
        fm["delta"] = ft.groupby("m").delta.sum()
        # the book: five-level imbalance and a top-of-book OFI from the depth frames
        dp = read("depth", D, [FC], ["scrip_code", "ts", "b1p", "b1q", "b2q", "b3q", "b4q", "b5q", "a1p", "a1q", "a2q", "a3q", "a4q", "a5q"])
        dp = dp.sort_values("ts"); dp["hm"] = [hm(x) for x in dp.ts]; dp = dp[(dp.hm >= "09:15") & (dp.hm < "15:30")]
        bq = dp[["b1q", "b2q", "b3q", "b4q", "b5q"]].sum(axis=1); aq = dp[["a1q", "a2q", "a3q", "a4q", "a5q"]].sum(axis=1)
        dp["imb"] = (bq - aq) / (bq + aq)
        pb, pa, qb, qa = dp.b1p.shift(), dp.a1p.shift(), dp.b1q.shift(), dp.a1q.shift()
        dp["ofi"] = ((dp.b1p >= pb) * dp.b1q - (dp.b1p <= pb) * qb - (dp.a1p <= pa) * dp.a1q + (dp.a1p >= pa) * qa).fillna(0)
        dp["m"] = ((dp.ts + 19800) // 60) * 60 - 19800
        dm = dp.groupby("m").agg(imb=("imb", "mean"), ofi=("ofi", "sum"), bspread=("a1p", "median"))
        fm = fm.join(dm[["imb", "ofi"]])
        f5 = i5.set_index("hm")[["o", "h", "l", "c"]].copy()
        fm5 = fm.assign(b5=[hm(((t + 19800) // 300) * 300 - 19800) for t in fm.index]).groupby("b5").agg(vol=("vol", "sum"), spread=("spread", "median"), big=("big", "sum"), delta=("delta", "sum"), imb=("imb", "mean"), ofi=("ofi", "sum"))
        f5 = f5.join(fm5)
        f5["ret"] = (f5.c / f5.c.shift(1) - 1) * 1e4
        for k in (1, 3, 6, 12):
            f5[f"fwd{k}"] = (f5.c.shift(-k) / f5.c - 1) * 1e4
        f5["cum_delta"] = f5.delta.cumsum()
        f5["d"] = D
        FLOW.append(f5.reset_index())
        # swings of the index (5m, local extreme over ±6 bars): delta into vs out of the swing
        c5 = f5.c.to_numpy(); n = len(c5)
        for i in range(6, n - 6):
            lo_ = f5.l.iloc[i]; hi_ = f5.h.iloc[i]
            if lo_ == f5.l.iloc[i - 6 : i + 7].min():
                SWING.append({"d": D, "hm": f5.index[i], "kind": "low", "px": lo_, "delta_in": f5.delta.iloc[i - 5 : i + 1].sum(), "delta_out": f5.delta.iloc[i + 1 : i + 7].sum(), "vol_bar": f5.vol.iloc[i], "vol_med": f5.vol.median(), "imb": f5.imb.iloc[i], "fwd6": f5.fwd6.iloc[i]})
            if hi_ == f5.h.iloc[i - 6 : i + 7].max():
                SWING.append({"d": D, "hm": f5.index[i], "kind": "high", "px": hi_, "delta_in": f5.delta.iloc[i - 5 : i + 1].sum(), "delta_out": f5.delta.iloc[i + 1 : i + 7].sum(), "vol_bar": f5.vol.iloc[i], "vol_med": f5.vol.median(), "imb": f5.imb.iloc[i], "fwd6": f5.fwd6.iloc[i]})
        # the board (tracked expiry) per minute
        fn = read("fno", D, ["NIFTY"], ["symbol", "ts", "fut_oi", "bias", "futures_state", "futures_oi_chg_pct", "x_expiry", "x_atm", "x_straddle", "x_implied_move_pct", "x_iv", "x_pcr_oi", "x_call_wall", "x_put_wall"], col="symbol")
        fn["hm"] = [hm(x) for x in fn.ts]; fn = fn[(fn.hm >= "09:15") & (fn.hm < "15:30")].set_index("hm").sort_index()
        def at(t: str):
            x = fn[fn.index <= t]
            if len(x):
                return x.iloc[-1]
            y = fn[(fn.index > t) & (fn.index <= f"{int(t[:2]) + (1 if int(t[3:]) >= 30 else 0):02d}:{(int(t[3:]) + 30) % 60:02d}")]
            return y.iloc[0] if len(y) else None
        b10, b15, b_open, b_close = at("10:00"), at("15:00"), at("09:20"), at("15:29")
        px10 = float(i1.c[i1.index <= "10:00"].iloc[-1]) if len(i1) else np.nan
        hi_after, lo_after = float(i1.h[i1.index > "10:00"].max()), float(i1.l[i1.index > "10:00"].min())
        close = float(i1.c.iloc[-1]); opn = float(i1.o.iloc[0]); dh, dl = float(i1.h.max()), float(i1.l.min())
        # per-strike OI of the tracked expiry: max pain at 10:00 and 15:00, the biggest adds of the day
        oi = read("oi", D, None, ["scrip_code", "ts", "oi"])
        oi["scrip_code"] = oi.scrip_code.astype(str); oi["hm"] = [hm(x) for x in oi.ts]
        pain = {}; adds = {}
        if b10 is not None and exps:
            e = str(b10.x_expiry) if isinstance(b10.x_expiry, str) else exps[0]
            oe = m[(m.Expiry == e) & m.ScripType.isin(["CE", "PE"])]
            seen = oi[oi.scrip_code.isin(set(oe.ScripCode))].merge(oe[["ScripCode", "StrikeRate", "ScripType"]], left_on="scrip_code", right_on="ScripCode")
            for t in ("10:00", "15:00"):
                x = seen[seen.hm <= t].sort_values("ts").groupby(["ScripType", "StrikeRate"]).oi.last()
                if len(x):
                    ce, pe = x.get("CE", pd.Series(dtype=float)), x.get("PE", pd.Series(dtype=float))
                    ks = sorted(set(ce.index) | set(pe.index))
                    pain[t] = min(ks, key=lambda k: sum(max(0.0, k2 - k) * ce.get(k2, 0) for k2 in ce.index) + sum(max(0.0, k - k2) * pe.get(k2, 0) for k2 in pe.index))
            first = seen[seen.hm <= "09:20"].sort_values("ts").groupby(["ScripType", "StrikeRate"]).oi.last()
            last = seen.sort_values("ts").groupby(["ScripType", "StrikeRate"]).oi.last()
            chg = (last - first.reindex(last.index)).dropna()
            if len(chg):
                for typ in ("CE", "PE"):
                    if typ in chg.index.get_level_values(0):
                        s = chg[typ]; adds[typ] = (float(s.idxmax()), float(s.max()))
        fo = oi[oi.scrip_code == FC].sort_values("ts")
        oi_open = float(fo.oi.iloc[0]) if len(fo) else np.nan; oi_close = float(fo.oi.iloc[-1]) if len(fo) else np.nan
        oi_10 = float(fo[fo.hm <= "10:00"].oi.iloc[-1]) if len(fo[fo.hm <= "10:00"]) else np.nan
        tot = fm.vol.sum()
        for b_, s in fm.groupby(fm.hm.map(bucket)):
            VOLB.append({"d": D, "b": b_, "vol_share": s.vol.sum() / tot if tot else np.nan, "spread": s.spread.median(), "basis": s.basis.median(), "big": s.big.sum(), "frames": s.frames.sum()})
        # lead/lag of the future vs the index (1m returns from ticks' last prints)
        ixm = ix.assign(m=((ix.ts + 19800) // 60) * 60 - 19800).groupby("m").ltp.last()
        fr = fm.px.pct_change(); ir = ixm.reindex(fm.index).pct_change()
        ll = {f"lag{k:+d}": fr.corr(ir.shift(-k)) for k in (-2, -1, 0, 1, 2)}  # corr(fut_t, idx_{t+k}): +k → the future leads
        DAYS.append({"d": D, "fut": fut.Name, "dte": dte, "lot": LOT, "minutes": fmin, "idx_frames": len(ix), "fut_frames": len(ft), "idx_first": ix.hm.iloc[0] if len(ix) else "", "bars_rest": int((nb.source == "rest").sum()),
                     "open": opn, "high": dh, "low": dl, "close": close, "ret_bps": (close / opn - 1) * 1e4, "range_pct": (dh - dl) / opn * 100,
                     "vol_lots": tot / LOT, "basis_open": fm.basis.iloc[:15].median(), "basis_close": fm.basis.iloc[-15:].median(), "basis_med": fm.basis.median(),
                     "spread_med": fm.spread.median(), "delta_day": f5.delta.sum(), "delta_1h": f5.delta.iloc[:12].sum(),
                     "oi_open": oi_open, "oi_10": oi_10, "oi_close": oi_close, "oi_chg_pct": (oi_close / oi_open - 1) * 100 if oi_open else np.nan, "oi_chg_1h_pct": (oi_10 / oi_open - 1) * 100 if oi_open else np.nan,
                     "px10": px10, "ret_after10_bps": (close / px10 - 1) * 1e4 if px10 else np.nan,
                     "cw10": float(b10.x_call_wall) if b10 is not None else np.nan, "pw10": float(b10.x_put_wall) if b10 is not None else np.nan,
                     "cw_open": float(b_open.x_call_wall) if b_open is not None else np.nan, "pw_open": float(b_open.x_put_wall) if b_open is not None else np.nan,
                     "cw_close": float(b_close.x_call_wall) if b_close is not None else np.nan, "pw_close": float(b_close.x_put_wall) if b_close is not None else np.nan,
                     "cw_changes": int((fn.x_call_wall.dropna().diff().fillna(0) != 0).sum()), "pw_changes": int((fn.x_put_wall.dropna().diff().fillna(0) != 0).sum()),
                     "hi_after10": hi_after, "lo_after10": lo_after,
                     "straddle10": float(b10.x_straddle) if b10 is not None else np.nan, "impl10_pct": float(b10.x_implied_move_pct) if b10 is not None else np.nan,
                     "iv_open": float(b_open.x_iv) if b_open is not None else np.nan, "iv_close": float(b_close.x_iv) if b_close is not None else np.nan,
                     "pcr10": float(b10.x_pcr_oi) if b10 is not None else np.nan, "pcr15": float(b15.x_pcr_oi) if b15 is not None else np.nan,
                     "atm10": float(b10.x_atm) if b10 is not None else np.nan, "expiry": str(b10.x_expiry) if b10 is not None else "", "bias10": str(b10.bias) if b10 is not None else "", "fstate10": str(b10.futures_state) if b10 is not None else "",
                     "pain10": pain.get("10:00", np.nan), "pain15": pain.get("15:00", np.nan), "ce_add_k": adds.get("CE", (np.nan, np.nan))[0], "ce_add": adds.get("CE", (np.nan, np.nan))[1], "pe_add_k": adds.get("PE", (np.nan, np.nan))[0], "pe_add": adds.get("PE", (np.nan, np.nan))[1],
                     **ll})
        print(f"-- {D} done: {fut.Name} dte {dte}, {fmin} min, idx ticks from {ix.hm.iloc[0] if len(ix) else 'n/a'}, bars rest {int((nb.source == 'rest').sum())}")
        del tk, ft, ix, oi, dp, ab; gc.collect()
    Dy = pd.DataFrame(DAYS); F = pd.concat(FLOW); S = pd.DataFrame(SWING); V = pd.DataFrame(VOLB)
    Dy.to_parquet(out / "days.parquet"); F.to_parquet(out / "flow5.parquet"); S.to_parquet(out / "swings.parquet"); V.to_parquet(out / "volbuckets.parquet")
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 60)
    print(f"\n== {len(Dy)} sessions: {', '.join(Dy.d)}")
    print(Dy[["d", "fut", "dte", "minutes", "idx_first", "bars_rest", "open", "high", "low", "close", "ret_bps", "range_pct", "vol_lots"]].round(1).to_string(index=False))
    print("\n== FUTURES: basis (future − index, points), OI, delta")
    print(Dy[["d", "dte", "basis_open", "basis_med", "basis_close", "spread_med", "oi_open", "oi_chg_1h_pct", "oi_chg_pct", "delta_1h", "delta_day", "ret_bps", "fstate10", "bias10"]].round(2).to_string(index=False))
    print("  basis per DTE: " + ", ".join(f"dte {int(r.dte)} → {r.basis_med:.0f}" for r in Dy.sort_values('dte').itertuples()))
    print(f"  basis close − open: median {(Dy.basis_close - Dy.basis_open).median():+.1f} pts over {len(Dy)} days; days it rose {(Dy.basis_close > Dy.basis_open).sum()}")
    print(f"  day delta sign = day direction: {int((np.sign(Dy.delta_day) == np.sign(Dy.ret_bps)).sum())} of {len(Dy)}; first-hour delta sign = day direction: {int((np.sign(Dy.delta_1h) == np.sign(Dy.ret_bps)).sum())} of {len(Dy)}")
    print(f"  OI change by 10:00 sign = day direction (long build-up → up): {int((np.sign(Dy.oi_chg_1h_pct) == np.sign(Dy.ret_bps)).sum())} of {len(Dy)}")
    print("  lead/lag corr of 1m returns, corr(fut_t, idx_t+k): " + ", ".join(f"k={k:+d} {Dy[f'lag{k:+d}'].mean():.2f}" for k in (-2, -1, 0, 1, 2)) + "  (k>0 = the future leads the index)")
    print("\n== VOLUME and MICRO by half hour (pooled medians across days): share of the day's volume, spread, basis, big prints (≥10 lots)")
    g = V.groupby("b").agg(vol_share=("vol_share", "median"), spread=("spread", "median"), basis=("basis", "median"), big=("big", "median"), n=("d", "size"))
    print((g.assign(vol_share=g.vol_share * 100)).round(2).to_string())
    print("\n== FLOW → forward index return (Spearman rank IC, pooled 5m bars across days; + means the signal's sign predicts the move); delta = tick-rule from the future's frames, ofi/imb from its five-level book")
    F2 = F.dropna(subset=["ret"])
    for sig in ("delta", "ofi", "imb", "ret", "vol"):
        parts = [f"{sig:<6}"]
        for k in (1, 3, 6, 12):
            ic, n = spearman(F2[sig], F2[f"fwd{k}"]); parts.append(f"fwd{k * 5:>3}m IC {ic:+.3f}")
        ic0, n0 = spearman(F2[sig], F2.ret)
        print("  " + "  ".join(parts) + f"   same-bar {ic0:+.3f}  (N {n0})")
    print("  bar-return autocorrelation (5m, lag 1): " + f"{F2.ret.autocorr(1):+.3f}")
    print(f"  cum delta vs price, same day, Spearman per day: " + ", ".join(f"{d_} {spearman(s.cum_delta, s.c)[0]:+.2f}" for d_, s in F.groupby('d')))
    print("\n== SWINGS of the index (5m local extremes ±30 min): delta into the swing (last 30 min) vs out (next 30 min), bar volume vs the day's median bar")
    for k_, s in S.groupby("kind"):
        print(f"  {k_:<5} N {len(s)}  delta_in < 0 {100 * (s.delta_in < 0).mean():.0f} %  delta_out > 0 {100 * (s.delta_out > 0).mean():.0f} %  bar volume ≥ 2× median {100 * (s.vol_bar >= 2 * s.vol_med).mean():.0f} %  book imbalance at the bar median {s.imb.median():+.2f}  next-30m move median {s.fwd6.median():+.0f} bps")
    r6 = F.groupby("d").delta.transform(lambda s: s.rolling(6).sum())
    print("  control: all 30-min windows — delta < 0 " + f"{100 * (r6.dropna() < 0).mean():.0f} %; all bars — volume ≥ 2× its day median {100 * (F2.vol >= 2 * F2.groupby('d').vol.transform('median')).mean():.0f} %")
    print("\n== OPTIONS: the 10:00 walls vs the rest of the day, straddle vs realised, IV, PCR, max pain")
    Dy["cw10_held"] = Dy.hi_after10 < Dy.cw10; Dy["pw10_held"] = Dy.lo_after10 > Dy.pw10
    Dy["real_pct"] = (Dy.hi_after10 - Dy.lo_after10) / Dy.px10 * 100; Dy["abs_move_pct"] = (Dy.close - Dy.px10).abs() / Dy.px10 * 100
    print(Dy[["d", "expiry", "px10", "cw10", "pw10", "hi_after10", "lo_after10", "cw10_held", "pw10_held", "cw_open", "cw_close", "pw_open", "pw_close", "cw_changes", "pw_changes"]].round(0).to_string(index=False))
    print(f"  the 10:00 call wall held the rest of the day {int(Dy.cw10_held.sum())} of {len(Dy)}; the put wall {int(Dy.pw10_held.sum())} of {len(Dy)}; both {int((Dy.cw10_held & Dy.pw10_held).sum())}")
    print(Dy[["d", "straddle10", "impl10_pct", "real_pct", "abs_move_pct", "iv_open", "iv_close", "pcr10", "pcr15", "ret_after10_bps", "pain10", "pain15", "close", "ce_add_k", "ce_add", "pe_add_k", "pe_add"]].round(2).to_string(index=False))
    print(f"  implied move (straddle/spot at 10:00) vs realised range after 10:00: realised/implied median {(Dy.real_pct / Dy.impl10_pct).median():.2f}; vs |close − 10:00| median {(Dy.abs_move_pct / Dy.impl10_pct).median():.2f}; days the close moved more than implied {int((Dy.abs_move_pct > Dy.impl10_pct).sum())} of {len(Dy)}")
    print(f"  IV close − open: median {(Dy.iv_close - Dy.iv_open).median():+.2f} vol points; fell on {int((Dy.iv_close < Dy.iv_open).sum())} of {len(Dy)} days")
    print(f"  PCR at 10:00 above 1 and the day rose after 10:00: {int(((Dy.pcr10 > 1) & (Dy.ret_after10_bps > 0)).sum())}; below 1 and fell: {int(((Dy.pcr10 < 1) & (Dy.ret_after10_bps < 0)).sum())}; of {len(Dy)}")
    print(f"  close − max pain at 15:00: median |distance| {(Dy.close - Dy.pain15).abs().median():.0f} pts; expiry days: " + ", ".join(f"{r.d} {r.close - r.pain15:+.0f}" for r in Dy[Dy.expiry == Dy.d].itertuples()))
    print(f"  the day's biggest CE OI add strike vs the day's high: within 100 pts {int(((Dy.ce_add_k - Dy.high).abs() <= 100).sum())} of {len(Dy)}; biggest PE add vs the low: {int(((Dy.pe_add_k - Dy.low).abs() <= 100).sum())} of {len(Dy)}")


if __name__ == "__main__":
    main()
