"""Double bottoms / tops on the RECORDED sessions, 1-minute bars, with the two checks history lacks.

Same detector and trade as ``confirm.py`` (entry on the first 1m close beyond the neckline, stop
beyond the lower low, target the measured move, out at the session end), run on the engine's own
1m bars (``archive/bars``, live) for every stock and index recorded. A recording gap over
``MAX_GAP_S`` splits the session: no pattern is read across a hole.

The checks, read at the trigger from data at or before it:

* **Selling exhausted** (bottom; buying exhausted for a top): the front future's net aggressive
  volume (``archive/flow`` delta) around the second low against around the first — eased when the
  second is less one-sided. The cash stock's flow when the future's is missing.
* **Option writers** (bottom): put OI at strikes at or below the price, nearest expiry
  (``archive/oi``), from the first low to the trigger — "puts written" when it rose. For a top,
  call OI at or above the price — "calls written".
* **Futures OI** over the same span: up (fresh positions) or down (covering).

A few recorded days are far too few to judge — the read-out says so; it is the instrument for when
there are enough.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import Segment, Settings
from ..instrument.catalogue import parse_master
from ..market.session import ist_at
from ..risk.costs import CostModel
from .confirm import ConfirmParams, ConfirmTrade, atr, doubles, exit_trade, make_trade, summarize

MAX_GAP_S = 180
ONE_MINUTE = ConfirmParams(min_gap=3, max_wait=15, tol_atr=0.5, min_depth_atr=1.5, first_entry="09:20", last_entry="15:00")


def _master(data_dir: Path, day: date) -> list[Any]:
    files = sorted((data_dir / "scripmaster").glob("nse_fo-*.csv"))
    path = next((f for f in reversed(files) if f.stem.split("-", 1)[1] <= day.isoformat()), files[-1] if files else None)
    return [i for i, ok in parse_master(path.read_text(), Segment.NSE_FO) if ok] if path else []


def _last_at(s: pd.DataFrame, t: float) -> float | None:
    """The last OI value of one contract at or before ``t``."""
    k = int(np.searchsorted(s["ts"].to_numpy(float), t, side="right")) - 1
    return float(s["oi"].iloc[k]) if k >= 0 else None


def day_trades(data_dir: Path, day: date, p: ConfirmParams, costs: CostModel) -> list[ConfirmTrade]:
    arc = data_dir / "archive"
    d = day.isoformat()
    bp = arc / "bars" / f"{d}.parquet"
    if not bp.exists():
        return []
    bars = pd.read_parquet(bp)
    bars = bars[bars["source"] == "live"].drop_duplicates(["symbol", "ts"], keep="last")
    lo_t, hi_t = ist_at(day, "09:15"), ist_at(day, "15:30")
    bars = bars[(bars["ts"] >= lo_t) & (bars["ts"] < hi_t)]
    flow = pd.read_parquet(arc / "flow" / f"{d}.parquet") if (arc / "flow" / f"{d}.parquet").exists() else pd.DataFrame()
    if not flow.empty:
        flow["code"] = flow["code"].astype(str)
        flow = flow.sort_values(["code", "ts", "frames"]).drop_duplicates(["code", "ts"], keep="last")
    oi = pd.read_parquet(arc / "oi" / f"{d}.parquet") if (arc / "oi" / f"{d}.parquet").exists() else pd.DataFrame()
    if not oi.empty:
        oi["scrip_code"] = oi["scrip_code"].astype(str)
        oi = oi.sort_values("ts")
    fo = _master(data_dir, day)
    by_root: dict[str, list[Any]] = {}
    for i in fo:
        by_root.setdefault(i.underlying, []).append(i)
    oi_by = {c: g for c, g in oi.groupby("scrip_code")} if not oi.empty else {}
    flow_by = {c: g.set_index("ts") for c, g in flow.groupby("code")} if not flow.empty else {}
    out: list[ConfirmTrade] = []
    for sym, b in bars.groupby("symbol"):
        b = b.sort_values("ts")
        mine = by_root.get(str(sym), [])
        futs = sorted((i for i in mine if i.kind.value == "FUTURE"), key=lambda i: i.expiry)
        fut = futs[0].scrip_code if futs else None
        opts = [i for i in mine if i.is_option and i.scrip_code in oi_by]
        near = min((i.expiry for i in opts), default=None)
        opts = [i for i in opts if i.expiry == near]
        fl = flow_by.get(fut) if fut in flow_by else flow_by.get(str(b["scrip_code"].iloc[0]))
        T = b["ts"].to_numpy(np.int64)
        Op, H, L, C = (b[x].to_numpy(float) for x in ("o", "h", "l", "c"))
        A = atr(H, L, C)
        cut = np.flatnonzero(np.r_[True, np.diff(T) > MAX_GAP_S])
        for a0, a1 in zip(cut, np.r_[cut[1:], len(T)], strict=True):
            ok = (T[a0:a1] + 60 >= ist_at(day, p.first_entry)) & (T[a0:a1] + 60 <= ist_at(day, p.last_entry))
            h, lo, c, a = H[a0:a1], L[a0:a1], C[a0:a1], A[a0:a1]
            for sign in (1, -1):
                hh, ll, cc = (h, lo, c) if sign > 0 else (-lo, -h, -c)
                for s in doubles(hh, ll, cc, a, p, ok):
                    k = s["k"]
                    low = min(s["l1"], s["l2"])
                    depth = s["neck"] - low
                    tr = make_trade(str(sym), day, "double-1m", sign, a0 + k, C[a0 + k], sign * (low - p.stop_buffer_atr * a[k]),
                                    sign * (s["neck"] + depth), T, p, {}, bar_s=60)
                    if not tr:
                        continue
                    t1, tk = float(T[a0 + s["i1"]]) + 60, float(T[a0 + k]) + 60
                    tr.tags = _checks(sign, fl, oi_by, opts, fut, T[a0 + s["i1"]], T[a0 + s["i2"]], t1, tk, C[a0 + k])
                    exit_trade(tr, Op, H, L, C, T, a0 + k, a1, p, costs, bar_s=60)
                    out.append(tr)
    return out


def _checks(sign: int, fl: pd.DataFrame | None, oi_by: dict[str, pd.DataFrame], opts: list[Any], fut: str | None,
            ts1: int, ts2: int, t1: float, tk: float, px: float) -> dict[str, str]:
    tags: dict[str, str] = {}
    if fl is not None and "delta" in fl:
        d1 = fl["delta"].reindex([ts1 - 60, ts1, ts1 + 60]).sum()
        d2 = fl["delta"].reindex([ts2 - 60, ts2, ts2 + 60]).sum()
        tags["flow"] = ("selling eased" if sign > 0 else "buying eased") if sign * (d2 - d1) > 0 else ("selling not eased" if sign > 0 else "buying not eased")
    else:
        tags["flow"] = "no flow"
    kind = "PE" if sign > 0 else "CE"
    side = [i for i in opts if i.option_type.value == kind and (i.strike <= px if sign > 0 else i.strike >= px)]
    if side:
        chg = 0.0
        for i in side:
            a, b = _last_at(oi_by[i.scrip_code], t1), _last_at(oi_by[i.scrip_code], tk)
            if a is not None and b is not None:
                chg += b - a
        tags["writers"] = f"{'puts' if sign > 0 else 'calls'} {'written' if chg > 0 else 'cut' if chg < 0 else 'flat'}"
    else:
        tags["writers"] = "no options"
    if fut and fut in oi_by:
        a, b = _last_at(oi_by[fut], t1), _last_at(oi_by[fut], tk)
        tags["fut_oi"] = "no OI" if a is None or b is None else "fut OI up" if b > a else "fut OI down" if b < a else "fut OI flat"
    else:
        tags["fut_oi"] = "no OI"
    return tags


def run(settings: Settings, days: list[date], p: ConfirmParams = ONE_MINUTE) -> dict[str, Any]:
    costs = CostModel(settings)
    trades: list[ConfirmTrade] = []
    for d in days:
        trades += day_trades(settings.data_dir, d, p, costs)
    return {"days": [d.isoformat() for d in days], "summary": summarize(trades), "trades": trades}


__all__ = ["ONE_MINUTE", "day_trades", "run"]
