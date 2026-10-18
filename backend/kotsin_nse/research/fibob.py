"""Trend pullbacks where Fibonacci, pivots and an order block line up — set up on 1h, entered on 15m.

The strategy agreed on 2026-10-18 (operator), with the research enhancements (ICT sequence, OTE,
CPR, OI, expiry, a non-Fibonacci control). Longs described; shorts are the mirror.

1. **Trend (daily, known before the day):** the previous close above its 50-day average and the
   20-day above the 50-day. Longs only then.
2. **The leg (1h):** a 1h bar CLOSES above the latest confirmed 1h swing high — a break of
   structure. The leg's low = the lowest low between that swing high and the break.
   - ``require_sweep``: the leg-low bar traded through resting liquidity (an untaken 1h swing low,
     the previous day's low, the daily S1 / S2 / BC or the weekly S1) and CLOSED back above it.
   - ``require_gap``: the leg left a fair value gap (a 1h bar's low above the high two bars back).
3. **The zone:** the leg's Fibonacci retracement band ``zone`` ("golden" 50–61.8 %, "ote" 61.8–79 %,
   "control" 40–55 % — the non-Fibonacci band that tells whether Fibonacci adds anything). The leg's
   high is the highest 1h high CLOSED so far, so the band moves up while the leg extends.
4. **Confluence:** the band must overlap the order block (the last red 1h candle before the leg
   rose) and/or a pivot (daily P, S1, S2, R1, CPR TC / BC; weekly P, S1, R1) within ``pivot_tol_adr``
   of a normal day; ``min_confluence`` counts them (Fibonacci itself is the first).
5. **Entry (15m):** after price trades into the band, the first 15m CLOSE above a 15m swing high
   that formed since (the pullback's own lower high) — the turn. Enter at that close, but only while
   it is still at a discount: retraced at least ``max_entry_fib`` (38.2 %) of the leg.
6. **Invalid / expired:** a 15m close below the leg low kills it; it expires after
   ``expiry_sessions``; one attempt per leg.
7. **Stop** below the lower of the order block and the leg low (the sweep wick), a ``stop_buffer_adr``
   buffer, sized ``min_stop_adr`` .. ``max_stop_adr`` of a normal day.
8. **Targets:** T1 = the leg's high (its 0 %, where stops rest); T2 = the next pivot above T1 when
   it is within the 1.618 extension, else the 1.272 extension. R:R to T2 ≥ ``min_rr``.
   Half at T1, stop to breakeven, rest at T2, out after ``max_sessions`` sessions. Stock future.
Filters: ``cpr_narrow`` (the day's CPR narrower than ``cpr_max_pct`` of price), ``oi_filter``.
"""

from __future__ import annotations

import json
import time as _time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from ..market.session import ist_at, ist_day
from ..risk.costs import CostModel
from .history import HistoryStore
from .mtf import _simulate, clean_5m, oi_alignment, portfolio, resample, summarize, swings

ZONES = {"golden": (0.5, 0.618), "ote": (0.618, 0.79), "control": (0.40, 0.55)}

#: how many legs reached each step, across a run — which rule removes the most
FUNNEL: dict[str, int] = {}


def _f(step: str) -> None:
    FUNNEL[step] = FUNNEL.get(step, 0) + 1


@dataclass(frozen=True, slots=True)
class FibParams:
    zone: str = "golden"
    require_sweep: bool = True
    require_gap: bool = True
    min_confluence: int = 2  # Fibonacci + at least one of (order block, pivot)
    pivot_tol_adr: float = 0.1
    swing_side: int = 2
    ltf_swing_side: int = 2
    expiry_sessions: int = 2
    stop_buffer_adr: float = 0.05
    min_stop_adr: float = 0.25
    max_stop_adr: float = 2.0
    min_rr: float = 1.5
    max_entry_fib: float = 0.382  # the entry must retrace at least this much of the leg
    cpr_narrow: bool = False
    cpr_max_pct: float = 0.3
    oi_filter: str = ""
    max_sessions: int = 5
    max_open: int = 5
    risk_inr: float = 5_000.0
    slip_bps: float = 5.0
    breakeven_after_t1: bool = True
    first_entry: str = "09:30"
    last_entry: str = "15:00"


@dataclass(slots=True)
class FibTrade:
    symbol: str
    side: str
    setup: str
    sweep_ts: int  # the break-of-structure bar's close
    level: float  # the leg's start (low for longs)
    wick: float  # the leg's high when entered
    signal_ts: int
    entry: float
    stop: float
    t1: float
    t2: float
    rr: float
    qty: int
    day: str = ""
    levels: list[dict[str, Any]] = field(default_factory=list)  # drawn on the trade's chart
    confluence: list[str] = field(default_factory=list)
    exits: list[dict[str, Any]] = field(default_factory=list)
    exit_reason: str = ""
    exit_ts: int = 0
    r: float = 0.0
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    sessions_held: int = 0
    oi_state: str = ""
    oi_chg_pct: float | None = None
    oi_align: str = ""


def daily_table(d1: pd.DataFrame) -> pd.DataFrame:
    """Per session, from sessions BEFORE it only: trend, ADR, classic daily pivots, CPR, weekly pivots."""
    d = d1.sort_values("ts").copy()
    d["day"] = [ist_day(int(x)) for x in d["ts"]]
    ph, pl, pc = d["h"].shift(1), d["l"].shift(1), d["c"].shift(1)
    p = (ph + pl + pc) / 3
    bc = (ph + pl) / 2
    tc = 2 * p - bc
    d["P"], d["R1"], d["S1"], d["R2"], d["S2"] = p, 2 * p - pl, 2 * p - ph, p + (ph - pl), p - (ph - pl)
    d["TC"], d["BC"] = np.maximum(tc, bc), np.minimum(tc, bc)
    d["cpr_pct"] = (d["TC"] - d["BC"]) / p * 100
    d["PDH"], d["PDL"] = ph, pl
    sma20, sma50 = pc.rolling(20).mean(), pc.rolling(50).mean()
    d["trend"] = np.where((pc > sma50) & (sma20 > sma50), 1, np.where((pc < sma50) & (sma20 < sma50), -1, 0))
    d["adr"] = (d["h"] - d["l"]).shift(1).rolling(50, min_periods=25).mean()
    jump = (d["c"] / d["c"].shift(1) - 1).abs() > 0.35
    d.loc[jump.rolling(51, min_periods=1).max().astype(bool), "adr"] = np.nan
    wk = pd.Series([x.isocalendar()[:2] for x in d["day"]], index=d.index)
    w = d.groupby(wk.values).agg(h=("h", "max"), l=("l", "min"), c=("c", "last"))
    keys = list(w.index)
    prev = {keys[i]: w.iloc[i - 1] for i in range(1, len(keys))}
    for name, f in (("WP", lambda r: (r.h + r.l + r.c) / 3), ("WS1", lambda r: 2 * (r.h + r.l + r.c) / 3 - r.h), ("WR1", lambda r: 2 * (r.h + r.l + r.c) / 3 - r.l)):
        d[name] = [f(prev[k]) if k in prev else np.nan for k in wk.values]
    return d.set_index("day")


def _pivots(dl: pd.Series, side: int) -> dict[str, float]:
    names = ["P", "S1", "S2", "R1", "R2", "TC", "BC", "WP", "WS1", "WR1"]
    return {n: float(dl[n]) for n in names if not np.isnan(dl[n])}


def trades_for(
    symbol: str, df5: pd.DataFrame, d1: pd.DataFrame, p: FibParams, start: date, end: date, costs: CostModel,
    oi: pd.DataFrame | None = None,
) -> list[FibTrade]:
    df5 = clean_5m(df5)
    if df5.empty or d1.empty:
        return []
    daily = daily_table(d1)
    H = resample(df5, "60m")[["ts", "o", "h", "l", "c", "end"]].to_numpy(dtype=float)
    L = resample(df5, "15m")[["ts", "o", "h", "l", "c", "end"]].to_numpy(dtype=float)
    F = df5[["ts", "o", "h", "l", "c"]].to_numpy(dtype=float)
    sh, sl = swings(H[:, 2], H[:, 3], p.swing_side)
    lsh, lsl = swings(L[:, 2], L[:, 3], p.ltf_swing_side)
    lsh_a, lsl_a = np.array(lsh, dtype=int), np.array(lsl, dtype=int)
    sh_a, sl_a = np.array(sh, dtype=int), np.array(sl, dtype=int)
    zlo, zhi = ZONES[p.zone]
    lo_ts, hi_ts = ist_at(start, "00:00"), ist_at(end, "23:59")
    out: list[FibTrade] = []
    busy_until = 0.0
    broken: set[tuple[int, int]] = set()

    for k in range(1, len(H)):
        ts0, _o, _h, _l, c, close_ts = H[k]
        if close_ts < lo_ts or ts0 > hi_ts or close_ts <= busy_until:
            continue
        day = ist_day(int(ts0))
        if day not in daily.index:
            continue
        dl = daily.loc[day]
        adr, trend = dl["adr"], int(dl["trend"])
        if trend == 0 or not adr or np.isnan(adr):
            continue
        sign = trend
        if p.cpr_narrow and not (dl["cpr_pct"] < p.cpr_max_pct):
            continue
        # -- the break of structure on 1h, in the trend's direction ------------------------------
        pool = sh_a if sign > 0 else sl_a
        conf = pool[pool + p.swing_side < k]
        if not len(conf):
            continue
        s_i = int(conf[-1])
        ref = H[s_i, 2] if sign > 0 else H[s_i, 3]
        if (sign, s_i) in broken or not ((c - ref) * sign > 0 and (H[k - 1, 4] - ref) * sign <= 0):
            continue
        broken.add((sign, s_i))
        _f("1 break of structure with the trend")
        seg = slice(s_i, k + 1)
        leg_i = s_i + int(np.argmin(H[seg, 3]) if sign > 0 else np.argmax(H[seg, 2]))
        leg_start = H[leg_i, 3] if sign > 0 else H[leg_i, 2]
        # -- the leg must begin with a sweep, and move away with a gap ------------------------------
        pv = _pivots(dl, sign)
        if p.require_sweep:
            prior = [j for j in (sl if sign > 0 else sh) if j + p.swing_side < leg_i]
            levels = [H[j, 3] if sign > 0 else H[j, 2] for j in prior[-4:]]
            levels += [pv[n] for n in (("S1", "S2", "BC", "WS1") if sign > 0 else ("R1", "R2", "TC", "WR1")) if n in pv]
            levels += [float(dl["PDL"] if sign > 0 else dl["PDH"])]
            lb = H[leg_i]
            swept = any((lb[3] < lv < lb[4]) if sign > 0 else (lb[2] > lv > lb[4]) for lv in levels if not np.isnan(lv))
            if not swept:
                continue
        _f("2 leg began with a sweep")
        if p.require_gap:
            gaps = [j for j in range(leg_i + 2, k + 1) if ((H[j, 3] > H[j - 2, 2]) if sign > 0 else (H[j, 2] < H[j - 2, 3]))]
            if not gaps:
                continue
        _f("3 leg left a gap")
        # -- the order block: the last opposite candle before the leg rose ---------------------------
        ob = None
        for j in range(min(leg_i + 1, k), max(-1, leg_i - 4), -1):
            if (H[j, 4] < H[j, 1]) if sign > 0 else (H[j, 4] > H[j, 1]):
                ob = (H[j, 3], H[j, 2])
                break
        # -- wait for the pullback and the 15m turn -------------------------------------------------------
        expiry_day = ist_day(int(close_ts))
        sessions_seen: list[date] = []
        touched_j = None
        trig = None
        j0 = int(np.searchsorted(L[:, 5], close_ts))
        for j in range(j0, len(L)):
            dj = ist_day(int(L[j, 0]))
            if not sessions_seen or sessions_seen[-1] != dj:
                sessions_seen.append(dj)
                if len(sessions_seen) > p.expiry_sessions:
                    break
            if ((L[j, 4] < leg_start) if sign > 0 else (L[j, 4] > leg_start)):
                break  # closed beyond the leg's start: the idea is dead
            n_closed = int(np.searchsorted(H[:, 5], L[j, 5], side="right"))
            leg_end = float(H[leg_i:n_closed, 2].max() if sign > 0 else H[leg_i:n_closed, 3].min()) if n_closed > leg_i else float(H[k, 2] if sign > 0 else H[k, 3])
            span = abs(leg_end - leg_start)
            if span <= 0:
                continue
            band = sorted((leg_end - sign * zlo * span, leg_end - sign * zhi * span))
            if touched_j is None:
                if (L[j, 3] <= band[1]) if sign > 0 else (L[j, 2] >= band[0]):
                    touched_j = j
                    touch_band, touch_end = band, leg_end
                continue
            if not (ist_at(dj, p.first_entry) <= L[j, 5] <= ist_at(dj, p.last_entry)):
                continue
            pool_l = lsh_a if sign > 0 else lsl_a
            # the turn = a close beyond a 15m swing that formed AFTER price reached the zone (the
            # pullback's own lower high), not one left over from before the pullback
            confl = pool_l[(pool_l + p.ltf_swing_side < j) & (pool_l >= touched_j)] if len(pool_l) else pool_l
            if not len(confl):
                continue
            lref = L[confl[-1], 2] if sign > 0 else L[confl[-1], 3]
            if (L[j, 4] - lref) * sign > 0:
                # still at a discount: no entry above the 38.2 % retracement — that is chasing
                if (L[j, 4] - (leg_end - sign * p.max_entry_fib * span)) * sign > 0:
                    _f("5x turned too late (above max_entry_fib)")
                    break
                trig = j
                break
        if touched_j is not None:
            _f("4 pulled back into the zone")
        if trig is None or touched_j is None:
            continue
        _f("5 15m turned")
        # -- confluence at the zone ------------------------------------------------------------------------
        tol = p.pivot_tol_adr * adr
        lo_b, hi_b = touch_band[0] - tol, touch_band[1] + tol
        confluence = ["FIB"]
        if ob is not None and ob[0] <= hi_b and ob[1] >= lo_b:
            confluence.append("OB")
        hit = [n for n, v in pv.items() if lo_b <= v <= hi_b]
        if hit:
            confluence.append("PIVOT " + "/".join(hit))
        if len(confluence) < p.min_confluence:
            continue
        _f("6 confluence")
        # -- the plan ------------------------------------------------------------------------------------
        entry = L[trig, 4] * (1 + sign * p.slip_bps / 1e4)
        floor = min(leg_start, ob[0]) if (sign > 0 and ob) else max(leg_start, ob[1]) if ob else leg_start
        stop = floor - sign * p.stop_buffer_adr * adr
        risk = abs(entry - stop)
        if risk < p.min_stop_adr * adr:
            stop = entry - sign * p.min_stop_adr * adr
            risk = abs(entry - stop)
        if risk > p.max_stop_adr * adr:
            _f("7x stop wider than max_stop_adr")
            continue
        if (touch_end - entry) * sign <= 0:
            _f("7x entered at/above the leg high")
            continue
        t1 = touch_end
        span = abs(touch_end - leg_start)
        ext127, ext162 = touch_end + sign * 0.272 * span, touch_end + sign * 0.618 * span
        beyond = sorted((v for v in pv.values() if (v - t1) * sign > 0 and (ext162 - v) * sign >= 0), key=lambda v: (v - t1) * sign)
        t2 = beyond[0] if beyond else ext127
        rr = abs(t2 - entry) / risk
        if rr < p.min_rr:
            _f("7x R:R below min_rr")
            continue
        _f("7 stop and R:R fit")
        qty = int(p.risk_inr / risk)
        if qty <= 0:
            continue
        oi_state, oi_chg = "", None
        if oi is not None and len(oi):
            prev = oi[oi["day"] < ist_day(int(L[trig, 5])).isoformat()]
            if len(prev):
                oi_state, oi_chg = str(prev["label"].iloc[-1]), float(prev["oi_chg_pct"].iloc[-1])
        side = "BULLISH" if sign > 0 else "BEARISH"
        align = oi_alignment(side, oi_state)
        if p.oi_filter and align != p.oi_filter:
            continue
        _f("8 traded")
        fib = {f: touch_end - sign * f * span for f in (0.0, 0.382, 0.5, 0.618, 0.705, 0.786, 1.0)}
        lv = [{"price": round(v, 4), "title": f"fib {f:g}", "color": "#38bdf8" if zlo <= f <= zhi else "rgba(56,189,248,0.4)"} for f, v in fib.items()]
        if ob:
            lv += [{"price": round(ob[0], 4), "title": "OB", "color": "#a78bfa"}, {"price": round(ob[1], 4), "title": "OB", "color": "#a78bfa"}]
        lv += [{"price": round(v, 4), "title": n, "color": "rgba(245,158,11,0.7)"} for n, v in pv.items() if lo_b - 2 * adr <= v <= hi_b + 2 * adr]
        t = FibTrade(
            symbol, side, f"{p.zone} " + "+".join(c.split(" ")[0] for c in confluence), int(close_ts), round(leg_start, 4), round(touch_end, 4),
            int(L[trig, 5]), round(entry, 4), round(stop, 4), round(t1, 4), round(t2, 4), round(rr, 2), qty,
            day=ist_day(int(L[trig, 5])).isoformat(), levels=lv, confluence=confluence,
            oi_state=oi_state, oi_chg_pct=round(oi_chg, 2) if oi_chg is not None else None, oi_align=align,
        )
        _simulate(t, F, p, costs)  # type: ignore[arg-type]
        out.append(t)
        busy_until = t.exit_ts
        _ = expiry_day
    return out


def run(settings: Settings, start: date, end: date, p: FibParams, *, symbols: list[str] | None = None, label: str = "", on_symbol: Any = None) -> dict[str, Any]:
    from ..market.fo_bhavcopy import OiDailyStore
    from .oi_daily import futures_table

    store = HistoryStore(settings.data_dir / "history")
    costs = CostModel(settings)
    names = symbols or store.symbols("5m")
    oi_store = OiDailyStore(settings.data_dir / "oi_daily")
    oi_all = futures_table(oi_store, settings.data_dir / "oi_daily" / "_futures.parquet") if oi_store.days() else None
    t0 = _time.time()
    FUNNEL.clear()
    found: list[FibTrade] = []
    for n, sym in enumerate(names, 1):
        oi = oi_all[oi_all["symbol"] == sym] if oi_all is not None else None
        got = trades_for(sym, store.load(sym, "5m"), store.load(sym, "1d"), p, start, end, costs, oi)
        found += got
        if on_symbol:
            on_symbol(sym, got, n, len(names))
    trades = portfolio(found, p.max_open)  # type: ignore[arg-type]
    sm = summarize(trades)  # type: ignore[arg-type]
    groups: dict[str, list[float]] = {}
    for t in trades:
        for c in t.confluence:
            groups.setdefault(c.split(" ")[0], []).append(t.r)
    sm["by_confluence"] = {k: {"n": len(v), "avg_r": round(float(np.mean(v)), 3), "total_r": round(float(np.sum(v)), 2)} for k, v in sorted(groups.items())}
    out = {
        "id": f"fib-{uuid.uuid4().hex[:10]}", "kind": "mtf", "label": label, "created_ts": _time.time(),
        "seconds": round(_time.time() - t0, 1), "params": asdict(p), "range": [start.isoformat(), end.isoformat()],
        "candidates": len(found), "funnel": dict(sorted(FUNNEL.items())), "summary": sm, "trades": [asdict(t) for t in trades],
    }
    root = settings.data_dir / "obtests"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out
