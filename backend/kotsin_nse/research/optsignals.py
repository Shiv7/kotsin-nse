"""Option signals → the stock's next 1 and 5 sessions (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §1).

From NSE's F&O bhavcopy (``oi_daily_legacy/`` to 2023, ``oi_daily/`` from 2024), per stock and session,
known after the close of D:

* the near month (≥ ``MIN_DTE`` days to expiry, else the next) and its future's settle F;
* ATM call and put IV (Black-76 on settle prices, the strike nearest F traded on both sides): **H1**
  call IV − put IV (Cremers & Weinbaum 2010, predicted +); **H2** the OTM put (strike nearest 0.95 F,
  traded) IV − ATM call IV (Xing, Zhang & Zhao 2010, predicted −);
* **H3** abnormal option volume (÷ its own median of the 60 sessions before) ÷ abnormal stock volume
  (the same, from the daily price cache) — Johnson & So 2012, predicted −;
* **H4** OI added in OTM puts (K < 0.98 F) − OTM calls (K > 1.02 F), ÷ all option OI — predicted −;
* **H5** an isolation forest (unsupervised) over the z-scored readings, refit each quarter on the year
  before: does unusual activity come before a bigger move than the options priced (|return| ÷ the
  ATM-implied move)?

Index rows (NIFTY): the nearest weekly expiry's ATM IV, with F implied by put-call parity at the ATM
strike, and the PCR — for the regime study.

Measured from D+1's open to the close of D+1 / D+5 (the bhavcopy is out after the close). Per session
the rank IC across stocks; by week, t over weeks; the top-minus-bottom quintile in bps beside the cost
of trading both legs. Returns are winsorised per session at 1 % / 99 % (the daily cache is not
adjusted for every corporate action).
"""

from __future__ import annotations

import json
import math
import time as _time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from ..fno.chain import implied_vol
from ..market.fo_bhavcopy import OiDailyStore
from .history import HistoryStore

MIN_DTE = 5
HOLDOUT_START = date(2025, 10, 1)
SIGNALS = {"H1_cw_spread": +1, "H2_skew": -1, "H3_abn_os": -1, "H4_otm_put_build": -1}
HALVES = ((date(2021, 4, 1), date(2023, 6, 30)), (date(2023, 7, 1), date(2025, 9, 30)))


# -- features from one bhavcopy ------------------------------------------------------------------------


def _atm_iv(calls: pd.DataFrame, puts: pd.DataFrame, f: float, t: float) -> tuple[float | None, float | None, float | None]:
    """IV of the call and the put at the traded strike nearest ``f``, and that strike."""
    both = calls.join(puts, lsuffix="_c", rsuffix="_p", how="inner")
    both = both[(both["volume_c"] > 0) & (both["volume_p"] > 0) & (both["settle_c"] > 0) & (both["settle_p"] > 0)]
    if both.empty:
        return None, None, None
    k = float(both.index[np.argmin(np.abs(both.index.to_numpy(float) - f))])
    r = both.loc[k]
    return implied_vol(float(r["settle_c"]), f, k, t, True), implied_vol(float(r["settle_p"]), f, k, t, False), k


def day_features(df: pd.DataFrame, d: date) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    df = df.copy()
    df["dte"] = (pd.to_datetime(df["expiry"]) - pd.Timestamp(d)).dt.days
    fut = df[df["kind"] == "FUTSTK"].set_index(["symbol", "expiry"])["settle"]
    rows: list[dict[str, Any]] = []
    for sym, g in df[df["kind"] == "OPTSTK"].groupby("symbol"):
        ex = sorted(e for e in g["expiry"].unique() if (sym, e) in fut.index)
        near = next((e for e in ex if (pd.Timestamp(e) - pd.Timestamp(d)).days >= MIN_DTE), None)
        if near is None:
            continue
        f = float(fut[(sym, near)])
        t = max((pd.Timestamp(near) - pd.Timestamp(d)).days, 1) / 365
        gn = g[g["expiry"] == near]
        calls = gn[gn["option_type"] == "CE"].set_index("strike")[["settle", "volume"]]
        puts = gn[gn["option_type"] == "PE"].set_index("strike")[["settle", "volume"]]
        iv_c, iv_p, _k = _atm_iv(calls, puts, f, t)
        otm = puts[(puts.index < 0.98 * f) & (puts["volume"] > 0) & (puts["settle"] > 0)]
        iv_o = None
        if len(otm):
            ko = float(otm.index[np.argmin(np.abs(otm.index.to_numpy(float) - 0.95 * f))])
            iv_o = implied_vol(float(otm.loc[ko, "settle"]), f, ko, t, False)
        ce, pe = g[g["option_type"] == "CE"], g[g["option_type"] == "PE"]
        rows.append({
            "symbol": sym, "day": d.isoformat(), "f": f, "t": t, "iv_c": iv_c, "iv_p": iv_p, "iv_otm_p": iv_o,
            "opt_vol": float(g["volume"].sum()), "call_vol": float(ce["volume"].sum()), "put_vol": float(pe["volume"].sum()),
            "call_oi": float(ce["oi"].sum()), "put_oi": float(pe["oi"].sum()), "oi_chg": float(g["oi_change"].sum()),
            "otm_put_add": float(pe.loc[pe["strike"] < 0.98 * f, "oi_change"].sum()),
            "otm_call_add": float(ce.loc[ce["strike"] > 1.02 * f, "oi_change"].sum()),
            "fut_oi": float(df.loc[(df["kind"] == "FUTSTK") & (df["symbol"] == sym), "oi"].sum()),
        })
    return rows, _index_row(df, d, "NIFTY")


def _index_row(df: pd.DataFrame, d: date, sym: str) -> dict[str, Any] | None:
    o = df[(df["kind"] == "OPTIDX") & (df["symbol"] == sym)]
    fu = df[(df["kind"] == "FUTIDX") & (df["symbol"] == sym)].sort_values("dte")
    if o.empty or fu.empty:
        return None
    f_near = float(fu["settle"].iloc[0])
    ex = sorted(e for e in o["expiry"].unique() if (pd.Timestamp(e) - pd.Timestamp(d)).days >= 1)
    if not ex:
        return None
    e = ex[0]
    t = max((pd.Timestamp(e) - pd.Timestamp(d)).days, 1) / 365
    gn = o[o["expiry"] == e]
    calls = gn[gn["option_type"] == "CE"].set_index("strike")[["settle", "volume"]]
    puts = gn[gn["option_type"] == "PE"].set_index("strike")[["settle", "volume"]]
    both = calls.join(puts, lsuffix="_c", rsuffix="_p", how="inner")
    both = both[(both["settle_c"] > 0) & (both["settle_p"] > 0) & (both["volume_c"] > 0) & (both["volume_p"] > 0)]
    if both.empty:
        return None
    k = float(both.index[np.argmin(np.abs(both.index.to_numpy(float) - f_near))])
    c, p = float(both.loc[k, "settle_c"]), float(both.loc[k, "settle_p"])
    f = k + (c - p) * math.exp(0.065 * t)  # put-call parity: the weekly has no future of its own
    iv = [implied_vol(c, f, k, t, True), implied_vol(p, f, k, t, False)]
    iv = [x for x in iv if x]
    stk = df[df["kind"] == "FUTSTK"]
    return {"day": d.isoformat(), "nifty_f": f_near, "nifty_iv": float(np.mean(iv)) if iv else None, "nifty_t": t,
            "nifty_pcr": float(o.loc[o["option_type"] == "PE", "oi"].sum() / max(o.loc[o["option_type"] == "CE", "oi"].sum(), 1.0)),
            "stock_fut_oi": float(stk["oi"].sum()), "stock_fut_vol": float(stk["volume"].sum())}


def build(data_dir: Path, on_day: Any = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Every held bhavcopy → ``oi_daily/_optsignals.parquet`` and ``_market.parquet`` (incremental)."""
    stores = [OiDailyStore(data_dir / "oi_daily_legacy"), OiDailyStore(data_dir / "oi_daily")]
    by_day: dict[date, OiDailyStore] = {}
    for s in stores:  # the newer file wins a day both hold
        for d in s.days():
            by_day[d] = s
    out_s, out_m = data_dir / "oi_daily" / "_optsignals.parquet", data_dir / "oi_daily" / "_market.parquet"
    held_s = pd.read_parquet(out_s) if out_s.exists() else pd.DataFrame()
    held_m = pd.read_parquet(out_m) if out_m.exists() else pd.DataFrame()
    done = set(held_m["day"]) if len(held_m) else set()
    rows_s: list[dict[str, Any]] = []
    rows_m: list[dict[str, Any]] = []
    cols = ("symbol", "kind", "expiry", "strike", "option_type", "settle", "oi", "oi_change", "volume")
    todo = [d for d in sorted(by_day) if d.isoformat() not in done]
    for n, d in enumerate(todo, 1):
        df = by_day[d].load(d, columns=cols)
        df["expiry"] = df["expiry"].astype(str)
        s, m = day_features(df, d)
        rows_s += s
        if m:
            rows_m.append(m)
        if on_day and (n % 50 == 0 or n == len(todo)):
            on_day(d, n, len(todo))
    feats = pd.concat([held_s, pd.DataFrame(rows_s)], ignore_index=True) if rows_s else held_s
    market = pd.concat([held_m, pd.DataFrame(rows_m)], ignore_index=True) if rows_m else held_m
    if rows_s or rows_m:
        feats.to_parquet(out_s)
        market.to_parquet(out_m)
    return feats, market


# -- evaluation ----------------------------------------------------------------------------------------


def _prices(store: HistoryStore, sym: str) -> pd.DataFrame | None:
    d1 = store.load(sym, "1d")
    if d1.empty:
        return None
    d1 = d1.sort_values("ts").drop_duplicates("ts")
    d1["day"] = [(pd.Timestamp(t, unit="s") + pd.Timedelta(hours=5, minutes=30)).date().isoformat() for t in d1["ts"]]
    o, c, v = d1["o"].to_numpy(float), d1["c"].to_numpy(float), d1["v"].to_numpy(float)
    nxt_o = np.r_[o[1:], np.nan]
    d1["fwd1"] = np.r_[c[1:], np.nan] / nxt_o - 1
    d1["fwd5"] = np.r_[c[5:], [np.nan] * 5] / nxt_o - 1
    d1["abn_stock_vol"] = v / pd.Series(v).shift(1).rolling(60, min_periods=40).median().to_numpy()
    return d1[["day", "fwd1", "fwd5", "abn_stock_vol"]]


def panel(settings: Settings, feats: pd.DataFrame) -> pd.DataFrame:
    """Features joined to forward returns, today's tickers, F&O members only."""
    from ..market.fo_universe import canonical
    from ..market.fo_universe import load as load_members

    fm, _ = load_members(settings.data_dir)
    changes = dict(fm.changes)
    f = feats.copy()
    f["symbol"] = [canonical(s, changes) for s in f["symbol"]]
    f = f.sort_values(["symbol", "day"]).reset_index(drop=True)
    g = f.groupby("symbol")
    f["abn_opt_vol"] = f["opt_vol"] / g["opt_vol"].transform(lambda s: s.shift(1).rolling(60, min_periods=40).median())
    store = HistoryStore(settings.data_dir / "history")
    px = []
    for sym in f["symbol"].unique():
        p = _prices(store, sym)
        if p is not None:
            px.append(p.assign(symbol=sym))
    f = f.merge(pd.concat(px), on=["symbol", "day"], how="inner")
    f["H1_cw_spread"] = f["iv_c"] - f["iv_p"]
    f["H2_skew"] = f["iv_otm_p"] - f["iv_c"]
    f["H3_abn_os"] = np.log(f["abn_opt_vol"] / f["abn_stock_vol"])
    f["H4_otm_put_build"] = (f["otm_put_add"] - f["otm_call_add"]) / (f["call_oi"] + f["put_oi"]).replace(0, np.nan)
    f["iv_atm"] = (f["iv_c"] + f["iv_p"]) / 2
    f["member"] = [fm.covers(date.fromisoformat(dd)) and fm.member(s, date.fromisoformat(dd)) for s, dd in zip(f["symbol"], f["day"], strict=True)]
    for h in ("fwd1", "fwd5"):  # winsorise per session
        lo, hi = f.groupby("day")[h].transform(lambda s: s.quantile(0.01)), f.groupby("day")[h].transform(lambda s: s.quantile(0.99))
        f[h] = f[h].clip(lo, hi)
    return f[f["member"]].drop(columns="member")


def _weekly_t(by_day: pd.Series) -> tuple[float | None, int]:
    s = by_day.dropna()
    if s.empty:
        return None, 0
    wk = s.groupby([date.fromisoformat(d).isocalendar()[:2] for d in s.index]).mean()
    sd = wk.std(ddof=1)
    return (float(wk.mean() / (sd / math.sqrt(len(wk)))) if sd > 0 else None), len(wk)


def _ic(df: pd.DataFrame, sig: str, ret: str) -> pd.Series:
    def one(g: pd.DataFrame) -> float:
        x = g[[sig, ret]].replace([np.inf, -np.inf], np.nan).dropna()
        return float(x[sig].rank().corr(x[ret].rank())) if len(x) >= 30 else np.nan
    return df.groupby("day")[[sig, ret]].apply(one)


def _spread(df: pd.DataFrame, sig: str, ret: str, sign: int) -> pd.Series:
    def one(g: pd.DataFrame) -> float:
        x = g[[sig, ret]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(x) < 30:
            return np.nan
        q = x[sig].rank(pct=True) * sign
        top, bot = x[ret][q >= (0.8 if sign > 0 else -0.2)], x[ret][q <= (0.2 if sign > 0 else -0.8)]
        return float((top.mean() - bot.mean()) * 1e4)
    return df.groupby("day")[[sig, ret]].apply(one)


def evaluate(settings: Settings, f: pd.DataFrame, start: date, end: date, cost_bps: float) -> dict[str, Any]:
    f = f[(f["day"] >= start.isoformat()) & (f["day"] <= end.isoformat())]
    out: dict[str, Any] = {"rows": len(f), "days": int(f["day"].nunique()), "cost_bps_round_trip": cost_bps, "tests": {}}
    for sig, sign in SIGNALS.items():
        for ret in ("fwd1", "fwd5"):
            ic = _ic(f, sig, ret) * sign  # positive = the predicted direction
            t, weeks = _weekly_t(ic)
            months = ic.dropna().groupby([d[:7] for d in ic.dropna().index]).mean()
            halves = [round(float(ic[(ic.index >= a.isoformat()) & (ic.index <= b.isoformat())].mean()), 4) for a, b in HALVES]
            sp = _spread(f, sig, ret, sign)
            hold = 1 if ret == "fwd1" else 5
            out["tests"][f"{sig}:{ret}"] = {
                "mean_ic_signed": round(float(ic.mean()), 4), "t_by_week": round(t, 2) if t is not None else None, "weeks": weeks,
                "months_right": f"{int((months > 0).sum())}/{len(months)}", "halves": halves,
                "q5_q1_bps_per_hold": round(float(sp.mean()), 2), "cost_bps_per_hold": round(2 * cost_bps, 1),
                "net_bps_per_day": round(float(sp.mean()) / hold - 2 * cost_bps / hold, 2),
                "passes": bool(t is not None and t >= 3.0 and (months > 0).mean() >= 0.6 and all(h > 0 for h in halves) and sp.mean() > 2 * cost_bps),
            }
    out["tests"]["H5_anomaly"] = _anomaly(f)
    return out


H5_FEATURES = ("log_opt_vol", "pc_vol", "oi_chg_rel", "pcr", "d_iv", "H2_skew", "H1_cw_spread", "H4_otm_put_build", "abn_stock_vol")


def _anomaly(f: pd.DataFrame) -> dict[str, Any]:
    """Isolation forest on per-stock z-scores, refit each quarter on the year before; then does the
    top anomaly decile move more than its ATM IV priced, against the rest, over the next 1 / 5 sessions."""
    from sklearn.ensemble import IsolationForest

    f = f.sort_values(["symbol", "day"]).copy()
    g = f.groupby("symbol")
    f["log_opt_vol"] = np.log1p(f["opt_vol"])
    f["pc_vol"] = np.log((f["put_vol"] + 1) / (f["call_vol"] + 1))
    f["oi_chg_rel"] = f["oi_chg"] / (f["call_oi"] + f["put_oi"]).replace(0, np.nan)
    f["pcr"] = f["put_oi"] / f["call_oi"].replace(0, np.nan)
    f["d_iv"] = g["iv_atm"].diff()
    z = pd.DataFrame(index=f.index)
    for c in H5_FEATURES:
        mu = g[c].transform(lambda s: s.shift(1).rolling(60, min_periods=40).mean())
        sd = g[c].transform(lambda s: s.shift(1).rolling(60, min_periods=40).std())
        z[c] = ((f[c] - mu) / sd).clip(-8, 8)
    z = z.replace([np.inf, -np.inf], np.nan)
    ok = z.notna().all(axis=1) & f["iv_atm"].notna()
    f, z = f[ok], z[ok]
    f["q"] = [d[:4] + "Q" + str((int(d[5:7]) - 1) // 3 + 1) for d in f["day"]]
    f["score"] = np.nan
    for q in sorted(f["q"].unique()):
        first = f.loc[f["q"] == q, "day"].min()
        train = (f["day"] < first) & (f["day"] >= (pd.Timestamp(first) - pd.Timedelta(days=365)).date().isoformat())
        if train.sum() < 5_000:
            continue
        m = IsolationForest(n_estimators=200, max_samples=4096, random_state=0).fit(z[train].to_numpy())
        f.loc[f["q"] == q, "score"] = -m.score_samples(z[f["q"] == q].to_numpy())
    f = f[f["score"].notna()]
    res: dict[str, Any] = {"rows_scored": len(f)}
    for ret, h in (("fwd1", 1), ("fwd5", 5)):
        f["ratio"] = f[ret].abs() / (f["iv_atm"] * math.sqrt(h / 252))

        def one(gr: pd.DataFrame) -> float:
            x = gr[["score", "ratio"]].dropna()
            if len(x) < 30:
                return np.nan
            top = x["score"] >= x["score"].quantile(0.9)
            return float(x.loc[top, "ratio"].mean() - x.loc[~top, "ratio"].mean())

        diff = f.groupby("day")[["score", "ratio"]].apply(one)
        t, _weeks = _weekly_t(diff)
        months = diff.dropna().groupby([d[:7] for d in diff.dropna().index]).mean()
        halves = [round(float(diff[(diff.index >= a.isoformat()) & (diff.index <= b.isoformat())].mean()), 4) for a, b in HALVES]
        res[ret] = {"top_decile_minus_rest_move_vs_implied": round(float(diff.mean()), 4), "t_by_week": round(t, 2) if t else None,
                    "months_right": f"{int((months > 0).sum())}/{len(months)}", "halves": halves,
                    "top_decile_ratio": round(float(f.loc[f["score"] >= f.groupby("day")["score"].transform(lambda s: s.quantile(0.9)), "ratio"].mean()), 3),
                    "all_ratio": round(float(f["ratio"].mean()), 3),
                    "passes": bool(t is not None and t >= 3.0 and (months > 0).mean() >= 0.6 and all(x > 0 for x in halves))}
    return res


def run(settings: Settings, start: date = date(2021, 4, 1), end: date = date(2025, 9, 30), label: str = "", on_day: Any = None) -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    from ..config import Segment
    from ..domain import Instrument, InstrumentKind
    from ..risk.costs import CostModel

    t0 = _time.time()
    feats, _market = build(settings.data_dir, on_day)
    f = panel(settings, feats)
    inst = Instrument("BT", "BT", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=1)
    cost_bps = CostModel(settings).round_trip(inst, 1000.0, 1000.0, 500).total / 500_000 * 1e4 + 10.0
    out = {"id": f"optsig-{uuid.uuid4().hex[:10]}", "kind": "optsignals", "label": label, "created_ts": _time.time(),
           "range": [start.isoformat(), end.isoformat()], **evaluate(settings, f, start, end, cost_bps), "seconds": round(_time.time() - t0, 1)}
    root = settings.data_dir / "obtests"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out


__all__ = ["SIGNALS", "build", "day_features", "evaluate", "panel", "run"]
