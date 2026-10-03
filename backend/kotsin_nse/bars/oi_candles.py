"""OI candles: the open interest of a contract as open/high/low/close per bucket, built from the raw
OI prints we receive — never from the broker's change field (operator, 2026-10-03: "if we getting raw
OI we store that right? take change from own, create OI candle").

5paisa sends a contract's OI level about once a minute (median 65 s, 90th percentile 88 s on
2026-10-01), so a 1-minute OI candle is the finest that means anything; 30m and the day roll up from
the same prints. A candle's close is the last print inside its bucket, which makes the day candle's
close the session's last OI — the reference the next session's change is measured from when the
exchange's own number (``market/fo_bhavcopy.py``) is not to hand.

Only prints inside the session become candles: the frames that arrive before the open and after the
close carry the previous close or the final number, and are the archive's business (``Engine.
_seed_oi_reference``), not a bucket's. Pure: no I/O, no clock — the engine hands it prints and asks.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from ..config import Segment
from ..market.session import bucket_end, bucket_start, in_session

TIMEFRAMES: tuple[str, ...] = ("1m", "30m", "1d")
#: closed candles kept per contract and timeframe: two sessions of 30m, a session of 1m, a month of days
KEEP: dict[str, int] = {"1m": 400, "30m": 30, "1d": 25}


@dataclass(slots=True)
class OiCandle:
    code: str
    tf: str
    ts: int  # bucket START, epoch seconds UTC — the same convention as the price bars
    o: float
    h: float
    l: float  # noqa: E741 - the candle's low, as everywhere else in the bars package
    c: float
    prints: int = 1
    last_ts: float = 0.0

    def add(self, oi: float, ts: float) -> None:
        self.h = max(self.h, oi)
        self.l = min(self.l, oi)
        self.c = oi
        self.prints += 1
        self.last_ts = ts

    @property
    def change(self) -> float:
        """The OI added (or shed) inside this bucket."""
        return self.c - self.o

    def to_json(self) -> dict[str, float | int | str]:
        return {"code": self.code, "tf": self.tf, "ts": self.ts, "o": self.o, "h": self.h, "l": self.l,
                "c": self.c, "prints": self.prints}


class OiCandleBuilder:
    """Every tracked contract's forming and recently closed OI candles, per timeframe."""

    def __init__(self, timeframes: tuple[str, ...] = TIMEFRAMES, keep: dict[str, int] | None = None) -> None:
        self.timeframes = timeframes
        self.keep = {**KEEP, **(keep or {})}
        self._forming: dict[tuple[str, str], OiCandle] = {}
        self._closed: dict[tuple[str, str], deque[OiCandle]] = {}
        self.prints = 0
        self.out_of_session = 0
        self.late = 0

    def on_print(self, code: str, oi: float, ts: float, segment: Segment, *, trading_day: bool = True) -> list[OiCandle]:
        """Take one OI print; returns the candles it closed (a print in a later bucket closes the
        one before). A print outside the session — or on a day the exchange is shut, which the
        caller's calendar answers (``trading_day``): 5paisa re-sends each contract's last level at a
        weekend subscribe, and between 09:15 and 15:30 it made a "Saturday" candle (review,
        2026-10-03) — or older than the bucket forming, is counted and dropped."""
        if oi <= 0:
            return []
        if not trading_day or not in_session(segment, ts):
            self.out_of_session += 1
            return []
        self.prints += 1
        closed: list[OiCandle] = []
        for tf in self.timeframes:
            bucket = int(bucket_start(segment, ts, tf))
            key = (code, tf)
            cur = self._forming.get(key)
            if cur is not None and bucket < cur.ts:
                self.late += 1
                continue
            if cur is not None and bucket > cur.ts:
                closed.append(self._close(key, cur))
                cur = None
            if cur is None:
                self._forming[key] = OiCandle(code, tf, bucket, oi, oi, oi, oi, 1, ts)
            else:
                cur.add(oi, ts)
        return closed

    def flush(self, now: float, segment_of: dict[str, Segment] | None = None,
              default: Segment = Segment.NSE_FO) -> list[OiCandle]:
        """Close every forming candle whose bucket has ended by ``now`` — a contract that stops
        printing at 14:32 must not leave its 14:30 candle open until tomorrow."""
        closed = []
        for key, cur in list(self._forming.items()):
            seg = (segment_of or {}).get(cur.code, default)
            if now >= bucket_end(seg, cur.ts, cur.tf):
                closed.append(self._close(key, cur))
        return closed

    def _close(self, key: tuple[str, str], candle: OiCandle) -> OiCandle:
        self._forming.pop(key, None)
        ring = self._closed.get(key)
        if ring is None:
            ring = self._closed[key] = deque(maxlen=self.keep.get(candle.tf, 30))
        ring.append(candle)
        return candle

    def forming(self, code: str, tf: str) -> OiCandle | None:
        return self._forming.get((code, tf))

    def closed(self, code: str, tf: str, n: int | None = None) -> list[OiCandle]:
        ring = list(self._closed.get((code, tf), ()))
        return ring[-n:] if n else ring

    def series(self, code: str, tf: str, n: int | None = None) -> list[OiCandle]:
        """Closed candles, then the forming one — what a chart shows."""
        out = self.closed(code, tf)
        cur = self.forming(code, tf)
        if cur is not None:
            out.append(cur)
        return out[-n:] if n else out

    def stats(self) -> dict[str, int]:
        return {"prints": self.prints, "out_of_session": self.out_of_session, "late": self.late,
                "contracts": len({c for c, _ in self._forming} | {c for c, _ in self._closed})}


def candles_from_prints(
    prints: Iterable[tuple[str, float, float]], tf: str, segment: Segment = Segment.NSE_FO,
) -> list[OiCandle]:
    """Rebuild one timeframe's candles from archived prints ``(code, ts, oi)`` — the same bucketing
    the live builder uses, so a day replayed from the archive gives the candles it gave live."""
    b = OiCandleBuilder(timeframes=(tf,), keep={tf: 1_000_000})
    out: list[OiCandle] = []
    for code, ts, oi in sorted(prints, key=lambda p: (p[0], p[1])):
        out.extend(b.on_print(str(code), float(oi), float(ts), segment))
    out.extend(b.flush(float("inf"), default=segment))
    return sorted(out, key=lambda c: (c.code, c.ts))


__all__ = ["KEEP", "TIMEFRAMES", "OiCandle", "OiCandleBuilder", "candles_from_prints"]
