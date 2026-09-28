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

from ..config import Segment
from ..domain import Instrument, InstrumentKind
from ..market.session import (
    NSE_EQ_CONTINUOUS_UNTIL,
    bucket_start,
    in_session,
    ist_day,
    ist_naive_to_ts,
    on_session_grid,
    to_ist,
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
    #: the broker's volume and the live build's disagree past belief (``volume_doubt``)
    volume_doubt: str = ""

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
            "volumeDoubt": self.volume_doubt,
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
    #: decision bars whose broker volume disagreed with the live build past belief
    volume_doubts: int = 0

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
        if c.volume_doubt:
            self.volume_doubts += 1
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
            "volume_doubts": self.volume_doubts,
        }


#: an NSE stock's broker candle and its live build disagree on volume by a median 6 % (12 % on the
#: 09:15 bar, which the live build reads with the pre-open); the worst of 612 bars on 2026-09-28 was
#: 37 %. Past half apart one of the two is broken, and nothing says which.
VOLUME_DOUBT_APART = 0.5


def volume_doubt(bar: UnifiedBar, rest_v: float, inst: Instrument | None) -> str:
    """Why a decision bar's volume cannot be believed, or "". Judged only where it can be: an NSE
    stock's continuous bars (the 15:15 auction differs by design), built end to end (a PARTIAL
    build is expected to differ), and with a live count to judge against."""
    if inst is None or inst.segment is not Segment.NSE_EQ or inst.kind is not InstrumentKind.EQUITY:
        return ""
    if bar.source is BarSource.PARTIAL or not on_session_grid(inst.segment, bar.ts, bar.tf, until=NSE_EQ_CONTINUOUS_UNTIL):
        return ""
    live_v = bar.volume
    if live_v <= 0:
        return ""
    if rest_v <= 0:
        return f"broker candle has no volume, live build {live_v:,.0f}"
    apart = abs(live_v - rest_v) / rest_v
    if apart > VOLUME_DOUBT_APART:
        return f"broker {rest_v:,.0f} vs live build {live_v:,.0f} ({apart:.0%} apart)"
    return ""


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
        self.decisions_on_live_bar = 0  # times the decision proceeded without REST truth
        self.decisions_skipped_partial = 0  # unconfirmed fragments that were NOT decided on
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
                c = self._install(bar, match, inst)
                self._record(c)
                return c
            if attempt == 0:
                await asyncio.sleep(self.retry_delay_s)
        c = BarCheck(bar.symbol, bar.tf, bar.ts, found=False, partial=bar.source is BarSource.PARTIAL)
        self._record(c)
        return c

    def _install(self, bar: UnifiedBar, rest_row: dict[str, Any], inst: Instrument | None = None) -> BarCheck:
        live, rest = _ohlcv(bar), _ohlcv(rest_row)
        exact = live == rest
        doubt = volume_doubt(bar, rest["v"], inst) if bar.tf == self.decision_tf and not exact else ""
        c = BarCheck(
            bar.symbol, bar.tf, bar.ts, found=True, exact=exact, live=live, rest=rest,
            partial=bar.source is BarSource.PARTIAL, volume_doubt=doubt,
        )
        if exact:
            # The build was right; just mark it exchange-confirmed.
            bar.extra["confirmed"] = True
            return c
        self.store.replace_closed(self._replacement(bar, rest, {"volume_doubt": doubt} if doubt else {}))
        c.replaced = True
        return c

    @staticmethod
    def _replacement(bar: UnifiedBar, rest: dict[str, float], extra: dict[str, Any]) -> UnifiedBar:
        """``bar`` with the exchange's OHLCV, everything else kept; the live values alongside."""
        live = _ohlcv(bar)
        return UnifiedBar(
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
            extra={**bar.extra, "confirmed": True, "live": live, "live_source": bar.source.value, **extra},
        )

    # -- after the close ------------------------------------------------------------------------

    async def audit_day(
        self, symbols: list[str], day: date, *, keep: Callable[[Instrument, int], bool], pace_s: float = 0.15,
    ) -> dict[str, Any]:
        """The day's decision bars against the broker's candles, fetched once more after the close:
        a bar held differently is replaced, a bar not held is added. ``keep`` says which buckets
        are judged (an NSE stock's continuous 09:15 … 14:45).

        The second look catches what the bar-close check cannot: a reconcile that failed or timed
        out (the live build stood), a candle revised after it answered, a bucket never built (the
        engine down, the feed silent). It is not a second opinion from the same source at the same
        moment — that would repeat whatever the first one got wrong."""
        out: dict[str, Any] = {"day": day.isoformat(), "names": 0, "bars": 0, "exact": 0, "repaired": 0, "added": 0, "failed": 0, "examples": []}
        for symbol in symbols:
            inst = self.resolve(symbol)
            if inst is None:
                continue
            async with self._sem:
                try:
                    rows = await self.rest.candles(inst, self.decision_tf, day.isoformat(), day.isoformat())
                except Exception as exc:  # noqa: BLE001 - one name unanswered never stops the audit
                    out["failed"] += 1
                    log.warning("bars.audit_unanswered", symbol=symbol, error=str(exc)[:120])
                    continue
            if not rows:
                out["failed"] += 1
                continue
            out["names"] += 1
            held = {int(b.ts): b for b in self.store.bars(symbol, self.decision_tf) if ist_day(b.ts) == day}
            prev_close = next(iter(held.values())).prev_close if held else None
            # each row onto its bucket: the broker stamps a candle with its first trade's minute
            # (09:16, 10:46), which IS the bucket; a bucket's own on-grid row wins
            by: dict[int, tuple[bool, dict[str, Any]]] = {}
            for r in rows:
                t = ist_naive_to_ts(str(r["dt"]))
                if not in_session(inst.segment, t):
                    continue
                b_ts = int(bucket_start(inst.segment, t, self.decision_tf))
                held_row = by.get(b_ts)
                if held_row is not None and held_row[0] and int(t) != b_ts:
                    continue
                by[b_ts] = (int(t) == b_ts, r)
            for ts in sorted(by):
                r = by[ts][1]
                if not keep(inst, ts):
                    continue
                out["bars"] += 1
                rest = _ohlcv(r)
                b = held.get(ts)
                if b is not None and _ohlcv(b) == rest:
                    out["exact"] += 1
                    continue
                if b is None:
                    self.store.replace_closed(UnifiedBar(
                        symbol=symbol, scrip_code=inst.scrip_code, tf=self.decision_tf, ts=ts,
                        open=rest["o"], high=rest["h"], low=rest["l"], close=rest["c"], volume=rest["v"],
                        source=BarSource.REST, complete=True, prev_close=prev_close,
                        extra={"confirmed": True, "audit": "added"},
                    ))
                    out["added"] += 1
                    what = f"{symbol} {to_ist(ts):%H:%M} added (v {rest['v']:,.0f})"
                else:
                    self.store.replace_closed(self._replacement(b, rest, {"audit": "repaired", "volume_doubt": ""}))
                    out["repaired"] += 1
                    what = f"{symbol} {to_ist(ts):%H:%M} v {b.volume:,.0f}→{rest['v']:,.0f}"
                if len(out["examples"]) < 8:
                    out["examples"].append(what)
            await asyncio.sleep(pace_s)
        return out

    def _record(self, c: BarCheck) -> None:
        self.stats.setdefault(c.tf, TfStats()).record(c)
        self.recent.append(c)
        if c.replaced:
            log.info("bars.reconciled", symbol=c.symbol, tf=c.tf, ts=c.ts, diffs=c.diffs)
        if c.volume_doubt:
            log.warning("bars.volume_doubt", symbol=c.symbol, tf=c.tf, ts=c.ts, why=c.volume_doubt)
        elif c.error:
            log.warning("bars.reconcile_failed", symbol=c.symbol, tf=c.tf, ts=c.ts, error=c.error)

    # -- periodic sweep -----------------------------------------------------------------------

    async def sweep(self, symbols: list[str], *, today: date | None = None) -> int:
        """Reconcile every unconfirmed closed bar of today for the sweep timeframes. One REST call
        per symbol per timeframe, paced by the semaphore."""
        today = today or date.today()
        day = today.isoformat()
        checked = 0
        for symbol in symbols:
            inst = self.resolve(symbol)
            if inst is None:
                continue
            for tf in self.sweep_tfs:
                pending = [
                    b
                    for b in self.store.bars(symbol, tf)
                    if ist_day(b.ts) == today and not b.extra.get("confirmed") and b.source is not BarSource.REST
                ]
                if not pending:
                    continue
                async with self._sem:
                    try:
                        rows = await self.rest.candles(inst, tf, day, day)
                    except Exception as exc:  # noqa: BLE001
                        self._record(BarCheck(symbol, tf, pending[-1].ts, found=False, error=str(exc)[:160]))
                        continue
                by_ts = {int(ist_naive_to_ts(r["dt"])): r for r in rows}
                for b in pending:
                    row = by_ts.get(b.ts)
                    if row is None:
                        continue  # still forming, or the exchange has not published it yet
                    self._record(self._install(b, row, inst))
                    checked += 1
                await asyncio.sleep(0.05)
        self.sweeps += 1
        self.last_sweep_ts = time.time()
        return checked

    def snapshot(self) -> dict[str, Any]:
        return {
            "by_tf": {tf: s.to_json() for tf, s in sorted(self.stats.items())},
            "decisions_on_live_bar": self.decisions_on_live_bar,
            "decisions_skipped_partial": self.decisions_skipped_partial,
            "sweeps": self.sweeps,
            "last_sweep_ts": self.last_sweep_ts,
            "recent_diffs": [c.to_json() for c in list(self.recent)[-25:] if c.found and not c.exact],
        }


__all__ = ["BarCheck", "BarReconciler", "TfStats"]
