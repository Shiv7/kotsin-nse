"""The live archive: what the socket delivered, kept.

Option and future OI exists only on the socket; the REST history has none of it, so a session
that is not archived is a session that can never be replayed.

Per IST day, Parquet files under ``data/archive/<kind>/<day>.parquet``:

* ``bars`` — every closed 1m bar of every tracked underlying (symbol, ts, o/h/l/c/v, source, oi)
* ``oi`` — every OI frame for every subscribed code (futures and option strikes)
* ``ticks`` — every quote frame of the underlyings and their futures (``archive_ticks``), with the
  data source that sent it: the raw material for volume-at-price, and for judging a source's
  resolution after the fact

Compact (tens of MB a day), append-safe (a flush re-reads the day's file and de-duplicates on
the key), readable by the same pandas the research code uses. Raw frames are not kept: at ~19k
ticks a minute they would be a gigabyte a day for information the 1m bar already carries.
"""

from __future__ import annotations

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
    "ticks": ("source", "scrip_code", "recv_ts"),
}


class DailyArchive:
    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        self._rows: dict[str, dict[str, list[dict[str, Any]]]] = {k: defaultdict(list) for k in KEYS}
        self.rows_buffered = 0
        self.rows_written = 0
        self.files_written = 0
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

    def tick(self, t: Mapping[str, Any]) -> None:
        self._add(
            "ticks",
            float(t["recv_ts"]),
            {
                "source": str(t.get("source", "")),
                "scrip_code": str(t["scrip_code"]),
                "ts": float(t["ts"]),
                "recv_ts": float(t["recv_ts"]),
                "ltp": float(t["ltp"]),
                "total_qty": int(t["total_qty"]),
                "last_qty": int(t["last_qty"]),
                "bid": float(t["bid"]),
                "ask": float(t["ask"]),
                "bid_qty": int(t["bid_qty"]),
                "ask_qty": int(t["ask_qty"]),
            },
        )

    # -- persist ---------------------------------------------------------------------------------

    def flush(self, *, final: bool = False) -> int:
        """Write every buffered day. Blocking pandas I/O — call it from a worker thread."""
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
        d = self.root / kind
        return sorted(p.stem for p in d.glob("*.parquet")) if d.exists() else []

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rows_buffered": self.rows_buffered,
            "rows_written": self.rows_written,
            "files_written": self.files_written,
            "flushes": self.flushes,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_flush_ts": self.last_flush_ts,
            "days": {k: len(self.days(k)) for k in KEYS} if self.root.exists() else {},
        }


__all__ = ["KEYS", "DailyArchive"]
