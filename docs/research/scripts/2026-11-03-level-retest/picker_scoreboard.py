"""The picker scoreboard — the owner's "learning curve to get best trades" (3 Nov 2026): the shadow book takes two trades a
day; every confirmed flip is recorded as a signal. This script simulates EVERY recorded signal's trade on the day's 5m bars
(the live exit rule, wall_then_trail, from the signal's entry, stop and walls) and scores selection rules that pick two of
them without hindsight, against the best possible two and a random two.
Usage: uv run python picker_scoreboard.py DAY_JSON [DAY_JSON ...]   (data-live/strategy/<day>.json, or a replay's json)
Bars come from the running engine's API (GET /api/bars/<sym>?tf=5m); set BARS_API to change the host.
"""
from __future__ import annotations

import json
import os
import random
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.structure.engine import Bar5

API = os.environ.get("BARS_API", "http://localhost:8500")
COST_BPS = 13.0


DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
_ARCH: dict[str, dict[str, list[Bar5]]] = {}


def _archive_5m(day: str) -> dict[str, list[Bar5]]:
    """The day's 5m bars from the feed's 1m archive — the fallback when the engine's store no longer holds the day
    (a restart empties the intraday series; the API serves the day itself in the evening)."""
    if day not in _ARCH:
        a = pd.read_parquet(DD / "archive" / "bars" / f"{day}.parquet")
        a = a[a["source"] != "partial"].copy()
        a["b"] = (a["ts"] // 300) * 300
        g = a.sort_values("ts").groupby(["symbol", "b"]).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
        _ARCH[day] = {sym: [Bar5(int(r.b), r.o, r.h, r.l, r.c, r.v) for r in d.itertuples()] for sym, d in g.groupby("symbol")}
    return _ARCH[day]


def bars_for(sym: str, day: str) -> list[Bar5]:
    try:
        with urllib.request.urlopen(f"{API}/api/bars/{sym}?tf=5m&day={day}", timeout=30) as r:
            rows = json.load(r)["bars"]
        if rows:
            return [Bar5(int(b["ts"]), b["o"], b["h"], b["l"], b["c"], b["v"]) for b in rows]
    except Exception:  # noqa: BLE001 - the archive is the fallback
        pass
    return _archive_5m(day).get(sym, [])


def simulate(sig: dict, bars: list[Bar5], last_ts: int) -> dict | None:
    side = 1 if sig["side"] == "LONG" else -1
    entry, stop = sig["entry"], sig["stop"]
    walls = [w["price"] for w in sig.get("walls") or []]
    if not walls or (entry - stop) * side <= 0:
        return None
    plan = cr.ExitPlan.open("wall_then_trail", side, entry, stop, walls, sig.get("tol") or 0.0)
    after = [b for b in bars if b.ts >= sig["ts"] and b.ts <= last_ts]
    res = None
    for i, b in enumerate(after):
        res = plan.step(b.h, b.l, b.c, last=(i == len(after) - 1), open=b.o)
        if res is not None:
            break
    if res is None:
        return None
    return {"pnl_bps": plan.pnl_bps(res[0]), "how": res[1], "bars": plan.bars}


def main() -> None:
    rng = random.Random(3)
    table = []
    for path in sys.argv[1:]:
        snap = json.load(open(path))
        day = snap["day"]
        sigs = [s for s in snap["signals"] if s.get("symbol") and s.get("walls") and s["symbol"] not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")]
        sigs.sort(key=lambda s: (s["ts"], s.get("decided_ts") or 0))
        cache: dict[str, list[Bar5]] = {}
        rows = []
        for s in sigs:
            if s["symbol"] not in cache:
                try:
                    cache[s["symbol"]] = bars_for(s["symbol"], day)
                except Exception:  # noqa: BLE001
                    cache[s["symbol"]] = []
            b = [x for x in cache[s["symbol"]] if x.ts >= s["ts"] - 300]
            # the session's last decision bar is the 15:10 bar: 15:15 onward is the auction
            last_ts = max((x.ts for x in cache[s["symbol"]] if (x.ts + 19800) % 86400 <= 15 * 3600 + 10 * 60), default=0)
            sim = simulate(s, b, last_ts)
            if sim is None:
                continue
            ctx = (s.get("context") or {}).get("fno") or {}
            side = 1 if s["side"] == "LONG" else -1
            rows.append({"ts": s["ts"], "sym": s["symbol"], "side": s["side"], "wall": s.get("wall"), "wtype": s.get("wtype"), "risk_bps": s["risk_bps"],
                         "room_R": (s["walls"][0]["bps"] / s["risk_bps"]) if s["risk_bps"] else np.nan, "taken": bool(s.get("taken")),
                         "board_with": (ctx.get("score") or 0) * side, "fut_with": ctx.get("futures_state") == ("LB" if side > 0 else "SB"),
                         "move_adr": (s.get("tags") or {}).get("move_adr"), "pnl_bps": sim["pnl_bps"], "net_bps": sim["pnl_bps"] - COST_BPS, "how": sim["how"]})
        D = pd.DataFrame(rows)
        if D.empty:
            print(f"{day}: no simulable signals")
            continue
        pickers = {
            "first two (the live book)": D.head(2),
            "first two with risk ≥ 80 bps": D[D.risk_bps >= 80].head(2),
            "first two with the first wall ≤ 0.5R": D[D.room_R <= 0.5].head(2),
            "first two with the board with the trade (score ≥ 2)": D[D.board_with >= 2].head(2),
            "first two with the future's state with the trade": D[D.fut_with].head(2),
            "first two longs": D[D.side == "LONG"].head(2),
            "first two after 10:15": D[(D.ts + 19800) % 86400 >= 10 * 3600 + 15 * 60].head(2),
            "best possible two (hindsight)": D.nlargest(2, "pnl_bps"),
        }
        rand = [D.sample(2, random_state=rng.randint(0, 10**6)).net_bps.sum() for _ in range(200)] if len(D) >= 2 else [0]
        print(f"\n== {day}: {len(D)} simulable signals (booked {int(D.taken.sum())}), all-signal mean {D.pnl_bps.mean():+.1f} bps, win {100 * (D.pnl_bps > 0).mean():.0f} %")
        for name, g in pickers.items():
            if len(g) == 0:
                print(f"  {name:52s} —")
                continue
            picks = ", ".join(f"{r.sym} {r.side[0]} {r.pnl_bps:+.0f}" for r in g.itertuples())
            print(f"  {name:52s} net {g.net_bps.sum():+7.1f} bps  ({picks})")
            table.append({"day": day, "picker": name, "net_bps": g.net_bps.sum(), "n": len(g)})
        print(f"  {'random two (mean of 200 draws)':52s} net {np.mean(rand):+7.1f} bps  (p90 {np.percentile(rand, 90):+.0f})")
        table.append({"day": day, "picker": "random two", "net_bps": float(np.mean(rand)), "n": 2})
    if table:
        T = pd.DataFrame(table)
        print("\n== pickers across the days given (net bps of the two trades, summed)")
        print(T.pivot_table(index="picker", columns="day", values="net_bps", aggfunc="sum").assign(total=lambda x: x.sum(axis=1)).sort_values("total", ascending=False).round(1).to_string())


if __name__ == "__main__":
    main()
