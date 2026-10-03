"""Historical OHLCV, fetched once and cached as Parquet.

The broker's historical endpoint is the slowest thing this system touches and it rate-limits.
Research re-runs constantly, so the cache is the difference between a 40-minute sweep and a
4-second one. Cache files are keyed by ``symbol/tf`` and merged on overlap, so extending a range
re-fetches only the missing tail.

Timestamps are converted to UTC epoch seconds **once, here**, using the same
``market.session.ist_naive_to_ts`` the live path uses. A backtest that parsed the broker's naive
IST differently from the engine would be measuring a different instrument.

Intraday rows are stored ON THE SESSION GRID (``market/candles.py``), as live holds them: a candle
stamped at its first trade is its bucket, a pre-open or post-close row is no bar. The cache written
before 2026-10-03 held the raw stamps — 1,233 off-grid rows across 182 names and 393 post-close bars
that fed the backtest's indicators and never reach live; ``normalize`` repairs a cache in place.

Each symbol's segment is recorded (``segments.json``) so a backtest decides a name on its own
session: the five MCX names were replayed as NSE stocks, on NSE's grid and costs.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from ..config import Segment
from ..domain import Instrument
from ..market.candles import snap_candles
from ..market.session import ist_naive_to_ts, to_ist

log = structlog.get_logger(__name__)

COLUMNS = ["ts", "o", "h", "l", "c", "v"]


@dataclass(slots=True)
class HistoryStore:
    root: Path

    def path(self, symbol: str, tf: str) -> Path:
        return self.root / tf / f"{symbol.upper()}.parquet"

    def load(self, symbol: str, tf: str) -> pd.DataFrame:
        p = self.path(symbol, tf)
        if not p.exists():
            return pd.DataFrame(columns=COLUMNS)
        return pd.read_parquet(p)

    def save(self, symbol: str, tf: str, df: pd.DataFrame) -> None:
        p = self.path(symbol, tf)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True).to_parquet(
            p, index=False
        )

    def merge(self, symbol: str, tf: str, rows: list[dict[str, Any]], *, segment: Segment | None = None) -> int:
        if segment is not None:
            self.set_segment(symbol, segment)
            rows = snap_candles(rows, segment, tf).rows  # idempotent; 1d rows pass through
        if not rows:
            return 0
        fresh = pd.DataFrame(
            [
                {
                    "ts": int(ist_naive_to_ts(r["dt"])),
                    "o": r["o"],
                    "h": r["h"],
                    "l": r["l"],
                    "c": r["c"],
                    "v": r["v"],
                }
                for r in rows
            ]
        )
        existing = self.load(symbol, tf)
        merged = pd.concat([existing, fresh], ignore_index=True) if len(existing) else fresh
        self.save(symbol, tf, merged)
        return len(merged)

    def coverage(self, symbol: str, tf: str) -> tuple[int, int] | None:
        df = self.load(symbol, tf)
        if df.empty:
            return None
        return int(df["ts"].min()), int(df["ts"].max())

    def symbols(self, tf: str) -> list[str]:
        d = self.root / tf
        return sorted(p.stem for p in d.glob("*.parquet")) if d.exists() else []

    # -- each symbol's segment ------------------------------------------------------------------------

    def _segments_path(self) -> Path:
        return self.root / "segments.json"

    def segments(self) -> dict[str, str]:
        p = self._segments_path()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            return {}

    def segment_of(self, symbol: str) -> Segment | None:
        name = self.segments().get(symbol.upper())
        return Segment[name] if name in Segment.__members__ else None

    def set_segment(self, symbol: str, segment: Segment) -> None:
        held = self.segments()
        if held.get(symbol.upper()) == segment.name:
            return
        held[symbol.upper()] = segment.name
        self.root.mkdir(parents=True, exist_ok=True)
        self._segments_path().write_text(json.dumps(held, indent=1, sort_keys=True), encoding="utf-8")

    # -- repairing a cache written before the grid ----------------------------------------------------

    def normalize(self, symbol: str, tf: str, segment: Segment) -> dict[str, int]:
        """Put one cached intraday series on the session grid, in place, and record its segment.
        Returns the rows before and after, and how many were moved or dropped."""
        self.set_segment(symbol, segment)
        df = self.load(symbol, tf)
        if df.empty or tf == "1d":
            return {"before": len(df), "after": len(df), "moved": 0, "dropped": 0}
        rows = [{"dt": to_ist(int(r.ts)).strftime("%Y-%m-%dT%H:%M:%S"), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v}
                for r in df.itertuples()]
        snapped = snap_candles(rows, segment, tf)
        fresh = pd.DataFrame([{"ts": int(ist_naive_to_ts(r["dt"])), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}
                              for r in snapped.rows], columns=COLUMNS)
        self.save(symbol, tf, fresh)
        return {"before": len(df), "after": len(fresh), "moved": snapped.moved, "dropped": len(snapped.dropped)}


def guess_segment(df: pd.DataFrame) -> Segment:
    """For a cache that never recorded it: a series with bars after 17:00 IST is MCX's; anything else
    is NSE cash. Only ``normalize-history`` uses it, once, for files written before segments.json."""
    if not df.empty and any(to_ist(int(t)).hour >= 17 for t in df["ts"]):
        return Segment.MCX_FO
    return Segment.NSE_EQ


async def fetch(
    rest: Any,
    store: HistoryStore,
    instruments: list[Instrument],
    *,
    tfs: tuple[str, ...] = ("30m", "1d"),
    start: date,
    end: date,
    chunk_days: int = 60,
    pause_s: float = 0.2,
) -> dict[str, int]:
    """Fill the cache. Requests are chunked and paced — the historical endpoint is the one place
    where hammering the broker reliably earns a retry storm."""
    out: dict[str, int] = {}
    for inst in instruments:
        for tf in tfs:
            total = 0
            cursor = start
            span = timedelta(days=chunk_days if tf != "1d" else 365)
            while cursor <= end:
                stop = min(cursor + span, end)
                try:
                    rows = await rest.candles(inst, tf, cursor.isoformat(), stop.isoformat())
                    total = store.merge(inst.symbol, tf, rows, segment=inst.segment)
                except Exception as exc:  # noqa: BLE001 - one bad window must not stop the sweep
                    log.warning(
                        "history.chunk_failed",
                        symbol=inst.symbol,
                        tf=tf,
                        start=cursor.isoformat(),
                        error=str(exc),
                    )
                cursor = stop + timedelta(days=1)
                await asyncio.sleep(pause_s)
            out[f"{inst.symbol}:{tf}"] = total
            log.info("history.cached", symbol=inst.symbol, tf=tf, bars=total)
    return out
