"""The owner's "wise trading" gate (4 Nov 2026 22:40: "don't trade until you have equity, future, option, market profile"):
a recorded signal is taken only when four layers say the same thing as the trade, scored on EVERY signal the engine
recorded (data-live/strategy/<day>.json) with the book's own exit rule on the archive's bars (picker_scoreboard.simulate).
Layers, all judged at the signal's decision bar, nothing after it:
  equity   the confirmed retest itself, on the move's side of the open (with_move) and with the 1H supertrend (st60_al).
  future   the stock's futures state with the trade: long build-up for a long, short build-up for a short (strict);
           'loose' also accepts short covering for a long and long unwinding for a short.
  option   the F&O board's bias with the trade (Bull for a long, Bear for a short) and the option wall on the trade's side
           at least one ADR away (room).
  profile  the day's developing volume profile from the 1m bars up to the decision bar (bins of half a tolerance, value
           area 70 %): the entry on the trade's side of the POC, and the retest's extreme not through the far edge of
           the value area by more than a tolerance (the pullback held value). Signals before 09:45 have no profile: fail.
Two full days carry every signal with context (3 and 4 Nov 2026; 2 Nov has three). Descriptions, not a test.
Usage: wise_gate.py OUT_DIR DAY_JSON [DAY_JSON ...]
"""
from __future__ import annotations

import datetime
import json
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/docs/research/scripts/2026-11-03-level-retest")
from picker_scoreboard import COST_BPS, _archive_5m, bars_for, simulate  # noqa: E402

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def hm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def profile(m1: pd.DataFrame, upto_ts: int, width: float) -> tuple[float, float, float] | None:
    d = m1[m1.ts < upto_ts]
    if len(d) < 30 or width <= 0 or d.v.sum() <= 0:
        return None
    b = (d.c // width) * width
    vp = d.groupby(b).v.sum().sort_values(ascending=False)
    poc = float(vp.index[0])
    acc, tot, lo, hi = 0.0, vp.sum(), poc, poc
    for px, v in vp.items():
        acc += v; lo, hi = min(lo, float(px)), max(hi, float(px))
        if acc >= 0.7 * tot:
            break
    return poc, lo, hi + width


def main() -> None:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(3)
    allrows = []
    for path in sys.argv[2:]:
        snap = json.load(open(path)); day = snap["day"]
        m1 = pd.read_parquet(DD / "archive" / "bars" / f"{day}.parquet", columns=["symbol", "ts", "c", "v"])
        m1["hm"] = [hm(x) for x in m1.ts]; m1 = m1[(m1.hm >= "09:15") & (m1.hm < "15:30")]
        by_sym = {s: g.sort_values("ts") for s, g in m1.groupby("symbol")}
        sigs = [s for s in snap["signals"] if s.get("symbol") and s.get("walls") and s["symbol"] not in INDEX]
        sigs.sort(key=lambda s: (s["ts"], s.get("decided_ts") or 0))
        cache: dict[str, list] = {}
        rows = []
        for s in sigs:
            sym = s["symbol"]
            if sym not in cache:
                try:
                    cache[sym] = bars_for(sym, day)
                except Exception:  # noqa: BLE001
                    cache[sym] = []
            b = [x for x in cache[sym] if x.ts >= s["ts"] - 300]
            last_ts = max((x.ts for x in cache[sym] if (x.ts + 19800) % 86400 <= 15 * 3600 + 10 * 60), default=0)
            sim = simulate(s, b, last_ts)
            if sim is None:
                continue
            side = 1 if s["side"] == "LONG" else -1
            fno = (s.get("context") or {}).get("fno") or {}
            tags = s.get("tags") or {}
            st = s.get("stage") or {}
            adr = s.get("adr") or 0.0; tol = s.get("tol") or 0.0; entry = s["entry"]
            fut = fno.get("futures_state")
            bias = str(fno.get("bias") or "")
            cw, pw = fno.get("x_call_wall"), fno.get("x_put_wall")
            room = ((cw - entry) if side > 0 and cw else (entry - pw) if side < 0 and pw else np.nan)
            eq = bool(s.get("with_move")) and bool(tags.get("st60_al"))
            fut_strict = fut == ("LB" if side > 0 else "SB")
            fut_loose = fut in (("LB", "SC") if side > 0 else ("SB", "LU"))
            opt = (("Bull" in bias) if side > 0 else ("Bear" in bias)) and (room == room and room >= adr)
            pr = profile(by_sym.get(sym, pd.DataFrame(columns=["ts", "c", "v"])), s["ts"], tol / 2) if sym in by_sym else None
            if pr is None:
                poc_with = va_held = False; poc = np.nan
            else:
                poc, val, vah = pr
                poc_with = (entry - poc) * side > 0
                ext = st.get("lo") if side > 0 else st.get("hi")
                va_held = (ext is not None) and ((ext >= val - tol) if side > 0 else (ext <= vah + tol))
            prof = bool(poc_with and va_held)
            layers = int(eq) + int(fut_strict) + int(opt) + int(prof)
            rows.append({"day": day, "ts": s["ts"], "time": hm(s["ts"]), "sym": sym, "side": s["side"], "wall": s.get("wall"), "entry": entry, "stop": s["stop"], "risk_bps": s["risk_bps"],
                         "taken": bool(s.get("taken")), "eq": eq, "fut": fut_strict, "fut_loose": fut_loose, "opt": opt, "prof": prof, "poc_with": poc_with, "va_held": va_held,
                         "layers": layers, "gate": layers == 4, "gate_loose": eq and fut_loose and opt and prof, "owner_stack": bool(tags.get("owner_stack")),
                         "fut_state": fut, "bias": bias, "poc": poc, "pnl_bps": sim["pnl_bps"], "net_bps": sim["pnl_bps"] - COST_BPS, "how": sim["how"], "bars": sim["bars"]})
        D = pd.DataFrame(rows)
        if D.empty:
            print(f"{day}: nothing simulable"); continue
        allrows.append(D)
        print(f"\n== {day}: {len(D)} simulable signals, all-signal mean {D.pnl_bps.mean():+.1f} bps gross, win {100 * (D.pnl_bps > 0).mean():.0f} %")
        for k, nm in (("eq", "equity: with the move and the 1H"), ("fut", "future: build-up with the trade (strict)"), ("fut_loose", "future: strict or covering/unwinding"), ("opt", "option: board bias with the trade and room ≥ 1 ADR"), ("prof", "profile: on the POC's side and the pullback held value"), ("owner_stack", "the engine's owner_stack tag")):
            g = D[D[k]]
            print(f"  {nm:58s} pass {len(g):>3} of {len(D):>3}  mean gross {g.pnl_bps.mean() if len(g) else float('nan'):+6.1f} bps  win {100 * (g.pnl_bps > 0).mean() if len(g) else float('nan'):3.0f} %   | fail: mean {D[~D[k]].pnl_bps.mean():+6.1f}, win {100 * (D[~D[k]].pnl_bps > 0).mean():3.0f} %")
        print("  by the number of layers agreeing:")
        for k, g in D.groupby("layers"):
            print(f"    {k} of 4: N {len(g):>3}  mean gross {g.pnl_bps.mean():+6.1f} bps  net {g.net_bps.mean():+6.1f}  win {100 * (g.pnl_bps > 0).mean():3.0f} %  median {g.pnl_bps.median():+5.0f}")
        G = D[D.gate]
        print(f"  ALL FOUR agree: {len(G)} signals" + (f", mean gross {G.pnl_bps.mean():+.1f} bps, net {G.net_bps.mean():+.1f}, win {100 * (G.pnl_bps > 0).mean():.0f} %, sum net {G.net_bps.sum():+.0f} bps" if len(G) else ""))
        if len(G):
            print(G[["time", "sym", "side", "wall", "entry", "stop", "risk_bps", "fut_state", "bias", "poc", "how", "bars", "pnl_bps", "net_bps"]].round(1).to_string(index=False))
        GL = D[D.gate_loose]
        print(f"  all four, loose future: {len(GL)} signals" + (f", mean gross {GL.pnl_bps.mean():+.1f}, win {100 * (GL.pnl_bps > 0).mean():.0f} %, sum net {GL.net_bps.sum():+.0f}" if len(GL) else ""))
        rand = [D.sample(2, random_state=rng.randint(0, 10**6)).net_bps.sum() for _ in range(200)] if len(D) >= 2 else [0]
        pick = {"the live book (first two)": D[D.taken].head(2), "first two with all four layers": G.head(2), "first two with all four, loose future": GL.head(2), "first two with ≥ 3 layers": D[D.layers >= 3].head(2), "best possible two (hindsight)": D.nlargest(2, "pnl_bps")}
        for nm, g in pick.items():
            print(f"  {nm:42s} " + (f"net {g.net_bps.sum():+7.1f} bps  (" + ", ".join(f"{r.sym} {r.side[0]} {r.pnl_bps:+.0f}" for r in g.itertuples()) + ")" if len(g) else "—"))
        print(f"  {'random two (mean of 200 draws)':42s} net {np.mean(rand):+7.1f} bps")
    A = pd.concat(allrows); A.to_csv(out / "signals-gated.csv", index=False)
    print(f"\n== all days pooled: {len(A)} signals")
    for k, g in A.groupby("layers"):
        print(f"    {k} of 4: N {len(g):>3}  mean gross {g.pnl_bps.mean():+6.1f} bps  net {g.net_bps.mean():+6.1f}  win {100 * (g.pnl_bps > 0).mean():3.0f} %")
    for k, nm in (("eq", "equity"), ("fut", "future strict"), ("opt", "option"), ("prof", "profile"), ("gate", "ALL FOUR"), ("gate_loose", "all four loose")):
        g = A[A[k]]; f = A[~A[k]]
        print(f"  {nm:16s} pass {len(g):>3}: mean gross {g.pnl_bps.mean() if len(g) else float('nan'):+6.1f}, win {100 * (g.pnl_bps > 0).mean() if len(g) else float('nan'):3.0f} %  | fail {len(f):>3}: {f.pnl_bps.mean():+6.1f}, {100 * (f.pnl_bps > 0).mean():3.0f} %")


if __name__ == "__main__":
    main()
