"""The positional shadow book for the daily order block (``strategy/daily_ob.py``): decisions at the close of each
finished session, positions that live across sessions and engine restarts.

The engine calls :meth:`PositionalRunner.on_session_close` after its post-close refresh of the daily series, and
:meth:`catch_up` at boot for the sessions missed. Everything persists in ``data_dir/strategy/positional.json``.
Sizing is by risk (``positional_risk_inr`` ÷ the distance to the stop); costs are the measured model with
delivery STT on the long cash leg and futures STT on the short leg, since a cash short cannot be held overnight.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import structlog

from ..bars.store import BarStore
from ..bars.unified import BarSource, UnifiedBar
from ..config import Settings
from ..domain import Instrument, InstrumentKind, new_id
from ..instrument.universe import INDEX_ROOTS
from ..market.session import ist_day
from ..risk.costs import CostModel
from ..scan.replay import guarded_adr
from ..structure.engine import Bar5
from . import daily_ob as ob

log = structlog.get_logger(__name__)
NIFTY = "NIFTY"
STT_DELIVERY_PCT = 0.1  # cash delivery, both legs


def daily_series(store: BarStore, symbol: str) -> list[UnifiedBar]:
    """One bar per IST date, oldest first; the broker's candle wins over a live-built one for the same date."""
    by_day: dict[date, UnifiedBar] = {}
    for b in store.bars(symbol, "1d"):
        if b.high <= 0 or b.low <= 0:
            continue
        d = ist_day(b.ts)
        cur = by_day.get(d)
        if cur is None or (cur.source is not BarSource.REST and b.source is BarSource.REST):
            by_day[d] = b
    return [by_day[d] for d in sorted(by_day)]


class PositionalRunner:
    def __init__(self, settings: Settings, *, store: BarStore, costs: CostModel, underlyings: Mapping[str, Instrument] | Callable[[], Mapping[str, Instrument]]) -> None:
        self.s = settings
        self.store = store
        self.costs = costs
        self._underlyings = underlyings
        self.path = settings.data_dir / "strategy" / "positional.json"
        self.last_day: str = ""
        self.pending: dict[str, list[dict[str, Any]]] = {}  # symbol → zones with no return yet
        self.positions: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.days: list[dict[str, Any]] = []  # one line per session processed
        self._lock = asyncio.Lock()
        self._load()

    @property
    def underlyings(self) -> Mapping[str, Instrument]:
        u = self._underlyings
        return u() if callable(u) else u

    # -- persistence -------------------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text())
        except Exception as exc:  # noqa: BLE001 - a corrupt file is logged and the book starts empty
            log.warning("positional.load_failed", error=repr(exc))
            return
        self.last_day = d.get("last_day", "")
        self.pending = d.get("pending", {})
        self.positions = d.get("positions", {})
        self.trades = d.get("trades", [])
        self.skipped = d.get("skipped", [])
        self.days = d.get("days", [])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last_day": self.last_day, "pending": self.pending, "positions": self.positions, "trades": self.trades,
                                   "skipped": self.skipped[-500:], "days": self.days[-400:]}, default=str))
        tmp.replace(self.path)

    # -- sessions ----------------------------------------------------------------------------------------

    def _equities(self) -> list[Instrument]:
        return [i for s, i in self.underlyings.items() if i.kind is InstrumentKind.EQUITY and s not in INDEX_ROOTS]

    def finished_sessions(self) -> list[date]:
        """The dates the daily series holds for NIFTY — the sessions the engine has a finished candle for."""
        return [ist_day(b.ts) for b in daily_series(self.store, NIFTY)]

    async def catch_up(self) -> list[str]:
        """Process every finished session after ``last_day``; on the first run, build the pending limits from the
        latest session only (no trades are back-filled)."""
        days = self.finished_sessions()
        if not days:
            return []
        if not self.last_day:
            last = days[-1]
            await self.on_session_close(last, first_run=True)
            return [last.isoformat()]
        done = []
        for d in days:
            if d.isoformat() > self.last_day:
                await self.on_session_close(d)
                done.append(d.isoformat())
        return done

    async def on_session_close(self, day: date, *, first_run: bool = False) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._process, day, first_run)

    def _bar_for(self, symbol: str, day: date, series: list[UnifiedBar] | None = None) -> Bar5 | None:
        rows = series if series is not None else daily_series(self.store, symbol)
        for b in reversed(rows):
            d = ist_day(b.ts)
            if d == day:
                return Bar5(int(b.ts), b.open, b.high, b.low, b.close, b.volume)
            if d < day:
                break
        return None

    def _process(self, day: date, first_run: bool) -> dict[str, Any]:
        t0 = time.time()
        dstr = day.isoformat()
        if self.last_day and dstr <= self.last_day:
            return {"day": dstr, "skipped": "already processed"}
        nifty = daily_series(self.store, NIFTY)
        before = [b.close for b in nifty if ist_day(b.ts) < day]
        nifty_ret = ob.trailing_return(before)
        allow_short = ob.short_allowed(self.s.positional_short_gate, nifty_ret)
        closed, opened, skipped, touched = 0, 0, 0, 0
        insts = {i.symbol: i for i in self._equities()}
        # 1. open positions: one more session
        for _pid, pos in list(self.positions.items()):
            bar = self._bar_for(pos["symbol"], day)
            if bar is None:
                continue
            pos["held"] += 1
            res = ob.step(pos["side"], pos["entry"], pos["stop"], pos["target"], bar, first=False, held=pos["held"], max_sessions=self.s.positional_max_sessions)
            pos["last"] = bar.c
            pos["mfe_bps"] = max(pos["mfe_bps"], (bar.h - pos["entry"]) * pos["side"] / pos["entry"] * 1e4 if pos["side"] > 0 else (pos["entry"] - bar.l) / pos["entry"] * 1e4)
            pos["mae_bps"] = min(pos["mae_bps"], (bar.l - pos["entry"]) * pos["side"] / pos["entry"] * 1e4 if pos["side"] > 0 else (pos["entry"] - bar.h) / pos["entry"] * 1e4)
            if res is not None:
                self._close(pos, res[0], res[1], dstr, insts.get(pos["symbol"]))
                closed += 1
        # 2. pending limits: filled, touched, or still waiting
        if not first_run:
            for sym, zones in list(self.pending.items()):
                inst = insts.get(sym)
                bar = self._bar_for(sym, day)
                if bar is None or inst is None:
                    continue
                keep = []
                for zn in zones:
                    side = zn["side"]
                    if not ob.returned(side, zn["high"], zn["low"], bar):
                        keep.append(zn)
                        continue
                    touched += 1
                    if not ob.fills(side, zn["z"], bar, self.s.positional_fill_through_bps / 1e4):
                        self.skipped.append({"day": dstr, "symbol": sym, "side": side, "z": zn["z"], "why": "touched, not through"})
                        continue
                    if side < 0 and not allow_short:
                        self.skipped.append({"day": dstr, "symbol": sym, "side": side, "z": zn["z"], "why": f"short gate: NIFTY 20-session {nifty_ret:+.1f}%" if nifty_ret is not None else "short gate: no NIFTY history"})
                        skipped += 1
                        continue
                    if len(self.positions) >= self.s.positional_max_open:
                        self.skipped.append({"day": dstr, "symbol": sym, "side": side, "z": zn["z"], "why": f"book full ({self.s.positional_max_open} open)"})
                        skipped += 1
                        continue
                    if any(p["symbol"] == sym for p in self.positions.values()):
                        self.skipped.append({"day": dstr, "symbol": sym, "side": side, "z": zn["z"], "why": "already in this stock"})
                        skipped += 1
                        continue
                    pos = self._open(inst, zn, bar, dstr, nifty_ret)
                    opened += 1
                    res = ob.step(side, pos["entry"], pos["stop"], pos["target"], bar, first=True, held=0, max_sessions=self.s.positional_max_sessions)
                    if res is not None:
                        self._close(pos, res[0], res[1], dstr, inst)
                        closed += 1
                if keep:
                    self.pending[sym] = keep
                else:
                    self.pending.pop(sym, None)
        # 3. the zones as of this close: every intact zone with no return yet becomes (or stays) a pending limit
        fresh: dict[str, list[dict[str, Any]]] = {}
        n_zones = 0
        for sym in insts:
            rows = daily_series(self.store, sym)
            rows = [b for b in rows if ist_day(b.ts) <= day]
            if len(rows) < 30:
                continue
            win = rows[-60:]
            adr = guarded_adr([b.high for b in win], [b.low for b in win], [b.close for b in win]) if len(win) >= 21 else None
            bars = [Bar5(int(b.ts), b.open, b.high, b.low, b.close, b.volume) for b in rows[-ob.ZONE_BARS:]]
            zs = ob.pending_zones(bars, adr)
            if zs:
                fresh[sym] = [z.to_json() for z in zs]
                n_zones += len(zs)
        self.pending = fresh
        self.last_day = dstr
        line = {"day": dstr, "nifty_ret20": round(nifty_ret, 2) if nifty_ret is not None else None, "shorts_allowed": allow_short, "pending": n_zones,
                "touched": touched, "opened": opened, "closed": closed, "skipped": skipped, "open": len(self.positions), "seconds": round(time.time() - t0, 1)}
        self.days.append(line)
        self._save()
        log.info("positional.session", **line)
        return line

    # -- the book ----------------------------------------------------------------------------------------

    def _open(self, inst: Instrument, zn: dict[str, Any], bar: Bar5, dstr: str, nifty_ret: float | None) -> dict[str, Any]:
        side, entry, stop = zn["side"], float(zn["z"]), float(zn["stop"])
        risk_px = abs(entry - stop)
        qty = max(1, int(self.s.positional_risk_inr / risk_px)) if risk_px > 0 else 1
        target = entry + side * self.s.positional_target_r * risk_px
        pos = {
            "id": new_id("pos"), "strategy": ob.NAME, "symbol": inst.symbol, "scrip_code": inst.scrip_code, "side": side, "venue": "cash" if side > 0 else "future",
            "entry_day": dstr, "entry": entry, "stop": stop, "target": target, "qty": qty, "notional_inr": round(entry * qty, 2), "risk_bps": round(risk_px / entry * 1e4, 1),
            "risk_inr": round(risk_px * qty, 2), "zone": {"low": zn["low"], "high": zn["high"], "impulse": zn["impulse"], "swept_before": zn["swept_before"], "ob_ts": zn["ob_ts"], "bos_ts": zn["bos_ts"]},
            "regime": {"nifty_ret20": round(nifty_ret, 2) if nifty_ret is not None else None}, "held": 0, "last": bar.c, "mfe_bps": 0.0, "mae_bps": 0.0,
            "exit_day": None, "exit": None, "exit_reason": None, "pnl_bps": None, "gross_inr": None, "charges_inr": None, "net_inr": None, "r": None,
        }
        self.positions[pos["id"]] = pos
        log.info("positional.entry", symbol=inst.symbol, side=side, entry=entry, stop=stop, target=target, qty=qty)
        return pos

    def _charges(self, inst: Instrument | None, side: int, entry: float, exit_px: float, qty: int) -> float:
        if inst is None:
            return 0.0
        base = self.costs.round_trip(inst, entry, exit_px, qty)
        if side > 0:  # cash delivery: STT on both legs, not the intraday sell-side rate
            stt = (entry + exit_px) * qty * STT_DELIVERY_PCT / 100
        else:  # a short held overnight is a stock future: STT on the sell leg (the entry) at the futures rate
            stt = entry * qty * self.s.cost_stt_pct_sell_future / 100
        return round(base.total - base.stt + stt, 2)

    def _close(self, pos: dict[str, Any], exit_px: float, how: str, dstr: str, inst: Instrument | None) -> None:
        side = pos["side"]
        gross = (exit_px - pos["entry"]) * side * pos["qty"]
        charges = self._charges(inst, side, pos["entry"], exit_px, pos["qty"])
        risk_px = abs(pos["entry"] - pos["stop"])
        pos.update({"exit_day": dstr, "exit": exit_px, "exit_reason": how, "pnl_bps": round((exit_px - pos["entry"]) * side / pos["entry"] * 1e4, 1),
                    "gross_inr": round(gross, 2), "charges_inr": charges, "net_inr": round(gross - charges, 2),
                    "r": round((exit_px - pos["entry"]) * side / risk_px, 2) if risk_px > 0 else None, "last": exit_px})
        self.positions.pop(pos["id"], None)
        self.trades.append(pos)
        log.info("positional.exit", symbol=pos["symbol"], side=side, how=how, exit=exit_px, pnl_bps=pos["pnl_bps"], net_inr=pos["net_inr"])

    # -- the view ----------------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        tr = self.trades
        def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
            if not rows:
                return {"closed": 0}
            return {"closed": len(rows), "wins": sum(1 for t in rows if (t["net_inr"] or 0) > 0), "avg_pnl_bps": round(sum(t["pnl_bps"] for t in rows) / len(rows), 1),
                    "sum_gross_inr": round(sum(t["gross_inr"] for t in rows), 2), "sum_charges_inr": round(sum(t["charges_inr"] for t in rows), 2),
                    "sum_net_inr": round(sum(t["net_inr"] for t in rows), 2), "sum_r": round(sum(t["r"] or 0 for t in rows), 2),
                    "exits": {how: sum(1 for t in rows if t["exit_reason"] == how) for how in ("target", "stop", "gap_stop", "time")}}
        return {
            "strategy": ob.NAME, "last_day": self.last_day, "settings": {"risk_inr": self.s.positional_risk_inr, "max_open": self.s.positional_max_open, "short_gate": self.s.positional_short_gate,
                                                                        "target_r": self.s.positional_target_r, "max_sessions": self.s.positional_max_sessions, "fill_through_bps": self.s.positional_fill_through_bps},
            "summary": {**agg(tr), "open": len(self.positions), "pending": sum(len(v) for v in self.pending.values()), "skipped": len(self.skipped),
                        "by_side": {"long": agg([t for t in tr if t["side"] > 0]), "short": agg([t for t in tr if t["side"] < 0])},
                        "by_regime": {"nifty20_down": agg([t for t in tr if (t["regime"].get("nifty_ret20") or 0) <= 0]), "nifty20_up": agg([t for t in tr if (t["regime"].get("nifty_ret20") or 0) > 0])}},
            "positions": sorted(self.positions.values(), key=lambda p: p["entry_day"]), "pending": self.pending, "trades": list(reversed(tr)), "skipped": list(reversed(self.skipped[-200:])),
            "days": list(reversed(self.days[-60:])),
        }


__all__ = ["PositionalRunner", "daily_series"]
