"""The day read (``analysis/dayread.py``) on every F&O stock-day we hold — the sweep behind
``docs/research/2026-10-26-dayread-sweep.md``.

Part A, price only: the 5-minute history cache in the development window 2024-01-01 → 2025-09-30
(the holdout 2025-10-01 → 2026-10-16 is spent and is not touched). Part B, with OI: the live
archive days (``archive/bars`` 1m, ``archive/oi`` through that day's scrip master: the front
future and the tracked expiry's strikes).

Per symbol (part A) or per day (part B) it writes parquet files under ``OUT/hist`` and ``OUT/arch``:
``days`` (one row a session: open/high/low/close, the levels, the gap, the initial balance, the
daily context), ``events`` (every level test, break and break of structure with the forward returns
from the bar on which it became known, and whether the level gave way within 6 / 12 bars / by the
close; ``BASE`` rows carry the unconditional forward returns at 09:15, 09:45, 10:15, 11:15, 12:15,
13:15, 14:15), ``legs`` (the 5m legs with the future's OI label and the option read where known),
``seq`` (the owner's sequence at bar level: open test held under the CPR → BOS within the hour →
close through the CPR → the first return to the band) and ``legs_htf`` (15m and 30m legs).
``dayread_stats.run`` turns these into the tables.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..analysis.dayread import (
    TOL_ADR,
    _resample,
    _strike_step,
    bands,
    cpr,
    despike,
    leg_oi,
    leg_options,
    level_events,
    structure_events,
)
from ..market.session import ist_at
from ..scan.replay import guarded_adr
from ..structure.engine import Bar5, resample_5m

DEV_START, DEV_END = date(2024, 1, 1), date(2025, 9, 30)
EPOCH_ORD = date(1970, 1, 1).toordinal()
OPEN_S, CLOSE_S = 9 * 3600 + 15 * 60, 15 * 3600 + 30 * 60
#: bars whose unconditional forward returns are recorded: 09:15 09:45 10:15 11:15 12:15 13:15 14:15
BASE_BARS = (0, 6, 12, 24, 36, 48, 60)
#: a session's OI change under this (%) is flat at the day level
DAY_FLAT_PCT = 0.5

DAY_COLS = ["sym", "day", "open", "high", "low", "close", "pdc", "gap_pct", "adr", "tol", "range_pos", "nbars", "i_high", "i_low",
            "P", "BC", "TC", "S1", "R1", "PDH", "PDL", "ibh", "ibl", "ib_break", "close_vs_ib", "n_legs",
            "oi_open", "oi_close", "day_build", "next_ret", "prev_ret", "ret5", "vs_dma20", "vs_dma50", "adr_pct"]
EV_COLS = ["sym", "day", "ts", "i", "kind", "level", "price", "c", "adrp", "r30", "r60", "rcl", "mfe", "mae", "f6", "f12", "fcl"]
LEG_COLS = ["sym", "day", "t0", "t1", "i0", "i1", "dir", "closed", "p0", "p1", "pct", "ret_close", "next_pct", "ret30",
            "build_up", "oi_chg_pct", "opt_read", "puts_net", "calls_net", "pcr"]
SEQ_COLS = ["sym", "day", "side", "i_os", "lvl_os", "rcl_os", "i_bos", "rcl_bos", "i_brk", "rcl_brk", "r60_brk", "i_rt", "result",
            "rcl_rt", "r60_rt", "mfe_rt", "mae_rt", "surv6"]
HTF_COLS = ["sym", "day", "tf", "t1", "dir", "pct", "next_pct"]

Row = tuple[Any, ...]
NO_CTX = (np.nan, np.nan, np.nan, np.nan)


def day_tables(
    sym: str, day: date, bars: list[Bar5], prev: tuple[float, float, float], adr: float | None, *,
    fut_oi: list[tuple[float, float]] | None = None, opt_oi: dict[tuple[str, float], list[tuple[float, float]]] | None = None,
    step: float | None = None, oi_day: tuple[float | None, float | None, str] = (None, None, ""), next_ret: float = np.nan,
    ctx: tuple[float, float, float, float] = NO_CTX,
) -> tuple[Row, list[Row], list[Row], list[Row], list[Row]]:
    """One session → its days row, event rows, leg rows, sequence rows and 15m/30m leg rows."""
    levels = cpr(*prev)
    c = np.array([b.c for b in bars])
    h = np.array([b.h for b in bars])
    lo = np.array([b.l for b in bars])
    n = len(bars)
    last = float(c[-1])
    tol = (adr or last * 0.01) * TOL_ADR
    brk = tol * 0.5
    bds = bands(levels)
    band = {b.name: b for b in bds}
    ev = level_events(bars, bds, tol)
    sev, legs = structure_events(bars)
    idx = {b.ts: i for i, b in enumerate(bars)}
    hmax = np.maximum.accumulate(h[::-1])[::-1]  # the highest high from i to the end
    lmin = np.minimum.accumulate(lo[::-1])[::-1]

    def fwd(i: int) -> tuple[float, float, float, float, float]:
        ci = c[i]
        r30 = (c[i + 6] / ci - 1) * 100 if i + 6 < n else np.nan
        r60 = (c[i + 12] / ci - 1) * 100 if i + 12 < n else np.nan
        rcl = (c[-1] / ci - 1) * 100
        mfe = (hmax[i + 1] / ci - 1) * 100 if i + 1 < n else np.nan
        mae = (lmin[i + 1] / ci - 1) * 100 if i + 1 < n else np.nan
        return r30, r60, rcl, mfe, mae

    def survival(kind: str, level: str | None, i: int) -> tuple[float, float, float]:
        """Did the level give way after the event (a close beyond it by half a tolerance) within 6 bars, 12 bars, by the close?"""
        b = band.get(level or "")
        if b is None:
            return np.nan, np.nan, np.nan
        if kind == "SUPPORT":
            bad = c < b.lo - brk
        elif kind == "RESISTANCE":
            bad = c > b.hi + brk
        elif kind == "BREAK_UP":
            bad = c < b.hi  # back below the level: a false break
        else:
            bad = c > b.lo
        return float(bad[i + 1:i + 7].any()), float(bad[i + 1:i + 13].any()), float(bad[i + 1:].any())

    dstr = day.isoformat()
    adrp = (lambda i: adr / c[i] * 100) if adr else (lambda i: np.nan)
    erows: list[Row] = []
    for e in ev + sev:
        i = idx[e.ts]
        erows.append((sym, dstr, e.ts, i, e.kind, e.level or "", e.price, float(c[i]), adrp(i), *fwd(i), *survival(e.kind, e.level, i)))
    for i in BASE_BARS:
        if i < n:
            erows.append((sym, dstr, bars[i].ts, i, "BASE", "", float(c[i]), float(c[i]), adrp(i), *fwd(i), np.nan, np.nan, np.nan))
    lrows: list[Row] = []
    oi = despike(list(fut_oi)) if fut_oi else []
    for k, lg in enumerate(legs):
        i0, i1 = idx[lg.t0], idx[lg.t1]
        o = leg_oi(lg, oi) if oi else None
        opts = leg_options(lg, lg.p1, opt_oi or {}, step) if opt_oi else None
        nxt = legs[k + 1] if k + 1 < len(legs) else None
        lrows.append((sym, dstr, lg.t0, lg.t1, i0, i1, "up" if lg.up else "down", lg.closed, lg.p0, lg.p1, (lg.p1 / lg.p0 - 1) * 100,
                      (c[-1] / lg.p1 - 1) * 100, ((nxt.p1 / nxt.p0 - 1) * 100) if nxt else np.nan,
                      ((c[i1 + 6] / c[i1] - 1) * 100) if i1 + 6 < n else np.nan,
                      o["build_up"] if o else "", o["oi_chg_pct"] if o else np.nan,
                      opts["read"] if opts else "", opts["puts_net"] if opts else np.nan, opts["calls_net"] if opts else np.nan,
                      (opts.get("pcr") if opts else None) or np.nan))
    # the owner's sequence at bar level: open test held under (over) the CPR → break of structure within an hour
    # → close through the CPR → the first return to the CPR band: held or failed, and the day from there
    tc, bc = levels["TC"], levels["BC"]
    srows: list[Row] = []
    for side in (1, -1):
        if side > 0:
            os_ = [e for e in ev if e.kind == "SUPPORT" and idx[e.ts] <= 6 and c[idx[e.ts]] < bc]
        else:
            os_ = [e for e in ev if e.kind == "RESISTANCE" and idx[e.ts] <= 6 and c[idx[e.ts]] > tc]
        if not os_:
            continue
        e0 = min(os_, key=lambda e: idx[e.ts])
        i_os = idx[e0.ts]
        row: list[Any] = [sym, dstr, side, i_os, e0.level, fwd(i_os)[2], np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, "", np.nan, np.nan, np.nan, np.nan, np.nan]
        bos_k = "BOS_UP" if side > 0 else "BOS_DOWN"
        bos = [idx[e.ts] for e in sev if e.kind == bos_k and i_os < idx[e.ts] <= i_os + 12]
        if bos:
            i_bos = min(bos)
            row[6], row[7] = i_bos, fwd(i_bos)[2]
            through = (c > tc + brk) if side > 0 else (c < bc - brk)
            cand = np.flatnonzero(through[i_bos:])
            if len(cand):
                i_brk = i_bos + int(cand[0])
                f = fwd(i_brk)
                row[8], row[9], row[10] = i_brk, f[2], f[1]
                back = np.flatnonzero(lo[i_brk + 1:] <= tc + tol) if side > 0 else np.flatnonzero(h[i_brk + 1:] >= bc - tol)
                if len(back):
                    i_rt = i_brk + 1 + int(back[0])
                    f = fwd(i_rt)
                    held = c[i_rt] >= bc if side > 0 else c[i_rt] <= tc
                    row[11], row[12] = i_rt, "held" if held else "failed"
                    row[13], row[14], row[15], row[16] = f[2], f[1], f[3], f[4]
                    bad = (c[i_rt + 1:i_rt + 7] < bc - brk) if side > 0 else (c[i_rt + 1:i_rt + 7] > tc + brk)
                    row[17] = float(not bad.any())
                else:
                    row[12] = "none"
        srows.append(tuple(row))
    hrows: list[Row] = []
    for tf, k in (("15m", 3), ("30m", 6)):
        _ev, hl = structure_events(_resample(bars, k))
        for j, lg in enumerate(hl):
            if not lg.closed or j + 1 >= len(hl):
                continue
            nxt = hl[j + 1]
            hrows.append((sym, dstr, tf, lg.t1, "up" if lg.up else "down", (lg.p1 / lg.p0 - 1) * 100, (nxt.p1 / nxt.p0 - 1) * 100))
    o_, hi, lw = bars[0].o, float(h.max()), float(lo.min())
    pdc = levels["PDC"]
    ibh, ibl = float(h[:12].max()), float(lo[:12].min())
    ib_break = ""
    for i in range(12, n):
        if c[i] > ibh:
            ib_break = "up"
            break
        if c[i] < ibl:
            ib_break = "down"
            break
    close_vs_ib = 1 if last > ibh else (-1 if last < ibl else 0)
    drow = (sym, dstr, o_, hi, lw, last, pdc, (o_ / pdc - 1) * 100, adr if adr else np.nan, tol, (last - lw) / (hi - lw) if hi > lw else np.nan, n,
            int(h.argmax()), int(lo.argmin()), levels["P"], levels["BC"], levels["TC"], levels["S1"], levels["R1"], prev[0], prev[1],
            ibh, ibl, ib_break, close_vs_ib, len(legs), oi_day[0], oi_day[1], oi_day[2], next_ret, *ctx, (adr / last * 100) if adr else np.nan)
    return drow, erows, lrows, srows, hrows


def write(out_dir: Path, name: str, tables: tuple[list[Row], ...]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for kind, cols, rows in zip(("days", "events", "legs", "seq", "legs_htf"), (DAY_COLS, EV_COLS, LEG_COLS, SEQ_COLS, HTF_COLS), tables, strict=True):
        pd.DataFrame(rows, columns=cols).to_parquet(out_dir / f"{name}.{kind}.parquet", index=False)


def prev_and_adr(d1_ts: np.ndarray, d1_h: np.ndarray, d1_l: np.ndarray, d1_c: np.ndarray, day: date) -> tuple[tuple[float, float, float] | None, float | None, tuple[float, float, float, float]]:
    """The previous session (h, l, c), the guarded ADR and the daily context (previous day's return,
    5-day return, close vs the 20- and 50-day average), from the rows strictly before ``day``."""
    k = int(np.searchsorted(d1_ts, ist_at(day, "00:00"), side="left"))
    if k < 2:
        return None, None, NO_CTX
    prev = (float(d1_h[k - 1]), float(d1_l[k - 1]), float(d1_c[k - 1]))
    j = max(0, k - 60)  # ADR_SESSIONS = 50
    adr = guarded_adr(d1_h[j:k].tolist(), d1_l[j:k].tolist(), d1_c[j:k].tolist())
    c = d1_c
    ctx = ((c[k - 1] / c[k - 2] - 1) * 100,
           (c[k - 1] / c[k - 6] - 1) * 100 if k >= 6 else np.nan,
           (c[k - 1] / c[k - 20:k].mean() - 1) * 100 if k >= 20 else np.nan,
           (c[k - 1] / c[k - 50:k].mean() - 1) * 100 if k >= 50 else np.nan)
    return prev, adr, ctx


def _daily(data_dir: Path, sym: str) -> tuple[np.ndarray, ...] | None:
    p1 = data_dir / "history" / "1d" / f"{sym}.parquet"
    if not p1.exists():
        return None
    d1 = pd.read_parquet(p1).drop_duplicates("ts", keep="last").sort_values("ts")
    return tuple(d1[c].to_numpy(dtype=float) for c in ("ts", "h", "l", "c"))


def sweep_history(data_dir: Path, out: Path, start: date = DEV_START, end: date = DEV_END, limit: int | None = None, log: Callable[[str], None] = print) -> int:
    """Part A: every symbol of the 5m cache, sessions in [start, end] with at least 70 bars."""
    syms = sorted(p.stem for p in (data_dir / "history" / "5m").glob("*.parquet") if p.stem != "CRUDEOIL")
    if limit:
        syms = syms[:limit]
    t_start = time.time()
    done = 0
    for si, sym in enumerate(syms):
        if (out / "hist" / f"{sym}.days.parquet").exists():
            continue
        daily = _daily(data_dir, sym)
        if daily is None:
            continue
        d1_ts, d1_h, d1_l, d1_c = daily
        h5 = pd.read_parquet(data_dir / "history" / "5m" / f"{sym}.parquet").drop_duplicates("ts", keep="last").sort_values("ts")
        ts = h5["ts"].to_numpy(dtype=np.int64)
        ist = ts + 19800
        dayn, secs = ist // 86400, ist % 86400
        keep = (secs >= OPEN_S) & (secs < CLOSE_S)
        h5 = h5[keep]
        ts, dayn = ts[keep], dayn[keep]
        o_, h_, l_, c_, v_ = (h5[c].to_numpy(dtype=float) for c in ("o", "h", "l", "c", "v"))
        tables: tuple[list[Row], ...] = ([], [], [], [], [])
        uniq, starts, counts = np.unique(dayn, return_index=True, return_counts=True)
        for dn, s, cnt in zip(uniq, starts, counts, strict=True):
            day = date.fromordinal(EPOCH_ORD + int(dn))
            if day < start or day > end or cnt < 70:
                continue
            prev, adr, ctx = prev_and_adr(d1_ts, d1_h, d1_l, d1_c, day)
            if prev is None or prev[0] <= prev[1]:
                continue
            bars = [Bar5(int(ts[i]), o_[i], h_[i], l_[i], c_[i], v_[i]) for i in range(s, s + cnt)]
            if any(b.h < b.l or b.c <= 0 for b in bars):
                continue
            d, e, lg, sq, ht = day_tables(sym, day, bars, prev, adr, ctx=ctx)
            tables[0].append(d)
            tables[1].extend(e)
            tables[2].extend(lg)
            tables[3].extend(sq)
            tables[4].extend(ht)
        if tables[0]:
            write(out / "hist", sym, tables)
            done += len(tables[0])
        log(f"[{si + 1}/{len(syms)}] {sym} days={len(tables[0])} events={len(tables[1])} legs={len(tables[2])} seq={len(tables[3])} {time.time() - t_start:.0f}s")
    return done


def archive_days(data_dir: Path, min_bars_rows: int = 10_000) -> list[date]:
    """Archive days with a real session of 1m bars (a partial evening file has a few hundred rows)."""
    out = []
    for p in sorted((data_dir / "archive" / "bars").glob("????-??-??.parquet")):
        if p.stat().st_size > min_bars_rows * 10:
            out.append(date.fromisoformat(p.stem))
    return out


def sweep_archive(data_dir: Path, out: Path, days: list[date] | None = None, log: Callable[[str], None] = print) -> int:
    """Part B: the archive days, with the front future's OI and the tracked expiry's strikes."""
    done = 0
    for day in days or archive_days(data_dir):
        dstr = day.isoformat()
        b = pd.read_parquet(data_dir / "archive" / "bars" / f"{dstr}.parquet")
        masters = sorted(p for p in (data_dir / "scripmaster").glob("nse_fo-*.csv") if p.stem[7:] <= dstr)
        oi_p = data_dir / "archive" / "oi" / f"{dstr}.parquet"
        if not masters or not oi_p.exists():
            log(f"archive {dstr}: no scrip master or OI archive, skipped")
            continue
        sm = pd.read_csv(masters[-1], usecols=["ScripCode", "SymbolRoot", "ScripType", "StrikeRate", "Expiry"], dtype=str)
        fut = sm[(sm["ScripType"] == "XX") & (sm["Expiry"] >= dstr)].sort_values("Expiry").drop_duplicates("SymbolRoot")
        fut_code = dict(zip(fut["SymbolRoot"], fut["ScripCode"], strict=True))
        oi = pd.read_parquet(oi_p)
        oi["scrip_code"] = oi["scrip_code"].astype(str)
        open_ts, close_ts = ist_at(day, "09:15"), ist_at(day, "15:30")
        oi = oi[(oi["ts"] >= open_ts - 300) & (oi["ts"] <= close_ts + 600)].sort_values("ts")
        opts_sm = sm[sm["ScripType"].isin(["CE", "PE"])]
        tables: tuple[list[Row], ...] = ([], [], [], [], [])
        for sym, g in b.groupby("symbol"):
            code = g["scrip_code"].astype(str).mode().iloc[0]
            g = g[g["scrip_code"].astype(str) == code]
            rows = g[(g["ts"] >= open_ts) & (g["ts"] < close_ts)].sort_values("ts")
            bars = resample_5m(list(rows[["ts", "o", "h", "l", "c", "v"]].itertuples(index=False, name=None)), open_ts)
            if len(bars) < 60:
                continue
            daily = _daily(data_dir, str(sym))
            if daily is None:
                continue
            d1_ts, d1_h, d1_l, d1_c = daily
            prev, adr, ctx = prev_and_adr(d1_ts, d1_h, d1_l, d1_c, day)
            if prev is None:
                continue
            after = d1_c[d1_ts > ist_at(day, "23:59")]
            next_ret = (float(after[0]) / bars[-1].c - 1) * 100 if len(after) else np.nan
            fc = fut_code.get(str(sym))
            fo = oi[oi["scrip_code"] == fc] if fc else oi.iloc[0:0]
            fut_oi = [(float(t), float(v)) for t, v in zip(fo["ts"], fo["oi"], strict=True)]
            oi_day: tuple[float | None, float | None, str] = (None, None, "")
            if len(fut_oi) >= 2:
                ds = despike(fut_oi)
                o0, o1 = ds[0][1], ds[-1][1]
                pc = (bars[-1].c / bars[0].o - 1) * 100
                oc = (o1 / o0 - 1) * 100 if o0 else 0.0
                lab = "FLAT" if abs(oc) < DAY_FLAT_PCT else ("LB" if oc > 0 and pc >= 0 else "SB" if oc > 0 else "SC" if pc >= 0 else "LU")
                oi_day = (o0, o1, lab)
            m = oi.merge(opts_sm[opts_sm["SymbolRoot"] == sym], left_on="scrip_code", right_on="ScripCode")
            opt_oi: dict[tuple[str, float], list[tuple[float, float]]] = {}
            step = None
            if not m.empty:
                exp = str(m.groupby("Expiry").size().idxmax())
                m = m[m["Expiry"] == exp]
                for (kind, k), gg in m.groupby(["ScripType", "StrikeRate"]):
                    opt_oi[(str(kind), float(k))] = [(float(t), float(v)) for t, v in zip(gg["ts"], gg["oi"], strict=True)]
                step = _strike_step(list(opt_oi))
            d, e, lg, sq, ht = day_tables(str(sym), day, bars, prev, adr, fut_oi=fut_oi, opt_oi=opt_oi, step=step, oi_day=oi_day, next_ret=next_ret, ctx=ctx)
            tables[0].append(d)
            tables[1].extend(e)
            tables[2].extend(lg)
            tables[3].extend(sq)
            tables[4].extend(ht)
        write(out / "arch", dstr, tables)
        done += len(tables[0])
        log(f"archive {dstr}: days={len(tables[0])} events={len(tables[1])} legs={len(tables[2])} with_fut_oi={sum(1 for r in tables[0] if r[28])}")
    return done
