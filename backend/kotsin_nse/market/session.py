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
    #: every open position is flattened from here (NSE 15:14, out before 15:20 — operator, 2026-10-17)
    force_flat: time

    def contains(self, t: time) -> bool:
        return self.open <= t <= self.close


# MCX closes 23:30 in winter and 23:55 in summer. 23:30 is the conservative choice: flattening at
# 23:20 is inside both. The old stack's single NSE-shaped 15:20 constant would have closed every
# commodity position eight hours early — see can2_consumer SEGMENT_FLAT.
# The entry cutoff here is FUDKII's end-of-day PHASE (a bar starting 14:45 or later takes strong
# signals only); the last minute a book may place an NSE entry is the engine's (engine.NSE_LAST_ENTRY_HM,
# 15:10).
# NSE: every open position of every book is flattened from 15:14 IST and must be OUT before 15:20
# (operator, 2026-10-17: "if we are in trade till 15:14IST, then we exit at the first avenue before
# 15:20IST"). It was 15:20 for the books and 15:24 for the graded-F shadow (out by 15:25, 2026-09-29);
# no book has a later NSE flatten now. The close stays 15:30: it is the bar and signal close (the 15:15
# closing-auction bar, the carry of its triggers), not the recorder's end — F&O ticks are recorded to
# 15:40 (ops/fulltape.py WINDOW, operator 2026-10-17: "we will record tick till 15:40IST").
SESSIONS: dict[Segment, SessionSpec] = {
    Segment.NSE_EQ: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 14)),
    Segment.NSE_FO: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 14)),
    Segment.NSE_IDX: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 14)),
    Segment.MCX_FO: SessionSpec(time(9, 0), time(23, 30), time(23, 0), time(23, 20)),
}
#: An NSE position the flatten could not sell by here — a stale book, no quote, a refused order — is
#: filled on PAPER at the last known bid (else the last known price), flagged as forced, and alarms
#: (operator, 2026-10-17). A LIVE book's position is never paper-filled: it alarms only.
NSE_FORCED_FILL = time(15, 19)
#: every NSE position must be OUT before this: one still open at or after it, or an exit filled at or
#: after it, alarms (a structured log event, a ledger event, Telegram and a failing health line)
NSE_OUT_BY = time(15, 20)


#: An MCX day with only one of its two sessions (``data/holidays_mcx.txt``): ``evening`` — the morning
#: session closed, trading from 17:00 to the close (NSE holidays: 20 Oct, 10 Nov, 24 Nov 2026); ``morning``
#: — open from 09:00 to 17:00, the evening closed (1 Jan 2026). MCX circular MCX/TRD/636/2025.
MCX_EVENING_OPEN = time(17, 0)
MCX_DAY_STATES = ("closed", "evening", "morning")


@dataclass(frozen=True, slots=True)
class McxDays:
    """MCX's own exceptions to a weekday session, from ``data/holidays_mcx.txt``."""

    closed: frozenset[date] = frozenset()
    evening: frozenset[date] = frozenset()
    morning: frozenset[date] = frozenset()

    @classmethod
    def from_file(cls, path: Path | None) -> McxDays | None:
        """``YYYY-MM-DD closed|evening|morning`` a line, ``#`` comments. None when there is no file — MCX
        then follows the NSE list, as it always did. A malformed line raises: a half-read list is worse
        than none (the engine falls back to the NSE list and says so)."""
        if path is None or not path.exists():
            return None
        out: dict[str, set[date]] = {k: set() for k in MCX_DAY_STATES}
        for n, raw in enumerate(path.read_text().splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2 or parts[1] not in out:
                raise ValueError(f"{path.name}:{n}: want 'YYYY-MM-DD {'|'.join(MCX_DAY_STATES)}', got {line!r}")
            d = date.fromisoformat(parts[0])
            if any(d in v for v in out.values()):
                raise ValueError(f"{path.name}:{n}: {d} listed twice")
            out[parts[1]].add(d)
        return cls(frozenset(out["closed"]), frozenset(out["evening"]), frozenset(out["morning"]))


class TradingCalendar:
    """Weekday sessions minus an explicit holiday list.

    The holiday list is **data, not code**: ``data/holidays.txt`` holds one ``YYYY-MM-DD`` per line.
    An empty or missing file means weekdays-only, which is wrong on about a dozen days a year — so
    :meth:`missing_holidays_warning` says so at boot rather than letting a silent assumption ride.
    Never guess a holiday: a wrong entry skips a real trading day, and a missing one produces a
    session of empty bars that looks like a feed outage.

    ``holidays.txt`` is NSE's. MCX keeps its own days in ``data/holidays_mcx.txt`` (``mcx``): on most NSE
    holidays it trades the evening session, and it has closures of its own. Every question asked with
    ``segment=Segment.MCX_FO`` reads that list; every other question — and every MCX one while the file
    is absent — reads the NSE list exactly as before (2026-10-19).
    """

    def __init__(self, holidays: frozenset[date] | None = None, mcx: McxDays | None = None) -> None:
        self.holidays: frozenset[date] = holidays or frozenset()
        self.mcx: McxDays | None = mcx

    @classmethod
    def from_file(cls, path: Path | None, mcx_path: Path | None = None) -> TradingCalendar:
        mcx = McxDays.from_file(mcx_path)
        if path is None or not path.exists():
            return cls(mcx=mcx)
        days: set[date] = set()
        for raw in path.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                days.add(date.fromisoformat(line))
        return cls(frozenset(days), mcx)

    def _mcx_days(self, segment: Segment | None) -> McxDays | None:
        return self.mcx if segment is Segment.MCX_FO else None

    def is_trading_day(self, d: date, segment: Segment | None = None) -> bool:
        mcx = self._mcx_days(segment)
        if mcx is not None:
            return d.weekday() < 5 and d not in mcx.closed  # an evening-only day trades
        return d.weekday() < 5 and d not in self.holidays

    def session_hours(self, segment: Segment, d: date) -> tuple[time, time] | None:
        """``segment``'s open and close on ``d`` (IST), or None when it does not trade that day."""
        if not self.is_trading_day(d, segment):
            return None
        sp = SESSIONS[segment]
        mcx = self._mcx_days(segment)
        if mcx is not None:
            if d in mcx.evening:
                return MCX_EVENING_OPEN, sp.close
            if d in mcx.morning:
                return sp.open, MCX_EVENING_OPEN
        return sp.open, sp.close

    def missing_holidays_warning(self) -> str | None:
        if self.holidays:
            return None
        return (
            "no holiday list loaded — every weekday counts as a trading day. "
            "Populate data/holidays.txt (one YYYY-MM-DD per line) from the exchange circular."
        )

    def previous_trading_day(self, d: date, segment: Segment | None = None) -> date:
        cur = d - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(cur, segment):
                return cur
            cur -= timedelta(days=1)
        return cur


#: MCX's own days, beside NSE's ``holidays.txt`` in the data folder
MCX_HOLIDAYS_FILE = "holidays_mcx.txt"


def load_calendar(data_dir: Path) -> tuple[TradingCalendar, str | None]:
    """``holidays.txt`` (NSE) and ``holidays_mcx.txt`` under ``data_dir``, with a warning for the boot when MCX
    is left on the NSE list: no file (the calendar before 2026-10-19), or a malformed one. NSE's list fails
    the boot as it always did."""
    nse, mcx = data_dir / "holidays.txt", data_dir / MCX_HOLIDAYS_FILE
    try:
        cal = TradingCalendar.from_file(nse, mcx)
    except ValueError as exc:
        if not mcx.exists():
            raise
        cal = TradingCalendar.from_file(nse)
        return cal, f"{MCX_HOLIDAYS_FILE} unreadable ({exc}) — MCX follows the NSE holiday list"
    if cal.mcx is None:
        return cal, (f"no data/{MCX_HOLIDAYS_FILE} — MCX follows the NSE holiday list: its evening sessions on "
                     "NSE holidays count as closed (no feed watchdog, sweep or health lines)")
    return cal, None


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
    """Is ``segment`` trading at ``ts`` — its own calendar's day and hours (an MCX evening-only day from 17:00)."""
    dt = to_ist(ts)
    hours = calendar.session_hours(segment, dt.date())
    return hours is not None and hours[0] <= dt.time() <= hours[1]


def in_entry_window(segment: Segment, ts: float, calendar: TradingCalendar) -> bool:
    dt = to_ist(ts)
    hours = calendar.session_hours(segment, dt.date())
    return hours is not None and hours[0] <= dt.time() <= min(hours[1], SESSIONS[segment].entry_cutoff)


def past_force_flat(segment: Segment, ts: float) -> bool:
    return to_ist(ts).time() >= SESSIONS[segment].force_flat


def held_overnight(ts: float, opened_ts: float | None) -> bool:
    """An NSE position opened on an earlier IST day than ``ts``: it was open past that day's 15:20, whatever the time
    of day now (review, 2026-10-17: an engine down over the close and back after midnight held yesterday's position
    as a normal one until 15:19 the next day, and one held in SHADOW never alarmed again)."""
    return opened_ts is not None and opened_ts > 0 and ist_day(opened_ts) < ist_day(ts)


def past_nse_forced_fill(segment: Segment, ts: float, opened_ts: float | None = None) -> bool:
    """From 15:19:00 an NSE position still open is filled on paper at its last known price (MCX: never) — and one
    opened on an earlier IST day (``opened_ts``) at once."""
    return segment is not Segment.MCX_FO and (to_ist(ts).time() >= NSE_FORCED_FILL or held_overnight(ts, opened_ts))


def past_nse_out_by(segment: Segment, ts: float, opened_ts: float | None = None) -> bool:
    """At or after 15:20:00 an NSE position is late: still open, or an exit filling now, alarms (MCX: never) — and one
    opened on an earlier IST day (``opened_ts``) is late all day."""
    return segment is not Segment.MCX_FO and (to_ist(ts).time() >= NSE_OUT_BY or held_overnight(ts, opened_ts))


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
            d = prev_day(d) if prev_day is not None else calendar.previous_trading_day(d, segment)
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
