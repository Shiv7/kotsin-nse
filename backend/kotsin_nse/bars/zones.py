"""The day's pivot zones — ONE builder for the live engine and the backtest.

Live and the backtest each built their own: live clustered at ``k × ATR30 / price`` (0.30 × ATR in a
NEUTRAL India VIX, ~0.08-0.17 % of price), the backtest at a flat 0.25 % — on the same 8,393 historical
triggers 14.5 % of the publish decisions, 18 % of the grades and a third of the stops differed, so every
research verdict was measured on geometry live does not trade (review, 2026-10-03).

Three more things the two did not guard, all here now:

* **The width is fixed before the session.** Live took ATR30 and the LTP at the FIRST call of the
  day, so a restart after 13:15 changed which levels clustered on ~30 % of symbol-days. The
  tolerance is the previous sessions' ATR30 over the previous close — the same at 09:00 and at 15:00.
* **A provisional daily candle is not a session.** On NSE 5paisa serves a day twice: a provisional row
  stamped at the open (09:15) whose high and low can still be wrong (32 / 27 of 204 names fetched
  overnight), and the end-of-day row stamped 00:00. Only the latter sets levels; until it lands the
  name has none. (MCX stamps its daily candle at the first trade and never at 00:00 — no such test.)
* **The two series must be on one price basis.** A corporate action adjusts the daily candles and not
  the 30m ones: VEDL's daily series ran at ×0.374 for 148 days, so price sat at 766 against zones at
  131-376 and a bearish trigger read grade A at RR 59.5. A session whose official close and its own
  last 30m close are more than 8 % apart sets no levels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ..config import Segment
from ..market.session import ist_day, ist_hm
from .daily import MIN_DAILY_BARS, basis_ok, is_official, previous_session
from .indicators import atr
from .periods import monthly, previous_complete, weekly
from .pivots import (
    ZONE_TOLERANCE_PCT,
    PivotPoint,
    Zone,
    classic_pivots,
    cluster_zones,
    pivot_points,
)
from .unified import UnifiedBar

#: the ATR the width is measured in: ATR(14) over the last 60 decision bars before the session
ATR_PERIOD = 14
ATR_BARS = 60
#: an NSE daily candle stamped at the open is the provisional one
NSE_PROVISIONAL_HM = "09:15"


@dataclass(frozen=True, slots=True)
class ZoneBuild:
    zones: list[Zone]
    tolerance_pct: float
    points: list[PivotPoint] = field(default_factory=list)
    #: why there are no zones — "history", "provisional", "basis" — or "" when there are
    refused: str = ""
    detail: str = ""


def is_provisional(bar: UnifiedBar, segment: Segment) -> bool:
    """An NSE daily row stamped at the session open is 5paisa's provisional candle."""
    return segment is not Segment.MCX_FO and ist_hm(bar.ts) == NSE_PROVISIONAL_HM


def pivot_points_for(dailies: Sequence[UnifiedBar], today: date) -> list[PivotPoint]:
    """Classic levels in force on ``today``: the previous session's, and the previous completed week's
    and month's, with their timeframe weights."""
    prior = [b for b in dailies if ist_day(b.ts) < today]
    prev = previous_session(prior, today)
    if prev is None:
        return []
    points: list[PivotPoint] = []
    lv = classic_pivots(prev.high, prev.low, prev.close)
    if lv:
        points += pivot_points(lv, "1d")
    for tf, periods in (("1wk", weekly(prior)), ("1mo", monthly(prior))):
        p = previous_complete(periods, today)
        if p:
            lv = classic_pivots(p.high, p.low, p.close)
            if lv:
                points += pivot_points(lv, tf)
    return points


def session_tolerance(intraday: Sequence[UnifiedBar], today: date, k: float) -> float:
    """``k × ATR30 / close`` in percent, from the decision bars BEFORE ``today`` only — so the width
    is the same whenever in the day it is first asked for. No ATR: the documented 0.25 %."""
    prior = [b for b in intraday if ist_day(b.ts) < today][-ATR_BARS:]
    a = atr(prior, ATR_PERIOD) if prior else None
    px = prior[-1].close if prior else 0.0
    return k * a / px * 100 if a and px > 0 else ZONE_TOLERANCE_PCT


def build_zones(
    dailies: Sequence[UnifiedBar],
    intraday: Sequence[UnifiedBar],
    today: date,
    *,
    k: float,
    segment: Segment,
) -> ZoneBuild:
    """The zones in force on ``today`` for one name, or none and why."""
    prior = [b for b in dailies if ist_day(b.ts) < today]
    prev = previous_session(prior, today)
    if len(prior) < MIN_DAILY_BARS or prev is None or not is_official(prev):
        return ZoneBuild([], ZONE_TOLERANCE_PCT, refused="history",
                         detail=f"{len(prior)} official daily candles before {today}")
    if is_provisional(prev, segment):
        return ZoneBuild([], ZONE_TOLERANCE_PCT, refused="provisional",
                         detail=f"{ist_day(prev.ts)} is still the provisional 09:15 candle")
    session = [b for b in intraday if ist_day(b.ts) == ist_day(prev.ts)]
    if session:
        # the last CONTINUOUS bar: an NSE stock's 15:15 bar is set from the official close itself
        last = next((b for b in reversed(session) if ist_hm(b.ts) <= "14:45"), session[-1])
        if not basis_ok(prev.close, last.close):
            return ZoneBuild([], ZONE_TOLERANCE_PCT, refused="basis",
                             detail=f"{ist_day(prev.ts)} official close {prev.close:g} vs its 30m close {last.close:g}")
    points = pivot_points_for(prior, today)
    tol = session_tolerance(intraday, today, k)
    return ZoneBuild(cluster_zones(points, tolerance_pct=tol), tol, points)


__all__ = ["ATR_BARS", "ATR_PERIOD", "ZoneBuild", "build_zones", "is_provisional", "pivot_points_for", "session_tolerance"]
