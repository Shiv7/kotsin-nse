"""The owner's five extra checks (4 Nov 2026 23:45: "why not the time of the retest, what PCR says, all 16 build-up
combinations, monthly pivots, candle patterns — engulfing, doji"), scored on every recorded signal of 3 and 4 Nov 2026
with the book's exit rule, alone and on top of the four-layer gate. Descriptions on two days.
  retest_time  the retest bar's clock: in the opening half-hour (09:15–09:45), the rest of the first hour, after 10:15.
  pcr          the stock's put-call OI ratio at the signal and its change on the day: rising PCR (puts being added)
               counts as with a long, falling as with a short.
  build-ups    futures, calls and puts states (LB / SB / SC / LU each, the 4×4×4): 'full' = futures LB + calls LB +
               puts SB for a long (fresh longs, calls bought, puts written), futures SB + calls SB + puts LB for a short.
  monthly      the previous month's candle → pivot, R1/S1 … and its high/low: is the retested wall within a tolerance
               of a monthly level?
  candle       the retest bar a doji (body ≤ 15 % of range) or a hammer / shooting star the trade's way (far wick ≥ 2×
               body); the confirmation bar engulfing the bar before it in the trade's direction.
Usage: extra_checks.py DAY_JSON [DAY_JSON ...]
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/docs/research/scripts/2026-11-03-level-retest")
from picker_scoreboard import COST_BPS, bars_for, simulate  # noqa: E402

from kotsin_nse.profile.core import bin_size, volume_profile  # noqa: E402
from kotsin_nse.strategy import cpr_retest as cr  # noqa: E402
from kotsin_nse.strategy import wise_gate as wg  # noqa: E402

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def hm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def monthly_levels(sym: str, day: str) -> dict[str, float] | None:
    """The previous calendar month's candle from the 1d history, completed from the archive's bars where the history ends."""
    f = DD / "history" / "1d" / f"{sym}.parquet"
    if not f.exists():
        return None
    d1 = pd.read_parquet(f).sort_values("ts")
    d1["d"] = pd.to_datetime(d1.ts + 19800, unit="s").dt.date
    d1 = d1.drop_duplicates("d", keep="last")
    D = datetime.date.fromisoformat(day)
    first = D.replace(day=1)
    pm_end = first - datetime.timedelta(days=1); pm_start = pm_end.replace(day=1)
    m = d1[(d1.d >= pm_start) & (d1.d <= pm_end)]
    hs, ls, closes = list(m.h), list(m.l), list(zip(m.d, m.c, strict=False))
    x = pm_start
    while x <= pm_end:
        if x > (m.d.max() if len(m) else pm_start - datetime.timedelta(days=1)):
            fa = DD / "archive" / "bars" / f"{x.isoformat()}.parquet"
            if fa.exists():
                ab = pd.read_parquet(fa, columns=["symbol", "ts", "h", "l", "c"]); ab = ab[ab.symbol == sym]
                if len(ab):
                    hs.append(float(ab.h.max())); ls.append(float(ab.l.min())); closes.append((x, float(ab.sort_values("ts").c.iloc[-1])))
        x += datetime.timedelta(days=1)
    if not hs:
        return None
    c = sorted(closes)[-1][1]
    lv = cr.day_levels(max(hs), min(ls), c)
    return {"MP": lv["P"], "MR1": lv["R1"], "MR2": lv["R2"], "MS1": lv["S1"], "MS2": lv["S2"], "MTC": lv["TC"], "MBC": lv["BC"], "PMH": max(hs), "PML": min(ls)}


def candle_flags(bars, retest_hm: str, confirm_hm: str, side: int) -> dict:
    by = {hm(b.ts): b for b in bars}
    out = {"doji": False, "pin": False, "engulf": False}
    r = by.get(retest_hm)
    if r is not None and r.h > r.l:
        body = abs(r.c - r.o); rng = r.h - r.l
        out["doji"] = body <= 0.15 * rng
        far = (min(r.o, r.c) - r.l) if side > 0 else (r.h - max(r.o, r.c))
        out["pin"] = far >= 2 * body and far >= 0.5 * rng
    c = by.get(confirm_hm)
    ts_prev = None
    if c is not None:
        ts_prev = c.ts - 300
        p = next((b for b in bars if b.ts == ts_prev), None)
        if p is not None:
            if side > 0:
                out["engulf"] = c.c > c.o and c.o <= min(p.o, p.c) and c.c >= max(p.o, p.c) and (c.c - c.o) > abs(p.c - p.o)
            else:
                out["engulf"] = c.c < c.o and c.o >= max(p.o, p.c) and c.c <= min(p.o, p.c) and (c.o - c.c) > abs(p.c - p.o)
    return out


def main() -> None:
    rows = []
    for path in sys.argv[1:]:
        snap = json.load(open(path)); day = snap["day"]
        m1 = pd.read_parquet(DD / "archive" / "bars" / f"{day}.parquet", columns=["symbol", "ts", "h", "l", "c", "v"])
        m1["hm"] = [hm(x) for x in m1.ts]; m1 = m1[(m1.hm >= "09:15") & (m1.hm < "15:30")]
        by_sym = {s: g.sort_values("ts") for s, g in m1.groupby("symbol")}
        sigs = sorted([s for s in snap["signals"] if s.get("walls") and s["symbol"] not in INDEX], key=lambda s: (s["ts"], s.get("decided_ts") or 0))
        cache: dict = {}; mcache: dict = {}
        for s in sigs:
            sym = s["symbol"]
            if sym not in cache:
                try:
                    cache[sym] = bars_for(sym, day)
                except Exception:  # noqa: BLE001
                    cache[sym] = []
                mcache[sym] = monthly_levels(sym, day)
            bars = cache[sym]
            b = [x for x in bars if x.ts >= s["ts"] - 300]
            last = max((x.ts for x in bars if (x.ts + 19800) % 86400 <= 15 * 3600 + 10 * 60), default=0)
            sim = simulate(s, b, last)
            if sim is None:
                continue
            side = 1 if s["side"] == "LONG" else -1
            f = s["context"]["fno"]; t = s["tags"]; st = s["stage"]; tol = s["tol"]; adr = s.get("adr") or 0.0
            mm = by_sym.get(sym)
            vp = None
            if mm is not None:
                mm = mm[mm.ts + 60 <= s["ts"]]
                if len(mm) >= wg.MIN_PROFILE_MINUTES:
                    v = volume_profile(list(mm[["h", "l", "v"]].itertuples(index=False, name=None)), bin_size(float(mm.c.iloc[-1]))); vp = {"poc": v.poc, "val": v.val, "vah": v.vah} if v else None
            g = wg.layers(side, s["entry"], tol, adr, with_move=s.get("with_move"), tags=t, fno=f, retest_ext=st.get("lo") if side > 0 else st.get("hi"), vp=vp)
            rt = s["retest_time"]
            pcr_chg = f.get("x_pcr_oi_chg"); pcr = f.get("x_pcr_oi")
            fs, cs, ps = f.get("futures_state"), f.get("calls_state"), f.get("puts_state")
            full = (fs == "LB" and cs == "LB" and ps == "SB") if side > 0 else (fs == "SB" and cs == "SB" and ps == "LB")
            calls_with = (cs in ("LB", "SC")) if side > 0 else (cs in ("SB", "LU"))
            puts_with = (ps in ("SB", "LU")) if side > 0 else (ps in ("LB", "SC"))
            ml = mcache.get(sym) or {}
            lo_, hi_ = st.get("lo"), st.get("hi")
            near_m = [k for k, v in ml.items() if lo_ is not None and hi_ is not None and lo_ - tol <= v <= hi_ + tol]
            cf = candle_flags(bars, rt, s["bar"], side)
            rows.append({"day": day, "sym": sym, "side": s["side"], "time": s["time"], "retest": rt, "gate": g["passed"], "pnl": sim["pnl_bps"], "net": sim["pnl_bps"] - COST_BPS,
                         "rt_open": rt < "09:45", "rt_first_hour": rt < "10:15", "rt_after_ib": rt >= "10:15",
                         "pcr": pcr, "pcr_chg": pcr_chg, "pcr_with": (pcr_chg is not None) and ((pcr_chg > 0) if side > 0 else (pcr_chg < 0)),
                         "pcr_high": (pcr is not None) and ((pcr >= 0.7) if side > 0 else (pcr <= 0.7)),
                         "fut": fs, "calls": cs, "puts": ps, "combo": f"{fs}/{cs}/{ps}", "fno_full": full, "calls_with": calls_with, "puts_with": puts_with, "options_with": calls_with and puts_with,
                         "near_monthly": bool(near_m), "monthly_name": ",".join(near_m), "doji": cf["doji"], "pin": cf["pin"], "engulf": cf["engulf"], "candle_any": cf["doji"] or cf["pin"] or cf["engulf"]})
    D = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    def line(mask, name):
        g, f = D[mask], D[~mask]
        return (f"  {name:44s} pass {len(g):>3}: gross {g.pnl.mean() if len(g) else float('nan'):+6.1f} bps, win {100 * (g.pnl > 0).mean() if len(g) else float('nan'):3.0f} %   | fail {len(f):>3}: {f.pnl.mean() if len(f) else float('nan'):+6.1f}, {100 * (f.pnl > 0).mean() if len(f) else float('nan'):3.0f} %")
    print(f"== {len(D)} recorded signals on {', '.join(sorted(D.day.unique()))}; all: gross {D.pnl.mean():+.1f} bps, win {100 * (D.pnl > 0).mean():.0f} %; the four-layer gate passes {int(D.gate.sum())}: gross {D[D.gate].pnl.mean():+.1f}, win {100 * (D[D.gate].pnl > 0).mean():.0f} %")
    print("\n== each check alone (all signals)")
    for m, nm in ((D.rt_open, "retest in the opening half-hour (09:15-09:45)"), (D.rt_first_hour & ~D.rt_open, "retest 09:45-10:15"), (D.rt_after_ib, "retest after 10:15"),
                  (D.pcr_with, "PCR change with the trade (rising for a long)"), (D.pcr_high, "PCR level ≥ 0.7 long / ≤ 0.7 short"),
                  (D.fno_full, "full build-up: fut+calls+puts all with"), (D.calls_with, "calls with the trade"), (D.puts_with, "puts with the trade"), (D.options_with, "calls and puts with"),
                  (D.near_monthly, "retested wall on a monthly level"), (D.doji, "retest bar a doji"), (D.pin, "retest bar a hammer/star the trade's way"), (D.engulf, "confirmation bar engulfing"), (D.candle_any, "any of the three candle patterns")):
        print(line(m, nm))
    print("\n== on top of the four-layer gate")
    G = D[D.gate]
    for m, nm in ((G.rt_open, "retest in the opening half-hour"), (G.rt_after_ib, "retest after 10:15"), (G.pcr_with, "PCR change with"), (G.fno_full, "full build-up"), (G.options_with, "calls and puts with"), (G.near_monthly, "on a monthly level"), (G.candle_any, "any candle pattern"), (G.engulf, "engulfing confirmation"), (G.doji | G.pin, "doji or hammer retest")):
        g, f = G[m], G[~m]
        print(f"  {nm:40s} gate+yes {len(g):>2}: gross {g.pnl.mean() if len(g) else float('nan'):+6.1f}, win {100 * (g.pnl > 0).mean() if len(g) else float('nan'):3.0f} %  | gate+no {len(f):>2}: {f.pnl.mean() if len(f) else float('nan'):+6.1f}, {100 * (f.pnl > 0).mean() if len(f) else float('nan'):3.0f} %")
    print("\n== the build-up combinations (futures/calls/puts), signals with ≥ 8, by side")
    for (sd, cb), g in D.groupby(["side", "combo"]):
        if len(g) >= 8:
            print(f"  {sd:5s} {cb:10s} N {len(g):>3}  gross {g.pnl.mean():+6.1f}  win {100 * (g.pnl > 0).mean():3.0f} %")
    print("\n== by the retest's half-hour (all signals)")
    D["rt_hh"] = D.retest.str[:2] + ":" + np.where(D.retest.str[3:] < "30", "00", "30")
    for k, g in D.groupby("rt_hh"):
        print(f"  {k}  N {len(g):>3}  gross {g.pnl.mean():+6.1f}  win {100 * (g.pnl > 0).mean():3.0f} %")
    D.to_csv(Path(sys.argv[1]).parent.parent.parent.parent / "docs" / "research" / "scripts" / "2026-11-04-wise-gate" / "extra-checks.csv", index=False) if False else D.to_csv("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/docs/research/scripts/2026-11-04-wise-gate/extra-checks.csv", index=False)


if __name__ == "__main__":
    main()
