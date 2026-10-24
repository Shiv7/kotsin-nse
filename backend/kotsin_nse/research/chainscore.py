"""A combined option-chain score, traded weekly (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §6a).

The §1 panel (``optsignals.panel``: one row per stock and session, known after the close) gives four
readings — the call−put IV spread, the put skew, the abnormal option/stock volume, the OTM put build.
Each becomes a per-session cross-sectional rank (0–1). The **score** is their weighted sum, the
weights fitted **walk-forward**: for each calendar month, a ridge regression of the next-5-session
return on the four ranks over the 12 months before it (first test month 2022-04).

**Primary rule.** At the last session of each week: long the top quintile of the score, short the
bottom quintile, equal-weighted stock futures, held to the last session of the next week. A name
already on the same side is kept; costs — ``cost_bps`` for a round trip, half per fill — are charged
only to names that enter or leave a leg. The long-short return is mean(long) − mean(short), each leg
net of its own costs. Reported beside it: the same rebalanced daily (held one session), the legs
apart, by year.
"""

from __future__ import annotations

import json
import math
import time as _time
import uuid
from datetime import date
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from .optsignals import HOLDOUT_START, SIGNALS, panel

FIRST_TEST = "2022-04"
HALVES = (("2022-04-01", "2023-12-31"), ("2024-01-01", "2025-09-30"))
RIDGE_ALPHA = 1.0


def ranks(f: pd.DataFrame) -> pd.DataFrame:
    """The four readings as per-session percentile ranks (0–1), rows with every reading present."""
    cols = list(SIGNALS)
    x = f.replace([np.inf, -np.inf], np.nan).dropna(subset=cols)
    r = x[["symbol", "day", "fwd1", "fwd5"]].copy()
    for c in cols:
        r[c] = x.groupby("day")[c].rank(pct=True) - 0.5
    return r.sort_values(["day", "symbol"]).reset_index(drop=True)


def walk_forward_scores(r: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list[float]]]:
    """Each month's score from a ridge fit on the 12 months before it. Returns the scored rows and
    the weights by month (for the read-out)."""
    from sklearn.linear_model import Ridge

    cols = list(SIGNALS)
    r = r.assign(month=r["day"].str[:7])
    months = sorted(m for m in r["month"].unique() if m >= FIRST_TEST)
    out, weights = [], {}
    for m in months:
        first = f"{m}-01"
        lo = (pd.Timestamp(first) - pd.DateOffset(months=12)).strftime("%Y-%m-%d")
        train = r[(r["day"] >= lo) & (r["day"] < first)].dropna(subset=["fwd5"])
        if len(train) < 5_000:
            continue
        model = Ridge(alpha=RIDGE_ALPHA).fit(train[cols].to_numpy(float), train["fwd5"].to_numpy(float))
        test = r[r["month"] == m].copy()
        test["score"] = model.predict(test[cols].to_numpy(float))
        weights[m] = [round(float(w), 6) for w in model.coef_]
        out.append(test)
    return pd.concat(out, ignore_index=True), weights


def _week_ends(days: list[str]) -> list[str]:
    """The last session of each ISO week."""
    by_week: dict[str, str] = {}
    for d in sorted(days):
        y, w, _ = date.fromisoformat(d).isocalendar()
        by_week[f"{y}-W{w:02d}"] = d
    return sorted(by_week.values())


def _closes(settings: Settings, symbols: list[str]) -> pd.DataFrame:
    from .history import HistoryStore
    from .regimes import _daily

    store = HistoryStore(settings.data_dir / "history")
    cols = {}
    for s in symbols:
        try:
            d = _daily(store, s)
        except FileNotFoundError:
            continue
        if len(d):
            cols[s] = d.set_index("day")["c"]
    return pd.DataFrame(cols).sort_index()


def portfolio(scored: pd.DataFrame, closes: pd.DataFrame, rebalance_days: list[str], cost_bps: float) -> pd.DataFrame:
    """One row per rebalance: the legs' gross returns to the next rebalance, their costs (half a round
    trip per fill, only on names that enter or leave), the net long-short return."""
    rows = []
    prev_long: set[str] = set()
    prev_short: set[str] = set()
    sc = {d: g for d, g in scored.groupby("day")}
    for d0, d1 in pairwise(rebalance_days):
        g = sc.get(d0)
        if g is None or len(g) < 30 or d0 not in closes.index or d1 not in closes.index:
            prev_long, prev_short = set(), set()
            continue
        q = g["score"].rank(pct=True)
        long = set(g.loc[q >= 0.8, "symbol"])
        short = set(g.loc[q <= 0.2, "symbol"])
        ret = (closes.loc[d1] / closes.loc[d0] - 1).clip(-0.25, 0.25)
        rl = ret.reindex(list(long)).dropna()
        rs = ret.reindex(list(short)).dropna()
        if rl.empty or rs.empty:
            continue
        half = cost_bps / 2 / 1e4
        cost_l = half * (len(long - prev_long) + len(prev_long - long)) / len(long)
        cost_s = half * (len(short - prev_short) + len(prev_short - short)) / len(short)
        rows.append({"day": d0, "to": d1, "n_long": len(rl), "n_short": len(rs),
                     "long_gross": float(rl.mean()), "short_gross": float(rs.mean()),
                     "cost_long": cost_l, "cost_short": cost_s,
                     "turnover": (len(long ^ prev_long) + len(short ^ prev_short)) / (2 * (len(long) + len(short))),
                     "ls_gross": float(rl.mean() - rs.mean()), "ls_net": float(rl.mean() - rs.mean() - cost_l - cost_s)})
        prev_long, prev_short = long, short
    return pd.DataFrame(rows)


def stats(p: pd.DataFrame, col: str, per_year: float) -> dict[str, Any]:
    if p.empty:
        return {"n": 0}
    s = p.set_index("day")[col] * 1e4
    wk = s.groupby(["{}-W{:02d}".format(*date.fromisoformat(d).isocalendar()[:2]) for d in s.index]).sum()
    sd = wk.std(ddof=1)
    t = float(wk.mean() / (sd / math.sqrt(len(wk)))) if sd > 0 else None
    halves = [round(float(s[(s.index >= a) & (s.index <= b)].mean()), 2) for a, b in HALVES]
    yrs = s.groupby(s.index.str[:4]).mean().round(2).to_dict()
    return {"n": len(s), "avg_bps": round(float(s.mean()), 2), "t_by_week": round(t, 2) if t else None, "weeks": len(wk),
            "weeks_positive": round(float((wk > 0).mean()) * 100, 1), "halves": halves,
            "sharpe": round(float(wk.mean() / sd * math.sqrt(52)), 2) if sd > 0 else None,
            "annual_pct": round(float(s.mean()) * per_year / 100, 2), "by_year_bps": yrs}


def run(settings: Settings, end: date = date(2025, 9, 30), label: str = "") -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    from ..config import Segment
    from ..domain import Instrument, InstrumentKind
    from ..risk.costs import CostModel

    t0 = _time.time()
    f = panel(settings, pd.read_parquet(settings.data_dir / "oi_daily" / "_optsignals.parquet"))
    f = f[f["day"] <= end.isoformat()]
    r = ranks(f)
    scored, weights = walk_forward_scores(r)
    inst = Instrument("BT", "BT", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=1)
    cost_bps = CostModel(settings).round_trip(inst, 1000.0, 1000.0, 500).total / 500_000 * 1e4 + 10.0
    closes = _closes(settings, sorted(scored["symbol"].unique()))
    days = sorted(set(scored["day"]) & set(closes.index))
    weekly = portfolio(scored, closes, _week_ends(days), cost_bps)
    daily = portfolio(scored, closes, days, cost_bps)
    primary = stats(weekly, "ls_net", 52)
    primary["passes"] = bool(primary.get("t_by_week") and primary["t_by_week"] >= 2.0 and primary["avg_bps"] > 0
                             and primary["weeks_positive"] >= 55 and all(h > 0 for h in primary["halves"])
                             and (primary.get("sharpe") or 0) >= 1.0)
    out = {"id": f"chainscore-{uuid.uuid4().hex[:10]}", "kind": "chainscore", "label": label, "end": end.isoformat(),
           "cost_bps_round_trip": round(cost_bps, 2), "stock_sessions_scored": len(scored),
           "avg_turnover_weekly": round(float(weekly["turnover"].mean()), 3) if len(weekly) else None,
           "weekly_ls_net": primary, "weekly_ls_gross": stats(weekly, "ls_gross", 52),
           "weekly_long_gross": stats(weekly, "long_gross", 52), "weekly_short_gross": stats(weekly, "short_gross", 52),
           "daily_ls_net": stats(daily, "ls_net", 252), "daily_ls_gross": stats(daily, "ls_gross", 252),
           "avg_turnover_daily": round(float(daily["turnover"].mean()), 3) if len(daily) else None,
           "weights_by_month": {m: dict(zip(SIGNALS, w, strict=True)) for m, w in weights.items()},
           "seconds": round(_time.time() - t0, 1)}
    root = settings.data_dir / "obtests"
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    weekly.to_parquet(root / f"{out['id']}-weekly.parquet")
    return out


__all__ = ["portfolio", "ranks", "run", "stats", "walk_forward_scores"]
