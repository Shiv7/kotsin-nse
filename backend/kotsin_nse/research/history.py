"""Historical OHLCV, fetched once and cached as Parquet.

The broker's historical endpoint is the slowest thing this system touches and it rate-limits.
Research re-runs constantly, so the cache is the difference between a 40-minute sweep and a
4-second one. Cache files are keyed by ``symbol/tf`` and merged on overlap, so extending a range
re-fetches only the missing tail.

Timestamps are converted to UTC epoch seconds **once, here**, using the same
``market.session.ist_naive_to_ts`` the live path uses. A backtest that parsed the broker's naive
IST differently from the engine would be measuring a different instrument.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from ..domain import Instrument
from ..market.session import ist_at, ist_naive_to_ts

log = structlog.get_logger(__name__)

COLUMNS = ["ts", "o", "h", "l", "c", "v"]

#: The broker's 1m history covers 09:15 to 15:14 IST only (measured 2026-10-18 on 5 stocks × 3
#: days: 361 bars ending 15:15 with a near-empty 15:15 print, the last 15 minutes of trading absent).
#: The LATEST session is served provisional — after 15:14 it is a few lumped bars (RELIANCE 16 Oct:
#: 15:14 = 8.3M shares, 15:28 = 16.8M at the close, 40 % of the day) until it is final. So every
#: reader of cached 1m bars uses 09:15 ≤ start < 15:14 and nothing later.
SESSION_FIRST_1M = "09:15"
SESSION_LAST_1M_EXCL = "15:14"


def session_rows(m1: pd.DataFrame, day: Any) -> pd.DataFrame:
    """The day's trustworthy cached 1m rows (see ``SESSION_LAST_1M_EXCL``), oldest first."""
    lo, hi = ist_at(day, SESSION_FIRST_1M), ist_at(day, SESSION_LAST_1M_EXCL)
    return m1[(m1["ts"] >= lo) & (m1["ts"] < hi)].sort_values("ts")


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

    def merge(self, symbol: str, tf: str, rows: list[dict[str, Any]]) -> int:
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


#: calendar days per request, by timeframe. 1m returns ~375 rows a session, so a window is kept
#: to a few thousand rows; daily asks for a year at a time.
CHUNK_DAYS = {"1m": 20, "3m": 40, "5m": 60, "1d": 365}

#: Measured 2026-10-18: the historical endpoint does not return the LATEST session unless the
#: requested end runs past it — ``from=2026-10-16&end=2026-10-17`` (Fri→Sat) came back empty,
#: ``end=2026-10-18`` returned all 371 candles, while an older Friday→Saturday window was fine.
#: So every request's end is padded; the overlap is de-duplicated by ``merge``.
END_PAD_DAYS = 2


async def fetch(
    rest: Any,
    store: HistoryStore,
    instruments: list[Instrument],
    *,
    tfs: tuple[str, ...] = ("30m", "1d"),
    start: date,
    end: date,
    pause_s: float = 0.2,
    on_done: Any = None,
) -> dict[str, int]:
    """Fill the cache. Requests are chunked and paced — the historical endpoint is the one place
    where hammering the broker reliably earns a retry storm."""
    out: dict[str, int] = {}
    for n, inst in enumerate(instruments, 1):
        for tf in tfs:
            total = 0
            cursor = start
            span = timedelta(days=CHUNK_DAYS.get(tf, 60))
            while cursor <= end:
                stop = min(cursor + span, end)
                ask_to = stop + timedelta(days=END_PAD_DAYS)
                try:
                    rows = await rest.candles(inst, tf, cursor.isoformat(), ask_to.isoformat())
                    total = store.merge(inst.symbol, tf, rows)
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
            log.info("history.cached", symbol=inst.symbol, tf=tf, bars=total, n=n, of=len(instruments))
        if on_done is not None:
            on_done(inst, n, len(instruments))
    return out
