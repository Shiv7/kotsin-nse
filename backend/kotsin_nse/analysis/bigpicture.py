"""The daily picture: trend, where the price sits in its ranges, volatility, the last sessions, and
how much of the last major swing has been retraced (Fibonacci).

Swing: the highest high and the lowest low of the last ``SWING_SESSIONS`` sessions. Whichever came
LATER sets the direction of the move — a high after the low is an up-move (retracement measured
down from the high), a low after the high a down-move (measured up from the low).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import date
from typing import Any

from ..market.session import ist_day

SWING_SESSIONS = 60
FIB = (0.236, 0.382, 0.5, 0.618, 0.786)

Daily = Sequence[tuple[int, float, float, float, float, float]]  # ts, o, h, l, c, v (completed sessions, oldest first)


def _sma(c: Sequence[float], n: int) -> float | None:
    return statistics.fmean(c[-n:]) if len(c) >= n else None


def _trend(last: float, s20: float | None, s50: float | None, s200: float | None) -> str:
    if s50 and s200:
        if last > s50 > s200:
            return "UP (above the 50- and 200-day, 50 above 200)"
        if last < s50 < s200:
            return "DOWN (below the 50- and 200-day, 50 below 200)"
    if s20 and s50:
        return "MIXED (" + ("above" if last > s20 else "below") + " the 20-day, " + ("above" if last > s50 else "below") + " the 50-day)"
    return "UNKNOWN"


def fibonacci(rows: Daily, last: float, sessions: int = SWING_SESSIONS) -> dict[str, Any] | None:
    w = list(rows)[-sessions:]
    if len(w) < 10:
        return None
    hi_i = max(range(len(w)), key=lambda i: w[i][2])
    lo_i = min(range(len(w)), key=lambda i: w[i][3])
    hi, lo = w[hi_i][2], w[lo_i][3]
    if hi <= lo:
        return None
    up = hi_i > lo_i  # the high came last: an up-move, retraced from the top
    retraced = (hi - last) / (hi - lo) if up else (last - lo) / (hi - lo)
    levels = [{"ratio": r, "price": round(hi - r * (hi - lo), 2) if up else round(lo + r * (hi - lo), 2)} for r in FIB]
    return {
        "move": "UP" if up else "DOWN",
        "swing_high": hi, "swing_high_day": ist_day(int(w[hi_i][0])).isoformat(),
        "swing_low": lo, "swing_low_day": ist_day(int(w[lo_i][0])).isoformat(),
        "retraced_pct": round(retraced * 100, 1),
        "levels": levels,
        "note": f"{'up' if up else 'down'}-move {lo:,.1f}→{hi:,.1f}" if up else f"down-move {hi:,.1f}→{lo:,.1f}",
    }


def big_picture(rows: Daily, today: date, last: float | None = None) -> dict[str, Any] | None:
    """``rows`` = completed daily sessions before ``today``; ``last`` = the live price (else the last close)."""
    rows = [r for r in rows if ist_day(int(r[0])) < today and r[2] > 0 and r[3] > 0]
    if len(rows) < 30:
        return None
    c = [r[4] for r in rows]
    px = last or c[-1]
    out: dict[str, Any] = {"last": px, "prev_close": c[-1], "sessions": len(rows)}
    for n, lab in ((20, "1m"), (60, "3m"), (250, "1y")):
        if len(rows) > n:
            w = rows[-n:]
            hi, lo = max(r[2] for r in w), min(r[3] for r in w)
            out[lab] = {"return_pct": round((px / c[-n - 1] - 1) * 100, 2), "high": hi, "low": lo,
                        "position_pct": round((px - lo) / (hi - lo) * 100, 1) if hi > lo else None}
    s20, s50, s200 = _sma(c, 20), _sma(c, 50), _sma(c, 200)
    out["sma"] = {"20": s20 and round(s20, 2), "50": s50 and round(s50, 2), "200": s200 and round(s200, 2)}
    out["trend"] = _trend(px, s20, s50, s200)
    rng = [r[2] - r[3] for r in rows]
    out["adr20"] = round(statistics.fmean(rng[-20:]), 2)
    out["adr20_pct"] = round(statistics.fmean([(r[2] - r[3]) / r[4] for r in rows[-20:]]) * 100, 2)
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    out["vol20_ann_pct"] = round(statistics.pstdev(rets[-20:]) * math.sqrt(250) * 100, 1)
    out["vol250_ann_pct"] = round(statistics.pstdev(rets[-250:]) * math.sqrt(250) * 100, 1) if len(rets) > 60 else None
    # weekly and monthly direction: the last three completed closes of each
    wk: dict[tuple[int, int], float] = {}
    mo: dict[tuple[int, int], float] = {}
    for r in rows:
        d = ist_day(int(r[0]))
        wk[d.isocalendar()[:2]] = r[4]
        mo[(d.year, d.month)] = r[4]
    wkc = [wk[k] for k in sorted(wk)][-4:]
    moc = [mo[k] for k in sorted(mo)][-4:]
    out["weekly_closes"] = [round(x, 2) for x in wkc]
    out["monthly_closes"] = [round(x, 2) for x in moc]
    out["last_sessions"] = [
        {"day": ist_day(int(r[0])).isoformat(), "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": int(r[5])} for r in rows[-8:]
    ]
    out["fibonacci"] = fibonacci(rows, px)
    return out


__all__ = ["FIB", "SWING_SESSIONS", "big_picture", "fibonacci"]
