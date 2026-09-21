"""Per-instrument, per-timeframe ring of finished bars, plus the one forming bar.

Deliberately dumb: a dict of lists with a cap. It exists so that "the last N bars of RELIANCE 30m"
is one call with one meaning, and so a strategy cannot accidentally read the *forming* bar as if it
were closed — which is the difference between a backtest and a lie.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .unified import BarSource, UnifiedBar


class BarStore:
    def __init__(self, max_bars: int = 1500) -> None:
        self.max_bars = max_bars
        self._closed: dict[tuple[str, str], list[UnifiedBar]] = defaultdict(list)
        self._forming: dict[tuple[str, str], UnifiedBar] = {}

    # -- writes -----------------------------------------------------------------------------------

    def set_forming(self, bar: UnifiedBar) -> None:
        self._forming[(bar.symbol, bar.tf)] = bar

    def close(self, bar: UnifiedBar) -> UnifiedBar:
        bar.complete = True
        key = (bar.symbol, bar.tf)
        series = self._closed[key]
        if series and series[-1].ts == bar.ts:
            # A REST backfill may overwrite a partial bar for the same bucket — but never the
            # other way round. On a mid-session start the REST bar lands first and the bucket we
            # were mid-way through when the feed connected closes *after* it; letting that PARTIAL
            # overwrite the broker's own candle replaces a correct bar with one missing every tick
            # before connect, and stamps it complete=True for the strategies to read.
            if not (series[-1].source is not BarSource.PARTIAL and bar.source is BarSource.PARTIAL):
                series[-1] = bar
        else:
            series.append(bar)
            if len(series) > self.max_bars:
                del series[: len(series) - self.max_bars]
        self._forming.pop(key, None)
        return bar

    def seed(self, symbol: str, tf: str, bars: list[UnifiedBar]) -> int:
        """Replace a series wholesale from a backfill. Returns how many bars are held after."""
        key = (symbol, tf)
        merged = {b.ts: b for b in bars}
        for existing in self._closed.get(key, []):
            merged.setdefault(existing.ts, existing)
        series = [merged[ts] for ts in sorted(merged)]
        self._closed[key] = series[-self.max_bars :]
        return len(self._closed[key])

    # -- reads ------------------------------------------------------------------------------------

    def bars(self, symbol: str, tf: str, n: int | None = None) -> list[UnifiedBar]:
        series = self._closed.get((symbol, tf), [])
        return series[-n:] if n else list(series)

    def last(self, symbol: str, tf: str) -> UnifiedBar | None:
        series = self._closed.get((symbol, tf), [])
        return series[-1] if series else None

    def forming(self, symbol: str, tf: str) -> UnifiedBar | None:
        return self._forming.get((symbol, tf))

    def count(self, symbol: str, tf: str) -> int:
        return len(self._closed.get((symbol, tf), []))

    def symbols(self, tf: str) -> list[str]:
        return sorted({sym for sym, t in self._closed if t == tf})

    def stats(self) -> dict[str, Any]:
        return {
            "series": len(self._closed),
            "bars": sum(len(v) for v in self._closed.values()),
            "forming": len(self._forming),
        }
