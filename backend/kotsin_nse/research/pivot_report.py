"""The pivot ladders in force on a given date, rebuilt from the broker's own daily candles.

``kotsin-nse pivots RELIANCE --for 2026-09-23`` answers "what levels did the engine hold on that
day, and from which bars" — for the underlying (daily, weekly, monthly), the front future (daily)
and the OTM strikes the book would trade (daily and weekly). No engine, no feed, no store: one
REST call per leg to ``V2/historical`` — the same endpoint the boot backfill and
``LegPivotLoader`` use — so what prints here is what the engine would have computed, not a second
implementation that could drift from it.

Every level comes from the engine's own functions: ``classic_pivots`` for the math,
``periods.weekly/monthly/previous_complete`` for the completed week and month, and
``levels_from_candles`` for the previous session with the thin-bar guard that the legs use live.

**Two conventions for R3/S3/R4/S4.** ``classic_pivots`` follows Kite (``P ± 2·range``) on purpose —
``tests/test_pivots.py`` records why. The old stack's pivot API (``scripFinder`` → FA
``InsideCPRLogic``) uses the floor-trader form ``H + 2(P−L)`` / ``L − 2(H−P)``. Pivot, R1/S1, R2/S2,
Fibonacci, Camarilla and CPR are identical in both. The report prints both outer sets side by
side so a number on the dashboard can be matched to a number here without guessing which
convention produced it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..bars.periods import PeriodOHLC, monthly, previous_complete, weekly
from ..bars.pivots import PivotLevels, classic_pivots
from ..bars.unified import UnifiedBar
from ..domain import Instrument, OptionType
from ..instrument.legs import (
    MIN_PREV_SESSION_VOLUME,
    STRIKES_PER_SIDE,
    levels_from_candles,
    otm_legs,
)
from ..market.session import ist_day, ist_naive_to_ts

#: Calendar days of daily candles requested. The previous completed month can start up to ~62
#: days before a date early in the following month; the rest is slack for holidays.
LOOKBACK_DAYS = 70
#: Mirrors the universe policy: an expiry closer than this is all theta, so the book trades the
#: next one and the report follows it.
MIN_DAYS_TO_EXPIRY = 2
CONCURRENCY = 6


@dataclass(frozen=True, slots=True)
class FloorOuter:
    """The floor-trader outer rungs, for matching against the old stack's pivot API."""

    r3: float
    r4: float
    s3: float
    s4: float


def floor_outer(high: float, low: float, close: float) -> FloorOuter:
    p = (high + low + close) / 3.0
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    return FloorOuter(r3=r3, r4=r3 + (high - low) * 0.5, s3=s3, s4=s3 - (high - low) * 0.5)


@dataclass(slots=True)
class Ladder:
    tf: str  # 1d | 1wk | 1mo
    source: str  # the session or period the levels came from
    sessions: int
    high: float
    low: float
    close: float
    volume: float | None
    levels: PivotLevels
    floor: FloorOuter


@dataclass(slots=True)
class LegReport:
    instrument: Instrument
    ladders: list[Ladder] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class Report:
    symbol: str
    for_date: date
    underlying: LegReport | None
    future: LegReport | None
    options: list[LegReport]
    spot_close: float | None  # the underlying's previous close, which picked the OTM strikes


def _bars(inst: Instrument, rows: list[dict[str, Any]]) -> list[UnifiedBar]:
    return [
        UnifiedBar(
            symbol=inst.symbol, scrip_code=inst.scrip_code, tf="1d", ts=ist_naive_to_ts(str(r["dt"])),
            open=r["o"], high=r["h"], low=r["l"], close=r["c"], volume=r["v"],
        )
        for r in rows
    ]


def _period_ladder(tf: str, p: PeriodOHLC, bars: list[UnifiedBar]) -> Ladder | None:
    lv = classic_pivots(p.high, p.low, p.close)
    if lv is None:
        return None
    sessions = sum(1 for b in bars if p.start <= ist_day(b.ts) <= p.end)
    return Ladder(
        tf=tf, source=f"{p.start}..{p.end}", sessions=sessions, high=p.high, low=p.low,
        close=p.close, volume=p.volume, levels=lv, floor=floor_outer(p.high, p.low, p.close),
    )


def ladders_from_rows(
    inst: Instrument, rows: list[dict[str, Any]], for_date: date, *, tfs: tuple[str, ...],
    min_volume: float,
) -> LegReport:
    """Daily from the previous completed session (with the legs' thin-bar guard), weekly and
    monthly from the previous completed period — exactly the engine's selection."""
    out = LegReport(instrument=inst)
    if "1d" in tfs:
        got = levels_from_candles(rows, for_date, min_volume=min_volume)
        if got is None:
            out.note = "no usable previous session (missing, too thin, or zero range)"
        else:
            lv, session, close, vol = got
            src = next(r for r in rows if str(r["dt"])[:10] == session)
            out.ladders.append(Ladder(
                tf="1d", source=session, sessions=1, high=src["h"], low=src["l"], close=close,
                volume=vol, levels=lv, floor=floor_outer(src["h"], src["l"], close),
            ))
    bars = _bars(inst, rows)
    for tf, periods in (("1wk", weekly(bars)), ("1mo", monthly(bars))):
        if tf not in tfs:
            continue
        p = previous_complete(periods, for_date)
        if p is None:
            continue
        ladder = _period_ladder(tf, p, bars)
        if ladder is not None:
            out.ladders.append(ladder)
    return out


async def build(symbol: str, for_date: date, *, catalogue: Any, rest: Any, per_side: int = STRIKES_PER_SIDE) -> Report:
    symbol = symbol.upper()
    underlying = catalogue.equity(symbol) or catalogue.front_future(symbol, on=for_date)
    if underlying is None:
        raise SystemExit(f"{symbol} is not in the scrip master")
    future = catalogue.front_future(symbol, on=for_date)
    start, end = (for_date - timedelta(days=LOOKBACK_DAYS)).isoformat(), for_date.isoformat()
    sem = asyncio.Semaphore(CONCURRENCY)

    async def rows_for(inst: Instrument) -> list[dict[str, Any]]:
        async with sem:
            try:
                return await rest.candles(inst, "1d", start, end)
            except Exception:  # noqa: BLE001 - one missing leg must not stop the report
                return []

    u_rows = await rows_for(underlying)
    u_rep = ladders_from_rows(underlying, u_rows, for_date, tfs=("1d", "1wk", "1mo"), min_volume=0)
    daily = next((ld for ld in u_rep.ladders if ld.tf == "1d"), None)
    spot = daily.close if daily else None

    f_rep = None
    if future is not None and future.scrip_code != underlying.scrip_code:
        f_rep = ladders_from_rows(future, await rows_for(future), for_date, tfs=("1d",), min_volume=0)

    legs: list[Instrument] = []
    if spot:
        expiries = [
            e for e in catalogue.expiries(symbol, on=for_date)
            if (date.fromisoformat(e) - for_date).days >= MIN_DAYS_TO_EXPIRY
        ]
        if expiries:
            chain = [*catalogue.chain(symbol, expiries[0], OptionType.CE), *catalogue.chain(symbol, expiries[0], OptionType.PE)]
            legs = otm_legs(chain=chain, spot=spot, per_side=per_side)
    o_rows = await asyncio.gather(*(rows_for(o) for o in legs))
    options = [
        ladders_from_rows(o, r, for_date, tfs=("1d", "1wk"), min_volume=MIN_PREV_SESSION_VOLUME)
        for o, r in zip(legs, o_rows, strict=True)
    ]
    return Report(symbol=symbol, for_date=for_date, underlying=u_rep, future=f_rep, options=options, spot_close=spot)


def render(rep: Report) -> str:
    lines: list[str] = []
    lines.append(f"{rep.symbol}  pivots in force on {rep.for_date}  (levels come from the last COMPLETED period before it)")
    if rep.spot_close:
        lines.append(f"OTM strikes chosen around the underlying's previous close {rep.spot_close:.2f}")
    hdr = (f"{'tf':<4} {'source':<23} {'sess':>4} {'H':>9} {'L':>9} {'C':>9} {'vol':>11} | "
           f"{'S2':>8} {'S1':>8} {'BC':>8} {'PIVOT':>8} {'TC':>8} {'R1':>8} {'R2':>8} | "
           f"{'R3':>8} {'R4':>8} {'S3':>8} {'S4':>8} (kite) | {'R3':>8} {'R4':>8} {'S3':>8} {'S4':>8} (floor)")

    def block(title: str, leg: LegReport | None) -> None:
        if leg is None:
            return
        lines.append("")
        lines.append(f"== {title}: {leg.instrument.name or leg.instrument.symbol}  scrip {leg.instrument.scrip_code}")
        if leg.note:
            lines.append(f"   {leg.note}")
        if leg.ladders:
            lines.append(hdr)
        for ld in leg.ladders:
            lv, fl = ld.levels, ld.floor
            vol = "-" if ld.volume is None else f"{ld.volume:,.0f}"
            lines.append(
                f"{ld.tf:<4} {ld.source:<23} {ld.sessions:>4} {ld.high:>9.2f} {ld.low:>9.2f} {ld.close:>9.2f} {vol:>11} | "
                f"{lv.s2:>8.2f} {lv.s1:>8.2f} {lv.bc:>8.2f} {lv.pivot:>8.2f} {lv.tc:>8.2f} {lv.r1:>8.2f} {lv.r2:>8.2f} | "
                f"{lv.r3:>8.2f} {lv.r4:>8.2f} {lv.s3:>8.2f} {lv.s4:>8.2f}        | "
                f"{fl.r3:>8.2f} {fl.r4:>8.2f} {fl.s3:>8.2f} {fl.s4:>8.2f}"
            )

    block("UNDERLYING", rep.underlying)
    block("FRONT FUTURE", rep.future)
    for o in rep.options:
        block(f"OPTION {o.instrument.option_type.value} {o.instrument.strike:g}", o)
    if not rep.options:
        lines.append("")
        lines.append("no option legs (no tradeable expiry, or the underlying had no previous session)")
    return "\n".join(lines)
