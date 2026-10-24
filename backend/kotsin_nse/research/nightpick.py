"""Overnight only, names picked by the chain (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §6c).

Each session D: the four chain readings of §1 as per-session ranks; a ridge score with weights fitted
walk-forward on the **overnight return** (D's close → D+1's open) over the 12 months before D's month;
long the top quintile, equal-weighted stock futures, from D's close to D+1's open; ``cost_bps`` per
name per night (a full round trip). The readings are the bhavcopy's close-of-D values — a stand-in for
what the engine's live chain shows at 15:25 (it computes the same readings live).

Reported beside it: every member's overnight (the baseline), the bottom quintile, top − bottom; by
year; by the regime at D's close (post-hoc, see the pre-registration); a NIFTY future the same nights.
"""

from __future__ import annotations

import json
import math
import time as _time
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from .chainscore import FIRST_TEST, RIDGE_ALPHA, ranks
from .optsignals import HOLDOUT_START, SIGNALS, panel

HALVES = (("2022-04-01", "2023-12-31"), ("2024-01-01", "2025-09-30"))


def overnight_returns(settings: Settings, symbols: list[str]) -> pd.DataFrame:
    """One row per (symbol, day D): D's close → the next session's open."""
    from .history import HistoryStore
    from .regimes import _daily

    store = HistoryStore(settings.data_dir / "history")
    rows = []
    for s in symbols:
        try:
            d = _daily(store, s)
        except FileNotFoundError:
            continue
        if len(d) < 2:
            continue
        c, o = d["c"].to_numpy(float), d["o"].to_numpy(float)
        on = np.r_[o[1:] / c[:-1] - 1, np.nan]
        rows.append(pd.DataFrame({"symbol": s, "day": d["day"].to_numpy(), "overnight": on, "next_day": np.r_[d["day"].to_numpy()[1:], None]}))
    out = pd.concat(rows, ignore_index=True)
    return out[out["overnight"].abs() < 0.25]  # a split or bonus is not a return


def scores(r: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list[float]]]:
    """Each month's score from a ridge fit of the overnight return on the four ranks, 12 months before."""
    from sklearn.linear_model import Ridge

    cols = list(SIGNALS)
    r = r.assign(month=r["day"].str[:7])
    out, weights = [], {}
    for m in sorted(x for x in r["month"].unique() if x >= FIRST_TEST):
        first = f"{m}-01"
        lo = (pd.Timestamp(first) - pd.DateOffset(months=12)).strftime("%Y-%m-%d")
        train = r[(r["day"] >= lo) & (r["day"] < first)].dropna(subset=["overnight"])
        if len(train) < 5_000:
            continue
        model = Ridge(alpha=RIDGE_ALPHA).fit(train[cols].to_numpy(float), train["overnight"].to_numpy(float))
        test = r[r["month"] == m].copy()
        test["score"] = model.predict(test[cols].to_numpy(float))
        weights[m] = [round(float(w), 6) for w in model.coef_]
        out.append(test)
    return pd.concat(out, ignore_index=True), weights


def nights(scored: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    """One row per session D: the overnight of the top and bottom quintile and of every member."""
    rows = []
    for d, g in scored.dropna(subset=["overnight"]).groupby("day"):
        if len(g) < 30:
            continue
        q = g["score"].rank(pct=True)
        top, bot = g.loc[q >= 0.8, "overnight"], g.loc[q <= 0.2, "overnight"]
        rows.append({"day": d, "n": len(g), "n_top": len(top), "top_gross": float(top.mean()), "bottom_gross": float(bot.mean()),
                     "all_gross": float(g["overnight"].mean()), "top_net": float(top.mean()) - cost_bps / 1e4,
                     "all_net": float(g["overnight"].mean()) - cost_bps / 1e4, "spread": float(top.mean() - bot.mean())})
    return pd.DataFrame(rows)


def _stats(s: pd.Series) -> dict[str, Any]:
    s = s.dropna()
    if s.empty:
        return {"n": 0}
    b = s * 1e4
    wk = b.groupby(["{}-W{:02d}".format(*date.fromisoformat(d).isocalendar()[:2]) for d in b.index]).sum()
    sd = wk.std(ddof=1)
    t = float(wk.mean() / (sd / math.sqrt(len(wk)))) if sd > 0 else None
    return {"n": len(b), "avg_bps": round(float(b.mean()), 2), "t_by_week": round(t, 2) if t else None, "weeks": len(wk),
            "weeks_positive": round(float((wk > 0).mean()) * 100, 1),
            "halves": [round(float(b[(b.index >= a) & (b.index <= z)].mean()), 2) for a, z in HALVES],
            "sharpe": round(float(wk.mean() / sd * math.sqrt(52)), 2) if sd > 0 else None,
            "annual_pct": round(float(b.mean()) * 252 / 100, 2), "by_year_bps": b.groupby(b.index.str[:4]).mean().round(2).to_dict()}


def run(settings: Settings, end: date = date(2025, 9, 30), label: str = "") -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    from ..config import Segment
    from ..domain import Instrument, InstrumentKind
    from ..risk.costs import CostModel
    from .overnight import _cost_bps

    t0 = _time.time()
    f = panel(settings, pd.read_parquet(settings.data_dir / "oi_daily" / "_optsignals.parquet"))
    f = f[f["day"] <= end.isoformat()]
    r = ranks(f).drop(columns=["fwd1", "fwd5"])
    on = overnight_returns(settings, sorted(r["symbol"].unique()))
    r = r.merge(on, on=["symbol", "day"], how="inner")
    r = r[r["next_day"].notna() & (r["next_day"] <= end.isoformat())]
    scored, weights = scores(r)
    inst = Instrument("BT", "BT", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=1)
    cost_bps = CostModel(settings).round_trip(inst, 1000.0, 1000.0, 500).total / 500_000 * 1e4 + 10.0
    n = nights(scored, cost_bps).set_index("day")
    primary = _stats(n["top_net"])
    primary["passes"] = bool(primary.get("t_by_week") and primary["t_by_week"] >= 2.0 and primary["avg_bps"] > 0
                             and primary["weeks_positive"] >= 55 and all(h > 0 for h in primary["halves"]) and (primary.get("sharpe") or 0) >= 1.0)
    out: dict[str, Any] = {"id": f"nightpick-{uuid.uuid4().hex[:10]}", "kind": "nightpick", "label": label, "end": end.isoformat(),
                           "cost_bps_per_name_per_night": round(cost_bps, 2), "sessions": len(n), "avg_names_in_top": round(float(n["n_top"].mean()), 1),
                           "top_quintile_net": primary, "top_quintile_gross": _stats(n["top_gross"]),
                           "all_members_gross": _stats(n["all_gross"]), "all_members_net": _stats(n["all_net"]),
                           "bottom_quintile_gross": _stats(n["bottom_gross"]), "top_minus_bottom_gross": _stats(n["spread"])}
    reg_p = settings.data_dir / "oi_daily" / "_regimes.parquet"
    if reg_p.exists():
        reg = pd.read_parquet(reg_p)["regime"].reindex(n.index)
        out["by_regime_at_close_post_hoc"] = {str(int(k)): {"nights": len(g), "top_net_bps": round(float(g["top_net"].mean()) * 1e4, 2),
                                                             "all_gross_bps": round(float(g["all_gross"].mean()) * 1e4, 2)}
                                              for k, g in n.groupby(reg.values) if k == k}
    # a NIFTY future the same nights, as the cheaper carrier
    from .history import HistoryStore
    from .overnight import split
    from .regimes import _daily

    nf = split(_daily(HistoryStore(settings.data_dir / "history"), "NIFTY"))["overnight"]
    nf_entry = pd.Series(nf.values[1:], index=nf.index[:-1]).reindex(n.index)  # the night taken on at D's close, indexed by D
    c10 = _cost_bps(settings, "NIFTY", 22_000.0, 10)
    out["nifty_future_same_nights_net_10_lots"] = _stats(nf_entry - c10 / 1e4)
    out["weights_first_last"] = {m: dict(zip(SIGNALS, weights[m], strict=True)) for m in (min(weights), max(weights))} if weights else {}
    out["seconds"] = round(_time.time() - t0, 1)
    root = settings.data_dir / "obtests"
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    n.to_parquet(root / f"{out['id']}-nights.parquet")
    return out


__all__ = ["nights", "overnight_returns", "run", "scores"]
