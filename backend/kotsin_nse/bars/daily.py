"""The daily series the pivots are built from — and what it takes to trust it after a restart.

Everything on the pivot side of the engine (``Engine.zones_for``, the leg ladders, the CLI report)
reads one input: the previous completed session's official bar, plus the completed weeks and months
aggregated from the same daily series. This module owns the two questions that decide whether that
input can be trusted:

* **Is the bar official?** Only the broker's own daily candle (``BarSource.REST``) is. It matches NSE
  bhavcopy to the paisa. A daily bar the aggregator built from ticks carries the last print as its
  close rather than the exchange's closing price; a bar rolled up from intraday candles is worse —
  5paisa's intraday candles stop at 15:15. The old stack computed its daily and weekly pivots on
  exactly that, and every level inherited the error (22-Sep-2026 RELIANCE: close 1244.50 against the
  official 1240.40; 18-Sep: low 1238.30 against 1226.40).

* **Is it the right session?** The previous *trading* day, not the previous calendar day. Holidays
  come from ``data/holidays.txt``; when that file is incomplete the market itself is the calendar —
  the latest previous-session date that every name agrees on is a day the exchange was open.

The cache exists for one corner: a restart while the broker's historical endpoint is down. The
official candles from the last successful refresh are on disk, so the engine resumes on real levels
immediately, and the repair loop replaces them the moment REST answers — ``BarStore.seed`` lets a
fresh REST bar win over a same-day cached one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..market.session import TradingCalendar, ist_day
from .unified import BarSource, UnifiedBar

#: ``Engine.zones_for``'s floor: a month of sessions, so the monthly ladder has a completed month.
MIN_DAILY_BARS = 25
#: Names refetched per repair pass. At the backfill's 0.15 s spacing this is ~15 s of REST, so a
#: pass finishes well inside the interval and never competes with the boot backfill for long.
REPAIR_BATCH = 80


def is_official(bar: UnifiedBar) -> bool:
    """The broker's own completed daily candle — the only source the pivots accept."""
    return bar.source is BarSource.REST and bar.complete


def previous_session(bars: Iterable[UnifiedBar], today: date) -> UnifiedBar | None:
    """The last bar strictly before ``today``. Today's own bar can never set today's levels."""
    prior = [b for b in bars if ist_day(b.ts) < today]
    return prior[-1] if prior else None


@dataclass(slots=True)
class DailyAudit:
    today: date
    #: the session the previous-session bar should come from, per the holiday calendar
    expected_prev: date | None
    #: the latest previous-session date any name actually holds — the market's own calendar
    consensus_prev: date | None
    ok: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # no bar before today at all
    unofficial: list[str] = field(default_factory=list)  # previous bar built from ticks, not REST
    stale: list[str] = field(default_factory=list)  # previous bar older than expected_prev
    short: list[str] = field(default_factory=list)  # fewer than MIN_DAILY_BARS
    #: no candle of any kind at the broker — a listed contract nobody trades (COTTON, KAPAS,
    #: MCXBULLDEX…). Reported, never a fault: there is no session to be missing from.
    dormant: list[str] = field(default_factory=list)

    @property
    def holiday_suspected(self) -> bool:
        """Every name's latest session predates the calendar's expectation: the calendar thinks
        the exchange was open on a day no instrument traded. That is a holiday missing from
        ``data/holidays.txt``, not two hundred simultaneous data failures."""
        return (
            self.expected_prev is not None
            and self.consensus_prev is not None
            and self.consensus_prev < self.expected_prev
            and not self.ok
            and not self.missing
            and not self.unofficial
        )

    @property
    def needs_refresh(self) -> list[str]:
        bad = set(self.missing) | set(self.unofficial) | set(self.short)
        if not self.holiday_suspected:
            bad |= set(self.stale)
        return sorted(bad)

    @property
    def ready(self) -> bool:
        return not self.needs_refresh

    def summary(self) -> str:
        total = len(self.ok) + len(self.missing) + len(self.unofficial) + len(self.stale) + len(self.short)
        parts = [f"{len(self.ok)}/{total} names on the official previous session"]
        if self.dormant:
            parts.append(f"{len(self.dormant)} dormant (no candles at the broker)")
        if self.missing:
            parts.append(f"{len(self.missing)} missing")
        if self.unofficial:
            parts.append(f"{len(self.unofficial)} unofficial (tick-built)")
        if self.stale:
            parts.append(
                f"{len(self.stale)} stale"
                + (" — holiday not in data/holidays.txt?" if self.holiday_suspected else "")
            )
        if self.short:
            parts.append(f"{len(self.short)} under {MIN_DAILY_BARS} bars")
        return "; ".join(parts)


def audit(
    series: Mapping[str, list[UnifiedBar]], today: date, calendar: TradingCalendar
) -> DailyAudit:
    """Classify every name's daily series by whether its previous-session bar can carry a pivot."""
    expected = calendar.previous_trading_day(today)
    prevs = {sym: previous_session(bars, today) for sym, bars in series.items()}
    consensus = max((ist_day(p.ts) for p in prevs.values() if p is not None), default=None)
    out = DailyAudit(today=today, expected_prev=expected, consensus_prev=consensus)
    for sym, bars in series.items():
        prev = prevs[sym]
        if not bars:
            out.dormant.append(sym)
        elif prev is None:
            out.missing.append(sym)
        elif not is_official(prev):
            out.unofficial.append(sym)
        elif ist_day(prev.ts) < expected:
            out.stale.append(sym)
        elif len(bars) < MIN_DAILY_BARS:
            out.short.append(sym)
        else:
            out.ok.append(sym)
    return out


class DailyCache:
    """Official daily candles on disk, one JSON file per symbol, so a boot never depends on the
    broker's historical endpoint being up at that moment."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.json"

    def save(self, symbol: str, bars: Iterable[UnifiedBar]) -> int:
        """Persist the official bars only; a tick-built bar cached today would be served as
        official tomorrow. Written atomically so a crash mid-write leaves the previous file."""
        rows = [[int(b.ts), b.open, b.high, b.low, b.close, b.volume] for b in bars if is_official(b)]
        if not rows:
            return 0
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path(symbol)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, separators=(",", ":")))
        tmp.replace(target)
        return len(rows)

    def load(self, symbol: str, scrip_code: str) -> list[UnifiedBar]:
        """The cached series, or nothing. A corrupt or missing file is an empty series, never an
        exception: the cache is a fallback, and a fallback that can fail the boot is not one."""
        try:
            rows = json.loads(self.path(symbol).read_text())
        except (OSError, ValueError):
            return []
        out: list[UnifiedBar] = []
        for r in rows if isinstance(rows, list) else []:
            if not (isinstance(r, list) and len(r) == 6):
                continue
            out.append(
                UnifiedBar(
                    symbol=symbol, scrip_code=scrip_code, tf="1d", ts=float(r[0]),
                    open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                    volume=float(r[5]), source=BarSource.REST, complete=True,
                )
            )
        return out
