"""Cheap implied volatility across the F&O universe, and what buying it has paid — the owner (6 Nov 2026): "which have
cheapest IV and can we make any strategy around that". From every daily F&O bhavcopy (2024-01-01 → 2026-11-05): per
stock-day the nearest-expiry future, the at-the-money straddle (the strike nearest the future, call + put closes), its
Black-76 implied volatility, the stock's 20-session realised volatility (close to close, annualised), the ratio IV / RV
and the IV's percentile against the stock's own trailing 250 stock-days. Then the test: buy that straddle at the close
and sell it five sessions later at the same strikes' closes, no hedging, on every stock-day with 10–40 days to expiry,
grouped by the IV / RV quintile and by the IV-percentile quintile. Settlement prices, no bid-ask, no charges: an upper
bound on what a long-vol buyer sees. Usage: iv_cheap.py OUT_DIR"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
from kotsin_nse.fno.chain import YEAR_S, implied_vol  # noqa: E402
from screen import DD, WINDOW, archive_daily  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def day_rows(day: str) -> list[dict]:
    t = pq.read_table(DD / "oi_daily" / f"{day}.parquet", columns=["symbol", "kind", "expiry", "strike", "option_type", "close"]).to_pandas()
    t = t[t["kind"].isin(["FUTSTK", "OPTSTK"])]
    t["expiry"] = t["expiry"].astype(str)
    d = datetime.date.fromisoformat(day)
    out = []
    fut = t[t["kind"] == "FUTSTK"]
    opt = t[t["kind"] == "OPTSTK"]
    for sym, f in fut.groupby("symbol"):
        f = f[f["expiry"] >= day].sort_values("expiry")
        if f.empty:
            continue
        exp, fpx = f["expiry"].iloc[0], float(f["close"].iloc[0])
        if fpx <= 0:
            continue
        o = opt[(opt["symbol"] == sym) & (opt["expiry"] == exp)]
        if o.empty:
            continue
        ks = o["strike"].unique()
        atm = float(ks[np.argmin(np.abs(ks - fpx))])
        ce = o[(o["strike"] == atm) & (o["option_type"] == "CE")]["close"]
        pe = o[(o["strike"] == atm) & (o["option_type"] == "PE")]["close"]
        if ce.empty or pe.empty:
            continue
        ce, pe = float(ce.iloc[0]), float(pe.iloc[0])
        e = datetime.date.fromisoformat(exp)
        dte = (e - d).days
        tt = max(dte, 0.5) / 365.0
        ivs = [v for v in (implied_vol(ce, fpx, atm, tt, True), implied_vol(pe, fpx, atm, tt, False)) if v is not None]
        out.append({"day": day, "symbol": sym, "expiry": exp, "dte": dte, "fut": fpx, "atm": atm, "ce": ce, "pe": pe, "straddle": ce + pe,
                    "straddle_pct": (ce + pe) / fpx * 100, "iv": (sum(ivs) / len(ivs) * 100) if ivs else np.nan})
    return out


def closes_all() -> pd.DataFrame:
    rows = []
    for p in (DD / "history" / "1d").glob("*.parquet"):
        h = pd.read_parquet(p, columns=["ts", "c"])
        h["day"] = h["ts"].apply(lambda x: datetime.datetime.fromtimestamp(x, IST).date().isoformat())
        h = h[h["day"] <= "2026-10-28"].drop_duplicates("day", keep="last")
        rows.append(pd.DataFrame({"symbol": p.stem, "day": h["day"].values, "c": h["c"].values}))
    for dd in WINDOW:
        a = archive_daily(dd)
        rows.append(pd.DataFrame({"symbol": a["symbol"].values, "day": dd, "c": a["c"].values}))
    return pd.concat(rows).sort_values(["symbol", "day"]).drop_duplicates(["symbol", "day"], keep="last")


def main() -> None:
    out = Path(sys.argv[1])
    days = sorted(p.stem for p in (DD / "oi_daily").glob("2*.parquet"))
    cache = out / "iv-stockdays.parquet"
    if cache.exists():
        S = pd.read_parquet(cache)
    else:
        rows = []
        for i, day in enumerate(days):
            try:
                rows += day_rows(day)
            except Exception as exc:  # noqa: BLE001
                print(day, "failed:", repr(exc)[:80])
            if i % 100 == 0:
                print("…", day, len(rows), flush=True)
        S = pd.DataFrame(rows)
        S.to_parquet(cache)
    S = S[~S["symbol"].isin(INDICES)].copy()
    C = closes_all()
    C["ret"] = C.groupby("symbol")["c"].transform(lambda x: np.log(x).diff())
    C["rv20"] = C.groupby("symbol")["ret"].transform(lambda x: x.rolling(20).std() * np.sqrt(252) * 100)
    S = S.merge(C[["symbol", "day", "c", "rv20"]], on=["symbol", "day"], how="left")
    S = S.sort_values(["symbol", "day"]).reset_index(drop=True)
    S["iv_over_rv"] = S["iv"] / S["rv20"]
    S["iv_pct"] = S.groupby("symbol")["iv"].transform(lambda x: x.rolling(250, min_periods=60).apply(lambda w: (w[:-1] < w[-1]).mean() * 100, raw=True))
    # the straddle five sessions later at the same strikes and expiry
    key = S.set_index(["day", "symbol", "expiry", "atm"])["straddle"]
    idx = {d: i for i, d in enumerate(days)}
    fwd = []
    for r in S.itertuples():
        i = idx.get(r.day)
        d5 = days[i + 5] if i is not None and i + 5 < len(days) else None
        fwd.append(key.get((d5, r.symbol, r.expiry, r.atm), np.nan) if d5 else np.nan)
    S["straddle_5d"] = fwd
    S["pnl_pct"] = (S["straddle_5d"] / S["straddle"] - 1) * 100
    c5 = C.set_index(["symbol", "day"])["c"]
    S["move_5d_pct"] = [abs(c5.get((r.symbol, days[idx[r.day] + 5]), np.nan) / r.c - 1) * 100 if idx.get(r.day) is not None and idx[r.day] + 5 < len(days) and r.c == r.c else np.nan for r in S.itertuples()]
    T = S[(S["dte"].between(10, 40)) & S["pnl_pct"].notna() & S["iv_over_rv"].notna() & (S["straddle"] > 0)].copy()
    pd.set_option("display.width", 250)
    print(f"== stock-days {len(S)} ({days[0]} → {days[-1]}); tested (10–40 days to expiry, a five-session forward straddle): {len(T)}")
    print(f"   all: buy the ATM straddle, sell five sessions later: mean {T.pnl_pct.mean():+.1f} % of cost, median {T.pnl_pct.median():+.1f}, positive {(T.pnl_pct > 0).mean():.0%}; "
          f"the move over the five sessions beat the straddle's cost {(T.move_5d_pct > T.straddle_pct).mean():.0%} of the time")
    for col, label in (("iv_over_rv", "IV / realised-20"), ("iv_pct", "IV percentile vs the stock's own year")):
        TT = T[T[col].notna()].copy()
        TT["q"] = pd.qcut(TT[col], 5, labels=["1 cheapest", "2", "3", "4", "5 richest"])
        g = TT.groupby("q", observed=True).agg(n=("pnl_pct", "size"), lo=(col, "min"), hi=(col, "max"), mean_pnl=("pnl_pct", "mean"), median_pnl=("pnl_pct", "median"),
                                             win=("pnl_pct", lambda x: (x > 0).mean() * 100), move_beats=("move_5d_pct", lambda x: np.nan))
        g["move_beats"] = TT.groupby("q", observed=True).apply(lambda x: (x.move_5d_pct > x.straddle_pct).mean() * 100, include_groups=False)
        print(f"\n== by {label} quintile (buy the straddle, sell five sessions later; % of cost)")
        print(g.round(2).to_string())
    # the cross-section now
    last = S[S["day"] == days[-1]].copy()
    last = last[last["iv"].notna() & last["rv20"].notna()]
    cols = ["symbol", "fut", "expiry", "dte", "atm", "straddle_pct", "iv", "rv20", "iv_over_rv", "iv_pct"]
    print(f"\n== {days[-1]}: the 25 cheapest by IV / realised-20 (and their IV percentile over the stock's own year)")
    print(last.sort_values("iv_over_rv")[cols].head(25).round(2).to_string(index=False))
    print(f"\n== {days[-1]}: the 25 cheapest by IV percentile (the stock's own history)")
    print(last.sort_values("iv_pct")[cols].head(25).round(2).to_string(index=False))
    print(f"\n== {days[-1]}: the 15 richest by IV / realised-20")
    print(last.sort_values("iv_over_rv", ascending=False)[cols].head(15).round(2).to_string(index=False))
    print(f"\n   universe medians today: IV {last.iv.median():.1f}, rv20 {last.rv20.median():.1f}, IV/RV {last.iv_over_rv.median():.2f}, IV percentile {last.iv_pct.median():.0f}")
    last.to_csv(out / f"iv-cross-section-{days[-1]}.csv", index=False)


if __name__ == "__main__":
    main()
