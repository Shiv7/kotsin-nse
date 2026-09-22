"""Daily pivots for the legs actually traded: the future and the OTM options, not just the cash.

The engine has always computed pivots on the underlying and projected them onto the option through
delta. That answers "where is the *stock* likely to turn", which is the right question for the
signal — but it is not the only question. A premium has its own prior session, its own high and low,
and its own pivot from them, and an option sitting on its own S1 is a different proposition from one
in mid-air, however the equity looks.

So this loads a second, independent ladder per leg.

**Daily only, and deliberately.** A weekly contract has days of history; ``previous_complete`` needs
completed weeks and months, and a monthly pivot computed from four sessions would be a number with a
name it has not earned. The equity keeps the full daily/weekly/monthly ladder; legs get the one
timeframe their history can actually support.

**The previous *completed* session**, so the levels are fixed for the day and walk-forward safe by
construction — the same rule ``Engine.zones_for`` follows.

**Off the boot path.** 216 underlyings times a future plus eight strikes is roughly two thousand
REST calls; blocking the open on that would cost more than the levels are worth. It runs as a
background task with a bounded concurrency and publishes as it goes, so a name is usable the moment
its own legs land rather than when the last one does.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import structlog

from ..bars.pivots import PivotLevels, classic_pivots
from ..domain import Instrument, OptionType

log = structlog.get_logger(__name__)

#: Strikes each side of spot. Eight legs per name — four calls, four puts — which covers the
#: one-step OTM the selector prefers and the further-out strikes the lot cap walks to.
STRIKES_PER_SIDE = 4
#: Concurrent REST calls. The historical endpoint is the same one the boot backfill uses, so this
#: stays modest to avoid competing with it.
CONCURRENCY = 6
#: Calendar days of history requested. Only the previous completed session is used; the rest is
#: slack for holidays and for a contract that listed recently.
LOOKBACK_DAYS = 12


@dataclass(slots=True)
class LegPivots:
    scrip_code: str
    symbol: str
    #: the underlying root, so a name's legs can be found without matching on the contract string
    root: str
    kind: str  # FUTURE | CE | PE
    strike: float
    levels: PivotLevels
    session: str  # the completed day the levels came from
    close: float

    def to_json(self) -> dict[str, Any]:
        lv = self.levels
        return {
            "scripCode": self.scrip_code,
            "symbol": self.symbol,
            "root": self.root,
            "kind": self.kind,
            "strike": self.strike,
            "session": self.session,
            "prevClose": round(self.close, 2),
            "pivot": round(lv.pivot, 2),
            "tc": round(lv.tc, 2),
            "bc": round(lv.bc, 2),
            "cprWidth": round(lv.cpr_width, 2),
            "r": [round(x, 2) for x in (lv.r1, lv.r2, lv.r3, lv.r4)],
            "s": [round(x, 2) for x in (lv.s1, lv.s2, lv.s3, lv.s4)],
        }


def otm_legs(
    *, chain: list[Instrument], spot: float, per_side: int = STRIKES_PER_SIDE
) -> list[Instrument]:
    """The ``per_side`` nearest out-of-the-money strikes on each side of spot.

    Out of the money, not merely nearest: a call below spot and a put above it are in the money and
    behave like the underlying with extra cost, which is not what this book trades.
    """
    if spot <= 0:
        return []
    calls = sorted((o for o in chain if o.option_type is OptionType.CE and o.strike > spot),
                   key=lambda o: o.strike)[:per_side]
    puts = sorted((o for o in chain if o.option_type is OptionType.PE and o.strike < spot),
                  key=lambda o: -o.strike)[:per_side]
    return [*calls, *puts]


def levels_from_candles(rows: list[dict[str, Any]], today: date) -> tuple[PivotLevels, str, float] | None:
    """Classic pivots from the last completed session strictly before ``today``."""
    prior = [r for r in rows if str(r.get("dt", ""))[:10] < today.isoformat()]
    if not prior:
        return None
    last = prior[-1]
    lv = classic_pivots(float(last["h"]), float(last["l"]), float(last["c"]))
    if lv is None:
        return None
    return lv, str(last["dt"])[:10], float(last["c"])


class LegPivotLoader:
    """Fetches and holds one daily ladder per traded leg."""

    def __init__(self, rest: Any) -> None:
        self.rest = rest
        self.by_code: dict[str, LegPivots] = {}
        self.day: str = ""
        self.loaded = 0
        self.failed = 0
        self.running = False
        self._sem = asyncio.Semaphore(CONCURRENCY)

    def for_code(self, scrip_code: str) -> LegPivots | None:
        return self.by_code.get(str(scrip_code))

    def for_root(self, root: str) -> list[LegPivots]:
        """Every leg loaded for one underlying — the full OTM set, not the subscribed few."""
        return [v for v in self.by_code.values() if v.root == root]

    async def _one(self, inst: Instrument, today: date) -> None:
        start = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        async with self._sem:
            try:
                rows = await self.rest.candles(inst, "1d", start, today.isoformat())
            except Exception as exc:  # noqa: BLE001 - one missing leg must not stop the rest
                self.failed += 1
                log.debug("legs.failed", scrip=inst.scrip_code, error=str(exc)[:80])
                return
        got = levels_from_candles(rows, today)
        if got is None:
            self.failed += 1
            return
        levels, session, close = got
        kind = (
            inst.option_type.value
            if inst.option_type in (OptionType.CE, OptionType.PE)
            else "FUTURE"
        )
        self.by_code[inst.scrip_code] = LegPivots(
            scrip_code=inst.scrip_code,
            symbol=inst.name or inst.symbol,
            root=inst.underlying or inst.symbol,
            kind=kind,
            strike=inst.strike,
            levels=levels,
            session=session,
            close=close,
        )
        self.loaded += 1

    async def load(self, legs: list[Instrument], today: date) -> int:
        """Load every leg's ladder. Safe to call again: a fresh day clears the previous one."""
        if self.day != today.isoformat():
            self.by_code.clear()
            self.day = today.isoformat()
            self.loaded = self.failed = 0
        self.running = True
        began = time.time()
        try:
            await asyncio.gather(*(self._one(i, today) for i in legs))
        finally:
            self.running = False
        log.info(
            "legs.loaded",
            legs=len(legs),
            ok=self.loaded,
            failed=self.failed,
            took_s=round(time.time() - began, 1),
        )
        return self.loaded

    def stats(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for v in self.by_code.values():
            kinds[v.kind] = kinds.get(v.kind, 0) + 1
        return {
            "day": self.day,
            "loaded": self.loaded,
            "failed": self.failed,
            "running": self.running,
            "byKind": kinds,
            "strikesPerSide": STRIKES_PER_SIDE,
            "note": (
                "daily pivots only — a weekly contract has days of history, so a weekly or monthly "
                "level computed from it would be a number with a name it has not earned"
            ),
        }
