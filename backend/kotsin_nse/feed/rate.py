"""How often a source actually updates each instrument — measured, never assumed.

A footprint, a delta or even a 1-minute candle is only as fine as the frames under it. 5paisa's
socket was measured at ~6.5 quote frames a minute per symbol on MCX; NSE was never measured, and a
vendor's "tick" feed is a 1-second snapshot, not every trade. This meter answers, per source and
segment, from the live session itself:

* ``frames_per_min`` — quote frames per instrument per minute of activity;
* ``trades_per_min`` — frames that carried NEW volume (``total_qty`` went up), i.e. the frames
  that can be attributed to trades at all;
* the gap between consecutive frames of one instrument (median / p90, seconds).

Cheap enough for the tick path: two dict lookups and a bounded ring of recent gaps per instrument.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import median
from typing import Any

#: gaps kept per instrument; enough for a stable median, bounded so a session cannot grow it
GAP_WINDOW = 200


@dataclass
class _Series:
    group: str
    first_ts: float
    last_ts: float
    frames: int = 0
    trades: int = 0
    last_total: int = 0
    gaps: deque[float] = field(default_factory=lambda: deque(maxlen=GAP_WINDOW))


class FeedRateMeter:
    def __init__(self) -> None:
        self._series: dict[tuple[str, str], _Series] = {}

    def on_quote(self, source: str, key: str, group: str, recv_ts: float, total_qty: int) -> None:
        s = self._series.get((source, key))
        if s is None:
            self._series[(source, key)] = _Series(group=group, first_ts=recv_ts, last_ts=recv_ts, frames=1, last_total=total_qty)
            return
        gap = recv_ts - s.last_ts
        if gap >= 0:
            s.gaps.append(gap)
        s.last_ts = recv_ts
        s.frames += 1
        if total_qty > s.last_total:
            s.trades += 1
            s.last_total = total_qty

    def reset(self) -> None:
        self._series.clear()

    def snapshot(self) -> dict[str, Any]:
        """Per source and group: medians across instruments, so one illiquid strike does not
        drag the reading and one hyperactive future does not flatter it."""
        by: dict[tuple[str, str], list[_Series]] = defaultdict(list)
        for (source, _key), s in self._series.items():
            by[(source, s.group)].append(s)
        out: dict[str, Any] = {}
        for (source, group), rows in sorted(by.items()):
            per_min: list[float] = []
            trades_min: list[float] = []
            gaps_med: list[float] = []
            gaps_p90: list[float] = []
            for s in rows:
                minutes = (s.last_ts - s.first_ts) / 60
                if minutes >= 1:
                    per_min.append(s.frames / minutes)
                    trades_min.append(s.trades / minutes)
                if len(s.gaps) >= 5:
                    g = sorted(s.gaps)
                    gaps_med.append(g[len(g) // 2])
                    gaps_p90.append(g[min(len(g) - 1, int(len(g) * 0.9))])
            out.setdefault(source, {})[group] = {
                "instruments": len(rows),
                "measured": len(per_min),
                "frames_per_min": round(median(per_min), 2) if per_min else None,
                "trades_per_min": round(median(trades_min), 2) if trades_min else None,
                "gap_median_s": round(median(gaps_med), 2) if gaps_med else None,
                "gap_p90_s": round(median(gaps_p90), 2) if gaps_p90 else None,
            }
        return out
