"""Strategies built on liquidity sweeps, the volume profile and the market profile, backtested on
the cached 1m history with one simulator and the measured cost model.

Levels known BEFORE the day (from the previous session's bars only): PDH / PDL / PDC, the previous
session's volume-profile POC / VAH / VAL and market-profile VAH / VAL. Levels formed DURING the day
are used only once formed: the initial balance (first hour) from 10:15, VWAP and the running
day high / low from the bars closed so far.

* ``SWEEP`` — a 5-minute bar trades through a support level and CLOSES back above it (bullish;
  mirrored for resistance). Entry at that close; stop under the sweep wick (at least
  ``min_stop_adr`` of a normal day); T1 = the nearest of VWAP / previous POC / IB mid that is at
  least ``min_t1_r`` away, T2 = the far value-area edge or the next level; R:R ≥ ``min_rr`` to T2.
* ``VA_REENTRY`` — opened outside the previous value area, then two consecutive 30-minute periods
  CLOSE back inside it: trade toward the other edge (Dalton's "80 % rule"). Stop beyond the
  day's extreme on the outside; T1 = previous POC, T2 = the other value edge.
* ``OB_CONFLUENCE`` — lives in obtest.py terms: an order-block setup whose zone overlaps a
  previous-day value level (see ``ob_confluence``).

The simulator: stop resting (filled at the stop or a worse open), half at T1 then the stop to
breakeven, the rest at T2, flat at 15:13 (the last cached minute). One trade per stock per day per
strategy; no entry after ``last_entry``.
"""

from __future__ import annotations

import json
import math
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np

from ..config import Settings
from ..market.session import ist_at, ist_day
from ..profile.core import bin_size, volume_profile
from ..risk.costs import CostModel
from .history import HistoryStore, session_rows
from .obtest import _fut


@dataclass(frozen=True, slots=True)
class PBParams:
    strategy: str = "SWEEP"  # SWEEP | VA_REENTRY
    first_entry: str = "09:30"
    last_entry: str = "14:30"
    min_stop_adr: float = 0.10
    min_t1_r: float = 0.8
    min_rr: float = 1.5
    risk_inr: float = 5_000.0
    slip_bps: float = 5.0
    sweep_levels: str = "PDL,PDH,PVAL,PVAH,IBL,IBH"  # which levels a sweep may take
    sweep_min_adr: float = 0.02  # the wick must go at least this far through the level
    breakeven_after_t1: bool = True
    max_trades_per_day: int = 6  # across all stocks, first come first served


@dataclass(slots=True)
class PBTrade:
    day: str
    symbol: str
    side: str
    setup: str  # e.g. "SWEEP PDL", "VA_REENTRY"
    signal_ts: int
    entry: float
    stop: float
    t1: float
    t2: float
    rr: float
    qty: int
    exits: list[dict[str, Any]] = field(default_factory=list)
    exit_reason: str = ""
    r: float = 0.0
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0


@dataclass(slots=True)
class DayData:
    symbol: str
    m1: np.ndarray  # (n, 6) ts o h l c v, 09:15 ≤ ts < 15:14
    open_ts: int
    adr: float | None
    pdh: float
    pdl: float
    pdc: float
    pvah: float | None
    pval: float | None
    ppoc: float | None


def _prev_session(m1_all: Any, day: date) -> Any:
    before = m1_all[m1_all["ts"] < ist_at(day, "00:00")]
    if before.empty:
        return None
    last_day = ist_day(int(before["ts"].iloc[-1]))
    return session_rows(m1_all, last_day)


def load(store: HistoryStore, symbol: str, day: date, cache: dict[str, Any] | None = None) -> DayData | None:
    """One stock's day. ``cache`` keeps each stock's frames across days, so a run reads every
    Parquet file once instead of once a day."""
    from ..scan.replay import guarded_adr

    if cache is not None and symbol in cache:
        m1, d1 = cache[symbol]
    else:
        m1, d1 = store.load(symbol, "1m"), store.load(symbol, "1d")
        if cache is not None:
            cache[symbol] = (m1, d1)
    if m1.empty or d1.empty:
        return None
    today = session_rows(m1, day)
    prev = _prev_session(m1, day)
    before = d1[d1["ts"] < ist_at(day, "00:00")].sort_values("ts")
    if today.empty or prev is None or prev.empty or before.empty:
        return None
    step = bin_size(float(prev["c"].iloc[-1]))
    vp = volume_profile(list(prev[["h", "l", "v"]].itertuples(index=False, name=None)), step)
    return DayData(
        symbol,
        today[["ts", "o", "h", "l", "c", "v"]].to_numpy(dtype=float),
        ist_at(day, "09:15"),
        guarded_adr(before["h"].tolist(), before["l"].tolist(), before["c"].tolist()),
        float(before["h"].iloc[-1]), float(before["l"].iloc[-1]), float(before["c"].iloc[-1]),
        vp.vah if vp else None, vp.val if vp else None, vp.poc if vp else None,
    )


def _bars5(m1: np.ndarray, open_ts: int) -> list[tuple[int, int, float, float, float, float]]:
    """(5m start, index of its last 1m row, o, h, l, c) for complete 5m buckets."""
    out = []
    keys = open_ts + ((m1[:, 0] - open_ts) // 300) * 300
    for k in np.unique(keys):
        idx = np.nonzero(keys == k)[0]
        out.append((int(k), int(idx[-1]), float(m1[idx[0], 1]), float(m1[idx, 2].max()), float(m1[idx, 3].min()), float(m1[idx[-1], 4])))
    return out


def _vwap_upto(m1: np.ndarray, i: int) -> float | None:
    v = m1[: i + 1, 5]
    if v.sum() <= 0:
        return None
    tp = (m1[: i + 1, 2] + m1[: i + 1, 3] + m1[: i + 1, 4]) / 3
    return float((tp * v).sum() / v.sum())


def signals_sweep(d: DayData, p: PBParams) -> list[dict[str, Any]]:
    if not d.adr:
        return []
    allowed = set(p.sweep_levels.split(","))
    first, last = ist_at_day(d.open_ts, p.first_entry), ist_at_day(d.open_ts, p.last_entry)
    ib_end = d.open_ts + 3600
    out: list[dict[str, Any]] = []
    for k, i, _o, h, lo, c in _bars5(d.m1, d.open_ts):
        close_ts = k + 300
        if close_ts < first or close_ts > last:
            continue
        ib = d.m1[d.m1[:, 0] < ib_end]
        supports: dict[str, float] = {"PDL": d.pdl}
        resist: dict[str, float] = {"PDH": d.pdh}
        if d.pval:
            supports["PVAL"] = d.pval
        if d.pvah:
            resist["PVAH"] = d.pvah
        if close_ts >= ib_end and len(ib):
            supports["IBL"] = float(ib[:, 3].min())
            resist["IBH"] = float(ib[:, 2].max())
        depth = p.sweep_min_adr * d.adr
        # a level already traded through earlier today is not resting liquidity any more
        prior = d.m1[d.m1[:, 0] < k]
        lo_so_far = float(prior[:, 3].min()) if len(prior) else math.inf
        hi_so_far = float(prior[:, 2].max()) if len(prior) else -math.inf
        vw = _vwap_upto(d.m1, i)
        for name, lvl in supports.items():
            if name in allowed and lo <= lvl - depth and c > lvl and lo_so_far > lvl:
                out.append(_plan(d, p, "BULLISH", f"SWEEP {name}", close_ts, c, lo, lvl, vw))
        for name, lvl in resist.items():
            if name in allowed and h >= lvl + depth and c < lvl and hi_so_far < lvl:
                out.append(_plan(d, p, "BEARISH", f"SWEEP {name}", close_ts, c, h, lvl, vw))
    return [s for s in out if s]


def signals_va_reentry(d: DayData, p: PBParams) -> list[dict[str, Any]]:
    if not (d.pvah and d.pval and d.ppoc and d.adr):
        return []
    open_px = float(d.m1[0, 1])
    if d.pval <= open_px <= d.pvah:
        return []
    side = "BEARISH" if open_px > d.pvah else "BULLISH"  # opened above value → trade back down through it
    inside_run = 0
    for start in range(d.open_ts, d.open_ts + 6 * 3600, 1800):
        per = d.m1[(d.m1[:, 0] >= start) & (d.m1[:, 0] < start + 1800)]
        if not len(per):
            continue
        c = float(per[-1, 4])
        close_ts = int(per[-1, 0]) + 60
        inside_run = inside_run + 1 if d.pval <= c <= d.pvah else 0
        if inside_run == 2 and ist_at_day(d.open_ts, p.first_entry) <= close_ts <= ist_at_day(d.open_ts, p.last_entry):
            so_far = d.m1[d.m1[:, 0] < close_ts]
            extreme = float(so_far[:, 2].max()) if side == "BEARISH" else float(so_far[:, 3].min())
            t2 = d.pval if side == "BEARISH" else d.pvah
            plan = _plan(d, p, side, "VA_REENTRY", close_ts, c, extreme, None, None, t1_override=d.ppoc, t2_override=t2)
            return [plan] if plan else []
    return []


def ist_at_day(open_ts: int, hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return open_ts + (h * 60 + m - (9 * 60 + 15)) * 60


def _plan(
    d: DayData, p: PBParams, side: str, setup: str, ts: int, entry: float, wick: float, lvl: float | None,
    vwap: float | None, *, t1_override: float | None = None, t2_override: float | None = None,
) -> dict[str, Any] | None:
    sign = 1 if side == "BULLISH" else -1
    assert d.adr
    buf = 0.02 * d.adr
    stop = wick - sign * buf
    if abs(entry - stop) < p.min_stop_adr * d.adr:
        stop = entry - sign * p.min_stop_adr * d.adr
    risk = abs(entry - stop)
    ahead = sorted(
        {x for x in (vwap, d.ppoc, d.pdc, d.pvah, d.pval, d.pdh, d.pdl, t1_override, t2_override) if x is not None and (x - entry) * sign > 0},
        key=lambda x: (x - entry) * sign,
    )
    t1 = t1_override if t1_override is not None and (t1_override - entry) * sign > 0 else next((x for x in ahead if abs(x - entry) >= p.min_t1_r * risk), None)
    if t1 is None:
        return None
    t2 = t2_override if t2_override is not None and (t2_override - t1) * sign > 0 else next((x for x in ahead if (x - t1) * sign > 0), None)
    if t2 is None:
        t2 = entry + sign * 2 * abs(t1 - entry)
    rr = abs(t2 - entry) / risk
    if rr < p.min_rr:
        return None
    return {"side": side, "setup": setup, "ts": ts, "entry": entry, "stop": stop, "t1": t1, "t2": t2, "rr": round(rr, 2), "level": lvl}


def simulate(d: DayData, s: dict[str, Any], p: PBParams, costs: CostModel, day: date) -> PBTrade | None:
    sign = 1 if s["side"] == "BULLISH" else -1
    risk = abs(s["entry"] - s["stop"])
    qty = int(p.risk_inr / risk) if risk > 0 else 0
    if qty <= 0:
        return None
    entry = s["entry"] * (1 + sign * p.slip_bps / 1e4)
    t = PBTrade(day.isoformat(), d.symbol, s["side"], s["setup"], s["ts"], round(entry, 4), s["stop"], s["t1"], s["t2"], s["rr"], qty)
    stop = s["stop"]
    left = 1.0
    rows = d.m1[d.m1[:, 0] >= s["ts"]]
    for r in rows:
        ts, o, h, lo = int(r[0]), float(r[1]), float(r[2]), float(r[3])
        t.mfe_r = max(t.mfe_r, ((h - entry) if sign > 0 else (entry - lo)) / risk)
        t.mae_r = max(t.mae_r, ((entry - lo) if sign > 0 else (h - entry)) / risk)
        if (sign > 0 and lo <= stop) or (sign < 0 and h >= stop):
            px = (min(o, stop) if sign > 0 else max(o, stop)) * (1 - sign * p.slip_bps / 1e4)
            t.exits.append({"ts": ts + 60, "price": round(px, 4), "frac": left, "why": "STOP" if left == 1.0 else "BE_STOP"})
            left = 0.0
            break
        if left == 1.0 and ((sign > 0 and h >= t.t1) or (sign < 0 and lo <= t.t1)):
            t.exits.append({"ts": ts + 60, "price": t.t1, "frac": 0.5, "why": "T1"})
            left = 0.5
            if p.breakeven_after_t1:
                stop = entry
        if (sign > 0 and h >= t.t2) or (sign < 0 and lo <= t.t2):
            t.exits.append({"ts": ts + 60, "price": t.t2, "frac": left, "why": "T2"})
            left = 0.0
            break
    if left > 0:
        last = rows[-1] if len(rows) else d.m1[-1]
        px = float(last[4]) * (1 - sign * p.slip_bps / 1e4)
        t.exits.append({"ts": int(last[0]) + 60, "price": round(px, 4), "frac": left, "why": "EOD"})
    t.gross = sum(sign * (e["price"] - entry) * qty * e["frac"] for e in t.exits)
    avg_exit = sum(e["price"] * e["frac"] for e in t.exits)
    t.costs = costs.round_trip(_fut(d.symbol), entry, avg_exit, qty).total
    t.net = t.gross - t.costs
    t.r = round(t.net / (risk * qty), 3)
    t.exit_reason = t.exits[-1]["why"]
    return t


def run_day(
    store: HistoryStore, day: date, p: PBParams, costs: CostModel, symbols: list[str], cache: dict[str, Any] | None = None
) -> list[PBTrade]:
    signals: list[tuple[int, DayData, dict[str, Any]]] = []
    for sym in symbols:
        d = load(store, sym, day, cache)
        if d is None:
            continue
        sigs = signals_sweep(d, p) if p.strategy == "SWEEP" else signals_va_reentry(d, p)
        if sigs:
            first = min(sigs, key=lambda s: s["ts"])  # one trade per stock per day
            signals.append((first["ts"], d, first))
    signals.sort(key=lambda x: x[0])
    out = []
    for _ts, d, s in signals[: p.max_trades_per_day]:
        t = simulate(d, s, p, costs, day)
        if t:
            out.append(t)
    return out


def summarize(trades: list[PBTrade], days: list[str]) -> dict[str, Any]:
    by_day = {d: 0.0 for d in days}
    for t in trades:
        by_day[t.day] = by_day.get(t.day, 0.0) + t.r
    daily = list(by_day.values())
    rs = [t.r for t in trades]
    sd = statistics.stdev(daily) if len(daily) > 1 else 0.0

    def group(key: Any) -> dict[str, Any]:
        g: dict[str, list[float]] = {}
        for t in trades:
            g.setdefault(key(t), []).append(t.r)
        return {k: {"n": len(v), "avg_r": round(statistics.fmean(v), 3), "total_r": round(sum(v), 2)} for k, v in sorted(g.items())}

    return {
        "trades": len(trades), "days": len(daily),
        "win_rate": round(100 * sum(r > 0 for r in rs) / len(rs), 1) if rs else None,
        "avg_r": round(statistics.fmean(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2),
        "net_inr": round(sum(t.net for t in trades)), "costs_inr": round(sum(t.costs for t in trades)),
        "t_by_day": round(statistics.fmean(daily) / (sd / math.sqrt(len(daily))), 2) if sd > 0 else None,
        "days_positive": sum(x > 0 for x in daily), "days_negative": sum(x < 0 for x in daily),
        "by_exit": group(lambda t: t.exit_reason), "by_setup": group(lambda t: t.setup), "by_side": group(lambda t: t.side),
        "by_day": {k: round(v, 3) for k, v in by_day.items()},
    }


def run(settings: Settings, days: list[date], p: PBParams, *, label: str = "", on_day: Any = None) -> dict[str, Any]:
    store = HistoryStore(settings.data_dir / "history")
    costs = CostModel(settings)
    symbols = store.symbols("1m")
    trades: list[PBTrade] = []
    cache: dict[str, Any] = {}
    t0 = time.time()
    for i, d in enumerate(days, 1):
        got = run_day(store, d, p, costs, symbols, cache)
        trades += got
        if on_day:
            on_day(d, got, i, len(days))
    out = {
        "id": f"pb-{uuid.uuid4().hex[:10]}", "kind": "playbook", "label": label, "created_ts": time.time(),
        "seconds": round(time.time() - t0, 1), "params": asdict(p),
        "range": [days[0].isoformat(), days[-1].isoformat()] if days else [],
        "summary": summarize(trades, [d.isoformat() for d in days]),
        "trades": [asdict(t) for t in trades],
    }
    root = settings.data_dir / "obtests"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out
