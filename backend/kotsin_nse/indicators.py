"""Indicators shared by the live strategies and the research backtests, on plain numpy arrays.

One implementation per indicator, so the rule a backtest scores is the rule the engine trades. The
Supertrend here is ``strategy/cpr_retest.py::supertrend_dir`` bar for bar: Wilder's ATR seeded with
the first bar's range, each band ratcheting until a close crosses it, and the direction judged
against the CURRENT bar's ratcheted band (the standard rule — a research copy once judged it against
the previous bar's band, which flips a bar late whenever the band moved).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def wilder_atr(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's ATR(n): the true range smoothed with alpha 1 / n, seeded with the first bar's own
    range (it has no previous close, so that IS its true range)."""
    h, lo, c = (np.asarray(x, dtype=float) for x in (h, lo, c))
    pc = np.r_[c[0], c[:-1]] if len(c) else c
    tr = np.maximum.reduce([h - lo, np.abs(h - pc), np.abs(lo - pc)])
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def supertrend(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 10, mult: float = 3.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Supertrend(n, mult) per bar: (direction, upper band, lower band).

    Bands ``mult`` × ATR(n) around each bar's (H + L) / 2. From the second bar on, the upper band
    only falls (and the lower only rises) until a close crosses it, which resets it to the bar's own
    band. The direction turns up on a close above the current ratcheted upper band and down on a
    close below the current lower band. Direction is +1 / −1 — and 0 for the first ``n + 1`` bars,
    too few to read, exactly as the live ``supertrend_dir`` reports a short series.
    """
    h, lo, c = (np.asarray(x, dtype=float) for x in (h, lo, c))
    a = wilder_atr(h, lo, c, n)
    mid = (h + lo) / 2
    ub, lb = mid + mult * a, mid - mult * a
    fu, fl = ub.copy(), lb.copy()
    d = np.ones(len(c), dtype=int)
    for i in range(1, len(c)):
        if i > 1:  # the first bar has no band to ratchet from: bar 1 starts with its own
            fu[i] = ub[i] if ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1] else fu[i - 1]
            fl[i] = lb[i] if lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1] else fl[i - 1]
        d[i] = (-1 if c[i] < fl[i] else 1) if d[i - 1] > 0 else (1 if c[i] > fu[i] else -1)
    d[: n + 1] = 0
    return d, fu, fl


__all__ = ["supertrend", "wilder_atr"]
