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
from itertools import pairwise
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
        if not fut.empty:
            rows.append(fut.assign(day=d.isoformat())[["day", "symbol", "expiry", "settle", "oi"]])
    if not rows:
        return pd.DataFrame(columns=["day", "symbol", "fut_oi", "near_close", "oi_chg_pct", "price_chg_pct", "label"])
    c = pd.concat(rows, ignore_index=True).sort_values(["symbol", "expiry", "day"], kind="stable")
    # a change is from the session before in the store's own day list: a stock absent from that
    # session's bhavcopy gets NaN, not a change measured from its last row weeks back (the OI
    # change is keyed the same way below: only the previous session's rows reach a day)
    prev_day = {b.isoformat(): a.isoformat() for a, b in pairwise(days)}
    # the price change of a session = the NEAR contract against ITS OWN previous settle, so the
    # switch to the next month after an expiry (which trades at a premium) is not a price move
    g = c.groupby(["symbol", "expiry"])
    c["prev_settle"] = g["settle"].shift(1).where(g["day"].shift(1) == c["day"].map(prev_day))
    # the near contract's own row, whole — ``groupby.first()`` takes the first NON-NULL per column,
    # so a near contract without a settle would borrow the next month's
    near = c.sort_values(["symbol", "day", "expiry"], kind="stable").drop_duplicates(["symbol", "day"], keep="first")
    tot = c.groupby(["symbol", "day"])["oi"].sum().rename("fut_oi").reset_index()
    # yesterday's OI is summed over the contracts STILL ALIVE today: a contract that expired
    # yesterday is in yesterday's total and cannot be in today's, so a plain total read every
    # session after an expiry as the whole market unwinding (28 Oct 2026: 143 of 218 stocks
    # "short covering", total OI −5.6 % — the October series running off, not a position closed)
    next_day = {a.isoformat(): b.isoformat() for a, b in pairwise(days)}
    alive_next = c[c["expiry"] >= c["day"].map(next_day).fillna("9999-12-31")]
    prev_live = alive_next.assign(day=alive_next["day"].map(next_day)).dropna(subset=["day"])
    prev_live = prev_live.groupby(["symbol", "day"])["oi"].sum().rename("prev_oi").reset_index()
    t = tot.merge(near[["symbol", "day", "settle", "prev_settle"]], on=["symbol", "day"]).sort_values(["symbol", "day"], kind="stable")
    t = t.merge(prev_live, on=["symbol", "day"], how="left")
    t = t.rename(columns={"settle": "near_close"})
    t["oi_chg_pct"] = (t["fut_oi"] / t["prev_oi"] - 1) * 100
    t = t.drop(columns=["prev_oi"])
    t["price_chg_pct"] = (t["near_close"] / t["prev_settle"] - 1) * 100
    t = t.drop(columns=["prev_settle"]).dropna(subset=["oi_chg_pct", "price_chg_pct"])
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
