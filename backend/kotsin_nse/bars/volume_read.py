"""One volume reading, checked before it may decide anything (operator, 2026-09-28: "we need
accurate volume values else our calculation gets affected … gate against random glitches").

A reading is the trigger bar (T) and the bar before it (T-1) against the mean of the six before
those (T-2 … T-7) — ``volume_surges``. It is only as good as the eight bars it reads, so they are
read by their SLOT on the session grid, never by position in a list:

* **only real session bars** — an NSE stock's continuous session is 09:15 … 14:45 (its 15:15 bar
  is the closing auction; a day fetched after the close also carries a 15:45 post-close bar, which
  sat in T-1 before the 2026-09-28 09:45 triggers and read 0.00x — 37 of 208 names "dried");
  a future counts to 15:15, it trades to 15:30;
* **all eight slots present** — a missing bar (a feed gap, a failed backfill) would otherwise make
  an older bar "T-1" without a word;
* **none zero, none flagged** — a 30m bar of an F&O stock does not trade nothing, and the
  reconciler flags a broker candle that disagrees with the live build by more than half.

Anything else is DOUBTFUL: no numbers, and the reason. A doubtful reading never decides — the
dried-volume gate cannot skip on it, and the fade reads that leg's volume as unknown.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, time

from ..config import Segment
from ..market.session import TradingCalendar, on_session_grid, session_buckets_back, to_ist
from .indicators import volume_surges

WINDOW = 6
FLOOR = 1000.0
#: the baseline floor in the segment's own units. NSE volume is in shares; MCX volume is in LOTS
#: (30m medians GOLD 214, COPPER 137, CRUDEOIL 870), where a floor of 1,000 set the baseline in
#: 98-99 % of GOLD and COPPER readings and read 93-95 % of them as "dried" (review, 2026-10-03).
FLOORS: dict[Segment, float] = {Segment.MCX_FO: 10.0}


def floor_for(segment: Segment) -> float:
    return FLOORS.get(segment, FLOOR)
#: the market-wide check: a bar whose median T or T-1 across the NSE names is outside this band,
#: or whose doubtful readings exceed this share, is a data fault, not a market
MARKET_SANE = (0.1, 10.0)
MARKET_DOUBT_SHARE = 0.3
MARKET_MIN_NAMES = 50


@dataclass(frozen=True, slots=True)
class VolBar:
    ts: int
    volume: float
    doubt: str = ""


@dataclass(frozen=True, slots=True)
class VolumeReading:
    surge_t: float | None = None
    surge_t1: float | None = None
    baseline: float | None = None
    doubt: str = ""
    #: what kind of doubt — "missing", "zero", "flagged", "off-grid", "baseline", "market"
    kind: str = ""

    @property
    def ok(self) -> bool:
        return not self.doubt and self.surge_t is not None and self.surge_t1 is not None


def _when(ts: int) -> str:
    return to_ist(ts).strftime("%d %b %H:%M")


def read_volume(
    bars: Sequence[VolBar],
    *,
    segment: Segment,
    t_ts: float,
    calendar: TradingCalendar,
    tf: str = "30m",
    until: time | None = None,
    window: int = WINDOW,
    floor: float | None = None,
    prev_day: Callable[[date], date] | None = None,
) -> VolumeReading:
    """The reading at the bucket starting ``t_ts``, or a doubtful one saying why. ``bars`` are the
    store's, already on the grid (the backfill snaps the broker's first-trade stamps, the tick
    path builds buckets); one that is not is ignored, never read as a neighbour's slot."""
    if not on_session_grid(segment, t_ts, tf, until=until):
        return VolumeReading(doubt=f"trigger bar {_when(int(t_ts))} is not a session bar", kind="off-grid")
    slots = session_buckets_back(segment, t_ts, window + 2, tf, calendar, until=until, prev_day=prev_day)
    by_ts = {int(b.ts): b for b in bars if on_session_grid(segment, b.ts, tf, until=until)}
    missing = [s for s in slots if s not in by_ts]
    if missing:
        more = f" (+{len(missing) - 3})" if len(missing) > 3 else ""
        return VolumeReading(doubt="missing bar " + ", ".join(_when(s) for s in missing[:3]) + more, kind="missing")
    zero = [s for s in slots if by_ts[s].volume <= 0]
    if zero:
        return VolumeReading(doubt="zero-volume bar " + ", ".join(_when(s) for s in zero[:3]), kind="zero")
    flagged = [s for s in slots if by_ts[s].doubt]
    if flagged:
        return VolumeReading(doubt=f"bar {_when(flagged[0])}: {by_ts[flagged[0]].doubt}", kind="flagged")
    s_t, s_t1, base = volume_surges([by_ts[s].volume for s in slots], window=window,
                                    floor=floor_for(segment) if floor is None else floor)
    if s_t is None or s_t1 is None:
        return VolumeReading(doubt="no baseline", kind="baseline")
    return VolumeReading(s_t, s_t1, base)


@dataclass(slots=True)
class MarketVolume:
    """The market-wide check on one bar: every NSE name's reading at it."""

    ts: int
    names: int = 0
    doubtful: int = 0
    median_t: float | None = None
    median_t1: float | None = None
    alarm: str = ""
    reasons: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "ts": self.ts, "names": self.names, "doubtful": self.doubtful,
            "medianT": None if self.median_t is None else round(self.median_t, 3),
            "medianT1": None if self.median_t1 is None else round(self.median_t1, 3),
            "alarm": self.alarm, "reasons": dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])[:5]),
        }


def market_volume(ts: int, readings: Sequence[VolumeReading]) -> MarketVolume:
    """Is the whole tape's volume plausible at this bar? A median T-1 of 0.002 across the market
    (2026-09-28 09:45) is not a quiet market, it is a broken bar — and a check that looks at one
    name at a time cannot tell the two apart."""
    mv = MarketVolume(ts=ts, names=len(readings))
    good = [r for r in readings if r.ok]
    mv.doubtful = mv.names - len(good)
    for r in readings:
        if not r.ok:
            mv.reasons[r.kind or "other"] = mv.reasons.get(r.kind or "other", 0) + 1
    if good:
        mv.median_t = statistics.median(r.surge_t for r in good if r.surge_t is not None)
        mv.median_t1 = statistics.median(r.surge_t1 for r in good if r.surge_t1 is not None)
    if mv.names < MARKET_MIN_NAMES:
        return mv
    lo, hi = MARKET_SANE
    if mv.doubtful / mv.names > MARKET_DOUBT_SHARE:
        mv.alarm = f"{mv.doubtful} of {mv.names} readings doubtful (> {MARKET_DOUBT_SHARE:.0%})"
    elif mv.median_t1 is not None and not lo <= mv.median_t1 <= hi:
        mv.alarm = f"market median T-1 {mv.median_t1:.3f}x (outside {lo:g}–{hi:g}x)"
    elif mv.median_t is not None and not lo <= mv.median_t <= hi:
        mv.alarm = f"market median T {mv.median_t:.3f}x (outside {lo:g}–{hi:g}x)"
    return mv


@dataclass(frozen=True, slots=True)
class SlotReading:
    """A bar's volume against the SAME time slot on earlier sessions — the reading that does not
    mistake the intraday U-shape for a market drying up or surging."""

    ratio: float | None = None
    median: float | None = None
    sessions: int = 0
    slot: str = ""
    doubt: str = ""

    @property
    def ok(self) -> bool:
        return not self.doubt and self.ratio is not None

    def to_json(self) -> dict[str, object]:
        return {"ratio": None if self.ratio is None else round(self.ratio, 3), "median": self.median,
                "sessions": self.sessions, "slot": self.slot, "doubt": self.doubt}


#: sessions of the same slot the median is taken over, and the fewest it may stand on
SLOT_SESSIONS = 20
SLOT_MIN_SESSIONS = 5


def slot_reading(bars: Sequence[VolBar], t_ts: int, *, sessions: int = SLOT_SESSIONS,
                 min_sessions: int = SLOT_MIN_SESSIONS) -> SlotReading:
    """The bar starting ``t_ts`` against the median of the same IST HH:MM slot over up to ``sessions``
    earlier sessions. The T-2…T-7 reading compares a bar with the six before it, so it reads the
    normal U-shape as signal: 0 % of 09:15 bars read "dried" against 60-68 % of late-morning ones and
    1 % at 14:45; divided by its own slot's median, 25 % / 35 % / 27 % (review, 2026-10-03). Reported,
    gating nothing — whether a book gates on it is the operator's call."""
    slot = to_ist(t_ts).strftime("%H:%M")
    by_ts = {b.ts: b for b in bars}
    t = by_ts.get(int(t_ts))
    if t is None:
        return SlotReading(slot=slot, doubt="no bar at that time")
    if t.doubt or t.volume <= 0:
        return SlotReading(slot=slot, doubt=t.doubt or "zero volume")
    same = sorted((b for b in bars if b.ts < t_ts and to_ist(b.ts).strftime("%H:%M") == slot
                   and b.volume > 0 and not b.doubt), key=lambda b: b.ts)[-sessions:]
    if len(same) < min_sessions:
        return SlotReading(slot=slot, sessions=len(same), doubt=f"only {len(same)} earlier sessions at {slot}")
    med = statistics.median(b.volume for b in same)
    return SlotReading(ratio=t.volume / med if med > 0 else None, median=med, sessions=len(same), slot=slot,
                       doubt="" if med > 0 else "median volume is zero")

