"""The six-session trend screen — the owner (6 Nov 2026): "proper analysis on last 6 days which stock are in trend, then
we do news and the rest, for swing trading find those stocks". Facts only, no verdicts: for every F&O stock the engine
tracks, the last six sessions (29 Oct → 5 Nov 2026) from the archive's own bars on top of the 1d history, the official
futures OI and volume from NSE's bhavcopies, the engine's board at Thursday's close, Devina's sector map (origin/main,
hotstocks-sectors.tsv — her HotStocks score's buckets are the same five ideas: flow, momentum, OI congruence, relative
strength, volume; her flow bucket needs NSE's deal files, which this screen does not fetch).

Per stock: the six-day return, in ADRs (the stock's own 20-session range), against NIFTY (relative strength), up days of
six, Kaufman's efficiency (net move over the sum of daily moves: 1 = a straight line), the deepest pullback inside the
window in ADRs, where the close sits in the six-day range and against the 20/50-day averages and the 20-day high, the
futures OI change over the window with its build-up label, the futures volume of the window against the 20 sessions
before, the board's bias / futures state / PCR at the close.
Usage: screen.py OUT_DIR   (writes screen-<day>.txt and screen-<day>.csv)"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
WINDOW = ["2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
BASE = "2026-10-28"
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"}


def archive_daily(day: str) -> pd.DataFrame:
    a = pq.read_table(DD / "archive" / "bars" / f"{day}.parquet").to_pandas()
    a = a[a["source"] != "partial"].sort_values("ts")
    g = a.groupby("symbol").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"))
    g["day"] = day
    return g.reset_index()


def history(sym: str) -> pd.DataFrame | None:
    p = DD / "history" / "1d" / f"{sym}.parquet"
    if not p.exists():
        return None
    h = pd.read_parquet(p)
    h["day"] = h["ts"].apply(lambda x: datetime.datetime.fromtimestamp(x, IST).date().isoformat())
    h = h[h["day"] <= BASE].drop_duplicates("day", keep="last").sort_values("day")
    return h[["day", "o", "h", "l", "c", "v"]]


def bhav(day: str) -> pd.DataFrame:
    t = pq.read_table(DD / "oi_daily" / f"{day}.parquet", columns=["symbol", "kind", "oi", "volume", "close"]).to_pandas()
    f = t[t["kind"] == "FUTSTK"].groupby("symbol").agg(oi=("oi", "sum"), vol=("volume", "sum")).reset_index()
    f["day"] = day
    return f


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    arch = pd.concat([archive_daily(d) for d in WINDOW])
    days = sorted(p.stem for p in (DD / "oi_daily").glob("2026-*.parquet"))
    bh = pd.concat([bhav(d) for d in days[-27:]])
    sectors = dict(line.split("\t")[:2] for line in (Path(__file__).parent / "sectors.tsv").read_text().splitlines() if "\t" in line)
    fno = pq.read_table(DD / "archive" / "fno" / f"{WINDOW[-1]}.parquet", columns=["symbol", "ts", "bias", "futures_state", "x_pcr_oi", "fut_oi"]).to_pandas()
    fno = fno.dropna(subset=["bias"]).sort_values("ts").groupby("symbol").last()

    def frame(sym: str) -> pd.DataFrame | None:
        h = history(sym)
        w = arch[arch["symbol"] == sym][["day", "o", "h", "l", "c", "v"]]
        if h is None or len(h) < 55 or len(w) < 5:
            return None
        return pd.concat([h, w]).reset_index(drop=True)

    nifty = frame("NIFTY")
    n_ret = nifty["c"].iloc[-1] / nifty[nifty["day"] == BASE]["c"].iloc[0] - 1
    rows = []
    for sym in sorted(arch["symbol"].unique()):
        if sym in INDICES:
            continue
        d = frame(sym)
        if d is None:
            continue
        base = d[d["day"] == BASE]
        if base.empty:
            continue
        i0 = base.index[0]
        pre = d.iloc[max(0, i0 - 19): i0 + 1]  # the 20 sessions to the base
        adr = float((pre["h"] - pre["l"]).mean())
        win = d.iloc[i0 + 1:]
        c0, c1 = float(d["c"].iloc[i0]), float(d["c"].iloc[-1])
        closes = [c0, *win["c"].tolist()]
        moves = np.diff(closes)
        ret = c1 / c0 - 1
        er = abs(c1 - c0) / np.abs(moves).sum() if np.abs(moves).sum() > 0 else 0.0
        run_hi = np.maximum.accumulate(win["h"].values)
        pullback = float((run_hi - win["l"].values).max()) / adr if adr else np.nan
        run_lo = np.minimum.accumulate(win["l"].values)
        bounce = float((win["h"].values - run_lo).max()) / adr if adr else np.nan
        hi6, lo6 = float(win["h"].max()), float(win["l"].min())
        last20 = d.iloc[-20:]
        dma20, dma50 = float(d["c"].iloc[-20:].mean()), float(d["c"].iloc[-50:].mean())
        b0, b1 = bh[(bh["symbol"] == sym) & (bh["day"] == BASE)], bh[(bh["symbol"] == sym) & (bh["day"] == WINDOW[-1])]
        oi_chg = (float(b1["oi"].iloc[0]) / float(b0["oi"].iloc[0]) - 1) * 100 if len(b0) and len(b1) and float(b0["oi"].iloc[0]) > 0 else np.nan
        vols = bh[bh["symbol"] == sym].sort_values("day")
        v6 = vols[vols["day"].isin(WINDOW)]["vol"].mean()
        v20 = vols[vols["day"] < WINDOW[0]].tail(20)["vol"].mean()
        label = None
        if not np.isnan(oi_chg):
            label = ("long build-up" if oi_chg > 1 else "short covering" if oi_chg < -1 else "flat OI") if ret > 0 else \
                    ("short build-up" if oi_chg > 1 else "long unwinding" if oi_chg < -1 else "flat OI")
        b = fno.loc[sym] if sym in fno.index else None
        rows.append({
            "symbol": sym, "sector": sectors.get(sym, "?"), "close": round(c1, 1), "ret6_pct": round(ret * 100, 1), "move_adr": round((c1 - c0) / adr, 1) if adr else np.nan,
            "rs6_pct": round((ret - n_ret) * 100, 1), "up_days": int((moves > 0).sum()), "eff": round(float(er), 2),
            "pullback_adr": round(pullback, 1), "bounce_adr": round(bounce, 1), "pos6": round((c1 - lo6) / (hi6 - lo6), 2) if hi6 > lo6 else np.nan,
            "vs_dma20_pct": round((c1 / dma20 - 1) * 100, 1), "vs_dma50_pct": round((c1 / dma50 - 1) * 100, 1),
            "vs_20d_high_pct": round((c1 / float(last20["h"].max()) - 1) * 100, 1), "vs_20d_low_pct": round((c1 / float(last20["l"].min()) - 1) * 100, 1),
            "adr_pct": round(adr / c1 * 100, 2), "oi6_pct": round(oi_chg, 1) if not np.isnan(oi_chg) else np.nan, "build_up": label,
            "vol6_over_20": round(v6 / v20, 2) if v20 and v20 > 0 else np.nan,
            "bias": b["bias"] if b is not None else None, "fut_state": b["futures_state"] if b is not None else None,
            "pcr": round(float(b["x_pcr_oi"]), 2) if b is not None and pd.notna(b["x_pcr_oi"]) else None,
        })
    D = pd.DataFrame(rows).set_index("symbol")
    D.to_csv(out / f"screen-{WINDOW[-1]}.csv")
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 30)
    cols = ["sector", "close", "ret6_pct", "move_adr", "rs6_pct", "up_days", "eff", "pullback_adr", "pos6", "vs_dma20_pct", "vs_dma50_pct", "vs_20d_high_pct", "oi6_pct", "build_up", "vol6_over_20", "bias", "fut_state", "pcr"]
    print(f"== six sessions {WINDOW[0]} → {WINDOW[-1]} on a {BASE} base; NIFTY {n_ret * 100:+.1f} %; {len(D)} F&O stocks with data")
    print(f"   breadth: up {int((D.ret6_pct > 0).sum())} / down {int((D.ret6_pct < 0).sum())}; beat NIFTY {int((D.rs6_pct > 0).sum())}; above the 20-DMA {int((D.vs_dma20_pct > 0).sum())}; "
          f"median move {D.move_adr.median():+.1f} ADR, median efficiency {D.eff.median():.2f}; build-up: {D.build_up.value_counts().to_dict()}")
    lead = D[(D.move_adr >= 1.5) & (D.up_days >= 4) & (D.eff >= 0.5) & (D.vs_dma20_pct > 0) & (D.rs6_pct > 0)].sort_values("rs6_pct", ascending=False)
    print(f"\n== LEADERS: ≥ 1.5 ADR up, ≥ 4 up days of 6, efficiency ≥ 0.5, above the 20-DMA, ahead of NIFTY — {len(lead)} names, by relative strength")
    print(lead[cols].head(30).to_string())
    clean = D[(D.eff >= 0.7) & (D.up_days >= 5) & (D.pullback_adr <= 1.0) & (D.move_adr >= 2)].sort_values("move_adr", ascending=False)
    print(f"\n== CLEANEST TRENDS: efficiency ≥ 0.7, 5+ up days, no pullback over 1 ADR, ≥ 2 ADR — {len(clean)} names")
    print(clean[cols].head(20).to_string())
    lag = D[(D.rs6_pct <= -3) | (D.ret6_pct < 0)].sort_values("rs6_pct")
    print(f"\n== LAGGARDS in an up week: six-day return negative, or 3 points or more behind NIFTY — {len(lag)} names")
    print(lag[cols].head(25).to_string())
    lb = D[(D.build_up == "long build-up") & (D.rs6_pct > 0)].sort_values("oi6_pct", ascending=False)
    print(f"\n== LONG BUILD-UP ahead of NIFTY (price up, futures OI up over the window) — {len(lb)} names, by OI added")
    print(lb[cols].head(20).to_string())
    sb = D[(D.build_up == "short build-up")].sort_values("oi6_pct", ascending=False)
    print(f"\n== SHORT BUILD-UP (price down, OI up) — {len(sb)} names")
    print(sb[cols].head(15).to_string())
    ext = D[(D.move_adr >= 4)].sort_values("move_adr", ascending=False)
    print(f"\n== EXTENDED: four ADRs or more in six sessions — {len(ext)} names (a chase, not an entry, until a pullback is recorded)")
    print(ext[cols].head(15).to_string())
    print("\n== by sector (Devina's map): count, median six-day return, median relative strength, share above the 20-DMA")
    S = D.groupby("sector").agg(n=("ret6_pct", "size"), ret6=("ret6_pct", "median"), rs6=("rs6_pct", "median"), above20=("vs_dma20_pct", lambda x: round((x > 0).mean(), 2)), eff=("eff", "median")).sort_values("rs6", ascending=False)
    print(S.round(1).to_string())


if __name__ == "__main__":
    main()
