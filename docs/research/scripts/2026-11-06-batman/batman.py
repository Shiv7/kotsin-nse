"""The "Batman" double ratio spread from the owner's pasted video transcript (6 Nov 2026), tested on NIFTY with the daily
F&O bhavcopies (2024-01-01 → 2026-11-05), settlement closes, no bid-ask, no charges.

The rule as the video states it: with 40–50 days to a monthly expiry, on the put side sell two lots at ~16 delta and
buy one lot at ~20 delta; the same on the call side; no adjustment; the stop is a mark-to-market loss equal to the
profit at the centre (the net credit, "4 % of margin"); otherwise hold to expiry. Pre-registered here: entry on the
first session with ≤ 45 days to that expiry (the middle of the video's window; 50 and 40 as sensitivity), deltas from
Black-76 on that expiry's future at the entry close, the stop checked at each session's close (the video cuts intraday
or at the next open, which a close-only test cannot do: a gap that blows through the stop lands at the close, a worse
fill than his), the expiry P&L from the expiry day's settlement closes. One trade per monthly expiry.
Usage: batman.py OUT_DIR [MAX_DTE]"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from kotsin_nse.fno.chain import greeks76, implied_vol

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
LOT = 65
MARGIN_INR = 224_000.0  # the video's figure for the structure, one lot per leg unit


def load(day: str) -> pd.DataFrame:
    t = pq.read_table(DD / "oi_daily" / f"{day}.parquet", columns=["symbol", "kind", "expiry", "strike", "option_type", "close"],
                      filters=[("symbol", "==", "NIFTY")]).to_pandas()
    t["expiry"] = t["expiry"].astype(str)
    return t


def t_years(expiry: str, day: str) -> float:
    return max((datetime.date.fromisoformat(expiry) - datetime.date.fromisoformat(day)).days, 0.5) / 365.0


def legs_value(t: pd.DataFrame, expiry: str, legs: dict[str, float]) -> float | None:
    """What it costs to close: + for the sold legs' closes (×2), − for the bought legs."""
    o = t[(t["kind"] == "OPTIDX") & (t["expiry"] == expiry)]
    v = 0.0
    for name, k in legs.items():
        kind = "PE" if name.startswith("p") else "CE"
        r = o[(o["strike"] == k) & (o["option_type"] == kind)]["close"]
        if r.empty:
            return None
        px = float(r.iloc[0])
        v += (2 * px if name.endswith("sell") else -px)
    return v


def main() -> None:
    out = Path(sys.argv[1])
    max_dte = int(sys.argv[2]) if len(sys.argv) > 2 else 45
    days = sorted(p.stem for p in (DD / "oi_daily").glob("2*.parquet"))
    cache: dict[str, pd.DataFrame] = {}

    def get(day: str) -> pd.DataFrame:
        if day not in cache:
            if len(cache) > 80:
                cache.pop(next(iter(cache)))
            cache[day] = load(day)
        return cache[day]

    # the monthly expiries: the last NIFTY option expiry in each calendar month
    exp_all = set()
    for d in days[::5]:
        exp_all |= set(get(d)[get(d)["kind"] == "OPTIDX"]["expiry"].unique())
    by_month: dict[str, str] = {}
    for e in sorted(exp_all):
        by_month[e[:7]] = e
    monthlies = sorted(by_month.values())
    rows = []
    for E in monthlies:
        cands = [d for d in days if 30 <= (datetime.date.fromisoformat(E) - datetime.date.fromisoformat(d)).days <= max_dte]
        if not cands or E > days[-1]:
            continue
        d0 = cands[0]
        t0 = get(d0)
        f = t0[(t0["kind"] == "FUTIDX") & (t0["expiry"] == E)]["close"]
        if f.empty:
            f = t0[t0["kind"] == "FUTIDX"].sort_values("expiry")["close"]
        if f.empty:
            continue
        F = float(f.iloc[0])
        o = t0[(t0["kind"] == "OPTIDX") & (t0["expiry"] == E)]
        tt = t_years(E, d0)
        puts = o[(o["option_type"] == "PE") & (o["strike"] < F)].sort_values("strike")
        calls = o[(o["option_type"] == "CE") & (o["strike"] > F)].sort_values("strike")

        def pick(side: pd.DataFrame, call: bool, target: float) -> float | None:
            best, bk = 9.0, None
            for r in side.itertuples():
                iv = implied_vol(float(r.close), F, float(r.strike), tt, call)
                if iv is None:
                    continue
                g = greeks76(F, float(r.strike), tt, iv, call)
                if g is None:
                    continue
                dl = abs(g["delta"])
                if abs(dl - target) < best:
                    best, bk = abs(dl - target), float(r.strike)
            return bk

        legs = {"p_sell": pick(puts, False, 0.16), "p_buy": pick(puts, False, 0.20), "c_sell": pick(calls, True, 0.16), "c_buy": pick(calls, True, 0.20)}
        if any(v is None for v in legs.values()):
            continue
        ks = sorted(float(k) for k in o["strike"].unique())
        if legs["p_buy"] <= legs["p_sell"]:  # the bought put must sit nearer the money than the sold one
            i = ks.index(legs["p_sell"])
            legs["p_buy"] = ks[i + 1] if i + 1 < len(ks) else legs["p_sell"]
        if legs["c_buy"] >= legs["c_sell"]:
            i = ks.index(legs["c_sell"])
            legs["c_buy"] = ks[i - 1] if i > 0 else legs["c_sell"]
        credit = legs_value(t0, E, legs)
        if credit is None:
            continue
        path = []
        exit_day, how, pnl = None, "expiry", None
        for d in [x for x in days if d0 < x <= E]:
            v = legs_value(get(d), E, legs)
            if v is None:
                continue
            p = credit - v
            path.append((d, round(p, 1)))
            if p <= -credit and how == "expiry":
                exit_day, how, pnl = d, "stop", p
                break
        if how == "expiry" and path:
            exit_day, pnl = path[-1]
        if pnl is None:
            continue
        fut_e = get(exit_day)
        fe = fut_e[(fut_e["kind"] == "FUTIDX")].sort_values("expiry")["close"]
        rows.append({"expiry": E, "entry": d0, "dte": (datetime.date.fromisoformat(E) - datetime.date.fromisoformat(d0)).days, "F": F,
                     "p_buy": legs["p_buy"], "p_sell": legs["p_sell"], "c_sell": legs["c_sell"], "c_buy": legs["c_buy"],
                     "width_pct": round((legs["c_sell"] / F - 1) * 100, 1), "credit": round(credit, 1), "exit": exit_day, "how": how,
                     "pnl": round(pnl, 1), "min_pnl": round(min(p for _, p in path), 1), "max_pnl": round(max(p for _, p in path), 1),
                     "expiry_pnl_no_stop": round(path[-1][1], 1) if how == "expiry" else round(credit - (legs_value(get(E), E, legs) or 0), 1),
                     "F_exit": round(float(fe.iloc[0]), 1) if len(fe) else None})
    D = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    D.to_csv(out / f"batman-trades-dte{max_dte}.csv", index=False)
    print(f"== NIFTY Batman (16/20 delta, 2:1 each side), entry on the first session with ≤ {max_dte} days to the monthly expiry, stop = −credit at a close; {len(D)} cycles")
    print(D.to_string(index=False))
    pts = D["pnl"]
    print(f"\n   with the stop: sum {pts.sum():+.0f} points, mean {pts.mean():+.1f}, wins {(pts > 0).sum()} / {len(D)}, worst {pts.min():+.0f}, stops {(D.how == 'stop').sum()}")
    ns = D["expiry_pnl_no_stop"]
    print(f"   held to expiry, no stop: sum {ns.sum():+.0f}, mean {ns.mean():+.1f}, wins {(ns > 0).sum()} / {len(D)}, worst {ns.min():+.0f}")
    print(f"   the credit (the centre profit): mean {D.credit.mean():.0f} points = ₹{D.credit.mean() * LOT:,.0f} a lot of {LOT}, {D.credit.mean() * LOT / MARGIN_INR * 100:.1f} % of the video's ₹{MARGIN_INR:,.0f} margin")
    print(f"   per cycle in ₹ at lot {LOT}: mean {pts.mean() * LOT:+,.0f}, sum {pts.sum() * LOT:+,.0f}; worst cycle {pts.min() * LOT:+,.0f}; the worst mark-to-market inside a cycle {D.min_pnl.min() * LOT:+,.0f}")
    print(f"   the sold strikes sat {D.width_pct.mean():.1f} % from the future on average; the future moved past a sold strike in {(((D.F_exit > D.c_sell) | (D.F_exit < D.p_sell))).sum()} cycles")


if __name__ == "__main__":
    main()
