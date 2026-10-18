"""Backtest the order-block playbook on cached days, minute by minute, with the live board and
structure code — nothing here re-implements a rule the board or the structure engine owns.

Version 1 is the playbook as published (order-block.com, read 2026-10-18), made mechanical:

* nothing before 09:20, no new trade after ``LAST_ENTRY``;
* trade the side breadth favours (more one-sided-up than one-sided-down stocks → longs only);
* a candidate is a PICK (top 3 a side, ≤ 2 per sector, no event risk) whose setup points its way,
  is still READY (never retested), has R:R ≥ ``MIN_RR`` and alignment ≥ ``MIN_ALIGN``;
* entry: a limit at Z, filled by the first 1-minute bar that trades to it (at its open when it
  gaps through); the zone's first retest is the trade;
* stop: S1, judged on a 5-minute CLOSE beyond it, exited at that close (a wick is not a stop) —
  plus a hard backstop at ``HARD_STOP_R`` × the risk, filled at the stop, for a collapse;
* targets: half at R1, the rest at R2 (limits, filled when a 1m bar trades through them);
* flat at the last cached minute (15:13 — the broker's 1m history ends at 15:14);
* risk ``RISK_INR`` a trade, shares = risk ÷ (Z − S1), costs = the measured cost model on a stock
  future, slippage ``SLIP_BPS`` on every fill that is not a resting limit;
* per day: at most ``MAX_OPEN`` positions at once, stop after ``DAILY_MAX_LOSSES`` losses.

Results are in R (multiples of the planned risk) and in rupees after costs; statistics are by day,
because trades on the same day are not independent.
"""

from __future__ import annotations

import json
import math
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Segment, Settings
from ..domain import Instrument, InstrumentKind
from ..market.session import ist_at
from ..risk.costs import CostModel
from ..scan.replay import DayReplay
from .history import HistoryStore


@dataclass(frozen=True, slots=True)
class Params:
    min_rr: float = 1.5
    min_align: int = 3
    side_rule: str = "breadth"  # breadth | both
    picks_only: bool = True  # False = any top-20 contender
    last_entry: str = "15:00"
    risk_inr: float = 5_000.0
    max_open: int = 3
    daily_max_losses: int = 2
    hard_stop_r: float = 2.0
    slip_bps: float = 5.0
    partial_at_r1: float = 0.5
    #: close5 = S1 judged on a 5-minute close (the playbook); intrabar = a stop resting at S1
    stop_mode: str = "close5"
    #: skip a setup whose risk Z − S1 is under this fraction of the stock's average daily range
    min_risk_adr: float = 0.0
    #: only zones whose move began with a sweep of liquidity on the far side
    require_sweep: bool = False


@dataclass(slots=True)
class Trade:
    day: str
    symbol: str
    side: str
    signal_ts: int
    entry_ts: int
    entry: float
    z: float
    s1: float
    r1: float | None
    r2: float | None
    rr_planned: float | None
    align: int | None
    rank: int | None
    qty: int
    exits: list[dict[str, Any]] = field(default_factory=list)
    exit_reason: str = ""
    r: float = 0.0
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0


def _fut(symbol: str) -> Instrument:
    return Instrument(
        scrip_code=f"BT-{symbol}", symbol=symbol, segment=Segment.NSE_FO, kind=InstrumentKind.FUTURE,
        name=f"{symbol} FUT", lot_size=1, tick_size=0.05, multiplier=1, underlying=symbol,
    )


def run_day(store: HistoryStore, day: date, p: Params, costs: CostModel) -> list[Trade]:
    rp = DayReplay(store, day)
    if not rp.days:
        return []
    open_ts = ist_at(day, "09:15")
    last_entry = ist_at(day, p.last_entry)
    end_ts = ist_at(day, "15:14")
    open_pos: dict[str, Trade] = {}
    remaining: dict[str, float] = {}
    done: list[Trade] = []
    traded: set[str] = set()
    losses = 0
    pending: dict[str, dict[str, Any]] = {}  # symbol → setup awaiting its retest

    def minute_bar(sym: str, ts: int) -> np.ndarray | None:
        raw = rp.days[sym].raw
        i = int(np.searchsorted(raw[:, 0], ts - 60))
        return raw[i] if i < len(raw) and int(raw[i, 0]) == ts - 60 else None

    def fill(t: Trade, qty_frac: float, price: float, ts: int, why: str, slip: bool) -> None:
        sign = 1 if t.side == "BULLISH" else -1
        px = price * (1 - sign * p.slip_bps / 1e4) if slip else price
        t.exits.append({"ts": ts, "price": round(px, 4), "frac": qty_frac, "why": why})
        remaining[t.symbol] -= qty_frac

    def close_out(t: Trade) -> None:
        sign = 1 if t.side == "BULLISH" else -1
        risk = abs(t.z - t.s1)
        t.gross = sum(sign * (e["price"] - t.entry) * t.qty * e["frac"] for e in t.exits)
        avg_exit = sum(e["price"] * e["frac"] for e in t.exits)
        t.costs = costs.round_trip(_fut(t.symbol), t.entry, avg_exit, t.qty).total
        t.net = t.gross - t.costs
        t.r = round(t.net / (risk * t.qty), 3) if risk > 0 and t.qty else 0.0
        t.exit_reason = t.exits[-1]["why"] if t.exits else ""
        done.append(t)
        open_pos.pop(t.symbol, None)

    for ts in range(ist_at(day, "09:20"), end_ts + 60, 60):
        # -- manage open positions on the minute that just closed --------------------------------------
        for sym, t in list(open_pos.items()):
            bar = minute_bar(sym, ts)
            if bar is None:
                continue
            _, o, h, lo, c, _v = (float(x) for x in bar)
            sign = 1 if t.side == "BULLISH" else -1
            risk = abs(t.z - t.s1)
            fav = (h - t.entry) if sign > 0 else (t.entry - lo)
            adv = (t.entry - lo) if sign > 0 else (h - t.entry)
            t.mfe_r = max(t.mfe_r, fav / risk)
            t.mae_r = max(t.mae_r, adv / risk)
            hard = t.entry - sign * p.hard_stop_r * risk
            if p.stop_mode == "intrabar":
                hard = t.s1
            if (sign > 0 and lo <= hard) or (sign < 0 and h >= hard):
                why = "S1_TOUCH" if p.stop_mode == "intrabar" else "HARD_STOP"
                fill(t, remaining[sym], min(o, hard) if sign > 0 else max(o, hard), ts, why, True)
                close_out(t)
                losses += 1
                continue
            if t.r1 is not None and not any(e["why"] == "R1" for e in t.exits):
                if (sign > 0 and h >= t.r1) or (sign < 0 and lo <= t.r1):
                    fill(t, p.partial_at_r1, t.r1, ts, "R1", False)
            if t.r2 is not None and ((sign > 0 and h >= t.r2) or (sign < 0 and lo <= t.r2)):
                fill(t, remaining[sym], t.r2, ts, "R2", False)
                close_out(t)
                continue
            if (ts - open_ts) % 300 == 0:  # a 5-minute bar just closed: judge S1 on its close
                if (sign > 0 and c < t.s1) or (sign < 0 and c > t.s1):
                    fill(t, remaining[sym], c, ts, "S1_CLOSE", True)
                    close_out(t)
                    if t.net < 0:
                        losses += 1
                    continue
            if ts >= end_ts:
                fill(t, remaining[sym], c, ts, "EOD", True)
                close_out(t)
                if t.net < 0:
                    losses += 1

        if ts > last_entry or losses >= p.daily_max_losses:
            continue

        # -- fill pending limits at Z ---------------------------------------------------------------------
        for sym, su in list(pending.items()):
            if sym in open_pos or len(open_pos) >= p.max_open:
                continue
            bar = minute_bar(sym, ts)
            if bar is None:
                continue
            _, o, h, lo, c, _v = (float(x) for x in bar)
            z, bull = su["z"], su["side"] == "BULLISH"
            if (bull and lo <= z) or (not bull and h >= z):
                entry = min(o, z) if bull else max(o, z)
                risk = abs(z - su["s1"])
                qty = int(p.risk_inr / risk) if risk > 0 else 0
                pending.pop(sym)
                if qty <= 0:
                    continue
                open_pos[sym] = Trade(
                    day.isoformat(), sym, su["side"], su["ts"], ts, round(entry, 4), z, su["s1"],
                    su["r1"], su["r2"], su["rr"], su["align"], su["rank"], qty,
                )
                remaining[sym] = 1.0
                traded.add(sym)

        # -- new candidates, once each 5-minute bar closes --------------------------------------------------
        if (ts - open_ts) % 300:
            continue
        board = rp.board_at(ts)
        b = board.breadth
        allowed = {"BULLISH", "BEARISH"}
        if p.side_rule == "breadth":
            allowed = {"BULLISH"} if b["bullish"] > b["bearish"] else {"BEARISH"} if b["bearish"] > b["bullish"] else set()
        for r in board.rows:
            if r.side not in allowed or r.symbol in traded or r.symbol in open_pos:
                continue
            if p.picks_only and not r.pick:
                continue
            if r.align is None or r.align < p.min_align or not r.setup:
                pending.pop(r.symbol, None)
                continue
            su = r.setup
            adr = rp.days[r.symbol].adr
            too_tight = p.min_risk_adr > 0 and (adr is None or abs(su["z"] - su["s1"]) < p.min_risk_adr * adr)
            if (
                su["state"] != "READY"
                or su["rr"] is None
                or su["rr"] < p.min_rr
                or too_tight
                or (p.require_sweep and not su.get("swept_before"))
            ):
                pending.pop(r.symbol, None)
                continue
            pending[r.symbol] = {**su, "side": r.side, "ts": ts, "align": r.align, "rank": r.rank}
        # a setup that is no longer a candidate stops waiting
        for sym in list(pending):
            row = next((r for r in board.rows if r.symbol == sym), None)
            if row is None or (p.picks_only and not row.pick) or row.side not in allowed:
                pending.pop(sym, None)

    for t in list(open_pos.values()):  # anything left (no 15:13 bar): out at its last close
        last = rp.days[t.symbol].raw[-1]
        fill(t, remaining[t.symbol], float(last[4]), int(last[0]) + 60, "EOD", True)
        close_out(t)
    return done


def summarize(trades: list[Trade], days: list[str]) -> dict[str, Any]:
    by_day: dict[str, float] = {d: 0.0 for d in days}
    for t in trades:
        by_day[t.day] = by_day.get(t.day, 0.0) + t.r
    daily = list(by_day.values())
    rs = [t.r for t in trades]
    n_days = len(daily)
    mean_d = statistics.fmean(daily) if daily else 0.0
    sd_d = statistics.stdev(daily) if n_days > 1 else 0.0
    reasons: dict[str, dict[str, float]] = {}
    for t in trades:
        x = reasons.setdefault(t.exit_reason, {"n": 0, "r": 0.0})
        x["n"] += 1
        x["r"] = round(x["r"] + t.r, 3)
    return {
        "trades": len(trades),
        "days": n_days,
        "days_traded": sum(1 for d in daily if d != 0),
        "wins": sum(1 for r in rs if r > 0),
        "win_rate": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1) if rs else None,
        "avg_r": round(statistics.fmean(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2),
        "net_inr": round(sum(t.net for t in trades), 0),
        "costs_inr": round(sum(t.costs for t in trades), 0),
        "t_by_day": round(mean_d / (sd_d / math.sqrt(n_days)), 2) if sd_d > 0 else None,
        "days_positive": sum(1 for d in daily if d > 0),
        "days_negative": sum(1 for d in daily if d < 0),
        "by_exit": reasons,
        "by_side": {
            s: {"n": len(x), "avg_r": round(statistics.fmean(x), 3) if x else None}
            for s in ("BULLISH", "BEARISH")
            for x in [[t.r for t in trades if t.side == s]]
        },
        "by_day": {d: round(v, 3) for d, v in by_day.items()},
    }


def run(settings: Settings, days: list[date], p: Params, *, label: str = "", on_day: Any = None) -> dict[str, Any]:
    store = HistoryStore(settings.data_dir / "history")
    costs = CostModel(settings)
    trades: list[Trade] = []
    t0 = time.time()
    for i, d in enumerate(days, 1):
        got = run_day(store, d, p, costs)
        trades += got
        if on_day:
            on_day(d, got, i, len(days))
    out = {
        "id": f"ob-{uuid.uuid4().hex[:10]}",
        "label": label,
        "created_ts": time.time(),
        "seconds": round(time.time() - t0, 1),
        "params": asdict(p),
        "range": [days[0].isoformat(), days[-1].isoformat()] if days else [],
        "summary": summarize(trades, [d.isoformat() for d in days]),
        "trades": [asdict(t) for t in trades],
    }
    root = settings.data_dir / "obtests"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out


def cached_days(store: HistoryStore, probe: str = "RELIANCE") -> list[date]:
    from ..market.session import ist_day

    df = store.load(probe, "1m")
    return sorted({ist_day(int(t)) for t in df["ts"][::30]})


def list_runs(root: Path, limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for f in sorted(root.glob("ob-*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        d = json.loads(f.read_text())
        out.append({k: d[k] for k in ("id", "label", "created_ts", "seconds", "params", "range", "summary")})
    return out


__all__ = ["Params", "Trade", "cached_days", "list_runs", "replace", "run", "run_day", "summarize"]
