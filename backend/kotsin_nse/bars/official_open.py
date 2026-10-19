"""The official open of every NSE underlying, captured off the feed (CTB-19Oct, operator 18-19 Oct 2026: "the 1 AM
definition").

NSE opens each name with a call auction: orders from 09:00, the match about 09:08, then a pause to 09:15. The match
price is the session's OPEN — the 1D candle's open, what the exchange and the broker's daily row both print; a name
with no match opens at its first continuous trade (NSE's rule). The official CLOSE is the closing-auction print, the
1D candle's close, already the previous session's daily row in the store. ``gapDatr`` has always read the open of
today's first 30m bar instead — the first continuous trade: a different number on 122 of 146 triggers checked 9-16 Oct,
and 8 of the 18 CT-Y fades of 29 Sep - 12 Oct came from it. The official numbers matched 126/126 (broker 1D = engine
1D = the tape's last pre-open print, 9-16 Oct).

The bar aggregator drops every pre-open frame (they are not a bar); the quote path sees them, and this reads them there,
per name, settled once a day:

* a frame whose EXCHANGE time (TickDt) is today, 09:07:00-09:14:59, is the pre-open price — the last such frame by
  exchange time wins. Order entry runs 09:00 to a random second in 09:07-09:08 and the match follows it, so a frame
  stamped 09:00-09:06:59 is the order book, never the match (MAHABANK 19 Oct, arrived 09:05:48 at the previous close
  with a crossed book; research has no pre-open match and the first trade 79.86): refused and counted;
* the first frame stamped 09:15:00 or later settles the open: the pre-open price if there was one (``preopen``), else
  this first continuous trade (``first_trade``) — only when the feed has been up, unbroken, since before 09:15, so the
  first trade seen was the first trade made. A process that joined later, or a feed that (re)connected after 09:15
  (``feed_connected``: a reconnect's snapshot carries the LAST trade, 10:29:58 after a sleep 08:55-10:30), settles
  nothing for a name it had no pre-open frame of, and the engine reads the first 30m bar's open instead (``first_bar``
  — the first trade, from the broker's candle, which the reconnect repair rebuilds); CT-B1 does not fade on it;
* a frame with no exchange time, or one from another day (a reconnect's snapshot carries the last trade's time —
  yesterday's, at the 08:30 subscribe), never counts;
* a pooled frame (the feed's pool priming a code, with the time it first ARRIVED — possibly days ago) is judged by its
  exchange time like any other, but it never says the feed was up, never settles a first trade and never rolls the
  day back.

The indices ride the cash feed and print their pre-open too: BANKNIFTY 12 Oct 55,350.15 at 09:13:59, the 1D open (the
first continuous value was 55,328.40); NIFTY 23,150.85 at 09:14:00 on 19 Oct. An index reads ``preopen`` like a stock.

Kept in ``<data>/archive/official_open/<day>.json`` as they settle and read back at boot, so a restart the same day
keeps them and research can audit them against the 1D candle. Pure apart from that file: no broker call.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import structlog

from ..market.session import IST, from_ist, ist_day, to_ist

log = structlog.get_logger(__name__)

#: the NSE equity pre-open: order entry from 09:00, closed at a random second in 09:07-09:08, the match after it, the
#: continuous session from 09:15. A frame stamped before MATCH_FROM is the order book, never the match (an index's
#: pre-open values swing: the broker's 08:59 NIFTY row of 25 Sep has a pre-open low of 22,282.65 against a real 23,020.95)
PREOPEN_FROM = dt_time(9, 0)
MATCH_FROM = dt_time(9, 7)
CONTINUOUS_FROM = dt_time(9, 15)
#: where an official open came from — the pre-open print, the first continuous trade (no pre-open match), or (the
#: engine's fallback when this never saw the open) the first 30m bar's open
PREOPEN, FIRST_TRADE, FIRST_BAR = "preopen", "first_trade", "first_bar"


@dataclass(frozen=True, slots=True)
class OfficialOpen:
    #: the official open
    price: float
    #: ``preopen`` or ``first_trade``
    src: str
    #: the exchange time (TickDt) of the frame that carried the price
    exch_ts: float
    #: the settling frame's own OpenRate and PClose fields, kept beside it for the audit — no rule reads them
    feed_open: float | None = None
    feed_prev_close: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"price": self.price, "src": self.src, "exchTs": self.exch_ts, "feedOpen": self.feed_open,
                "feedPrevClose": self.feed_prev_close}

    @staticmethod
    def from_json(d: dict[str, Any]) -> OfficialOpen:
        return OfficialOpen(float(d["price"]), str(d["src"]), float(d["exchTs"]),
                            None if d.get("feedOpen") is None else float(d["feedOpen"]),
                            None if d.get("feedPrevClose") is None else float(d["feedPrevClose"]))


def official_gap(open_px: float, close_px: float, datr: float, *, bullish: bool) -> float | None:
    """The gap in daily ATRs, signed the trigger's way: positive when the session opened beyond the previous close in
    the trigger's own direction (a gap up for a bullish trigger, a gap down for a bearish one). None without an ATR."""
    if not datr or datr <= 0 or open_px <= 0 or close_px <= 0:
        return None
    return (1 if bullish else -1) * (open_px - close_px) / datr


class OfficialOpens:
    """Today's official opens, by symbol. ``note`` is called with every NSE underlying's feed frame."""

    def __init__(self, folder: Path | None) -> None:
        self.folder = folder
        self.day: date | None = None
        #: the IST day's bounds and its 09:00 / 09:07 / 09:15, as epochs — a frame is judged without building a datetime
        self._day_from = self._day_to = self._pre_from = self._match_from = self._cont_from = 0.0
        #: since when the feed has been up unbroken: the day's first live frame's ARRIVAL, moved to a (re)connect after
        #: 09:15 (``feed_connected``) — up before 09:15, a first trade can be told; up after, it cannot
        self.watch_from: float | None = None
        #: symbol → (price, exchange time) of its latest pre-open frame, until the open settles it
        self._pre: dict[str, tuple[float, float]] = {}
        self.opens: dict[str, OfficialOpen] = {}
        self.dirty = False
        #: frames refused: no exchange time, stamped on another day, and stamped today in order entry (09:00-09:06:59) —
        #: the last says whether the broker stamps an order-entry frame with today's time at all (the tape cannot tell)
        self.untimed = 0
        self.other_day = 0
        self.order_entry = 0

    def _roll(self, day: date) -> None:
        self.day = day
        start = from_ist(datetime.combine(day, dt_time(0, 0), tzinfo=IST))
        self._day_from, self._day_to = start, from_ist(datetime.combine(day + timedelta(days=1), dt_time(0, 0), tzinfo=IST))
        self._pre_from = from_ist(datetime.combine(day, PREOPEN_FROM, tzinfo=IST))
        self._match_from = from_ist(datetime.combine(day, MATCH_FROM, tzinfo=IST))
        self._cont_from = from_ist(datetime.combine(day, CONTINUOUS_FROM, tzinfo=IST))
        self.watch_from = None
        self._pre.clear()
        self.opens.clear()
        self.untimed = self.other_day = self.order_entry = 0
        self.dirty = False

    def feed_connected(self, now: float) -> None:
        """The feed (re)connected at ``now`` (review 19 Oct). After 09:15 a name with no pre-open price may have made its
        first trade while the feed was down, and the reconnect's snapshot carries its LAST one (TickDt 10:29:58 after a
        sleep 08:55-10:30, settled as the open at the 10:29 price before this): from here such a name settles nothing
        and reads ``first_bar``. A pre-open price held, or re-sent with its own exchange time, still settles. Before
        09:15 nothing moves — the open is still ahead."""
        if self.day is None or now >= self._day_to:
            self._roll(ist_day(now))
        if now >= self._cont_from:
            self.watch_from = now

    def note(self, symbol: str, ltp: float, exch_ts: float | None, recv_ts: float, *, pooled: bool = False,
             feed_open: float | None = None, feed_prev_close: float | None = None) -> OfficialOpen | None:
        """One frame of ``symbol``'s. Returns the open when this frame settles it, else None. ``pooled``: a frame the
        feed's pool primed, ``recv_ts`` its first arrival — judged by its exchange time alone."""
        if self.day is None or recv_ts >= self._day_to:
            self._roll(ist_day(recv_ts))  # forward only
        elif recv_ts < self._day_from:
            return None  # arrived on an earlier day (a pooled frame keeps that time): it never rolls today away
        if self.watch_from is None and not pooled:
            self.watch_from = recv_ts
        if symbol in self.opens or ltp <= 0:
            return None  # once a day
        if exch_ts is None or exch_ts <= 0:
            self.untimed += 1
            return None
        if not (self._day_from <= exch_ts < self._day_to):
            self.other_day += 1
            return None
        if exch_ts < self._match_from:
            if exch_ts >= self._pre_from:
                self.order_entry += 1
            return None
        if exch_ts < self._cont_from:
            held = self._pre.get(symbol)
            if held is None or exch_ts >= held[1]:
                self._pre[symbol] = (ltp, exch_ts)
                self.dirty = True
            return None
        pre = self._pre.pop(symbol, None)
        if pre is not None:
            got = OfficialOpen(pre[0], PREOPEN, pre[1], feed_open or None, feed_prev_close or None)
        elif not pooled and self.watch_from is not None and self.watch_from < self._cont_from:
            got = OfficialOpen(ltp, FIRST_TRADE, exch_ts, feed_open or None, feed_prev_close or None)
        else:
            return None  # joined after the open, or a pooled frame: the first trade was not seen, and this one is not it
        self.opens[symbol] = got
        self.dirty = True
        return got

    def get(self, symbol: str, day: date) -> OfficialOpen | None:
        """``symbol``'s official open on ``day``, when this settled one."""
        return self.opens.get(symbol) if self.day == day else None

    def stats(self) -> dict[str, Any]:
        by_src: dict[str, int] = {}
        for o in self.opens.values():
            by_src[o.src] = by_src.get(o.src, 0) + 1
        return {
            "day": self.day.isoformat() if self.day else None,
            "watchFrom": to_ist(self.watch_from).strftime("%H:%M:%S") if self.watch_from else None,
            "settled": len(self.opens), "bySource": by_src, "preopenPending": len(self._pre),
            "refusedUntimed": self.untimed, "refusedOtherDay": self.other_day, "refusedOrderEntry": self.order_entry,
        }

    # -- the day's file ----------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any] | None:
        """What ``write`` saves, taken on the event loop (where ``note`` mutates the maps); clears ``dirty``."""
        if self.day is None:
            return None
        self.dirty = False
        return {
            "day": self.day.isoformat(), "watchFrom": self.watch_from,
            "opens": {s: o.to_json() for s, o in self.opens.items()},
            "preopen": {s: [p, t] for s, (p, t) in self._pre.items()},
        }

    def write(self, snap: dict[str, Any] | None) -> None:
        """Write a ``snapshot`` atomically — in a worker thread."""
        if snap is None or self.folder is None:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{snap['day']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, day: date) -> int:
        """Today's file, if a run earlier today wrote one: its settled opens and pending pre-open prices. When the
        feed was up is NOT taken from it — a process restarted after 09:15 did not watch this open. ``day`` is started
        with or without a file, so a pooled frame that arrived on an earlier day is never taken for today's. Returns the
        opens read."""
        self._roll(day)
        if self.folder is None:
            return 0
        path = self.folder / f"{day.isoformat()}.json"
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if d.get("day") != day.isoformat():
            return 0
        try:
            self.opens = {str(s): OfficialOpen.from_json(o) for s, o in (d.get("opens") or {}).items()}
            self._pre = {str(s): (float(v[0]), float(v[1])) for s, v in (d.get("preopen") or {}).items()}
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            log.warning("official_open.load_failed", path=str(path), error=str(exc)[:120])
            self.opens, self._pre = {}, {}
            return 0
        return len(self.opens)
