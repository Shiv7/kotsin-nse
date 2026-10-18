"""Strength: how far a stock has gone one way today, measured in its own normal day.

``strength = (last − open) / average daily range`` — positive bullish, negative bearish — and **zero
unless the day is one-sided**: the excursion on the trade's side of the open must exceed
``ONE_SIDED_RATIO`` × the excursion on the other side. A stock that ran up and came straight back
scores nothing, because its last price is back at the open; a stock that whipsawed both ways
scores nothing, because neither side dominates.

The average daily range is the mean high − low of the last ``ADR_SESSIONS`` completed sessions
(prices adjusted for splits and bonuses upstream), fixed for the day. Scores are not published
before ``SCORE_FROM_IST`` — the first minutes after the open are auction noise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

ADR_SESSIONS = 50
ONE_SIDED_RATIO = 2.0
SCORE_FROM_IST = "09:20"


@dataclass(frozen=True, slots=True)
class DayState:
    """What the scanner needs about one stock, at one minute."""

    symbol: str
    open: float
    high: float
    low: float
    last: float
    prev_close: float
    adr: float | None  # average daily range in rupees; None = not enough history
    volume: float = 0.0
    vwap: float | None = None


def average_daily_range(highs: Sequence[float], lows: Sequence[float], n: int = ADR_SESSIONS) -> float | None:
    """Mean high − low over the last ``n`` completed sessions (oldest first in, today excluded)."""
    pairs = [(h, lo) for h, lo in zip(highs, lows, strict=True) if h > 0 and lo > 0 and h >= lo]
    if len(pairs) < max(10, n // 2):
        return None
    tail = pairs[-n:]
    return sum(h - lo for h, lo in tail) / len(tail)


def strength(d: DayState, ratio: float = ONE_SIDED_RATIO) -> float:
    if not d.adr or d.adr <= 0 or d.open <= 0:
        return 0.0
    up = d.high - d.open
    down = d.open - d.low
    if d.last > d.open and up > ratio * down:
        return (d.last - d.open) / d.adr
    if d.last < d.open and down > ratio * up:
        return (d.last - d.open) / d.adr
    return 0.0


def pct_change(d: DayState) -> float | None:
    return (d.last / d.prev_close - 1) * 100 if d.prev_close > 0 else None


def vwap(bars: Sequence[tuple[float, float, float, float]]) -> float | None:
    """Session VWAP from (high, low, close, volume) bars, typical price weighted by volume."""
    pv = vol = 0.0
    for h, lo, c, v in bars:
        if v > 0:
            pv += (h + lo + c) / 3 * v
            vol += v
    return pv / vol if vol > 0 else None
