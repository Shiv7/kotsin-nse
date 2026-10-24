"""Overnight vs intraday (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §6b).

Each session splits into **overnight** (open ÷ the previous close − 1) and **intraday** (close ÷ open
− 1). From the daily cache, 2021-04-01 → 2025-09-30: NIFTY, BANKNIFTY, and every F&O stock on the
days it had futures (the cross-sectional mean per session).

**Primary rule**: long one NIFTY future from each close to the next open; costs = the future's charges
(``risk/costs.py``, one lot) + 5 bp slippage per fill. Reported beside it: BANKNIFTY the same; the
intraday short (open → close) as the mirror; the overnight by the regime known at the close
(``regimes.py``) and by weekday; 10 lots (brokerage is per order).
"""

from __future__ import annotations

import json
import math
import time as _time
import uuid
from datetime import date
from typing import Any

import pandas as pd

from ..config import Settings
from .history import HistoryStore
from .optsignals import HOLDOUT_START
from .regimes import _daily

HALVES = (("2021-04-01", "2023-06-30"), ("2023-07-01", "2025-09-30"))
LOT = {"NIFTY": 75, "BANKNIFTY": 35}


def split(d1: pd.DataFrame) -> pd.DataFrame:
    """overnight and intraday per session, indexed by the session's day."""
    d = d1.set_index("day")
    prev_c = d["c"].shift(1)
    out = pd.DataFrame({"overnight": d["o"] / prev_c - 1, "intraday": d["c"] / d["o"] - 1, "close_to_close": d["c"] / prev_c - 1})
    return out[(out.abs() < 0.25).all(axis=1)].dropna()


def _stats(s: pd.Series, per_year: float = 252) -> dict[str, Any]:
    s = s.dropna()
    if s.empty:
        return {"n": 0}
    wk = s.groupby(["{}-W{:02d}".format(*date.fromisoformat(d).isocalendar()[:2]) for d in s.index]).sum()
    sd = wk.std(ddof=1)
    t = float(wk.mean() / (sd / math.sqrt(len(wk)))) if sd > 0 else None
    return {"n": len(s), "avg_bps": round(float(s.mean()) * 1e4, 2), "t_by_week": round(t, 2) if t else None, "weeks": len(wk),
            "weeks_positive": round(float((wk > 0).mean()) * 100, 1),
            "halves_bps": [round(float(s[(s.index >= a) & (s.index <= b)].mean()) * 1e4, 2) for a, b in HALVES],
            "by_year_bps": (s.groupby(s.index.str[:4]).mean() * 1e4).round(2).to_dict(),
            "annual_pct": round(float(s.mean()) * per_year * 100, 2),
            "sharpe": round(float(wk.mean() / sd * math.sqrt(52)), 2) if sd > 0 else None,
            "worst_day_bps": round(float(s.min()) * 1e4, 1), "best_day_bps": round(float(s.max()) * 1e4, 1)}


def _cost_bps(settings: Settings, sym: str, price: float, lots: int) -> float:
    from ..config import Segment
    from ..domain import Instrument, InstrumentKind
    from ..risk.costs import CostModel

    inst = Instrument(f"{sym}-FUT", sym, Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=LOT[sym])
    q = LOT[sym] * lots
    return CostModel(settings).round_trip(inst, price, price, q).total / (price * q) * 1e4 + 10.0


def run(settings: Settings, start: date = date(2021, 4, 1), end: date = date(2025, 9, 30), label: str = "") -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    from ..market.fo_universe import load as load_members

    t0 = _time.time()
    store = HistoryStore(settings.data_dir / "history")
    fm, _ = load_members(settings.data_dir)
    lo, hi = start.isoformat(), end.isoformat()
    out: dict[str, Any] = {"id": f"overnight-{uuid.uuid4().hex[:10]}", "kind": "overnight", "label": label, "range": [lo, hi]}
    reg_p = settings.data_dir / "oi_daily" / "_regimes.parquet"
    regime = pd.read_parquet(reg_p)["regime"] if reg_p.exists() else None
    for sym in ("NIFTY", "BANKNIFTY"):
        d = _daily(store, sym)
        x = split(d)
        x = x[(x.index >= lo) & (x.index <= hi)]
        px = float(d["c"].iloc[-1])
        c1, c10 = _cost_bps(settings, sym, px, 1), _cost_bps(settings, sym, px, 10)
        on = x["overnight"]
        # the overnight of session D+1 is taken on at D's close: index it by D for the regime / weekday
        entry_day = pd.Series(list(x.index[:-1]), index=x.index[1:])
        on_by_entry = pd.Series(on.values[1:], index=entry_day.values)
        res = {"overnight_gross": _stats(on), "intraday_gross": _stats(x["intraday"]), "close_to_close": _stats(x["close_to_close"]),
               "cost_bps_round_trip_1_lot": round(c1, 2), "cost_bps_round_trip_10_lots": round(c10, 2),
               "long_overnight_net_1_lot": _stats(on - c1 / 1e4), "long_overnight_net_10_lots": _stats(on - c10 / 1e4),
               "short_intraday_net_10_lots": _stats(-x["intraday"] - c10 / 1e4),
               "overnight_by_weekday_bps": {wd: round(float(v) * 1e4, 2) for wd, v in
                                            on_by_entry.groupby([date.fromisoformat(d).strftime("%a") for d in on_by_entry.index]).mean().items()}}
        if regime is not None:
            rg = on_by_entry.groupby(regime.reindex(on_by_entry.index).values).agg(["mean", "size"])
            res["overnight_by_regime_at_close"] = {str(int(k)): {"avg_bps": round(float(v["mean"]) * 1e4, 2), "n": int(v["size"])}
                                                   for k, v in rg.iterrows() if k == k}
        if sym == "NIFTY":
            p = res["long_overnight_net_10_lots"]
            p["passes"] = bool(p.get("t_by_week") and p["t_by_week"] >= 2.0 and p["avg_bps"] > 0 and p["weeks_positive"] >= 55
                               and all(h > 0 for h in _stats(on - c10 / 1e4)["halves_bps"]))
        out[sym] = res
    # the stocks: the cross-sectional mean per session
    on_rows, in_rows = [], []
    for sym in store.symbols("1d"):
        if sym in LOT:
            continue
        try:
            x = split(_daily(store, sym))
        except Exception:  # noqa: BLE001 - one bad cache file must not stop the sweep
            continue
        x = x[(x.index >= lo) & (x.index <= hi)]
        if x.empty:
            continue
        mem = [fm.covers(date.fromisoformat(d)) and fm.member(sym, date.fromisoformat(d)) for d in x.index]
        x = x[pd.Series(mem, index=x.index)]  # a boolean mask, not a column list (an empty list would be one)
        if x.empty:
            continue
        on_rows.append(x["overnight"].rename(sym))
        in_rows.append(x["intraday"].rename(sym))
    ON, IN = pd.concat(on_rows, axis=1), pd.concat(in_rows, axis=1)
    out["stocks_cross_sectional_mean"] = {"overnight": _stats(ON.mean(axis=1)), "intraday": _stats(IN.mean(axis=1)),
                                          "stocks": int(ON.shape[1]), "share_of_stocks_with_positive_mean_overnight": round(float((ON.mean() > 0).mean()) * 100, 1),
                                          "share_with_negative_mean_intraday": round(float((IN.mean() < 0).mean()) * 100, 1)}
    out["seconds"] = round(_time.time() - t0, 1)
    (settings.data_dir / "obtests" / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out


__all__ = ["run", "split"]
