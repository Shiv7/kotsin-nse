"""The 16 positioning states: futures price × futures OI × call OI × put OI, each up or down.

Per stock and session, from NSE's own bhavcopy (``market/fo_bhavcopy.py``):

* futures: price change of the near contract against its own previous settle, OI summed over every
  expiry (``research/oi_daily.py``) — the 4 build-up states;
* options: the call and put OI CHANGE (NSE's own ``ChngInOpnIntrst``) summed over the strikes within
  ``BAND`` of the near future's price, for the nearest expiry — and the next one too when the nearest
  is within ``ROLL_DAYS`` of expiring, so the monthly roll is not read as unwinding.

Reading (the common Indian convention): put OI rising = put writers defending a floor (bullish);
call OI rising = call writers capping the upside (bearish). The state is known after the close, so
a trade on day D reads the state of the last session before D.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from ..market.fo_bhavcopy import OiDailyStore
from .oi_daily import futures_table

BAND = 0.10
ROLL_DAYS = 5

# (futures state, call OI up?, put OI up?) → (number, short reading)
STATES: dict[tuple[str, bool, bool], tuple[int, str]] = {
    ("LONG BUILD-UP", False, True): (1, "strongest bullish: put writers defend, call writers leave"),
    ("LONG BUILD-UP", False, False): (2, "bullish, options quiet"),
    ("LONG BUILD-UP", True, True): (3, "bullish but capped: both sides writing"),
    ("LONG BUILD-UP", True, False): (4, "weak up-move: call writers sell the rise"),
    ("SHORT COVERING", False, True): (5, "bullish: shorts exit, put writers defend"),
    ("SHORT COVERING", False, False): (6, "relief rally, no new conviction"),
    ("SHORT COVERING", True, True): (7, "mixed"),
    ("SHORT COVERING", True, False): (8, "rally likely to fade: call writers sell it"),
    ("SHORT BUILD-UP", True, False): (9, "strongest bearish: call writers defend, put writers leave"),
    ("SHORT BUILD-UP", True, True): (10, "bearish but supported: both sides writing"),
    ("SHORT BUILD-UP", False, False): (11, "bearish, options quiet"),
    ("SHORT BUILD-UP", False, True): (12, "weak down-move: put writers buy the dip"),
    ("LONG UNWINDING", True, False): (13, "bearish: longs exit, call writers defend"),
    ("LONG UNWINDING", False, False): (14, "profit-taking, no new conviction"),
    ("LONG UNWINDING", True, True): (15, "mixed"),
    ("LONG UNWINDING", False, True): (16, "dip likely to hold: put writers support it"),
}


def options_table(store: OiDailyStore, fut: pd.DataFrame, cache: Path | None = None) -> pd.DataFrame:
    """Per (symbol, day): call / put OI and their change over the near-the-money strikes."""
    if cache is not None and cache.exists():
        held = pd.read_parquet(cache)
        if len(held) and held["day"].max() >= fut["day"].max():
            return held
    ref = fut.set_index(["symbol", "day"])["near_close"]
    rows = []
    for d in store.days():
        ds = d.isoformat()
        df = store.load(d, columns=("symbol", "kind", "expiry", "strike", "option_type", "oi", "oi_change"))
        o = df[df["kind"] == "OPTSTK"]
        if o.empty:
            continue
        px = ref.xs(ds, level="day") if ds in ref.index.get_level_values("day") else None
        if px is None or px.empty:
            continue
        o = o[o["symbol"].isin(px.index)]
        o = o.assign(px=o["symbol"].map(px))
        o = o[(o["strike"] - o["px"]).abs() <= BAND * o["px"]]
        exp = pd.to_datetime(o["expiry"])
        near = exp.groupby(o["symbol"]).transform("min")
        days_left = (near - pd.Timestamp(d)).dt.days
        nxt = exp[exp > near].groupby(o["symbol"][exp > near]).min()
        keep = (exp == near) | ((days_left <= ROLL_DAYS) & (exp == o["symbol"].map(nxt)))
        o = o[keep]
        g = o.groupby(["symbol", "option_type"]).agg(oi=("oi", "sum"), chg=("oi_change", "sum")).unstack("option_type")
        g.columns = [f"{a}_{b}" for a, b in g.columns]
        g = g.reset_index().assign(day=ds)
        rows.append(g)
    t = pd.concat(rows, ignore_index=True)
    for c in ("oi_CE", "oi_PE", "chg_CE", "chg_PE"):
        if c not in t:
            t[c] = 0.0
    t = t.fillna(0.0).rename(columns={"oi_CE": "call_oi", "oi_PE": "put_oi", "chg_CE": "call_chg", "chg_PE": "put_chg"})
    t["pcr"] = np.where(t["call_oi"] > 0, t["put_oi"] / t["call_oi"], np.nan)
    if cache is not None:
        t.to_parquet(cache, index=False)
    return t


def state_table(store: OiDailyStore, cache_dir: Path | None = None) -> pd.DataFrame:
    """Per (symbol, day): the futures state, call / put direction, the 16-state number and reading."""
    fut = futures_table(store, cache_dir / "_futures.parquet" if cache_dir else None)
    opt = options_table(store, fut, cache_dir / "_options.parquet" if cache_dir else None)
    t = fut.merge(opt, on=["symbol", "day"], how="inner")
    t["call_up"] = t["call_chg"] > 0
    t["put_up"] = t["put_chg"] > 0
    st = [STATES[(lb, cu, pu)] for lb, cu, pu in zip(t["label"], t["call_up"], t["put_up"], strict=True)]
    t["state"] = [s[0] for s in st]
    t["reading"] = [s[1] for s in st]
    return t


def previous_state(table: pd.DataFrame, symbol: str, day: date) -> int | None:
    rows = table[(table["symbol"] == symbol) & (table["day"] < day.isoformat())]
    return int(rows["state"].iloc[-1]) if len(rows) else None
