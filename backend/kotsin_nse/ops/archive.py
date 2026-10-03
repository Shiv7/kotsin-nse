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
import threading
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
#: today's flushes: ``<kind>/<day>.part-<ns>.parquet``, compacted into ``<kind>/<day>.parquet`` once the
#: day is over (or at the final flush). Rewriting the whole day file at every flush re-read the day so
#: far each time — a 2.4M-row OI day grew a flush from 60 ms to 1.3 s, ~50 s of CPU a day (review,
#: 2026-10-03).
_PART = re.compile(r"^(\d{4}-\d{2}-\d{2})\.part-\d+$")


def day_paths(root: Path, kind: str, day: str) -> list[Path]:
    """A stream's files for one day: the compacted day file first, then today's parts in order."""
    d = root / kind
    main = d / f"{day}.parquet"
    # never a part still being written (its temp file matches the glob too)
    parts = sorted(p for p in d.glob(f"{day}.part-*.parquet") if not p.name.endswith(".tmp.parquet")) if d.exists() else []
    return ([main] if main.exists() else []) + parts


def read_day_frame(root: Path, kind: str, day: str, columns: list[str] | None = None, *, strict: bool = False) -> pd.DataFrame:
    """One day of a stream as one frame — the day file and any parts, de-duplicated on the stream's
    key (the last write wins). Every reader goes through here, so a crash mid-day that left parts
    behind reads the same as a compacted day. Empty when there is nothing."""
    frames = []
    for path in day_paths(root, kind, day):
        try:
            frames.append(pd.read_parquet(path, columns=columns))
        except (OSError, ValueError) as exc:
            if strict:
                # the archive's own compaction and set-aside: an unreadable file is never
                # overwritten or deleted on the strength of what could be read around it
                raise
            log.warning("archive.unreadable", path=str(path), error=str(exc)[:120])
    if not frames:
        return pd.DataFrame(columns=columns or list(KEYS.get(kind, ())))
    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    keys = [k for k in KEYS.get(kind, ()) if k in df.columns]
    if keys and len(frames) > 1:
        df = df.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
    return df


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
        #: ``flush`` is dispatched to a worker thread from two places — the housekeeping loop and
        #: ``Engine.stop`` — and ``stop`` does not cancel housekeeping before its final flush, so
        #: the two can run at once. They share the buffer and ``_write``'s per-day temp path.
        self._lock = threading.Lock()

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

    def oi(
        self,
        scrip_code: str,
        ts: float,
        oi: float,
        change_pct: float | None,
        *,
        change: float | None = None,
        tick_ts: float | None = None,
        ltp: float | None = None,
        volume: float | None = None,
    ) -> None:
        """One OI frame, every field the broker sent (operator, 2026-10-03: keep the raw frame).
        ``change`` and ``change_pct`` are 5paisa's own fields, kept to be checked, never used: the
        percent is 0.0 on every frame. ``tick_ts`` is the broker's time, ``ts`` our arrival."""
        self._add(
            "oi",
            ts,
            {"scrip_code": str(scrip_code), "ts": float(ts), "oi": float(oi), "change_pct": change_pct,
             "change": change, "tick_ts": tick_ts, "ltp": ltp, "volume": volume},
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
        with self._lock:
            return self._flush(final=final)

    def _flush(self, *, final: bool) -> int:
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
        self._compact_finished_days(final=final)
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
        """Today's rows go to a new part file — nothing is re-read; a past day's rows (a late
        flush, a set-aside) are merged into its day file at once."""
        if day == ist_day(time.time()).isoformat():
            d = self.root / kind
            d.mkdir(parents=True, exist_ok=True)
            part = d / f"{day}.part-{time.time_ns()}.parquet"
            tmp = part.with_name(part.name.replace(".parquet", ".tmp.parquet"))
            pd.DataFrame(rows).to_parquet(tmp, index=False)
            tmp.replace(part)
            self.rows_written += len(rows)
            self.files_written += 1
            return len(rows)
        return self._compact(kind, day, rows)

    def _compact(self, kind: str, day: str, rows: list[dict[str, Any]] | None = None) -> int:
        """Fold a day's parts (and ``rows``) into its day file, then drop the parts."""
        path = self.root / kind / f"{day}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        parts = [p for p in day_paths(self.root, kind, day) if p != path]
        frames = [read_day_frame(self.root, kind, day, strict=True)] if (parts or path.exists()) else []
        if rows:
            frames.append(pd.DataFrame(rows))
        frames = [f for f in frames if len(f)]
        if not frames:
            return 0
        fresh = pd.concat(frames, ignore_index=True)
        keys = list(KEYS[kind])
        fresh = fresh.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
        tmp = path.with_suffix(".tmp.parquet")
        fresh.to_parquet(tmp, index=False)
        tmp.replace(path)
        for p in parts:
            p.unlink(missing_ok=True)
        n = len(rows or [])
        self.rows_written += n
        self.files_written += 1
        return n

    def read_day(self, kind: str, day: str, columns: list[str] | None = None) -> pd.DataFrame:
        return read_day_frame(self.root, kind, day, columns)

    def _compact_finished_days(self, *, final: bool) -> None:
        """Every day with parts that is over — and at the final flush, today's too."""
        today = ist_day(time.time()).isoformat()
        for kind in KEYS:
            d = self.root / kind
            if not d.exists():
                continue
            pending = {m.group(1) for p in d.glob("*.part-*.parquet") if (m := _PART.match(p.name.removesuffix(".parquet")))}
            for day in sorted(pending):
                if final or day < today:
                    try:
                        self._compact(kind, day)
                    except Exception as exc:  # noqa: BLE001 - the parts stay and are read as they are
                        self.errors += 1
                        self.last_error = f"compact {kind}/{day}: {exc}"[:200]
                        log.warning("archive.compact_failed", kind=kind, day=day, error=str(exc))

    def days(self, kind: str) -> list[str]:
        """The day files a stream holds, oldest first. A ``*.tmp.parquet`` left by a crash is not
        a day and is never counted as one."""
        d = self.root / kind
        if not d.exists():
            return []
        out = {p.stem for p in d.glob("*.parquet") if _DAY.match(p.stem)}
        out |= {m.group(1) for p in d.glob("*.part-*.parquet") if (m := _PART.match(p.name.removesuffix(".parquet")))}
        return sorted(out)

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
                for p in day_paths(self.root, kind, day):
                    p.unlink(missing_ok=True)
                removed[kind] = removed.get(kind, 0) + 1
                self.files_pruned += 1
            # Only temp files for days that are themselves gone: a live ``_write`` uses a fixed
            # temp path per stream and day, and sweeping the newest one out from under it turns
            # that flush's rows into a re-buffer that a shutdown never retries.
            d = self.root / kind
            newest = days[-1] if days else ""
            for tmp in d.glob("*.tmp.parquet") if d.exists() else ():
                if tmp.name.removesuffix(".tmp.parquet") < newest:
                    tmp.unlink(missing_ok=True)
        if removed:
            log.info("archive.pruned", files=removed, keep_tape=self.keep_sessions)
        return removed

    def _set_aside(self, kind: str, aside: str, day: str) -> bool:
        """Move ``day``'s held rows into the longer-lived stream. True when the source may now be
        deleted — including when there was genuinely nothing to preserve. False means the rows are
        still only in the source file, so the caller must keep it."""
        try:
            df = read_day_frame(self.root, kind, day, strict=True)
        except (OSError, ValueError) as exc:
            log.warning("archive.set_aside_unreadable", kind=kind, day=day, error=str(exc)[:120])
            return False
        if "held" not in df.columns or "scrip_code" not in df.columns:
            return True
        # Every row of every code that was held at ANY point in the day, not only the rows stamped
        # held. The tape is change-compressed and the reader forward-fills, so the row in force at
        # the entry second is usually the one written before the position existed; keeping only the
        # stamped rows would leave the start of the hold — and an illiquid contract's whole hold —
        # with nothing to forward-fill from once the day left the tape's own window.
        held_codes = set(df.loc[df["held"].astype(bool), "scrip_code"])
        if not held_codes:
            return True
        rows = df[df["scrip_code"].isin(held_codes)]
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


__all__ = ["HELD_ASIDE", "KEYS", "DailyArchive", "day_paths", "read_day_frame"]
