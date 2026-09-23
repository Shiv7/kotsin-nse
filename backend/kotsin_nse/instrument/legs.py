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
#: Back-off between attempts on a failed candle call. Three tries covers a rate-limit blip and a
#: dropped connection; a leg that fails all three is left to the engine's repair loop.
RETRY_DELAYS_S: tuple[float, ...] = (0.5, 2.0)
#: Calendar days of history requested. Only the previous completed session is used; the rest is
#: slack for holidays and for a contract that listed recently.
LOOKBACK_DAYS = 12
#: A previous session thinner than this does not get a ladder. Measured on MCX crude options,
#: 2026-09-23: the 22-Sep bars carried 46,418 and 18,553 lots and their wide ranges were real — a
#: crude premium genuinely halved that day — but the 17- and 18-Sep bars on the same contracts
#: carried 11 and 5. A high and a low built from five contracts are not a range, and a pivot built
#: on them is indistinguishable from a good one once it is a number on a card. Declined instead.
#:
#: A row with *no* volume field is a different case and is allowed through: a feed that does not
#: report volume is an absence of evidence, not evidence of thinness, and silently dropping every
#: ladder from such a source would be a worse failure than the one this guards against. Those
#: publish ``prevVolume: null`` so the card can say the range is unverified.
MIN_PREV_SESSION_VOLUME = 100.0


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
    #: that session's volume — the evidence the range is worth anything. ``None`` when the feed
    #: did not report it, which is unverified rather than thin.
    volume: float | None = None

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
            "prevVolume": None if self.volume is None else round(self.volume),
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


def levels_from_candles(
    rows: list[dict[str, Any]], today: date, *, min_volume: float = MIN_PREV_SESSION_VOLUME
) -> tuple[PivotLevels, str, float, float | None] | None:
    """Classic pivots from the last completed session strictly before ``today``.

    Declines a session too thin to have a meaningful high and low. An illiquid option prints a few
    contracts at whatever price someone asked, and the resulting "range" is one counterparty's
    opinion rather than the market's — but once it becomes a pivot on a card it looks exactly like
    a level that thousands of lots agreed on.
    """
    prior = [r for r in rows if str(r.get("dt", ""))[:10] < today.isoformat()]
    if not prior:
        return None
    last = prior[-1]
    raw = last.get("v")
    vol = None if raw is None else float(raw)
    if vol is not None and vol < min_volume:
        return None
    high, low = float(last["h"]), float(last["l"])
    # A session that printed at one price has no range, and ``classic_pivots`` will happily return
    # S3 == pivot == R3 == that price. That is not a degenerate ladder, it is an actively harmful
    # one: every level sits on top of every other, so "price is at S1" and "price is at R3" become
    # true at the same instant, and clustering merges nine coincident levels into a fortress wall
    # made of nothing. Measured on MCX silver options, 2026-09-23 (SILVERM 238000 PE, one contract).
    if high <= low:
        return None
    lv = classic_pivots(high, low, float(last["c"]))
    if lv is None:
        return None
    return lv, str(last["dt"])[:10], float(last["c"]), vol


class LegPivotLoader:
    """Fetches and holds one daily ladder per traded leg."""

    def __init__(self, rest: Any) -> None:
        self.rest = rest
        self.by_code: dict[str, LegPivots] = {}
        self.day: str = ""
        self.loaded = 0
        #: REST failures after retries — the repair loop's work list
        self.failed = 0
        self.failed_codes: set[str] = set()
        #: guard refusals (thin or zero-range previous session) — correct outcomes, never retried
        self.refused = 0
        self.refused_codes: set[str] = set()
        self.running = False
        self._sem = asyncio.Semaphore(CONCURRENCY)

    def for_code(self, scrip_code: str) -> LegPivots | None:
        return self.by_code.get(str(scrip_code))

    def for_root(self, root: str) -> list[LegPivots]:
        """Every leg loaded for one underlying — the full OTM set, not the subscribed few."""
        return [v for v in self.by_code.values() if v.root == root]

    async def _one(self, inst: Instrument, today: date) -> None:
        start = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        rows: list[dict[str, Any]] | None = None
        async with self._sem:
            for attempt, delay in enumerate((*RETRY_DELAYS_S, None)):
                try:
                    rows = await self.rest.candles(inst, "1d", start, today.isoformat())
                    break
                except Exception as exc:  # noqa: BLE001 - one missing leg must not stop the rest
                    log.debug("legs.attempt_failed", scrip=inst.scrip_code, attempt=attempt, error=str(exc)[:80])
                    if delay is not None:
                        await asyncio.sleep(delay)
        if rows is None:
            self.failed += 1
            self.failed_codes.add(inst.scrip_code)
            return
        self.failed_codes.discard(inst.scrip_code)
        got = levels_from_candles(rows, today)
        if got is None:
            self.refused += 1
            self.refused_codes.add(inst.scrip_code)
            return
        levels, session, close, vol = got
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
            volume=vol,
        )
        self.loaded += 1

    async def load(self, legs: list[Instrument], today: date) -> int:
        """Load every leg's ladder. Safe to call again: a fresh day clears the previous one."""
        if self.day != today.isoformat():
            self.by_code.clear()
            self.failed_codes.clear()
            self.refused_codes.clear()
            self.day = today.isoformat()
            self.loaded = self.failed = self.refused = 0
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

    def missing(self, legs: list[Instrument]) -> list[Instrument]:
        """Legs with no ladder that were not refused by a guard — what a repair pass refetches."""
        return [i for i in legs if i.scrip_code not in self.by_code and i.scrip_code not in self.refused_codes]

    def stats(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for v in self.by_code.values():
            kinds[v.kind] = kinds.get(v.kind, 0) + 1
        return {
            "day": self.day,
            "loaded": self.loaded,
            "failed": self.failed,
            "refused": self.refused,
            "running": self.running,
            "byKind": kinds,
            "strikesPerSide": STRIKES_PER_SIDE,
            "minPrevVolume": MIN_PREV_SESSION_VOLUME,
            "note": (
                "daily pivots only — a weekly contract has days of history, so a weekly or monthly "
                "level computed from it would be a number with a name it has not earned"
            ),
        }
