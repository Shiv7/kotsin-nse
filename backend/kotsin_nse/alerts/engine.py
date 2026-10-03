"""Runs the ported books on every closed bar and keeps what fired.

One instance, driven from ``Engine._on_bar_close``, so the detectors see exactly the bars the
decision path sees — including the exchange-reconciled ones, since a book that fired on a live
build the broker later corrected would be reporting a bar that never existed.

Alerts live in a bounded ring per book. They are **not** written to the ledger: the ledger is the
record of what the engine traded, and these books do not trade. Mixing an advisory alert into it
would make ``signals`` stop meaning "something the gateway saw".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import structlog

from ..bars.indicators import SUPERTREND_CONVERGED_BARS, atr
from ..bars.pivots import PivotLevels, classic_pivots
from ..bars.unified import UnifiedBar
from ..market.session import TF_SECONDS, to_ist
from ..strategy.keys import StrategyKey
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

#: Per book, and a ceiling rather than a page size: the ring is emptied every session
#: (``reset_day``, 00:30 IST), and a day across 216 underlyings does not come near this, so no
#: signal is ever dropped for want of room. The page asks for all of them.
RING = 5000
#: a carded contract stays on the tape (``ops/tape.py``) this long after the alert fired. The ring
#: holds a whole day; the tape wants the window the card was live in, not every contract since 09:15.
TAPE_CARD_TTL_S = 1800.0
#: Bars of history a detector is handed. Enough for BB(20), SuperTrend(7) and a 20-bar volume
#: median with room to warm.
LOOKBACK = SUPERTREND_CONVERGED_BARS


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
        #: what each live card's numbers were last computed from, and each contract's last archived
        #: quote row — refresh_live recomputes and archives only on a change
        self._card_inputs: dict[tuple[str, str, int, float], tuple[Any, ...]] = {}
        self._quote_rows: dict[str, tuple[Any, ...]] = {}
        self.refreshes = 0
        #: the session the rings were last emptied for; "" until the first reset
        self.reset_day_stamp = ""
        #: Where the session is saved so a restart does not blank the page. None keeps it in
        #: memory only (tests, previews). Set by the engine; written by its housekeeping loop.
        self.store_dir: Path | None = None
        #: something worth saving has changed since the last save
        self.dirty = False

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
        from ..domain import OptionType as _OT

        tol = None
        if opt_ltp:
            tol, _ = self.engine.option_ladder_tolerance(
                own.root, float(listed.get("strike") or own.strike), _OT(str(listed.get("type") or own.kind)), float(opt_ltp)
            )
        rungs = own.rungs_above(opt_ltp or 0.0, tolerance_pct=tol) if tol else own.rungs_above(opt_ltp or 0.0)
        return [
            {
                "n": i,
                "option": r["price"],
                "optionGainPct": round((r["price"] - opt_ltp) / opt_ltp * 100, 1) if opt_ltp else None,
                "strength": r["strength"],
                "source": f"option's own {','.join(r['members'])} ({own.session}"
                          f"{' / ' + own.weekly_session if own.weekly_session else ''})",
            }
            for i, r in enumerate(rungs[:4], start=1)
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
                # the RT book's own slots: every book counts only its own positions (2026-09-23)
                open_trades=sum(
                    1 for pos in self.engine.positions.values()
                    if pos.status == "OPEN" and pos.strategy == StrategyKey.FUDKII_RT_X.value
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

    def _emit(self, a: Alert, *, replayed: bool = False) -> None:
        # Stamped here rather than in the detector: the detector is a pure function of the bars
        # it is handed and may not read a clock, or it would not replay identically.
        a.bar_close = int(a.ts + TF_SECONDS.get(a.tf, 0))
        if replayed:
            # Rebuilt at boot for a bar that closed while the process was down: it is stamped at
            # its bar's close — the instant it would have fired — and says it was rebuilt.
            a.fired_at = float(a.bar_close)
            a.evidence = {**(a.evidence or {}), "replayed": True}
        else:
            a.fired_at = time.time()
        ring = self.alerts.setdefault(a.book, deque(maxlen=RING))
        ring.appendleft(a)
        self.counts[a.book] = self.counts.get(a.book, 0) + 1
        self.dirty = True
        log.info(
            "alert.replayed" if replayed else "alert",
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

    def mark_entered(self, signal_id: str, *, ts: float, price: float, qty: int, book: str = "") -> None:
        """A book filled: put the actual fill — time to the millisecond, price, quantity — on the
        ENTRY row's card, EACH book's under its own name (every book places its own order since
        2026-09-26, and one ``entered`` slot showed whichever book filled last). ``entered`` stays the
        first fill. What was modelled at fire time becomes what happened."""
        for a in self.alerts.get("FUDKII_RT", ()):
            if a.kind == "ENTRY" and (a.evidence or {}).get("signalId") == signal_id:
                card = a.card if a.card is not None else {}
                fill = {
                    "ts": ts,
                    "ist": to_ist(ts).strftime("%H:%M:%S.%f")[:-3],
                    "price": price,
                    "qty": qty,
                    "lagFromFiredS": round(ts - a.fired_at, 3) if a.fired_at else None,
                }
                card.setdefault("entered", {**fill, "book": book})
                if book:
                    card.setdefault("entries", {})[book] = fill
                a.card = card
                self.dirty = True
                return

    def mark_route(self, signal_id: str, *, decision: dict[str, Any]) -> None:
        """The counter-trend route on the trigger (COUNTER / IN_TREND, the wall behind it) — on the
        ENTRY card, so an in-trend trade shows the wall it ran into and a fade shows why."""
        for a in self.alerts.get("FUDKII_RT", ()):
            if a.kind == "ENTRY" and (a.evidence or {}).get("signalId") == signal_id:
                card = a.card if a.card is not None else {}
                card["route"] = decision
                a.card = card
                self.dirty = True
                return

    def mark_skipped(self, signal_id: str, *, book: str, reason: str) -> None:
        """A twin declined the mirror (dried volume): say so on the ENTRY card, per book, so an RT
        book with no trade reads as "skipped, because" rather than "missed"."""
        for a in self.alerts.get("FUDKII_RT", ()):
            if a.kind == "ENTRY" and (a.evidence or {}).get("signalId") == signal_id:
                card = a.card if a.card is not None else {}
                card.setdefault("skipped", []).append({"book": book, "reason": reason})
                a.card = card
                self.dirty = True
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

        for a in self._book_alerts(bar, self.engine.store.bars(bar.symbol, bar.tf, LOOKBACK)):
            self._emit(a)

    def _book_alerts(self, bar: UnifiedBar, history: Sequence[UnifiedBar]) -> list[Alert]:
        """Every advisory book on one closed bar, given the bars up to and including it. Returns
        the enriched alerts rather than emitting them, so the boot replay can run the same code
        on history cut at each past bar."""
        out: list[Alert] = []
        history = list(history)
        if len(history) < 25:
            return out
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
                out.append(a)

        if bar.tf == "30m":
            self._seen("FUDKOI")
            a = self.fudkoi.on_bar(bar, history, exch=exch)
            if a:
                self._enrich(a, bar, history)
                out.append(a)

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
                out.append(pb)
        return out

    # -- the session across a restart ---------------------------------------------------------------

    FILE = "alerts.json"

    def snapshot(self) -> dict[str, Any]:
        """Everything the page shows, as JSON. Built on the event loop, where the rings live, so
        it never iterates a ring mid-append; only the file write goes to a thread."""
        self.dirty = False
        return {
            "version": 1,
            "saved_ts": time.time(),
            "reset_day": self.reset_day_stamp,
            "counts": dict(self.counts),
            "evaluated": dict(self.evaluated),
            "rings": {book: [a.to_json() for a in ring] for book, ring in self.alerts.items()},
        }

    def write(self, snap: dict[str, Any]) -> Path | None:
        """Atomically replace the saved session. Safe off the loop: it touches only ``snap``."""
        if self.store_dir is None:
            return None
        self.store_dir.mkdir(parents=True, exist_ok=True)
        path = self.store_dir / self.FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, default=str, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def save(self) -> Path | None:
        return self.write(self.snapshot())

    def restore(self, since_ts: float) -> int:
        """Bring back the session saved before a restart — but only if it was saved after the last
        00:30 IST reset, so yesterday's page never comes back. Returns the alerts restored."""
        if self.store_dir is None:
            return 0
        path = self.store_dir / self.FILE
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            log.warning("alerts.restore_unreadable", path=str(path), error=str(exc)[:120])
            return 0
        if float(snap.get("saved_ts") or 0) < since_ts:
            log.info("alerts.restore_skipped", reason="saved before the last reset", saved_ts=snap.get("saved_ts"))
            return 0
        n = 0
        for book, rows in (snap.get("rings") or {}).items():
            ring: deque[Alert] = deque(maxlen=RING)
            for row in rows:
                try:
                    ring.append(Alert.from_json(row))
                except (KeyError, TypeError, ValueError):
                    continue
            if ring:
                self.alerts[book] = ring
                n += len(ring)
        self.counts = {k: int(v) for k, v in (snap.get("counts") or {}).items()}
        self.evaluated = {k: int(v) for k, v in (snap.get("evaluated") or {}).items()}
        self.reset_day_stamp = snap.get("reset_day") or self.reset_day_stamp
        log.info("alerts.restored", alerts=n, books=sorted(self.alerts))
        return n

    def has_signal(self, signal_id: str) -> bool:
        return any((a.evidence or {}).get("signalId") == signal_id for a in self.alerts.get("FUDKII_RT", ()))

    def adopt_rebuilt(
        self, sig: dict[str, Any], bar: UnifiedBar, history: Sequence[UnifiedBar], *, skipped: str | None
    ) -> bool:
        """A FUDKII signal found after the fact: its ENTRY row, stamped at its bar's close and
        marked rebuilt. ``skipped`` says why no book traded it; None for one that was handled
        live and only lost its row (a restart before the session was saved). Never living: nothing
        re-checks a trade that was never taken."""
        if self.has_signal(sig["signal_id"]):
            return False
        a = self.rt.adopt(sig, float(bar.ts + TF_SECONDS.get(bar.tf, 0)), bar)
        self.rt.living.pop(sig["signal_id"], None)
        if a is None:
            return False
        try:
            self._enrich(a, bar, list(history))
        except Exception as exc:  # noqa: BLE001 - a row without its plan beats no row
            log.warning("alerts.enrich_failed", symbol=bar.symbol, error=str(exc)[:120])
        if skipped:
            a.card = {**(a.card or {}), "skipped": [{"book": "ALL", "reason": skipped}]}
        self._emit(a, replayed=True)
        return True

    @staticmethod
    def _key(a: Alert) -> tuple[str, str, int, str, str]:
        return (a.book, a.symbol, int(a.ts), a.kind, a.direction)

    async def catch_up(
        self,
        bars: Iterable[UnifiedBar],
        *,
        history_of: Callable[[UnifiedBar], Sequence[UnifiedBar]],
        fudkii: Callable[[UnifiedBar], Sequence[Any]] | None = None,
        known_signal_ids: set[str] | frozenset[str] = frozenset(),
        yield_every: int = 100,
    ) -> dict[str, int]:
        """Replay today's closed bars through fresh detectors, oldest first.

        Two gaps close here. A restart used to blank the page — the rings lived only in memory —
        and a process that was down at a boundary never evaluated it at all (2026-09-25: nothing
        ran before 10:58, so 09:45, 10:15 and 10:45 were simply absent). The detectors are pure
        functions of the bars handed to them, so running them on history cut at each past bar is
        what they would have said then. Fresh detectors, because their caps and cooldowns must be
        rebuilt from the open, not continued from whatever a restored ring implies.

        Only alerts the page does not already hold are added, each stamped at its bar's close and
        marked replayed. FUDKII triggers come back as ENTRY rows that say they were NOT traded;
        signals the ledger already has were handled live and are left to it.
        """
        have = {self._key(a) for ring in self.alerts.values() for a in ring}
        have_ids = {
            str((a.evidence or {}).get("signalId") or "")
            for a in self.alerts.get("FUDKII_RT", ())
        } | set(known_signal_ids)
        restored_eval = dict(self.evaluated)
        self.bb = [BbBreakDetector(c) for c in BB_BOOKS]
        self.fudkoi = FudkoiDetector()
        self.pivotboss = PivotBossDetector()
        self.evaluated = {}
        added = {"books": 0, "fudkii": 0, "bars": 0}
        for i, bar in enumerate(bars):
            added["bars"] += 1
            history = history_of(bar)
            try:
                for a in self._book_alerts(bar, history):
                    if self._key(a) not in have:
                        have.add(self._key(a))
                        self._emit(a, replayed=True)
                        added["books"] += 1
                for sig in (fudkii(bar) if fudkii else ()):
                    sj = sig.to_json() if hasattr(sig, "to_json") else dict(sig)
                    if sj["signal_id"] in have_ids:
                        continue
                    have_ids.add(sj["signal_id"])
                    if self.adopt_rebuilt(
                        sj, bar, history,
                        skipped="not traded — the engine was not running at this bar's close; rebuilt at boot",
                    ):
                        added["fudkii"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad bar must not cost the rest of the day
                log.warning("alerts.catch_up_failed", symbol=bar.symbol, ts=bar.ts, error=str(exc)[:160])
            if yield_every and i % yield_every == yield_every - 1:
                await asyncio.sleep(0)  # let the page be served while the day replays
        # a restored count may include 1m/15m evaluations the replay cannot redo; keep the larger
        for book, n in restored_eval.items():
            self.evaluated[book] = max(n, self.evaluated.get(book, 0))
        self.counts = {book: len(ring) for book, ring in self.alerts.items()}
        log.info("alerts.caught_up", **added)
        return added

    # -- reads ------------------------------------------------------------------------------------

    def feed(self, book: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Today's alerts, newest first. ``limit=None`` — the default, and what the page asks for
        — is every one of them: the session's signals are the whole point of the page, and a page
        that silently shows the newest 100 of 140 is a page that lies about the day."""
        if book:
            rows = list(self.alerts.get(book, ()))
        else:
            rows = [a for ring in self.alerts.values() for a in ring]
            rows.sort(key=lambda a: a.ts, reverse=True)
        return [a.to_json() for a in (rows[:limit] if limit else rows)]

    def reset_day(self, day: str) -> dict[str, int]:
        """Empty the page for the coming session (``KN_ALERTS_RESET_IST``, 00:30 IST).

        Yesterday's triggers are on the ledger and on the trigger-card tabs, which are read per
        day; the alert rings are the *live* view and were the one thing that carried across the
        roll, so a 09:20 page opened with a day of stale cards above the new ones. The detectors
        are rebuilt rather than poked: their daily caps, cooldowns and the living book are the
        rest of yesterday, and a fresh instance is the same state a fresh process would have.
        """
        cleared = {book: len(ring) for book, ring in self.alerts.items() if ring}
        self.alerts.clear()
        self._card_inputs.clear()
        self._quote_rows.clear()
        self.counts.clear()
        self.evaluated.clear()
        self._cpr_avg.clear()
        self.bb = [BbBreakDetector(c) for c in BB_BOOKS]
        self.fudkoi = FudkoiDetector()
        self.pivotboss = PivotBossDetector()
        self.rt = FudkiiRtDetector()
        self.reset_day_stamp = day
        self.dirty = True  # the saved session must empty too, or a restart would bring it back
        log.info("alerts.reset_day", day=day, cleared=cleared)
        return cleared

    def stats(self) -> dict[str, Any]:
        return {
            "resetDay": self.reset_day_stamp,
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
                # Nothing it reads has moved since the last pass: the same inputs give the same
                # numbers, so they are not recomputed — it re-derived every card of the day every
                # second (review, 2026-10-03). The marks are still current, and say so. The book's
                # age is an input too: the exit walks leave the ladder for the touch when it crosses
                # MAX_QUOTE_AGE_S with nothing else moving (a stalled feed, after the close).
                stale_book = (now - book.ts > entry_model.MAX_QUOTE_AGE_S) if book is not None else None
                inputs = (getattr(q, "ltp", None), getattr(q, "bid", None), getattr(q, "ask", None), round(float(spot), 4),
                          getattr(book, "ts", None), stale_book)
                ident = (a.book, a.symbol, a.ts, a.fired_at)
                if self._card_inputs.get(ident) == inputs:
                    card["marksTs"] = now
                    continue
                self._card_inputs[ident] = inputs
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
                # Time-boxed: the ring keeps 500 alerts a book all day, and renewing every one
                # of them every second would pin every contract ever carded onto the tape.
                if now - a.fired_at <= TAPE_CARD_TTL_S:
                    self.engine.tape.follow(a.symbol, [code], now=now)
                row = (ltp, getattr(q, "bid", None) if q else None, getattr(q, "ask", None) if q else None, spot, round(delta, 4))
                if self._quote_rows.get(code) != row:
                    # a row when the quote, the spot or the delta moved — not one a second per card
                    self._quote_rows[code] = row
                    self.engine.archive.option_quote(code, now, ltp=row[0], bid=row[1], ask=row[2], spot=spot, delta=delta)
                touched += 1
        self.last_refresh_ts = now
        self.refreshes += 1
        return touched
