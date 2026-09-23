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
from ..market.session import TF_SECONDS, to_ist
from . import entry as entry_model
from . import plan as planner
from . import rtcard
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
        self.last_refresh_ts = 0.0
        self.refreshes = 0

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
                a.card = self._rt_card(a, bar, history, tp)
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
        # FUDKII-RT only. The wall panel, the dual-trigger stop and the lot caps are that book's
        # rules, and nothing else here has been given them — a PIVOTBOSS alert wearing FUDKII-RT's
        # sizing would be asserting a position size no one specified for it.
        if tp and a.book == "FUDKII_RT":
            a.card = self._rt_card(a, bar, history, tp)

    def _own_r1(self, listed: dict[str, Any]) -> float | None:
        own = self.engine.leg_pivots.for_code(str(listed.get("scripCode") or ""))
        return round(own.levels.r1, 2) if own is not None else None

    def _own_option_ladder(self, listed: dict[str, Any], opt_ltp: float | None) -> list[dict[str, Any]]:
        own = self.engine.leg_pivots.for_code(str(listed.get("scripCode") or ""))
        if own is None:
            return []
        lv = own.levels
        return [
            {
                "n": i,
                "option": round(r, 2),
                "optionGainPct": round((r - opt_ltp) / opt_ltp * 100, 1) if opt_ltp else None,
                "source": f"option's own classic R{i} ({own.session})",
            }
            for i, r in enumerate((lv.r1, lv.r2, lv.r3, lv.r4), start=1)
        ]

    def _rt_card(self, a: Alert, bar: UnifiedBar, history: list[UnifiedBar], tp: Any) -> dict[str, Any]:
        """Walls on both sides, the dual-trigger stop, the option ladder and the odds."""
        from ..bars.indicators import atr as _atr

        ev = a.evidence or {}
        bullish = a.direction == "BULLISH"
        zones = self.engine.zones_for(a.symbol)
        atr_v = _atr(history, 14) if history else None
        price = float(ev.get("entry") or (tp.entry if tp else 0) or bar.close)
        listed = (tp.listed or {}) if tp else {}
        opt_ltp = listed.get("ltp")
        eq_ltp = self.engine.ltps.get(
            getattr(self.engine.underlyings.get(a.symbol), "scrip_code", "")
        )

        ahead = behind = None
        if atr_v:
            ahead = rtcard.find_wall(zones, price, atr_v, ahead=True, bullish=bullish)
            behind = rtcard.find_wall(zones, price, atr_v, ahead=False, bullish=bullish)

        stop = float(ev.get("stop") or (tp.stop if tp and tp.stop else 0) or 0)
        target = float(
            ev.get("target") or (tp.targets[0] if tp and tp.targets else 0) or 0
        )
        odds = rtcard.hit_probability(price, stop, target) if stop and target else {"pT1": None}

        strike = listed.get("strike") or (tp.strike if tp else 0)
        # estimate_delta is what map_levels_to_option and the live position use. The dashboard's
        # logistic is a different curve; showing it here would put a different option stop on
        # the card from the one the engine would set.
        from ..domain import OptionType
        from ..instrument.select import estimate_delta

        delta = abs(
            estimate_delta(
                spot=eq_ltp or price,
                strike=float(strike or 0),
                option_type=OptionType.CE if bullish else OptionType.PE,
            )
        )

        return {
            "wallAhead": ahead.to_json() if ahead else None,
            "wallBehind": behind.to_json() if behind else None,
            "odds": odds,
            "confidence": rtcard.confidence(
                wall_ahead=ahead, wall_behind=behind, surge=ev.get("volumeSurge"), p_t1=odds.get("pT1")
            ),
            "stop": rtcard.dual_stop(
                bullish=bullish,
                equity_entry=price,
                equity_stop=stop,
                option_entry=opt_ltp,
                option_ltp=opt_ltp,
                equity_ltp=eq_ltp,
                delta=delta,
                basis="pivot",
            ) if stop else None,
            # The RT books trade the contract's OWN classic ladder (R1–R4 from its previous
            # session) and arm on the underlying's T1 or the option's 1m close over its R1; the
            # delta-projected ladder is shown only when the contract has no ladder of its own.
            "optionLadder": self._own_option_ladder(listed, opt_ltp) or (rtcard.option_ladder(
                option_entry=opt_ltp,
                equity_entry=price,
                targets=list(tp.targets) if (tp and tp.targets) else ([target] if target else []),
                delta=delta,
            ) if (opt_ltp and (target or (tp and tp.targets))) else []),
            "armOn": {"equityT1": target or None, "optionR1": self._own_r1(listed),
                      "rule": "underlying touches its T1, or the option's 1-minute close ≥ its own R1"},
            "volumeBaseline": rtcard.same_slot_volume(history, bar) if history else None,
            "greeks": {
                "delta": round(delta, 3),
                "deltaSource": "logistic approximation on moneyness",
                "dte": rtcard.dte(listed.get("expiry", "")) if listed.get("expiry") else None,
                "gamma": None,
                "theta": None,
                "iv": None,
                "unavailable": "no implied-vol source on this venue — gamma, theta and IV are not computed",
            },
            # One slot per trade from entry to final exit: a tranche scale-out at T1-T4 is one
            # trade leaving in pieces, so open POSITIONS is the counter, not fills.
            "sizing": rtcard.size_with_fallback(
                chain=list(getattr(self.engine.groups.get(a.symbol), "options", []) or []),
                spot=eq_ltp or price,
                direction=a.direction,
                quote_of=self.engine.quotes.get,
                open_trades=sum(
                    1 for pos in self.engine.positions.values() if pos.status == "OPEN"
                ),
            ),
            "atr30m": round(atr_v, 2) if atr_v else None,
            "liveEquity": eq_ltp,
        }

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

    def adopt_signal(self, sig: dict[str, Any], bar: UnifiedBar | None = None) -> None:
        """A FUDKII signal just fired — hand it to the living-signal book, and publish the ENTRY
        row now. ``fired_at`` is stamped here, in the same call that goes on to place the parent's
        order, so the tab's timestamp is the entry instant to the second."""
        a = self.rt.adopt(sig, time.time(), bar)
        if a is None or bar is None:
            return
        try:
            self._enrich(a, bar, self.engine.store.bars(bar.symbol, "30m", LOOKBACK))
            self._emit(a)
        except Exception as exc:  # noqa: BLE001 - an advisory row may not stall the entry
            log.warning("alerts.failed", book="FUDKII_RT", symbol=bar.symbol, tf=bar.tf, error=str(exc))

    def mark_entered(self, signal_id: str, *, ts: float, price: float, qty: int) -> None:
        """The twin filled: put the actual fill — time to the millisecond, price, quantity — on
        the ENTRY row's card. What was modelled at fire time becomes what happened."""
        for a in self.alerts.get("FUDKII_RT", ()):
            if a.kind == "ENTRY" and (a.evidence or {}).get("signalId") == signal_id:
                card = a.card if a.card is not None else {}
                card["entered"] = {
                    "ts": ts,
                    "ist": to_ist(ts).strftime("%H:%M:%S.%f")[:-3],
                    "price": price,
                    "qty": qty,
                    "lagFromFiredS": round(ts - a.fired_at, 3) if a.fired_at else None,
                }
                a.card = card
                return

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
            "marksAgeS": round(time.time() - self.last_refresh_ts, 2) if self.last_refresh_ts else None,
            "refreshes": self.refreshes,
            "books": sorted({*self.counts, *self.evaluated, "FUDKII_RT"}),
        }

    # -- live marks -------------------------------------------------------------------------------

    def refresh_live(self) -> int:
        """Recompute every volatile number on every live card. Driven at 1s by the engine clock.

        The alternative — computing these in the API read path — would give the card fresh numbers
        that the exit loop never saw, so a stop could appear breached on screen while the engine's
        own evaluation used something else. One computation, one cadence, one answer: whatever
        fires, fires on the numbers the card was showing.

        Delta, the option-side stop, the bid/ask and both exit walks move every tick; the walls,
        the odds and the equity stop do not, so they are left alone.
        """
        from ..domain import OptionType
        from ..instrument.select import estimate_delta

        now = time.time()
        touched = 0
        for ring in self.alerts.values():
            for a in ring:
                card, plan = a.card, a.plan
                if not card or not plan:
                    continue
                listed = plan.get("listed") or {}
                code = listed.get("scripCode")
                if not code:
                    continue
                q = self.engine.quotes.get(code)
                book = self.engine.book_for(code)
                inst = self.engine.underlyings.get(a.symbol)
                spot = self.engine.ltps.get(getattr(inst, "scrip_code", "")) or plan.get("entry")
                if not spot:
                    continue
                bullish = a.direction == "BULLISH"
                delta = abs(
                    estimate_delta(
                        spot=spot,
                        strike=float(listed.get("strike") or 0),
                        option_type=OptionType.CE if bullish else OptionType.PE,
                    )
                )
                ltp = getattr(q, "ltp", None) if q else None
                st = card.get("stop")
                if st:
                    card["stop"] = rtcard.dual_stop(
                        bullish=bullish,
                        equity_entry=float(plan.get("entry") or spot),
                        equity_stop=float(st["equityStop"]),
                        option_entry=float(listed.get("ltp") or 0) or None,
                        option_ltp=ltp,
                        equity_ltp=spot,
                        delta=delta,
                        basis=st.get("basis", "pivot"),
                    )
                lot = int(listed.get("lotSize") or 1)
                # T1 takes one lot; the trail takes the rest. Different depths, different prices,
                # so they are separate walks rather than one average that describes neither.
                card["exitWalks"] = {
                    "t1_1lot": entry_model.exit_walk(
                        quote=q, book=book, lot_size=lot, lots=1, now=now
                    ).to_json(),
                    "trail_3lots": entry_model.exit_walk(
                        quote=q, book=book, lot_size=lot, lots=3, now=now
                    ).to_json(),
                }
                card["liveEquity"] = spot
                card["greeks"] = {**card.get("greeks", {}), "delta": round(delta, 3)}
                card["marksTs"] = now
                # Sampled here because this is the one place that already holds the quote, the
                # spot and the delta together — the three numbers the gamma residual needs.
                self.engine.archive.option_quote(
                    code,
                    now,
                    ltp=ltp,
                    bid=getattr(q, "bid", None) if q else None,
                    ask=getattr(q, "ask", None) if q else None,
                    spot=spot,
                    delta=delta,
                )
                touched += 1
        self.last_refresh_ts = now
        self.refreshes += 1
        return touched
