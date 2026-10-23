"""Day types (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §3).

Per stock-day, from the 1m cache (09:15 ≤ start < 15:14, read the same way every day); ADR = the
average daily range of the 20 sessions before, in % of price:

* **Shape** (the whole day — for the types only): open→close / ADR, the close's place in the range,
  when the high and the low came (fraction of the session), VWAP crossings, range / ADR, path
  efficiency (|close − open| ÷ the sum of |1m changes|).
Levels (ADR, CPR, the weekly pivot, previous closes) come from the 1m bars, not the daily cache.

* **Known by 11:00** (the model's inputs): gap / ADR, 09:15–11:00 return and range / ADR, the 11:00
  price's place in the morning range, VWAP distance and crossings, morning volume vs the 20-session
  average, path efficiency, high before low, price vs P / TC / BC and CPR width, distance to the
  previous week's pivot, the previous 1 and 3 sessions' returns / ADR and the previous close's place in
  its range; and the market's same readings at 11:00 (the mean across stocks, the share up).
* **Outcome**: 11:00 → the 15:13 close, in bps and / ADR.

**Types**: k-means, k = 5, on the standardised shape, fitted on 2024-04 → 2024-09 only. **Model**:
gradient boosting predicts the type from the 11:00 readings, walk-forward (every month before → the
next month). **D1**: when the predicted type's probability ≥ 0.6 and that type's rest-of-day move in the
training months leans one way (|mean| ≥ 0.1 ADR), trade 11:00 → close that way as a stock future.
Baselines on the same stock-days: follow the morning move; fade it.
"""

from __future__ import annotations

import json
import time as _time
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from .confirm import cpr
from .history import HistoryStore
from .optsignals import HOLDOUT_START, _weekly_t

SHAPE = ("oc", "close_loc", "t_high", "t_low", "vwap_x", "range", "eff")
MORNING = ("gap", "m_ret", "m_range", "m_loc", "m_vwap", "m_vwap_x", "m_vol", "m_eff", "m_hi_first", "vs_p", "vs_tc", "vs_bc", "cpr_w",
           "vs_wp", "prev1", "prev3", "prev_loc", "mkt_m_ret", "mkt_up", "mkt_gap")
K = 5
TYPES_FIT = ("2024-04-01", "2024-09-30")
HALVES_1M = (("2024-04-01", "2024-12-31"), ("2025-01-01", "2025-09-30"))
OPEN_M, ELEVEN_M, LAST_M = 9 * 60 + 15, 11 * 60, 15 * 60 + 14
BREAK_SKIP = 10


def _vwap_x(px: np.ndarray, vw: np.ndarray) -> int:
    s = np.sign(px - vw)
    s = s[s != 0]
    return int((s[1:] != s[:-1]).sum()) if len(s) > 1 else 0


def stock_days(sym: str, m1: pd.DataFrame, start: date, end: date) -> list[dict[str, Any]]:
    """Every level is built from the 1m bars themselves: the daily cache is adjusted for bonuses and
    splits and the 1m history is not (RELIANCE's 1:1 bonus, Oct 2024), so mixing them misplaces every
    level. The range is in % of price; ``BREAK_SKIP`` sessions after an overnight jump over 20 % (a
    split or bonus) are skipped, as their prior levels and returns are on the old price scale."""
    if m1.empty:
        return []
    m1 = m1.sort_values("ts").drop_duplicates("ts")
    t = pd.to_datetime(m1["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    mins = (t.dt.hour * 60 + t.dt.minute).to_numpy()
    days = t.dt.date.to_numpy()
    keep = (mins >= OPEN_M) & (mins < LAST_M) & (days <= end)
    m1, mins, days = m1[keep], mins[keep], days[keep]
    daily = m1.assign(day=days).groupby("day").agg(O=("o", "first"), H=("h", "max"), L=("l", "min"), C=("c", "last"))
    dd = list(daily.index)
    O1, H, L, C = (daily[x].to_numpy(float) for x in ("O", "H", "L", "C"))
    adr_pct = pd.Series((H - L) / C).shift(1).rolling(20, min_periods=15).mean().to_numpy()  # sessions before
    jump = np.r_[False, np.abs(O1[1:] / C[:-1] - 1) > 0.2]
    since_break = np.full(len(dd), 10_000)
    last = -10_000
    for i in range(len(dd)):
        if jump[i]:
            last = i
        since_break[i] = i - last
    idx = {d: i for i, d in enumerate(dd)}
    wk: dict[tuple[int, int], list[int]] = {}
    for i, d in enumerate(dd):
        wk.setdefault(d.isocalendar()[:2], []).append(i)
    wkeys = sorted(wk)
    wp = {}
    for n, k in enumerate(wkeys[1:], 1):
        prev = wk[wkeys[n - 1]]
        p = (H[prev].max() + L[prev].min() + C[prev[-1]]) / 3
        for i in wk[k]:
            wp[dd[i]] = p
    keep = days >= start
    m1, mins, days = m1[keep], mins[keep], days[keep]
    if not len(days):
        return []
    mvol_hist: list[float] = []
    out = []
    Oo, Hh, Ll, Cc, V = (m1[x].to_numpy(float) for x in ("o", "h", "l", "c", "v"))
    cut = np.flatnonzero(np.r_[True, days[1:] != days[:-1]])
    for a, b in zip(cut, np.r_[cut[1:], len(days)], strict=True):
        d = days[a]
        i = idx.get(d)
        if i is None or i < 4 or np.isnan(adr_pct[i]) or since_break[i] < BREAK_SKIP or b - a < 300:
            continue
        ad, lv = adr_pct[i] * C[i - 1], cpr(H[i - 1], L[i - 1], C[i - 1])
        mm = mins[a:b]
        o, h, lo, c, v = Oo[a:b], Hh[a:b], Ll[a:b], Cc[a:b], V[a:b]
        tp = (h + lo + c) / 3
        vw = np.cumsum(tp * v) / np.maximum(np.cumsum(v), 1e-9)
        mo = mm < ELEVEN_M
        if mo.sum() < 90 or (~mo).sum() < 150:
            continue
        j = int(np.flatnonzero(mo)[-1])
        p11, op = c[j], o[0]
        mh, ml = h[: j + 1].max(), lo[: j + 1].min()
        mvol = float(v[: j + 1].sum())
        avg_mvol = float(np.mean(mvol_hist[-20:])) if len(mvol_hist) >= 10 else np.nan
        mvol_hist.append(mvol)
        dh, dl, dc = h.max(), lo.min(), c[-1]
        n = len(c)
        steps = np.abs(np.diff(c))
        out.append({
            "symbol": sym, "day": d.isoformat(), "adr_bps": adr_pct[i] * 1e4,
            # shape (whole day)
            "oc": (dc - op) / ad, "close_loc": (dc - dl) / (dh - dl) if dh > dl else 0.5,
            "t_high": int(np.argmax(h)) / n, "t_low": int(np.argmin(lo)) / n, "vwap_x": _vwap_x(c, vw), "range": (dh - dl) / ad,
            "eff": abs(dc - op) / steps.sum() if steps.sum() else 0.0,
            # known by 11:00
            "gap": (op - C[i - 1]) / ad, "m_ret": (p11 - op) / ad, "m_range": (mh - ml) / ad, "m_loc": (p11 - ml) / (mh - ml) if mh > ml else 0.5,
            "m_vwap": (p11 - vw[j]) / ad, "m_vwap_x": _vwap_x(c[: j + 1], vw[: j + 1]), "m_vol": mvol / avg_mvol if avg_mvol else np.nan,
            "m_eff": abs(p11 - op) / steps[:j].sum() if steps[:j].sum() else 0.0,
            "m_hi_first": float(np.argmax(h[: j + 1]) < np.argmin(lo[: j + 1])),
            "vs_p": (p11 - lv["P"]) / ad, "vs_tc": (p11 - lv["TC"]) / ad, "vs_bc": (p11 - lv["BC"]) / ad, "cpr_w": (lv["TC"] - lv["BC"]) / ad,
            "vs_wp": (p11 - wp[d]) / ad if d in wp else np.nan,
            "prev1": (C[i - 1] - C[i - 2]) / ad, "prev3": (C[i - 1] - C[i - 4]) / ad,
            "prev_loc": (C[i - 1] - L[i - 1]) / (H[i - 1] - L[i - 1]) if H[i - 1] > L[i - 1] else 0.5,
            # outcome
            "rod_bps": (dc / p11 - 1) * 1e4, "rod_adr": (dc - p11) / ad,
        })
    return out


def build(settings: Settings, start: date, end: date, on_symbol: Any = None) -> pd.DataFrame:
    from ..market.fo_universe import load as load_members

    fm, _ = load_members(settings.data_dir)
    store = HistoryStore(settings.data_dir / "history")
    rows: list[dict[str, Any]] = []
    names = [s for s in store.symbols("1m") if s not in ("NIFTY", "BANKNIFTY")]
    for n, sym in enumerate(names, 1):
        got = stock_days(sym, store.load(sym, "1m"), start, end)
        rows += [r for r in got if fm.member(sym, date.fromisoformat(r["day"]))]
        if on_symbol and (n % 25 == 0 or n == len(names)):
            on_symbol(sym, n, len(names))
    df = pd.DataFrame(rows)
    g = df.groupby("day")
    df["mkt_m_ret"] = g["m_ret"].transform("mean")
    df["mkt_up"] = g["m_ret"].transform(lambda s: (s > 0).mean())
    df["mkt_gap"] = g["gap"].transform("mean")
    return df


def types(df: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    from sklearn.cluster import KMeans

    x = df[list(SHAPE)].astype(float)
    fit = (df["day"] >= TYPES_FIT[0]) & (df["day"] <= TYPES_FIT[1])
    mu, sd = x[fit].mean(), x[fit].std()
    km = KMeans(K, n_init=10, random_state=0).fit(((x[fit] - mu) / sd).to_numpy())
    lab = pd.Series(km.predict(((x - mu) / sd).to_numpy()), index=df.index)
    prof = df[fit].assign(type=lab[fit]).groupby("type")[[*SHAPE, "rod_adr"]].mean().round(3)
    prof["share"] = lab[fit].value_counts(normalize=True).sort_index().round(3)
    return lab, prof


def walk_forward(df: pd.DataFrame, lab: pd.Series, cost_bps: float, p_min: float = 0.6, lean: float = 0.1) -> pd.DataFrame:
    from sklearn.ensemble import HistGradientBoostingClassifier

    df = df.assign(type=lab, month=df["day"].str[:7])
    months = sorted(df["month"].unique())
    first_test = next(m for m in months if m > TYPES_FIT[1][:7])
    picks = []
    for m in months[months.index(first_test):]:
        tr, te = df[df["month"] < m], df[df["month"] == m]
        clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, l2_regularization=1.0, random_state=0)
        clf.fit(tr[list(MORNING)].to_numpy(float), tr["type"].to_numpy())
        p = clf.predict_proba(te[list(MORNING)].to_numpy(float))
        lean_by_type = tr.groupby("type")["rod_adr"].mean()
        k = clf.classes_[p.argmax(axis=1)]
        side = np.array([np.sign(lean_by_type[x]) if abs(lean_by_type[x]) >= lean else 0 for x in k])
        take = (p.max(axis=1) >= p_min) & (side != 0)
        picks.append(te.assign(pred=k, p=p.max(axis=1), side=side, take=take))
    out = pd.concat(picks)
    out["net_bps"] = out["side"] * out["rod_bps"] - cost_bps
    out["follow_bps"] = np.sign(out["m_ret"]) * out["rod_bps"] - cost_bps
    out["fade_bps"] = -np.sign(out["m_ret"]) * out["rod_bps"] - cost_bps
    return out


def _stats(per_trade: pd.Series, days: pd.Series) -> dict[str, Any]:
    if per_trade.empty:
        return {"n": 0}
    by_day = per_trade.groupby(days).mean()
    t, weeks = _weekly_t(by_day)
    months = per_trade.groupby(days.str[:7]).mean()
    halves = [round(float(per_trade[(days >= a) & (days <= b)].mean()), 2) for a, b in HALVES_1M]
    return {"n": len(per_trade), "avg_net_bps": round(float(per_trade.mean()), 2), "t_by_week": round(t, 2) if t else None, "weeks": weeks,
            "months_positive": f"{int((months > 0).sum())}/{len(months)}", "halves": halves}


def run(settings: Settings, start: date = date(2024, 4, 1), end: date = date(2025, 9, 30), label: str = "", on_symbol: Any = None) -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    from ..config import Segment
    from ..domain import Instrument, InstrumentKind
    from ..risk.costs import CostModel

    t0 = _time.time()
    df = build(settings, start, end, on_symbol).replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=[*SHAPE, "rod_bps"])
    lab, prof = types(df)
    inst = Instrument("BT", "BT", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=1)
    cost_bps = CostModel(settings).round_trip(inst, 1000.0, 1000.0, 500).total / 500_000 * 1e4 + 10.0
    wf = walk_forward(df, lab, cost_bps)
    tk = wf[wf["take"]]
    d1 = _stats(tk["net_bps"], tk["day"])
    d1["passes"] = bool(d1.get("t_by_week") and d1["t_by_week"] >= 3 and d1["avg_net_bps"] > 0
                        and int(d1["months_positive"].split("/")[0]) >= 0.6 * int(d1["months_positive"].split("/")[1])
                        and all(h > 0 for h in d1["halves"]))
    acc = float((wf["pred"] == wf["type"]).mean())
    base = float(wf["type"].value_counts(normalize=True).max())
    out = {"id": f"daytype-{uuid.uuid4().hex[:10]}", "kind": "daytype", "label": label, "range": [start.isoformat(), end.isoformat()],
           "stock_days": len(df), "cost_bps": round(cost_bps, 2), "types": prof.reset_index().to_dict("records"),
           "type_accuracy_walk_forward": round(acc, 3), "most_common_type_share": round(base, 3),
           "D1_model": d1,
           "baseline_follow_same_days": _stats(tk["follow_bps"], tk["day"]), "baseline_fade_same_days": _stats(tk["fade_bps"], tk["day"]),
           "baseline_follow_all": _stats(wf["follow_bps"], wf["day"]), "baseline_fade_all": _stats(wf["fade_bps"], wf["day"]),
           "by_predicted_type": {str(k): _stats(g["net_bps"], g["day"]) for k, g in tk.groupby("pred")},
           "gross_bps_model": round(float((tk["side"] * tk["rod_bps"]).mean()), 2) if len(tk) else None,
           "seconds": round(_time.time() - t0, 1)}
    root = settings.data_dir / "obtests"
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    df.assign(type=lab).to_parquet(settings.data_dir / "history" / "_daytypes.parquet")
    return out


__all__ = ["MORNING", "SHAPE", "K", "build", "run", "stock_days", "types", "walk_forward"]

