"""REST cross-check: the broker's own candles are the truth for a CLOSED bucket.

Why this exists — measured, not assumed, on the first live MCX session (2026-09-21):

5paisa's ``MarketFeedV3`` is a **snapshot feed, not a tape**. Each frame carries ``LastRate`` and
the cumulative ``TotalQty`` at the moment it was sent (~6.5 frames/min/symbol on MCX); trades
between two frames are invisible, so a bar built from it can miss an intra-frame high or low, and
volume arrives in lumps that land on whichever side of a minute boundary the next frame falls.
Full-session measurement across 11 symbols: **1m bars 122/139 exact (87.8%)**; every miss was a
boundary attribution (COPPER 23:14 close 1412.75 vs exchange 1412.45, the difference reappearing
in 23:15's open). Volume is never lost — ``TotalQty`` is cumulative — only shifted a bucket.

That is fine for the *forming* bar, which nobody acts on, and exactly wrong for a closed bar that
a strategy will read forever. The historical endpoint returns the exchange's own OHLCV per bucket,
and it serves a bucket while it is still forming, so a just-closed bucket is final within seconds.

So:

* the **decision frame (30m)** is reconciled **before** the strategy sees it — the engine holds
  the decision until REST answers (bounded), then decides on exchange truth;
* the finer frames are reconciled by a periodic sweep, which also yields the running **fidelity**
  metric on the System page: how often, and by how much, the live build disagrees.

Replacement keeps the live values alongside (``extra["live"]``) so the disagreement is inspectable
rather than erased.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from ..domain import Instrument
from ..market.session import (
    TF_SECONDS,
    bucket_end,
    ist_day,
    ist_naive_to_ts,
    session_close_ts,
    session_open_ts,
)
from .store import BarStore
from .unified import BarSource, UnifiedBar

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class BarCheck:
    symbol: str
    tf: str
    ts: int
    found: bool
    exact: bool = False
    replaced: bool = False
    #: the live build was PARTIAL (the socket joined mid-bucket). Expected to differ; counted
    #: separately so the fidelity metric measures only bars built end to end.
    partial: bool = False
    live: dict[str, float] | None = None
    rest: dict[str, float] | None = None
    error: str = ""

    @property
    def diffs(self) -> dict[str, float]:
        if not (self.live and self.rest):
            return {}
        return {k: round(self.live[k] - self.rest[k], 4) for k in ("o", "h", "l", "c", "v") if self.live[k] != self.rest[k]}

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "ts": self.ts,
            "found": self.found,
            "exact": self.exact,
            "replaced": self.replaced,
            "partial": self.partial,
            "diffs": self.diffs,
            "live": self.live,
            "rest": self.rest,
            "error": self.error,
        }


@dataclass(slots=True)
class TfStats:
    compared: int = 0
    exact: int = 0
    replaced: int = 0
    o_diff: int = 0
    h_diff: int = 0
    l_diff: int = 0
    c_diff: int = 0
    v_diff: int = 0
    missing_from_rest: int = 0
    rest_failures: int = 0
    #: partial bars installed from REST — corrections, not fidelity failures
    partial_replaced: int = 0
    partial_missing: int = 0
    #: buckets the socket never delivered, installed from REST by the sweep
    inserted: int = 0

    def record(self, c: BarCheck) -> None:
        if c.error:
            self.rest_failures += 1
            return
        if c.partial:
            # A bar the socket joined mid-bucket was never a measurement of the feed. Measured
            # 2026-09-21 after a 23:46 restart: 16/16 "differed" — every one a single closing
            # snapshot. Count the correction; keep it out of `compared`.
            if c.found:
                self.partial_replaced += 1
            else:
                self.partial_missing += 1
            return
        if not c.found:
            self.missing_from_rest += 1
            return
        self.compared += 1
        if c.exact:
            self.exact += 1
        if c.replaced:
            self.replaced += 1
        d = c.diffs
        for k, attr in (("o", "o_diff"), ("h", "h_diff"), ("l", "l_diff"), ("c", "c_diff"), ("v", "v_diff")):
            if k in d:
                setattr(self, attr, getattr(self, attr) + 1)

    def to_json(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "exact": self.exact,
            "exact_pct": round(self.exact / self.compared * 100, 1) if self.compared else None,
            "replaced": self.replaced,
            "o_diff": self.o_diff,
            "h_diff": self.h_diff,
            "l_diff": self.l_diff,
            "c_diff": self.c_diff,
            "v_diff": self.v_diff,
            "missing_from_rest": self.missing_from_rest,
            "rest_failures": self.rest_failures,
            "partial_replaced": self.partial_replaced,
            "partial_missing": self.partial_missing,
            "inserted": self.inserted,
        }


def _ohlcv(b: UnifiedBar | dict[str, Any]) -> dict[str, float]:
    if isinstance(b, UnifiedBar):
        return {"o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
    return {k: float(b[k]) for k in ("o", "h", "l", "c", "v")}


class BarReconciler:
    """Fetches the exchange's candle for a closed bucket and installs it over the live build."""

    def __init__(
        self,
        rest: Any,
        store: BarStore,
        resolve: Callable[[str], Instrument | None],
        *,
        decision_tf: str = "30m",
        sweep_tfs: tuple[str, ...] = ("1m",),
        settle_delay_s: float = 3.0,
        retry_delay_s: float = 5.0,
        max_concurrent: int = 6,
        keep_checks: int = 200,
        on_insert: Callable[[UnifiedBar], None] | None = None,
    ) -> None:
        self.rest = rest
        self.store = store
        self.resolve = resolve
        self.decision_tf = decision_tf
        self.sweep_tfs = sweep_tfs
        self.settle_delay_s = settle_delay_s
        self.retry_delay_s = retry_delay_s
        self._sem = asyncio.Semaphore(max_concurrent)
        self.stats: dict[str, TfStats] = {}
        self.recent: deque[BarCheck] = deque(maxlen=keep_checks)
        self.on_insert = on_insert
        self.live_bar_kept = 0  # closed 30m / decided 5m bars REST could not confirm in time
        self.partial_unconfirmed = 0  # of those, fragments the socket joined mid-way
        self.inserted = 0  # buckets the socket never delivered, installed from REST by the sweep
        #: (symbol, tf, bucket) of today the exchange has no candle for either — nothing traded; asked once
        self._absent: set[tuple[str, str, int]] = set()
        self._absent_day: date | None = None
        self.last_sweep_ts: float | None = None
        self.sweeps = 0

    # -- one bucket ---------------------------------------------------------------------------

    async def reconcile_bar(self, bar: UnifiedBar, *, timeout_s: float = 12.0) -> BarCheck:
        """Fetch the exchange's version of ``bar``'s bucket and install it. Bounded by ``timeout_s``
        so a slow broker delays the decision, never blocks it."""
        try:
            return await asyncio.wait_for(self._reconcile(bar), timeout=timeout_s)
        except TimeoutError:
            c = BarCheck(bar.symbol, bar.tf, bar.ts, found=False, error=f"timeout after {timeout_s:.0f}s", partial=bar.source is BarSource.PARTIAL)
            self._record(c)
            return c

    async def _reconcile(self, bar: UnifiedBar) -> BarCheck:
        inst = self.resolve(bar.symbol)
        if inst is None:
            c = BarCheck(bar.symbol, bar.tf, bar.ts, found=False, error="no instrument", partial=bar.source is BarSource.PARTIAL)
            self._record(c)
            return c
        await asyncio.sleep(self.settle_delay_s)
        day = ist_day(bar.ts).isoformat()
        for attempt in (0, 1):
            async with self._sem:
                try:
                    rows = await self.rest.candles(inst, bar.tf, day, day)
                except Exception as exc:  # noqa: BLE001 - REST failing must not stop the decision
                    c = BarCheck(bar.symbol, bar.tf, bar.ts, found=False, error=str(exc)[:160], partial=bar.source is BarSource.PARTIAL)
                    self._record(c)
                    return c
            match = next((r for r in rows if int(ist_naive_to_ts(r["dt"])) == bar.ts), None)
            if match is not None:
                c = self._install(bar, match)
                self._record(c)
                return c
            if attempt == 0:
                await asyncio.sleep(self.retry_delay_s)
        c = BarCheck(bar.symbol, bar.tf, bar.ts, found=False, partial=bar.source is BarSource.PARTIAL)
        self._record(c)
        return c

    def _install(self, bar: UnifiedBar, rest_row: dict[str, Any]) -> BarCheck:
        live, rest = _ohlcv(bar), _ohlcv(rest_row)
        exact = live == rest
        c = BarCheck(
            bar.symbol, bar.tf, bar.ts, found=True, exact=exact, live=live, rest=rest,
            partial=bar.source is BarSource.PARTIAL,
        )
        if exact:
            # The build was right; just mark it exchange-confirmed.
            bar.extra["confirmed"] = True
            return c
        replacement = UnifiedBar(
            symbol=bar.symbol,
            scrip_code=bar.scrip_code,
            tf=bar.tf,
            ts=bar.ts,
            open=rest["o"],
            high=rest["h"],
            low=rest["l"],
            close=rest["c"],
            volume=rest["v"],
            trades=bar.trades,
            source=BarSource.REST,
            complete=True,
            vwap=bar.vwap,
            prev_close=bar.prev_close,
            oi=bar.oi,
            oi_change_pct=bar.oi_change_pct,
            fut_scrip_code=bar.fut_scrip_code,
            extra={**bar.extra, "confirmed": True, "live": live, "live_source": bar.source.value},
        )
        self.store.replace_closed(replacement)
        c.replaced = True
        return c

    def _record(self, c: BarCheck) -> None:
        self.stats.setdefault(c.tf, TfStats()).record(c)
        self.recent.append(c)
        if c.replaced:
            log.info("bars.reconciled", symbol=c.symbol, tf=c.tf, ts=c.ts, diffs=c.diffs)
        elif c.error:
            log.warning("bars.reconcile_failed", symbol=c.symbol, tf=c.tf, ts=c.ts, error=c.error)

    # -- periodic sweep -----------------------------------------------------------------------

    async def sweep(self, symbols: list[str], *, today: date | None = None, now: float | None = None) -> int:
        """Reconcile every unconfirmed closed bar of today for the sweep timeframes, and install from REST
        every bucket of today that has ended and the store does not hold at all. One REST call per symbol
        per timeframe, paced by the semaphore.

        The missing buckets are real: a laptop that slept through 09:15 (27 Oct 2026) woke with no bars for
        the gap, and a socket reconnect leaves the same shape; a sweep that only repaired buckets it held left
        them empty forever. A bucket the exchange has no candle for either — nothing traded: an illiquid
        minute, or 15:15–15:27 on every F&O stock since NSE's closing auction (3 Aug 2026) — is remembered
        and not asked for again."""
        today = today or date.today()
        now = time.time() if now is None else now
        day = today.isoformat()
        if self._absent_day != today:
            self._absent, self._absent_day = set(), today
        checked = 0
        for symbol in symbols:
            inst = self.resolve(symbol)
            if inst is None:
                continue
            for tf in self.sweep_tfs:
                held = {int(b.ts): b for b in self.store.bars(symbol, tf) if ist_day(b.ts) == today}
                pending = [b for b in held.values() if not b.extra.get("confirmed") and b.source is not BarSource.REST]
                missing = self._missing_buckets(inst, tf, today, held, now)
                if not pending and not missing:
                    continue
                async with self._sem:
                    try:
                        rows = await self.rest.candles(inst, tf, day, day)
                    except Exception as exc:  # noqa: BLE001
                        ref_ts = pending[-1].ts if pending else missing[-1]
                        self._record(BarCheck(symbol, tf, ref_ts, found=False, error=str(exc)[:160]))
                        continue
                by_ts = {int(ist_naive_to_ts(r["dt"])): r for r in rows}
                for b in pending:
                    row = by_ts.get(b.ts)
                    if row is None:
                        continue  # still forming, or the exchange has not published it yet
                    self._record(self._install(b, row))
                    checked += 1
                for ts in missing:
                    row = by_ts.get(ts)
                    if row is None:
                        # the exchange has no candle for it either (nothing traded) — once it is two
                        # minutes old, stop asking: an illiquid name's empty minutes are permanent
                        if rows and bucket_end(inst.segment, ts, tf) <= now - 120:
                            self._absent.add((symbol, tf, ts))
                        continue
                    self._insert(inst, tf, ts, row)
                    checked += 1
                await asyncio.sleep(0.05)
        self.sweeps += 1
        self.last_sweep_ts = time.time()
        return checked

    def _missing_buckets(self, inst: Instrument, tf: str, today: date, held: dict[int, UnifiedBar], now: float) -> list[int]:
        """The buckets of ``today`` that ended at least ``settle_delay_s`` ago and are not in ``held``."""
        if tf not in TF_SECONDS:
            return []
        seg = inst.segment
        start, close = int(session_open_ts(seg, today)), int(session_close_ts(seg, today))
        return [
            b0
            for b0 in range(start, close, TF_SECONDS[tf])
            if b0 not in held
            and (inst.symbol, tf, b0) not in self._absent
            and bucket_end(seg, b0, tf) <= now - self.settle_delay_s
        ]

    def _insert(self, inst: Instrument, tf: str, ts: int, rest_row: dict[str, Any]) -> None:
        rest = _ohlcv(rest_row)
        bar = UnifiedBar(
            symbol=inst.symbol,
            scrip_code=inst.scrip_code,
            tf=tf,
            ts=int(ts),
            open=rest["o"],
            high=rest["h"],
            low=rest["l"],
            close=rest["c"],
            volume=rest["v"],
            source=BarSource.REST,
            complete=True,
            extra={"confirmed": True, "inserted": True},
        )
        self.store.replace_closed(bar)
        self.inserted += 1
        self.stats.setdefault(tf, TfStats()).inserted += 1
        log.info("bars.inserted", symbol=inst.symbol, tf=tf, ts=ts)
        if self.on_insert is not None:
            self.on_insert(bar)

    def snapshot(self) -> dict[str, Any]:
        return {
            "by_tf": {tf: s.to_json() for tf, s in sorted(self.stats.items())},
            "live_bar_kept": self.live_bar_kept,
            "partial_unconfirmed": self.partial_unconfirmed,
            "inserted": self.inserted,
            "sweeps": self.sweeps,
            "last_sweep_ts": self.last_sweep_ts,
            "recent_diffs": [c.to_json() for c in list(self.recent)[-25:] if c.found and not c.exact],
        }


__all__ = ["BarCheck", "BarReconciler", "TfStats"]
