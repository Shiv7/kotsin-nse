"""The ONLY module in this package that knows about IST.

The NSE stack hard-coded ``Asia/Kolkata`` in 61 classes in one service and 45 in another, which is
why none of its maths was portable. Here every other module speaks **UTC epoch seconds**; this file
converts, and it owns every session rule: when a market is open, where a bar boundary falls, when a
book is force-flattened, and whether a given day trades at all.

Boundaries are anchored on the **session open**, not on the hour. NSE cash opens 09:15, so the 30m
grid is 09:15 / 09:45 / … / 15:15 — exactly the boundaries the old ``FudkiiSignalTrigger`` fired on.
Anchoring on the hour instead would put a boundary at 09:30 and silently shift every SuperTrend bar.

The opening minute is **09:15, and it is included**. The old ``tick_candles_1m`` collection started
at 09:16 and lost the minute that often holds the day's extreme; a bar builder that buckets by
``floor((t - open) / 60)`` cannot make that mistake.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import Segment

IST = ZoneInfo("Asia/Kolkata")

TF_SECONDS: dict[str, int] = {"1m": 60, "2m": 120, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600}
INTRADAY_TFS: tuple[str, ...] = ("1m", "2m", "3m", "5m", "15m", "30m", "60m")


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """One segment's daily clock, in IST wall time."""

    open: time
    close: time
    #: entries stop here — the last window in which a new position may be opened
    entry_cutoff: time
    #: every open position is flattened here (10 minutes before close, as validated on NSE cash)
    force_flat: time

    def contains(self, t: time) -> bool:
        return self.open <= t <= self.close


# MCX closes 23:30 in winter and 23:55 in summer. 23:30 is the conservative choice: flattening at
# 23:20 is inside both. The old stack's single NSE-shaped 15:20 constant would have closed every
# commodity position eight hours early — see can2_consumer SEGMENT_FLAT.
# The entry cutoff here is FUDKII's end-of-day PHASE (a bar starting 14:45 or later takes strong
# signals only); the last minute a book may place an NSE entry is the engine's (engine.NSE_LAST_ENTRY_HM),
# and so is the graded-F shadow's later flatten (engine.FORCE_FLAT_HM, 15:24 — every NSE position out
# by 15:25, operator 2026-09-29).
SESSIONS: dict[Segment, SessionSpec] = {
    Segment.NSE_EQ: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 20)),
    Segment.NSE_FO: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 20)),
    Segment.NSE_IDX: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 20)),
    Segment.MCX_FO: SessionSpec(time(9, 0), time(23, 30), time(23, 0), time(23, 20)),
}


class TradingCalendar:
    """Weekday sessions minus an explicit holiday list.

    The holiday list is **data, not code**: ``data/holidays.txt`` holds one ``YYYY-MM-DD`` per line.
    An empty or missing file means weekdays-only, which is wrong on about a dozen days a year — so
    :meth:`missing_holidays_warning` says so at boot rather than letting a silent assumption ride.
    Never guess a holiday: a wrong entry skips a real trading day, and a missing one produces a
    session of empty bars that looks like a feed outage.
    """

    def __init__(self, holidays: frozenset[date] | None = None) -> None:
        self.holidays: frozenset[date] = holidays or frozenset()

    @classmethod
    def from_file(cls, path: Path | None) -> TradingCalendar:
        if path is None or not path.exists():
            return cls()
        days: set[date] = set()
        for raw in path.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                days.add(date.fromisoformat(line))
        return cls(frozenset(days))

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def missing_holidays_warning(self) -> str | None:
        if self.holidays:
            return None
        return (
            "no holiday list loaded — every weekday counts as a trading day. "
            "Populate data/holidays.txt (one YYYY-MM-DD per line) from the exchange circular."
        )

    def previous_trading_day(self, d: date) -> date:
        cur = d - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        return cur


# ---- conversions ------------------------------------------------------------------------------


def to_ist(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(IST)


def from_ist(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.timestamp()


def ist_naive_to_ts(text: str) -> float:
    """Parse a broker timestamp (``2026-09-19T11:05:00`` / ``2026-09-19 11:05:00``), which 5paisa
    always returns as naive IST, into epoch seconds."""
    return from_ist(datetime.fromisoformat(text.replace("T", " ")))


def ist_day(ts: float) -> date:
    return to_ist(ts).date()


def ist_today() -> date:
    """Today in Kolkata. ``date.today()`` is the machine's date, and a UTC box is a day behind
    between 00:00 and 05:30 IST — which is when a pre-open boot picks its previous session."""
    return datetime.now(IST).date()


def ist_hm(ts: float) -> str:
    return to_ist(ts).strftime("%H:%M")


# ---- session queries ---------------------------------------------------------------------------


def spec(segment: Segment) -> SessionSpec:
    return SESSIONS[segment]


def session_open_ts(segment: Segment, d: date) -> float:
    return from_ist(datetime.combine(d, SESSIONS[segment].open, tzinfo=IST))


def session_close_ts(segment: Segment, d: date) -> float:
    return from_ist(datetime.combine(d, SESSIONS[segment].close, tzinfo=IST))


def is_open(segment: Segment, ts: float, calendar: TradingCalendar) -> bool:
    dt = to_ist(ts)
    return calendar.is_trading_day(dt.date()) and SESSIONS[segment].contains(dt.time())


def in_entry_window(segment: Segment, ts: float, calendar: TradingCalendar) -> bool:
    dt = to_ist(ts)
    s = SESSIONS[segment]
    return calendar.is_trading_day(dt.date()) and s.open <= dt.time() <= s.entry_cutoff


def past_force_flat(segment: Segment, ts: float) -> bool:
    return to_ist(ts).time() >= SESSIONS[segment].force_flat


def seconds_to_close(segment: Segment, ts: float) -> float:
    return session_close_ts(segment, ist_day(ts)) - ts


# ---- bar boundaries ------------------------------------------------------------------------------


def bucket_start(segment: Segment, ts: float, tf: str) -> float:
    """Start of the ``tf`` bucket that ``ts`` falls in, anchored on the session open.

    Ticks outside the session are clamped to the session's first bucket for the pre-open case and
    the last bucket for the post-close case; callers that care filter with :func:`is_open` first.
    """
    if tf == "1d":
        return session_open_ts(segment, ist_day(ts))
    step = TF_SECONDS[tf]
    open_ts = session_open_ts(segment, ist_day(ts))
    if ts < open_ts:
        return open_ts
    return open_ts + ((ts - open_ts) // step) * step


def bucket_end(segment: Segment, bucket: float, tf: str) -> float:
    if tf == "1d":
        return session_close_ts(segment, ist_day(bucket))
    return min(bucket + TF_SECONDS[tf], session_close_ts(segment, ist_day(bucket)))


def is_boundary(segment: Segment, ts: float, tf: str) -> bool:
    return bucket_start(segment, ts, tf) == ts


#: An NSE stock trades continuously until the closing auction, which starts at 15:15: its 15:15 bar
#: is one auction print (and, fetched after the close, the whole auction's volume)
NSE_EQ_CONTINUOUS_UNTIL = time(15, 15)


def in_session(segment: Segment, ts: float) -> bool:
    """Is ``ts`` inside the segment's session — the open included, the close not."""
    day = ist_day(ts)
    return session_open_ts(segment, day) <= ts < session_close_ts(segment, day)


def on_session_grid(segment: Segment, ts: float, tf: str, *, until: time | None = None) -> bool:
    """Is ``ts`` the start of a real ``tf`` bucket: at or after the session open, before the close
    (or ``until``), and exactly on the grid anchored at the open.

    A broker ROW is not judged by this: 5paisa stamps a 30m candle with the minute of its first
    trade (09:16, 10:46; the closing auction 15:28), and such a row IS its bucket — snap it with
    ``bucket_start`` after ``in_session``. This is for bars already on the grid."""
    step = TF_SECONDS[tf]
    day = ist_day(ts)
    open_ts = session_open_ts(segment, day)
    end = from_ist(datetime.combine(day, until, tzinfo=IST)) if until is not None else session_close_ts(segment, day)
    return open_ts <= ts < end and (ts - open_ts) % step == 0


def last_bucket_start(segment: Segment, d: date, tf: str, *, until: time | None = None) -> float:
    """The last ``tf`` bucket of ``d``'s session that starts before the close (or ``until``):
    14:45 for an NSE stock's continuous 30m session, 15:15 for its future."""
    step = TF_SECONDS[tf]
    open_ts = session_open_ts(segment, d)
    end = from_ist(datetime.combine(d, until, tzinfo=IST)) if until is not None else session_close_ts(segment, d)
    return open_ts + ((end - open_ts - 1) // step) * step


def session_buckets_back(
    segment: Segment, last: float, n: int, tf: str, calendar: TradingCalendar, *, until: time | None = None,
    prev_day: Callable[[date], date] | None = None,
) -> list[int]:
    """The ``n`` bucket starts ending at ``last`` (inclusive), oldest first, walking back through
    the session grid and across weekends and holidays — the slots a reading of ``n`` bars must hold.
    ``prev_day`` replaces the (NSE) calendar's previous trading day for a segment it does not
    describe — MCX trades evenings on some NSE holidays and closes on some NSE sessions."""
    step = TF_SECONDS[tf]
    out = [int(last)]
    d = ist_day(last)
    cur = last
    while len(out) < n:
        cur -= step
        if cur < session_open_ts(segment, d):
            d = prev_day(d) if prev_day is not None else calendar.previous_trading_day(d)
            cur = last_bucket_start(segment, d, tf, until=until)
        out.append(int(cur))
    return out[::-1]


@lru_cache(maxsize=64)
def boundaries(segment: Segment, tf: str) -> tuple[str, ...]:
    """Every ``tf`` bucket label of a full session, as ``HH:MM`` IST — handy for tests and the UI."""
    s = SESSIONS[segment]
    step = TF_SECONDS[tf]
    out: list[str] = []
    cur = datetime.combine(date(2026, 1, 1), s.open)
    end = datetime.combine(date(2026, 1, 1), s.close)
    while cur < end:
        out.append(cur.strftime("%H:%M"))
        cur += timedelta(seconds=step)
    return tuple(out)


def session_phase(segment: Segment, ts: float) -> str:
    """``OPEN`` | ``MID`` | ``EOD`` for the decision bar starting at ``ts``.

    A strategy must know it is on the last bar of the session without knowing that the session is
    IST, or that MCX closes at 23:30 while NSE closes at 15:30. The engine asks for its strategies
    and the review committee asks for its post-mortems; this is the one answer.
    """
    sp = spec(segment)
    hm = ist_hm(ts)
    if hm >= sp.entry_cutoff.strftime("%H:%M"):
        return "EOD"
    if hm <= sp.open.strftime("%H:%M"):
        return "OPEN"
    return "MID"
