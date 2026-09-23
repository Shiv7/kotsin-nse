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
    #: "week" | "month" — which calendar unit this period is, so completeness can be decided from
    #: the calendar rather than from whichever bars happen to have arrived.
    kind: str
    start: date
    end: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def _WEEK_KEY(d: date) -> tuple[int, int]:
    return d.isocalendar()[:2]


def _MONTH_KEY(d: date) -> tuple[int, int]:
    return (d.year, d.month)


_KEY_FOR: dict[str, object] = {"week": _WEEK_KEY, "month": _MONTH_KEY}


def _aggregate(bars: list[UnifiedBar], key, kind: str) -> list[PeriodOHLC]:
    groups: dict[object, list[UnifiedBar]] = {}
    for b in bars:
        groups.setdefault(key(ist_day(b.ts)), []).append(b)
    out: list[PeriodOHLC] = []
    # Ordered by the calendar, not by the key's repr. ``sorted(groups, key=str)`` put ISO week 9
    # after week 38 -- "(2026, 9)" > "(2026, 38)" as strings -- so ``previous_complete`` returned
    # the last week of February as "last week" for seven months. The group's own earliest day is
    # the only ordering that cannot disagree with the calendar, whatever shape the key has.
    for k in sorted(groups, key=lambda g: min(ist_day(b.ts) for b in groups[g])):
        rows = sorted(groups[k], key=lambda b: b.ts)
        days = [ist_day(r.ts) for r in rows]
        out.append(
            PeriodOHLC(
                label=str(k),
                kind=kind,
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
    return _aggregate(bars, _WEEK_KEY, "week")


def monthly(bars: list[UnifiedBar]) -> list[PeriodOHLC]:
    return _aggregate(bars, _MONTH_KEY, "month")


def previous_complete(periods: list[PeriodOHLC], today: date) -> PeriodOHLC | None:
    """The last period that has definitely finished — i.e. does not contain ``today``.

    Sorts by ``end`` rather than trusting the caller's ordering. ``_aggregate`` already returns
    chronological periods, but this function is the one whose wrong answer is silent: a stale
    pivot is a plausible number in the right units, and it fixed every weekly level in the book
    to February for seven months before anyone had reason to look.
    """
    done = []
    for p in periods:
        keyfn = _KEY_FOR.get(p.kind)
        # Completeness is a calendar fact. ``p.end < today`` alone asks whether any bar has
        # arrived yet, and pre-open — before the first daily bar of the session forms — the
        # running week's last observed day is yesterday, which would present a *partial* week
        # as the previous completed one.
        running = keyfn is not None and keyfn(p.start) == keyfn(today)  # type: ignore[operator]
        if p.end < today and not running:
            done.append(p)
    done.sort(key=lambda p: p.end)
    return done[-1] if done else None
