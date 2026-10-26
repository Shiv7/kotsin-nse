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
from ..bars.unified import UnifiedBar
from ..config import Segment, Settings
from ..domain import Instrument, InstrumentKind, OrderIntent, OrderSide, Purpose, new_id, to_json
from ..exec.gateway import Gateway
from ..ledger.db import Ledger
from ..market.session import bucket_end, ist_day, ist_hm, session_close_ts, session_open_ts
from ..risk.costs import CostModel
from ..scan.replay import guarded_adr
from ..structure.engine import Bar5
from . import cpr_retest as cr

log = structlog.get_logger(__name__)

TF = "5m"
NIFTY = "NIFTY"
Spawn = Callable[[Coroutine[Any, Any, Any], str], Any]


@dataclass(slots=True)
class SymbolDay:
    """One symbol's session: its levels, and where each side of the sequence stands."""

    levels: dict[str, float]
    weekly: dict[str, float] | None
    adr: float | None
    tol: float
    daily: dict[int, dict[str, Any]] = field(default_factory=dict)
    stages: dict[int, cr.Stage] = field(default_factory=lambda: {1: cr.Stage(1), -1: cr.Stage(-1)})
    entered: set[int] = field(default_factory=set)
    last_ts: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"levels": self.levels, "weekly": self.weekly, "adr": self.adr, "tol": self.tol,
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
        underlyings: Mapping[str, Instrument],
        spawn: Spawn | None = None,
        segment: Segment = Segment.NSE_EQ,
    ) -> None:
        self.s = settings
        self.store = store
        self.gateway = gateway
        self.ledger = ledger
        self.costs = costs
        self.underlyings = underlyings
        self.segment = segment
        self.rule = settings.strategy_rule
        self.filter = settings.strategy_filter
        self.notional = settings.strategy_notional_inr
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
        self._writes: list[Awaitable[Any]] = []
        self._lock = asyncio.Lock()  # the writes of consecutive bars land in order, one at a time

    # -- the day -----------------------------------------------------------------------------------------

    def _roll(self, day: date) -> None:
        if self.positions:  # a session that never saw its last bar: mark what is open at the last price
            for pos in list(self.positions.values()):
                last = self.store.last(pos["symbol"], TF)
                self._close(pos, last.close if last else pos["entry"], "END", time.time())
        self.day = day.isoformat()
        self.syms, self.signals, self.trades, self.plans = {}, [], [], {}
        self.positions = {}

    def _today_bars(self, symbol: str, day: date) -> list[Bar5]:
        open_ts = session_open_ts(self.segment, day)
        return [Bar5(int(b.ts), b.open, b.high, b.low, b.close, b.volume) for b in self.store.bars(symbol, TF) if b.ts >= open_ts]

    def _symbol_day(self, symbol: str, day: date, price: float) -> SymbolDay | None:
        sd = self.syms.get(symbol)
        if sd is not None:
            return sd
        open_ts = session_open_ts(self.segment, day)
        rows = [b for b in self.store.bars(symbol, "1d") if b.ts < open_ts and b.high > 0 and b.low > 0]
        if not rows:
            return None
        prev = rows[-1]
        levels = cr.day_levels(prev.high, prev.low, prev.close)
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
        sd = SymbolDay(levels=levels, weekly=weekly, adr=adr, tol=cr.tolerance(adr, price))
        self.syms[symbol] = sd
        return sd

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
            return  # the reconciler re-installing an older bucket is not a new bar
        sd.last_ts = int(bar.ts)
        bars = self._today_bars(bar.symbol, day)
        if not bars:
            return
        last = bucket_end(self.segment, bar.ts, TF) >= session_close_ts(self.segment, day)
        changed = False
        # 1. the positions on this symbol — open ones, and closed ones whose other rules are still being scored
        for pos in [p for p in [*self.positions.values(), *self.trades] if p["symbol"] == bar.symbol and p["id"] in self.plans]:
            changed |= self._manage(pos, bar, last)
        # 2. the sequence on the bars so far; the confirmation bar is this one → entry
        sd.stages = cr.sequence(bars, sd.levels, sd.tol)
        n = len(bars)
        for side, st in sd.stages.items():
            if st.j is None or st.j != n - 1 or side in sd.entered or last:
                continue
            sd.entered.add(side)
            self._enter(inst, sd, bars, side, st, bar)
            changed = True
        if last:
            changed |= self._eod_sweep(bar)
        if changed:
            self._writes.append(self._write_day())

    # -- entries -----------------------------------------------------------------------------------------

    def _enter(self, inst: Instrument, sd: SymbolDay, bars: list[Bar5], side: int, st: cr.Stage, bar: UnifiedBar) -> None:
        j, i_rt = int(st.j or 0), int(st.i_rt or 0)
        entry = bars[j].c
        stop = (bars[i_rt].l - sd.tol) if side > 0 else (bars[i_rt].h + sd.tol)
        risk_bps = abs(entry - stop) / entry * 1e4
        ws = cr.walls(side, entry, sd.levels, sd.weekly, sd.tol)
        signal_id = new_id("sig")
        tags = self._tags(inst.symbol, sd, bars, side, j, bar)
        sig: dict[str, Any] = {
            "id": signal_id, "ts": int(bar.ts) + 300, "time": ist_hm(int(bar.ts) + 300), "symbol": inst.symbol, "side": "LONG" if side > 0 else "SHORT",
            "entry": entry, "stop": stop, "risk_bps": round(risk_bps, 1), "walls": [{"name": k, "price": v, "bps": round(abs(v - entry) / entry * 1e4, 1)} for k, v in ws],
            "stage": st.to_json(), "retest_time": ist_hm(bars[i_rt].ts), "tol": sd.tol, "adr": sd.adr, "tags": tags, "taken": False, "why": "",
        }
        self.signals.append(sig)
        if risk_bps <= 0:
            sig["why"] = "no risk: the stop is at the entry"
            return
        if not ws:
            sig["why"] = "no wall beyond the entry by a tolerance"
            return
        if not cr.passes(self.filter, tags):
            sig["why"] = f"filter {self.filter!r} not met"
            return
        qty = max(inst.qty_step, int(self.notional / entry) // inst.qty_step * inst.qty_step)
        pos_id = new_id("pos")
        intent = OrderIntent(
            strategy=cr.NAME, instrument=inst, side=OrderSide.BUY if side > 0 else OrderSide.SELL, qty=qty, purpose=Purpose.ENTRY,
            signal_id=signal_id, client_order_id=f"{signal_id}|ENTRY|{pos_id}", position_id=pos_id, ref_price=entry,
            reason=f"CPR retest held {sig['retest_time']} → first close beyond at {sig['time']}; stop {stop:.2f}, next wall {ws[0][0]} {ws[0][1]:.2f}",
        )
        res = self.gateway.submit(intent)
        self.orders += 1
        self._writes.append(self._ledger_order(res.order, res.decision.value))
        if res.decision.value.startswith("REJECTED") or res.decision.value == "DUP_BLOCKED":
            sig["why"] = f"gateway {res.decision.value}: {res.order.note}"
            return
        fill = res.fill.price if res.fill is not None else entry
        sig["taken"] = True
        plans = {rule: cr.ExitPlan.open(rule, side, fill, stop, [v for _k, v in ws], sd.tol) for rule in cr.RULES}
        primary = plans[self.rule]
        pos: dict[str, Any] = {
            "id": pos_id, "strategy": cr.NAME, "symbol": inst.symbol, "scrip_code": inst.scrip_code, "status": "OPEN", "day": self.day,
            "side": sig["side"], "qty": qty, "entry": fill, "initial_stop": stop, "stop": primary.stop_price(), "target": primary.target_price(),
            "target_name": ws[0][0], "walls": sig["walls"], "risk_bps": round(abs(fill - stop) / fill * 1e4, 1),
            "reward_bps": round(abs(ws[0][1] - fill) / fill * 1e4, 1), "rule": self.rule, "signal_id": signal_id, "opened_ts": int(bar.ts) + 300,
            "entry_time": sig["time"], "retest_time": sig["retest_time"], "mode": res.order.mode, "tags": tags, "tol": sd.tol,
            "bars_held": 0, "mfe_bps": 0.0, "mae_bps": 0.0, "last": fill, "pnl_bps": 0.0, "closed_ts": None, "exit": None, "exit_reason": None,
            "gross_inr": None, "charges_inr": None, "net_inr": None, "r": None, "virtual": {},
        }
        self.positions[pos_id] = pos
        self.plans[pos_id] = plans
        self._writes.append(self._ledger_position(pos))
        self._writes.append(self._ledger_event("strategy.entry", {k: pos[k] for k in ("id", "symbol", "side", "qty", "entry", "stop", "target", "target_name", "risk_bps", "reward_bps", "mode")}))
        log.info("strategy.entry", symbol=inst.symbol, side=pos["side"], entry=fill, stop=stop, target=ws[0], mode=res.order.mode)

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
            if sym == symbol or inst.kind is not InstrumentKind.EQUITY:
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
        exit_now: tuple[float, str] | None = None
        for rule, plan in plans.items():
            res = plan.step(bar.high, bar.low, bar.close, last=last)
            if res is not None:
                pos["virtual"][rule] = plan.to_json()
            if rule == self.rule and res is not None:
                exit_now = res
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
        self._close(pos, exit_now[0], exit_now[1], int(bar.ts) + 300)
        return True

    def _close(self, pos: dict[str, Any], exit_px: float, how: str, ts: float) -> None:
        inst = self.underlyings.get(pos["symbol"])
        side = 1 if pos["side"] == "LONG" else -1
        if inst is not None:
            intent = OrderIntent(
                strategy=cr.NAME, instrument=inst, side=OrderSide.SELL if side > 0 else OrderSide.BUY, qty=pos["qty"], purpose=Purpose.EXIT,
                signal_id=pos["signal_id"], client_order_id=f"{pos['signal_id']}|EXIT|{pos['id']}", position_id=pos["id"], ref_price=exit_px,
                reason=f"{how} at {exit_px:.2f}",
            )
            res = self.gateway.submit(intent)
            self.orders += 1
            self._writes.append(self._ledger_order(res.order, res.decision.value))
            if res.fill is not None:
                exit_px = res.fill.price
            charges = self.costs.round_trip(inst, pos["entry"], exit_px, pos["qty"]).total
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
        for pos in list(self.positions.values()):
            if pos["symbol"] == bar.symbol:
                continue
            last = self.store.last(pos["symbol"], TF)
            if last is not None and bucket_end(self.segment, last.ts, TF) >= session_close_ts(self.segment, ist_day(bar.ts)):
                self._close(pos, last.close, "close", int(bar.ts) + 300)
                changed = True
        return changed

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
        await asyncio.to_thread(self._dump, snap)

    def _dump(self, snap: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{self.day}.json.tmp"
        tmp.write_text(json.dumps(snap, default=str))
        tmp.replace(self.dir / f"{self.day}.json")

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
                    stages.append({"symbol": sym, "side": "bullish" if side > 0 else "bearish", **st.to_json(), "tol": round(sd.tol, 2), "levels": sd.levels})
        taken = [s for s in self.signals if s["taken"]]
        return {
            "strategy": cr.NAME, "day": self.day, "rule": self.rule, "filter": self.filter, "notional_inr": self.notional,
            "universe": sum(1 for i in self.underlyings.values() if i.kind is InstrumentKind.EQUITY), "with_levels": len(self.syms),
            "orders": self.orders, "errors": self.errors,
            "summary": {
                "signals": len(self.signals), "taken": len(taken), "skipped": len(self.signals) - len(taken), "open": len(self.positions), "closed": len(closed),
                "wins": len(wins), "losses": sum(1 for t in closed if (t.get("pnl_bps") or 0) < 0),
                "avg_pnl_bps": round(sum(t["pnl_bps"] for t in closed) / len(closed), 1) if closed else None,
                "sum_gross_inr": round(sum(t["gross_inr"] or 0 for t in closed), 2), "sum_net_inr": round(sum(t["net_inr"] or 0 for t in closed), 2),
                "sum_r": round(sum(t["r"] or 0 for t in closed), 2), "open_pnl_bps": round(sum(p["pnl_bps"] for p in self.positions.values()), 1),
                "by_rule": by_rule,
            },
            "stages": stages, "signals": list(reversed(self.signals)), "positions": sorted(self.positions.values(), key=lambda p: -p["opened_ts"]),
            "trades": list(reversed(closed)),
        }


__all__ = ["TF", "ShadowRunner", "SymbolDay"]
