"""Daily futures open interest per stock, from NSE's own bhavcopy (``market/fo_bhavcopy.py``).

Per stock and session: the futures OI summed over every live expiry (so the monthly roll from the
near to the next contract is not read as a collapse), its change from the session before, the
stock's price change, and the build-up label that pair implies:

=================  ==========  ===========
label              price       OI
=================  ==========  ===========
LONG BUILD-UP      up          up
SHORT BUILD-UP     down        up
SHORT COVERING     up          down
LONG UNWINDING     down        down
=================  ==========  ===========

A session's label is known after its close, so a trade on day D may read the label of the last
session before D and nothing later.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from ..market.fo_bhavcopy import OiDailyStore


def label(price_chg: float, oi_chg: float) -> str:
    if price_chg >= 0:
        return "LONG BUILD-UP" if oi_chg >= 0 else "SHORT COVERING"
    return "SHORT BUILD-UP" if oi_chg >= 0 else "LONG UNWINDING"


def futures_table(store: OiDailyStore, cache: Path | None = None) -> pd.DataFrame:
    """One row per (symbol, day): fut_oi, oi_chg_pct, near_close, price_chg_pct, label. Built once
    from every held bhavcopy and cached to ``cache`` (rebuilt when a newer session arrives)."""
    days = store.days()
    if cache is not None and cache.exists():
        held = pd.read_parquet(cache)
        if len(held) and held["day"].max() >= days[-1].isoformat() if days else True:
            return held
    rows = []
    for d in days:
        df = store.load(d, columns=("symbol", "kind", "expiry", "close", "settle", "oi"))
        fut = df[df["kind"] == "FUTSTK"]
        if fut.empty:
            continue
        near = fut.sort_values("expiry").groupby("symbol").first()
        tot = fut.groupby("symbol")["oi"].sum()
        rows.append(pd.DataFrame({"day": d.isoformat(), "symbol": tot.index, "fut_oi": tot.values, "near_close": near.loc[tot.index, "settle"].values}))
    if not rows:
        return pd.DataFrame(columns=["day", "symbol", "fut_oi", "near_close", "oi_chg_pct", "price_chg_pct", "label"])
    t = pd.concat(rows, ignore_index=True).sort_values(["symbol", "day"])
    g = t.groupby("symbol")
    t["oi_chg_pct"] = (t["fut_oi"] / g["fut_oi"].shift(1) - 1) * 100
    t["price_chg_pct"] = (t["near_close"] / g["near_close"].shift(1) - 1) * 100
    t = t.dropna(subset=["oi_chg_pct", "price_chg_pct"])
    t["label"] = [label(p, o) for p, o in zip(t["price_chg_pct"], t["oi_chg_pct"], strict=True)]
    t = t.reset_index(drop=True)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        t.to_parquet(cache, index=False)
    return t


def previous_state(table: pd.DataFrame, symbol: str, day: date) -> tuple[str, float, float] | None:
    """(label, OI change %, price change %) of the last session before ``day``."""
    rows = table[(table["symbol"] == symbol) & (table["day"] < day.isoformat())]
    if rows.empty:
        return None
    r = rows.iloc[-1]
    return str(r["label"]), float(r["oi_chg_pct"]), float(r["price_chg_pct"])
