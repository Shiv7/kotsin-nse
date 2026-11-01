"""The shadow runner: closed 5-minute bars in, the CPR retest sequence per symbol, a shadow book out.

The engine hands every closed 5m bar to :meth:`ShadowRunner.on_bar`. The runner rebuilds the day's
levels once per symbol per session from the 1d series (previous session → CPR and pivots; the last
50 sessions → ADR; the previous ISO week → weekly pivots), runs ``cpr_retest.sequence`` on the bars so
far, and on the confirmation bar submits an ENTRY through the gateway — in SHADOW the gateway records
the order and fills nothing, so the book here is the one that scores the trade: entry at the bar's
close, the three exit rules stepped on every bar after it, the primary rule's exit submitted as an
EXIT order, every row written to the ledger and to ``data_dir/strategy/<day>.json``.

Every decision is synchronous (nothing awaits between reading the bars and deciding); the ledger and
file writes are spawned afterwards, in order, off the tick path.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog

from ..bars.store import BarStore
from ..bars.unified import BarSource, UnifiedBar
from ..config import Segment, Settings
from ..domain import Instrument, InstrumentKind, OrderIntent, OrderSide, Purpose, new_id, to_json
from ..exec.gateway import Decision, Gateway
from ..instrument.universe import INDEX_ROOTS
from ..ledger.db import Ledger
from ..market.session import bucket_end, continuous_end_ts, ist_day, ist_hm, session_open_ts
from ..risk.costs import CostModel
from ..scan.replay import guarded_adr
from ..structure.engine import Bar5
from . import cpr_retest as cr

log = structlog.get_logger(__name__)

TF = "5m"
#: NSE cash since 2026-08-03: continuous trading in the F&O stocks ends here and a closing auction sets the
#: official close. A chart drawn from ticks (the owner's Kite) takes the last continuous trade as the previous
#: day's close — AXISBANK 26 Oct 2026: 1225.0, against the auction's 1220.0.
CONTINUOUS_END_HM = "15:15"
NIFTY = "NIFTY"
Spawn = Callable[[Coroutine[Any, Any, Any], str], Any]


@dataclass(slots=True)
class SymbolDay:
    """One symbol's session: its levels, and where each side of the sequence stands."""

    levels: dict[str, float]
    weekly: dict[str, float] | None
    adr: float | None
    tol: float
    #: the previous session's closes — the official one, the last traded, which one built the levels, its date
    closes: dict[str, Any] = field(default_factory=dict)
    daily: dict[int, dict[str, Any]] = field(default_factory=dict)
    stages: dict[int, cr.Stage] = field(default_factory=lambda: {1: cr.Stage(1), -1: cr.Stage(-1)})
    entered: set[int] = field(default_factory=set)
    last_ts: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"levels": self.levels, "weekly": self.weekly, "adr": self.adr, "tol": self.tol, "closes": self.closes,
                "stages": {("bullish" if s > 0 else "bearish"): st.to_json() for s, st in self.stages.items()}}


class ShadowRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        store: BarStore,
        gateway: Gateway,
        ledger: Ledger | None,
        costs: CostModel,
        underlyings: Mapping[str, Instrument] | Callable[[], Mapping[str, Instrument]],
        spawn: Spawn | None = None,
        segment: Segment = Segment.NSE_EQ,
    ) -> None:
        self.s = settings
        self.store = store
        self.gateway = gateway
        self.ledger = ledger
        self.costs = costs
        #: a getter, because the engine *replaces* its ``underlyings`` dict when the universe is built
        #: after boot (and again on a rebuild) — a reference taken at construction would stay empty
        self._underlyings = underlyings
        self.segment = segment
        self.rule = settings.strategy_rule
        self.filter = settings.strategy_filter
        self.notional = settings.strategy_notional_inr
        self.pdc = settings.strategy_pdc
        self.dir = settings.data_dir / "strategy"
        self._spawn: Spawn = spawn or (lambda coro, name: asyncio.create_task(coro, name=name))
        self.day = ""
        self.syms: dict[str, SymbolDay] = {}
        self.signals: list[dict[str, Any]] = []
        self.positions: dict[str, dict[str, Any]] = {}  # open, by position id
        self.trades: list[dict[str, Any]] = []
        self.plans: dict[str, dict[str, cr.ExitPlan]] = {}
        self.orders = 0
        self.errors = 0
        #: the live route (2026-11-01): entries the broker refused, exits that could not be filled
        self.live_rejected = 0
        self.exit_failures = 0
        self._writes: list[Awaitable[Any]] = []
        self._lock = asyncio.Lock()  # the writes of consecutive bars land in order, one at a time
        #: the engine's readings at the signal, on every signal (taken or not) for the evening read: the F&O board
        #: (the future's build-up and OI change, the calls' and puts' states, PCR, IV, the walls), the day's order
        #: flow so far (buy/sell, delta, OFI, imbalance) and the book's microstructure; ``bhav`` the previous
        #: session's official closing OI (NSE's build-up label). Set by the engine; None in tests and replays.
        self.enrich: Callable[[str], dict[str, Any]] | None = None
        self.bhav: Callable[[str, date], dict[str, Any] | None] | None = None

    @property
    def underlyings(self) -> Mapping[str, Instrument]:
        u = self._underlyings
        return u() if callable(u) else u

    # -- the day -----------------------------------------------------------------------------------------

    def _roll(self, day: date) -> None:
        if self.positions:  # a session that never saw its last bar: mark what is open at ITS day's last price
            for pos in list(self.positions.values()):
                own = [b for b in self.store.bars(pos["symbol"], TF) if ist_day(b.ts).isoformat() == self.day]
                self._close(pos, own[-1].close if own else pos["entry"], "END", time.time())
        if self.day and (self.signals or self.trades):
            # the old day's file gets its final state (the END trades included) BEFORE the reset:
            # with the reset first, a trade closed here never reached the day it belonged to
            self._writes.append(asyncio.to_thread(self._dump, self.snapshot(), self.day))
        self.day = day.isoformat()
        self.syms, self.signals, self.trades, self.plans = {}, [], [], {}
        self.positions = {}
        self._restored_entered: set[tuple[str, int]] = set()
        self._restore(day)

    def _restore(self, day: date) -> None:
        """A restart inside the session: the day's book comes back from ``<day>.json`` — signals,
        trades, the open positions and every rule's exit plan, stepped on from where they were.
        Before 2026-11-01 the first bar after a restart overwrote the file with an empty book and
        the open positions were orphaned (the 30 Oct 11:29 restart needed a hand-renamed file)."""
        snap = self.read_day(day.isoformat())
        if not snap or snap.get("day") != day.isoformat() or snap.get("strategy") != cr.NAME:
            return
        try:
            self.signals = list(reversed(snap.get("signals") or []))
            self.trades = list(reversed(snap.get("trades") or []))
            for p in snap.get("positions") or []:
                self.positions[p["id"]] = p
                for key in ("live", "live_exit"):
                    if (p.get(key) or {}).get("status") == "PENDING":
                        # the process died while the broker had the order: the reconciler decides what is real
                        p[key]["status"] = "UNKNOWN"
                        p[key]["note"] = "restart while the order was pending — reconcile against the broker"
            for pid, rules in (snap.get("plans") or {}).items():
                self.plans[pid] = {rule: cr.ExitPlan.from_state(st) for rule, st in rules.items()}
            self._restored_entered = {(s["symbol"], 1 if s["side"] == "LONG" else -1) for s in self.signals}
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("strategy.restore_failed", day=day.isoformat(), error=repr(exc))
            self.signals, self.trades, self.positions, self.plans = [], [], {}, {}
            return
        if self.signals or self.positions:
            log.info("strategy.restored", day=day.isoformat(), signals=len(self.signals), open=len(self.positions), trades=len(self.trades))

    def _today_bars(self, symbol: str, day: date) -> list[Bar5]:
        open_ts = session_open_ts(self.segment, day)
        return [Bar5(int(b.ts), b.open, b.high, b.low, b.close, b.volume) for b in self.store.bars(symbol, TF) if b.ts >= open_ts]

    def _symbol_day(self, symbol: str, day: date, price: float) -> SymbolDay | None:
        sd = self.syms.get(symbol)
        if sd is not None:
            return sd
        rows = self._sessions_before(symbol, day)
        if not rows:
            return None
        prev = rows[-1]
        prev_day = ist_day(prev.ts)
        last_trade = self._last_trade(symbol, prev_day)
        close = last_trade if self.pdc == "last" and last_trade is not None else prev.close
        hl = self._continuous_hl(symbol, prev_day) if (self.pdc == "last" and last_trade is not None and self.s.strategy_levels_hl == "continuous") else None
        high, low = hl if hl is not None else (prev.high, prev.low)
        levels = cr.day_levels(high, low, close)
        win = rows[-60:]
        adr = guarded_adr([b.high for b in win], [b.low for b in win], [b.close for b in win]) if len(win) >= 21 else None
        weeks: dict[tuple[int, int], list[UnifiedBar]] = {}
        for b in rows:
            weeks.setdefault(ist_day(b.ts).isocalendar()[:2], []).append(b)
        this_week = day.isocalendar()[:2]
        done = [k for k in weeks if k < this_week]
        weekly = None
        if done:
            g = weeks[max(done)]
            weekly = cr.weekly_pivots([(max(b.high for b in g), min(b.low for b in g), g[-1].close)])
        closes = {"official": prev.close, "last": last_trade, "used": self.pdc, "day": prev_day.isoformat(),
                  "hl": "continuous" if hl is not None else "daily", "high": high, "low": low, "high_official": prev.high, "low_official": prev.low}
        sd = SymbolDay(levels=levels, weekly=weekly, adr=adr, tol=cr.tolerance(adr, price), closes=closes)
        sd.entered = {side for sym, side in getattr(self, "_restored_entered", set()) if sym == symbol}
        self.syms[symbol] = sd
        return sd

    def _sessions_before(self, symbol: str, day: date) -> list[UnifiedBar]:
        """The finished sessions before ``day``, one bar per date, oldest first.

        Dates, not timestamps: a running day's candle the broker returned mid-session once sat at 09:14
        (NIFTY, 2026-10-27) and was "the last 1d bar before the open" — that day's CPR was its own first
        5m bar. The store can also hold two bars for one date, the broker's candle at midnight and the
        live 1d bar the aggregator closed at 15:30; the broker's wins."""
        by_day: dict[date, UnifiedBar] = {}
        for b in self.store.bars(symbol, "1d"):
            d = ist_day(b.ts)
            if d >= day or b.high <= 0 or b.low <= 0:
                continue
            cur = by_day.get(d)
            if cur is None or (cur.source is not BarSource.REST and b.source is BarSource.REST):
                by_day[d] = b
        return [by_day[d] for d in sorted(by_day)]

    def _continuous_hl(self, symbol: str, d: date) -> tuple[float, float] | None:
        """Session ``d``'s high and low from continuous trading: its 30m bars that began before the closing
        auction, all twelve of them (a gap → None, and the daily candle stands)."""
        bars = [b for b in self.store.bars(symbol, "30m") if ist_day(b.ts) == d and ist_hm(b.ts) < CONTINUOUS_END_HM]
        if len(bars) < 12:
            return None
        return max(b.high for b in bars), min(b.low for b in bars)

    def _last_trade(self, symbol: str, d: date) -> float | None:
        """The last continuous trade of session ``d`` for a stock: the close of its last 30m bar that began
        before the closing auction. ``None`` for an index, whose official close is its last print."""
        if symbol in INDEX_ROOTS:
            return None
        bars = [b for b in self.store.bars(symbol, "30m") if ist_day(b.ts) == d and ist_hm(b.ts) < CONTINUOUS_END_HM]
        return bars[-1].close if bars else None

    # -- bars ------------------------------------------------------------------------------------------

    async def on_bar(self, bar: UnifiedBar) -> None:
        """A closed bar from the aggregator. Decides synchronously; persists afterwards."""
        if bar.tf != TF:
            return
        inst = self.underlyings.get(bar.symbol)
        if inst is None or inst.kind is not InstrumentKind.EQUITY or inst.segment is not self.segment:
            return
        try:
            self._on_bar(bar, inst)
        except Exception as exc:  # noqa: BLE001 - one symbol's failure must not stop the others' bars
            self.errors += 1
            log.warning("strategy.bar_failed", symbol=bar.symbol, ts=bar.ts, error=repr(exc))
        self._flush_writes()

    def _on_bar(self, bar: UnifiedBar, inst: Instrument) -> None:
        day = ist_day(bar.ts)
        if day.isoformat() != self.day:
            self._roll(day)
        sd = self._symbol_day(bar.symbol, day, bar.close)
        if sd is None:
            return
        if bar.ts <= sd.last_ts:
            # the reconciler installing an older bucket is not a new bar for the sequence (it is
            # recomputed from the store on the next one) — but a position that was open across it
            # never saw it: if its range crossed the initial stop, the trade was over then
            if self._late_bar(bar):
                self._writes.append(self._write_day())
            return
        sd.last_ts = int(bar.ts)
        bars = self._today_bars(bar.symbol, day)
        if not bars:
            return
        # the session's last decision bar is the one that ends continuous trading (15:10 for NSE
        # cash, whose 15:15 auction nothing can be sold into), not the one that ends at the close
        last = bucket_end(self.segment, bar.ts, TF) >= continuous_end_ts(self.segment, day)
        changed = False
        # 1. the positions on this symbol — open ones, and closed ones whose other rules are still being scored
        for pos in [p for p in [*self.positions.values(), *self.trades] if p["symbol"] == bar.symbol and p["id"] in self.plans]:
            changed |= self._manage(pos, bar, last)
        # 2. the sequence on the bars so far; the confirmation bar is this one → entry
        sd.stages = cr.sequence(bars, sd.levels, sd.tol, open_ts=int(session_open_ts(self.segment, day)))
        n = len(bars)
        for side, st in sd.stages.items():
            if st.j is None or st.j != n - 1 or side in sd.entered or last:
                continue
            self._enter(inst, sd, bars, side, st, bar)
            sd.entered.add(side)
            changed = True
        if last:
            changed |= self._eod_sweep(bar)
        if changed:
            self._writes.append(self._write_day())

    def _late_bar(self, bar: UnifiedBar) -> bool:
        """An older bucket installed after the positions on its symbol were stepped past it. The
        plans cannot be re-run in order, so the honest reading is the one that cannot be wrong:
        a range through the INITIAL stop (never further from the entry than any later stop)
        means the stop was hit on that bar at the latest."""
        changed = False
        for pos in [p for p in self.positions.values() if p["symbol"] == bar.symbol]:
            if not (pos["opened_ts"] - 300 < bar.ts < pos.get("last_stepped_ts", 0)):
                continue
            pos["late_bars"] = pos.get("late_bars", 0) + 1
            side = 1 if pos["side"] == "LONG" else -1
            touched = bar.low <= pos["initial_stop"] if side > 0 else bar.high >= pos["initial_stop"]
            log.warning("strategy.late_bar", symbol=bar.symbol, ts=bar.ts, position=pos["id"], stop_touched=touched)
            if touched:
                pos["note"] = f"stopped on the {ist_hm(bar.ts)} bar the feed delivered late"
                self._close(pos, pos["initial_stop"], "stop", int(bar.ts) + 300)
            changed = True
        return changed

    # -- entries -----------------------------------------------------------------------------------------

    def _enter(self, inst: Instrument, sd: SymbolDay, bars: list[Bar5], side: int, st: cr.Stage, bar: UnifiedBar) -> None:
        j, i_rt = int(st.j or 0), int(st.i_rt or 0)
        entry = bars[j].c
        stop = (bars[i_rt].l - sd.tol) if side > 0 else (bars[i_rt].h + sd.tol)
        risk_bps = abs(entry - stop) / entry * 1e4
        ws = cr.walls(side, entry, sd.levels, sd.weekly, sd.tol, use_round=self.s.strategy_walls_round)
        near = cr.nearest_level(side, entry, sd.levels, sd.weekly)
        signal_id = new_id("sig")
        tags = self._tags(inst.symbol, sd, bars, side, j, bar)
        ctx, bhav = self._context(inst.symbol, sd)
        sig: dict[str, Any] = {
            # ``ts`` is the bar's close; ``decided_ts`` the wall clock of the decision (5–25 s later, after the
            # exchange's candle) — both, as LEARNINGS asks, so a fill can be read against the instant it was ordered
            "id": signal_id, "ts": int(bar.ts) + 300, "decided_ts": round(time.time(), 3), "time": ist_hm(int(bar.ts) + 300), "bar": ist_hm(int(bar.ts)),
            "symbol": inst.symbol, "side": "LONG" if side > 0 else "SHORT",
            "entry": entry, "stop": stop, "risk_bps": round(risk_bps, 1), "walls": [{"name": k, "price": v, "bps": round(abs(v - entry) / entry * 1e4, 1)} for k, v in ws],
            "stage": st.to_json(), "retest_time": ist_hm(bars[i_rt].ts), "tol": sd.tol, "adr": sd.adr, "tags": tags, "taken": False, "why": "",
            "context": ctx, "bhav": bhav,
            # the nearest real level beyond the entry at any distance, and its distance in R: the one inside the
            # tolerance is the one that stops the trade (the 27 Oct BANKNIFTY short, S1 32 points below)
            "nearest": {"name": near[0], "price": near[1], "bps": round(abs(near[1] - entry) / entry * 1e4, 1), "r": round(abs(near[1] - entry) / entry * 1e4 / risk_bps, 2) if risk_bps > 0 else None} if near else None,
        }
        self.signals.append(sig)
        if inst.symbol in INDEX_ROOTS:
            # day 1 (2026-10-27) booked NIFTY and BANKNIFTY as ₹25,000 of "stock": the index roots carry an
            # NSE_EQ leg in the catalogue. The sequence stays visible; the book is cash equities only.
            sig["why"] = "index: no cash leg to trade — recorded, not booked"
            return
        if risk_bps <= 0:
            sig["why"] = "no risk: the stop is at the entry"
            return
        if not ws:
            sig["why"] = "no wall beyond the entry by a tolerance"
            return
        mv = tags.get("move_adr")
        if self.s.strategy_max_move_adr > 0 and mv is not None and mv >= self.s.strategy_max_move_adr:
            sig["why"] = f"moved {mv:.2f} ADR from the open already (cap {self.s.strategy_max_move_adr})"
            return
        cutoff = self.s.strategy_entry_cutoff
        if cutoff and sig["bar"] >= cutoff:
            sig["why"] = f"entry bar {sig['bar']} at or after the cutoff {cutoff}: too late to reach a wall"
            return
        if not cr.passes(self.filter, tags):
            sig["why"] = f"filter {self.filter!r} not met"
            return
        qty = max(inst.qty_step, int(self.notional / entry) // inst.qty_step * inst.qty_step)
        pos_id = new_id("pos")
        intent = OrderIntent(
            strategy=cr.NAME, instrument=inst, side=OrderSide.BUY if side > 0 else OrderSide.SELL, qty=qty, purpose=Purpose.ENTRY,
            # ≤ 38 characters: the broker's RemoteOrderID limit (sig-… 16 + |E| 3 + pos-… 16 = 35)
            signal_id=signal_id, client_order_id=f"{signal_id}|E|{pos_id}", position_id=pos_id, ref_price=entry,
            reason=f"CPR retest held {sig['retest_time']} → first close beyond: the {sig['bar']} bar (fires {sig['time']}); stop {stop:.2f}, next wall {ws[0][0]} {ws[0][1]:.2f}",
        )
        res = self.gateway.submit(intent)
        self.orders += 1
        self._writes.append(self._ledger_order(res.order, res.decision.value))
        if res.decision.value.startswith("REJECTED") or res.decision.value == "DUP_BLOCKED":
            sig["why"] = f"gateway {res.decision.value}: {res.order.note}"
            return
        # the live route answers SUBMITTED at once: the book is scored on the rule's price from here, and the
        # broker's actual fill lands beside it through ``on_live_result`` (slippage, latency, or a refusal)
        live = (
            {"status": "PENDING", "client_order_id": intent.client_order_id, "sent_ts": round(time.time(), 3), "ref": entry, "qty_sent": qty}
            if res.decision is Decision.SUBMITTED
            else None
        )
        fill = res.fill.price if res.fill is not None else entry
        if res.fill is not None and 0 < res.fill.qty < qty:
            # PAPER walked the book and stopped at the 10 % ceiling: the position is what filled,
            # not what was asked — the exit must not sell shares the book never bought
            sig["truncated"] = {"asked": qty, "filled": res.fill.qty}
            qty = res.fill.qty
        sig["taken"] = True
        plans = {rule: cr.ExitPlan.open(rule, side, fill, stop, [v for _k, v in ws], sd.tol) for rule in cr.RULES}
        primary = plans[self.rule]
        pos: dict[str, Any] = {
            "id": pos_id, "strategy": cr.NAME, "symbol": inst.symbol, "scrip_code": inst.scrip_code, "status": "OPEN", "day": self.day,
            "side": sig["side"], "qty": qty, "entry": fill, "initial_stop": stop, "stop": primary.stop_price(), "target": primary.target_price(),
            "target_name": ws[0][0], "walls": sig["walls"], "risk_bps": round(abs(fill - stop) / fill * 1e4, 1),
            "reward_bps": round(abs(ws[0][1] - fill) / fill * 1e4, 1), "rule": self.rule, "signal_id": signal_id, "opened_ts": int(bar.ts) + 300,
            "entry_time": sig["time"], "entry_bar": sig["bar"], "retest_time": sig["retest_time"], "mode": res.order.mode, "tags": tags, "tol": sd.tol,
            "context": ctx, "bhav": bhav, "last_stepped_ts": int(bar.ts),
            "bars_held": 0, "mfe_bps": 0.0, "mae_bps": 0.0, "last": fill, "pnl_bps": 0.0, "closed_ts": None, "exit": None, "exit_reason": None,
            "gross_inr": None, "charges_inr": None, "net_inr": None, "r": None, "virtual": {},
            "live": live, "live_exit": None,
        }
        self.positions[pos_id] = pos
        self.plans[pos_id] = plans
        self._writes.append(self._ledger_position(pos))
        self._writes.append(self._ledger_event("strategy.entry", {k: pos[k] for k in ("id", "symbol", "side", "qty", "entry", "stop", "target", "target_name", "risk_bps", "reward_bps", "mode")}))
        log.info("strategy.entry", symbol=inst.symbol, side=pos["side"], entry=fill, stop=stop, target=ws[0], mode=res.order.mode)

    def _context(self, symbol: str, sd: SymbolDay) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """The engine's readings at the signal and the previous session's official OI. A reader that fails is
        logged; it never blocks the signal (the readings are for the evening, the sequence is the strategy)."""
        ctx: dict[str, Any] = {}
        bhav = None
        if self.enrich is not None:
            try:
                ctx = self.enrich(symbol) or {}
            except Exception as exc:  # noqa: BLE001 - enrichment never blocks the book
                log.warning("strategy.enrich_failed", symbol=symbol, error=repr(exc))
        prev = sd.closes.get("day")
        if self.bhav is not None and prev:
            try:
                bhav = self.bhav(symbol, date.fromisoformat(prev))
            except Exception as exc:  # noqa: BLE001
                log.warning("strategy.bhav_failed", symbol=symbol, error=repr(exc))
        return ctx, bhav

    def _tags(self, symbol: str, sd: SymbolDay, bars: list[Bar5], side: int, j: int, bar: UnifiedBar) -> dict[str, Any]:
        day = ist_day(bar.ts)
        open_ts = session_open_ts(self.segment, day)
        if side not in sd.daily:
            rows = [b for b in self.store.bars(symbol, "1d") if b.ts < open_ts and b.high > 0]
            nifty = [b.close for b in self.store.bars(NIFTY, "1d") if b.ts < open_ts] or None
            sd.daily[side] = cr.daily_tags(side, [b.close for b in rows], [b.high for b in rows], [b.low for b in rows], nifty)
        tags = dict(sd.daily[side])
        tags.update(self._st60(symbol, side, bar))
        nifty_bars = [Bar5(int(b.ts), b.open, b.high, b.low, b.close, b.volume) for b in self.store.bars(NIFTY, TF) if b.ts >= open_ts] or None
        tags.update(cr.intraday_tags(side, bars, j, sd.adr, nifty_bars, self._peers_move(symbol, day, side, j)))
        tags["owner_stack"] = bool(tags.get("st60_al") and tags.get("dma20_al"))
        return tags

    def _st60(self, symbol: str, side: int, bar: UnifiedBar) -> dict[str, Any]:
        """SuperTrend(10, 3) on hourly bars built from the closed 30m series, up to the hour completed before this bar."""
        h30 = [b for b in self.store.bars(symbol, "30m") if bucket_end(self.segment, b.ts, "30m") <= bar.ts + 300]
        groups: dict[tuple[str, int], list[UnifiedBar]] = {}
        for b in h30:
            d = ist_day(b.ts)
            groups.setdefault((d.isoformat(), int((b.ts - session_open_ts(self.segment, d)) // 3600)), []).append(b)
        hs, ls, cs = [], [], []
        for (_d, hour), g in sorted(groups.items()):
            if len(g) < 2 and hour < 6:
                continue  # an hour still forming
            hs.append(max(x.high for x in g))
            ls.append(min(x.low for x in g))
            cs.append(g[-1].close)
        d = cr.supertrend_dir(hs, ls, cs)
        return {"st60": d, "st60_al": d == side}

    def _peers_move(self, symbol: str, day: date, side: int, j: int) -> list[float]:
        out: list[float] = []
        for sym, inst in self.underlyings.items():
            if sym == symbol or inst.kind is not InstrumentKind.EQUITY or sym in INDEX_ROOTS:
                continue
            pb = self._today_bars(sym, day)
            if len(pb) <= j or pb[0].o <= 0:
                continue
            psd = self._symbol_day(sym, day, pb[j].c)
            if psd is None or not psd.adr:
                continue
            out.append(side * (pb[j].c / pb[0].o - 1) * 100 / (psd.adr / pb[j].c * 100))
        return out

    # -- exits -------------------------------------------------------------------------------------------

    def _manage(self, pos: dict[str, Any], bar: UnifiedBar, last: bool) -> bool:
        """Step every rule's plan on this bar. The primary rule's exit closes the position; the other rules
        keep running to their own end, as the research scored them, and land in ``virtual``."""
        plans = self.plans.get(pos["id"])
        if plans is None:
            return False
        # every closed bar since the one last stepped, in order, this one last: a bucket the sweep
        # installed between two live bars is stepped too (a stop touched inside it is not missed)
        since = int(pos.get("last_stepped_ts") or pos["opened_ts"] - 300)
        todo = [b for b in self.store.bars(pos["symbol"], TF) if since < b.ts < bar.ts]
        todo.append(bar)
        exit_now: tuple[float, str] | None = None
        exit_ts = int(bar.ts)
        for b in todo:
            pos["last_stepped_ts"] = int(b.ts)
            for rule, plan in plans.items():
                res = plan.step(b.high, b.low, b.close, last=last and b is bar, open=b.open)
                if res is not None:
                    pos["virtual"][rule] = plan.to_json()
                if rule == self.rule and res is not None and exit_now is None:
                    exit_now, exit_ts = res, int(b.ts)
            if exit_now is not None and all(pl.done is not None for pl in plans.values()):
                break
        if all(pl.done is not None for pl in plans.values()):
            self.plans.pop(pos["id"], None)
        if pos["status"] != "OPEN":
            self._writes.append(self._ledger_position(pos))
            return True
        primary = plans[self.rule]
        pos["bars_held"] = primary.bars
        pos["stop"], pos["target"] = primary.stop_price(), primary.target_price()
        pos["mfe_bps"] = round((primary.hh - primary.side * primary.entry) / primary.entry * 1e4, 1)
        pos["mae_bps"] = round((primary.ll - primary.side * primary.entry) / primary.entry * 1e4, 1)
        pos["last"] = bar.close
        pos["pnl_bps"] = round(primary.pnl_bps(bar.close), 1)
        if exit_now is None:
            self._writes.append(self._ledger_position(pos))
            return True
        if primary.gapped:
            pos["gapped"] = True
        self._close(pos, exit_now[0], exit_now[1], exit_ts + 300)
        return True

    def _close(self, pos: dict[str, Any], exit_px: float, how: str, ts: float) -> None:
        inst = self.underlyings.get(pos["symbol"])
        side = 1 if pos["side"] == "LONG" else -1
        if inst is not None:
            intent = OrderIntent(
                strategy=cr.NAME, instrument=inst, side=OrderSide.SELL if side > 0 else OrderSide.BUY, qty=pos["qty"], purpose=Purpose.EXIT,
                signal_id=pos["signal_id"], client_order_id=f"{pos['signal_id']}|X|{pos['id']}", position_id=pos["id"], ref_price=exit_px,
                reason=f"{how} at {exit_px:.2f}",
            )
            res = self.gateway.submit(intent)
            self.orders += 1
            self._writes.append(self._ledger_order(res.order, res.decision.value))
            if res.decision is Decision.SUBMITTED:
                pos["live_exit"] = {"status": "PENDING", "client_order_id": intent.client_order_id, "sent_ts": round(time.time(), 3), "ref": exit_px, "qty_sent": pos["qty"]}
            if res.fill is not None:
                exit_px = res.fill.price
            # a short's entry is the SELL: STT on it, stamp duty on the exit — not the other way round
            charges = self.costs.round_trip(inst, pos["entry"], exit_px, pos["qty"], entry_side=OrderSide.BUY if side > 0 else OrderSide.SELL).total
        else:
            charges = 0.0
        gross = (exit_px - pos["entry"]) * side * pos["qty"]
        risk = abs(pos["entry"] - pos["initial_stop"])
        pos.update({
            "status": "CLOSED", "closed_ts": ts, "exit": exit_px, "exit_reason": how, "pnl_bps": round((exit_px - pos["entry"]) * side / pos["entry"] * 1e4, 1),
            "gross_inr": round(gross, 2), "charges_inr": round(charges, 2), "net_inr": round(gross - charges, 2),
            "r": round((exit_px - pos["entry"]) * side / risk, 2) if risk > 0 else None, "last": exit_px,
        })
        plans = self.plans.get(pos["id"], {})
        for rule, plan in plans.items():
            if plan.done is not None:
                pos["virtual"][rule] = plan.to_json()
        if how == "END":  # the session ended without its last bar: nothing more will be scored
            for rule, plan in plans.items():
                if plan.done is None:
                    plan.done = (exit_px, "END")
                pos["virtual"][rule] = plan.to_json()
            self.plans.pop(pos["id"], None)
        self.positions.pop(pos["id"], None)
        self.trades.append(pos)
        trade = {**pos, "id": new_id("trd"), "position_id": pos["id"], "underlying": pos["symbol"], "net": pos["net_inr"], "charges": charges,
                 "r_multiple": pos["r"] or 0.0, "exit_reason": how}
        self._writes.append(self._ledger_position(pos))
        self._writes.append(self._ledger_trade(trade))
        self._writes.append(self._ledger_event("strategy.exit", {k: pos[k] for k in ("id", "symbol", "side", "entry", "exit", "exit_reason", "pnl_bps", "net_inr", "r")}))
        log.info("strategy.exit", symbol=pos["symbol"], side=pos["side"], how=how, exit=exit_px, pnl_bps=pos["pnl_bps"])

    def _eod_sweep(self, bar: UnifiedBar) -> bool:
        """The session's last bucket closed for one symbol: a position whose own last bar never came is
        marked at its last close rather than left open overnight."""
        changed = False
        end_ts = continuous_end_ts(self.segment, ist_day(bar.ts))
        for pos in list(self.positions.values()):
            if pos["symbol"] == bar.symbol:
                continue
            last = self.store.last(pos["symbol"], TF)
            if last is not None and bucket_end(self.segment, last.ts, TF) >= end_ts:
                self._close(pos, last.close, "close", int(bar.ts) + 300)
                changed = True
        return changed

    # -- the live route's answers ------------------------------------------------------------------------

    def on_live_result(self, client_order_id: str, res: Any) -> None:
        """The live route reports back (``Engine._place_live``): the broker's actual fill, or its refusal, for an
        order ``submit`` answered SUBMITTED. The book was scored on the rule's price when the order went out; the
        fill is recorded beside it (price, quantity, slippage, latency). An entry the broker refused is removed —
        no position exists. An exit that failed is marked loudly; the engine has already alerted and halted, and
        the reconciler sees the real position."""
        is_exit = "|X|" in client_order_id
        key = "live_exit" if is_exit else "live"
        pos = next((p for p in [*self.positions.values(), *self.trades] if (p.get(key) or {}).get("client_order_id") == client_order_id), None)
        if pos is None:
            log.warning("strategy.live_result_unmatched", client_order_id=client_order_id)
            return
        o, fill = res.order, res.fill
        rec = dict(pos.get(key) or {})
        rec.update({"ts": round(time.time(), 3), "broker_order_id": getattr(o, "broker_order_id", ""), "note": getattr(o, "note", ""),
                    "order_status": getattr(o, "status", ""), **(getattr(res, "meta", None) or {})})
        side = 1 if pos["side"] == "LONG" else -1
        if fill is not None:
            ref = rec.get("ref") or pos["entry"]
            adverse = side if not is_exit else -side  # positive slippage = paid more / received less than the rule's price
            rec.update({"status": "FILLED", "price": fill.price, "qty": fill.qty, "charges": round(fill.charges, 2),
                        "slippage_bps": round((fill.price - ref) / ref * 1e4 * adverse, 2) if ref else None})
        else:
            rec.update({"status": "FAILED" if is_exit else "REJECTED", "reason": getattr(o, "note", "")})
        pos[key] = rec
        if not is_exit:
            if fill is None:
                self.positions.pop(pos["id"], None)
                self.plans.pop(pos["id"], None)
                for sig in self.signals:
                    if sig["id"] == pos["signal_id"]:
                        sig["taken"] = False
                        sig["why"] = f"live entry refused: {rec.get('reason', '')}"
                        sig["live"] = f"REJECTED: {rec.get('reason', '')}"
                self.live_rejected += 1
                log.error("strategy.live_entry_rejected", symbol=pos["symbol"], position=pos["id"], reason=rec.get("reason"))
                self._writes.append(self._ledger_event("strategy.live_entry_rejected", {"position": pos["id"], "symbol": pos["symbol"], "reason": rec.get("reason")}))
            else:
                if 0 < fill.qty < pos["qty"]:
                    rec["note"] = f"partial: {fill.qty} of {pos['qty']} filled; the book holds the filled quantity"
                    pos["qty"] = fill.qty
                log.info("strategy.live_entry", symbol=pos["symbol"], position=pos["id"], price=fill.price, qty=fill.qty, slippage_bps=rec.get("slippage_bps"), via=rec.get("via"))
                self._writes.append(self._ledger_event("strategy.live_entry", {"position": pos["id"], "symbol": pos["symbol"], "price": fill.price, "qty": fill.qty, "slippage_bps": rec.get("slippage_bps"), "latency_ms": rec.get("latency_ms")}))
        elif fill is not None:
            entry_px = (pos.get("live") or {}).get("price") or pos["entry"]
            entry_ch = float((pos.get("live") or {}).get("charges") or 0.0)
            pos["live_net_inr"] = round((fill.price - entry_px) * side * fill.qty - entry_ch - fill.charges, 2)
            log.info("strategy.live_exit", symbol=pos["symbol"], position=pos["id"], price=fill.price, qty=fill.qty, net_inr=pos["live_net_inr"], slippage_bps=rec.get("slippage_bps"))
            self._writes.append(self._ledger_event("strategy.live_exit", {"position": pos["id"], "symbol": pos["symbol"], "price": fill.price, "qty": fill.qty, "net_inr": pos["live_net_inr"], "slippage_bps": rec.get("slippage_bps")}))
        else:
            self.exit_failures += 1
            log.error("strategy.live_exit_failed", symbol=pos["symbol"], position=pos["id"], reason=rec.get("reason"))
            self._writes.append(self._ledger_event("strategy.live_exit_failed", {"position": pos["id"], "symbol": pos["symbol"], "reason": rec.get("reason")}))
        self._writes.append(self._ledger_position(pos))
        self._writes.append(self._write_day())
        self._flush_writes()

    def live_summary(self) -> dict[str, Any]:
        allp = [*self.positions.values(), *self.trades]
        ent = [(p.get("live") or {}).get("status") for p in allp]
        ex = [(p.get("live_exit") or {}).get("status") for p in allp]
        return {
            "pending": ent.count("PENDING") + ex.count("PENDING"), "filled": ent.count("FILLED"), "unknown": ent.count("UNKNOWN") + ex.count("UNKNOWN"),
            "rejected": self.live_rejected, "exit_filled": ex.count("FILLED"), "exit_failed": ex.count("FAILED"),
            "net_inr": round(sum(p.get("live_net_inr") or 0 for p in allp), 2),
        }

    # -- persistence -----------------------------------------------------------------------------------

    def _flush_writes(self) -> None:
        if not self._writes:
            return
        writes, self._writes = self._writes, []

        async def run() -> None:
            async with self._lock:
                for w in writes:
                    try:
                        await w
                    except Exception as exc:  # noqa: BLE001 - a failed write is logged; the book in memory stands
                        log.warning("strategy.write_failed", error=repr(exc))

        self._spawn(run(), "strategy.persist")

    async def _ledger_order(self, order: Any, decision: str) -> None:
        if self.ledger is not None:
            await self.ledger.insert_order(to_json(order), decision)

    async def _ledger_position(self, pos: dict[str, Any]) -> None:
        if self.ledger is not None:
            await self.ledger.upsert_position(pos)

    async def _ledger_trade(self, trade: dict[str, Any]) -> None:
        if self.ledger is not None:
            await self.ledger.insert_trade(trade)

    async def _ledger_event(self, kind: str, data: dict[str, Any]) -> None:
        if self.ledger is not None:
            await self.ledger.event(kind, data)

    async def _write_day(self) -> None:
        snap = self.snapshot()
        await asyncio.to_thread(self._dump, snap, self.day)

    def _dump(self, snap: dict[str, Any], day: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{day}.json.tmp"
        tmp.write_text(json.dumps(snap, default=str))
        tmp.replace(self.dir / f"{day}.json")

    def read_day(self, day: str) -> dict[str, Any] | None:
        p = self.dir / f"{day}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text())

    # -- the view --------------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        closed = self.trades
        wins = [t for t in closed if (t.get("pnl_bps") or 0) > 0]
        by_rule: dict[str, dict[str, Any]] = {}
        for rule in cr.RULES:
            xs = [t["virtual"][rule]["pnl_bps"] for t in closed if rule in t.get("virtual", {}) and t["virtual"][rule].get("pnl_bps") is not None]
            by_rule[rule] = {"closed": len(xs), "avg_pnl_bps": round(sum(xs) / len(xs), 1) if xs else None,
                             "win_pct": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1) if xs else None,
                             "exits": {how: sum(1 for t in closed if t.get("virtual", {}).get(rule, {}).get("how") == how) for how in ("stop", "target", "trail", "close")}}
        stages = []
        for sym, sd in sorted(self.syms.items()):
            for side, st in sd.stages.items():
                if st.name != "none":
                    stages.append({"symbol": sym, "side": "bullish" if side > 0 else "bearish", **st.to_json(), "tol": round(sd.tol, 2), "levels": sd.levels, "closes": sd.closes})
        taken = [s for s in self.signals if s["taken"]]
        return {
            "strategy": cr.NAME, "day": self.day, "rule": self.rule, "filter": self.filter, "pdc": self.pdc, "notional_inr": self.notional,
            "walls_round": self.s.strategy_walls_round, "max_move_adr": self.s.strategy_max_move_adr, "levels_hl": self.s.strategy_levels_hl, "entry_cutoff": self.s.strategy_entry_cutoff,
            "universe": sum(1 for sym, i in self.underlyings.items() if i.kind is InstrumentKind.EQUITY and sym not in INDEX_ROOTS), "with_levels": len(self.syms),
            "orders": self.orders, "errors": self.errors,
            "summary": {
                "signals": len(self.signals), "taken": len(taken), "skipped": len(self.signals) - len(taken), "open": len(self.positions), "closed": len(closed),
                "wins": len(wins), "losses": sum(1 for t in closed if (t.get("pnl_bps") or 0) < 0),
                "avg_pnl_bps": round(sum(t["pnl_bps"] for t in closed) / len(closed), 1) if closed else None,
                "sum_gross_inr": round(sum(t["gross_inr"] or 0 for t in closed), 2), "sum_net_inr": round(sum(t["net_inr"] or 0 for t in closed), 2),
                "sum_r": round(sum(t["r"] or 0 for t in closed), 2), "open_pnl_bps": round(sum(p["pnl_bps"] for p in self.positions.values()), 1),
                "by_rule": by_rule,
                "live": self.live_summary(),
            },
            "stages": stages, "signals": list(reversed(self.signals)), "positions": sorted(self.positions.values(), key=lambda p: -p["opened_ts"]),
            "trades": list(reversed(closed)),
            # every rule's exit plan still running, so a restart steps on from here (``_restore``)
            "plans": {pid: {rule: plan.state() for rule, plan in rules.items()} for pid, rules in self.plans.items()},
        }


__all__ = ["TF", "ShadowRunner", "SymbolDay"]
