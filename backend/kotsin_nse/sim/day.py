"""One recorded session of one stock, as time-ordered arrays the environment reads "as of" a second.

From the engine's archive (``data/archive/<kind>/<day>.parquet``): every quote frame of the cash
stock, its front future and its option strikes (``ticks``, with bid and ask), the per-minute order
flow (``flow``) and the 1-minute bars (``bars``); NSE's contract master of the day names each
option's strike, type, expiry and lot.

Nothing is read past the time asked for: quotes as of ``t`` are the last frame received at or
before ``t``; a minute's flow is known only once it has closed (``ts + 60 <= t``).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import Segment
from ..instrument.catalogue import parse_master


@dataclass(slots=True)
class Quotes:
    """Frames of one instrument: receive time, last price, bid, ask, cumulative volume."""

    ts: np.ndarray
    ltp: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    vol: np.ndarray

    def at(self, t: float) -> int:
        """Index of the last frame at or before ``t`` (−1 when none)."""
        return int(np.searchsorted(self.ts, t, side="right")) - 1


@dataclass(slots=True)
class Option:
    code: str
    strike: float
    kind: str  # CE | PE
    expiry: str
    lot: int
    q: Quotes


@dataclass(slots=True)
class RootDay:
    symbol: str
    day: date
    cash: Quotes | None
    future: Quotes | None
    options: list[Option] = field(default_factory=list)
    flow: pd.DataFrame | None = None  # the cash stock's minutes (orderflow/meter.py columns)
    flow_future: pd.DataFrame | None = None
    bars: pd.DataFrame | None = None  # 1m: ts, o, h, l, c, v
    _arr: dict[str, np.ndarray] = field(default_factory=dict)

    def arrays(self) -> dict[str, np.ndarray]:
        """The bars and flow as numpy columns, cumulative where a window sum is needed — read with
        a binary search per step instead of filtering frames (built once, on first use)."""
        if self._arr:
            return self._arr
        a: dict[str, np.ndarray] = {}
        if self.bars is not None and len(self.bars):
            b = self.bars
            a["bar_end"] = (b["ts"] + 60).to_numpy(float)
            v = b["v"].to_numpy(float)
            a["cum_pv"] = np.cumsum((b["h"] + b["l"] + b["c"]).to_numpy(float) / 3 * v)
            a["cum_v"] = np.cumsum(v)
            a["run_hi"] = np.maximum.accumulate(b["h"].to_numpy(float))
            a["run_lo"] = np.minimum.accumulate(b["l"].to_numpy(float))
        for key, f in (("f", self.flow), ("ff", self.flow_future)):
            if f is None or f.empty:
                continue
            col = lambda c, f=f: f[c].to_numpy(float) if c in f else np.full(len(f), np.nan)  # noqa: E731
            a[f"{key}_end"] = (f["ts"] + 60).to_numpy(float)
            for c in ("buy", "sell", "ofi", "bid_depth", "ask_depth", "qi", "micro_bps", "oi"):
                a[f"{key}_{c}"] = col(c)
        self._arr = a
        return a

    def underlying(self) -> Quotes | None:
        return self.cash if self.cash is not None and len(self.cash.ts) else self.future

    def mid(self, t: float) -> float | None:
        q = self.underlying()
        if q is None:
            return None
        i = q.at(t)
        if i < 0:
            return None
        b, a = q.bid[i], q.ask[i]
        return float((b + a) / 2) if b > 0 and a > b else float(q.ltp[i])

    def closed_minutes(self, t: float, n: int, future: bool = False) -> pd.DataFrame:
        """The last ``n`` flow minutes that had closed by ``t``."""
        f = self.flow_future if future else self.flow
        if f is None or f.empty:
            return pd.DataFrame()
        k = bisect.bisect_right(f["ts"].tolist(), t - 60)
        return f.iloc[max(0, k - n) : k]


def _quotes(df: pd.DataFrame) -> Quotes:
    df = df.sort_values("recv_ts")
    return Quotes(df["recv_ts"].to_numpy(float), df["ltp"].to_numpy(float), df["bid"].fillna(0).to_numpy(float),
                  df["ask"].fillna(0).to_numpy(float), df["total_qty"].fillna(0).to_numpy(float))


def load_day(data_dir: Path, day: date, roots: list[str] | None = None) -> dict[str, RootDay]:
    arc = data_dir / "archive"
    d = day.isoformat()
    ticks = pd.read_parquet(arc / "ticks" / f"{d}.parquet", columns=["scrip_code", "recv_ts", "ltp", "bid", "ask", "total_qty"])
    ticks["scrip_code"] = ticks["scrip_code"].astype(str)
    bars = pd.read_parquet(arc / "bars" / f"{d}.parquet")
    bars = bars[bars["source"] == "live"].drop_duplicates(["symbol", "ts"], keep="last")
    flow_p = arc / "flow" / f"{d}.parquet"
    flow = pd.read_parquet(flow_p) if flow_p.exists() else pd.DataFrame()
    if not flow.empty:
        flow["code"] = flow["code"].astype(str)
        flow = flow.sort_values(["code", "ts", "frames"]).drop_duplicates(["code", "ts"], keep="last")
    master = sorted((data_dir / "scripmaster").glob("nse_fo-*.csv"))
    mpath = next((p for p in reversed(master) if p.stem.split("-", 1)[1] <= d), master[-1] if master else None)
    fo = [i for i, ok in parse_master(mpath.read_text(), Segment.NSE_FO) if ok] if mpath else []
    cash_code = {s: str(c) for s, c in bars[["symbol", "scrip_code"]].drop_duplicates("symbol").itertuples(index=False)}
    seen = set(ticks["scrip_code"].unique())
    by_root: dict[str, list[Any]] = {}
    for i in fo:
        if i.scrip_code in seen:
            by_root.setdefault(i.underlying, []).append(i)
    g = ticks.groupby("scrip_code")
    out: dict[str, RootDay] = {}
    for sym in sorted(roots or cash_code):
        mine = by_root.get(sym, [])
        futs = sorted((i for i in mine if i.kind.value == "FUTURE"), key=lambda i: i.expiry)
        opts = [i for i in mine if i.is_option]
        cc = cash_code.get(sym)
        groups = {c: g.get_group(c) for c in {cc, *(i.scrip_code for i in futs[:1]), *(i.scrip_code for i in opts)} if c in seen}
        rd = RootDay(
            sym, day,
            _quotes(groups[cc]) if cc in groups else None,
            _quotes(groups[futs[0].scrip_code]) if futs else None,
            [Option(i.scrip_code, i.strike, i.option_type.value, i.expiry, i.lot_size, _quotes(groups[i.scrip_code])) for i in opts],
            flow[flow["code"] == cc].reset_index(drop=True) if cc and not flow.empty else None,
            flow[flow["code"] == futs[0].scrip_code].reset_index(drop=True) if futs and not flow.empty else None,
            bars[bars["symbol"] == sym].sort_values("ts").reset_index(drop=True),
        )
        if rd.underlying() is not None and rd.options:
            out[sym] = rd
    return out


def coverage(rd: RootDay) -> dict[str, Any]:
    u = rd.underlying()
    return {"symbol": rd.symbol, "cash_frames": len(rd.cash.ts) if rd.cash else 0, "future_frames": len(rd.future.ts) if rd.future else 0,
            "options": len(rd.options), "option_frames": int(sum(len(o.q.ts) for o in rd.options)),
            "from": float(u.ts[0]) if u is not None and len(u.ts) else None, "to": float(u.ts[-1]) if u is not None and len(u.ts) else None}


__all__ = ["Option", "Quotes", "RootDay", "coverage", "load_day"]
