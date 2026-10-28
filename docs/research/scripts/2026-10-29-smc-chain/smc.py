"""The intraday SMC chain on 5m: the engine's own order blocks (structure.analyze), entered on the first return to Z,
with the sweep-before, 1H-bias, discount, MTF-pivot-confluence and FVG gates recorded per trade. See the pre-registration."""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.instrument.universe import INDEX_ROOTS
from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.structure.engine import SWING_SIDE, Bar5, _swings, analyze

DATA = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
OUT = Path(sys.argv[1])
START, END = date(2024, 1, 1), date(2025, 9, 30)
EPOCH_ORD = date(1970, 1, 1).toordinal()
OPEN_S, CLOSE_S = 33300, 55800
LATE_BAR = 63
H1_BARS = 60


def load_5m(sym: str):
    df = pd.read_parquet(DATA / "history" / "5m" / f"{sym}.parquet").drop_duplicates("ts", keep="last").sort_values("ts")
    ts = df["ts"].to_numpy(dtype=np.int64)
    ist = ts + 19800
    dayn, secs = ist // 86400, ist % 86400
    keep = (secs >= OPEN_S) & (secs < CLOSE_S)
    df, ts, dayn, secs = df[keep], ts[keep], dayn[keep], secs[keep]
    o_, h_, l_, c_, v_ = (df[k].to_numpy(dtype=float) for k in ("o", "h", "l", "c", "v"))
    days = {}
    uniq, starts, counts = np.unique(dayn, return_index=True, return_counts=True)
    for dn, s, cnt in zip(uniq, starts, counts, strict=True):
        bars = [Bar5(int(ts[i]), o_[i], h_[i], l_[i], c_[i], v_[i]) for i in range(s, s + cnt)]
        if cnt >= 70 and not any(b.h < b.l or b.c <= 0 for b in bars):
            days[date.fromordinal(EPOCH_ORD + int(dn))] = bars
    # 1H bars on the session grid (09:15, 10:15, …), every day of the cache
    hour = (dayn * 10 + (secs - OPEN_S) // 3600).astype(np.int64)
    h1 = []
    uniq, starts, counts = np.unique(hour, return_index=True, return_counts=True)
    for hk, s, cnt in zip(uniq, starts, counts, strict=True):
        sl = slice(s, s + cnt)
        h1.append((int(ts[s]), int(ts[s + cnt - 1]) + 300, o_[s], h_[sl].max(), l_[sl].min(), c_[s + cnt - 1]))
    return days, h1


def load_1d(sym: str) -> pd.DataFrame | None:
    p = DATA / "history" / "1d" / f"{sym}.parquet"
    if not p.exists():
        return None
    d = pd.read_parquet(p).drop_duplicates("ts", keep="last").sort_values("ts")
    d = d[(d["h"] > 0) & (d["l"] > 0)].copy()
    d["day"] = ((d["ts"] + 19800) // 86400).astype(int).map(lambda x: date.fromordinal(EPOCH_ORD + x))
    return d.drop_duplicates("day", keep="last").reset_index(drop=True)


def h1_context(h1: list, h1_end: np.ndarray, entry_ts: int) -> tuple[int, float, float]:
    """(bias, range low, range high) from the 1H bars completed before ``entry_ts``: the last 1H break of structure's
    direction (+1 / −1 / 0) and the dealing range between the last confirmed 1H swing low and high."""
    k = int(np.searchsorted(h1_end, entry_ts, side="right"))
    win = h1[max(0, k - H1_BARS):k]
    if len(win) < 10:
        return 0, float("nan"), float("nan")
    bars = [Bar5(w[0], w[2], w[3], w[4], w[5], 0.0) for w in win]
    sw = _swings(bars, SWING_SIDE)
    bias, last_bos = 0, -1
    for s in sw:
        for i in range(s.i + SWING_SIDE + 1, len(bars)):
            if (s.kind == "HIGH" and bars[i].c > s.price) or (s.kind == "LOW" and bars[i].c < s.price):
                if i > last_bos:
                    last_bos, bias = i, (1 if s.kind == "HIGH" else -1)
                break
    lows = [s.price for s in sw if s.kind == "LOW"]
    highs = [s.price for s in sw if s.kind == "HIGH"]
    return bias, (lows[-1] if lows else float("nan")), (highs[-1] if highs else float("nan"))


def weekly_for(d1: pd.DataFrame, day: date):
    rows = d1[d1["day"] < day]
    if rows.empty:
        return None
    wk = rows["day"].map(lambda x: x.isocalendar()[:2])
    done = [w for w in wk.unique() if w < day.isocalendar()[:2]]
    if not done:
        return None
    g = rows[wk == max(done)]
    return cr.weekly_pivots([(float(g["h"].max()), float(g["l"].min()), float(g["c"].iloc[-1]))])


def run_trade(side: int, bars, k: int, entry: float, stop: float, levels, weekly, tol: float) -> dict | None:
    risk = (entry - stop) * side
    if risk <= 0:
        return None
    ws = cr.walls(side, entry, levels, weekly, tol)
    if not ws:
        return None
    out = {"risk_bps": risk / entry * 1e4, "w1_bps": abs(ws[0][1] - entry) / entry * 1e4, "room_R": abs(ws[0][1] - entry) / risk}
    for rule in ("wall_then_trail", "fixed_1to2"):
        plan = cr.ExitPlan.open(rule, side, entry, stop, [v for _k, v in ws], tol)
        res = None
        # the fill bar itself: its remaining range can hit the stop (pessimistic: the whole bar is checked)
        for q in range(k, len(bars)):
            res = plan.step(bars[q].h, bars[q].l, bars[q].c, last=(q == len(bars) - 1))
            if res is not None:
                break
        if res is None:
            res = (bars[-1].c, "close")
        out[rule] = plan.pnl_bps(res[0])
        out[rule + "_how"] = res[1]
    return out


def main() -> None:
    syms = sorted(p.stem for p in (DATA / "history" / "5m").glob("*.parquet") if p.stem not in INDEX_ROOTS and p.stem != "CRUDEOIL")
    rows = []
    t0 = time.time()
    for si, sym in enumerate(syms):
        d1 = load_1d(sym)
        if d1 is None:
            continue
        days, h1 = load_5m(sym)
        h1_end = np.array([w[1] for w in h1], dtype=np.int64)
        for day, bars in sorted(days.items()):
            if not (START <= day <= END):
                continue
            prev = d1[d1["day"] < day]
            if len(prev) < 25:
                continue
            p = prev.iloc[-1]
            if not (p["h"] > p["l"] > 0):
                continue
            levels = cr.day_levels(float(p["h"]), float(p["l"]), float(p["c"]))
            win = prev.tail(60)
            adr = guarded_adr(win["h"].tolist(), win["l"].tolist(), win["c"].tolist()) if len(win) >= 21 else None
            tol = cr.tolerance(adr, float(p["c"]))
            weekly = weekly_for(d1, day)
            piv = [v for k_, v in levels.items() if k_ in ("P", "TC", "BC", "R1", "R2", "S1", "S2", "PDH", "PDL")] + ([v for v in weekly.values()] if weekly else [])
            st = analyze(bars, prev_day=(float(p["h"]), float(p["l"]), float(p["c"])), adr=adr)
            idx = {b.ts: i for i, b in enumerate(bars)}
            for zn in st.zones:
                if not zn.retest_ts:
                    continue
                side = 1 if zn.side == "BULLISH" else -1
                # the first return to the zone; the limit at Z fills only if that bar trades to Z
                k = idx[zn.retest_ts[0]]
                b = bars[k]
                filled = (b.l <= zn.z) if side > 0 else (b.h >= zn.z)
                if not filled or k > len(bars) - 2:
                    continue
                entry = zn.z
                bias, rlo, rhi = h1_context(h1, h1_end, b.ts)
                disc = (entry <= (rlo + rhi) / 2) if side > 0 else (entry >= (rlo + rhi) / 2)
                if np.isnan(rlo) or np.isnan(rhi):
                    disc = False
                conf = any(zn.low - tol <= v <= zn.high + tol for v in piv)
                t = run_trade(side, bars, k, entry, zn.s1, levels, weekly, tol)
                if t is None:
                    continue
                rows.append({"sym": sym, "day": day.isoformat(), "side": side, "k": k, "late": k > LATE_BAR, "bos_kind": zn.bos_kind, "impulse": zn.impulse,
                             "swept_before": zn.swept_before, "h1_bias": bias, "h1_agree": bias == side, "discount": bool(disc), "confluence": conf,
                             "bars_to_retest": k - zn.bos_i, **t})
        if (si + 1) % 10 == 0 or si + 1 == len(syms):
            print(f"[{si + 1}/{len(syms)}] {sym} rows={len(rows)} {time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(rows).to_parquet(OUT / "smc.parquet", index=False)
    print("done", len(rows))


if __name__ == "__main__":
    main()
