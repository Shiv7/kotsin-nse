"""Daily bars → the previous completed week and month, for multi-timeframe pivots.

Pivots are computed from the **previous completed period**, never the running one: this week's
pivot is derived from last week's high/low/close and is fixed for the whole week. Recomputing it
from a partial week would move every level intraday, which is the opposite of what a pivot is for.

The week is Monday–Friday (an Indian trading week); the month is a calendar month. Both are taken
from the daily series we already hold rather than from a second broker call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..market.session import ist_day
from .unified import UnifiedBar


@dataclass(frozen=True, slots=True)
class PeriodOHLC:
    label: str
    start: date
    end: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def _aggregate(bars: list[UnifiedBar], key) -> list[PeriodOHLC]:
    groups: dict[object, list[UnifiedBar]] = {}
    for b in bars:
        groups.setdefault(key(ist_day(b.ts)), []).append(b)
    out: list[PeriodOHLC] = []
    for k in sorted(groups, key=str):
        rows = sorted(groups[k], key=lambda b: b.ts)
        days = [ist_day(r.ts) for r in rows]
        out.append(
            PeriodOHLC(
                label=str(k),
                start=min(days),
                end=max(days),
                open=rows[0].open,
                high=max(r.high for r in rows),
                low=min(r.low for r in rows),
                close=rows[-1].close,
                volume=sum(r.volume for r in rows),
            )
        )
    return out


def weekly(bars: list[UnifiedBar]) -> list[PeriodOHLC]:
    return _aggregate(bars, lambda d: d.isocalendar()[:2])


def monthly(bars: list[UnifiedBar]) -> list[PeriodOHLC]:
    return _aggregate(bars, lambda d: (d.year, d.month))


def previous_complete(periods: list[PeriodOHLC], today: date) -> PeriodOHLC | None:
    """The last period that has definitely finished — i.e. does not contain ``today``."""
    done = [p for p in periods if p.end < today]
    return done[-1] if done else None
