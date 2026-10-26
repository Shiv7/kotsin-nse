"""The owner's CPR retest sequence, mechanical, on closed 5-minute bars.

The sequence, in his words and as the research sweep measured it (``research/dayread_sweep.py``,
docs/research/2026-10-26-dayread-sweep.md §9–10):

1. **Open test** — by 09:45 (bar index ≤ 6) price tests a level below the CPR and the bar closes
   under the bottom central pivot (bullish; mirrored for bearish over the top).
2. **Break of structure** — within the next hour a close above the last swing high.
3. **Through the CPR** — a close above TC by half a tolerance.
4. **Retest** — the first bar back to TC + tolerance; *held* if it closes at or above BC.
5. **Confirmation / entry** — the first close above the retest bar's high within six bars. The entry is
   that close, the stop the retest bar's low less a tolerance, the target the next wall (daily pivots,
   the previous day's high, the previous week's pivots, the next round number), then trail wall to wall.

Everything here is a pure function of bars and levels so the live runner and the research see the same
thing: ``sequence`` is the sweep's logic on the bars so far, ``ExitPlan`` is ``retest_mgmt.simulate``
one bar at a time. The research verdict on this trade is 0 to +4 bps gross on 3,101 instances (2024-01
→ 2025-09), negative after costs, and no filter known at the entry picks the trend days. It runs in
SHADOW to be looked at, not because it is expected to pay.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..analysis.dayread import BREAK_FRAC, TOL_ADR, bands, cpr, level_events, structure_events
from ..structure.engine import Bar5
from ..structure.levels import floor_pivots

NAME = "cpr_retest"
RULES: tuple[str, ...] = ("wall_then_trail", "fixed_wall", "fixed_1to2")
#: the open test must be in by this bar index (09:45 on the 5m grid)
OPEN_TEST_BARS = 6
#: the break of structure within this many bars of the open test
BOS_BARS = 12
#: the first close beyond the retest bar within this many bars
CONFIRM_BARS = 6
ROUND_STEPS: tuple[float, ...] = (1000, 500, 100, 50, 20, 10, 5, 2, 1, 0.5)
#: the round-number step is the largest of these at most this fraction of the price
ROUND_FRAC = 0.007
STAGES = ("none", "open_test", "bos", "through", "retest_failed", "retest_held", "confirmed")


@dataclass(slots=True)
class Stage:
    """Where one side of the sequence stands on the bars so far. Indices are bar positions."""

    side: int
    i_os: int | None = None
    lvl_os: str = ""
    i_bos: int | None = None
    i_brk: int | None = None
    i_rt: int | None = None
    result: str = ""  # held | failed | ""
    j: int | None = None  # the confirmation bar

    @property
    def name(self) -> str:
        if self.j is not None:
            return "confirmed"
        if self.i_rt is not None:
            return "retest_held" if self.result == "held" else "retest_failed"
        if self.i_brk is not None:
            return "through"
        if self.i_bos is not None:
            return "bos"
        if self.i_os is not None:
            return "open_test"
        return "none"

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "stage": self.name}


def tolerance(adr: float | None, price: float) -> float:
    """A touch counts within this of a level: a tenth of a normal day's range (1 % of price without one)."""
    return (adr if adr and adr > 0 else price * 0.01) * TOL_ADR


def sequence(bars: Sequence[Bar5], levels: dict[str, float], tol: float) -> dict[int, Stage]:
    """The sequence on the closed bars so far, per side (+1 bullish, −1 bearish) — the sweep's logic
    (``dayread_sweep.day_tables``) verbatim, so a live stage equals the research's for the same bars."""
    out = {1: Stage(1), -1: Stage(-1)}
    if not bars:
        return out
    tc, bc = levels["TC"], levels["BC"]
    brk = tol * BREAK_FRAC
    c = [b.c for b in bars]
    h = [b.h for b in bars]
    lo = [b.l for b in bars]
    ev = level_events(bars, bands(levels), tol)
    sev, _legs = structure_events(bars)
    idx = {b.ts: i for i, b in enumerate(bars)}
    n = len(bars)
    for side, st in out.items():
        if side > 0:
            os_ = [e for e in ev if e.kind == "SUPPORT" and idx[e.ts] <= OPEN_TEST_BARS and c[idx[e.ts]] < bc]
        else:
            os_ = [e for e in ev if e.kind == "RESISTANCE" and idx[e.ts] <= OPEN_TEST_BARS and c[idx[e.ts]] > tc]
        if not os_:
            continue
        e0 = min(os_, key=lambda e: idx[e.ts])
        st.i_os, st.lvl_os = idx[e0.ts], e0.level or ""
        bos_k = "BOS_UP" if side > 0 else "BOS_DOWN"
        bos = [idx[e.ts] for e in sev if e.kind == bos_k and st.i_os < idx[e.ts] <= st.i_os + BOS_BARS]
        if not bos:
            continue
        st.i_bos = min(bos)
        through = next((i for i in range(st.i_bos, n) if (c[i] > tc + brk if side > 0 else c[i] < bc - brk)), None)
        if through is None:
            continue
        st.i_brk = through
        back = next((i for i in range(st.i_brk + 1, n) if (lo[i] <= tc + tol if side > 0 else h[i] >= bc - tol)), None)
        if back is None:
            continue
        st.i_rt = back
        held = c[back] >= bc if side > 0 else c[back] <= tc
        st.result = "held" if held else "failed"
        if not held:
            continue
        trig = h[back] if side > 0 else lo[back]
        st.j = next((k for k in range(back + 1, min(n, back + CONFIRM_BARS + 1)) if (c[k] > trig if side > 0 else c[k] < trig)), None)
    return out


def round_step(px: float) -> float:
    for step in ROUND_STEPS:
        if step <= px * ROUND_FRAC:
            return step
    return ROUND_STEPS[-1]


def walls(side: int, entry: float, levels: dict[str, float], weekly: dict[str, float] | None, tol: float) -> list[tuple[str, float]]:
    """The walls beyond the entry by at least a tolerance, nearest first: daily R1/R2/R3 (S1/S2/S3), the
    previous day's high (low), the previous week's pivot and R1/R2 (S1/S2), the next round number."""
    cands: dict[str, float] = {}
    if side > 0:
        cands.update({k: levels[k] for k in ("R1", "R2", "R3") if k in levels})
        cands["PDH"] = levels["PDH"]
        if weekly:
            cands.update({k: weekly[k] for k in ("WP", "WR1", "WR2") if k in weekly})
        step = round_step(entry)
        cands["ROUND"] = math.floor(entry / step) * step + step
        return sorted(((k, v) for k, v in cands.items() if v - entry >= tol), key=lambda kv: kv[1])
    cands.update({k: levels[k] for k in ("S1", "S2", "S3") if k in levels})
    cands["PDL"] = levels["PDL"]
    if weekly:
        cands.update({k: weekly[k] for k in ("WP", "WS1", "WS2") if k in weekly})
    step = round_step(entry)
    cands["ROUND"] = math.ceil(entry / step) * step - step
    return sorted(((k, v) for k, v in cands.items() if entry - v >= tol), key=lambda kv: -kv[1])


def weekly_pivots(weeks: Sequence[tuple[float, float, float]]) -> dict[str, float] | None:
    """``weeks`` = (high, low, close) of complete weeks before this one, oldest first; the last one's pivots."""
    if not weeks:
        return None
    h, lo, c = weeks[-1]
    fp = floor_pivots(h, lo, c)
    return {"WP": fp["P"], "WR1": fp["R1"], "WR2": fp["R2"], "WS1": fp["S1"], "WS2": fp["S2"]}


def day_levels(prev_h: float, prev_l: float, prev_c: float) -> dict[str, float]:
    """CPR, floor pivots and the previous day's extremes, by name (``analysis.dayread.cpr``)."""
    return cpr(prev_h, prev_l, prev_c)


@dataclass(slots=True)
class ExitPlan:
    """``retest_mgmt.simulate`` one bar at a time, in 'long space' (prices × side) so a short is a long.

    The bar's low against the stop first, then its high against the target — a bar touching both is
    the stop, the pessimistic reading. ``wall_then_trail``: on reaching a wall the stop rises to that
    wall less a tolerance and the target becomes the following wall; the last wall is taken.
    """

    rule: str
    side: int
    entry: float
    stop: float  # long space
    tol: float
    walls: list[float] = field(default_factory=list)  # long space, ascending
    target: float | None = None
    wall_i: int = 0
    bars: int = 0
    hh: float = 0.0  # best excursion (long space)
    ll: float = 0.0  # worst excursion (long space)
    done: tuple[float, str] | None = None  # (exit price in real terms, how)

    @classmethod
    def open(cls, rule: str, side: int, entry: float, stop: float, wall_prices: Sequence[float], tol: float) -> ExitPlan:
        e = side * entry
        s = side * stop
        ws = [side * w for w in wall_prices]
        p = cls(rule=rule, side=side, entry=entry, stop=s, tol=tol, walls=ws, hh=e, ll=e)
        if rule == "fixed_1to2":
            p.target = e + 2 * (e - s)
        else:
            p.target = ws[0] if ws else None
        return p

    @property
    def risk(self) -> float:
        return self.side * self.entry - self.stop

    def stop_price(self) -> float:
        return self.side * self.stop

    def target_price(self) -> float | None:
        return None if self.target is None else self.side * self.target

    def pnl_bps(self, exit_px: float) -> float:
        return (exit_px - self.entry) * self.side / abs(self.entry) * 1e4

    def step(self, high: float, low: float, close: float, *, last: bool = False) -> tuple[float, str] | None:
        """One closed bar after the entry bar. Returns (exit price, how) once, when the trade ends."""
        if self.done is not None:
            return None
        sg = self.side
        hi = sg * (high if sg > 0 else low)
        lw = sg * (low if sg > 0 else high)
        self.bars += 1
        self.hh = max(self.hh, hi)
        self.ll = min(self.ll, lw)
        e = sg * self.entry
        if lw <= self.stop:
            self.done = (sg * self.stop, "stop" if self.stop < e else "trail")
            return self.done
        if self.target is not None and hi >= self.target:
            if self.rule == "wall_then_trail":
                self.wall_i += 1
                if self.wall_i < len(self.walls):
                    self.stop = max(self.stop, self.target - self.tol)
                    self.target = self.walls[self.wall_i]
                else:
                    self.done = (sg * self.target, "target")
                    return self.done
            else:
                self.done = (sg * self.target, "target")
                return self.done
        if last:
            self.done = (close, "close")
            return self.done
        return None

    def to_json(self) -> dict[str, Any]:
        sg = self.side
        return {"rule": self.rule, "stop": sg * self.stop, "target": self.target_price(), "wall_i": self.wall_i, "bars": self.bars,
                "mfe_bps": (self.hh - sg * self.entry) / abs(self.entry) * 1e4, "mae_bps": (self.ll - sg * self.entry) / abs(self.entry) * 1e4,
                "exit": self.done[0] if self.done else None, "how": self.done[1] if self.done else None,
                "pnl_bps": self.pnl_bps(self.done[0]) if self.done else None}


# -- context tags: what the research tested as "is this stock trending" (none of it helped) ---------------


def supertrend_dir(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int = 10, m: float = 3.0) -> int:
    """+1 / −1: the SuperTrend(10, 3) direction after the last bar; 0 with too few bars."""
    k = len(c)
    if k < n + 2:
        return 0
    atr = h[0] - lo[0]
    fub = flb = None
    d = 1
    for i in range(1, k):
        tr = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        atr = atr + (tr - atr) / n
        mid = (h[i] + lo[i]) / 2
        ub, lb = mid + m * atr, mid - m * atr
        fub = ub if fub is None or ub < fub or c[i - 1] > fub else fub
        flb = lb if flb is None or lb > flb or c[i - 1] < flb else flb
        d = (-1 if c[i] < flb else 1) if d > 0 else (1 if c[i] > fub else -1)
    return d


def _ema(xs: Sequence[float], span: int) -> float:
    a = 2 / (span + 1)
    e = xs[0]
    for x in xs[1:]:
        e = e + a * (x - e)
    return e


def daily_tags(side: int, closes: Sequence[float], highs: Sequence[float], lows: Sequence[float], nifty_closes: Sequence[float] | None) -> dict[str, Any]:
    """From the sessions before today (oldest first): the close against the 20- and 50-day averages, the
    9/21/50 EMA stack, the 5- and 20-day returns, 20-day strength against NIFTY, yesterday's close in its range."""
    out: dict[str, Any] = {}
    if len(closes) < 51:
        return out
    pdc = closes[-1]
    dma20 = sum(closes[-20:]) / 20
    dma50 = sum(closes[-50:]) / 50
    out["vs_dma20_pct"] = round((pdc / dma20 - 1) * 100, 2)
    out["vs_dma50_pct"] = round((pdc / dma50 - 1) * 100, 2)
    out["dma20_al"] = side * (pdc - dma20) > 0
    out["dma50_al"] = side * (pdc - dma50) > 0
    e9, e21, e50 = _ema(closes[-120:], 9), _ema(closes[-120:], 21), _ema(closes[-120:], 50)
    out["ema_stack_al"] = (e9 > e21 > e50) if side > 0 else (e9 < e21 < e50)
    ret5 = (pdc / closes[-6] - 1) * 100
    ret20 = (pdc / closes[-21] - 1) * 100
    out["ret5_pct"], out["ret20_pct"] = round(ret5, 2), round(ret20, 2)
    out["ret5_al"], out["ret20_al"] = side * ret5 > 0, side * ret20 > 0
    if nifty_closes and len(nifty_closes) >= 21:
        rs = ret20 - (nifty_closes[-1] / nifty_closes[-21] - 1) * 100
        out["rs20_pct"] = round(rs, 2)
        out["rs20_al"] = side * rs > 0
    rng = highs[-1] - lows[-1]
    rpos = (pdc - lows[-1]) / rng if rng > 0 else 0.5
    out["prev_range_pos"] = round(rpos, 2)
    out["prev_trend_al"] = (rpos >= 0.7) if side > 0 else (rpos <= 0.3)
    out["prev_ret_al"] = side * (pdc - closes[-2]) > 0
    out["daily_all"] = bool(out["dma20_al"] and out["dma50_al"] and out["ema_stack_al"] and out.get("rs20_al", False))
    return out


def intraday_tags(side: int, bars: Sequence[Bar5], j: int, adr: float | None, nifty: Sequence[Bar5] | None, peers_move_adr: Sequence[float]) -> dict[str, Any]:
    """At the confirmation bar: the move from the open in ADRs, the close in the day's range so far, the
    opening hour's range broken, NIFTY's own move, and where the move ranks across the universe at that bar."""
    out: dict[str, Any] = {}
    o0 = bars[0].o
    c = bars[j].c
    hi = max(b.h for b in bars[: j + 1])
    lo = min(b.l for b in bars[: j + 1])
    mv = (c / o0 - 1) * 100 if o0 > 0 else 0.0
    adr_pct = adr / c * 100 if adr and adr > 0 else None
    move_adr = side * mv / adr_pct if adr_pct else None
    out["move_pct"] = round(side * mv, 2)
    out["move_adr"] = round(move_adr, 2) if move_adr is not None else None
    out["move_al_03"] = bool(move_adr is not None and move_adr >= 0.3)
    pos = (c - lo) / (hi - lo) if hi > lo else 0.5
    out["range_pos"] = round(pos, 2)
    out["pos_08"] = (pos >= 0.8) if side > 0 else (pos <= 0.2)
    if j >= 12:
        ibh, ibl = max(b.h for b in bars[:12]), min(b.l for b in bars[:12])
        out["ib_al"] = (c > ibh) if side > 0 else (c < ibl)
    else:
        out["ib_al"] = False
    if nifty and len(nifty) > j and nifty[0].o > 0:
        nmv = (nifty[j].c / nifty[0].o - 1) * 100
        out["nifty_move_pct"] = round(nmv, 2)
        out["nifty_al"] = side * nmv >= 0.2
        out["rel_al"] = side * (mv - nmv) >= 0.3
    if peers_move_adr and move_adr is not None:
        below = sum(1 for x in peers_move_adr if x < move_adr)
        out["xs_pct"] = round(below / len(peers_move_adr), 2)
        out["xs_n"] = len(peers_move_adr)
        out["xs_top20"] = out["xs_pct"] >= 0.8
    return out


def passes(filter_spec: str, tags: dict[str, Any]) -> bool:
    """``none`` takes every signal; ``a+b`` needs every named tag true."""
    spec = (filter_spec or "none").strip().lower()
    if spec in ("", "none", "all"):
        return True
    return all(bool(tags.get(name.strip())) for name in spec.split("+") if name.strip())


__all__ = ["BOS_BARS", "CONFIRM_BARS", "NAME", "OPEN_TEST_BARS", "RULES", "STAGES", "ExitPlan", "Stage", "daily_tags", "day_levels",
           "intraday_tags", "passes", "round_step", "sequence", "supertrend_dir", "tolerance", "walls", "weekly_pivots"]
