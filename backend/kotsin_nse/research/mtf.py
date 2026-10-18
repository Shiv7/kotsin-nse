"""Multi-timeframe sweeps: liquidity taken on the 1h / 4h chart, the entry timed on 15m / 5m.

Built from the 5-minute cache (2.5 years of it), so every timeframe comes from one source, on the
session grid — 1h bars start 09:15, 10:15 … 15:15; 4h bars start 09:15 and 13:15 (TradingView's NSE
convention). Everything reads CLOSED bars only.

**Higher timeframe (HTF).** Liquidity = HTF swing highs/lows (``swing_side`` lower highs each side)
no HTF bar has closed beyond yet, equal highs/lows among them, the previous session's high/low and
the previous week's high/low. A **sweep** = an HTF bar that trades through such a level by at least
``sweep_min_adr`` of a normal day and CLOSES back on the near side. It sets the bias for
``valid_htf_bars`` HTF bars.

**Lower timeframe (LTF), in sync.** After a bullish sweep, the first LTF bar that CLOSES above the
latest LTF swing high formed since the sweep bar opened (the LTF's own break of structure) is the
entry, at that close plus slippage. Bearish mirrored.

**The plan.** Stop beyond the sweep wick (a ``stop_buffer_adr`` buffer), sized ``min_stop_adr`` ..
``max_stop_adr`` of a normal day. T1 = the nearest opposite HTF liquidity at ≥ ``min_t1_r``; T2 =
the next; R:R to T2 ≥ ``min_rr`` (no T2 → ``fallback_t2_r`` R). Optional daily filter: longs only
above the 20-day average, shorts only below.

**Management** on 5-minute bars: a resting stop; half at T1 and the stop to breakeven; the rest at
T2; out at the close of the ``max_sessions``-th session. Held overnight as a stock future. Each
stock's trades are simulated on their own, then the portfolio takes them in time order while fewer
than ``max_open`` are open.
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
from .obtest import _fut

TF_MIN = {"5m": 5, "15m": 15, "60m": 60, "240m": 240}


@dataclass(frozen=True, slots=True)
class MTFParams:
    htf: str = "60m"
    ltf: str = "15m"
    swing_side: int = 2
    ltf_swing_side: int = 2
    sweep_min_adr: float = 0.03
    valid_htf_bars: int = 3
    levels: str = "SWING,EQUAL,PD,PW"
    stop_buffer_adr: float = 0.05
    min_stop_adr: float = 0.25
    max_stop_adr: float = 1.5
    min_t1_r: float = 1.0
    min_rr: float = 2.0
    fallback_t2_r: float = 3.0
    daily_filter: bool = False
    max_sessions: int = 5
    max_open: int = 5
    risk_inr: float = 5_000.0
    slip_bps: float = 5.0
    first_entry: str = "09:30"
    last_entry: str = "15:00"
    breakeven_after_t1: bool = True
    #: only trades whose previous-session OI build-up points their way (WITH) or the other (AGAINST)
    oi_filter: str = ""  # "" | WITH | AGAINST


@dataclass(slots=True)
class MTFTrade:
    symbol: str
    side: str
    setup: str
    sweep_ts: int  # when the HTF sweep bar closed
    level: float
    wick: float
    signal_ts: int  # when the LTF trigger bar closed (= entry time)
    entry: float
    stop: float
    t1: float
    t2: float
    rr: float
    qty: int
    day: str = ""
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
    #: the previous session's futures OI build-up (NSE bhavcopy), and whether it points the trade's way
    oi_state: str = ""
    oi_chg_pct: float | None = None
    oi_align: str = ""  # WITH | AGAINST | ""


# -- bars -------------------------------------------------------------------------------------------


def clean_5m(df: pd.DataFrame) -> pd.DataFrame:
    """Session bars only (09:15 ≤ start < 15:30 IST), without the broker's end-of-day artefacts: a
    near-empty print at or after 15:15, and the lumped bars of a provisional latest session (volume
    above 20× the day's median at or after 15:10)."""
    if df.empty:
        return df
    t = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    mins = t.dt.hour * 60 + t.dt.minute
    day = t.dt.date
    df = df.assign(_m=mins.values, _d=day.values)
    df = df[(df["_m"] >= 9 * 60 + 15) & (df["_m"] < 15 * 60 + 30)]
    med = df.groupby("_d")["v"].transform("median")
    late = df["_m"] >= 15 * 60 + 10
    junk = late & ((df["v"] < 0.01 * med) | (df["v"] > 20 * med))
    return df[~junk].drop(columns=["_m", "_d"]).sort_values("ts").reset_index(drop=True)


def resample(df5: pd.DataFrame, tf: str) -> pd.DataFrame:
    """5m bars → ``tf`` bars on the session grid; ``end`` = when the bar closed (its last 5m + 300)."""
    m = TF_MIN[tf]
    t = pd.to_datetime(df5["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    since_open = (t.dt.hour * 60 + t.dt.minute - (9 * 60 + 15)).values
    day_start = df5["ts"].values - since_open * 60 - (t.dt.second.values)
    key = day_start + (since_open // m) * m * 60
    g = df5.assign(k=key).groupby("k", sort=True)
    out = g.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"), last5=("ts", "max")).reset_index()
    out["end"] = out["last5"] + 300
    return out.rename(columns={"k": "ts"}).drop(columns=["last5"])


def swings(h: np.ndarray, lo: np.ndarray, side: int) -> tuple[list[int], list[int]]:
    highs, lows = [], []
    for i in range(side, len(h) - side):
        win_h = np.r_[h[i - side : i], h[i + 1 : i + side + 1]]
        win_l = np.r_[lo[i - side : i], lo[i + 1 : i + side + 1]]
        if (win_h < h[i]).all():
            highs.append(i)
        if (win_l > lo[i]).all():
            lows.append(i)
    return highs, lows


# -- the strategy -------------------------------------------------------------------------------------


def _daily_levels(d1: pd.DataFrame) -> pd.DataFrame:
    """Per session: previous day high/low, previous week high/low, 20-day average close and the
    50-session average daily range — all from sessions BEFORE it."""
    d = d1.sort_values("ts").copy()
    d["day"] = [ist_day(int(x)) for x in d["ts"]]
    d["pdh"], d["pdl"] = d["h"].shift(1), d["l"].shift(1)
    d["sma20"] = d["c"].shift(1).rolling(20).mean()
    d["adr"] = (d["h"] - d["l"]).shift(1).rolling(50, min_periods=25).mean()
    wk = pd.Series([x.isocalendar()[:2] for x in d["day"]], index=d.index)
    weekly = d.groupby(wk.values).agg(wh=("h", "max"), wl=("l", "min"))
    keys = list(weekly.index)
    prev = {keys[i]: weekly.iloc[i - 1] for i in range(1, len(keys))}
    d["pwh"] = [prev[k]["wh"] if k in prev else np.nan for k in wk.values]
    d["pwl"] = [prev[k]["wl"] if k in prev else np.nan for k in wk.values]
    # a stock that split inside the ADR window has no trustworthy range: no trades until it rolls out
    jump = (d["c"] / d["c"].shift(1) - 1).abs() > 0.35
    d.loc[jump.rolling(51, min_periods=1).max().astype(bool), "adr"] = np.nan
    return d.set_index("day")


def oi_alignment(side: str, state: str) -> str:
    if not state:
        return ""
    up = state in ("LONG BUILD-UP", "SHORT COVERING")
    return "WITH" if (side == "BULLISH") == up else "AGAINST"


def trades_for(
    symbol: str, df5: pd.DataFrame, d1: pd.DataFrame, p: MTFParams, start: date, end: date, costs: CostModel,
    oi: pd.DataFrame | None = None,
) -> list[MTFTrade]:
    df5 = clean_5m(df5)
    if df5.empty or d1.empty:
        return []
    daily = _daily_levels(d1)
    htf = resample(df5, p.htf)
    ltf = df5 if p.ltf == "5m" else resample(df5, p.ltf)
    H = htf[["ts", "o", "h", "l", "c", "end"]].to_numpy(dtype=float)
    L = ltf[["ts", "o", "h", "l", "c", "end"]].to_numpy(dtype=float) if p.ltf != "5m" else np.c_[df5[["ts", "o", "h", "l", "c"]].to_numpy(dtype=float), df5["ts"].to_numpy(dtype=float) + 300]
    F = df5[["ts", "o", "h", "l", "c"]].to_numpy(dtype=float)
    sh, sl = swings(H[:, 2], H[:, 3], p.swing_side)
    lsh, lsl = swings(L[:, 2], L[:, 3], p.ltf_swing_side)
    lsh_a, lsl_a = np.array(lsh, dtype=int), np.array(lsl, dtype=int)
    kinds = set(p.levels.split(","))
    lo_ts, hi_ts = ist_at(start, "00:00"), ist_at(end, "23:59")
    out: list[MTFTrade] = []
    busy_until = 0.0
    taken: set[tuple[str, float]] = set()

    for k in range(len(H)):
        ts0, _o, h, lo, c, close_ts = H[k]
        if close_ts < lo_ts or ts0 > hi_ts or close_ts <= busy_until:
            continue
        day = ist_day(int(ts0))
        if day not in daily.index:
            continue
        dl = daily.loc[day]
        adr = dl["adr"]
        if not adr or np.isnan(adr):
            continue
        # liquidity resting before this bar: swings confirmed by bars closed before it, not closed beyond since
        lows: list[tuple[str, float]] = []
        highs: list[tuple[str, float]] = []
        if "SWING" in kinds or "EQUAL" in kinds:
            sw_l = [i for i in sl if i + p.swing_side < k and H[i, 3] < H[i + 1 : k, 4].min(initial=np.inf)]
            sw_h = [i for i in sh if i + p.swing_side < k and H[i, 2] > H[i + 1 : k, 4].max(initial=-np.inf)]
            tol = 0.05 * adr
            for i in sw_l[-6:]:
                eq = any(abs(H[i, 3] - H[j, 3]) <= tol for j in sw_l if j != i)
                if ("EQUAL" in kinds and eq) or "SWING" in kinds:
                    lows.append(("EQL" if eq else "SWL", H[i, 3]))
            for i in sw_h[-6:]:
                eq = any(abs(H[i, 2] - H[j, 2]) <= tol for j in sw_h if j != i)
                if ("EQUAL" in kinds and eq) or "SWING" in kinds:
                    highs.append(("EQH" if eq else "SWH", H[i, 2]))
        if "PD" in kinds and not np.isnan(dl["pdl"]):
            lows.append(("PDL", float(dl["pdl"])))
            highs.append(("PDH", float(dl["pdh"])))
        if "PW" in kinds and not np.isnan(dl["pwl"]):
            lows.append(("PWL", float(dl["pwl"])))
            highs.append(("PWH", float(dl["pwh"])))
        depth = p.sweep_min_adr * adr
        sweeps = [("BULLISH", n, lv, lo) for n, lv in lows if lo <= lv - depth and c > lv]
        sweeps += [("BEARISH", n, lv, h) for n, lv in highs if h >= lv + depth and c < lv]
        if not sweeps:
            continue
        side, name, level, wick = max(sweeps, key=lambda s: abs(s[2] - (s[3])))  # the deepest-taken level
        if (side, round(level, 2)) in taken:
            continue
        if p.daily_filter and not np.isnan(dl["sma20"]):
            if (side == "BULLISH" and c < dl["sma20"]) or (side == "BEARISH" and c > dl["sma20"]):
                continue
        sign = 1 if side == "BULLISH" else -1
        # -- the LTF trigger -----------------------------------------------------------------------
        valid_until = H[min(k + p.valid_htf_bars, len(H) - 1), 5]
        j0 = int(np.searchsorted(L[:, 5], close_ts))  # first LTF bar closing after the sweep closed
        trig = None
        for j in range(j0, len(L)):
            if L[j, 5] > valid_until:
                break
            day_j = ist_day(int(L[j, 0]))
            first_ok = L[j, 5] >= ist_at(day_j, p.first_entry) and L[j, 5] <= ist_at(day_j, p.last_entry)
            if not first_ok:
                continue
            if sign > 0 and (L[j, 3] < wick):
                break  # the sweep low was taken out again: the idea is dead
            if sign < 0 and L[j, 2] > wick:
                break
            pool = lsh_a if sign > 0 else lsl_a
            conf = pool[(pool + p.ltf_swing_side < j) & (L[pool, 0] >= ts0)] if len(pool) else pool
            if not len(conf):
                continue
            ref = L[conf[-1], 2] if sign > 0 else L[conf[-1], 3]
            if (sign > 0 and L[j, 4] > ref) or (sign < 0 and L[j, 4] < ref):
                trig = j
                break
        if trig is None:
            continue
        entry = L[trig, 4] * (1 + sign * p.slip_bps / 1e4)
        stop = wick - sign * p.stop_buffer_adr * adr
        risk = abs(entry - stop)
        if risk < p.min_stop_adr * adr:
            stop = entry - sign * p.min_stop_adr * adr
            risk = abs(entry - stop)
        if risk > p.max_stop_adr * adr:
            continue
        ahead = sorted({lv for _n, lv in (highs if sign > 0 else lows) if (lv - entry) * sign > 0}, key=lambda x: (x - entry) * sign)
        t1 = next((x for x in ahead if abs(x - entry) >= p.min_t1_r * risk), None)
        if t1 is None:
            t1 = entry + sign * p.min_t1_r * risk
        t2 = next((x for x in ahead if (x - t1) * sign > 0), None) or entry + sign * p.fallback_t2_r * risk
        rr = abs(t2 - entry) / risk
        if rr < p.min_rr:
            continue
        qty = int(p.risk_inr / risk)
        if qty <= 0:
            continue
        oi_state, oi_chg = "", None
        if oi is not None and len(oi):
            prev = oi[oi["day"] < ist_day(int(L[trig, 5])).isoformat()]
            if len(prev):
                oi_state, oi_chg = str(prev["label"].iloc[-1]), float(prev["oi_chg_pct"].iloc[-1])
        align = oi_alignment(side, oi_state)
        if p.oi_filter and align != p.oi_filter:
            continue
        t = MTFTrade(
            symbol, side, f"{p.htf} SWEEP {name} → {p.ltf} BOS", int(close_ts), round(level, 4), round(wick, 4),
            int(L[trig, 5]), round(entry, 4), round(stop, 4), round(t1, 4), round(t2, 4), round(rr, 2), qty,
            day=ist_day(int(L[trig, 5])).isoformat(),
            oi_state=oi_state, oi_chg_pct=round(oi_chg, 2) if oi_chg is not None else None, oi_align=align,
        )
        _simulate(t, F, p, costs)
        out.append(t)
        taken.add((side, round(level, 2)))
        busy_until = t.exit_ts
    return out


def _simulate(t: MTFTrade, F: np.ndarray, p: MTFParams, costs: CostModel) -> None:
    sign = 1 if t.side == "BULLISH" else -1
    risk = abs(t.entry - t.stop)
    stop = t.stop
    left = 1.0
    i = int(np.searchsorted(F[:, 0], t.signal_ts))
    sessions: list[date] = []
    last = None
    while i < len(F):
        ts, o, h, lo, _c = F[i]
        d = ist_day(int(ts))
        if not sessions or sessions[-1] != d:
            if len(sessions) == p.max_sessions:
                break
            sessions.append(d)
        last = F[i]
        t.mfe_r = max(t.mfe_r, ((h - t.entry) if sign > 0 else (t.entry - lo)) / risk)
        t.mae_r = max(t.mae_r, ((t.entry - lo) if sign > 0 else (h - t.entry)) / risk)
        if (sign > 0 and lo <= stop) or (sign < 0 and h >= stop):
            px = (min(o, stop) if sign > 0 else max(o, stop)) * (1 - sign * p.slip_bps / 1e4)
            t.exits.append({"ts": int(ts) + 300, "price": round(float(px), 4), "frac": left, "why": "STOP" if left == 1.0 else "BE_STOP"})
            left = 0.0
            break
        if left == 1.0 and ((sign > 0 and h >= t.t1) or (sign < 0 and lo <= t.t1)):
            t.exits.append({"ts": int(ts) + 300, "price": t.t1, "frac": 0.5, "why": "T1"})
            left = 0.5
            if p.breakeven_after_t1:
                stop = t.entry
        if (sign > 0 and h >= t.t2) or (sign < 0 and lo <= t.t2):
            t.exits.append({"ts": int(ts) + 300, "price": t.t2, "frac": left, "why": "T2"})
            left = 0.0
            break
        i += 1
    if left > 0 and last is not None:
        px = float(last[4]) * (1 - sign * p.slip_bps / 1e4)
        t.exits.append({"ts": int(last[0]) + 300, "price": round(px, 4), "frac": left, "why": "TIME"})
    t.sessions_held = len(sessions)
    t.exit_ts = t.exits[-1]["ts"] if t.exits else t.signal_ts
    t.gross = sum(sign * (e["price"] - t.entry) * t.qty * e["frac"] for e in t.exits)
    avg_exit = sum(e["price"] * e["frac"] for e in t.exits)
    t.costs = costs.round_trip(_fut(t.symbol), t.entry, avg_exit, t.qty).total
    t.net = t.gross - t.costs
    t.r = round(t.net / (risk * t.qty), 3)
    t.exit_reason = t.exits[-1]["why"] if t.exits else ""


def portfolio(trades: list[MTFTrade], max_open: int) -> list[MTFTrade]:
    """Take trades in entry order while fewer than ``max_open`` are open."""
    taken: list[MTFTrade] = []
    for t in sorted(trades, key=lambda x: x.signal_ts):
        if sum(1 for x in taken if x.signal_ts <= t.signal_ts < x.exit_ts) < max_open:
            taken.append(t)
    return taken


def summarize(trades: list[MTFTrade]) -> dict[str, Any]:
    rs = [t.r for t in trades]
    months: dict[str, float] = {}
    for t in trades:
        months[t.day[:7]] = months.get(t.day[:7], 0.0) + t.r
    by_week: dict[str, float] = {}
    for t in trades:
        y, w, _ = date.fromisoformat(t.day).isocalendar()
        by_week[f"{y}-W{w:02d}"] = by_week.get(f"{y}-W{w:02d}", 0.0) + t.r
    wk = list(by_week.values())
    sd = statistics.stdev(wk) if len(wk) > 1 else 0.0

    def group(key: Any) -> dict[str, Any]:
        g: dict[str, list[float]] = {}
        for t in trades:
            g.setdefault(key(t), []).append(t.r)
        return {k: {"n": len(v), "avg_r": round(statistics.fmean(v), 3), "total_r": round(sum(v), 2)} for k, v in sorted(g.items())}

    return {
        "trades": len(trades),
        "win_rate": round(100 * sum(r > 0 for r in rs) / len(rs), 1) if rs else None,
        "avg_r": round(statistics.fmean(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2),
        "net_inr": round(sum(t.net for t in trades)),
        "costs_inr": round(sum(t.costs for t in trades)),
        "costs_r_per_trade": round(statistics.fmean([t.costs / (abs(t.entry - t.stop) * t.qty) for t in trades]), 3) if trades else None,
        "t_by_week": round(statistics.fmean(wk) / (sd / math.sqrt(len(wk))), 2) if sd > 0 else None,
        "weeks": len(wk),
        "months_positive": sum(v > 0 for v in months.values()),
        "months_negative": sum(v < 0 for v in months.values()),
        "avg_sessions_held": round(statistics.fmean([t.sessions_held for t in trades]), 2) if trades else None,
        "by_month": {k: round(v, 2) for k, v in sorted(months.items())},
        "by_exit": group(lambda t: t.exit_reason),
        "by_setup": group(lambda t: t.setup.split(" → ")[0]),
        "by_side": group(lambda t: t.side),
        "by_oi_state": group(lambda t: t.oi_state or "no OI data"),
        "by_oi_align": group(lambda t: t.oi_align or "no OI data"),
    }


def run(settings: Settings, start: date, end: date, p: MTFParams, *, symbols: list[str] | None = None, label: str = "", on_symbol: Any = None) -> dict[str, Any]:
    store = HistoryStore(settings.data_dir / "history")
    costs = CostModel(settings)
    names = symbols or store.symbols("5m")
    from ..market.fo_bhavcopy import OiDailyStore
    from .oi_daily import futures_table

    oi_store = OiDailyStore(settings.data_dir / "oi_daily")
    oi_all = futures_table(oi_store, settings.data_dir / "oi_daily" / "_futures.parquet") if oi_store.days() else None
    t0 = _time.time()
    all_trades: list[MTFTrade] = []
    for n, sym in enumerate(names, 1):
        oi = oi_all[oi_all["symbol"] == sym] if oi_all is not None else None
        got = trades_for(sym, store.load(sym, "5m"), store.load(sym, "1d"), p, start, end, costs, oi)
        all_trades += got
        if on_symbol:
            on_symbol(sym, got, n, len(names))
    trades = portfolio(all_trades, p.max_open)
    out = {
        "id": f"mtf-{uuid.uuid4().hex[:10]}", "kind": "mtf", "label": label, "created_ts": _time.time(),
        "seconds": round(_time.time() - t0, 1), "params": asdict(p), "range": [start.isoformat(), end.isoformat()],
        "candidates": len(all_trades),
        "summary": summarize(trades),
        "trades": [asdict(t) for t in trades],
    }
    root = settings.data_dir / "obtests"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{out['id']}.json").write_text(json.dumps(out, default=float))
    return out
