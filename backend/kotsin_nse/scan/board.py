"""The board: every stock scored, ranked per side, the contenders, the picks, and the market around
them (breadth, sectors). One pure function, :func:`build_board`, over one minute's ``DayState``s.

Rules (each a named constant, so a replay can be re-run with another value):

* contenders — the ``CONTENDERS`` strongest on each side;
* picks — the top ``PICKS_PER_SIDE`` of each side, at most ``MAX_PER_SECTOR`` from one sector, and
  never a stock flagged as event risk or one listed in ``no_pick`` (MCX commodities: ranked and
  shown, but the rule was only ever tested on NSE stocks);
* event risk — fell more than ``EVENT_RISK_ADRS`` average daily ranges from the previous close: a
  move that size is news, and a level drawn on it means nothing;
* OI build-up — the near future's price change against its OI change, the standard four labels.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .strength import DayState, pct_change, strength

CONTENDERS = 20
PICKS_PER_SIDE = 3
MAX_PER_SECTOR = 2
EVENT_RISK_ADRS = 3.0
#: a futures OI move smaller than this (% vs the previous session) is no build-up at all: on
#: 2026-10-19 a +0.05 % change labelled TECHM "SHORT BUILD-UP" along with 90 other stocks
OI_NEUTRAL_PCT = 1.0
#: the sector the live board files MCX commodities under
COMMODITY = "Commodity"


@dataclass(frozen=True, slots=True)
class OIRead:
    oi_change_pct: float
    price_change_pct: float

    @property
    def build_up(self) -> str:
        if abs(self.oi_change_pct) < OI_NEUTRAL_PCT:
            return "NEUTRAL"
        up, more = self.price_change_pct >= 0, self.oi_change_pct >= 0
        if up and more:
            return "LONG BUILD-UP"
        if not up and more:
            return "SHORT BUILD-UP"
        if up and not more:
            return "SHORT COVERING"
        return "LONG UNWINDING"


@dataclass(slots=True)
class Row:
    symbol: str
    sector: str
    last: float
    pct: float | None
    score: float
    side: str  # BULLISH | BEARISH | NONE
    rank: int | None = None  # 1 = strongest on its side
    percentile: float | None = None  # across every scored stock, 0..100
    event_risk: bool = False
    above_vwap: bool | None = None
    oi_build_up: str | None = None
    oi_change_pct: float | None = None
    pick: bool = False
    rule: str = ""
    #: the four-point check for a contender: in the top 20, its sector leans its way, breadth
    #: leans its way, price on its side of VWAP
    align: int | None = None
    setup: dict[str, Any] | None = None


@dataclass(slots=True)
class Board:
    ts: int
    rows: list[Row]
    bullish: list[str] = field(default_factory=list)  # contenders, strongest first
    bearish: list[str] = field(default_factory=list)
    picks: list[str] = field(default_factory=list)
    breadth: dict[str, int] = field(default_factory=dict)
    sectors: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "rows": [asdict(r) for r in self.rows],
            "bullish": self.bullish,
            "bearish": self.bearish,
            "picks": self.picks,
            "breadth": self.breadth,
            "sectors": self.sectors,
        }


def build_board(
    ts: int,
    states: Sequence[DayState],
    sectors: Mapping[str, str],
    oi: Mapping[str, OIRead] | None = None,
    *,
    contenders: int = CONTENDERS,
    picks_per_side: int = PICKS_PER_SIDE,
    max_per_sector: int = MAX_PER_SECTOR,
    event_risk_adrs: float = EVENT_RISK_ADRS,
    no_pick: Mapping[str, str] | None = None,
) -> Board:
    """``no_pick`` maps a symbol to the reason it may rank but never be picked."""
    oi = oi or {}
    no_pick = no_pick or {}
    rows: list[Row] = []
    for d in states:
        sc = strength(d)
        drop_adrs = (d.last - d.prev_close) / d.adr if d.adr and d.prev_close > 0 else 0.0
        o = oi.get(d.symbol)
        rows.append(
            Row(
                symbol=d.symbol,
                sector=sectors.get(d.symbol, "Other"),
                last=d.last,
                pct=round(p, 3) if (p := pct_change(d)) is not None else None,
                score=round(sc, 4),
                side="BULLISH" if sc > 0 else "BEARISH" if sc < 0 else "NONE",
                event_risk=drop_adrs <= -event_risk_adrs,
                above_vwap=(d.last > d.vwap) if d.vwap else None,
                oi_build_up=o.build_up if o else None,
                oi_change_pct=round(o.oi_change_pct, 2) if o else None,
            )
        )

    scored = sorted((r for r in rows if r.score != 0), key=lambda r: r.score)
    for i, r in enumerate(scored):
        r.percentile = round(100 * (i + 0.5) / len(scored), 1)
    bull = sorted((r for r in rows if r.side == "BULLISH"), key=lambda r: -r.score)
    bear = sorted((r for r in rows if r.side == "BEARISH"), key=lambda r: r.score)
    for side in (bull, bear):
        for i, r in enumerate(side, 1):
            r.rank = i

    picks: list[str] = []
    for side in (bull, bear):
        per_sector: dict[str, int] = defaultdict(int)
        taken = 0
        for r in side:
            if r.symbol in no_pick:
                r.rule = no_pick[r.symbol]  # takes no pick slot
                continue
            if taken >= picks_per_side:
                r.rule = r.rule or ("NOT PICKED · outside top" if r.rank and r.rank <= contenders else "")
                continue
            if r.event_risk:
                r.rule = "NOT PICKED · event risk"
                continue
            if per_sector[r.sector] >= max_per_sector:
                r.rule = "NOT PICKED · sector cap"
                continue
            per_sector[r.sector] += 1
            taken += 1
            r.pick = True
            r.rule = f"PICK · {r.side} #{r.rank}"
            picks.append(r.symbol)

    up = sum(1 for r in rows if r.pct is not None and r.pct > 0)
    down = sum(1 for r in rows if r.pct is not None and r.pct < 0)
    by_sector: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_sector[r.sector].append(r)
    sector_rows = []
    for name, members in by_sector.items():
        pcts = [m.pct for m in members if m.pct is not None]
        sector_rows.append(
            {
                "sector": name,
                "stocks": len(members),
                "avg_pct": round(sum(pcts) / len(pcts), 3) if pcts else None,
                "avg_score": round(sum(m.score for m in members) / len(members), 4),
                "up": sum(1 for p in pcts if p > 0),
                "down": sum(1 for p in pcts if p < 0),
                "cells": [{"symbol": m.symbol, "pct": m.pct} for m in sorted(members, key=lambda m: -(m.pct or 0))],
            }
        )
    sector_rows.sort(key=lambda s: -(s["avg_score"]))

    sector_lean = {s["sector"]: s["avg_score"] for s in sector_rows}
    for side, rows_of in (("BULLISH", bull[:contenders]), ("BEARISH", bear[:contenders])):
        sign = 1 if side == "BULLISH" else -1
        breadth_ok = (len(bull) - len(bear)) * sign > 0
        for r in rows_of:
            vwap_ok = r.above_vwap is not None and r.above_vwap == (side == "BULLISH")
            r.align = 1 + int(sector_lean.get(r.sector, 0) * sign > 0) + int(breadth_ok) + int(vwap_ok)

    return Board(
        ts=ts,
        rows=sorted(rows, key=lambda r: -r.score),
        bullish=[r.symbol for r in bull[:contenders]],
        bearish=[r.symbol for r in bear[:contenders]],
        picks=picks,
        breadth={
            "bullish": len(bull),
            "bearish": len(bear),
            "neutral": len(rows) - len(bull) - len(bear),
            "up": up,
            "down": down,
            "total": len(rows),
        },
        sectors=sector_rows,
    )


def attach_setups(board: Board, structure_of: Any) -> Board:
    """Give every contender its structure setup, when the setup points the contender's way.
    ``structure_of(symbol)`` → a ``structure.engine.Structure`` or None."""
    for r in board.rows:
        if r.align is None:
            continue
        st = structure_of(r.symbol)
        su = st.setup if st is not None else None
        if su is not None and su.side == r.side:
            zn = st.zones[su.zone_index]
            r.setup = {
                "z": su.z, "s1": round(su.s1, 4), "r1": su.r1, "r2": su.r2, "rr": su.rr, "state": su.state,
                "distance_pct": round((r.last / su.z - 1) * 100, 3) if su.z else None,
                "swept_before": zn.swept_before, "impulse": zn.impulse, "bos_kind": zn.bos_kind,
                "zone_low": zn.low, "zone_high": zn.high,
            }
    return board
