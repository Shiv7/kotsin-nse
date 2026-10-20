"""The option recorder — RECORD-ONLY: what the 5-read and the strike walls read, kept as-of from the feed's own frames.

Operator, 2026-10-19: "yes - the engine should start recording the 5-read and strike-wall state live, for research
only, so CT-B1 and later B2 can use them". Nothing here is read by a gate, a book, a sizer or an exit (an
import-linter contract keeps ``strategy``, ``risk``, ``exec``, ``venue`` and ``feed`` from importing it).

The feed hands every market-feed and OI row it reads to ``OptionRecorder.on_frame`` — the rows this engine asked for
and the ones the socket carries for the twin or the record band alike (``FivePaisaFeed.on_record``) — before, and
apart from, the engine's own handlers. A row of a code the recorder does not watch costs a dict lookup.

What it keeps, per watched code (the options of each name's legs' expiry, its front future and its cash/index code):

* **quotes** — a 40-minute change history of (time, last price, bid, ask, the start of the current last-price run),
  one entry a second at most (the full tape's 1 Hz on change: a frame that changes nothing writes nothing, two in one
  second keep the later), so a quote can be read AS OF any moment in the read window, exactly as the research read
  its tape (``research/read5.py`` ``opt_px``, ``tte_years``); a future's or a stock's last state of every minute
  for the whole day beyond it; and each code's first entry at or after 09:15;
* **open interest** — every change of the day (time, OI), and the previous session's last print (seeded from the
  engine's OI archive at boot, rolled at the day change), so OI can be read as of any moment, with its basis
  (session / pre-open / previous day);
* **option volume per minute** — the change of the broker's running ``TotalQty`` between frames, put in the minute
  of the frame's exchange time (else its arrival). The first frame of a code only sets the reference, and its
  coverage starts there; a frame whose last trade is from an earlier day says no trade today (the reference is 0); a
  running total that goes backwards restarts the code's coverage; a feed reconnect, or more than a minute of silence
  in the session (the Mac asleep), is a gap. A 30m bar overlapping a gap or starting before a code's coverage is
  **unverified** — research's ``c.bad`` session: the VOL read does not use it.

Archived: the minute volumes as part files under ``archive/option_volume/<day>/`` (rows ADD UP on (code, minute): a
frame whose minute was already written comes as a second row), and every covered code's 30m bar to
``option_volume_30m`` through the daily archive.
"""

from __future__ import annotations

import math
import shutil
import threading
import time
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
import structlog

from ..market.session import IST, ist_day
from ..research.read5 import GRID, at_ist, bar_bounds

log = structlog.get_logger(__name__)

#: the quote history kept per code: the trigger candle (30 min) and the walls' window behind a 3-of-5 moment
RING_S = 2400.0
#: entries kept before a ring is compacted (amortised: an array's front is never deleted entry by entry)
_COMPACT_AT = 512
#: no frame at all for this long inside the session is a gap in the volume record
SILENCE_GAP_S = 60.0
#: minutes of per-minute volume kept in memory; older minutes live on in their 30m bars
MINUTES_KEPT_S = 3600.0
#: a minute is archived this long after it ended (late frames first); a 30m bar is closed this long after its end
MINUTE_GRACE_S = 120.0
BAR_GRACE_S = 90.0
VOLUME_COLUMNS = ("scrip_code", "ts", "v", "cum")
ROLES = ("opt", "fut", "eq")
#: the as-of OI grid of a previous session the walls' oiChangePct reads (wall_state.py: each 30m bar's start — the
#: 09:15 bar from 09:16 — and its end)
OI_GRID: tuple[str, ...] = ("09:16", *GRID[1:], "15:30")


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def broker_ts(raw: Any) -> float | None:
    """``/Date(1758271500000)/`` → epoch seconds; None when absent or .NET's minimum date (a negative date)."""
    if raw is None:
        return None
    text = str(raw)
    start = text.find("(")
    if start < 0:
        return None
    digits = ""
    for ch in text[start + 1 :]:
        if ch.isdigit() or (ch == "-" and not digits):
            digits += ch
        else:
            break
    if not digits or digits == "-":
        return None
    ms = int(digits)
    return ms / 1000 if ms > 0 else None


def hms(t: float | None) -> str:
    return datetime.fromtimestamp(float(t), IST).strftime("%H:%M:%S") if t is not None and math.isfinite(t) else ""


def day_start(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()


class _Ring:
    """One code's quote changes: parallel arrays, a logical start ``lo`` (entries before it are older than the ring,
    except the last one, which is the state at the ring's edge)."""

    __slots__ = ("ask", "bid", "lo", "ltp", "run", "t", "trimmed")

    def __init__(self) -> None:
        self.t = array("d")
        self.ltp = array("d")
        self.bid = array("d")
        self.ask = array("d")
        self.run = array("d")
        self.lo = 0
        self.trimmed = False

    def add(self, t: float, ltp: float, bid: float, ask: float) -> bool:
        n = len(self.t)
        if n and ltp == self.ltp[-1] and bid == self.bid[-1] and ask == self.ask[-1]:
            return False  # nothing changed: no entry, the last one ages (the full tape's rule)
        if n and t < self.t[-1]:
            return False  # an older frame (a pooled one) never rewrites what came after it
        if n and int(t) == int(self.t[-1]):
            # a second frame in the same second replaces the entry: one a second, the later state
            prev = n - 2
            run = self.run[prev] if prev >= 0 and self.ltp[prev] == ltp else t
            self.t[-1], self.ltp[-1], self.bid[-1], self.ask[-1], self.run[-1] = t, ltp, bid, ask, run
            return True
        run = self.run[-1] if n and self.ltp[-1] == ltp else t
        self.t.append(t)
        self.ltp.append(ltp)
        self.bid.append(bid)
        self.ask.append(ask)
        self.run.append(run)
        return True

    def trim(self, cut: float) -> None:
        """Keep the entries after ``cut`` and the last one at or before it."""
        n = len(self.t)
        lo = self.lo
        while lo + 1 < n and self.t[lo + 1] <= cut:
            lo += 1
        self.lo = lo
        if lo >= _COMPACT_AT and lo * 2 >= n:
            for name in ("t", "ltp", "bid", "ask", "run"):
                setattr(self, name, getattr(self, name)[lo:])
            self.lo = 0
            self.trimmed = True

    def at(self, x: float) -> int:
        """Index of the last entry at or before ``x``; -1 when none is held (none yet, or trimmed away)."""
        j = bisect_right(self.t, x) - 1
        return j

    def __len__(self) -> int:
        return len(self.t) - self.lo


class _MinuteStates:
    """A future's or a stock's last quote of every minute, the whole day: what an as-of read older than the ring
    falls back to (the time-matched IV's future at an old last-trade time)."""

    __slots__ = ("ask", "bid", "ltp", "m", "run", "t")

    def __init__(self) -> None:
        self.m = array("d")
        self.t = array("d")
        self.ltp = array("d")
        self.bid = array("d")
        self.ask = array("d")
        self.run = array("d")

    def add(self, t: float, ltp: float, bid: float, ask: float, run: float) -> None:
        m = t // 60 * 60
        if self.m and self.m[-1] == m:
            self.t[-1], self.ltp[-1], self.bid[-1], self.ask[-1], self.run[-1] = t, ltp, bid, ask, run
            return
        if self.m and m < self.m[-1]:
            return
        for arr, v in ((self.m, m), (self.t, t), (self.ltp, ltp), (self.bid, bid), (self.ask, ask), (self.run, run)):
            arr.append(v)

    def at(self, x: float) -> int:
        j = bisect_right(self.t, x) - 1
        return j


class _Oi:
    __slots__ = ("oi", "t")

    def __init__(self) -> None:
        self.t = array("d")
        self.oi = array("d")


class _Vol:
    """One option's volume today: the running-total reference, since when it is covered, the minutes kept."""

    __slots__ = ("cum", "m", "ref", "restarts", "since", "v")

    def __init__(self) -> None:
        self.ref: float | None = None
        self.since: float | None = None
        self.restarts = 0
        self.m = array("l")  # minute start, epoch seconds
        self.v = array("d")
        self.cum = array("d")


class OptionRecorder:
    """See the module docstring. Every public read is as of a moment, so when a reader runs does not matter."""

    def __init__(self, *, ring_s: float = RING_S, silence_s: float | None = SILENCE_GAP_S, clock: Any = time.time) -> None:
        self.ring_s = float(ring_s)
        #: no frame at all for this long inside the session is a volume gap (None: never — a replay feeds sparse rows)
        self.silence_s = silence_s
        self._clock = clock
        #: code -> role ('opt' / 'fut' / 'eq'); only these are kept
        self.watch: dict[str, str] = {}
        self.day: date | None = None
        self._day0 = 0.0
        self._open = 0.0
        self._close = 0.0
        self._ring: dict[str, _Ring] = {}
        self._mins: dict[str, _MinuteStates] = {}
        self._first: dict[str, tuple[float, float, float]] = {}  # code -> (t, ltp, run) of the first entry >= 09:15
        self._oi: dict[str, _Oi] = {}
        self.oi_prev: dict[str, float] = {}
        #: (code, previous session) -> the as-of OI at OI_GRID — the walls' oiChangePct windows
        self.oi_grid_prev: dict[tuple[str, str], tuple[float, ...]] = {}
        self._vol: dict[str, _Vol] = {}
        self._pend: dict[tuple[str, int], list[float]] = {}
        #: minutes that started before this are no longer held (``trim``)
        self._minutes_from = 0.0
        #: (code, day, bar start) -> (volume, verified, covered since) — today's closed bars and the loaded history
        self._b30: dict[tuple[str, str, str], tuple[float, bool, float | None]] = {}
        self._bars_closed: set[str] = set()
        self._rows30: list[dict[str, Any]] = []
        self.gaps: list[tuple[float, float, str]] = []
        self._last_frame: float | None = None
        # counters
        self.frames = 0
        self.frames_watched = 0
        self.frames_pooled = 0
        self.oi_prints = 0
        self.volume_restarts = 0
        self.late_minutes = 0
        self.errors = 0
        self.last_error = ""

    # -- the day --------------------------------------------------------------------------------------

    def roll(self, d: date) -> bool:
        """Start ``d``: yesterday's last OI prints become the previous session's, every per-day record is emptied.
        True when the day changed."""
        if self.day == d:
            return False
        if self.day is not None and d > self.day:
            # the previous session's last print: yesterday's, never an older one carried on
            self.oi_prev = {code: o.oi[-1] for code, o in self._oi.items() if len(o.oi)}
            # yesterday's closed bars stay: the next day's earliest VOL baselines reach back into them
            keep = {self.day.isoformat()}
            self._b30 = {k: v for k, v in self._b30.items() if k[1] in keep}
        self.day = d
        self._day0 = day_start(d)
        self._open = at_ist(d, "09:15")
        self._close = at_ist(d, "15:30")
        self._ring.clear()
        self._mins.clear()
        self._first.clear()
        self._oi.clear()
        self._vol.clear()
        self._pend.clear()
        self._minutes_from = 0.0
        self._bars_closed.clear()
        self.gaps = []
        self._last_frame = None
        return True

    def set_watch(self, watch: Mapping[str, str]) -> None:
        self.watch = {str(c): r for c, r in watch.items() if r in ROLES}

    # -- the feed tap ---------------------------------------------------------------------------------

    def on_frame(self, channel: str, row: Mapping[str, Any], arrived: float, pooled: bool = False) -> None:
        """Every market-feed / OI row the socket carries. Never raises: a recorder fault is counted, never the
        reader's."""
        try:
            if not pooled:
                self._arrival(arrived)
            code = str(row.get("Token") or row.get("ScripCode") or "")
            role = self.watch.get(code)
            if role is None:
                return
            if self.day is None or arrived >= self._day0 + 86_400:
                self.roll(ist_day(arrived))
            if arrived < self._day0:
                return  # a pooled frame that first arrived on an earlier day: no quote, OI or volume of today's
            self.frames_watched += 1
            if pooled:
                self.frames_pooled += 1
            if channel == "oi":
                if not pooled:
                    self._take_oi(code, _num(row.get("OpenInterest")), arrived)
                return
            if channel != "mf":
                return
            ltp = _num(row.get("LastRate"))
            if ltp > 0:
                self._take_quote(code, role, arrived, ltp, _num(row.get("BidRate")), _num(row.get("OffRate")))
            if role == "opt" and row.get("TotalQty") is not None:
                self._take_volume(code, _num(row.get("TotalQty")), broker_ts(row.get("TickDt")), arrived, pooled)
        except Exception as exc:  # noqa: BLE001 - the recorder never costs the socket reader a frame
            self.errors += 1
            self.last_error = str(exc)[:200]

    def _arrival(self, arrived: float) -> None:
        self.frames += 1
        last = self._last_frame
        if (self.silence_s is not None and last is not None and arrived - last > self.silence_s
                and self._open <= arrived and last <= self._close):
            self.note_gap(last, arrived, "no frame for over a minute in the session")
        if last is None or arrived > last:
            self._last_frame = arrived

    def note_gap(self, a: float, b: float, why: str) -> None:
        """A stretch the volume record cannot vouch for (a reconnect, a silence): every 30m bar it touches is
        unverified."""
        if b > a:
            self.gaps.append((a, b, why))

    def note_reconnect(self, now: float) -> None:
        self.note_gap(self._last_frame if self._last_frame is not None else now, now, "feed reconnect")

    # -- quotes ---------------------------------------------------------------------------------------

    def _take_quote(self, code: str, role: str, t: float, ltp: float, bid: float, ask: float) -> None:
        ring = self._ring.get(code)
        if ring is None:
            ring = self._ring[code] = _Ring()
        if not ring.add(t, ltp, bid, ask):
            return
        first = self._first.get(code)
        if t >= self._open and (first is None or int(first[0]) == int(ring.t[-1])):
            # the first entry at or after 09:15 (a second frame in its second replaces it, as in the ring)
            self._first[code] = (ring.t[-1], ring.ltp[-1], ring.run[-1])
        if role != "opt":
            ms = self._mins.get(code)
            if ms is None:
                ms = self._mins[code] = _MinuteStates()
            ms.add(ring.t[-1], ltp, bid, ask, ring.run[-1])

    def trim(self, now: float) -> None:
        """The housekeeping pass: quote rings to ``ring_s``, minute volumes to ``MINUTES_KEPT_S``."""
        cut = now - self.ring_s
        for ring in self._ring.values():
            ring.trim(cut)
        mcut = now - MINUTES_KEPT_S
        if self.day is not None:
            # never a minute of a bar not yet closed: its total is read from them
            open_bars = [bar_bounds(self.day.isoformat(), s)[0] for s in GRID if s not in self._bars_closed]
            if open_bars:
                mcut = min(mcut, open_bars[0])
        if mcut <= self._minutes_from:
            return
        self._minutes_from = mcut
        for vs in self._vol.values():
            k = bisect_left(vs.m, mcut)
            if k:
                del vs.m[:k]
                del vs.v[:k]
                del vs.cum[:k]

    def quote(self, code: str, x: float, fresh_s: float = 60.0) -> dict[str, Any] | None:
        """research ``TapeDay.quote``: the last entry at or before ``x`` — eff, ltp, bid, ask, two, fresh, mid — plus
        ``run`` (when its last price first appeared) and ``src`` ('ring' / 'minute', the minute state beyond the
        ring). None when nothing is held for that moment."""
        ring = self._ring.get(code)
        j = ring.at(x) if ring is not None else -1
        if ring is not None and j >= 0:
            t, ltp, bid, ask, run, src = ring.t[j], ring.ltp[j], ring.bid[j], ring.ask[j], ring.run[j], "ring"
        else:
            ms = self._mins.get(code)
            k = ms.at(x) if ms is not None else -1
            if ms is None or k < 0:
                return None
            t, ltp, bid, ask, run, src = ms.t[k], ms.ltp[k], ms.bid[k], ms.ask[k], ms.run[k], "minute"
        two = bid > 0 and ask > 0 and ask >= bid
        return {"eff": t, "ltp": ltp, "bid": bid, "ask": ask, "two": two, "fresh": (x - t) <= fresh_s,
                "mid": (bid + ask) / 2 if two else math.nan, "age": x - t, "run": run, "src": src}

    def first_after_open(self, code: str) -> tuple[float, float, float] | None:
        """The code's first entry at or after 09:15 today: (time, last price, its run start)."""
        return self._first.get(code)

    def streaming_since(self, code: str) -> float | None:
        ring = self._ring.get(code)
        return ring.t[0] if ring is not None and len(ring.t) and not ring.trimmed else None

    # -- open interest --------------------------------------------------------------------------------

    def _take_oi(self, code: str, oi: float, t: float) -> None:
        if oi <= 0:
            return
        o = self._oi.get(code)
        if o is None:
            o = self._oi[code] = _Oi()
        if len(o.oi) and (o.oi[-1] == oi or t < o.t[-1]):
            return
        o.t.append(t)
        o.oi.append(oi)
        self.oi_prints += 1

    def oi_at(self, code: str, x: float, *, allow_prev: bool = True) -> tuple[float, float, str]:
        """research ``OIDay.at``: (OI, its time, basis) — the last print today at or before ``x`` (basis 'session'
        from 09:15, else 'preopen'), else the previous session's last print ('prevday') when allowed."""
        o = self._oi.get(code)
        if o is not None:
            j = bisect_right(o.t, x) - 1
            if j >= 0:
                return o.oi[j], o.t[j], ("session" if o.t[j] >= self._open else "preopen")
        if allow_prev and code in self.oi_prev:
            return self.oi_prev[code], math.nan, "prevday"
        return math.nan, math.nan, ""

    def oi_recorded(self, code: str, x: float) -> bool:
        """An OI print today at or before ``x`` (the walls' "recorded strike")."""
        o = self._oi.get(code)
        return o is not None and len(o.t) > 0 and o.t[0] <= x

    # -- volume ---------------------------------------------------------------------------------------

    def _take_volume(self, code: str, total: float, exch: float | None, arrived: float, pooled: bool) -> None:
        vs = self._vol.get(code)
        if vs is None:
            vs = self._vol[code] = _Vol()
        now = self._clock() if pooled else arrived
        if exch is not None and exch < self._day0:
            # the frame's last trade is from an earlier day: nothing has traded today, the reference is 0
            if vs.ref is None:
                vs.ref, vs.since = 0.0, now
            return
        if vs.ref is None:
            vs.ref, vs.since = total, now
            return
        if pooled:
            return  # a pooled frame is a state already streamed: it never adds volume
        d = total - vs.ref
        if d < 0:
            # the running total went back: whatever happened is unknown — the coverage restarts here
            vs.ref, vs.since = total, arrived
            vs.restarts += 1
            self.volume_restarts += 1
            return
        vs.ref = total
        if d == 0:
            return
        m = int((exch if exch is not None else arrived) // 60 * 60)
        if not vs.m or m > vs.m[-1]:
            vs.m.append(m)
            vs.v.append(d)
            vs.cum.append(total)
        elif m == vs.m[-1]:
            vs.v[-1] += d
            vs.cum[-1] = total
        else:
            k = bisect_left(vs.m, m)
            if k < len(vs.m) and vs.m[k] == m:
                vs.v[k] += d
            else:
                vs.m.insert(k, m)
                vs.v.insert(k, d)
                vs.cum.insert(k, total)
            self.late_minutes += 1
        p = self._pend.get((code, m))
        if p is None:
            self._pend[(code, m)] = [d, total]
        else:
            p[0] += d
            p[1] = max(p[1], total)

    def covered(self, code: str, a: float, b: float) -> tuple[bool, str]:
        """Is the volume of ``code`` over [a, b) on record: covered from at or before ``a`` and no gap inside."""
        vs = self._vol.get(code)
        if vs is None or vs.since is None:
            return False, "volume not recorded (no frame yet)"
        if vs.since > a:
            return False, f"volume recorded only from {hms(vs.since)}"
        for g0, g1, why in self.gaps:
            if g1 > a and g0 < b:
                return False, f"feed gap {hms(g0)}-{hms(g1)} ({why})"
        return True, ""

    def volume(self, code: str, a: float, b: float) -> tuple[float, str]:
        """research ``vol_since`` / the candle volume: the volume of the minutes starting at or after ``a`` and
        complete by ``b``; (NaN, why) when that stretch is not on record."""
        ok, why = self.covered(code, a, b)
        if not ok:
            return math.nan, why
        if a < self._minutes_from:
            return math.nan, f"minutes before {hms(self._minutes_from)} are no longer held"
        vs = self._vol[code]
        i = bisect_left(vs.m, a)
        j = bisect_right(vs.m, b - 60)
        return float(sum(vs.v[i:j])), ""

    def bar30(self, code: str, day: str, start: str, now: float) -> tuple[float, str]:
        """A 30m bar's volume for the VOL baseline: today's closed bar or the loaded history; (NaN, why) when it was
        not recorded or not verified. A bar of today that ended but is not closed yet is read from its minutes."""
        hit = self._b30.get((code, day, start))
        if hit is None:
            if self.day is not None and day == self.day.isoformat() and start not in self._bars_closed:
                a, b = bar_bounds(day, start)
                if b <= now:
                    v, why = self.volume(code, a, b)
                    return (v, "") if not why else (math.nan, f"baseline bar {day} {start}: {why}")
            return math.nan, f"baseline bar {day} {start} not recorded"
        v, verified, since = hit
        if not verified:
            return math.nan, f"baseline bar {day} {start} not verified" + (f" (recorded from {hms(since)})" if since else "")
        return v, ""

    def close_bars(self, now: float) -> int:
        """Close every grid bar of today that ended ``BAR_GRACE_S`` ago: each covered option's volume, verified or
        not, kept for the baselines and queued for the archive."""
        if self.day is None:
            return 0
        d = self.day.isoformat()
        n = 0
        for start in GRID:
            if start in self._bars_closed:
                continue
            a, b = bar_bounds(d, start)
            if b + BAR_GRACE_S > now:
                break
            self._bars_closed.add(start)
            for code, vs in self._vol.items():
                if vs.since is None or vs.since >= b:
                    continue
                ok, _ = self.covered(code, a, b)
                i, j = bisect_left(vs.m, a), bisect_right(vs.m, b - 60)
                v = float(sum(vs.v[i:j]))
                self._b30[(code, d, start)] = (v, ok, vs.since)
                self._rows30.append({"scrip_code": code, "bucket_ts": int(a), "v": v, "verified": ok, "from_ts": float(vs.since)})
                n += 1
        return n

    def take_bar_rows(self) -> list[dict[str, Any]]:
        rows, self._rows30 = self._rows30, []
        return rows

    def take_minute_rows(self, now: float, *, final: bool = False) -> list[tuple[str, int, float, float]]:
        """The minutes that ended ``MINUTE_GRACE_S`` ago (every one at ``final``), as additive rows."""
        cut = now - MINUTE_GRACE_S - 60
        out: list[tuple[str, int, float, float]] = []
        for key in [k for k in self._pend if final or k[1] <= cut]:
            v, cum = self._pend.pop(key)
            out.append((key[0], key[1], v, cum))
        out.sort(key=lambda r: (r[1], r[0]))
        return out

    def load_bars(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """30m bars from the archive (the previous session, and today's after a restart): kept unless today's
        own closed bar is already held."""
        n = 0
        for r in rows:
            a = float(r["bucket_ts"])
            d = ist_day(a).isoformat()
            start = datetime.fromtimestamp(a, IST).strftime("%H:%M")
            key = (str(r["scrip_code"]), d, start)
            if key in self._b30:
                continue
            since = r.get("from_ts")
            self._b30[key] = (float(r["v"]), bool(r["verified"]), float(since) if since is not None and since == since else None)
            n += 1
        return n

    # -- the walls' previous sessions ----------------------------------------------------------------

    def set_oi_grid(self, day: str, frame: pd.DataFrame) -> int:
        """A previous session's OI prints (scrip_code, ts, oi) → each code's as-of OI at ``OI_GRID`` (no previous-day
        fallback: research ``OIDay.at(..., allow_prev=False)``)."""
        if frame is None or not len(frame):
            return 0
        times = [at_ist(day, hm) for hm in OI_GRID]
        df = frame[frame.oi > 0].sort_values(["scrip_code", "ts"], kind="stable")
        n = 0
        for code, g in df.groupby(df.scrip_code.astype(str), sort=False):
            ts = g.ts.to_numpy(float)
            oi = g.oi.to_numpy(float)
            idx = np.searchsorted(ts, times, "right") - 1
            self.oi_grid_prev[(str(code), day)] = tuple(float(oi[j]) if j >= 0 else math.nan for j in idx)
            n += 1
        return n

    def oi_windows(self, code: str, c0: float, prev_days: list[str]) -> list[tuple[float, float]]:
        """The (OI at start, OI at end) of the 30m bars before the trigger candle, newest first: today's finished
        bars (end <= ``c0``) from the day's prints, then the previous sessions' from their grids (wall_state.py)."""
        out: list[tuple[float, float]] = []
        if self.day is not None:
            d = self.day.isoformat()
            for start in reversed(GRID):
                a, z = bar_bounds(d, start)
                if z > c0:
                    continue
                a_r = a + 60 if start == "09:15" else a
                out.append((self.oi_at(code, a_r, allow_prev=False)[0], self.oi_at(code, z, allow_prev=False)[0]))
        for d in prev_days:
            g = self.oi_grid_prev.get((code, d))
            for i in range(len(GRID) - 1, -1, -1):
                out.append((g[i], g[i + 1]) if g is not None else (math.nan, math.nan))
        return out

    # -- reads for the engine -------------------------------------------------------------------------

    def recorded_chain(self, codes: Iterable[str], x: float) -> list[str]:
        return [c for c in codes if self.oi_recorded(c, x)]

    def stats(self) -> dict[str, Any]:
        ring_entries = sum(len(r.t) for r in self._ring.values())
        oi_entries = sum(len(o.t) for o in self._oi.values())
        vol_minutes = sum(len(v.m) for v in self._vol.values())
        covered = sum(1 for v in self._vol.values() if v.since is not None)
        verified = sum(1 for k, v in self._b30.items() if self.day is not None and k[1] == self.day.isoformat() and v[1])
        today_bars = sum(1 for k in self._b30 if self.day is not None and k[1] == self.day.isoformat())
        return {
            "day": self.day.isoformat() if self.day else None, "watched": len(self.watch), "frames": self.frames,
            "frames_watched": self.frames_watched, "frames_pooled": self.frames_pooled, "codes_quoted": len(self._ring),
            "ring_entries": ring_entries, "oi_codes": len(self._oi), "oi_entries": oi_entries, "oi_prev": len(self.oi_prev),
            "volume_codes": covered, "volume_minutes_held": vol_minutes, "minutes_waiting": len(self._pend),
            "bars_closed_today": today_bars, "bars_verified_today": verified,
            "vol_verified_share": round(verified / today_bars, 3) if today_bars else None,
            "gaps": [[hms(a), hms(b), w] for a, b, w in self.gaps[-10:]], "volume_restarts": self.volume_restarts,
            "late_minutes": self.late_minutes, "errors": self.errors, "last_error": self.last_error,
            # arrays of 8-byte floats: quotes 5 a entry, OI 2, volume 3 (+ the minute states)
            "memory_mb_est": round((ring_entries * 40 + oi_entries * 16 + vol_minutes * 24) / 1e6, 1),
        }


class VolumeTape:
    """The per-minute option volumes as part files, ``<root>/<day>/<HHMMSS>.parquet`` (nothing is re-read; a reader
    concatenates a day's parts and SUMS ``v`` on (scrip_code, ts)). Day folders older than ``keep_days`` calendar
    days are deleted after each write."""

    def __init__(self, root: Path, *, keep_days: int = 90) -> None:
        self.root = root
        self.keep_days = max(1, int(keep_days))
        self._lock = threading.Lock()
        self.rows_written = 0
        self.files_written = 0
        self.errors = 0
        self.last_error = ""

    def write(self, rows: list[tuple[str, int, float, float]], *, now: float | None = None) -> int:
        """Blocking — a worker thread."""
        if not rows:
            return 0
        with self._lock:
            by_day: dict[str, list[tuple[str, int, float, float]]] = {}
            for r in rows:
                by_day.setdefault(ist_day(r[1]).isoformat(), []).append(r)
            n = 0
            for day, part in by_day.items():
                try:
                    d = self.root / day
                    d.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.fromtimestamp(now if now is not None else time.time(), IST).strftime("%H%M%S")
                    path, k = d / f"{stamp}.parquet", 1
                    while path.exists():
                        path, k = d / f"{stamp}-{k}.parquet", k + 1
                    tmp = path.with_suffix(".tmp")
                    pd.DataFrame(part, columns=list(VOLUME_COLUMNS)).to_parquet(tmp, index=False)
                    tmp.replace(path)
                    n += len(part)
                    self.files_written += 1
                except Exception as exc:  # noqa: BLE001 - the recorder must never take the engine down
                    self.errors += 1
                    self.last_error = f"{day}: {exc}"[:200]
                    log.warning("option_volume.write_failed", day=day, error=str(exc)[:120])
            self.rows_written += n
            try:
                self.prune(now)
            except Exception as exc:  # noqa: BLE001 - retention is housekeeping
                self.errors += 1
                self.last_error = f"prune: {exc}"[:200]
            return n

    def prune(self, now: float | None = None) -> int:
        if not self.root.exists():
            return 0
        cutoff = ist_day(now if now is not None else time.time()) - timedelta(days=self.keep_days)
        removed = 0
        for d in self.root.iterdir():
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue
            if d.is_dir() and day < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        return {"keep_days": self.keep_days, "rows_written": self.rows_written, "files_written": self.files_written,
                "errors": self.errors, "last_error": self.last_error}


def read_volume_day(root: Path, day: str) -> pd.DataFrame:
    """A day's minute volumes, the parts summed on (scrip_code, ts)."""
    d = root / day
    parts = sorted(d.glob("*.parquet")) if d.exists() else []
    if not parts:
        return pd.DataFrame(columns=list(VOLUME_COLUMNS))
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    return df.groupby(["scrip_code", "ts"], as_index=False).agg(v=("v", "sum"), cum=("cum", "max"))


__all__ = ["OI_GRID", "RING_S", "OptionRecorder", "VolumeTape", "broker_ts", "day_start", "hms", "read_volume_day"]
