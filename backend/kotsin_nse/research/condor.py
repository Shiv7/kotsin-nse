"""Hedged NIFTY selling in the calm regime (pre-registered: docs/research/2026-10-23-prereg-unsupervised.md §5).

At the close of session D: sell a NIFTY iron butterfly on the nearest weekly expiry with ≥ 1 day to go —
short the ATM call and put (the strike nearest F; F from put-call parity at the strike nearest the
future), long the call and the put 2 one-day σ away (σ_d = ATM IV × F / √252; the listed strike
nearest, at least one strike out). Close the four legs at D + 1's close (the final settlement when D + 1
is the expiry). Prices from NSE's bhavcopy (``oi_daily_legacy/``, ``oi_daily/``).

Costs per leg and transaction: ``spread_pts`` index points + the engine's charges (``risk/costs.py``,
an option leg of ``LOT`` units, divided back to points). Legs that expire are cash-settled at their
intrinsic value from the final settlement level — no exit spread. P&L is in index points per unit; ₹ for one
lot = points × ``LOT``.

The primary rule trades only when D's regime (``regimes.py``, fitted on the past) is the calm uptrend
— the regime with the lowest IV in that run's profile. Every day is scored too, by regime, for
context (not judged).
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

from ..config import Segment, Settings
from ..domain import Instrument, InstrumentKind, OrderSide
from ..fno.chain import implied_vol
from ..market.fo_bhavcopy import OiDailyStore
from ..risk.costs import CostModel
from .optsignals import HOLDOUT_START, _weekly_t

LOT = 75
WING_SD = 2.0
HALVES = (("2022-04-01", "2023-12-31"), ("2024-01-01", "2025-09-30"))
COLS = ("symbol", "kind", "expiry", "strike", "option_type", "close", "settle", "volume")


def _nifty(store: OiDailyStore, d: date) -> pd.DataFrame:
    df = store.load(d, columns=COLS)
    df = df[(df["symbol"] == "NIFTY") & df["kind"].isin(["OPTIDX", "FUTIDX"])].copy()
    df["expiry"] = df["expiry"].astype(str)
    return df


def open_fly(df: pd.DataFrame, d: date, wing_sd: float = WING_SD, to_expiry: bool = False) -> dict[str, Any] | None:
    """The iron butterfly sold at D's close, or None when the chain cannot price it."""
    fut = df[df["kind"] == "FUTIDX"].assign(dte=lambda x: (pd.to_datetime(x["expiry"]) - pd.Timestamp(d)).dt.days).sort_values("dte")
    opt = df[df["kind"] == "OPTIDX"]
    if fut.empty or opt.empty:
        return None
    ex = sorted(e for e in opt["expiry"].unique() if (pd.Timestamp(e) - pd.Timestamp(d)).days >= 1)
    if not ex:
        return None
    e = ex[0]
    dte = (pd.Timestamp(e) - pd.Timestamp(d)).days
    t = dte / 365
    o = opt[opt["expiry"] == e]
    calls = o[(o["option_type"] == "CE") & (o["close"] > 0)].set_index("strike")["close"]
    puts = o[(o["option_type"] == "PE") & (o["close"] > 0)].set_index("strike")["close"]
    both = sorted(set(calls.index) & set(puts.index))
    if len(both) < 5:
        return None
    f0 = float(fut["settle"].iloc[0])
    k0 = min(both, key=lambda k: abs(k - f0))
    f = k0 + (calls[k0] - puts[k0]) * math.exp(0.065 * t)  # put-call parity
    k = min(both, key=lambda x: abs(x - f))
    iv = [v for v in (implied_vol(float(calls[k]), f, k, t, True), implied_vol(float(puts[k]), f, k, t, False)) if v]
    if not iv:
        return None
    sd = float(np.mean(iv)) * f / math.sqrt(252)
    bdays = int(np.busday_count(d + pd.Timedelta(days=1).to_pytimedelta(), date.fromisoformat(e) + pd.Timedelta(days=1).to_pytimedelta()))
    if to_expiry:
        sd *= math.sqrt(max(bdays, 1))  # the move to expiry, not one day's
    up = [x for x in calls.index if x > k]
    dn = [x for x in puts.index if x < k]
    if not up or not dn:
        return None
    kc = min(up, key=lambda x: abs(x - (k + wing_sd * sd)))
    kp = min(dn, key=lambda x: abs(x - (k - wing_sd * sd)))
    legs = [("CE", k, -1, float(calls[k])), ("PE", k, -1, float(puts[k])), ("CE", kc, 1, float(calls[kc])), ("PE", kp, 1, float(puts[kp]))]
    credit = -sum(side * px for _t, _k, side, px in legs)
    return {"day": d.isoformat(), "expiry": e, "dte": dte, "bdays": bdays, "f": f, "k": k, "kc": kc, "kp": kp, "iv": float(np.mean(iv)), "sd_pts": sd,
            "credit": credit, "max_loss": max(kc - k, k - kp) - credit, "legs": legs}


def close_fly(fly: dict[str, Any], df: pd.DataFrame, d1: date) -> tuple[float, dict[tuple[str, float], float], bool] | None:
    """The four legs at D + 1: the position's value (long +, short −), each leg's price, and whether
    they expired. On expiry day NSE's bhavcopy carries the index's final settlement level in every
    expiring option's settle (old and new formats alike), so a leg is worth its intrinsic value."""
    o = df[(df["kind"] == "OPTIDX") & (df["expiry"] == fly["expiry"])].set_index(["option_type", "strike"])
    expired = d1.isoformat() == fly["expiry"]
    s_final = float(o["settle"].median()) if expired and len(o) else None
    value, px = 0.0, {}
    for typ, k, side, _p in fly["legs"]:
        if expired:
            assert s_final is not None
            p = max(s_final - k, 0.0) if typ == "CE" else max(k - s_final, 0.0)
        else:
            if (typ, k) not in o.index:
                return None
            r = o.loc[(typ, k)]
            p = float(r["close"]) if r["close"] > 0 else float(r["settle"])
        px[(typ, k)] = p
        value += side * p
    return value, px, expired


def leg_charges_pts(costs: CostModel, fly: dict[str, Any], exit_px: dict[tuple[str, float], float], expired: bool, lots: int = 1) -> float:
    """Charges in points: every leg in; out by a trade, or — expired — STT etc. on the in-the-money legs'
    intrinsic value (a sale at that price stands in for the exercise)."""
    inst = Instrument("NIFTY-OPT", "NIFTY", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=LOT)
    total, q = 0.0, LOT * lots
    for typ, k, side, px in fly["legs"]:
        total += costs.leg(inst, OrderSide.SELL if side < 0 else OrderSide.BUY, px, q).total
        xp = exit_px[(typ, k)]
        if not expired or xp > 0:
            total += costs.leg(inst, OrderSide.BUY if side < 0 else OrderSide.SELL, max(xp, 0.05), q).total
    return total / q


def trades(settings: Settings, start: date, end: date, spread_pts: float = 1.0, wing_sd: float = WING_SD) -> pd.DataFrame:
    root = settings.data_dir
    by_day: dict[date, OiDailyStore] = {}
    for s in (OiDailyStore(root / "oi_daily_legacy"), OiDailyStore(root / "oi_daily")):
        for d in s.days():
            by_day[d] = s
    days = [d for d in sorted(by_day) if start <= d <= end]
    costs = CostModel(settings)
    rows = []
    prev: tuple[date, pd.DataFrame] | None = None
    for d in days:
        df = _nifty(by_day[d], d)
        if prev is not None:
            d0, df0 = prev
            fly = open_fly(df0, d0, wing_sd)
            if fly is not None:
                got = close_fly(fly, df, d)
                if got is not None:
                    v1, xp, expired = got
                    gross = fly["credit"] + v1  # the credit taken, then the position's value at the exit
                    cost = (4 if expired else 8) * spread_pts + leg_charges_pts(costs, fly, xp, expired)
                    rows.append({**{k: v for k, v in fly.items() if k != "legs"}, "exit_day": d.isoformat(), "gross_pts": gross,
                                 "cost_pts": cost, "net_pts": gross - cost, "move_pts": abs(_close_f(df, fly) - fly["f"]) if _close_f(df, fly) else np.nan})
        prev = (d, df)
    return pd.DataFrame(rows)


def _close_f(df: pd.DataFrame, fly: dict[str, Any]) -> float | None:
    fut = df[df["kind"] == "FUTIDX"]
    return float(fut.sort_values("expiry")["settle"].iloc[0]) if len(fut) else None


def stats(t: pd.DataFrame, col: str = "net_pts") -> dict[str, Any]:
    if t.empty:
        return {"n": 0}
    s = t.set_index("day")[col]
    tw, weeks = _weekly_t(s)
    months = s.groupby(s.index.str[:7]).sum()
    cum = s.cumsum()
    gains, losses = s[s > 0].sum(), -s[s < 0].sum()
    halves = [round(float(s[(s.index >= a) & (s.index <= b)].mean()), 2) for a, b in HALVES]
    return {"n": len(s), "avg_pts": round(float(s.mean()), 2), "t_by_week": round(tw, 2) if tw else None, "weeks": weeks,
            "win": round(float((s > 0).mean()) * 100, 1), "months_positive": f"{int((months > 0).sum())}/{len(months)}",
            "halves": halves, "profit_factor": round(float(gains / losses), 2) if losses > 0 else None,
            "total_pts": round(float(s.sum()), 1), "worst_day_pts": round(float(s.min()), 1), "best_day_pts": round(float(s.max()), 1),
            "worst_month_pts": round(float(months.min()), 1), "max_drawdown_pts": round(float((cum.cummax() - cum).max()), 1),
            "total_inr_one_lot": round(float(s.sum()) * LOT), "avg_credit_pts": round(float(t["credit"].mean()), 1),
            "avg_cost_pts": round(float(t["cost_pts"].mean()), 1)}


def run(settings: Settings, start: date = date(2022, 4, 1), end: date = date(2025, 9, 30), label: str = "") -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    t0 = _time.time()
    reg = pd.read_parquet(settings.data_dir / "oi_daily" / "_regimes.parquet")["regime"]
    prof_file = max((settings.data_dir / "obtests").glob("regime-*.json"), key=lambda p: p.stat().st_mtime)
    prof = json.loads(prof_file.read_text())["profile"]
    calm = int(min(prof, key=lambda k: prof[k]["iv"]))
    t = trades(settings, start, end)
    t["regime"] = t["day"].map(reg)
    t2 = trades(settings, start, end, spread_pts=2.0)
    t2["regime"] = t2["day"].map(reg)
    primary = t[t["regime"] == calm]
    p = stats(primary)
    p2 = stats(t2[t2["regime"] == calm])
    p["passes"] = bool(p.get("t_by_week") and p["t_by_week"] >= 2.0 and p["avg_pts"] > 0
                       and int(p["months_positive"].split("/")[0]) >= 0.6 * int(p["months_positive"].split("/")[1])
                       and all(h > 0 for h in p["halves"]) and (p["profit_factor"] or 0) >= 1.2 and p2.get("avg_pts", -1) > 0)
    out = {"id": f"condor-{uuid.uuid4().hex[:10]}", "kind": "condor", "label": label, "range": [start.isoformat(), end.isoformat()],
           "calm_regime": calm, "regime_run": prof_file.stem, "primary_calm": p, "primary_at_2pts_spread": p2,
           "every_day": stats(t), "by_regime": {str(int(k)): stats(g) for k, g in t.dropna(subset=["regime"]).groupby("regime")},
           "gross_calm": stats(primary, "gross_pts"), "seconds": round(_time.time() - t0, 1)}
    root = settings.data_dir / "obtests"
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    t.to_parquet(root / f"{out['id']}-trades.parquet")
    return out


def trades_to_expiry(settings: Settings, start: date, end: date, regime: pd.Series, calm: int, spread_pts: float = 1.0, lots: int = 10) -> pd.DataFrame:
    """Variant B (§5b): on a calm-regime close with the nearest weekly 2–7 days out and nothing open, sell the
    butterfly with wings 2σ of the move to expiry; hold to the final settlement. One position at a time."""
    root = settings.data_dir
    by_day: dict[date, OiDailyStore] = {}
    for s in (OiDailyStore(root / "oi_daily_legacy"), OiDailyStore(root / "oi_daily")):
        for d in s.days():
            by_day[d] = s
    costs = CostModel(settings)
    rows: list[dict[str, Any]] = []
    open_: dict[str, Any] | None = None
    for d in [d for d in sorted(by_day) if start <= d <= end]:
        df = _nifty(by_day[d], d)
        if open_ is not None and d.isoformat() >= open_["expiry"]:
            got = close_fly(open_, df, d) if d.isoformat() == open_["expiry"] else None
            if got is not None:
                v1, xp, expired = got
                cost = 4 * spread_pts + leg_charges_pts(costs, open_, xp, expired, lots)
                gross = open_["credit"] + v1
                rows.append({**{k: v for k, v in open_.items() if k != "legs"}, "exit_day": d.isoformat(), "gross_pts": gross,
                             "cost_pts": cost, "net_pts": gross - cost, "settled": expired})
            else:
                rows.append({**{k: v for k, v in open_.items() if k != "legs"}, "exit_day": d.isoformat(), "gross_pts": np.nan,
                             "cost_pts": np.nan, "net_pts": np.nan, "settled": False})
            open_ = None
        if open_ is None and regime.get(d.isoformat()) == calm:
            fly = open_fly(df, d, WING_SD, to_expiry=True)
            if fly is not None and 2 <= fly["dte"] <= 7:
                open_ = fly
    return pd.DataFrame(rows)


def run_b(settings: Settings, start: date = date(2022, 4, 1), end: date = date(2025, 9, 30), label: str = "") -> dict[str, Any]:
    if end >= HOLDOUT_START:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START})")
    reg = pd.read_parquet(settings.data_dir / "oi_daily" / "_regimes.parquet")["regime"]
    prof_file = max((settings.data_dir / "obtests").glob("regime-*.json"), key=lambda p: p.stat().st_mtime)
    prof = json.loads(prof_file.read_text())["profile"]
    calm = int(min(prof, key=lambda k: prof[k]["iv"]))
    t10 = trades_to_expiry(settings, start, end, reg, calm, 1.0, 10)
    missing = int(t10["net_pts"].isna().sum())
    t10 = t10.dropna(subset=["net_pts"])
    t1 = trades_to_expiry(settings, start, end, reg, calm, 1.0, 1).dropna(subset=["net_pts"])
    t2 = trades_to_expiry(settings, start, end, reg, calm, 2.0, 10).dropna(subset=["net_pts"])
    p = stats(t10)
    p2 = stats(t2)
    p["passes"] = bool(p.get("t_by_week") and p["t_by_week"] >= 2.0 and p["avg_pts"] > 0
                       and int(p["months_positive"].split("/")[0]) >= 0.6 * int(p["months_positive"].split("/")[1])
                       and all(h > 0 for h in p["halves"]) and (p["profit_factor"] or 0) >= 1.2 and p2.get("avg_pts", -1) > 0)
    out = {"id": f"condorB-{uuid.uuid4().hex[:10]}", "kind": "condor_b", "label": label, "range": [start.isoformat(), end.isoformat()],
           "calm_regime": calm, "regime_run": prof_file.stem, "unsettled_skipped": missing,
           "B_10_lots": p, "B_10_lots_2pts_spread": p2, "B_1_lot": stats(t1), "B_gross": stats(t10, "gross_pts"),
           "avg_bdays_held": round(float(t10["bdays"].mean()), 2) if len(t10) else None,
           "avg_max_loss_pts": round(float(t10["max_loss"].mean()), 1) if len(t10) else None}
    root = settings.data_dir / "obtests"
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    t10.to_parquet(root / f"{out['id']}-trades.parquet")
    return out


__all__ = ["close_fly", "open_fly", "run", "run_b", "stats", "trades", "trades_to_expiry"]

