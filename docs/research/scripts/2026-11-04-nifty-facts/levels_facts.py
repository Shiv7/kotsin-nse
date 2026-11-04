"""Price-action, level and timing FACTS on the development window (2024-01-01 → 2025-09-30), from the history
cache's 5m bars (data-live/history/5m/<SYM>.parquet) and the broker's daily candles — descriptions with counts, not a
strategy test; the holdout (2025-10-01 → 2026-10-16) is untouched. Levels, tolerance and break margin are the engine's
(strategy/cpr_retest: day_levels, tolerance = 0.1 ADR, brk = 0.5 tol; ADR = scan.replay.guarded_adr).

Definitions (so every number here can be rebuilt):
  session   = the 5m bars 09:15 ≤ t < 15:30 (75 bars); the 15:30 closing bar is dropped; the daily candle gives PDH/PDL/PDC.
  walls     = TC/BC/P, R1/R2, S1/S2, PDH/PDL, PDC (previous close), the round numbers at the engine's step (RN100 for
              NIFTY) and the half-steps between them (RN50), ORH/ORL (first three bars, valid from bar 3), IBH/IBL (first
              twelve bars, valid from bar 12), H1H/H1L (each completed 09:15-grid hourly candle's high/low, from its end).
  touch     = support: the bar's low within ±tol of the wall and the previous close above wall+tol (a fresh approach);
              resistance: mirrored. Bar 0 counts when the open is on the far side. Bars in the zone after a touch are
              not new touches; the next touch needs a close more than tol away first.
  outcome   = over the touch bar and the next K=6 bars (30 min, the rule's confirmation window):
              broke = any close beyond wall ∓ brk; held = not broke;
              bounce = (best excursion away from the wall after the touch bar − the touch bar's extreme) / tol;
              undercut = how far the wick went through the wall while the touch held, / tol (the stop question);
              fwd = the close 6 bars later vs the touch bar's close, in tol, signed so that + = away from the wall;
              reach = the next wall beyond (≥ tol away) was touched within 12 bars after the touch (held touches).
  payoff    = enter at the touch bar's close, stop one tol beyond the wall (risk = close − (wall − tol)); over the next
              12 bars: mfe/mae in tol; stopped = mae ≥ risk; 2R = mfe ≥ 2 × risk (before any stop check — a ceiling).
  touch_no  = how many touches of that same wall the day had already seen (1st, 2nd, 3rd+).
  confl     = distinct walls (any kind) within tol of the touched wall at that bar (1 = alone); confl_t = within 0.3 tol.
  control   = the same wall set moved by ±0.43 ADR (same density, no information), scored the same way.
Usage: levels_facts.py OUT_DIR [SYMBOL ... | N50]   (default NIFTY; N50 = data-live/universe-nifty50.txt without the indices)
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.scan.replay import guarded_adr
from kotsin_nse.strategy import cpr_retest as cr

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
DEV0, DEV1 = datetime.date(2024, 1, 1), datetime.date(2025, 9, 30)
K = 6
REACH = 12
SHIFTS = (0.43, -0.43)
BUCKETS = [("09:15-10:15", 0, 12), ("10:15-12:15", 12, 36), ("12:15-14:15", 36, 60), ("14:15-15:00", 60, 69)]
ORDER = ["TC", "BC", "P", "R1", "S1", "R2", "S2", "PDH", "PDL", "PDC", "ORH", "ORL", "IBH", "IBL", "H1H", "H1L", "RNstep", "RNhalf"]


def load(sym: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    m5 = pd.read_parquet(DD / "history" / "5m" / f"{sym}.parquet").drop_duplicates("ts").sort_values("ts")
    t = pd.to_datetime(m5.ts + 19800, unit="s")
    m5["d"] = t.dt.date
    m5["hm"] = t.dt.strftime("%H:%M")
    m5 = m5[(m5.hm >= "09:15") & (m5.hm < "15:30")]
    d1 = pd.read_parquet(DD / "history" / "1d" / f"{sym}.parquet").sort_values("ts")
    d1["d"] = pd.to_datetime(d1.ts + 19800, unit="s").dt.date
    d1["final"] = ((d1.ts + 19800) % 86400 == 0)
    d1 = d1.sort_values(["d", "final"]).drop_duplicates("d", keep="last")
    return m5, d1


def touches(kind: str, w: float, v0: int, hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, op: float, tol: float, side: int) -> list[dict]:
    brk = tol * 0.5
    n = len(cl)
    out: list[dict] = []
    i = max(v0, 0)
    armed = True
    nth = 0
    while i <= n - K - 1:
        fresh = (cl[i - 1] > w + tol) if i > 0 else (op > w + tol)
        if not armed and cl[i] > w + tol:
            armed = True
        if armed and fresh and (w - tol <= lo[i] <= w + tol):
            seg_c = cl[i : i + K + 1]; seg_l = lo[i : i + K + 1]; seg_h = hi[i + 1 : i + K + 1]
            h12 = hi[i + 1 : i + 1 + REACH]; l12 = lo[i + 1 : i + 1 + REACH]
            nth += 1
            out.append({"kind": kind, "bar": i, "side": side, "touch_no": nth, "broke": bool((seg_c < w - brk).any()),
                        "bounce": (seg_h.max() - lo[i]) / tol, "undercut": (w - seg_l.min()) / tol, "fwd": (cl[i + K] - cl[i]) / tol,
                        "c_rel": (cl[i] - w) / tol, "mfe": (h12.max() - cl[i]) / tol, "mae": (cl[i] - l12.min()) / tol, "above_open": bool(w > op), "w": w})
            armed = False
        i += 1
    return out


def day_walls(lv: dict, pdc: float, step: float, h: np.ndarray, lo: np.ndarray, lo_px: float, hi_px: float) -> list[tuple[str, float, int]]:
    ws = [("TC", lv["TC"], 0), ("BC", lv["BC"], 0), ("P", lv["P"], 0), ("R1", lv["R1"], 0), ("R2", lv["R2"], 0), ("S1", lv["S1"], 0), ("S2", lv["S2"], 0),
          ("PDH", lv["PDH"], 0), ("PDL", lv["PDL"], 0), ("PDC", pdc, 0)]
    r = np.floor(lo_px / step) * step
    while r <= hi_px + step:
        ws.append(("RNstep", float(r), 0)); r += step
    half = step / 2
    r = np.floor(lo_px / half) * half
    while r <= hi_px + half:
        if r % step:
            ws.append(("RNhalf", float(r), 0))
        r += half
    ws += [("ORH", float(h[:3].max()), 3), ("ORL", float(lo[:3].min()), 3), ("IBH", float(h[:12].max()), 12), ("IBL", float(lo[:12].min()), 12)]
    for k in range(1, 6):
        a, b = 12 * (k - 1), 12 * k
        ws += [("H1H", float(h[a:b].max()), b), ("H1L", float(lo[a:b].min()), b)]
    return ws


def one_symbol(sym: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    m5, d1 = load(sym)
    T: list[dict] = []; DAYS: list[dict] = []
    for D, g in m5.groupby("d"):
        if not (DEV0 <= D <= DEV1) or len(g) < 70:
            continue
        before = d1[d1.d < D]
        if len(before) < 61:
            continue
        prev = before.iloc[-1]
        adr = guarded_adr(before.h.tolist(), before.l.tolist(), before.c.tolist())
        if not adr:
            continue
        tol = cr.tolerance(adr, float(prev.c))
        lv = cr.day_levels(float(prev.h), float(prev.l), float(prev.c))
        step = cr.round_step(float(prev.c))
        o, h, lo, c = (g[x].to_numpy(float) for x in ("o", "h", "l", "c"))
        op = float(o[0]); dh, dl = float(h.max()), float(lo.min()); rng = dh - dl
        ws = day_walls(lv, float(prev.c), step, h, lo, float(prev.c) - 2.5 * adr, float(prev.c) + 2.5 * adr)
        sets = [("real", ws)] + [(f"ctl{s:+.2f}", [(k, w + s * adr, v) for k, w, v in ws]) for s in SHIFTS]
        for tag, wl in sets:
            prices = np.array([w for _, w, _ in wl]); valid = np.array([v for _, _, v in wl])
            for kind, w, v0 in wl:
                for side, (H, L, C, W, O) in ((1, (h, lo, c, w, op)), (-1, (-lo, -h, -c, -w, -op))):
                    for t in touches(kind, W, v0, H, L, C, O, tol, side):
                        i = t["bar"]
                        near = (valid <= i)
                        t["confl"] = int((near & (np.abs(prices - w) <= tol)).sum()); t["confl_t"] = int((near & (np.abs(prices - w) <= 0.3 * tol)).sum())
                        if t["broke"]:
                            t["reach"] = np.nan
                        elif side == 1:
                            nxt = prices[(prices >= w + tol) & near]; t["reach"] = float(len(nxt) and h[i + 1 : i + 1 + REACH].max() >= nxt.min())
                        else:
                            nxt = prices[(prices <= w - tol) & near]; t["reach"] = float(len(nxt) and lo[i + 1 : i + 1 + REACH].min() <= nxt.max())
                        t.update(sym=sym, d=D, set=tag, tol=tol, adr=adr)
                        del t["w"]
                        T.append(t)
        ibh, ibl = float(h[:12].max()), float(lo[:12].min()); ibr = ibh - ibl
        up = bool((c[12:] > ibh).any()); dn = bool((c[12:] < ibl).any())
        hb = int(h.argmax()); lb = int(lo.argmin())
        DAYS.append({"sym": sym, "d": D, "adr": adr, "tol": tol, "rng_adr": rng / adr, "gap_adr": (op - float(prev.c)) / adr, "open": op, "pdc": float(prev.c), "close": float(c[-1]),
                     "cpr_w_adr": (lv["TC"] - lv["BC"]) / adr, "open_in_cpr": bool(lv["BC"] <= op <= lv["TC"]),
                     "cpr_touched": bool((lo <= lv["TC"] + tol).any() and (h >= lv["BC"] - tol).any()),
                     "pdh_touch": bool((h >= lv["PDH"] - tol).any()), "pdl_touch": bool((lo <= lv["PDL"] + tol).any()),
                     "pdc_touch": bool((lo <= prev.c + tol).any() and (h >= prev.c - tol).any()), "pdc_touch_1h": bool((lo[:12] <= prev.c + tol).any() and (h[:12] >= prev.c - tol).any()),
                     "hi_bar": hb, "lo_bar": lb, "hi_in_ib": hb < 12, "lo_in_ib": lb < 12, "hi_bar0": hb == 0, "lo_bar0": lb == 0,
                     "ib_adr": ibr / adr, "ib_up": up, "ib_dn": dn, "ext_up_ib": (dh - ibh) / ibr if up and ibr else np.nan, "ext_dn_ib": (ibl - dl) / ibr if dn and ibr else np.nan,
                     "close_above_ibh": bool(c[-1] > ibh), "close_below_ibl": bool(c[-1] < ibl), "close_pos": (c[-1] - dl) / rng if rng else np.nan,
                     "first_bar_rng_share": (h[0] - lo[0]) / rng if rng else np.nan, "day_dir": np.sign(c[-1] - op), "ib_dir": np.sign(c[11] - op),
                     **{f"rng_{a}": (h[i:j].max() - lo[i:j].min()) / adr for a, i, j in BUCKETS}, **{f"vol5_{a}": float(np.median(h[i:j] - lo[i:j])) / tol for a, i, j in BUCKETS}})
    T_ = pd.DataFrame(T)
    for col in ("bounce", "undercut", "fwd", "c_rel", "mfe", "mae", "reach", "tol", "adr"):
        if col in T_:
            T_[col] = T_[col].astype("float32")
    for col in ("kind", "set", "sym"):
        if col in T_:
            T_[col] = T_[col].astype("category")
    return T_, pd.DataFrame(DAYS)


def main() -> None:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    syms = sys.argv[2:] or ["NIFTY"]
    if syms == ["N50"]:
        syms = [s for s in (DD / "universe-nifty50.txt").read_text().split() if not s.startswith("#") and s.isupper() and "NIFTY" not in s]
        syms = [s for s in syms if (DD / "history" / "5m" / f"{s}.parquet").exists()]
    TT, DDs = [], []
    for sym in syms:
        try:
            t_, d_ = one_symbol(sym)
        except FileNotFoundError:
            continue
        TT.append(t_); DDs.append(d_)
        print(f"  {sym}: {len(d_)} sessions, {len(t_):,} touches", file=sys.stderr)
    T_ = pd.concat(TT, ignore_index=True); Dy = pd.concat(DDs, ignore_index=True)
    T_["kind"] = T_.kind.astype(str); T_["set"] = T_.set.astype(str)
    T_.to_parquet(out / "touches.parquet"); Dy.to_parquet(out / "days.parquet")
    pct = lambda s: f"{100 * s.mean():.0f} %"
    label = ", ".join(syms) if len(syms) <= 3 else f"{len(syms)} symbols"
    print(f"== {label}: {len(Dy)} symbol-sessions {Dy.d.min()} → {Dy.d.max()}; ADR median {Dy.adr.median():.0f}, tol median {Dy.tol.median():.1f} (0.1 ADR)")
    print("\n== A. the day: gaps, extremes, initial balance (first hour), previous-day levels")
    print(f"  gap vs previous close (ADR units): |gap| median {Dy.gap_adr.abs().median():.2f}; P(|gap| > 0.3 ADR) {pct(Dy.gap_adr.abs() > 0.3)}; P(|gap| > 0.6 ADR) {pct(Dy.gap_adr.abs() > 0.6)}")
    for lo_, hi_ in ((0, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 9)):
        s = Dy[(Dy.gap_adr.abs() >= lo_) & (Dy.gap_adr.abs() < hi_)]
        if len(s):
            print(f"    |gap| {lo_}–{hi_} ADR: N {len(s)}, previous close touched during the day {pct(s.pdc_touch)}, within the first hour {pct(s.pdc_touch_1h)}, day closes on the gap's side of PDC {pct(np.sign(s.close - s.pdc) == np.sign(s.gap_adr))}")
    print(f"  day high in the first hour {pct(Dy.hi_in_ib)}, day low in the first hour {pct(Dy.lo_in_ib)}, both (the first hour is the day's range) {pct(Dy.hi_in_ib & Dy.lo_in_ib)}, neither {pct(~Dy.hi_in_ib & ~Dy.lo_in_ib)}")
    print(f"  day high in the first 5m bar {pct(Dy.hi_bar0)}, day low in the first 5m bar {pct(Dy.lo_bar0)}; the first bar's range is {100 * Dy.first_bar_rng_share.median():.0f} % of the day's (median)")
    lab = ["09:15", "09:45", "10:15", "10:45", "11:15", "11:45", "12:15", "12:45", "13:15", "13:45", "14:15", "14:45"]
    for nm, col in (("HIGH", "hi_bar"), ("LOW ", "lo_bar")):
        hist = pd.Series(np.minimum(Dy[col] // 6, 11)).value_counts(normalize=True).sort_index()
        print(f"  when the day's {nm} forms (half-hour, % of days): " + "  ".join(f"{lab[int(k)]} {100 * v:.0f}" for k, v in hist.items()))
    print(f"  IB range median {Dy.ib_adr.median():.2f} ADR; day range median {Dy.rng_adr.median():.2f} ADR")
    print(f"  after the first hour: close beyond IBH only {pct(Dy.ib_up & ~Dy.ib_dn)}, beyond IBL only {pct(Dy.ib_dn & ~Dy.ib_up)}, both {pct(Dy.ib_up & Dy.ib_dn)}, neither (inside all day) {pct(~Dy.ib_up & ~Dy.ib_dn)}")
    print(f"  IBH broken (a close above): day closes above IBH {pct(Dy[Dy.ib_up].close_above_ibh)} (N {int(Dy.ib_up.sum())}); extension beyond IBH median {Dy.ext_up_ib.median():.2f} × IB range, P(≥ 0.5 IB) {pct(Dy.ext_up_ib.dropna() >= 0.5)}")
    print(f"  IBL broken (a close below): day closes below IBL {pct(Dy[Dy.ib_dn].close_below_ibl)} (N {int(Dy.ib_dn.sum())}); extension beyond IBL median {Dy.ext_dn_ib.median():.2f} × IB range, P(≥ 0.5 IB) {pct(Dy.ext_dn_ib.dropna() >= 0.5)}")
    print(f"  first-hour direction (close of bar 12 vs open) matches the day's close direction {pct(Dy.ib_dir == Dy.day_dir)} (N {len(Dy)})")
    print(f"  trend day (close in the top/bottom 20 % of the range): {pct((Dy.close_pos >= 0.8) | (Dy.close_pos <= 0.2))}; close in the middle 40 % {pct((Dy.close_pos > 0.3) & (Dy.close_pos < 0.7))}")
    print(f"  PDH touched {pct(Dy.pdh_touch)}, PDL touched {pct(Dy.pdl_touch)}, both {pct(Dy.pdh_touch & Dy.pdl_touch)}, neither (inside day) {pct(~Dy.pdh_touch & ~Dy.pdl_touch)}; CPR band touched {pct(Dy.cpr_touched)}; open inside the CPR {pct(Dy.open_in_cpr)}")
    Dy["cpr_t"] = pd.qcut(Dy.cpr_w_adr, 3, labels=["narrow", "mid", "wide"])
    for t_, s in Dy.groupby("cpr_t", observed=True):
        print(f"    CPR {t_} (width {s.cpr_w_adr.min():.2f}–{s.cpr_w_adr.max():.2f} ADR, N {len(s)}): day range median {s.rng_adr.median():.2f} ADR, trend day {pct((s.close_pos >= 0.8) | (s.close_pos <= 0.2))}, IB broken on one side only {pct(s.ib_up ^ s.ib_dn)}, CPR touched {pct(s.cpr_touched)}")
    print("  range formed per bucket (median, ADR): " + ", ".join(f"{a} {Dy[f'rng_{a}'].median():.2f}" for a, _, _ in BUCKETS) + " | median 5m bar range (tol units): " + ", ".join(f"{a} {Dy[f'vol5_{a}'].median():.2f}" for a, _, _ in BUCKETS))

    print("\n== B. wall touches: hold rate, bounce, undercut (all in tol = 0.1 ADR units; K = 6 bars). 'ctl' = the same walls moved ±0.43 ADR")
    R = T_[T_.set == "real"]; Cc = T_[T_.set != "real"]

    def row(s: pd.DataFrame, name: str) -> str:
        if not len(s):
            return f"  {name:<16} N     0"
        hd = s[~s.broke]; rc = hd.reach.dropna()
        return (f"  {name:<16} N {len(s):>6}  held {100 * (~s.broke).mean():5.1f} %  bounce≥1 {100 * (s.bounce >= 1).mean():4.0f} %  bounce≥2 {100 * (s.bounce >= 2).mean():4.0f} %  "
                f"fwd med {s.fwd.median():+.2f}  | held: undercut p50 {hd.undercut.median():.2f} p75 {hd.undercut.quantile(.75):.2f} p90 {hd.undercut.quantile(.9):.2f}  "
                f"through at all {100 * (hd.undercut > 0).mean():3.0f} %  next wall reached {100 * rc.mean() if len(rc) else float('nan'):3.0f} %")
    print("  by wall kind — each real kind, then its own control:")
    for k in ORDER:
        if len(R[R.kind == k]):
            print(row(R[R.kind == k], k)); print(row(Cc[Cc.kind == k], f"  ctl {k}"))
    print(row(R, "ALL real")); print(row(Cc, "ALL control"))
    for title, key, fmt in (("touch number of the day", np.minimum(T_.touch_no, 3), lambda n: f"{n}{'rd+' if n == 3 else ('st' if n == 1 else 'nd')}"),
                            ("confluence within tol (itself included)", np.minimum(T_.confl, 3), lambda n: f"confl {n}{'+' if n == 3 else ''}"),
                            ("TIGHT confluence within 0.3 tol", np.minimum(T_.confl_t, 3), lambda n: f"tight {n}{'+' if n == 3 else ''}")):
        print(f"  by {title} (real, then control):")
        for n_ in (1, 2, 3):
            print(row(R[key[R.index] == n_], f"real {fmt(n_)}")); print(row(Cc[key[Cc.index] == n_], f"  ctl {fmt(n_)}"))
    print("  by time of the touch:")
    for a, i, j in BUCKETS:
        print(row(R[(R.bar >= i) & (R.bar < j)], f"real {a}")); print(row(Cc[(Cc.bar >= i) & (Cc.bar < j)], f"  ctl {a}"))
    print("  by side and position vs the open:")
    for (sd, ab), s in R.groupby(["side", "above_open"]):
        print(row(s, f"{'sup' if sd == 1 else 'res'} {'above' if ab else 'below'} open"))
    print("  CPR edges (TC/BC/P pooled) by touch number, real then control:")
    for n_ in (1, 2, 3):
        print(row(R[R.kind.isin(["TC", "BC", "P"]) & (np.minimum(R.touch_no, 3) == n_)], f"CPR touch {n_}")); print(row(Cc[Cc.kind.isin(["TC", "BC", "P"]) & (np.minimum(Cc.touch_no, 3) == n_)], f"  ctl touch {n_}"))

    print("\n== C. what a hold pays: enter at the touch bar's close, stop one tol beyond the wall; the next 12 bars (60 min); R = risk in tol")
    def pay(s: pd.DataFrame, name: str) -> str:
        if not len(s):
            return f"  {name:<16} N 0"
        risk = s.c_rel + 1.0
        stopped = s.mae >= risk; twoR = s.mfe >= 2 * risk; oneR = s.mfe >= risk
        return (f"  {name:<16} N {len(s):>6}  risk med {risk.median():.2f} tol  stopped within 60 min {100 * stopped.mean():3.0f} %  reached 1R {100 * oneR.mean():3.0f} %  2R {100 * twoR.mean():3.0f} %  "
                f"mfe med {s.mfe.median():.2f} tol  mae med {s.mae.median():.2f} tol  close+6 med {s.fwd.median():+.2f}")
    for nm, s in (("all touches real", R), ("all touches ctl", Cc), ("held@6 real", R[~R.broke]), ("held@6 ctl", Cc[~Cc.broke])):
        print(pay(s, nm))
    print("  held touches by kind (real):")
    for k in ORDER:
        s = R[(R.kind == k) & ~R.broke]
        if len(s):
            print(pay(s, k))
    print("  held touches by touch number (real then control):")
    for n_ in (1, 2, 3):
        print(pay(R[~R.broke & (np.minimum(R.touch_no, 3) == n_)], f"real {n_}")); print(pay(Cc[~Cc.broke & (np.minimum(Cc.touch_no, 3) == n_)], f"  ctl {n_}"))
    print("  held touches by tight confluence (real then control):")
    for n_ in (1, 2, 3):
        print(pay(R[~R.broke & (np.minimum(R.confl_t, 3) == n_)], f"real tight {n_}")); print(pay(Cc[~Cc.broke & (np.minimum(Cc.confl_t, 3) == n_)], f"  ctl tight {n_}"))
    print("  held touches by time (real then control):")
    for a, i, j in BUCKETS:
        print(pay(R[~R.broke & (R.bar >= i) & (R.bar < j)], f"real {a}")); print(pay(Cc[~Cc.broke & (Cc.bar >= i) & (Cc.bar < j)], f"  ctl {a}"))


if __name__ == "__main__":
    main()
