"""Quality swing trades: a higher-timeframe reversal from a price-action level, positioning in
agreement, room for 5R, a pullback entry, and a stop that follows market structure.

Longs described; shorts are the mirror. Everything reads CLOSED bars only.

1. **Level (daily):** the day's low touches, within ``touch_adr`` of a normal day,
   - an equal low — a confirmed daily swing low of the last ``lookback`` days that no daily close
     has broken since (a double bottom), or
   - a broken high retested from above (an old swing high price closed through upward), or
   - the previous week's or previous month's pivot P / S1.
2. **Rejection:** that day leaves a long lower tail (tail ≥ ``tail_body`` × body and ≥ half the
   range, close in the top ``close_top`` of the range) on volume ≥ ``vol_mult`` × its 20-day average.
3. **New momentum:** within ``confirm_days`` days, a daily CLOSE above the last lower high (the
   latest confirmed daily swing high before the rejection), with no close below the rejection low —
   the change from lower highs to a higher high. The rejection low is the new higher low.
4. **Positioning (optional):** the 64-state bias of the confirmation day agrees (Bull / Strong Bull).
5. **Room:** the nearest resistance above (an untaken daily swing high of the last 250 days, the
   previous month's high or monthly R1) at least ``room_r`` × the risk away.
6. **Entry:** ``entry="daily"`` — the next day's open; ``entry="1h"`` — after the confirmation, a 1h
   pullback of ≥ ``pullback`` of the move off the low, then a 1h close above a 1h swing high formed
   since; expires after ``entry_days`` sessions.
7. **Stop** under the rejection low (``stop_buffer_adr``). **Trail by structure:** every newly
   confirmed daily swing low above the stop becomes the stop. No target, no time limit (a
   ``max_sessions`` safety net), no fixed %. Held as a stock future, rolled monthly (a round trip of
   charges per ~20 sessions).
"""

from __future__ import annotations

import json
import math
import statistics
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
from .mtf import clean_5m, resample, swings
from .obtest import _fut

FUNNEL: dict[str, int] = {}


def _f(step: str) -> None:
    FUNNEL[step] = FUNNEL.get(step, 0) + 1


@dataclass(frozen=True, slots=True)
class SwingParams:
    lookback: int = 120
    touch_adr: float = 0.15
    tail_body: float = 2.0
    close_top: float = 0.4
    vol_mult: float = 1.2
    confirm_days: int = 10
    use_bias: bool = True
    room_r: float = 5.0
    entry: str = "daily"  # daily | 1h
    pullback: float = 0.382
    entry_days: int = 10
    stop_buffer_adr: float = 0.1
    min_stop_adr: float = 0.3
    max_stop_adr: float = 3.0
    max_sessions: int = 250
    risk_inr: float = 5_000.0
    slip_bps: float = 5.0
    max_open: int = 8
    sides: str = "both"  # both | long | short


@dataclass(slots=True)
class SwingTrade:
    symbol: str
    side: str
    setup: str
    sweep_ts: int  # rejection day (for the chart)
    level: float  # the level touched
    wick: float  # the rejection low (the new higher low)
    signal_ts: int  # entry time
    entry: float
    stop: float
    t1: float  # the resistance used for the room test (drawn; not a target)
    t2: float
    rr: float  # room in R
    qty: int
    day: str = ""
    levels: list[dict[str, Any]] = field(default_factory=list)
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
    stop_path: list[dict[str, Any]] = field(default_factory=list)


def _period_pivots(d: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    g = d.groupby(key.values).agg(h=("h", "max"), l=("l", "min"), c=("c", "last"))
    keys = list(g.index)
    prev = {keys[i]: g.iloc[i - 1] for i in range(1, len(keys))}
    out = pd.DataFrame(index=d.index)
    for name, f in (
        ("P", lambda r: (r.h + r.l + r.c) / 3),
        ("S1", lambda r: 2 * (r.h + r.l + r.c) / 3 - r.h),
        ("R1", lambda r: 2 * (r.h + r.l + r.c) / 3 - r.l),
        ("H", lambda r: r.h),
        ("L", lambda r: r.l),
    ):
        out[name] = [f(prev[k]) if k in prev else np.nan for k in key.values]
    return out


def daily_frame(d1: pd.DataFrame) -> pd.DataFrame:
    d = d1.sort_values("ts").drop_duplicates("ts").reset_index(drop=True).copy()
    d["day"] = [ist_day(int(x)) for x in d["ts"]]
    d = d.drop_duplicates("day", keep="last").reset_index(drop=True)
    d["adr"] = (d["h"] - d["l"]).shift(1).rolling(50, min_periods=25).mean()
    d["vavg"] = d["v"].shift(1).rolling(20, min_periods=10).mean()
    jump = (d["c"] / d["c"].shift(1) - 1).abs() > 0.35
    d.loc[jump.rolling(51, min_periods=1).max().astype(bool), "adr"] = np.nan
    wk = pd.Series([x.isocalendar()[:2] for x in d["day"]], index=d.index)
    mo = pd.Series([(x.year, x.month) for x in d["day"]], index=d.index)
    w, m = _period_pivots(d, wk), _period_pivots(d, mo)
    for c in ("P", "S1", "R1"):
        d["W" + c], d["M" + c] = w[c], m[c]
    d["MH"], d["ML"] = m["H"], m["L"]
    return d


def setups(d: pd.DataFrame, p: SwingParams, bias: dict[str, str] | None) -> list[dict[str, Any]]:
    """Every confirmed reversal: the rejection day, the level, the confirmation day, the plan."""
    o, h, lo, c, v = (d[x].to_numpy(dtype=float) for x in ("o", "h", "l", "c", "v"))
    adr, vavg = d["adr"].to_numpy(dtype=float), d["vavg"].to_numpy(dtype=float)
    sh, sl = swings(h, lo, 2)
    sh_a, sl_a = np.array(sh, dtype=int), np.array(sl, dtype=int)
    out: list[dict[str, Any]] = []
    for k in range(60, len(d) - 1):
        a = adr[k]
        if not a or np.isnan(a) or np.isnan(vavg[k]) or vavg[k] <= 0:
            continue
        rng = h[k] - lo[k]
        if rng <= 0:
            continue
        body = abs(c[k] - o[k])
        for sign in (1, -1):
            if (p.sides == "long" and sign < 0) or (p.sides == "short" and sign > 0):
                continue
            tail = (min(o[k], c[k]) - lo[k]) if sign > 0 else (h[k] - max(o[k], c[k]))
            close_pos = (c[k] - lo[k]) / rng if sign > 0 else (h[k] - c[k]) / rng
            if not (tail >= p.tail_body * max(body, 1e-9) and tail >= 0.5 * rng and close_pos >= 1 - p.close_top):
                continue
            if v[k] < p.vol_mult * vavg[k]:
                continue
            _f("1 rejection candle on volume")
            extreme = lo[k] if sign > 0 else h[k]
            tol = p.touch_adr * a
            levels: list[tuple[str, float]] = []
            # equal lows (double bottom): confirmed swing lows not closed through since
            pool = sl_a if sign > 0 else sh_a
            for i in pool[(pool + 2 < k) & (pool >= k - p.lookback)]:
                lvl = lo[i] if sign > 0 else h[i]
                after = c[i + 1 : k]
                intact = (after.min(initial=np.inf) > lvl) if sign > 0 else (after.max(initial=-np.inf) < lvl)
                if intact and abs(extreme - lvl) <= tol and k - i >= 5:
                    levels.append(("DOUBLE " + ("BOTTOM" if sign > 0 else "TOP"), lvl))
            # broken high retested from above (role reversal)
            other = sh_a if sign > 0 else sl_a
            for i in other[(other + 2 < k) & (other >= k - p.lookback)]:
                lvl = h[i] if sign > 0 else lo[i]
                broke = (c[i + 1 : k].max(initial=-np.inf) > lvl) if sign > 0 else (c[i + 1 : k].min(initial=np.inf) < lvl)
                if broke and abs(extreme - lvl) <= tol and ((c[k] > lvl) if sign > 0 else (c[k] < lvl)):
                    levels.append(("RETEST", lvl))
            for name in ("WP", "MP") + (("WS1", "MS1") if sign > 0 else ("WR1", "MR1")):
                lvl = d[name].iloc[k]
                if not np.isnan(lvl) and abs(extreme - lvl) <= tol:
                    levels.append(("PIVOT " + name, float(lvl)))
            if not levels:
                continue
            _f("2 at a price-action level")
            # the lower high that must break
            lh_pool = sh_a if sign > 0 else sl_a
            prior = lh_pool[lh_pool + 2 <= k]
            if not len(prior):
                continue
            lh = h[prior[-1]] if sign > 0 else lo[prior[-1]]
            conf = None
            for j in range(k + 1, min(len(d), k + 1 + p.confirm_days)):
                if (c[j] < extreme) if sign > 0 else (c[j] > extreme):
                    break
                if (c[j] > lh) if sign > 0 else (c[j] < lh):
                    conf = j
                    break
            if conf is None:
                continue
            _f("3 new momentum (broke the last lower high)")
            side = "BULLISH" if sign > 0 else "BEARISH"
            b = (bias or {}).get(d["day"].iloc[conf].isoformat(), "")
            if p.use_bias and bias is not None:
                ok = b in ("Bull", "Strong Bull") if sign > 0 else b in ("Bear", "Strong Bear")
                if not ok:
                    continue
                _f("4 positioning agrees")
            out.append({
                "k": k, "conf": conf, "side": side, "sign": sign, "extreme": float(extreme), "level": levels[0],
                "levels": levels, "lh": float(lh), "adr": float(a), "bias": b,
            })
    return out


def resistance_above(d: pd.DataFrame, j: int, price: float, sign: int) -> float | None:
    """The nearest overhead resistance at day j: untaken daily swing highs of the last 250 days, the
    previous month's high and monthly R1 (mirrored for shorts)."""
    h, lo, c = (d[x].to_numpy(dtype=float) for x in ("h", "l", "c"))
    sh, sl = swings(h[: j + 1], lo[: j + 1], 2)
    cands: list[float] = []
    for i in (sh if sign > 0 else sl):
        if i + 2 > j or i < j - 250:
            continue
        lvl = h[i] if sign > 0 else lo[i]
        later = c[i + 1 : j + 1]
        intact = (later.max(initial=-np.inf) < lvl) if sign > 0 else (later.min(initial=np.inf) > lvl)
        if intact:
            cands.append(lvl)
    for name in ("MH", "MR1") if sign > 0 else ("ML", "MS1"):
        x = d[name].iloc[j]
        if not np.isnan(x):
            cands.append(float(x))
    ahead = [x for x in cands if (x - price) * sign > 0]
    return min(ahead, key=lambda x: abs(x - price)) if ahead else None


def simulate(t: SwingTrade, d: pd.DataFrame, start_i: int, F: np.ndarray | None, p: SwingParams, costs: CostModel, adr: float) -> None:
    """Daily structure trail. Fills from 5m bars when we have them for the day, else the daily bar."""
    sign = 1 if t.side == "BULLISH" else -1
    risk = abs(t.entry - t.stop)
    stop = t.stop
    h, lo = d["h"].to_numpy(dtype=float), d["l"].to_numpy(dtype=float)
    o = d["o"].to_numpy(dtype=float)
    sessions = 0
    exit_px = None
    exit_ts = 0
    why = ""
    for i in range(start_i, len(d)):
        sessions += 1
        day = d["day"].iloc[i]
        day_lo, day_hi, day_o = lo[i], h[i], o[i]
        first = i == start_i
        if F is not None:
            rows = F[(F[:, 0] >= ist_at(day, "09:15")) & (F[:, 0] < ist_at(day, "15:30"))]
            if first:
                rows = rows[rows[:, 0] >= t.signal_ts]
            if len(rows):
                day_lo, day_hi, day_o = rows[:, 3].min(), rows[:, 2].max(), rows[0, 1]
        t.mfe_r = max(t.mfe_r, ((day_hi - t.entry) if sign > 0 else (t.entry - day_lo)) / risk)
        t.mae_r = max(t.mae_r, ((t.entry - day_lo) if sign > 0 else (day_hi - t.entry)) / risk)
        if (sign > 0 and day_lo <= stop) or (sign < 0 and day_hi >= stop):
            fill = (min(day_o, stop) if sign > 0 else max(day_o, stop)) if not first else stop
            exit_px = fill * (1 - sign * p.slip_bps / 1e4)
            exit_ts = ist_at(day, "15:30")
            why = "STOP" if abs(stop - t.stop) < 1e-9 else "TRAIL"
            break
        # the day closed: any daily swing confirmed now (2 bars after it) above the stop raises it
        if i >= 4:
            m = i - 2
            is_sw = (lo[m] < lo[m - 2 : m].min() and lo[m] < lo[m + 1 : m + 3].min()) if sign > 0 else (h[m] > h[m - 2 : m].max() and h[m] > h[m + 1 : m + 3].max())
            if is_sw and m >= start_i - 1:
                cand = (lo[m] if sign > 0 else h[m]) - sign * p.stop_buffer_adr * adr
                if (cand - stop) * sign > 0:
                    stop = cand
                    t.stop_path.append({"ts": ist_at(day, "15:30"), "stop": round(float(stop), 4)})
        if sessions >= p.max_sessions:
            exit_px = d["c"].iloc[i] * (1 - sign * p.slip_bps / 1e4)
            exit_ts = ist_at(day, "15:30")
            why = "MAX_SESSIONS"
            break
    if exit_px is None:
        i = len(d) - 1
        exit_px = d["c"].iloc[i]
        exit_ts = ist_at(d["day"].iloc[i], "15:30")
        why = "OPEN"
    t.exits = [{"ts": int(exit_ts), "price": round(float(exit_px), 4), "frac": 1.0, "why": why}]
    t.exit_reason, t.exit_ts, t.sessions_held = why, int(exit_ts), sessions
    t.gross = sign * (exit_px - t.entry) * t.qty
    rolls = max(0, sessions // 20)
    fut = _fut(t.symbol)
    t.costs = costs.round_trip(fut, t.entry, float(exit_px), t.qty).total + rolls * costs.round_trip(fut, t.entry, t.entry, t.qty).total
    t.net = t.gross - t.costs
    t.r = round(t.net / (risk * t.qty), 3)


def trades_for(symbol: str, d1: pd.DataFrame, df5: pd.DataFrame | None, p: SwingParams, start: date, end: date, costs: CostModel, bias: dict[str, str] | None) -> list[SwingTrade]:
    if d1.empty:
        return []
    d = daily_frame(d1)
    F = clean_5m(df5)[["ts", "o", "h", "l", "c"]].to_numpy(dtype=float) if df5 is not None and not df5.empty else None
    H = resample(clean_5m(df5), "60m")[["ts", "o", "h", "l", "c", "end"]].to_numpy(dtype=float) if (F is not None and p.entry == "1h") else None
    out: list[SwingTrade] = []
    busy_until = -1
    for s in setups(d, p, bias):
        conf, sign = s["conf"], s["sign"]
        cday = d["day"].iloc[conf]
        if cday < start or cday > end or conf <= busy_until:
            continue
        a = s["adr"]
        stop = s["extreme"] - sign * p.stop_buffer_adr * a
        if p.entry == "daily":
            if conf + 1 >= len(d):
                continue
            ei = conf + 1
            entry = float(d["o"].iloc[ei]) * (1 + sign * p.slip_bps / 1e4)
            entry_ts = ist_at(d["day"].iloc[ei], "09:15")
        else:
            if H is None:
                continue
            got = _entry_1h(H, s, d, conf, p)
            if got is None:
                continue
            entry_ts, entry = got
            entry *= 1 + sign * p.slip_bps / 1e4
            eday = ist_day(int(entry_ts))
            ei = int(d.index[d["day"] == eday][0]) if (d["day"] == eday).any() else None
            if ei is None:
                continue
        risk = abs(entry - stop)
        if risk < p.min_stop_adr * a:
            stop = entry - sign * p.min_stop_adr * a
            risk = abs(entry - stop)
        if risk > p.max_stop_adr * a or (entry - stop) * sign <= 0:
            _f("5x stop too wide / entry beyond the stop")
            continue
        res = resistance_above(d, conf, entry, sign)
        room = abs(res - entry) / risk if res is not None else math.inf
        if room < p.room_r:
            _f("5x no room for room_r")
            continue
        _f("5 room for room_r")
        qty = int(p.risk_inr / risk)
        if qty <= 0:
            continue
        name, lvl = s["level"]
        lv = [{"price": round(lvl, 4), "title": name.lower(), "color": "#a78bfa"},
              {"price": round(s["lh"], 4), "title": "lower high broken", "color": "#38bdf8"}]
        t = SwingTrade(
            symbol, s["side"], name, ist_at(d["day"].iloc[s["k"]], "15:30"), round(lvl, 4), round(s["extreme"], 4),
            int(entry_ts), round(entry, 4), round(stop, 4), round(res, 4) if res is not None else round(entry + sign * 10 * risk, 4),
            round(res, 4) if res is not None else round(entry + sign * 10 * risk, 4), round(room, 2) if room != math.inf else 99.0, qty,
            day=d["day"].iloc[ei].isoformat(), levels=lv, confluence=[x[0] for x in s["levels"]] + ([s["bias"]] if s["bias"] else []),
            oi_state=s["bias"],
        )
        simulate(t, d, ei, F, p, costs, a)
        out.append(t)
        _f("6 traded")
        busy_until = int(d.index[d["day"] == ist_day(t.exit_ts)][0]) if (d["day"] == ist_day(t.exit_ts)).any() else len(d)
    return out


def _entry_1h(H: np.ndarray, s: dict[str, Any], d: pd.DataFrame, conf: int, p: SwingParams) -> tuple[int, float] | None:
    sign = s["sign"]
    start_ts = ist_at(d["day"].iloc[conf], "15:30")
    j0 = int(np.searchsorted(H[:, 5], start_ts + 1))
    hi_since = s["lh"]
    pulled = None
    sessions: list[date] = []
    for j in range(j0, len(H)):
        dj = ist_day(int(H[j, 0]))
        if not sessions or sessions[-1] != dj:
            sessions.append(dj)
            if len(sessions) > p.entry_days:
                return None
        if (H[j, 4] < s["extreme"]) if sign > 0 else (H[j, 4] > s["extreme"]):
            return None
        span = abs(hi_since - s["extreme"])
        if pulled is None:
            hi_since = max(hi_since, H[j, 2]) if sign > 0 else min(hi_since, H[j, 3])
            depth = (hi_since - H[j, 3]) if sign > 0 else (H[j, 2] - hi_since)
            if span > 0 and depth >= p.pullback * span:
                pulled = j
            continue
        # a 1h swing high formed since the pullback started, then a close above it
        seg = H[pulled : j]
        if len(seg) < 3:
            continue
        ref = seg[:, 2].max() if sign > 0 else seg[:, 3].min()
        if (H[j, 4] - ref) * sign > 0:
            return int(H[j, 5]), float(H[j, 4])
    return None


def summarize(trades: list[SwingTrade]) -> dict[str, Any]:
    rs = [t.r for t in trades]
    if not rs:
        return {"trades": 0, "total_r": 0.0, "by_month": {}, "by_exit": {}, "by_setup": {}, "by_side": {}, "by_oi_state": {}, "by_oi_align": {},
                "win_rate": None, "avg_r": None, "net_inr": 0, "costs_inr": 0, "costs_r_per_trade": None, "t_by_week": None, "weeks": 0,
                "months_positive": 0, "months_negative": 0, "avg_sessions_held": None}
    months: dict[str, float] = {}
    weeks: dict[str, float] = {}
    for t in trades:
        months[t.day[:7]] = months.get(t.day[:7], 0.0) + t.r
        y, w, _ = date.fromisoformat(t.day).isocalendar()
        weeks[f"{y}-W{w:02d}"] = weeks.get(f"{y}-W{w:02d}", 0.0) + t.r
    mv = list(months.values())
    wv = list(weeks.values())
    sd = statistics.stdev(wv) if len(wv) > 1 else 0.0

    def group(key: Any) -> dict[str, Any]:
        g: dict[str, list[float]] = {}
        for t in trades:
            g.setdefault(key(t), []).append(t.r)
        return {k: {"n": len(v), "avg_r": round(statistics.fmean(v), 3), "total_r": round(sum(v), 2)} for k, v in sorted(g.items())}

    big = [r for r in rs if r >= 5]
    return {
        "trades": len(trades),
        "win_rate": round(100 * sum(r > 0 for r in rs) / len(rs), 1),
        "avg_r": round(statistics.fmean(rs), 3),
        "median_r": round(statistics.median(rs), 3),
        "total_r": round(sum(rs), 2),
        "five_r_plus": len(big),
        "best_r": round(max(rs), 2),
        "net_inr": round(sum(t.net for t in trades)),
        "costs_inr": round(sum(t.costs for t in trades)),
        "costs_r_per_trade": round(statistics.fmean([t.costs / (abs(t.entry - t.stop) * t.qty) for t in trades]), 3),
        "t_by_week": round(statistics.fmean(wv) / (sd / math.sqrt(len(wv))), 2) if sd > 0 else None,
        "weeks": len(wv),
        "months_positive": sum(x > 0 for x in mv),
        "months_negative": sum(x < 0 for x in mv),
        "avg_sessions_held": round(statistics.fmean([t.sessions_held for t in trades]), 1),
        "by_month": {k: round(v, 2) for k, v in sorted(months.items())},
        "by_exit": group(lambda t: t.exit_reason),
        "by_setup": group(lambda t: t.setup.split(" ")[0]),
        "by_side": group(lambda t: t.side),
        "by_oi_state": group(lambda t: t.oi_state or "—"),
        "by_oi_align": {},
    }


def run(settings: Settings, start: date, end: date, p: SwingParams, *, symbols: list[str] | None = None, label: str = "") -> dict[str, Any]:
    from ..market.fo_bhavcopy import OiDailyStore
    from .oi16 import state64_table

    store = HistoryStore(settings.data_dir / "history")
    costs = CostModel(settings)
    names = symbols or store.symbols("1d")
    root = settings.data_dir / "oi_daily"
    st = state64_table(OiDailyStore(root), root) if p.use_bias and OiDailyStore(root).days() else None
    t0 = _time.time()
    FUNNEL.clear()
    found: list[SwingTrade] = []
    for sym in names:
        bias = dict(zip(st[st["symbol"] == sym]["day"], st[st["symbol"] == sym]["bias"], strict=True)) if st is not None else None
        bias = {k if isinstance(k, str) else k.isoformat(): v for k, v in bias.items()} if bias else bias
        df5 = store.load(sym, "5m")  # fills from 5m wherever the cache has the day
        found += trades_for(sym, store.load(sym, "1d"), df5, p, start, end, costs, bias)
    found.sort(key=lambda x: x.signal_ts)
    taken: list[SwingTrade] = []
    for t in found:
        if sum(1 for x in taken if x.signal_ts <= t.signal_ts < x.exit_ts) < p.max_open:
            taken.append(t)
    out = {
        "id": f"swing-{uuid.uuid4().hex[:10]}", "kind": "mtf", "label": label, "created_ts": _time.time(),
        "seconds": round(_time.time() - t0, 1), "params": asdict(p), "range": [start.isoformat(), end.isoformat()],
        "candidates": len(found), "funnel": dict(sorted(FUNNEL.items())), "summary": summarize(taken),
        "trades": [asdict(t) for t in taken],
    }
    root2 = settings.data_dir / "obtests"
    root2.mkdir(parents=True, exist_ok=True)
    (root2 / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out
