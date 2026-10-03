"""What the pivot zones look like at a given width and wall threshold — a measuring tool for the
operator's calibration, not a calibration.

The cluster width (``k`` in ``k × ATR30 / price``) and the wall threshold (``WALL_MIN_STRENGTH``) were
left where they were when Fibonacci left the confluence engine (6ff868b, 23 Sep), with the note that
the threshold was "worth re-reading against the new distribution". Nobody did: 88 % of live zones are
single levels and 29 % of triggers grade F "no wall ahead" (6 % before) — review, 2026-10-03. This
reads the distribution for any ``k`` and threshold off the cached history, with the same builder live
and the backtest use (``bars/zones.py``).
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import date

from ..bars.indicators import atr
from ..bars.unified import BarSource, UnifiedBar
from ..bars.zones import ATR_BARS, ATR_PERIOD, ZoneBuild, build_zones
from ..config import Segment
from ..market.session import ist_day
from .history import HistoryStore


def _bars(store: HistoryStore, symbol: str, tf: str) -> list[UnifiedBar]:
    df = store.load(symbol, tf)
    return [UnifiedBar(symbol=symbol, scrip_code=symbol, tf=tf, ts=int(r.ts), open=float(r.o), high=float(r.h),
                       low=float(r.l), close=float(r.c), volume=float(r.v), source=BarSource.REST, complete=True)
            for r in df.itertuples()]


def zones_on(store: HistoryStore, symbol: str, day: date, *, k: float) -> ZoneBuild:
    seg = store.segment_of(symbol) or Segment.NSE_EQ
    return build_zones(_bars(store, symbol, "1d"), _bars(store, symbol, "30m"), day, k=k, segment=seg)


@dataclass(slots=True)
class Distribution:
    symbol_days: int = 0
    refused: int = 0
    zones_per_day: float = 0.0
    single_level_share: float = 0.0
    walls_per_day: float = 0.0
    wall_within_3atr_above: float = 0.0
    wall_within_3atr_below: float = 0.0
    no_wall_above: float = 0.0
    median_tolerance_pct: float = 0.0

    def to_json(self) -> dict[str, float | int]:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(self).items()}


def distribution(store: HistoryStore, *, k: float, wall: float, sessions: int = 60,
                 symbols: list[str] | None = None) -> Distribution:
    """Over the last ``sessions`` days of every cached name (or ``symbols``)."""
    out = Distribution()
    zones_n: list[int] = []
    singles = members = 0
    walls_n: list[int] = []
    above = below = none_above = 0
    tols: list[float] = []
    for sym in symbols or store.symbols("30m"):
        dailies = _bars(store, sym, "1d")
        intraday = _bars(store, sym, "30m")
        seg = store.segment_of(sym) or Segment.NSE_EQ
        days = sorted({ist_day(b.ts) for b in intraday})[-sessions:]
        for d in days:
            built = build_zones(dailies, intraday, d, k=k, segment=seg)
            out.symbol_days += 1
            if built.refused:
                out.refused += 1
                continue
            prior = [b for b in intraday if ist_day(b.ts) < d][-ATR_BARS:]
            a = atr(prior, ATR_PERIOD) if prior else None
            if not prior or not a:
                continue
            close = prior[-1].close
            ws = [z for z in built.zones if z.strength >= wall]
            zones_n.append(len(built.zones))
            singles += sum(1 for z in built.zones if z.count == 1)
            members += len(built.zones)
            walls_n.append(len(ws))
            up = [z.price for z in ws if z.price > close]
            dn = [z.price for z in ws if z.price < close]
            above += any(p - close <= 3 * a for p in up)
            below += any(close - p <= 3 * a for p in dn)
            none_above += not up
            tols.append(built.tolerance_pct)
    n = len(zones_n)
    if n:
        out.zones_per_day = statistics.fmean(zones_n)
        out.single_level_share = singles / members if members else 0.0
        out.walls_per_day = statistics.fmean(walls_n)
        out.wall_within_3atr_above = above / n
        out.wall_within_3atr_below = below / n
        out.no_wall_above = none_above / n
        out.median_tolerance_pct = statistics.median(tols)
    return out


__all__ = ["Distribution", "distribution", "zones_on"]
