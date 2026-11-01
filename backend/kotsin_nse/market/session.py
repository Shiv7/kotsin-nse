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
    #: the last instant of CONTINUOUS trading. On NSE cash since 2026-08-03 the F&O stocks stop
    #: trading at 15:15 and a closing auction (one print, ~15:28) sets the official close; the
    #: feed keeps sending frames with the indicative auction quantity in ``TotalQty``. Nothing
    #: between ``continuous_end`` and ``close`` is a trade a bar may be built from or an order may
    #: be filled at. Equal to ``close`` where there is no auction.
    continuous_end: time
    #: when the broker's daily candle for the session can be trusted as final (after the auction
    #: has settled). ``bars/daily.py`` seeds a session as a finished day only past this.
    daily_final: time

    def contains(self, t: time) -> bool:
        """Half-open, like every bar bucket: the close instant itself is outside the session."""
        return self.open <= t < self.close


# MCX closes 23:30 in winter and 23:55 in summer. 23:30 is the conservative choice: flattening at
# 23:20 is inside both. The old stack's single NSE-shaped 15:20 constant would have closed every
# commodity position eight hours early — see can2_consumer SEGMENT_FLAT.
#
# NSE F&O contracts trade to 15:40 since 2026-08-03 (NSE/CMTR/73362); ``close`` here still says
# 15:30 because every bar grid, the research cache and the REST reconciler are built on it —
# moving it is a deliberate change with a re-run of the research behind it (HANDOFF §6), not a
# constant edit. The ten minutes cost the index futures' last ticks and OI in SHADOW, nothing else.
SESSIONS: dict[Segment, SessionSpec] = {
    Segment.NSE_EQ: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 20), time(15, 15), time(15, 45)),
    Segment.NSE_FO: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 20), time(15, 30), time(15, 45)),
    Segment.NSE_IDX: SessionSpec(time(9, 15), time(15, 30), time(14, 45), time(15, 20), time(15, 30), time(15, 45)),
    Segment.MCX_FO: SessionSpec(time(9, 0), time(23, 30), time(23, 0), time(23, 20), time(23, 30), time(23, 30)),
}


class TradingCalendar:
    """Weekday sessions minus an explicit holiday list, one list per exchange.

    The holiday lists are **data, not code**: ``data/holidays.txt`` holds NSE's, one ``YYYY-MM-DD``
    per line, and ``data/holidays-mcx.txt`` MCX's. An empty or missing file means weekdays-only,
    which is wrong on about a dozen days a year — so :meth:`missing_holidays_warning` says so at boot
    rather than letting a silent assumption ride. Never guess a holiday: a wrong entry skips a real
    trading day, and a missing one produces a session of empty bars that looks like a feed outage.

    The lists differ: on 2026-10-20 (Dussehra) NSE was shut while MCX traded its evening session.
    One shared set marked MCX closed all evening — health read "market closed" with ticks flowing.
    """

    def __init__(
        self, holidays: frozenset[date] | None = None, mcx_holidays: frozenset[date] | None = None
    ) -> None:
        self.holidays: frozenset[date] = holidays or frozenset()
        self.mcx_holidays: frozenset[date] = mcx_holidays or frozenset()

    @staticmethod
    def _read(path: Path | None) -> frozenset[date]:
        if path is None or not path.exists():
            return frozenset()
        days: set[date] = set()
        for raw in path.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                days.add(date.fromisoformat(line))
        return frozenset(days)

    @classmethod
    def from_file(cls, path: Path | None, mcx_path: Path | None = None) -> TradingCalendar:
        return cls(cls._read(path), cls._read(mcx_path))

    def is_trading_day(self, d: date, segment: Segment = Segment.NSE_EQ) -> bool:
        closed = self.mcx_holidays if segment is Segment.MCX_FO else self.holidays
        return d.weekday() < 5 and d not in closed

    def missing_holidays_warning(self) -> str | None:
        if self.holidays:
            return None
        return (
            "no holiday list loaded — every weekday counts as a trading day. "
            "Populate data/holidays.txt (one YYYY-MM-DD per line) from the exchange circular."
        )

    def previous_trading_day(self, d: date, segment: Segment = Segment.NSE_EQ) -> date:
        cur = d - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(cur, segment):
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


def ist_hm(ts: float) -> str:
    return to_ist(ts).strftime("%H:%M")


# ---- session queries ---------------------------------------------------------------------------


def spec(segment: Segment) -> SessionSpec:
    return SESSIONS[segment]


def session_open_ts(segment: Segment, d: date) -> float:
    return from_ist(datetime.combine(d, SESSIONS[segment].open, tzinfo=IST))


def session_close_ts(segment: Segment, d: date) -> float:
    return from_ist(datetime.combine(d, SESSIONS[segment].close, tzinfo=IST))


def continuous_end_ts(segment: Segment, d: date) -> float:
    """The end of continuous trading on ``d`` — the last instant a bar may be built from or an
    order filled at (the closing auction follows on NSE cash)."""
    return from_ist(datetime.combine(d, SESSIONS[segment].continuous_end, tzinfo=IST))


def daily_final_ts(segment: Segment, d: date) -> float:
    """When the broker's daily candle for session ``d`` is final (the auction has settled)."""
    return from_ist(datetime.combine(d, SESSIONS[segment].daily_final, tzinfo=IST))


def day_start_ts(d: date) -> float:
    """Midnight IST of ``d`` — where the broker stamps a finished daily candle."""
    return from_ist(datetime.combine(d, time(0, 0), tzinfo=IST))


def is_open(segment: Segment, ts: float, calendar: TradingCalendar) -> bool:
    dt = to_ist(ts)
    return calendar.is_trading_day(dt.date(), segment) and SESSIONS[segment].contains(dt.time())


def in_entry_window(segment: Segment, ts: float, calendar: TradingCalendar) -> bool:
    dt = to_ist(ts)
    s = SESSIONS[segment]
    return calendar.is_trading_day(dt.date(), segment) and s.open <= dt.time() <= s.entry_cutoff


def past_force_flat(segment: Segment, ts: float) -> bool:
    return to_ist(ts).time() >= SESSIONS[segment].force_flat


def seconds_to_close(segment: Segment, ts: float) -> float:
    return session_close_ts(segment, ist_day(ts)) - ts


# ---- bar boundaries ------------------------------------------------------------------------------


def bucket_start(segment: Segment, ts: float, tf: str) -> float:
    """Start of the ``tf`` bucket that ``ts`` falls in, anchored on the session open.

    A pre-open tick is clamped to the session's first bucket; a post-close tick is NOT clamped (it
    floors to a bucket past the close). Callers that care filter with :func:`is_open` first — the
    aggregator does.
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


def ist_at(d: date, hm: str) -> int:
    """Epoch seconds of ``HH:MM`` IST on calendar day ``d``."""
    h, m = (int(x) for x in hm.split(":"))
    return int(datetime.combine(d, time(h, m), tzinfo=IST).timestamp())

