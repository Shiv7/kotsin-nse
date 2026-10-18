"""Where the structure engine gets its bars: the history cache (replay, backtest) or the live
store. Both hand :func:`analyze` the same thing — today's closed 5m bars on the session grid, the
previous session's high/low/close and the guarded average daily range."""

from __future__ import annotations

from datetime import date

from ..bars.store import BarStore
from ..market.session import ist_day
from ..research.history import HistoryStore
from ..scan.replay import _ist_ts, guarded_adr
from .engine import Structure, analyze, resample_5m


def from_history(store: HistoryStore, symbol: str, day: date, until_ts: int) -> Structure | None:
    m1 = store.load(symbol, "1m")
    d1 = store.load(symbol, "1d")
    if m1.empty:
        return None
    open_ts = _ist_ts(day, "09:15")
    rows = m1[(m1["ts"] >= open_ts) & (m1["ts"] + 60 <= until_ts)].sort_values("ts")
    # a 5m bar is used only once all five of its minutes have closed
    last_closed_5m = open_ts + ((until_ts - open_ts) // 300) * 300
    rows = rows[rows["ts"] < last_closed_5m]
    if rows.empty:
        return None
    before = d1[d1["ts"] < _ist_ts(day, "00:00")].sort_values("ts")
    prev = (float(before["h"].iloc[-1]), float(before["l"].iloc[-1]), float(before["c"].iloc[-1])) if len(before) else None
    adr = guarded_adr(before["h"].tolist(), before["l"].tolist(), before["c"].tolist()) if len(before) else None
    bars = resample_5m(list(rows[["ts", "o", "h", "l", "c", "v"]].itertuples(index=False, name=None)), open_ts)
    return analyze(bars, prev_day=prev, adr=adr)


def from_store(bars: BarStore, symbol: str, now: float) -> Structure | None:
    today = ist_day(now)
    m1 = [b for b in bars.bars(symbol, "1m") if ist_day(b.ts) == today]
    if not m1:
        return None
    open_ts = _ist_ts(today, "09:15")
    last_closed_5m = open_ts + ((int(now) - open_ts) // 300) * 300
    rows = [(int(b.ts), b.open, b.high, b.low, b.close, b.volume) for b in m1 if b.ts < last_closed_5m]
    prior = [b for b in bars.bars(symbol, "1d") if ist_day(b.ts) < today]
    prev = (prior[-1].high, prior[-1].low, prior[-1].close) if prior else None
    adr = guarded_adr([b.high for b in prior], [b.low for b in prior], [b.close for b in prior]) if prior else None
    return analyze(resample_5m(rows, open_ts), prev_day=prev, adr=adr) if rows else None


__all__ = ["date", "from_history", "from_store"]
