"""The owner's CPR sequence (2026-10-23), on 1-minute bars: a double bottom under the CPR, the 1-minute
Supertrend turning, a rejection at P or TC, and the first push up from it. Short mirrored.

Long, step by step — each step read only from bars that have closed:

1. **Double bottom on 5m, below the CPR.** ``confirm.doubles`` on 5m bars built from the 1m (session
   grid): both lows under BC, then a 5m close above the neckline.
2. **The 1m Supertrend(10, 3) turns up** at a 1m bar after the second low began.
3. **Rejection at P or TC.** Once both have happened, for each of P and TC: a 1m close above the
   level arms it; a later 1m bar that opens the minute above it (the bar before closed above) comes
   back to it (low ≤ level + ``touch_atr`` × 1m ATR) and closes above it — the rejection; a 1m close
   below the level before the next step cancels that rejection.
4. **It starts growing up:** the first 1m close above the rejection bar's high, within ``confirm_bars``
   bars, is the entry (between ``first_entry`` and ``last_entry``).

Short: a 5m double top above TC, the 1m Supertrend turning down, a rejection at P or BC from below, the
first 1m close below the rejection bar's low.

The CPR (P, BC, TC) is from the previous session's daily bar; weekly levels from the previous week's.
Two stops are scored on the same entry, as two trades (``stop`` tag): ``next level`` — one CPR level
beyond (a P rejection → under BC, a TC rejection → under P; a short's P → over TC, BC → over P) — and
``rejection bar`` — beyond the rejection bar's extreme; each ``stop_buffer_atr`` 1m ATR further.
Target ``rr`` × the risk; else out at the session's last bar. One entry per side per session.
Recorded: the 1h structure (``confirm.structure``) and Supertrend with or against, a weekly level
inside the CPR, the level rejected, the time of day.
"""

from __future__ import annotations

import json
import time as _time
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings
from ..market.session import ist_at, ist_day
from ..risk.costs import CostModel
from .confirm import (
    HOLDOUT_START,
    WEEKLY_NAMES,
    ConfirmParams,
    ConfirmTrade,
    atr,
    cpr,
    doubles,
    exit_trade,
    make_trade,
    structure,
    summarize,
    supertrend,
)
from .history import HistoryStore

LAST_1M = 15 * 60 + 14  # a finished session's 1m history ends 15:14 on recent days: read every day the same


@dataclass(frozen=True, slots=True)
class DbCprParams:
    rr: float = 2.0
    touch_atr: float = 0.25
    confirm_bars: int = 10
    stop_buffer_atr: float = 0.1
    first_entry: str = "09:30"
    last_entry: str = "14:30"
    st_n: int = 10
    st_m: float = 3.0
    # the 5m double bottom (confirm.doubles)
    side: int = 2
    min_gap: int = 3
    tol_atr: float = 0.5
    min_depth_atr: float = 1.0
    max_wait: int = 12
    risk_inr: float = 5_000.0
    slip_bps: float = 5.0
    fo_members: bool = True

    def confirm(self) -> ConfirmParams:
        return ConfirmParams(side=self.side, min_gap=self.min_gap, tol_atr=self.tol_atr, min_depth_atr=self.min_depth_atr,
                             max_wait=self.max_wait, risk_inr=self.risk_inr, slip_bps=self.slip_bps)


def bars_from_1m(m1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1m bars → ``minutes`` bars on the session grid (from 09:15); ``end`` = when the bar closed."""
    t = pd.to_datetime(m1["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    since = (t.dt.hour * 60 + t.dt.minute - (9 * 60 + 15)).to_numpy()
    key = m1["ts"].to_numpy() - since * 60 + (since // minutes) * minutes * 60
    g = m1.assign(k=key).groupby("k", sort=True)
    out = g.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"), last=("ts", "max"), first=("ts", "min")).reset_index()
    out["end"] = out["last"] + 60
    return out.rename(columns={"k": "ts"})


def _session_1m(m1: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(m1["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    m = (t.dt.hour * 60 + t.dt.minute).to_numpy()
    return m1[(m >= 9 * 60 + 15) & (m < LAST_1M)].sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def _levels(d1: pd.DataFrame) -> tuple[dict[date, dict[str, float]], dict[date, dict[str, float] | None]]:
    """Per session: its CPR (from the session before) and the previous week's levels."""
    d1 = d1.sort_values("ts")
    days = [ist_day(int(t)) for t in d1["ts"]]
    H, L, C = d1["h"].to_numpy(float), d1["l"].to_numpy(float), d1["c"].to_numpy(float)
    daily = {days[i]: cpr(H[i - 1], L[i - 1], C[i - 1]) for i in range(1, len(days))}
    wk: dict[tuple[int, int], list[int]] = {}
    for i, d in enumerate(days):
        wk.setdefault(d.isocalendar()[:2], []).append(i)
    keys = sorted(wk)
    weekly: dict[date, dict[str, float] | None] = {}
    for n, k in enumerate(keys):
        prev = wk[keys[n - 1]] if n else None
        lv = cpr(H[prev].max(), L[prev].min(), C[prev[-1]]) if prev else None
        for i in wk[k]:
            weekly[days[i]] = lv
    return daily, weekly


def trades_for(sym: str, m1: pd.DataFrame, d1: pd.DataFrame, p: DbCprParams, start: date, end: date, costs: CostModel, member: Any = None) -> list[ConfirmTrade]:
    m1 = _session_1m(m1)
    if m1.empty or d1.empty:
        return []
    cp = p.confirm()
    daily, weekly = _levels(d1)
    T = m1["ts"].to_numpy(np.int64)
    Op, H, L, C = (m1[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    A1 = atr(H, L, C)
    ST1 = supertrend(H, L, C, p.st_n, p.st_m)
    b60 = bars_from_1m(m1, 60)
    e60 = b60["end"].to_numpy(np.int64)
    h60, l60, c60 = (b60[x].to_numpy(float) for x in ("h", "l", "c"))
    s60, st60 = structure(h60, l60, c60), supertrend(h60, l60, c60)
    b5 = bars_from_1m(m1, 5)
    T5 = b5["ts"].to_numpy(np.int64)
    H5, L5, C5 = (b5[x].to_numpy(float) for x in ("h", "l", "c"))
    A5 = atr(H5, L5, C5)
    dk1, dk5 = (T + 19_800) // 86_400, (T5 + 19_800) // 86_400
    out: list[ConfirmTrade] = []
    for dk in np.unique(dk1):
        day = ist_day(int(dk * 86_400 - 19_800 + 43_200))
        lv = daily.get(day)
        if lv is None or not (start <= day <= end) or (member is not None and not member(sym, day)):
            continue
        i0, i1 = np.searchsorted(dk1, [dk, dk + 1])
        j0, j1 = np.searchsorted(dk5, [dk, dk + 1])
        if i1 - i0 < 60 or j1 - j0 < 12:
            continue
        t_first, t_last = ist_at(day, p.first_entry), ist_at(day, p.last_entry)
        wl = weekly.get(day)
        for sign in (1, -1):
            h5, l5, c5 = (H5[j0:j1], L5[j0:j1], C5[j0:j1]) if sign > 0 else (-L5[j0:j1], -H5[j0:j1], -C5[j0:j1])
            edge = lv["BC"] if sign > 0 else lv["TC"]  # the double bottom (top) sits beyond this side of the CPR
            # both lows under BC (both highs over TC: in the mirrored prices, both under −TC)
            pats = [s for s in doubles(h5, l5, c5, A5[j0:j1], cp, np.ones(j1 - j0, bool)) if max(s["l1"], s["l2"]) < sign * edge]
            for s in pats:
                t_db = int(T5[j0 + s["k"]]) + 300  # the 5m close above the neckline
                t_l2 = int(T5[j0 + s["i2"]])
                tr = _after(sym, day, sign, lv, wl, s, t_db, t_l2, T, Op, H, L, C, A1, ST1, i0, i1, t_first, t_last,
                            (e60, s60, st60), p, costs)
                if tr:
                    out += tr
                    break  # one entry per side per session
    return out


def _after(sym: str, day: date, sign: int, lv: dict[str, float], wl: dict[str, float] | None, s: dict[str, Any], t_db: int, t_l2: int,
           T: np.ndarray, Op: np.ndarray, H: np.ndarray, L: np.ndarray, C: np.ndarray, A1: np.ndarray, ST1: np.ndarray, i0: int, i1: int,
           t_first: int, t_last: int, h1: tuple[np.ndarray, np.ndarray, np.ndarray], p: DbCprParams, costs: CostModel) -> list[ConfirmTrade] | None:
    """Steps 2–4 after a double bottom (top): the 1m Supertrend turn, a rejection at P or TC (BC), the push."""
    k_l2 = int(np.searchsorted(T, t_l2))
    flip = next((j for j in range(max(k_l2, i0 + 1), i1) if ST1[j] == sign and ST1[j - 1] == -sign), None)
    if flip is None:
        return None
    ready = max(int(np.searchsorted(T, t_db - 60)), flip + 1)  # bars that close after both happened
    levels = {"P": lv["P"], "TC": lv["TC"]} if sign > 0 else {"P": lv["P"], "BC": lv["BC"]}
    nxt = {"P": lv["BC"], "TC": lv["P"]} if sign > 0 else {"P": lv["TC"], "BC": lv["P"]}
    armed = dict.fromkeys(levels, False)  # a close beyond the level since both steps happened
    rej: dict[str, int | None] = dict.fromkeys(levels)
    for j in range(ready, i1):
        tclose = int(T[j]) + 60
        for name, lvl in levels.items():
            r = rej[name]
            if r is not None:
                if sign * (C[j] - lvl) < 0 or j - r > p.confirm_bars:
                    rej[name] = None  # back through the level, or no push in time
                elif (sign * (C[j] - (H[r] if sign > 0 else L[r])) > 0) and t_first <= tclose <= t_last:
                    return _enter(sym, day, sign, name, lvl, nxt[name], r, j, wl, lv, T, Op, H, L, C, A1, i1, h1, p, costs)
            if armed[name] and rej[name] is None:
                touch = (L[j] <= lvl + p.touch_atr * A1[j]) if sign > 0 else (H[j] >= lvl - p.touch_atr * A1[j])
                if touch and sign * (C[j] - lvl) > 0 and sign * (C[j - 1] - lvl) > 0:  # came back from beyond, held
                    rej[name] = j
            if sign * (C[j] - lvl) > 0:
                armed[name] = True
    return None


def _enter(sym: str, day: date, sign: int, name: str, lvl: float, nxt: float, r: int, j: int, wl: dict[str, float] | None, lv: dict[str, float],
           T: np.ndarray, Op: np.ndarray, H: np.ndarray, L: np.ndarray, C: np.ndarray, A1: np.ndarray, i1: int,
           h1: tuple[np.ndarray, np.ndarray, np.ndarray], p: DbCprParams, costs: CostModel) -> list[ConfirmTrade]:
    e60, s60, st60 = h1
    t = int(T[j]) + 60
    k60 = int(np.searchsorted(e60, t, side="right")) - 1
    with_ = lambda d: "with" if d == sign else "against"  # noqa: E731
    lo, hi = sorted((lv["BC"], lv["TC"]))
    pad = A1[j]
    confl = [nm for nm in WEEKLY_NAMES if wl and lo - pad <= wl[nm] <= hi + pad]
    m = (t - ist_at(day, "09:15")) // 60
    tags = {"level": name, "struct60": with_(s60[k60]) if k60 >= 0 else "none", "st60": with_(st60[k60]) if k60 >= 0 else "none",
            "weekly": "weekly level in CPR" if confl else "no weekly level",
            "tod": "09:30-10:30" if m < 75 else "10:30-13:00" if m < 225 else "13:00-15:15"}
    cp = p.confirm()
    out = []
    buf = p.stop_buffer_atr * A1[j]
    for stop_name, stop in (("next level", nxt - sign * buf), ("rejection bar", (L[r] if sign > 0 else H[r]) - sign * buf)):
        entry = C[j] * (1 + sign * p.slip_bps / 1e4)
        target = entry + p.rr * (entry - stop)
        tr = make_trade(sym, day, "dbcpr", sign, j, C[j], stop, target, T, cp, {**tags, "stop": stop_name}, bar_s=60)
        if tr:
            exit_trade(tr, Op, H, L, C, T, j, i1, cp, costs, bar_s=60)
            out.append(tr)
    return out


def run(settings: Settings, start: date, end: date, p: DbCprParams, *, symbols: list[str] | None = None, label: str = "",
        on_symbol: Any = None, allow_holdout: bool = False) -> dict[str, Any]:
    if end >= HOLDOUT_START and not allow_holdout:
        raise ValueError(f"{end} is inside the holdout (from {HOLDOUT_START}); the exploration window ends 2025-09-30")
    store = HistoryStore(settings.data_dir / "history")
    costs = CostModel(settings)
    names = symbols or store.symbols("1m")
    member = None
    if p.fo_members:
        from ..market.fo_universe import load as load_members

        member = load_members(settings.data_dir)[0].member
    t0 = _time.time()
    trades: list[ConfirmTrade] = []
    for n, sym in enumerate(names, 1):
        got = trades_for(sym, store.load(sym, "1m"), store.load(sym, "1d"), p, start, end, costs, member)
        trades += got
        if on_symbol:
            on_symbol(sym, got, n, len(names))
    by_stop = {s: summarize([t for t in trades if t.tags.get("stop") == s]) for s in ("next level", "rejection bar")}
    out = {
        "id": f"dbcpr-{uuid.uuid4().hex[:10]}", "kind": "dbcpr", "label": label, "created_ts": _time.time(),
        "seconds": round(_time.time() - t0, 1), "params": asdict(p), "range": [start.isoformat(), end.isoformat()],
        "summary": by_stop, "trades": [asdict(t) for t in trades],
    }
    root = settings.data_dir / "obtests"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out


__all__ = ["DbCprParams", "bars_from_1m", "run", "trades_for"]
