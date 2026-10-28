"""Five concepts × six timeframes. See docs/research/2026-10-29-prereg-all-timeframes.md."""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.analysis.dayread import BREAK_FRAC
from kotsin_nse.instrument.universe import INDEX_ROOTS
from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.structure.engine import Bar5, analyze
from kotsin_nse.structure.levels import floor_pivots

DATA = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
OUT = Path(sys.argv[1])
TFS = sys.argv[2].split(",") if len(sys.argv) > 2 else ["5m", "15m", "30m", "1h", "4h", "1d"]
START, END = date(2024, 1, 1), date(2025, 9, 30)
EPOCH_ORD = date(1970, 1, 1).toordinal()
OPEN_S, CLOSE_S = 33300, 55800
SECS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
INTRADAY = {"5m", "15m", "30m"}
HOLD_SESSIONS = {"1h": 5, "4h": 5, "1d": 20}
CHAIN_SESSIONS = {"1h": 2, "4h": 2, "1d": 10}
TOL_MULT = {"5m": 1.0, "15m": 1.0, "30m": 1.0, "1h": 2.0, "4h": 2.0, "1d": 5.0}  # × a tenth of the ADR
RULES = ("wall_then_trail", "fixed_1to2")
GAP_AWARE = True
NO_LAST_BAR = True
PLACEBO_SEED = 20261029


def load_1d(sym: str) -> pd.DataFrame | None:
    p = DATA / "history" / "1d" / f"{sym}.parquet"
    if not p.exists():
        return None
    d = pd.read_parquet(p).drop_duplicates("ts", keep="last").sort_values("ts")
    d = d[(d["h"] > 0) & (d["l"] > 0)].copy()
    d["day"] = ((d["ts"] + 19800) // 86400).astype(int).map(lambda x: date.fromordinal(EPOCH_ORD + x))
    return d.drop_duplicates("day", keep="last").reset_index(drop=True)


def load_days(sym: str, tf: str) -> dict[date, list[Bar5]]:
    """Session bars at ``tf`` built from the 5m cache, per day (days with a full 5m session only)."""
    df = pd.read_parquet(DATA / "history" / "5m" / f"{sym}.parquet").drop_duplicates("ts", keep="last").sort_values("ts")
    ts = df["ts"].to_numpy(dtype=np.int64)
    ist = ts + 19800
    dayn, secs = ist // 86400, ist % 86400
    keep = (secs >= OPEN_S) & (secs < CLOSE_S)
    df, ts, dayn, secs = df[keep], ts[keep], dayn[keep], secs[keep]
    o_, h_, l_, c_, v_ = (df[k].to_numpy(dtype=float) for k in ("o", "h", "l", "c", "v"))
    step = SECS[tf]
    out = {}
    uniq, starts, counts = np.unique(dayn, return_index=True, return_counts=True)
    for dn, s, cnt in zip(uniq, starts, counts, strict=True):
        if cnt < 70:
            continue
        sl = slice(s, s + cnt)
        if (h_[sl] < l_[sl]).any() or (c_[sl] <= 0).any():
            continue
        d = date.fromordinal(EPOCH_ORD + int(dn))
        bk = (secs[sl] - OPEN_S) // step
        bars = []
        for b in np.unique(bk):
            m = np.where(bk == b)[0] + s
            bars.append(Bar5(int(ts[m[0]]), o_[m[0]], h_[m].max(), l_[m].min(), c_[m[-1]], v_[m].sum()))
        out[d] = bars
    return out


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / x.std() * np.sqrt(len(x))) if len(x) > 2 and x.std() > 0 else float("nan")


def score(side: int, bars: list[Bar5], k: int, entry: float, stop: float, levels, extra, tol: float, *, from_bar: int | None = None) -> dict | None:
    risk = (entry - stop) * side
    if risk <= 0:
        return None
    ws = cr.walls(side, entry, levels, extra, tol)
    if not ws:
        return None
    out = {"risk_bps": risk / entry * 1e4, "room_R": abs(ws[0][1] - entry) / risk}
    start = k + 1 if from_bar is None else from_bar
    for rule in RULES:
        plan = cr.ExitPlan.open(rule, side, entry, stop, [v for _k, v in ws], tol)
        res = None
        for q in range(start, len(bars)):
            sp = plan.stop_price()
            if GAP_AWARE and ((side > 0 and bars[q].o <= sp) or (side < 0 and bars[q].o >= sp)):
                res = (bars[q].o, "gap_stop" if sp * side < entry * side else "gap_trail")  # the open is already through the stop
                plan.bars += 1
                break
            res = plan.step(bars[q].h, bars[q].l, bars[q].c, last=(q == len(bars) - 1))
            if res is not None:
                break
        if res is None:
            res = (bars[-1].c, "close")
        out[rule] = plan.pnl_bps(res[0])
        out[rule + "_how"] = res[1]
        out[rule + "_bars"] = plan.bars
    return out


def retest_chain(bars: list[Bar5], levels, tol: float, side: int, confirm: int, max_entry_bar: int) -> tuple[int, int, int] | None:
    """(through, retest, confirm) indices of the generic chain, or None."""
    tc, bc = levels["TC"], levels["BC"]
    brk = tol * BREAK_FRAC
    c, h, lo = [b.c for b in bars], [b.h for b in bars], [b.l for b in bars]
    n = len(bars)
    thr = next((i for i in range(n) if (c[i] > tc + brk if side > 0 else c[i] < bc - brk)), None)
    if thr is None or thr > max_entry_bar:
        return None
    back = next((i for i in range(thr + 1, n) if (lo[i] <= tc + tol if side > 0 else h[i] >= bc - tol)), None)
    if back is None or back > max_entry_bar:
        return None
    if not (c[back] >= bc if side > 0 else c[back] <= tc):
        return None
    trig = h[back] if side > 0 else lo[back]
    j = next((k for k in range(back + 1, min(n, back + confirm + 1)) if (c[k] > trig if side > 0 else c[k] < trig)), None)
    if j is None or j > max_entry_bar:
        return None
    return thr, back, j


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


def ob_and_sweeps(bars: list[Bar5], prev_day, adr, tol, levels, extra, rows: list, base: dict, seen: set, k_off: int = 0, max_entry_bar: int | None = None, skip_ts: set | None = None, min_entry_bar: int = 0) -> None:
    st = analyze(bars, prev_day=prev_day, adr=adr)
    idx = {b.ts: i for i, b in enumerate(bars)}
    for zn in st.zones:
        key = ("ob", zn.side, zn.ob_ts)
        if key in seen or not zn.retest_ts:
            continue
        seen.add(key)
        side = 1 if zn.side == "BULLISH" else -1
        k = idx[zn.retest_ts[0]]
        if (max_entry_bar is not None and k > max_entry_bar) or k < min_entry_bar:
            continue  # an entry in the window's back part would use levels from after it
        b = bars[k]
        if not ((b.l <= zn.z) if side > 0 else (b.h >= zn.z)) or k >= len(bars) - 1 or (skip_ts and b.ts in skip_ts):
            continue
        t = score(side, bars, k, zn.z, zn.s1, levels, extra, tol, from_bar=k)
        if t:
            rows.append({**base, "concept": "order_block", "side": side, "entry_ts": b.ts, **t})
    for sw in st.sweeps:
        key = ("sw", sw.side, sw.ts)
        if key in seen:
            continue
        seen.add(key)
        k = idx[sw.ts]
        if (max_entry_bar is not None and k > max_entry_bar) or k >= len(bars) - 1 or k < min_entry_bar:
            continue
        side = 1 if sw.side == "BULLISH" else -1
        b = bars[k]
        if skip_ts and b.ts in skip_ts:
            continue
        stop = (b.l - tol) if side > 0 else (b.h + tol)
        t = score(side, bars, k, b.c, stop, levels, extra, tol)
        if t:
            rows.append({**base, "concept": "sweep_reversal", "side": side, "entry_ts": b.ts, **t})


def run_tf(tf: str, syms: list[str]) -> pd.DataFrame:
    rows: list[dict] = []
    t0 = time.time()
    intraday = tf in INTRADAY
    if intraday:
        scale = SECS[tf] // 300
        cr.OPEN_TEST_BARS, cr.BOS_BARS, cr.CONFIRM_BARS = max(1, 6 // scale), max(2, 12 // scale), max(1, 6 // scale)
        ib_bars = max(1, 3 // scale)
    for si, sym in enumerate(syms):
        d1 = load_1d(sym)
        if d1 is None or len(d1) < 80:
            continue
        if tf == "1d":
            d1 = d1[(d1["day"] >= date(2023, 1, 1)) & (d1["day"] <= END)].reset_index(drop=True)
            dbars = [Bar5(int(r.ts), r.o, r.h, r.l, r.c, r.v) for r in d1.itertuples()]
            wk = d1["day"].map(lambda x: x.isocalendar()[:2]).tolist()
            weeks = sorted(set(wk))
            wstart = {w: wk.index(w) for w in weeks}
            seen: set = set()
            for wi, w in enumerate(weeks):
                if wi < 8:
                    continue
                s0 = wstart[w]
                if not (START <= d1["day"].iloc[s0] <= END):
                    continue
                pw = d1.iloc[wstart[weeks[wi - 1]]:s0]
                this_m = (d1["day"].iloc[s0].year, d1["day"].iloc[s0].month)
                before = d1.iloc[:s0]
                ym = before["day"].map(lambda x: (x.year, x.month))
                prev_m = max(m for m in ym.unique() if m < this_m)
                pm = before[ym == prev_m]
                levels = cr.day_levels(float(pw["h"].max()), float(pw["l"].min()), float(pw["c"].iloc[-1]))
                fpm = floor_pivots(float(pm["h"].max()), float(pm["l"].min()), float(pm["c"].iloc[-1]))
                extra = {"WP": fpm["P"], "WR1": fpm["R1"], "WR2": fpm["R2"], "WS1": fpm["S1"], "WS2": fpm["S2"]}
                hist = d1.iloc[max(0, s0 - 60):s0]
                adr = guarded_adr(hist["h"].tolist(), hist["l"].tolist(), hist["c"].tolist()) if len(hist) >= 21 else None
                tol = cr.tolerance(adr, float(pw["c"].iloc[-1])) * TOL_MULT[tf]
                end = wstart[weeks[wi + 4]] if wi + 4 < len(weeks) else len(dbars)
                win = dbars[s0:end]
                max_entry = (wstart[weeks[wi + 2]] if wi + 2 < len(weeks) else len(dbars)) - s0 - 1
                base = {"tf": tf, "sym": sym, "day": d1["day"].iloc[s0].isoformat()}
                for side in (1, -1):
                    ch = retest_chain(win, levels, tol, side, 6, max_entry)
                    if ch:
                        thr, back, j = ch
                        stop = (win[back].l - tol) if side > 0 else (win[back].h + tol)
                        t = score(side, win, j, win[j].c, stop, levels, extra, tol)
                        if t:
                            rows.append({**base, "concept": "level_retest", "side": side, "entry_ts": win[j].ts, **t})
                    thr = next((i for i in range(min(len(win), max_entry + 1)) if (win[i].c > levels["TC"] + tol * BREAK_FRAC if side > 0 else win[i].c < levels["BC"] - tol * BREAK_FRAC)), None)
                    if thr is not None and thr < len(win) - 1:
                        stop = (win[thr].l - tol) if side > 0 else (win[thr].h + tol)
                        t = score(side, win, thr, win[thr].c, stop, levels, extra, tol)
                        if t:
                            rows.append({**base, "concept": "through_bar", "side": side, "entry_ts": win[thr].ts, **t})
                    rb = next((i for i in range(min(len(win), max_entry + 1)) if (win[i].c > levels["PDH"] if side > 0 else win[i].c < levels["PDL"])), None)
                    if rb is not None and rb < len(win) - 1:
                        stop = (win[rb].l - tol) if side > 0 else (win[rb].h + tol)
                        t = score(side, win, rb, win[rb].c, stop, levels, extra, tol)
                        if t:
                            rows.append({**base, "concept": "range_break", "side": side, "entry_ts": win[rb].ts, **t})
                if wi % 4 == 0:
                    before = len(rows)
                    wb = dbars[max(0, s0 - 60):end]
                    ob_and_sweeps(wb, None, adr, tol, levels, extra, rows, base, seen, max_entry_bar=min(len(dbars), s0 - max(0, s0 - 60) + max_entry), min_entry_bar=s0 - max(0, s0 - 60))
                    made = rows[before:]
                    if made:
                        rng = np.random.default_rng(PLACEBO_SEED + hash((sym, wi)) % 100000)
                        lo_i, hi_i = s0 - max(0, s0 - 60), min(len(wb) - 2, s0 - max(0, s0 - 60) + max_entry)
                        for r_ in made:
                            i = int(rng.integers(lo_i, hi_i + 1))
                            side = int(r_["side"])
                            b = wb[i]
                            stop = (b.l - tol) if side > 0 else (b.h + tol)
                            t = score(side, wb, i, b.c, stop, levels, extra, tol)
                            if t:
                                rows.append({**base, "concept": "placebo_" + r_["concept"], "side": side, "entry_ts": b.ts, **t})
        else:
            days = load_days(sym, tf)
            dlist = sorted(days)
            seen_pos: set = set()
            for di, d in enumerate(dlist):
                if not (START <= d <= END):
                    continue
                prev = d1[d1["day"] < d]
                if len(prev) < 25:
                    continue
                p = prev.iloc[-1]
                if not (p["h"] > p["l"] > 0):
                    continue
                levels = cr.day_levels(float(p["h"]), float(p["l"]), float(p["c"]))
                hist = prev.tail(60)
                adr = guarded_adr(hist["h"].tolist(), hist["l"].tolist(), hist["c"].tolist()) if len(hist) >= 21 else None
                tol = cr.tolerance(adr, float(p["c"])) * TOL_MULT[tf]
                extra = weekly_for(d1, d)
                base = {"tf": tf, "sym": sym, "day": d.isoformat()}
                prev_day = (float(p["h"]), float(p["l"]), float(p["c"]))
                if intraday:
                    bars = days[d]
                    n = len(bars)
                    if tf == "5m":
                        st = cr.sequence(bars, levels, tol)
                    else:
                        # at 15m / 30m the live chain's open test and BOS cannot form (a swing needs two bars each side):
                        # the generic chain — through the edge, the first bar back that holds, the first close beyond it
                        st = {}
                        for side in (1, -1):
                            ch = retest_chain(bars, levels, tol, side, cr.CONFIRM_BARS, n - 2)
                            thr = next((i for i in range(n) if (bars[i].c > levels["TC"] + tol * BREAK_FRAC if side > 0 else bars[i].c < levels["BC"] - tol * BREAK_FRAC)), None)
                            stg = cr.Stage(side)
                            stg.i_brk = thr
                            if ch:
                                stg.i_brk, stg.i_rt, stg.j = ch
                            st[side] = stg
                    for side, s in st.items():
                        if s.i_brk is not None and s.i_brk < n - 1:
                            k = int(s.i_brk)
                            stop = (bars[k].l - tol) if side > 0 else (bars[k].h + tol)
                            t = score(side, bars, k, bars[k].c, stop, levels, extra, tol)
                            if t:
                                rows.append({**base, "concept": "through_bar", "side": side, "entry_ts": bars[k].ts, **t})
                        if s.j is not None and s.j < n - 1:
                            j, i_rt = int(s.j), int(s.i_rt)
                            stop = (bars[i_rt].l - tol) if side > 0 else (bars[i_rt].h + tol)
                            t = score(side, bars, j, bars[j].c, stop, levels, extra, tol)
                            if t:
                                rows.append({**base, "concept": "level_retest", "side": side, "entry_ts": bars[j].ts, **t})
                    if n > ib_bars + 1:
                        ibh, ibl = max(b.h for b in bars[:ib_bars]), min(b.l for b in bars[:ib_bars])
                        ku = next((k for k in range(ib_bars, n) if bars[k].c > ibh), None)
                        kd = next((k for k in range(ib_bars, n) if bars[k].c < ibl), None)
                        cands = [(k, sd) for k, sd in ((ku, 1), (kd, -1)) if k is not None and k < n - 1]
                        if cands:
                            k, sd = min(cands)
                            stop = (ibl - tol) if sd > 0 else (ibh + tol)
                            t = score(sd, bars, k, bars[k].c, stop, levels, extra, tol)
                            if t:
                                rows.append({**base, "concept": "range_break", "side": sd, "entry_ts": bars[k].ts, **t})
                    ob_and_sweeps(bars, prev_day, adr, tol, levels, extra, rows, base, set())
                else:
                    hold = HOLD_SESSIONS[tf]
                    win = [b for dd in dlist[di:di + hold + 1] for b in days[dd]]
                    last_ts = {days[dd][-1].ts for dd in dlist[di:di + hold + 1]}
                    if NO_LAST_BAR:
                        last_ts_all = {days[dd][-1].ts for dd in dlist}
                    else:
                        last_ts_all = set()
                    n_chain = sum(len(days[dd]) for dd in dlist[di:di + CHAIN_SESSIONS[tf]])
                    max_entry = n_chain - 1
                    for side in (1, -1):
                        ch = retest_chain(win, levels, tol, side, 6, max_entry)
                        if ch:
                            thr, back, j = ch
                            stop = (win[back].l - tol) if side > 0 else (win[back].h + tol)
                            t = score(side, win, j, win[j].c, stop, levels, extra, tol)
                            if t:
                                rows.append({**base, "concept": "level_retest", "side": side, "entry_ts": win[j].ts, **t})
                        thr = next((i for i in range(min(len(win), max_entry + 1)) if (win[i].c > levels["TC"] + tol * BREAK_FRAC if side > 0 else win[i].c < levels["BC"] - tol * BREAK_FRAC)), None)
                        if thr is not None and thr < len(win) - 1:
                            stop = (win[thr].l - tol) if side > 0 else (win[thr].h + tol)
                            t = score(side, win, thr, win[thr].c, stop, levels, extra, tol)
                            if t:
                                rows.append({**base, "concept": "through_bar", "side": side, "entry_ts": win[thr].ts, **t})
                        rb = next((i for i in range(min(len(win), max_entry + 1)) if (win[i].c > levels["PDH"] if side > 0 else win[i].c < levels["PDL"])), None)
                        if rb is not None and rb < len(win) - 1:
                            stop = (win[rb].l - tol) if side > 0 else (win[rb].h + tol)
                            t = score(side, win, rb, win[rb].c, stop, levels, extra, tol)
                            if t:
                                rows.append({**base, "concept": "range_break", "side": side, "entry_ts": win[rb].ts, **t})
                    if di % 5 == 0:
                        back_bars = [b for dd in dlist[max(0, di - 10):di] for b in days[dd]]
                        before = len(rows)
                        ob_and_sweeps(back_bars + win, prev_day, adr, tol, levels, extra, rows, base, seen_pos, max_entry_bar=len(back_bars) + max_entry, skip_ts=last_ts_all, min_entry_bar=len(back_bars))
                        # placebo: as many random-bar entries as real ones just made, same side mix, same stop rule, walls, hold
                        made = rows[before:]
                        if made:
                            rng = np.random.default_rng(PLACEBO_SEED + hash((sym, di)) % 100000)
                            cands = [i for i in range(0, max_entry + 1) if win[i].ts not in last_ts_all and i < len(win) - 1]
                            for r_ in made:
                                if not cands:
                                    break
                                i = int(rng.choice(cands))
                                side = int(r_["side"])
                                b = win[i]
                                stop = (b.l - tol) if side > 0 else (b.h + tol)
                                t = score(side, win, i, b.c, stop, levels, extra, tol)
                                if t:
                                    rows.append({**base, "concept": "placebo_" + r_["concept"], "side": side, "entry_ts": b.ts, **t})
        if (si + 1) % 20 == 0 or si + 1 == len(syms):
            print(f"[{tf} {si + 1}/{len(syms)}] rows={len(rows)} {time.time() - t0:.0f}s", flush=True)
    return pd.DataFrame(rows)


def main() -> None:
    syms = sorted(p.stem for p in (DATA / "history" / "5m").glob("*.parquet") if p.stem not in INDEX_ROOTS and p.stem != "CRUDEOIL")
    for tf in TFS:
        df = run_tf(tf, syms)
        df.to_parquet(OUT / f"mtf_{tf}.parquet", index=False)
        print("done", tf, len(df), flush=True)


if __name__ == "__main__":
    main()
