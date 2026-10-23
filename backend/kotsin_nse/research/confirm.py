"""Confirmed entries on the 5-minute chart: the double bottom / top, and the CPR retest.

Both wait for confirmation — the candle where the pattern first shows is an alert, never the entry.
Built from the 5-minute cache on the session grid (``mtf.clean_5m``); 15m and 1h bars are resampled
from it and read only once CLOSED. One trade is a stock future sized to ``risk_inr``; costs are
``risk/costs.py`` with ``slip_bps`` on the market fills; a stock is traded only on days it had stock
futures (``market/fo_universe.py``).

**Double bottom** (top mirrored). L1 = a 5m swing low (``side`` higher lows each side). The rally from
it makes the neckline = the highest high after L1. L2 = the lowest low after the neckline bar, within
``tol_atr`` ATR of L1 (a higher low or a small undercut — recorded), at least ``min_gap`` bars after
L1; the pattern depth (neckline − the lower low) is at least ``min_depth_atr`` ATR. No low in between
breaks L1 by more than the tolerance. **Entry: the first 5m CLOSE above the neckline**, within
``max_wait`` bars of L2 and between ``first_entry`` and ``last_entry``. Stop: the lower low −
``stop_buffer_atr`` ATR. Target: the measured move, neckline + depth.

**CPR retest** (the owner's rule, long; short mirrored). The session's central pivot range from the
previous session (BC, P, TC; R1 / S1). A 5m close above TC, later a 5m bar that comes back to TC
(low ≤ TC + ``retest_tol_atr`` ATR) and CLOSES above it — the retest held — while the last closed 15m
Supertrend(10, 3) is up. Entry at that close; stop under the retest low − ``stop_buffer_atr`` ATR;
target R1 (R2 when R1 is under 1 R away). One trade per side per session. (The rule's 1-minute step
cannot be tested: the 1m cache starts inside the holdout.)

Both exit at the stop, the target, or the session's last bar — intraday only. A bar that touches the
stop and the target is counted a stop.

**What a hit rate means here.** The target and the stop are not equally far, so a coin would hit the
nearer one more often: ``p_random`` = stop distance / (stop + target distance) is what a random walk
scores on the same trade. A pattern helps only if it beats that — and the average R after costs is 0
for a random walk before costs, negative after.

Recorded per trade, for the read-out (decided before the run, not tuned): the second low's volume
against the first (selling exhausted?), higher low vs undercut, at a daily pivot or not, the 1h and
15m Supertrend with or against the trade, the time of day.
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
from .mtf import clean_5m, resample
from .obtest import _fut

HOLDOUT_START = date(2025, 10, 1)
PIVOT_NAMES = ("P", "BC", "TC", "R1", "S1", "R2", "S2", "PDH", "PDL")


@dataclass(frozen=True, slots=True)
class ConfirmParams:
    setup: str = "double"  # double | cpr
    side: int = 2
    min_gap: int = 3
    tol_atr: float = 0.5
    min_depth_atr: float = 1.0
    max_wait: int = 12
    stop_buffer_atr: float = 0.1
    retest_tol_atr: float = 0.1
    min_t1_r: float = 1.0
    first_entry: str = "09:30"
    last_entry: str = "14:30"
    pivot_tol_atr: float = 0.5
    risk_inr: float = 5_000.0
    slip_bps: float = 5.0
    fo_members: bool = True


@dataclass(slots=True)
class ConfirmTrade:
    symbol: str
    day: str
    setup: str
    side: str  # LONG | SHORT
    signal_ts: int
    entry: float
    stop: float
    target: float
    qty: int
    p_random: float
    exit_ts: int = 0
    exit: float = 0.0
    exit_reason: str = ""
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    r: float = 0.0
    tags: dict[str, str] = field(default_factory=dict)


# -- indicators ---------------------------------------------------------------------------------------


def atr(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - lo, np.abs(h - pc), np.abs(lo - pc)])
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def supertrend(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 10, m: float = 3.0) -> np.ndarray:
    """Direction (+1 up / −1 down) of the usual Supertrend: ATR(n) (Wilder) bands ``m`` × ATR around
    the bar's (H + L) / 2, each band ratcheting until a close crosses it."""
    a = atr(h, lo, c, n)
    mid = (h + lo) / 2
    ub, lb = mid + m * a, mid - m * a
    fu, fl = ub.copy(), lb.copy()
    d = np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1] else fu[i - 1]
        fl[i] = lb[i] if lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1] else fl[i - 1]
        if d[i - 1] < 0 and c[i] > fu[i - 1]:
            d[i] = 1
        elif d[i - 1] > 0 and c[i] < fl[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
    return d


def cpr(h: float, lo: float, c: float) -> dict[str, float]:
    p = (h + lo + c) / 3
    bc, tc = sorted(((h + lo) / 2, 2 * p - (h + lo) / 2))
    return {"P": p, "BC": bc, "TC": tc, "R1": 2 * p - lo, "S1": 2 * p - h, "R2": p + (h - lo), "S2": p - (h - lo), "PDH": h, "PDL": lo}


# -- the double bottom / top --------------------------------------------------------------------------


def doubles(h: np.ndarray, lo: np.ndarray, c: np.ndarray, a: np.ndarray, p: ConfirmParams, ok_entry: np.ndarray) -> list[dict[str, Any]]:
    """Double bottoms in one session's bars (pass −lo, −h, −c for tops). Each is read at its trigger
    bar ``k`` from bars ≤ k only. One per trigger bar."""
    n, s = len(c), p.side
    out: dict[int, dict[str, Any]] = {}
    for i1 in range(s, n - s):
        l1 = lo[i1]
        if not ((lo[i1 - s : i1] > l1).all() and (lo[i1 + 1 : i1 + s + 1] > l1).all()):
            continue
        neck, jn = -np.inf, -1
        for k in range(i1 + 1, n):
            tol = p.tol_atr * a[k]
            if lo[k] < l1 - tol:
                break  # L1 broken: no double bottom from it
            if jn >= 0 and c[k] > neck and k > jn + 1:  # a close above the high after a pullback from it
                i2 = jn + 1 + int(np.argmin(lo[jn + 1 : k]))
                l2 = lo[i2]
                low = min(l1, l2)
                if abs(l2 - l1) <= tol:  # the pullback came back to L1: this is the double bottom's breakout
                    if (i2 - i1 >= p.min_gap and k - i2 <= p.max_wait and neck - low >= p.min_depth_atr * a[k]
                            and ok_entry[k] and k not in out):
                        out[k] = {"k": k, "i1": i1, "i2": i2, "jn": jn, "l1": float(l1), "l2": float(l2), "neck": float(neck)}
                    break  # used up, taken or not
                # a shallow pullback (a higher low well above L1): the rally goes on, the neckline moves up
            if h[k] > neck:
                neck, jn = h[k], k
    return list(out.values())


# -- simulation ---------------------------------------------------------------------------------------


def exit_trade(t: ConfirmTrade, Op: np.ndarray, H: np.ndarray, L: np.ndarray, C: np.ndarray, T: np.ndarray, k: int, end: int, p: ConfirmParams,
          costs: CostModel, bar_s: int = 300) -> None:
    sign = 1 if t.side == "LONG" else -1
    for j in range(k + 1, end):
        if (sign > 0 and L[j] <= t.stop) or (sign < 0 and H[j] >= t.stop):
            px = (min(Op[j], t.stop) if sign > 0 else max(Op[j], t.stop)) * (1 - sign * p.slip_bps / 1e4)
            t.exit, t.exit_ts, t.exit_reason = float(px), int(T[j]) + bar_s, "STOP"
            break
        if (sign > 0 and H[j] >= t.target) or (sign < 0 and L[j] <= t.target):
            t.exit, t.exit_ts, t.exit_reason = t.target, int(T[j]) + bar_s, "TARGET"
            break
    else:
        j = end - 1
        t.exit, t.exit_ts, t.exit_reason = float(C[j]) * (1 - sign * p.slip_bps / 1e4), int(T[j]) + bar_s, "SESSION_END"
    risk = abs(t.entry - t.stop)
    t.gross = sign * (t.exit - t.entry) * t.qty
    t.costs = costs.round_trip(_fut(t.symbol), t.entry, t.exit, t.qty).total
    t.net = t.gross - t.costs
    t.r = round(t.net / (risk * t.qty), 3)


def make_trade(sym: str, day: date, setup: str, sign: int, k: int, close: float, stop: float, target: float,
           T: np.ndarray, p: ConfirmParams, tags: dict[str, str], bar_s: int = 300) -> ConfirmTrade | None:
    entry = close * (1 + sign * p.slip_bps / 1e4)
    risk, reward = sign * (entry - stop), sign * (target - entry)
    if risk <= 0 or reward <= 0:
        return None
    return ConfirmTrade(sym, day.isoformat(), setup, "LONG" if sign > 0 else "SHORT", int(T[k]) + bar_s, round(entry, 4),
                        round(stop, 4), round(target, 4), max(1, int(p.risk_inr / risk)), round(risk / (risk + reward), 3), tags=tags)


def _htf_dir(df5: pd.DataFrame, tf: str) -> tuple[np.ndarray, np.ndarray]:
    b = resample(df5, tf)
    d = supertrend(b["h"].to_numpy(float), b["l"].to_numpy(float), b["c"].to_numpy(float))
    return b["end"].to_numpy(np.int64), d


def _dir_at(ends: np.ndarray, d: np.ndarray, t: int) -> int:
    i = int(np.searchsorted(ends, t, side="right")) - 1
    return int(d[i]) if i >= 0 else 0


def _tod(ts: int, day: date) -> str:
    m = (ts - ist_at(day, "09:15")) // 60
    return "09:30-10:30" if m < 75 else "10:30-13:00" if m < 225 else "13:00-15:30"


def trades_for(sym: str, df5: pd.DataFrame, p: ConfirmParams, start: date, end: date, costs: CostModel, member: Any = None) -> list[ConfirmTrade]:
    df5 = clean_5m(df5)
    if df5.empty:
        return []
    T = df5["ts"].to_numpy(np.int64)
    Op, H, L, C, V = (df5[x].to_numpy(float) for x in ("o", "h", "l", "c", "v"))
    A = atr(H, L, C)
    e15, d15 = _htf_dir(df5, "15m")
    e60, d60 = _htf_dir(df5, "60m")
    dkey = (T + 19_800) // 86_400  # the IST calendar day of each bar
    cut = np.flatnonzero(np.r_[True, dkey[1:] != dkey[:-1]])
    bounds = list(zip(cut, np.r_[cut[1:], len(T)], strict=True))
    out: list[ConfirmTrade] = []
    for n_s, (a0, a1) in enumerate(bounds):
        day = ist_day(int(T[a0]))
        if n_s == 0 or not (start <= day <= end) or (member is not None and not member(sym, day)):
            continue
        b0, b1 = bounds[n_s - 1]
        lv = cpr(H[b0:b1].max(), L[b0:b1].min(), C[b1 - 1])
        ok = (T[a0:a1] + 300 >= ist_at(day, p.first_entry)) & (T[a0:a1] + 300 <= ist_at(day, p.last_entry))
        h, lo, c, a = H[a0:a1], L[a0:a1], C[a0:a1], A[a0:a1]

        def tags(sign: int, k: int, extra: dict[str, str], a0: int = a0, day: date = day) -> dict[str, str]:
            t = int(T[a0 + k]) + 300
            with_ = lambda d: "with" if d == sign else "against"  # noqa: E731
            return {"tod": _tod(t, day), "st15": with_(_dir_at(e15, d15, t)), "st60": with_(_dir_at(e60, d60, t)), **extra}

        if p.setup == "double":
            for sign in (1, -1):
                hh, ll, cc = (h, lo, c) if sign > 0 else (-lo, -h, -c)
                for s in doubles(hh, ll, cc, a, p, ok):
                    k = s["k"]
                    low = min(s["l1"], s["l2"])
                    depth = s["neck"] - low
                    stop = sign * (low - p.stop_buffer_atr * a[k])
                    target = sign * (s["neck"] + depth)
                    v1 = V[a0 + max(0, s["i1"] - 1) : a0 + s["i1"] + 2].sum()
                    v2 = V[a0 + max(0, s["i2"] - 1) : a0 + s["i2"] + 2].sum()
                    near = min(PIVOT_NAMES, key=lambda nm: abs(lv[nm] - sign * low))
                    extra = {
                        "exhaustion": "lighter 2nd test" if v2 < v1 else "heavier 2nd test",
                        "shape": "higher low" if s["l2"] > s["l1"] else "undercut",
                        "pivot": f"at {near}" if abs(lv[near] - sign * low) <= p.pivot_tol_atr * a[k] else "no pivot",
                    }
                    tr = make_trade(sym, day, "double", sign, a0 + k, C[a0 + k], stop, target, T, p, tags(sign, k, extra))
                    if tr:
                        exit_trade(tr, Op, H, L, C, T, a0 + k, a1, p, costs)
                        out.append(tr)
        elif p.setup == "cpr":
            for sign, lvl, tgt1, tgt2 in ((1, lv["TC"], lv["R1"], lv["R2"]), (-1, lv["BC"], lv["S1"], lv["S2"])):
                beyond = False
                for k in range(len(c)):
                    t = int(T[a0 + k]) + 300
                    if beyond and ok[k] and sign * (c[k] - lvl) > 0:
                        touched = (lo[k] <= lvl + p.retest_tol_atr * a[k]) if sign > 0 else (h[k] >= lvl - p.retest_tol_atr * a[k])
                        if touched and _dir_at(e15, d15, t) == sign:
                            stop = (lo[k] - p.stop_buffer_atr * a[k]) if sign > 0 else (h[k] + p.stop_buffer_atr * a[k])
                            entry = C[a0 + k]
                            target = tgt1 if sign * (tgt1 - entry) >= p.min_t1_r * sign * (entry - stop) else tgt2
                            tr = make_trade(sym, day, "cpr", sign, a0 + k, entry, stop, target, T, p,
                                        tags(sign, k, {"opened": "beyond CPR" if sign * (Op[a0] - lvl) > 0 else "inside/other side",
                                                       "cpr_width": "narrow" if (lv["TC"] - lv["BC"]) / lv["P"] < 0.003 else "wide"}))
                            if tr:
                                exit_trade(tr, Op, H, L, C, T, a0 + k, a1, p, costs)
                                out.append(tr)
                            break
                    if sign * (c[k] - lvl) > 0:
                        beyond = True
    return out


# -- read-out -----------------------------------------------------------------------------------------


def summarize(trades: list[ConfirmTrade]) -> dict[str, Any]:
    def stats(ts: list[ConfirmTrade]) -> dict[str, Any]:
        if not ts:
            return {"n": 0}
        rs = [t.r for t in ts]
        hit = [t.exit_reason == "TARGET" for t in ts]
        return {
            "n": len(ts),
            "target_hit": round(100 * statistics.fmean(hit), 1),
            "p_random": round(100 * statistics.fmean([t.p_random for t in ts]), 1),
            "win": round(100 * sum(r > 0 for r in rs) / len(rs), 1),
            "avg_r": round(statistics.fmean(rs), 3),
            "gross_r": round(statistics.fmean([t.gross / (abs(t.entry - t.stop) * t.qty) for t in ts]), 3),
            "total_r": round(sum(rs), 1),
        }

    by_week: dict[str, float] = {}
    months: dict[str, float] = {}
    for t in trades:
        y, w, _ = date.fromisoformat(t.day).isocalendar()
        by_week[f"{y}-W{w:02d}"] = by_week.get(f"{y}-W{w:02d}", 0.0) + t.r
        months[t.day[:7]] = months.get(t.day[:7], 0.0) + t.r
    wk = list(by_week.values())
    sd = statistics.stdev(wk) if len(wk) > 1 else 0.0
    groups: dict[str, dict[str, Any]] = {}
    for key in sorted({k for t in trades for k in t.tags}):
        vals = sorted({t.tags.get(key, "") for t in trades})
        groups[key] = {v: stats([t for t in trades if t.tags.get(key, "") == v]) for v in vals}
    return {
        **stats(trades),
        "t_by_week": round(statistics.fmean(wk) / (sd / math.sqrt(len(wk))), 2) if sd > 0 else None,
        "months_positive": sum(v > 0 for v in months.values()),
        "months_negative": sum(v < 0 for v in months.values()),
        "by_side": {s: stats([t for t in trades if t.side == s]) for s in ("LONG", "SHORT")},
        "by_exit": {e: sum(t.exit_reason == e for t in trades) for e in ("TARGET", "STOP", "SESSION_END")},
        "by": groups,
    }


def run(settings: Settings, start: date, end: date, p: ConfirmParams, *, symbols: list[str] | None = None, label: str = "",
        on_symbol: Any = None, allow_holdout: bool = False) -> dict[str, Any]:
    if end >= HOLDOUT_START and not allow_holdout:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START}); the exploration window ends 2025-09-30")
    store = HistoryStore(settings.data_dir / "history")
    costs = CostModel(settings)
    names = symbols or store.symbols("5m")
    member = None
    if p.fo_members:
        from ..market.fo_universe import load as load_members

        fm, _ = load_members(settings.data_dir)
        member = fm.member
    t0 = _time.time()
    trades: list[ConfirmTrade] = []
    for n, sym in enumerate(names, 1):
        got = trades_for(sym, store.load(sym, "5m"), p, start, end, costs, member)
        trades += got
        if on_symbol:
            on_symbol(sym, got, n, len(names))
    out = {
        "id": f"confirm-{uuid.uuid4().hex[:10]}", "kind": "confirm", "label": label, "created_ts": _time.time(),
        "seconds": round(_time.time() - t0, 1), "params": asdict(p), "range": [start.isoformat(), end.isoformat()],
        "summary": summarize(trades), "trades": [asdict(t) for t in trades],
    }
    root = settings.data_dir / "obtests"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out


__all__ = ["ConfirmParams", "ConfirmTrade", "atr", "cpr", "doubles", "exit_trade", "make_trade", "run", "summarize", "supertrend", "trades_for"]

