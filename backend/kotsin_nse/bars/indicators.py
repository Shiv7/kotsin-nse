"""Pure indicator maths. No I/O, no clock, no config — a list of bars in, numbers out.

Every formula here is the one the NSE stack actually ran, not the textbook version, because the
deviations are the part that matters:

* **ATR is Wilder's RMA**, seeded with a simple mean of the first ``p`` true ranges. FUDKII runs
  ``p=7``; the BB-squeeze family ran ``p=14``. Two ATR periods coexisted in one service, so the
  period is always an argument here and never a module constant.
* **SuperTrend bands ratchet**: the upper band may only fall while the trend is down and the lower
  may only rise while it is up. Without that lock the trend flips on noise.
* **Bollinger σ is the population standard deviation** (divide by ``n``, not ``n-1``) — that is what
  ``BBSuperTrendCalculator`` and ``BbSqueezeDetector`` both computed, and matching it matters more
  than being statistically fastidious, because the live thresholds were fitted against it.

Parameters are arguments, never constants. ``fudkii.trigger.bb.period`` existing in a properties
file while ``BB_PERIOD=20`` was hardcoded in the calculator — so tuning it changed nothing but a log
line — is the single most expensive dead-config bug in the old codebase.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Protocol


class OHLCV(Protocol):
    open: float
    high: float
    low: float
    close: float
    volume: float


def sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def pstdev(values: Sequence[float], period: int) -> float | None:
    """Population σ over the last ``period`` values — the divisor the live calculators used."""
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    mean = sum(window) / period
    return math.sqrt(sum((v - mean) ** 2 for v in window) / period)


@dataclass(frozen=True, slots=True)
class Bollinger:
    upper: float
    middle: float
    lower: float
    width: float  # (upper - lower) / middle — dimensionless, comparable across instruments

    @property
    def valid(self) -> bool:
        return self.middle > 0


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> Bollinger | None:
    mid = sma(closes, period)
    sd = pstdev(closes, period)
    if mid is None or sd is None:
        return None
    upper, lower = mid + mult * sd, mid - mult * sd
    return Bollinger(upper, mid, lower, (upper - lower) / mid if mid > 0 else 0.0)


def true_ranges(bars: Sequence[OHLCV]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        out.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    return out


def atr_series(bars: Sequence[OHLCV], period: int) -> list[float | None]:
    """Wilder's RMA of true range, aligned to ``bars`` (``None`` until warm)."""
    out: list[float | None] = [None] * len(bars)
    trs = true_ranges(bars)
    if len(trs) < period or period <= 0:
        return out
    prev = sum(trs[:period]) / period
    out[period] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i + 1] = prev
    return out


def atr(bars: Sequence[OHLCV], period: int) -> float | None:
    series = atr_series(bars, period)
    return series[-1] if series else None


@dataclass(frozen=True, slots=True)
class SuperTrendPoint:
    trend: int  # +1 up, -1 down
    upper: float
    lower: float
    value: float  # the active band — the line a chart would draw


def supertrend(
    bars: Sequence[OHLCV], atr_period: int = 7, mult: float = 3.0
) -> list[SuperTrendPoint | None]:
    """SuperTrend with the standard band lock, recomputed over the whole window.

    Recomputing (rather than carrying state across calls) is deliberate: the old service persisted
    ``SuperTrendState`` to Mongo and then recomputed from 200 bars anyway, precisely so a restart
    could not change a signal. A pure function makes that free.
    """
    n = len(bars)
    out: list[SuperTrendPoint | None] = [None] * n
    atrs = atr_series(bars, atr_period)
    trend = 1
    final_upper = final_lower = 0.0
    started = False
    for i in range(n):
        a = atrs[i]
        if a is None or a <= 0:
            continue
        b = bars[i]
        hl2 = (b.high + b.low) / 2
        basic_upper, basic_lower = hl2 + mult * a, hl2 - mult * a
        if not started:
            final_upper, final_lower = basic_upper, basic_lower
            trend = 1 if b.close >= hl2 else -1
            started = True
        else:
            prev_close = bars[i - 1].close
            final_upper = (
                basic_upper if (basic_upper < final_upper or prev_close > final_upper) else final_upper
            )
            final_lower = (
                basic_lower if (basic_lower > final_lower or prev_close < final_lower) else final_lower
            )
            if trend == 1 and b.close < final_lower:
                trend = -1
            elif trend == -1 and b.close > final_upper:
                trend = 1
        out[i] = SuperTrendPoint(
            trend=trend,
            upper=final_upper,
            lower=final_lower,
            value=final_lower if trend == 1 else final_upper,
        )
    return out


def bars_in_trend(points: Sequence[SuperTrendPoint | None]) -> int:
    """How many bars the current trend has run, counting back through the unchanged run."""
    if not points or points[-1] is None:
        return 0
    trend = points[-1].trend
    count = 0
    for p in reversed(points):
        if p is None or p.trend != trend:
            break
        count += 1
    return count


def volume_baseline(
    volumes: Sequence[float], *, window: int, skip_last: int, floor: float = 0.0
) -> float | None:
    """Mean of ``window`` volumes ending ``skip_last`` bars before the end.

    ``skip_last=2`` is FUKAA's: the baseline is ``T-2 … T-(window+1)``, so **neither** the bar
    under test **nor** the one before it can inflate the average they are compared against. Getting
    this off by one silently changes how selective the gate is.

    ``floor`` is a hard minimum on the baseline (FUKAA used 1000). Without it a dead scrip whose
    six-bar average is three shares manufactures a 400x "surge" out of one retail order.

    Returns ``None`` — not 0, and not 1 — when history is short: 0 would read as "fails the bar"
    and 1 as "passes", and both are guesses. The caller's gate decides what missing means.
    """
    if window <= 0 or skip_last < 0:
        return None
    end = len(volumes) - skip_last
    start = end - window
    if start < 0 or end <= 0:
        return None
    base = volumes[start:end]
    if not base:
        return None
    return max(sum(base) / len(base), floor)


def volume_surges(
    volumes: Sequence[float], *, window: int, floor: float = 0.0
) -> tuple[float | None, float | None, float | None]:
    """FUKAA's pair: ``(surge_T, surge_T-1, baseline)`` — **both against the same baseline**.

    Sharing one baseline is not an implementation detail. Recomputing a shifted baseline for
    ``T-1`` would compare the two candidate bars against two different denominators, so "the higher
    of the two surges" would no longer mean what it says.
    """
    base = volume_baseline(volumes, window=window, skip_last=2, floor=floor)
    if base is None or base <= 0 or len(volumes) < 2:
        return None, None, base
    return volumes[-1] / base, volumes[-2] / base, base


def volume_surge_median(volumes: Sequence[float], window: int) -> float | None:
    """CAN2's shape: current volume over the **median** of the prior ``window`` bars, current bar
    excluded. Kept distinct from the mean form because the two are not interchangeable — the median
    is robust to one huge bar in the lookback, the mean is not."""
    if window <= 0 or len(volumes) < window + 1:
        return None
    base = volumes[-window - 1 : -1]
    med = median(base) if base else 0.0
    return volumes[-1] / med if med > 0 else None


def vwap(bars: Sequence[OHLCV]) -> float | None:
    """Volume-weighted average of the typical price over exactly the bars given. The *caller*
    decides the window — a session VWAP passes today's bars, a rolling VWAP passes the last N."""
    vol = sum(b.volume for b in bars)
    if vol <= 0:
        return None
    return sum((b.high + b.low + b.close) / 3 * b.volume for b in bars) / vol


def body_outside_ratio(bar: OHLCV, level: float, *, above: bool) -> float:
    """Fraction of the candle **body** (not the wick) beyond ``level``. The BB-squeeze family's
    ``MIN_BODY_OUTSIDE = 0.50`` bar. A doji with a long wick through the band scores ~0."""
    top, bottom = max(bar.open, bar.close), min(bar.open, bar.close)
    body = top - bottom
    if body <= 0:
        return 1.0 if ((bar.close > level) if above else (bar.close < level)) else 0.0
    outside = (top - max(bottom, level)) if above else (min(top, level) - bottom)
    return max(0.0, min(1.0, outside / body))


def close_position_in_range(bar: OHLCV) -> float | None:
    """``(close − low) / (high − low)`` — 1.0 closes on the high, 0.0 on the low.

    FUDKII-RT's body-opposition gate reads this: a "rejection" only counts when the close sits in
    the opposing 60% of its own range.
    """
    rng = bar.high - bar.low
    return None if rng <= 0 else (bar.close - bar.low) / rng


def pct_change(new: float, old: float) -> float | None:
    return None if old == 0 else (new - old) / old * 100
