"""Rebuild the board for a past day, minute by minute, from the history cache.

The same ``build_board`` the live engine runs, fed from cached 1m and daily bars instead of the
socket. Uses only what was knowable at each minute: bars that had closed by then, the previous
session's close, and the average daily range of the sessions before the day.

Split and bonus adjustment: the broker's history is not adjusted, so a stock whose daily closes
jump by more than ``SPLIT_JUMP`` in the ADR window gets no ADR (and so no score) rather than a
score inflated by a pre-split range — the bug order-block.com reported living with for weeks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..instrument.universe import INDEX_ROOTS
from ..market.session import ist_at
from ..research.history import HistoryStore, session_rows
from ..structure.engine import Structure, analyze, resample_5m
from .board import Board, attach_setups, build_board
from .sectors import sectors
from .strength import ADR_SESSIONS, SCORE_FROM_IST, DayState, average_daily_range

SPLIT_JUMP = 0.35  # |close / prev close − 1| beyond this inside the ADR window = unadjusted action


@dataclass(slots=True)
class _Day:
    symbol: str
    minute_ts: np.ndarray  # bar START, epoch s
    open: float
    cum_high: np.ndarray
    cum_low: np.ndarray
    close: np.ndarray
    vwap: np.ndarray
    cum_vol: np.ndarray
    prev_close: float
    adr: float | None
    raw: np.ndarray  # (n, 6): ts, o, h, l, c, v — for the structure engine
    prev_day: tuple[float, float, float]


def guarded_adr(highs: list[float], lows: list[float], closes: list[float]) -> float | None:
    """The average daily range, or None when the window holds an unadjusted split or bonus."""
    wc = np.asarray(closes[-(ADR_SESSIONS + 1) :], dtype=float)
    if len(wc) > 1 and (np.abs(wc[1:] / wc[:-1] - 1) > SPLIT_JUMP).any():
        return None
    return average_daily_range(highs, lows)




def load_day(store: HistoryStore, symbol: str, day: date) -> _Day | None:
    m1 = store.load(symbol, "1m")
    d1 = store.load(symbol, "1d")
    if m1.empty or d1.empty:
        return None
    bars = session_rows(m1, day)
    if bars.empty:
        return None
    # Sessions strictly BEFORE the day, by IST date: NSE stamps a final daily candle 00:00, so
    # "before 09:15" would let the day's own candle in as its previous close (found 2026-10-18).
    before = d1[d1["ts"] < ist_at(day, "00:00")].sort_values("ts")
    if before.empty:
        return None
    closes = before["c"].to_numpy(dtype=float)
    adr = guarded_adr(before["h"].tolist(), before["l"].tolist(), before["c"].tolist())
    h = bars["h"].to_numpy(dtype=float)
    lw = bars["l"].to_numpy(dtype=float)
    c = bars["c"].to_numpy(dtype=float)
    v = bars["v"].to_numpy(dtype=float)
    tp = (h + lw + c) / 3
    cum_v = np.cumsum(v)
    with np.errstate(invalid="ignore", divide="ignore"):
        vw = np.where(cum_v > 0, np.cumsum(tp * v) / cum_v, np.nan)
    return _Day(
        symbol=symbol,
        minute_ts=bars["ts"].to_numpy(dtype=np.int64),
        open=float(bars["o"].iloc[0]),
        cum_high=np.maximum.accumulate(h),
        cum_low=np.minimum.accumulate(lw),
        close=c,
        vwap=vw,
        cum_vol=cum_v,
        prev_close=float(closes[-1]),
        adr=adr,
        raw=bars[["ts", "o", "h", "l", "c", "v"]].to_numpy(dtype=float),
        prev_day=(float(before["h"].iloc[-1]), float(before["l"].iloc[-1]), float(closes[-1])),
    )


def state_at(d: _Day, ts: int) -> DayState | None:
    """The stock as it stood at ``ts``: every 1m bar that had CLOSED by then (start + 60 ≤ ts)."""
    i = int(np.searchsorted(d.minute_ts, ts - 60, side="right")) - 1
    if i < 0:
        return None
    vw = float(d.vwap[i])
    return DayState(
        symbol=d.symbol,
        open=d.open,
        high=float(d.cum_high[i]),
        low=float(d.cum_low[i]),
        last=float(d.close[i]),
        prev_close=d.prev_close,
        adr=d.adr,
        volume=float(d.cum_vol[i]),
        vwap=None if np.isnan(vw) else vw,
    )


class DayReplay:
    """Load a day once; ask for the board at any minute."""

    def __init__(self, store: HistoryStore, day: date, symbols: list[str] | None = None) -> None:
        self.day = day
        # the indices are the market, not candidates: they never rank among the stocks
        names = symbols if symbols is not None else [s for s in store.symbols("1m") if s not in INDEX_ROOTS]
        self.days = {s: d for s in names if (d := load_day(store, s, day)) is not None}
        self.sectors = sectors()

    def minutes(self) -> list[int]:
        """Every minute the board is published: from 09:20 to the close, on the minute + 60 s
        (the scan reads closed bars)."""
        start, end = ist_at(self.day, SCORE_FROM_IST), ist_at(self.day, "15:30")
        return list(range(start, end + 1, 60))

    def board_at(self, ts: int, *, setups: bool = True) -> Board:
        states = [st for d in self.days.values() if (st := state_at(d, ts)) is not None]
        board = build_board(ts, states, self.sectors)
        return attach_setups(board, lambda s: self.structure_at(s, ts)) if setups else board

    def structure_at(self, symbol: str, ts: int) -> Structure:
        """The 5m structure of ``symbol`` from the 5m bars fully closed by ``ts``."""
        d = self.days[symbol]
        open_ts = ist_at(self.day, "09:15")
        last_5m = open_ts + ((ts - open_ts) // 300) * 300
        rows = d.raw[d.raw[:, 0] < last_5m]
        bars = resample_5m([(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows], open_ts)
        return analyze(bars, prev_day=d.prev_day, adr=d.adr)

    def summary(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "symbols": len(self.days),
            "without_adr": sorted(s for s, d in self.days.items() if d.adr is None),
        }


def frames(replay: DayReplay, every_min: int = 1) -> pd.DataFrame:
    """The whole day as one table: minute × symbol → score, rank, side, pick. For research."""
    rows: list[dict[str, Any]] = []
    for k, ts in enumerate(replay.minutes()):
        if k % every_min:
            continue
        b = replay.board_at(ts)
        for r in b.rows:
            rows.append({"ts": ts, "symbol": r.symbol, "score": r.score, "side": r.side, "rank": r.rank, "pick": r.pick, "last": r.last})
    return pd.DataFrame(rows)
