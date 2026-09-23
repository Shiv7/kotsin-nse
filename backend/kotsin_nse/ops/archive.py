"""The live archive: what the socket delivered, kept.

Nothing kept the live session until 2026-09-22 — ``archive_enabled`` was a config key nothing
read (R1). Everything FUKAA needs to be tested (option and future OI at bar resolution, the
depth-derived microstructure) exists only on the socket; the REST history has none of it, so a
session that is not archived is a session that can never be replayed.

Per IST day, one Parquet file per stream under ``data/archive/<kind>/<day>.parquet``:

* ``bars`` — every closed 1m bar of every tracked underlying (symbol, ts, o/h/l/c/v, source, oi)
* ``oi`` — every OI frame for every subscribed code (futures and option strikes)
* ``micro`` — the decision-frame microstructure metrics the engine stamps on 30m bars
* ``option_quotes`` — the alert cards' contracts sampled with the spot and delta that priced them
* ``quotes`` — the tick tape (``ops/tape.py``): the top-of-book of every contract the engine held,
  considered or carded, and of its equity and future legs, one row per second per change
* ``quotes_held`` — the rows of the tape that belong to a contract that was actually held, moved
  here when their day rolls out of the ``quotes`` window, so a trade stays replayable for a year

Compact (tens of MB a day), append-safe (a flush re-reads the day's file and de-duplicates on
the key), readable by the same pandas the research code uses. Raw frames are not kept: at ~19k
ticks a minute they would be a gigabyte a day for information the 1m bar already carries.

**Retention** (operator, 2026-09-23 evening): a rolling window of day files per stream — the disk
this runs on was at 96 %. The window is per stream because the streams answer different
questions:

* ``quotes`` — 15 sessions. The tape exists to replay an exit second by second; three weeks
  covers "why was this stop hit" and a policy change checked against the last fortnight. It is
  also the only stream big enough to matter (5–8 MB a session against ~10 MB a session for all
  the rest together).
* ``quotes_held`` — 250 sessions (~a year). A contract that was actually traded stays replayable
  long after the day it traded on left the tape's window.
* ``bars`` / ``oi`` / ``micro`` / ``option_quotes`` — 250 sessions. These are the committee's
  and FUKAA's backtest inputs, and FUKAA needs *months* of option OI: a 15-session window on
  ``oi`` would quietly delete the one input that book cannot be tested without.

The day file is the unit: nothing is rewritten in place, the oldest files past a stream's window
are deleted after each flush, so a crash mid-session can never touch an older day.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from ..bars.unified import UnifiedBar
from ..market.session import ist_day

log = structlog.get_logger(__name__)

KEYS: dict[str, tuple[str, ...]] = {
    "bars": ("symbol", "ts"),
    "oi": ("scrip_code", "ts"),
    "micro": ("scrip_code", "bucket_ts"),
    # Sampled rather than bar-aggregated, so the de-dup key is the sample instant itself.
    "option_quotes": ("scrip_code", "ts"),
    "quotes": ("scrip_code", "ts"),
    "quotes_held": ("scrip_code", "ts"),
}
#: streams whose ``held`` rows are moved to a longer-lived stream before their day is pruned
HELD_ASIDE: dict[str, str] = {"quotes": "quotes_held"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DailyArchive:
    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        keep_sessions: int = 0,
        keep_held_sessions: int = 0,
        keep_research_sessions: int = 0,
    ) -> None:
        self.root = root
        self.enabled = enabled
        #: tape day files kept after a flush; 0 = keep everything (the same for the two below)
        self.keep_sessions = max(0, int(keep_sessions))
        #: the traded-contract tape set aside from it
        self.keep_held_sessions = max(0, int(keep_held_sessions))
        #: bars / oi / micro / option_quotes — the backtest inputs
        self.keep_research_sessions = max(0, int(keep_research_sessions))
        self._rows: dict[str, dict[str, list[dict[str, Any]]]] = {k: defaultdict(list) for k in KEYS}
        self.rows_buffered = 0
        self.rows_written = 0
        self.files_written = 0
        self.files_pruned = 0
        self.rows_set_aside = 0
        #: day files kept past their window because their held rows could not be preserved
        self.set_aside_failed = 0
        self.flushes = 0
        self.errors = 0
        self.last_error = ""
        self.last_flush_ts: float | None = None

    # -- record ----------------------------------------------------------------------------------

    def _add(self, kind: str, ts: float, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self._rows[kind][ist_day(ts).isoformat()].append(row)
        self.rows_buffered += 1

    def bar(self, b: UnifiedBar) -> None:
        self._add(
            "bars",
            b.ts,
            {
                "symbol": b.symbol,
                "scrip_code": b.scrip_code,
                "ts": int(b.ts),
                "o": float(b.open),
                "h": float(b.high),
                "l": float(b.low),
                "c": float(b.close),
                "v": float(b.volume),
                "source": b.source.value,
                "oi": float(b.oi) if b.oi is not None else None,
            },
        )

    def oi(self, scrip_code: str, ts: float, oi: float, change_pct: float | None) -> None:
        self._add(
            "oi",
            ts,
            {"scrip_code": str(scrip_code), "ts": float(ts), "oi": float(oi), "change_pct": change_pct},
        )

    def option_quote(
        self,
        scrip_code: str,
        ts: float,
        *,
        ltp: float | None,
        bid: float | None,
        ask: float | None,
        spot: float | None,
        delta: float | None,
    ) -> None:
        """A sampled option quote, with the spot and delta that priced it.

        Option 1m bars cannot be built the way underlying bars are: an option Instrument's
        ``symbol`` is the underlying root (``catalogue.py:152``), so tracking one in the aggregator
        trips the bar-series collision guard that exists to stop futures ticks landing in the
        equity's series. Sampling the quote instead costs nothing and answers the question that
        needs answering — whether the premium moved by more than delta explains, which is the
        whole gamma-versus-theta test. The spot and delta are stored beside it so the residual can
        be computed without re-deriving either.
        """
        self._add(
            "option_quotes",
            ts,
            {
                "scrip_code": str(scrip_code),
                "ts": float(ts),
                "ltp": None if ltp is None else float(ltp),
                "bid": None if bid is None else float(bid),
                "ask": None if ask is None else float(ask),
                "spot": None if spot is None else float(spot),
                "delta": None if delta is None else float(delta),
            },
        )

    def quote(
        self,
        scrip_code: str,
        ts: int,
        *,
        symbol: str,
        role: str,
        ltp: float,
        bid: float,
        ask: float,
        quote_ts: float,
        held: bool,
    ) -> None:
        """One second of the tick tape: the top of book the exit engine judged against at ``ts``
        (whole seconds — the sample instant is the key), with the broker's own tick time beside
        it so a replay can tell a live quote from one that had gone stale."""
        self._add(
            "quotes",
            float(ts),
            {
                "scrip_code": str(scrip_code),
                "ts": int(ts),
                "symbol": symbol,
                "role": role,
                "ltp": float(ltp),
                "bid": float(bid),
                "ask": float(ask),
                "quote_ts": float(quote_ts),
                "held": bool(held),
            },
        )

    def micro(self, scrip_code: str, bucket_ts: int, metrics: Mapping[str, Any]) -> None:
        row: dict[str, Any] = {"scrip_code": str(scrip_code), "bucket_ts": int(bucket_ts)}
        for k, v in metrics.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                row[k] = float(v)
        self._add("micro", bucket_ts, row)

    # -- persist ---------------------------------------------------------------------------------

    def flush(self, *, final: bool = False) -> int:
        """Write every buffered day, then apply the retention window. Blocking pandas I/O — call
        it from a worker thread."""
        if not self.enabled:
            return 0
        written = 0
        for kind, by_day in self._rows.items():
            for day in list(by_day):
                rows = by_day.pop(day)
                if not rows:
                    continue
                try:
                    written += self._write(kind, day, rows)
                except Exception as exc:  # noqa: BLE001 - the archive must never take the engine down
                    self.errors += 1
                    self.last_error = f"{kind}/{day}: {exc}"[:200]
                    by_day[day].extend(rows)  # keep them for the next attempt
                    log.warning("archive.write_failed", kind=kind, day=day, error=str(exc))
        self.rows_buffered = sum(len(r) for by_day in self._rows.values() for r in by_day.values())
        self.flushes += 1
        self.last_flush_ts = time.time()
        if written:
            log.info("archive.flushed", rows=written, final=final)
        if self._any_window:
            try:
                self.prune()
            except Exception as exc:  # noqa: BLE001 - retention is housekeeping, never a fault
                self.errors += 1
                self.last_error = f"prune: {exc}"[:200]
                log.warning("archive.prune_failed", error=str(exc))
        return written

    def _write(self, kind: str, day: str, rows: list[dict[str, Any]]) -> int:
        path = self.root / kind / f"{day}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = pd.DataFrame(rows)
        if path.exists():
            existing = pd.read_parquet(path)
            fresh = pd.concat([existing, fresh], ignore_index=True)
        keys = list(KEYS[kind])
        fresh = fresh.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
        tmp = path.with_suffix(".tmp.parquet")
        fresh.to_parquet(tmp, index=False)
        tmp.replace(path)
        self.rows_written += len(rows)
        self.files_written += 1
        return len(rows)

    def days(self, kind: str) -> list[str]:
        """The day files a stream holds, oldest first. A ``*.tmp.parquet`` left by a crash is not
        a day and is never counted as one."""
        d = self.root / kind
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.parquet") if _DAY.match(p.stem))

    @property
    def _any_window(self) -> bool:
        return bool(self.keep_sessions or self.keep_held_sessions or self.keep_research_sessions)

    def keep_for(self, kind: str) -> int:
        """The window a stream is kept for. The tape is the operator's 15; the traded-contract
        tape and the backtest inputs are kept far longer (see the module docstring)."""
        if kind in HELD_ASIDE:
            return self.keep_sessions
        if kind in HELD_ASIDE.values():
            return self.keep_held_sessions
        return self.keep_research_sessions

    def prune(self) -> dict[str, int]:
        """Keep the newest ``keep_for(kind)`` day files of every stream and delete the rest.

        Sessions, not calendar days: a file exists only for a day something was recorded, so the
        window is fifteen trading days whatever the holidays did. Before a ``quotes`` day goes,
        its ``held`` rows — the contracts that were actually traded and their legs — are merged
        into ``quotes_held``, which has its own (longer) window. Stray temp files older than the
        newest day are removed with the rest.
        """
        removed: dict[str, int] = {}
        if not self._any_window:
            return removed
        for kind in KEYS:
            keep = self.keep_for(kind)
            if not keep:
                continue
            days = self.days(kind)
            for day in days[:-keep]:
                aside = HELD_ASIDE.get(kind)
                if aside and not self._set_aside(kind, aside, day):
                    # Deleting it anyway would lose the day's traded contracts for good, and the
                    # only sign would be a log line. Keep the file; the next flush tries again.
                    self.set_aside_failed += 1
                    continue
                (self.root / kind / f"{day}.parquet").unlink(missing_ok=True)
                removed[kind] = removed.get(kind, 0) + 1
                self.files_pruned += 1
            d = self.root / kind
            if d.exists():
                for tmp in d.glob("*.tmp.parquet"):
                    tmp.unlink(missing_ok=True)
        if removed:
            log.info("archive.pruned", files=removed, keep_tape=self.keep_sessions)
        return removed

    def _set_aside(self, kind: str, aside: str, day: str) -> bool:
        """Move ``day``'s held rows into the longer-lived stream. True when the source may now be
        deleted — including when there was genuinely nothing to preserve. False means the rows are
        still only in the source file, so the caller must keep it."""
        src = self.root / kind / f"{day}.parquet"
        try:
            df = pd.read_parquet(src)
        except (OSError, ValueError) as exc:
            log.warning("archive.set_aside_unreadable", kind=kind, day=day, error=str(exc)[:120])
            return False
        if "held" not in df.columns:
            return True
        rows = df[df["held"].astype(bool)]
        if rows.empty:
            return True
        try:
            self._write(aside, day, rows.to_dict("records"))
        except Exception as exc:  # noqa: BLE001 - a failed move must not become a deletion
            log.warning("archive.set_aside_failed", kind=kind, day=day, error=str(exc)[:120])
            return False
        self.rows_set_aside += len(rows)
        return True

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "keep_sessions": self.keep_sessions,
            "keep_held_sessions": self.keep_held_sessions,
            "keep_research_sessions": self.keep_research_sessions,
            "rows_buffered": self.rows_buffered,
            "rows_written": self.rows_written,
            "files_written": self.files_written,
            "files_pruned": self.files_pruned,
            "rows_set_aside": self.rows_set_aside,
            "set_aside_failed": self.set_aside_failed,
            "flushes": self.flushes,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_flush_ts": self.last_flush_ts,
            "days": {k: len(self.days(k)) for k in KEYS} if self.root.exists() else {},
        }


__all__ = ["HELD_ASIDE", "KEYS", "DailyArchive"]
