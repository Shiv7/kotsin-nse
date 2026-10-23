"""Build-up across timeframes: who is adding or closing positions over 15 minutes, an hour, the
day so far, five and fifteen sessions — not the minute, which is noise.

Each window compares the price and the front future's OI at its start and end: price up + OI up =
long build-up (LB), down + up = short build-up (SB), up + down = short covering (SC), down + down =
long unwinding (LU); an OI change under ``FLAT_OI_PCT`` is FLAT. Intraday windows use the cash
price (the future moves with it) and the future's OI as the feed delivered it; the day's window
measures from NSE's closing OI of the previous session; the multi-session ones come from the
bhavcopy (``analysis/positioning.py``)."""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from typing import Any

FLAT_OI_PCT = 0.3
INTRADAY = ((15, "15 min"), (60, "1 hour"))
#: an intraday window is read only if both series have a point this close to its start: across a
#: recording gap the "value 15 minutes ago" is really the last one before the gap (HDFCBANK
#: 2026-10-23: unsubscribed 09:44–10:42, so its 15-minute and 1-hour windows showed the same OI)
EDGE_S = 180.0


def label(price_chg: float, oi_chg_pct: float) -> str:
    if abs(oi_chg_pct) < FLAT_OI_PCT:
        return "FLAT"
    if oi_chg_pct > 0:
        return "LB" if price_chg >= 0 else "SB"
    return "SC" if price_chg >= 0 else "LU"


def _at(series: Sequence[tuple[float, float]], ts: float, within: float | None = None) -> float | None:
    """The last value at or before ``ts`` — no older than ``within`` seconds, when given."""
    i = bisect.bisect_right([t for t, _v in series], ts) - 1
    if i < 0 or (within is not None and ts - series[i][0] > within):
        return None
    return series[i][1]


def mtf(
    price: Sequence[tuple[float, float]],
    oi: Sequence[tuple[float, float]],
    now: float,
    *,
    prev_close: float | None,
    oi_ref: float | None,
    sessions: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """``price`` / ``oi`` = (ts, value), oldest first; ``sessions`` = positioning rows, oldest first."""
    out: list[dict[str, Any]] = []
    p_now, oi_now = (price[-1][1] if price else None), (oi[-1][1] if oi else None)
    for minutes, name in INTRADAY:
        p0, o0 = _at(price, now - minutes * 60, EDGE_S), _at(oi, now - minutes * 60, EDGE_S)
        if p_now and oi_now and p0 and o0:
            pc, oc = (p_now / p0 - 1) * 100, (oi_now / o0 - 1) * 100
            out.append({"window": name, "price_chg_pct": round(pc, 2), "oi_chg_pct": round(oc, 2), "oi_chg": oi_now - o0, "build_up": label(pc, oc)})
    if p_now and oi_now and prev_close and oi_ref:
        pc, oc = (p_now / prev_close - 1) * 100, (oi_now / oi_ref - 1) * 100
        out.append({"window": "today", "price_chg_pct": round(pc, 2), "oi_chg_pct": round(oc, 2), "oi_chg": oi_now - oi_ref, "build_up": label(pc, oc)})
    for n in (5, 15):
        if len(sessions) > n:
            a, b = sessions[-n - 1], sessions[-1]
            if a["fut_oi"] and a["fut_close"]:
                pc, oc = (b["fut_close"] / a["fut_close"] - 1) * 100, (b["fut_oi"] / a["fut_oi"] - 1) * 100
                out.append({"window": f"{n} sessions", "price_chg_pct": round(pc, 2), "oi_chg_pct": round(oc, 2),
                            "oi_chg": b["fut_oi"] - a["fut_oi"], "build_up": label(pc, oc)})
    return out


__all__ = ["FLAT_OI_PCT", "label", "mtf"]
