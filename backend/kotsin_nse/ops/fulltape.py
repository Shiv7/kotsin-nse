"""The full tape: the top of book of EVERY code on the market feed, once a second on change, for the
whole NSE session (operator, 2026-09-27: record the bid/ask of every candidate strike so a coming day
can be replayed against the real book; "auto-delete tapes older than 60 days").

The tick tape (``ops/tape.py``) keeps what the engine held, considered or carded — enough to replay
an exit, not to test a different rule, which may pick a strike nobody looked at. This keeps every
code the feed quotes: the equities, their futures and the option strikes the universe builder
selected around each spot (~3,000 codes). MCX is left out (it trades to 23:30 and is not what this
is for). A code enters the engine's quote table on its first traded price, so a strike that has not
traded yet today is not on it either.

Written apart from the daily archive on purpose: that archive re-reads and rewrites the whole day's
file at every flush, which at this size would mean rewriting millions of rows every few minutes
inside the engine. Here the rows wait as tuples, and each flush writes a NEW part file —
``<root>/<day>/<HHMMSS>.parquet`` — nothing is ever re-read; a reader concatenates a day's parts.
A row is written when the last price, the bid or the ask changed since that code's last row (the
broker's tick time alone is not a change; it is kept as ``quote_ts``). Day directories older than
``keep_days`` calendar days are deleted after each flush.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pandas as pd
import structlog

from ..market.session import IST, ist_hm

log = structlog.get_logger(__name__)

COLUMNS = ("scrip_code", "ts", "ltp", "bid", "ask", "quote_ts")
#: the NSE session with a minute either side: pre-open prints and the close
WINDOW = ("09:14", "15:31")


class QuoteLike(Protocol):
    ltp: float
    bid: float
    ask: float
    ts: float


class FullTape:
    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        keep_days: int = 60,
        skip: Callable[[str], bool] | None = None,
        window: tuple[str, str] = WINDOW,
    ) -> None:
        self.root = root
        self.enabled = enabled
        self.keep_days = max(1, int(keep_days))
        self.skip = skip or (lambda _code: False)
        self.window = window
        self._rows: list[tuple[str, int, float, float, float, float]] = []
        self._last: dict[str, tuple[float, float, float]] = {}
        self._lock = threading.Lock()
        self.rows_written = 0
        self.files_written = 0
        self.days_pruned = 0
        self.errors = 0
        self.last_error = ""
        self.last_write_ts: float | None = None
        #: bytes on disk, measured after each write's retention (a health poll must not walk the tree)
        self.disk_bytes = 0

    def sample(self, now: float, quotes: Mapping[str, QuoteLike]) -> int:
        """This second's changes. Runs on the engine's clock, in the event loop (as the quote table
        is written there), so nothing here waits on I/O."""
        if not self.enabled or not (self.window[0] <= ist_hm(now) <= self.window[1]):
            return 0
        ts = int(now)
        added = 0
        for code, q in quotes.items():
            if q.ltp <= 0 and q.bid <= 0 and q.ask <= 0:
                continue
            key = (float(q.ltp), float(q.bid), float(q.ask))
            if self._last.get(code) == key or self.skip(code):
                continue
            self._last[code] = key
            self._rows.append((str(code), ts, key[0], key[1], key[2], float(q.ts)))
            added += 1
        return added

    def take(self) -> list[tuple[str, int, float, float, float, float]]:
        """The rows waiting, handed over in the event loop so the writer thread never shares a list
        that ``sample`` is appending to."""
        rows, self._rows = self._rows, []
        return rows

    @property
    def rows_waiting(self) -> int:
        return len(self._rows)

    def write(self, rows: list[tuple[str, int, float, float, float, float]], *, now: float | None = None) -> int:
        """A new part file per IST day in ``rows``, then the retention. Blocking — a worker thread."""
        if not rows:
            return 0
        with self._lock:
            written = 0
            by_day: dict[str, list[tuple[str, int, float, float, float, float]]] = {}
            for r in rows:
                by_day.setdefault(datetime.fromtimestamp(r[1], IST).date().isoformat(), []).append(r)
            for day, part in by_day.items():
                try:
                    d = self.root / day
                    d.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.fromtimestamp(part[-1][1], IST).strftime("%H%M%S")
                    path = d / f"{stamp}.parquet"
                    n = 1
                    while path.exists():  # two flushes in one second never overwrite each other
                        path, n = d / f"{stamp}-{n}.parquet", n + 1
                    tmp = path.with_suffix(".tmp")
                    pd.DataFrame(part, columns=list(COLUMNS)).to_parquet(tmp, index=False)
                    tmp.replace(path)
                    written += len(part)
                    self.files_written += 1
                except Exception as exc:  # noqa: BLE001 - the tape must never take the engine down
                    self.errors += 1
                    self.last_error = f"{day}: {exc}"[:200]
                    log.warning("tape_full.write_failed", day=day, error=str(exc)[:120])
            self.rows_written += written
            self.last_write_ts = time.time()
            try:
                self.prune(now)
            except Exception as exc:  # noqa: BLE001 - retention is housekeeping, never a fault
                self.errors += 1
                self.last_error = f"prune: {exc}"[:200]
            return written

    def prune(self, now: float | None = None) -> int:
        """Delete the day directories older than ``keep_days`` calendar days."""
        if not self.root.exists():
            return 0
        today = datetime.fromtimestamp(now if now is not None else time.time(), IST).date()
        cutoff = today - timedelta(days=self.keep_days)
        removed = 0
        for d in self.root.iterdir():
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue
            if d.is_dir() and day < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        if removed:
            self.days_pruned += removed
            log.info("tape_full.pruned", days=removed, keep_days=self.keep_days)
        self.disk_bytes = sum(p.stat().st_size for p in self.root.rglob("*.parquet"))
        return removed

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "keep_days": self.keep_days, "rows_waiting": len(self._rows), "rows_written": self.rows_written,
            "files_written": self.files_written, "days_pruned": self.days_pruned, "errors": self.errors, "last_error": self.last_error,
            "last_write_ts": self.last_write_ts, "codes_seen": len(self._last), "disk_mb": round(self.disk_bytes / 1e6, 1),
        }


def read_day(root: Path, day: str) -> pd.DataFrame:
    """A day's full tape: every part, in time order (forward-fill per code to read a quote at a second)."""
    d = root / day
    parts = sorted(d.glob("*.parquet")) if d.exists() else []
    if not parts:
        return pd.DataFrame(columns=list(COLUMNS))
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True).sort_values(["ts", "scrip_code"]).reset_index(drop=True)


__all__ = ["COLUMNS", "FullTape", "read_day"]
