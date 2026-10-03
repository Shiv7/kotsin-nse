"""Daily futures OI per underlying, from the exchange's bhavcopies (``market/fo_bhavcopy.py``).

The history the backtests never had: 5paisa's candles carry no OI, and the engine's own archive
starts on the day it was first left running. The bhavcopy goes back as far as NSE keeps it.

Per session and underlying the table carries the front month's OI and close, and the OI of all its
futures summed — the measure a roll cannot distort: on the expiry days the front month empties into
the next (NIFTY50 average −59 % on the 29 Sep expiry day) while their sum barely moves.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from ..bars.oi_read import oi_quadrant
from ..market.fo_bhavcopy import OiDailyStore

FUT_KINDS = ("FUTSTK", "FUTIDX")


def futures_oi(store: OiDailyStore, symbols: Iterable[str] | None = None,
               start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """One row per (day, symbol): ``front_expiry``, ``front_oi``, ``front_close``, ``front_prev_close``
    (the SAME contract's close the session before — on a roll day the front month changes, and its
    close against the old month's measures the basis, not the move), ``total_oi`` (every expiry
    summed), ``total_oi_change`` (the exchange's own changes summed) and ``futures``."""
    want = {s.upper() for s in symbols} if symbols else None
    rows = []
    last_close: dict[str, float] = {}
    for d in store.days():
        if (start and d < start) or (end and d > end):
            continue
        df = store.load(d)
        df = df[df.kind.isin(FUT_KINDS)]
        if want is not None:
            df = df[df.symbol.isin(want)]
        if df.empty:
            continue
        before = last_close
        last_close = {**last_close, **dict(zip(df.scrip_code, df.close.astype(float), strict=True))}
        df = df.sort_values(["symbol", "expiry"])
        for sym, g in df.groupby("symbol", sort=True):
            front = g.iloc[0]
            rows.append({
                "day": d, "symbol": sym, "front_expiry": front.expiry, "front_oi": float(front.oi),
                "front_close": float(front.close), "front_prev_close": before.get(front.scrip_code),
                "total_oi": float(g.oi.sum()),
                "total_oi_change": float(g.oi_change.sum()), "futures": len(g),
            })
    return pd.DataFrame(rows, columns=["day", "symbol", "front_expiry", "front_oi", "front_close", "front_prev_close",
                                       "total_oi", "total_oi_change", "futures"])


def with_changes(table: pd.DataFrame) -> pd.DataFrame:
    """Day-on-day changes per symbol: ``oi_chg_pct`` from the summed OI (the exchange's change over
    the previous total), ``px_chg_pct`` from the front month against its own previous close, and the
    price–OI quadrant."""
    if table.empty:
        return table.assign(oi_chg_pct=[], px_chg_pct=[], quadrant=[])
    t = table.sort_values(["symbol", "day"]).copy()
    prev_total = t.total_oi - t.total_oi_change
    t["oi_chg_pct"] = (t.total_oi_change / prev_total.where(prev_total > 0)) * 100.0
    t["px_chg_pct"] = (t.front_close / t.front_prev_close.astype(float) - 1.0) * 100.0
    # NaN is truthy, so it would slip past the quadrant's "unknown" test and read as a fall
    t["quadrant"] = pd.Series([oi_quadrant(None if pd.isna(p) else p, None if pd.isna(o) else o)
                               for p, o in zip(t.px_chg_pct, t.oi_chg_pct, strict=True)], index=t.index, dtype=object)
    return t


__all__ = ["FUT_KINDS", "futures_oi", "with_changes"]
