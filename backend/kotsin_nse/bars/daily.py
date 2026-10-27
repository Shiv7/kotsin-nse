"""The broker's daily candles as 1d bars.

5paisa's daily history returns the running session's candle as well (measured 2026-10-27 at a 09:25
restart: today's row stamped 09:14 for NIFTY and 09:15 for every stock, the open plus thirteen minutes,
shaped like a finished day). Seeded as complete it became "the previous day" for anyone taking the last
1d bar before the open — NIFTY's CPR that day was its own first 5m bar — and, at 09:15, it shadowed the
live 1d bar the aggregator builds for the day (the store keeps a non-partial bar over a partial one for
the same bucket). A row is a day only once its session has closed.
"""

from __future__ import annotations

from typing import Any

from ..domain import Instrument
from ..market.session import day_start_ts, ist_day, ist_naive_to_ts, session_close_ts
from .unified import BarSource, UnifiedBar


def daily_bars(inst: Instrument, rows: list[dict[str, Any]], *, now: float) -> list[UnifiedBar]:
    """Finished sessions only, each stamped at midnight IST of its day, source REST."""
    out: list[UnifiedBar] = []
    for r in rows:
        d = ist_day(ist_naive_to_ts(r["dt"]))
        if now < session_close_ts(inst.segment, d):
            continue  # that session is still running (or has not begun): not a day yet
        out.append(
            UnifiedBar(
                symbol=inst.symbol,
                scrip_code=inst.scrip_code,
                tf="1d",
                ts=int(day_start_ts(d)),
                open=float(r["o"]),
                high=float(r["h"]),
                low=float(r["l"]),
                close=float(r["c"]),
                volume=float(r["v"]),
                source=BarSource.REST,
                complete=True,
            )
        )
    return out


__all__ = ["daily_bars"]
