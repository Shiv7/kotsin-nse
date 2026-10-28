"""Entry variants and skip filters read out of the 50 chart labels (owner 20 + chat-Claude 30), scored
on the engine's own sequence code (`cpr_retest.sequence`, `walls`, `ExitPlan`) over 5m bars.

Per symbol-day, per side where the baseline sequence confirmed (st.j):
  base      entry = close of the confirmation bar j, stop = retest bar low/high ∓ tol   (the live book)
  brk_bar   entry = close of the through bar i_brk ("entry when price leaves the zone"), stop = that bar's low/high ∓ tol
  brk_cpr   same entry, stop = the far CPR edge ∓ tol ("SL below BC")
  stop_in   entry at the retest bar's high/low (a stop order) in the first later bar that crosses it, same stop as base
Features at the base entry: rejections at the edge before the through bar, room to wall 1 in R, wall 1 already
touched before the retest ("trend gone"), the entry bar's range in tol, the excursion beyond the edge before the
retest in tol, the entry bar's clock.
IB15: per symbol-day, the first 5m close beyond the first-15-minute high/low (bar ≥ 3), stop = the other IB edge ∓ tol.
All exits: wall_then_trail to the session's last bar. Net = gross − 15 bps.
"""
from __future__ import annotations

import argparse
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

OPEN_S, CLOSE_S = 9 * 3600 + 15 * 60, 15 * 3600 + 30 * 60
EPOCH_ORD = date(1970, 1, 1).toordinal()
LATE_BAR = 63  # 14:30 on the 5m grid
COST = 15.0


def hm(i: int) -> str:
    m = 9 * 60 + 15 + 5 * i
    return f"{m // 60:02d}:{m % 60:02d}"


def trade(side: int, bars: list[Bar5], k: int, entry: float, stop: float, levels, weekly, tol: float) -> dict | None:
    risk = (entry - stop) * side
    if risk <= 0:
        return None
    ws = cr.walls(side, entry, levels, weekly, tol)
    if not ws:
        return None
    plan = cr.ExitPlan.open("wall_then_trail", side, entry, stop, [v for _k, v in ws], tol)
    n = len(bars)
    res = None
    for q in range(k + 1, n):
        res = plan.step(bars[q].h, bars[q].l, bars[q].c, last=(q == n - 1))
        if res is not None:
            break
    if res is None:
        res = (bars[-1].c, "close")
    pnl = plan.pnl_bps(res[0])
    return {"pnl": pnl, "how": res[1], "held": plan.bars, "risk_bps": risk / entry * 1e4, "w1_bps": abs(ws[0][1] - entry) / entry * 1e4,
            "w1": ws[0][0], "room_R": abs(ws[0][1] - entry) / risk}


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


def day_rows(sym: str, day: date, bars: list[Bar5], d1: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    prev = d1[d1["day"] < day]
    if len(prev) < 2:
        return [], []
    p = prev.iloc[-1]
    if not (p["h"] > p["l"] > 0):
        return [], []
    levels = cr.day_levels(float(p["h"]), float(p["l"]), float(p["c"]))
    win = prev.tail(60)
    adr = guarded_adr(win["h"].tolist(), win["l"].tolist(), win["c"].tolist()) if len(win) >= 21 else None
    tol = cr.tolerance(adr, float(p["c"]))
    weekly = weekly_for(d1, day)
    tc, bc = levels["TC"], levels["BC"]
    h = [b.h for b in bars]
    lo = [b.l for b in bars]
    c = [b.c for b in bars]
    n = len(bars)
    out: list[dict] = []
    st = cr.sequence(bars, levels, tol)
    for side, s in st.items():
        if s.i_brk is None:
            continue  # scored from the through bar on: everything known there is in the past
        i_brk, i_os = int(s.i_brk), int(s.i_os)
        i_rt = int(s.i_rt) if s.i_rt is not None else None
        j = int(s.j) if s.j is not None else None
        edge, far = (tc, bc) if side > 0 else (bc, tc)
        row = {"sym": sym, "day": day.isoformat(), "side": side, "stage": s.name, "i_os": i_os, "i_brk": i_brk, "i_rt": i_rt if i_rt is not None else -1,
               "j": j if j is not None else -1, "j_hm": hm(j) if j is not None else "", "brk_hm": hm(i_brk), "tol_bps": tol / c[i_brk] * 1e4, "late_brk": i_brk > LATE_BAR}
        # rejections = distinct visits to the edge before the through bar: a run of bars touching it (within a
        # tolerance) without closing through, re-armed once a bar stays a tolerance clear of it
        rej, touching = 0, False
        for k in range(i_os, i_brk):
            near = (h[k] >= edge - tol) if side > 0 else (lo[k] <= edge + tol)
            if near and not touching:
                rej += 1
            touching = near
        row["rej_n"] = rej
        # through-bar entries, known at the through bar
        e2 = c[i_brk]
        ta = trade(side, bars, i_brk, e2, (lo[i_brk] - tol) if side > 0 else (h[i_brk] + tol), levels, weekly, tol)
        tb = trade(side, bars, i_brk, e2, (far - tol) if side > 0 else (far + tol), levels, weekly, tol)
        row.update({"brk_bar_pnl": ta["pnl"] if ta else np.nan, "brk_bar_risk": ta["risk_bps"] if ta else np.nan, "brk_bar_room": ta["room_R"] if ta else np.nan,
                    "brk_bar_how": ta["how"] if ta else "", "brk_cpr_pnl": tb["pnl"] if tb else np.nan, "brk_cpr_risk": tb["risk_bps"] if tb else np.nan,
                    "brk_cpr_room": tb["room_R"] if tb else np.nan, "brk_range_tol": (h[i_brk] - lo[i_brk]) / tol})
        if i_rt is not None and s.result == "held":
            exc = ((max(h[i_brk:i_rt + 1]) - edge) if side > 0 else (edge - min(lo[i_brk:i_rt + 1]))) / tol
            row.update({"exc_tol": exc, "gap_rt": i_rt - i_brk})
            stop = (lo[i_rt] - tol) if side > 0 else (h[i_rt] + tol)
            # stop-order entry at the retest bar's extreme, known once the held retest bar closed
            trig = h[i_rt] if side > 0 else lo[i_rt]
            kk = next((k for k in range(i_rt + 1, min(n, i_rt + cr.CONFIRM_BARS + 1)) if (h[k] > trig if side > 0 else lo[k] < trig)), None)
            ts_ = trade(side, bars, kk, trig, stop, levels, weekly, tol) if kk is not None else None
            row.update({"stop_in_pnl": ts_["pnl"] if ts_ else np.nan, "stop_in_k": kk if kk is not None else -1})
        if j is not None:
            row.update({"bar_range_tol": (h[j] - lo[j]) / tol, "late": j > LATE_BAR})
            entry = c[j]
            stop = (lo[i_rt] - tol) if side > 0 else (h[i_rt] + tol)
            t = trade(side, bars, j, entry, stop, levels, weekly, tol)
            if t is not None:
                w1_px = entry + side * t["w1_bps"] / 1e4 * entry
                touched = (max(h[i_brk:j]) >= w1_px) if side > 0 else (min(lo[i_brk:j]) <= w1_px)
                row.update({"base_pnl": t["pnl"], "base_how": t["how"], "base_held": t["held"], "risk_bps": t["risk_bps"], "w1_bps": t["w1_bps"], "w1": t["w1"],
                            "room_R": t["room_R"], "trend_gone": bool(touched)})
        out.append(row)
    # IB15 break, independent of the sequence
    ib: list[dict] = []
    if n > 4:
        ibh, ibl = max(h[:3]), min(lo[:3])
        k_up = next((k for k in range(3, n) if c[k] > ibh), None)
        k_dn = next((k for k in range(3, n) if c[k] < ibl), None)
        cands = [(k_up, 1), (k_dn, -1)]
        cands = [(k, sd) for k, sd in cands if k is not None]
        if cands:
            k, sd = min(cands)
            stop = (ibl - tol) if sd > 0 else (ibh + tol)
            t = trade(sd, bars, k, c[k], stop, levels, weekly, tol)
            if t is not None:
                ib.append({"sym": sym, "day": day.isoformat(), "side": sd, "k": k, "k_hm": hm(k), "pnl": t["pnl"], "how": t["how"], "risk_bps": t["risk_bps"],
                           "w1_bps": t["w1_bps"], "room_R": t["room_R"], "late": k > LATE_BAR, "ib_tol": (ibh - ibl) / tol})
    return out, ib


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
        if any(b.h < b.l or b.c <= 0 for b in bars):
            continue
        out[date.fromordinal(EPOCH_ORD + int(dn))] = bars
    return out


def archive_5m(data_dir: Path, day: date) -> dict[str, pd.DataFrame]:
    """The feed's 1m archive of one day, built into 5m bars per symbol."""
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
    ap.add_argument("--archive-days", nargs="*", default=[], help="days taken from the feed's 1m archive instead of the 5m cache")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    data_dir, out = Path(a.data_dir), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    arch = {date.fromisoformat(d): None for d in a.archive_days}
    for d in list(arch):
        arch[d] = archive_5m(data_dir, d)
    syms = sorted(p.stem for p in (data_dir / "history" / "5m").glob("*.parquet") if p.stem not in INDEX_ROOTS and p.stem != "CRUDEOIL")
    if a.limit:
        syms = syms[: a.limit]
    rows: list[dict] = []
    ibs: list[dict] = []
    t0 = time.time()
    for si, sym in enumerate(syms):
        d1 = load_1d(data_dir, sym)
        if d1 is None:
            continue
        days: dict[date, list[Bar5]] = {}
        if start <= end:
            h5 = pd.read_parquet(data_dir / "history" / "5m" / f"{sym}.parquet")
            days.update({d: b for d, b in bars_by_day(h5).items() if start <= d <= end and len(b) >= 70})
        for d, by_sym in arch.items():
            if sym in by_sym:
                b = bars_by_day(by_sym[sym])
                if d in b and len(b[d]) >= 60:
                    days[d] = b[d]
        for d, bars in sorted(days.items()):
            r, ib = day_rows(sym, d, bars, d1)
            rows.extend(r)
            ibs.extend(ib)
        if (si + 1) % 10 == 0 or si + 1 == len(syms):
            print(f"[{si + 1}/{len(syms)}] {sym} rows={len(rows)} ib={len(ibs)} {time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(rows).to_parquet(out / "instances.parquet", index=False)
    pd.DataFrame(ibs).to_parquet(out / "ib15.parquet", index=False)
    print("done", len(rows), len(ibs))


if __name__ == "__main__":
    sys.exit(main())
