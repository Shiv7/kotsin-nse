"""Profiles from the history cache (replay) or the live store, cut at a moment so a replay never
sees the future."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from ..bars.store import BarStore
from ..market.session import ist_at, ist_day
from ..research.history import HistoryStore, session_rows
from .core import TPO_MINUTES, bin_size, market_profile, volume_profile


def from_history(store: HistoryStore, symbol: str, day: date, until_ts: int, days: int = 1) -> dict[str, Any] | None:
    m1 = store.load(symbol, "1m")
    if m1.empty:
        return None
    open_ts = ist_at(day, "09:15")
    today = session_rows(m1, day)
    today = today[today["ts"] + 60 <= until_ts]
    if today.empty:
        return None
    step = bin_size(float(today["c"].iloc[-1]))
    out: dict[str, Any] = {
        "volume": _vp(today, step),
        "market": _mp(today, open_ts, step),
    }
    if days > 1:
        start = ist_at(day - timedelta(days=days * 2), "00:00")
        before = m1[(m1["ts"] >= start) & (m1["ts"] < ist_at(day, "00:00"))]
        sessions = sorted({ist_day(int(t)) for t in before["ts"][::60]})[-(days - 1) :]
        past = pd.concat([session_rows(m1, s) for s in sessions], ignore_index=True) if sessions else before.iloc[0:0]
        out["composite"] = _vp(pd.concat([past, today], ignore_index=True), step)
        out["composite_sessions"] = len(sessions) + 1
    return out


def from_store(bars: BarStore, symbol: str, now: float, open_ts: float | None = None) -> dict[str, Any] | None:
    """``open_ts`` = the segment's session open today (default NSE's 09:15). A session that began
    later than its first TPO period — MCX's evening-only session on an NSE holiday, 17:00 — is
    lettered from its first bar, so A and B are its real first hour and the day type is known."""
    today = ist_day(now)
    m1 = [b for b in bars.bars(symbol, "1m") if ist_day(b.ts) == today and b.ts + 60 <= now]
    if not m1:
        return None
    nominal = int(open_ts if open_ts is not None else ist_at(today, "09:15"))
    anchor = nominal if m1[0].ts < nominal + TPO_MINUTES * 60 else int(m1[0].ts)
    step = bin_size(m1[-1].close)
    vp = volume_profile([(b.high, b.low, b.volume) for b in m1], step)
    mp = market_profile([(int(b.ts), b.high, b.low) for b in m1], anchor, step)
    return {"volume": vp.to_json() if vp else None, "market": mp.to_json() if mp else None}


def _vp(df: Any, step: float) -> dict[str, Any] | None:
    vp = volume_profile(list(df[["h", "l", "v"]].itertuples(index=False, name=None)), step)
    return vp.to_json() if vp else None


def _mp(df: Any, open_ts: int, step: float) -> dict[str, Any] | None:
    mp = market_profile([(int(t), float(h), float(lo)) for t, h, lo in df[["ts", "h", "l"]].itertuples(index=False, name=None)], open_ts, step)
    return mp.to_json() if mp else None
