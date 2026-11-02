"""The held retest of ANY wall, with the day's move — docs/research/2026-11-03-prereg-level-retest.md.

Per stock-day on 5m bars (session 09:15–15:14, the 15:15+ auction dropped): walls = CPR band, P, R1-R3, S1-S3, PDH,
PDL, weekly WP/WR1/WR2/WS1/WS2, opening-range high/low (first 15 min), round numbers. For each wall and side:
break (close beyond the wall by brk from a state not beyond) → first pullback within RT_BARS whose extreme reaches
the wall ± tol → held if the close is at/beyond the wall − brk (CPR: inside the band or beyond) → entry at the first
close beyond the retest bar's extreme within CONFIRM_BARS, entry bar ≤ 14:30 → stop retest extreme ∓ tol →
wall_then_trail to the next walls (with round numbers). Control: entry at the break bar's close, stop = its extreme.
Usage: python level_retest.py --out DIR [--symbols A,B] [--archive-days 2026-10-27,...]
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.instrument.universe import INDEX_ROOTS
from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.structure.engine import Bar5

OPEN_S, CLOSE_S = 9 * 3600 + 15 * 60, 15 * 3600 + 15 * 60  # the auction from 15:15 is not a decision bar
EPOCH_ORD = date(1970, 1, 1).toordinal()
LATE_BAR = 63  # the 14:30 bar on the 5m grid: entries after it are not taken (live cutoff)
RT_BARS = 36  # the pullback must come within three hours of the break
COST = 15.0
OR_BARS = 3  # the opening range = the first 15 minutes


def hm(i: int) -> str:
    m = 9 * 60 + 15 + 5 * i
    return f"{m // 60:02d}:{m % 60:02d}"


def load_1d(data_dir: Path, sym: str) -> pd.DataFrame | None:
    p = data_dir / "history" / "1d" / f"{sym}.parquet"
    if not p.exists():
        return None
    d = pd.read_parquet(p).drop_duplicates("ts", keep="last").sort_values("ts")
    d = d[(d["h"] > 0) & (d["l"] > 0)]
    d["day"] = ((d["ts"] + 19800) // 86400).astype(int).map(lambda x: date.fromordinal(EPOCH_ORD + x))
    return d.drop_duplicates("day", keep="last")


def bars_by_day(df: pd.DataFrame) -> dict[date, list[Bar5]]:
    df = df.drop_duplicates("ts", keep="last").sort_values("ts")
    ts = df["ts"].to_numpy(dtype=np.int64)
    ist = ts + 19800
    dayn, secs = ist // 86400, ist % 86400
    keep = (secs >= OPEN_S) & (secs < CLOSE_S)
    df, ts, dayn = df[keep], ts[keep], dayn[keep]
    o_, h_, l_, c_, v_ = (df[k].to_numpy(dtype=float) for k in ("o", "h", "l", "c", "v"))
    out: dict[date, list[Bar5]] = {}
    uniq, starts, counts = np.unique(dayn, return_index=True, return_counts=True)
    for dn, s, cnt in zip(uniq, starts, counts, strict=True):
        bars = [Bar5(int(ts[i]), o_[i], h_[i], l_[i], c_[i], v_[i]) for i in range(s, s + cnt)]
        if any(b.h < b.l or b.c <= 0 for b in bars) or len(bars) < 30:
            continue
        out[date.fromordinal(EPOCH_ORD + int(dn))] = bars
    return out


def weekly_for(d1: pd.DataFrame, day: date):
    rows = d1[d1["day"] < day]
    if rows.empty:
        return None
    wk = rows["day"].map(lambda x: x.isocalendar()[:2])
    this = day.isocalendar()[:2]
    done = [w for w in wk.unique() if w < this]
    if not done:
        return None
    g = rows[wk == max(done)]
    return cr.weekly_pivots([(float(g["h"].max()), float(g["l"].min()), float(g["c"].iloc[-1]))])


def wall_set(levels: dict[str, float], weekly: dict[str, float] | None, orh: float, orl: float, lo_px: float, hi_px: float, tol: float):
    """(name, lo, hi, type, from_bar) — the CPR is a band; the rest are lines. Round numbers across the session's range."""
    ws = [("CPR", min(levels["BC"], levels["TC"]), max(levels["BC"], levels["TC"]), "cpr", 0)]
    ws += [(k, levels[k], levels[k], "pivot", 0) for k in ("P", "R1", "R2", "R3", "S1", "S2", "S3") if k in levels]
    ws += [(k, levels[k], levels[k], "pd", 0) for k in ("PDH", "PDL")]
    if weekly:
        ws += [(k, weekly[k], weekly[k], "weekly", 0) for k in ("WP", "WR1", "WR2", "WS1", "WS2")]
    ws += [("ORH", orh, orh, "or", OR_BARS), ("ORL", orl, orl, "or", OR_BARS)]
    step = cr.round_step((lo_px + hi_px) / 2)
    r = math.floor((lo_px - 2 * tol) / step) * step
    while r <= hi_px + 2 * tol:
        if r > 0:
            ws.append((f"R{r:g}", r, r, "round", 0))
        r += step
    # confluence: another wall (of a different name) within a tolerance
    out = []
    for name, lo, hi, typ, fb in ws:
        n_near = sum(1 for n2, lo2, hi2, _t, _f in ws if n2 != name and not (lo2 > hi + tol or hi2 < lo - tol))
        out.append((name, lo, hi, typ, fb, n_near))
    return out


def run_exit(side: int, bars: list[Bar5], j: int, entry: float, stop: float, levels, weekly, tol: float):
    risk = (entry - stop) * side
    if risk <= 0:
        return None
    walls = cr.walls(side, entry, levels, weekly, tol, use_round=True)
    if not walls:
        return None
    plan = cr.ExitPlan.open("wall_then_trail", side, entry, stop, [v for _k, v in walls], tol)
    n = len(bars)
    res = None
    for q in range(j + 1, n):
        res = plan.step(bars[q].h, bars[q].l, bars[q].c, last=(q == n - 1), open=bars[q].o)
        if res is not None:
            break
    if res is None:
        res = (bars[-1].c, "close")
    return {"pnl": plan.pnl_bps(res[0]), "how": res[1], "bars_held": plan.bars, "risk_bps": risk / entry * 1e4,
            "w1": walls[0][0], "w1_bps": abs(walls[0][1] - entry) / entry * 1e4, "mfe": (plan.hh - side * entry) / entry * 1e4}


def day_rows(sym: str, day: date, bars: list[Bar5], d1: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    prev = d1[d1["day"] < day]
    if len(prev) < 21:
        return [], []
    p = prev.iloc[-1]
    if not (p["h"] > p["l"] > 0):
        return [], []
    levels = cr.day_levels(float(p["h"]), float(p["l"]), float(p["c"]))
    win = prev.tail(60)
    adr = guarded_adr(win["h"].tolist(), win["l"].tolist(), win["c"].tolist())
    tol = cr.tolerance(adr, float(p["c"]))
    brk = tol * cr.BREAK_FRAC
    weekly = weekly_for(d1, day)
    o = np.array([b.o for b in bars]); h = np.array([b.h for b in bars]); lo = np.array([b.l for b in bars]); c = np.array([b.c for b in bars])
    n = len(bars)
    day_open = float(o[0])
    gap_pct = (day_open / float(p["c"]) - 1) * 100
    orh, orl = float(h[:OR_BARS].max()), float(lo[:OR_BARS].min())
    walls = wall_set(levels, weekly, orh, orl, float(lo.min()), float(h.max()), tol)
    trades: list[dict] = []
    ctrl: list[dict] = []
    for name, wlo, whi, typ, from_bar, n_near in walls:
        for side in (1, -1):
            # long space: price above the wall after a break up; mirrored for shorts
            edge = whi if side > 0 else wlo          # the side of the band price must clear
            far = wlo if side > 0 else whi           # the far edge (CPR: the hold must close at/beyond it)
            beyond = lambda x: (x > edge + brk) if side > 0 else (x < edge - brk)
            inside_or_behind = lambda x: (x < far - brk) if side > 0 else (x > far + brk)
            state = 1 if beyond(c[from_bar]) else -1  # the state at the first bar the wall exists: no break on that bar
            i = from_bar + 1
            while i < n:
                if state != 1 and beyond(c[i]):
                    i_brk = i
                    state = 1
                    with_move = (c[i] > day_open) if side > 0 else (c[i] < day_open)
                    # control: enter at the break bar's close
                    stop_c = (lo[i] - tol) if side > 0 else (h[i] + tol)
                    if i <= LATE_BAR:
                        t = run_exit(side, bars, i, float(c[i]), float(stop_c), levels, weekly, tol)
                        if t:
                            ctrl.append({"sym": sym, "day": day.isoformat(), "side": side, "wall": name, "wtype": typ, "conf": n_near, "i": i,
                                         "with_move": bool(with_move), "gap_pct": gap_pct, **t})
                    # the pullback: the first bar whose extreme reaches the wall ± tol
                    k = None
                    for q in range(i + 1, min(n, i + RT_BARS + 1)):
                        touch = (lo[q] <= edge + tol) if side > 0 else (h[q] >= edge - tol)
                        if touch:
                            k = q
                            break
                        if inside_or_behind(c[q]):  # fell back through the wall before any retest: the break failed
                            break
                    if k is not None:
                        held = (c[k] >= far - brk) if side > 0 else (c[k] <= far + brk)
                        j = None
                        if held:
                            trig = h[k] if side > 0 else lo[k]
                            j = next((q for q in range(k + 1, min(n, k + cr.CONFIRM_BARS + 1)) if ((c[q] > trig) if side > 0 else (c[q] < trig))), None)
                        row = {"sym": sym, "day": day.isoformat(), "side": side, "wall": name, "wtype": typ, "conf": n_near, "i_brk": i, "k": k,
                               "held": bool(held), "j": -1 if j is None else j, "with_move": bool(with_move), "gap_pct": gap_pct, "tol_bps": tol / c[i] * 1e4}
                        if j is not None and j <= LATE_BAR:
                            stop = (lo[k] - tol) if side > 0 else (h[k] + tol)
                            t = run_exit(side, bars, j, float(c[j]), float(stop), levels, weekly, tol)
                            if t:
                                row.update({"entry": float(c[j]), "j_hm": hm(j), **t})
                        trades.append(row)
                        i = k + 1
                        # after a retest (held or not) the wall re-arms only once price is back behind it
                        state = 1
                        continue
                    i += 1
                    continue
                if state == 1 and inside_or_behind(c[i]):
                    state = -1
                i += 1
    return trades, ctrl


def archive_5m(data_dir: Path, day: date) -> dict[str, pd.DataFrame]:
    a = pd.read_parquet(data_dir / "archive" / "bars" / f"{day.isoformat()}.parquet")
    a = a[a["source"] != "partial"].copy()
    a["b"] = (a["ts"] // 300) * 300
    g = a.sort_values("ts").groupby(["symbol", "b"]).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
    g = g.rename(columns={"b": "ts"})
    return {sym: d.drop(columns=["symbol"]) for sym, d in g.groupby("symbol")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2025-09-30")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--archive-days", default="")
    a = ap.parse_args()
    data_dir, out = Path(a.data_dir), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    t0 = time.time()
    if a.archive_days:
        rows, ctrls = [], []
        for ds in a.archive_days.split(","):
            day = date.fromisoformat(ds)
            per = archive_5m(data_dir, day)
            for sym, df in per.items():
                if sym in INDEX_ROOTS:
                    continue
                d1 = load_1d(data_dir, sym)
                if d1 is None:
                    continue
                bd = bars_by_day(df)
                if day not in bd:
                    continue
                tr, ct = day_rows(sym, day, bd[day], d1)
                rows += tr; ctrls += ct
            print(f"{ds}: {len(per)} symbols, trades so far {len(rows)}", file=sys.stderr)
        pd.DataFrame(rows).to_parquet(out / "archive_trades.parquet"); pd.DataFrame(ctrls).to_parquet(out / "archive_control.parquet")
        return
    syms = a.symbols.split(",") if a.symbols else sorted(p.stem for p in (data_dir / "history" / "5m").glob("*.parquet"))
    syms = [s for s in syms if s not in INDEX_ROOTS]
    rows, ctrls = [], []
    for si, sym in enumerate(syms):
        d1 = load_1d(data_dir, sym)
        if d1 is None:
            continue
        df = pd.read_parquet(data_dir / "history" / "5m" / f"{sym}.parquet")
        for day, bars in bars_by_day(df).items():
            if not (start <= day <= end):
                continue
            tr, ct = day_rows(sym, day, bars, d1)
            rows += tr; ctrls += ct
        if si % 20 == 0:
            print(f"{si}/{len(syms)} {sym} trades {len(rows)} ctrl {len(ctrls)} {time.time() - t0:.0f}s", file=sys.stderr)
    pd.DataFrame(rows).to_parquet(out / "trades.parquet")
    pd.DataFrame(ctrls).to_parquet(out / "control.parquet")
    print(f"done {len(rows)} retest rows, {len(ctrls)} control rows in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
