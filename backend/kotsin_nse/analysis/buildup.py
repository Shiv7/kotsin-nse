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
#: (minutes, name, the OI change under which it reads FLAT): OI moves less in 15 minutes than in a day
#: (NIFTY and ETERNAL futures 2026-10-23: 15-minute changes of −0.35 % … +0.20 %), so a day's 0.3 %
#: would read every short window as FLAT. A display threshold, not a tested one.
INTRADAY = ((15, "15 min", 0.1), (60, "1 hour", 0.2))
#: an intraday window is read only if both series have a point this close to its start: across a
#: recording gap the "value 15 minutes ago" is really the last one before the gap (HDFCBANK
#: 2026-10-23: unsubscribed 09:44–10:42, so its 15-minute and 1-hour windows showed the same OI)
EDGE_S = 180.0


def label(price_chg: float, oi_chg_pct: float, flat: float = FLAT_OI_PCT) -> str:
    if abs(oi_chg_pct) < flat:
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
    for minutes, name, flat in INTRADAY:
        p0, o0 = _at(price, now - minutes * 60, EDGE_S), _at(oi, now - minutes * 60, EDGE_S)
        if p_now and oi_now and p0 and o0:
            pc, oc = (p_now / p0 - 1) * 100, (oi_now / o0 - 1) * 100
            out.append({"window": name, "price_chg_pct": round(pc, 2), "oi_chg_pct": round(oc, 2), "oi_chg": oi_now - o0, "build_up": label(pc, oc, flat)})
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


def recent(
    price: Sequence[tuple[float, float]],
    fut_oi: Sequence[tuple[float, float]],
    options: Sequence[tuple[str, float, Sequence[tuple[float, float]]]],
    now: float,
) -> list[dict[str, Any]]:
    """Who is positioning over the last 15 minutes and the last hour — the turn a since-the-open
    label hides (NIFTY 2026-10-23: "LU" and "Strong Bear" all morning while the double bottom of
    11:00–11:05 formed with put writing at 22,400–22,450 and fresh longs on the break).

    ``options`` = (CE | PE, strike, OI series) for the nearest expiry. The calls' and puts' change
    is summed over the strikes that have an OI point at both ends of the window, so a strike
    subscribed or dropped mid-window (strike re-centring) cannot show as a jump; the PCR is over
    the same strikes. A window is skipped where the futures have no point near its start."""
    out: list[dict[str, Any]] = []
    p_now, o_now = (price[-1][1] if price else None), (fut_oi[-1][1] if fut_oi else None)
    for minutes, name, flat in INTRADAY:
        t0 = now - minutes * 60
        p0, o0 = _at(price, t0, EDGE_S), _at(fut_oi, t0, EDGE_S)
        if not (p_now and o_now and p0 and o0):
            continue
        pc, oc = (p_now / p0 - 1) * 100, (o_now / o0 - 1) * 100
        legs: dict[str, dict[str, float]] = {"CE": {"then": 0.0, "now": 0.0, "n": 0}, "PE": {"then": 0.0, "now": 0.0, "n": 0}}
        for kind, _strike, s in options:
            a, b = _at(s, t0), (s[-1][1] if s else None)
            if a is not None and b is not None and kind in legs:
                legs[kind]["then"] += a
                legs[kind]["now"] += b
                legs[kind]["n"] += 1
        row: dict[str, Any] = {"window": name, "futures": {"price_chg_pct": round(pc, 2), "oi_chg_pct": round(oc, 2),
                                                           "oi_chg": o_now - o0, "build_up": label(pc, oc, flat)}}
        for kind, key in (("CE", "calls"), ("PE", "puts")):
            g = legs[kind]
            row[key] = {"oi_chg": g["now"] - g["then"], "oi_chg_pct": round((g["now"] / g["then"] - 1) * 100, 2) if g["then"] else None,
                        "strikes": int(g["n"])} if g["n"] else None
        ce, pe = legs["CE"], legs["PE"]
        if ce["then"] and ce["now"] and ce["n"] and pe["n"]:
            row["pcr_then"], row["pcr_now"] = round(pe["then"] / ce["then"], 3), round(pe["now"] / ce["now"], 3)
        out.append(row)
    return out


__all__ = ["FLAT_OI_PCT", "label", "mtf", "recent"]
