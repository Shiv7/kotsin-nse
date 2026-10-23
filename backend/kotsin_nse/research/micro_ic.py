"""Does the book predict? Each recorded order-flow reading against the next 5 / 15 / 30 minutes.

For every minute, across every stock recorded that minute (the cash underlyings), the Spearman rank
correlation (IC) between a reading and the stock's forward return. A reading is known at the END of
its minute (flow minute ``ts`` closes at ``ts + 60``), so the forward return runs from the close of
the 1m bar starting at ``ts`` to the close of the bar ``h`` minutes later: nothing after the minute
is used to build the reading.

Statistics are by day, as the research protocol asks: the day's mean IC, then t over days. Also the
top-minus-bottom decile forward return in bps, to hold against the cost of a round trip. Fewer than
15 days is printed as too few to judge. A stable IC of 0.03–0.05 that survives costs would be worth
a strategy; anything that does not is dropped, as SMC was.

Inputs: ``archive/flow/<day>.parquet`` (``orderflow/meter.py``) and ``archive/bars/<day>.parquet``.
Book readings (OFI, queue, microprice, weighted imbalance) exist from 2026-10-26; earlier days have
delta and the top-5 imbalance only, and a reading a day lacks is simply absent that day.
"""

from __future__ import annotations

import json
import math
import statistics
import time as _time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..market.session import ist_at

HORIZONS = (5, 15, 30)
MIN_NAMES = 20  # a minute with fewer stocks recorded has no cross-section worth ranking
MIN_DAYS = 15

#: name → how to build it from a flow frame (one row per code and minute, sorted by ts)
SIGNALS: dict[str, Any] = {
    "delta_share": lambda f: f["delta"] / (f["buy"] + f["sell"]).replace(0, np.nan),
    "delta_5m": lambda f: _roll(f, "delta") / _roll(f, "vol"),
    "imbalance_top5": lambda f: f["imbalance"],
    "ofi_per_depth": lambda f: f["ofi"] / _depth(f),
    "ofi_5m": lambda f: _roll(f, "ofi") / _depth(f),
    "ofi_deep_per_depth": lambda f: f["ofi_deep"] / _depth(f),
    "qi": lambda f: f["qi"],
    "micro_bps": lambda f: f["micro_bps"],
    "wimb": lambda f: f["wimb"],
}
NEEDS = {"ofi_per_depth": "ofi", "ofi_5m": "ofi", "ofi_deep_per_depth": "ofi_deep", "qi": "qi", "micro_bps": "micro_bps", "wimb": "wimb"}


def _roll(f: pd.DataFrame, col: str, n: int = 5) -> pd.Series:
    return f.groupby("code")[col].transform(lambda s: s.rolling(n, min_periods=n).sum())


def _depth(f: pd.DataFrame) -> pd.Series:
    return ((f["bid_depth"] + f["ask_depth"]) / 2).replace(0, np.nan)


def day_frame(archive: Path, day: date) -> pd.DataFrame | None:
    """One day's readings joined to forward returns, one row per stock and minute."""
    fp, bp = archive / "flow" / f"{day}.parquet", archive / "bars" / f"{day}.parquet"
    if not fp.exists() or not bp.exists():
        return None
    bars = pd.read_parquet(bp, columns=["symbol", "scrip_code", "ts", "c", "source"])
    bars = bars[bars["source"] == "live"].drop_duplicates(["scrip_code", "ts"], keep="last")
    bars["scrip_code"] = bars["scrip_code"].astype(str)
    close = bars.pivot(index="ts", columns="scrip_code", values="c").sort_index()
    close = close.reindex(range(int(close.index.min()), int(close.index.max()) + 60, 60))
    lo, hi = ist_at(day, "09:15"), ist_at(day, "15:30")
    f = pd.read_parquet(fp)
    f["code"] = f["code"].astype(str)
    f["ts"] = f["ts"].astype("int64")
    f = f[f["code"].isin(close.columns) & (f["ts"] >= lo) & (f["ts"] < hi)].sort_values(["code", "ts"])
    if f.empty:
        return None
    f["vol"] = f["buy"] + f["sell"]
    for name, build in SIGNALS.items():
        need = NEEDS.get(name)
        f[name] = build(f) if need is None or need in f.columns else np.nan
    for h in HORIZONS:
        fwd = close.shift(-h) / close - 1
        long = fwd.stack().reset_index()
        long.columns = ["ts", "code", f"fwd_{h}"]
        f = f.merge(long, on=["ts", "code"], how="left")
    f["day"] = day.isoformat()
    return f


def _ic_by_minute(f: pd.DataFrame, sig: str, h: int) -> list[float]:
    out = []
    for _ts, g in f[["ts", sig, f"fwd_{h}"]].dropna().groupby("ts"):
        if len(g) >= MIN_NAMES and g[sig].nunique() > 1 and g[f"fwd_{h}"].nunique() > 1:
            out.append(float(g[sig].rank().corr(g[f"fwd_{h}"].rank())))
    return out


def _decile_spread_bps(f: pd.DataFrame, sig: str, h: int) -> float | None:
    g = f[["ts", sig, f"fwd_{h}"]].dropna()
    if g.empty:
        return None
    q = g.groupby("ts")[sig].rank(pct=True)
    top, bot = g.loc[q >= 0.9, f"fwd_{h}"], g.loc[q <= 0.1, f"fwd_{h}"]
    return round(float(top.mean() - bot.mean()) * 1e4, 2) if len(top) and len(bot) else None


def run(data_dir: Path, start: date, end: date) -> dict[str, Any]:
    archive = data_dir / "archive"
    frames = []
    d = start
    while d <= end:
        fr = day_frame(archive, d)
        if fr is not None:
            frames.append(fr)
        d += timedelta(days=1)
    days = [fr["day"].iloc[0] for fr in frames]
    rows: list[dict[str, Any]] = []
    for sig in SIGNALS:
        for h in HORIZONS:
            daily = []
            spreads = []
            for fr in frames:
                ics = _ic_by_minute(fr, sig, h)
                if ics:
                    daily.append(statistics.fmean(ics))
                    sp = _decile_spread_bps(fr, sig, h)
                    if sp is not None:
                        spreads.append(sp)
            if not daily:
                continue
            sd = statistics.stdev(daily) if len(daily) > 1 else 0.0
            rows.append({
                "signal": sig, "horizon_min": h, "days": len(daily),
                "ic": round(statistics.fmean(daily), 4),
                "t_by_day": round(statistics.fmean(daily) / (sd / math.sqrt(len(daily))), 2) if sd > 0 else None,
                "days_positive": sum(x > 0 for x in daily),
                "top_minus_bottom_bps": round(statistics.fmean(spreads), 2) if spreads else None,
            })
    out = {"created_ts": _time.time(), "range": [start.isoformat(), end.isoformat()], "days": days,
           "enough_days": len(days) >= MIN_DAYS, "rows": rows}
    root = data_dir / "micro_ic"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"micro_ic-{start}-{end}.json").write_text(json.dumps(out, default=float))
    return out


__all__ = ["HORIZONS", "MIN_DAYS", "SIGNALS", "day_frame", "run"]
