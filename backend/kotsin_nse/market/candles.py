"""5paisa's intraday candles, put on the session grid — once, the same way for every reader.

The historical endpoint stamps a candle with the minute of its FIRST trade, not its bucket: 09:16,
10:46, the closing auction at 15:28 — such a row IS its bucket. It also returns rows outside the
session: a post-close 15:45 / 15:50 row (the bar that read 0.00x before the 2026-09-28 09:45
triggers) and pre-open prints. This rule was implemented three times (``aggregator.seed``,
``verify.audit_day``, ``engine._snap_fut_rows``) and missing in three more readers — the decision-
time reconcile and the sweep matched rows by exact timestamp, and the backtest cache stored the raw
stamps, so the backtest's SuperTrend and Bollinger ran on 393 post-close bars live never sees.

``snap_candles`` is idempotent: the 5paisa adapter applies it at the edge and any reader may apply it
again to rows from anywhere (a cache written before this existed, a test's fake broker).

Daily (``1d``) rows are not touched: a session's daily candle has its own rules
(``bars/daily.one_per_session``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..config import Segment
from .session import bucket_start, in_session, ist_naive_to_ts, to_ist

_FMT = "%Y-%m-%dT%H:%M:%S"


@dataclass(slots=True)
class Snapped:
    rows: list[dict[str, Any]]
    #: broker stamps dropped as outside the session (pre-open prints, post-close rows)
    dropped: list[str] = field(default_factory=list)
    #: rows moved onto their bucket (stamped at their first trade)
    moved: int = 0


def snap_candles(rows: Iterable[dict[str, Any]], segment: Segment, tf: str) -> Snapped:
    """``rows``: the broker's ``{"dt", "o", "h", "l", "c", "v"}`` dicts (``dt`` naive IST). Returns the
    in-session rows, oldest first, one per bucket, each ``dt`` rewritten to its bucket start; a row
    that was moved keeps its own stamp in ``stamped``. A bucket's own on-grid row wins over a moved
    one, should both come."""
    rows = list(rows)
    if tf == "1d":
        return Snapped(rows=rows)
    by: dict[int, tuple[bool, dict[str, Any]]] = {}
    dropped: list[str] = []
    for r in rows:
        ts = ist_naive_to_ts(str(r["dt"]))
        if not in_session(segment, ts):
            dropped.append(str(r["dt"]))
            continue
        bucket = int(bucket_start(segment, ts, tf))
        on_grid = int(ts) == bucket
        held = by.get(bucket)
        if held is not None and held[0] and not on_grid:
            continue
        by[bucket] = (on_grid, r if on_grid else {**r, "dt": to_ist(bucket).strftime(_FMT), "stamped": str(r["dt"])})
    out = [by[b][1] for b in sorted(by)]
    return Snapped(rows=out, dropped=dropped, moved=sum(1 for _, (on, _r) in by.items() if not on))


__all__ = ["Snapped", "snap_candles"]
