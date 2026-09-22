"""Runs the ported books on every closed bar and keeps what fired.

One instance, driven from ``Engine._on_bar_close``, so the detectors see exactly the bars the
decision path sees — including the exchange-reconciled ones, since a book that fired on a live
build the broker later corrected would be reporting a bar that never existed.

Alerts live in a bounded ring per book. They are **not** written to the ledger: the ledger is the
record of what the engine traded, and these books do not trade. Mixing an advisory alert into it
would make ``signals`` stop meaning "something the gateway saw".
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import structlog

from ..bars.indicators import atr
from ..bars.pivots import PivotLevels, classic_pivots
from ..bars.unified import UnifiedBar
from ..market.session import TF_SECONDS
from . import entry as entry_model
from . import plan as planner
from .detectors import (
    BB_BOOKS,
    Alert,
    BbBreakDetector,
    FudkiiRtDetector,
    FudkoiDetector,
    PivotBossDetector,
)

log = structlog.get_logger(__name__)

#: Per book. A trading day across 216 underlyings does not come close, and the page pages anyway.
RING = 500
#: Bars of history a detector is handed. Enough for BB(20), SuperTrend(7) and a 20-bar volume
#: median with room to warm.
LOOKBACK = 120


class AlertEngine:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.bb = [BbBreakDetector(c) for c in BB_BOOKS]
        self.fudkoi = FudkoiDetector()
        self.pivotboss = PivotBossDetector()
        self.rt = FudkiiRtDetector()
        self.alerts: dict[str, deque[Alert]] = {}
        self.counts: dict[str, int] = {}
        self.evaluated: dict[str, int] = {}
        self._cpr_avg: dict[str, float] = {}
        self._pending_bar_close = 0.0
        self._pending_fired_at = 0.0
        self.started_ts = time.time()

    # -- plumbing ---------------------------------------------------------------------------------

    def _enrich(self, a: Alert, bar: UnifiedBar, history: list[UnifiedBar]) -> None:
        """Attach the plan, the entry model and the CTA. Never raises."""
        # The entry is modelled at *this* instant, so the option's quote age is measured against
        # the moment the trade would be placed rather than the moment the bar closed.
        self._pending_bar_close = float(bar.ts + TF_SECONDS.get(bar.tf, 0))
        self._pending_fired_at = time.time()
        inst = self.engine.underlyings.get(a.symbol)
        a.company = getattr(inst, "name", "") if inst else ""
        a.exchange = inst.segment.exch if inst else "N"
        if a.kind != "TRIGGER":
            # A living signal is not plan-less: its parent shipped a ladder, and the card needs the
            # option leg as much as an entry does. Inherit rather than recompute, so a keep-alive
            # never quotes different levels from the signal it is keeping alive.
            ev = a.evidence or {}
            if {"entry", "stop", "target"} <= set(ev):
                tp = planner.from_living(
                    entry=float(ev["entry"]),
                    stop=float(ev["stop"]),
                    target=float(ev["target"]),
                    direction=a.direction,
                    atr_value=atr(history, 14) if history else None,
                    listed=self._listed_option(a.symbol, a.direction, float(ev["entry"])),
                )
                a.plan = tp.to_json()
                a.cta = planner.cta(tp, a.score, a.kind)
            else:
                a.cta = planner.cta(None, a.score, a.kind)
            return
        try:
            tp = planner.build(
                bar=bar,
                direction=a.direction,
                zones=self.engine.zones_for(a.symbol),
                atr_value=atr(history, 14),
                tick_size=getattr(inst, "tick_size", 0.05) or 0.05,
                listed=self._listed_option(a.symbol, a.direction, bar.close),
            )
        except Exception as exc:  # noqa: BLE001 - the alert is the point; the plan is a bonus
            log.warning("alerts.plan_failed", symbol=a.symbol, error=str(exc))
            tp = None
        a.plan = tp.to_json() if tp else None
        a.cta = planner.cta(tp, a.score, a.kind)

    def _listed_option(self, symbol: str, direction: str, spot: float) -> dict[str, Any] | None:
        """The real contract nearest the OTM strike, when the chain actually lists one.

        The theoretical strike comes off a ladder heuristic; this is what the exchange has. A
        position was opened yesterday on a strike outside the subscribed band and never quoted, so
        the card shows whether the contract it names is one the engine can actually price.
        """
        g = self.engine.groups.get(symbol)
        if not g or not getattr(g, "options", None):
            return None
        want = "CE" if direction == "BULLISH" else "PE"
        target, _ = planner.otm_strike(spot, direction)
        best = None
        for o in g.options:
            if o.option_type.value != want:
                continue
            if best is None or abs(o.strike - target) < abs(best.strike - target):
                best = o
        if best is None:
            return None
        q = self.engine.quotes.get(best.scrip_code)
        ltp = self.engine.ltps.get(best.scrip_code)
        return {
            "scripCode": best.scrip_code,
            "symbol": best.name,
            "strike": best.strike,
            "type": want,
            "expiry": best.expiry,
            "lotSize": best.lot_size,
            "ltp": ltp,
            "oi": self.engine.option_oi.get(best.scrip_code),
            "quotes": ltp is not None,
            "strikeGapFromTheoretical": round(best.strike - target, 2),
            # The entry as it would actually happen: priced off the ask ladder at this instant,
            # not off the last trade at signal time. A buy lifts the ask, and the quote has an age.
            "entry": entry_model.model(
                now=time.time(),
                bar_close=self._pending_bar_close,
                fired_at=self._pending_fired_at,
                underlying_ltp=self.engine.ltps.get(
                    getattr(self.engine.underlyings.get(symbol), "scrip_code", "")
                ),
                quote=q,
                book=self.engine.book_for(best.scrip_code),
                lot_size=best.lot_size,
            ).to_json(),
        }

    def _emit(self, a: Alert) -> None:
        # Stamped here rather than in the detector: the detector is a pure function of the bars
        # it is handed and may not read a clock, or it would not replay identically.
        a.fired_at = time.time()
        a.bar_close = int(a.ts + TF_SECONDS.get(a.tf, 0))
        ring = self.alerts.setdefault(a.book, deque(maxlen=RING))
        ring.appendleft(a)
        self.counts[a.book] = self.counts.get(a.book, 0) + 1
        log.info(
            "alert",
            book=a.book,
            symbol=a.symbol,
            direction=a.direction,
            kind=a.kind,
            score=round(a.score, 1),
            reason=a.reason[:120],
        )

    def _seen(self, book: str) -> None:
        self.evaluated[book] = self.evaluated.get(book, 0) + 1

    def _segment_exch(self, symbol: str) -> str:
        inst = self.engine.underlyings.get(symbol)
        return inst.segment.exch if inst else "N"

    def _cpr_for(self, symbol: str) -> tuple[PivotLevels | None, float | None]:
        """Today's CPR from the previous complete session, and its recent average width."""
        dailies = self.engine.store.bars(symbol, "1d", 25)
        if len(dailies) < 12:
            return None, None
        prev = dailies[-2] if len(dailies) >= 2 else dailies[-1]
        levels = classic_pivots(prev.high, prev.low, prev.close)
        cached = self._cpr_avg.get(symbol)
        if cached is None:
            widths = []
            for b in dailies[-12:-1]:
                lv = classic_pivots(b.high, b.low, b.close)
                if lv and lv.cpr_width > 0:
                    widths.append(lv.cpr_width)
            cached = sum(widths) / len(widths) if widths else 0.0
            self._cpr_avg[symbol] = cached
        return levels, cached or None

    def adopt_signal(self, sig: dict[str, Any]) -> None:
        """A FUDKII signal just fired — hand it to the living-signal book."""
        self.rt.adopt(sig, time.time())

    # -- the bar path -----------------------------------------------------------------------------

    def on_bar(self, bar: UnifiedBar) -> None:
        """Every closed bar of every timeframe. Never raises into the feed."""
        try:
            self._on_bar(bar)
        except Exception as exc:  # noqa: BLE001 - an advisory book may not stall the engine
            log.warning("alerts.failed", book="?", symbol=bar.symbol, tf=bar.tf, error=str(exc))

    def _on_bar(self, bar: UnifiedBar) -> None:
        if bar.tf == "1m":
            out = self.rt.on_bar(bar)
            if out:
                # The decision frame's bars, not the 1m ones: the inherited ladder was measured
                # against the 30m ATR, so the noise check must use the same yardstick.
                decision = self.engine.store.bars(bar.symbol, "30m", LOOKBACK)
                for a in out:
                    self._enrich(a, bar, decision)
                    self._emit(a)
            return

        history = self.engine.store.bars(bar.symbol, bar.tf, LOOKBACK)
        if len(history) < 25:
            return
        exch = self._segment_exch(bar.symbol)

        for det in self.bb:
            if det.cfg.tf != bar.tf:
                continue
            # MCX books run on MCX instruments and the NSE book on NSE ones; the same break on the
            # wrong exchange is a different book with different deployed parameters.
            if det.cfg.book.startswith("MCX") != (exch == "M"):
                continue
            self._seen(det.cfg.book)
            a = det.on_bar(bar, history)
            if a:
                self._enrich(a, bar, history)
                self._emit(a)

        if bar.tf == "30m":
            self._seen("FUDKOI")
            a = self.fudkoi.on_bar(bar, history, exch=exch)
            if a:
                self._enrich(a, bar, history)
                self._emit(a)

            self._seen("PIVOTBOSS")
            levels, avg = self._cpr_for(bar.symbol)
            from ..market.session import ist_day

            pb = self.pivotboss.on_bar(
                bar,
                history,
                levels=levels,
                zones=self.engine.zones_for(bar.symbol),
                cpr_avg_width=avg,
                day=ist_day(bar.ts).isoformat(),
            )
            if pb:
                self._enrich(pb, bar, history)
                self._emit(pb)

    # -- reads ------------------------------------------------------------------------------------

    def feed(self, book: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if book:
            return [a.to_json() for a in list(self.alerts.get(book, ()))[:limit]]
        merged: list[Alert] = []
        for ring in self.alerts.values():
            merged.extend(ring)
        merged.sort(key=lambda a: a.ts, reverse=True)
        return [a.to_json() for a in merged[:limit]]

    def stats(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "evaluated": dict(self.evaluated),
            "living": len(self.rt.living),
            "suppressedByCap": {
                "PIVOTBOSS": self.pivotboss.cap.suppressed,
            },
            "capReached": {
                "PIVOTBOSS": self.pivotboss.cap.global_cap_reached,
            },
            "uptime_s": round(time.time() - self.started_ts, 1),
            "books": sorted({*self.counts, *self.evaluated, "FUDKII_RT"}),
        }
