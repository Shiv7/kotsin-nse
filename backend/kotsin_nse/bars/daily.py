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
from ..market.session import daily_final_ts, day_start_ts, ist_day, ist_naive_to_ts
from .unified import BarSource, UnifiedBar


def daily_bars(inst: Instrument, rows: list[dict[str, Any]], *, now: float) -> list[UnifiedBar]:
    """Finished sessions only, each stamped at midnight IST of its day, source REST.

    "Finished" is the segment's ``daily_final`` (15:45 on NSE), not its ``close``: the cash close is
    set by an auction that settles after 15:30 and F&O trades to 15:40, so a boot between 15:30 and
    15:45 would otherwise seed a candle still being written as a complete day — the 27 Oct 2026
    regression, moved fifteen minutes later.
    """
    out: list[UnifiedBar] = []
    for r in rows:
        d = ist_day(ist_naive_to_ts(r["dt"]))
        if now < daily_final_ts(inst.segment, d):
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
