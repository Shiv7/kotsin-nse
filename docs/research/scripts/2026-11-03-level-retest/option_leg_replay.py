"""The shadow option leg on the archived sessions: replay each day through the engine's own runner in level mode (1d/30m
from the running engine's API, the day's 5m bars from the feed's 1m archive), then price one lot of the nearest ATM option
of the nearest expiry after the day (CE for a long, PE for a short) from the day's option ticks — bought at the first ask
at or after the entry (within five minutes), sold at the last bid at or before the exit — with charges from the cost model.
Usage: uv run python option_leg_replay.py OUT_DIR 2026-10-27 2026-10-29 2026-10-30 2026-11-02
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OrderSide
from kotsin_nse.exec.gateway import Gateway, Mode
from kotsin_nse.exec.paper import PaperMatcher
from kotsin_nse.market.session import session_close_ts, session_open_ts
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy.runner import ShadowRunner

API = "http://localhost:8500"
DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
MASTER = DD / "scripmaster" / "nse_fo-2026-11-02.csv"
SETTINGS = {"strategy_sequence": "level", "strategy_walls_round": True, "strategy_max_move_adr": 0, "strategy_notional_inr": 100000}


def get(sym: str, tf: str, n: int):
    with urllib.request.urlopen(f"{API}/api/bars/{sym}?tf={tf}&n={n}", timeout=30) as r:
        return json.load(r)["bars"]


def ubar(inst: Instrument, tf: str, b: dict) -> UnifiedBar:
    return UnifiedBar(symbol=inst.symbol, scrip_code=inst.scrip_code, tf=tf, ts=int(b["ts"]), open=b["o"], high=b["h"], low=b["l"], close=b["c"],
                      volume=b["v"], source=BarSource.REST if b.get("source") == "rest" else BarSource.LIVE, complete=True)


def archive_5m(day: str) -> dict[str, list[dict]]:
    a = pd.read_parquet(DD / "archive" / "bars" / f"{day}.parquet")
    a = a[a["source"] != "partial"].copy()
    a["b"] = (a["ts"] // 300) * 300
    g = a.sort_values("ts").groupby(["symbol", "b"]).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
    return {sym: [{"ts": int(r.b), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v, "source": "live"} for r in d.itertuples()] for sym, d in g.groupby("symbol")}


async def replay(day: str, out: Path) -> dict:
    d = date.fromisoformat(day)
    OPEN, CLOSE = session_open_ts(Segment.NSE_EQ, d), session_close_ts(Segment.NSE_EQ, d)
    codes = pd.read_parquet(DD / "archive" / "bars" / f"{day}.parquet", columns=["symbol", "scrip_code"]).drop_duplicates("symbol")
    insts = {r.symbol: Instrument(scrip_code=str(r.scrip_code), symbol=r.symbol, segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY) for r in codes.itertuples()}
    store = BarStore()
    today: list[UnifiedBar] = []
    arch = archive_5m(day)
    for sym, inst in insts.items():
        try:
            d1, m30 = get(sym, "1d", 80), get(sym, "30m", 160)
        except Exception:  # noqa: BLE001
            continue
        store.seed(sym, "1d", [ubar(inst, "1d", b) for b in d1 if b["ts"] < OPEN])
        store.seed(sym, "30m", [ubar(inst, "30m", b) for b in m30 if b["ts"] < OPEN])
        today += [ubar(inst, "5m", b) for b in arch.get(sym, []) if OPEN <= b["ts"] < CLOSE]
    today.sort(key=lambda b: (b.ts, b.symbol))
    s = Settings(_env_file=None, data_dir=out / "data", engine_enabled=False, strategy_pdc="last", **SETTINGS)
    costs = CostModel(s)
    gw = Gateway(matcher=PaperMatcher(costs), mode=lambda: Mode.SHADOW, halted=lambda: (False, ""), book_for=lambda _c: None, ltp_for=lambda _c: None)
    tasks: list[asyncio.Task] = []

    def spawn(coro, name):
        t = asyncio.create_task(coro, name=name)
        tasks.append(t)
        return t

    r = ShadowRunner(s, store=store, gateway=gw, ledger=None, costs=costs, underlyings=insts, spawn=spawn)
    for b in today:
        store.close(b)
        await r.on_bar(b)
    await asyncio.gather(*tasks)
    snap = r.snapshot()
    (out / f"{day}.json").write_text(json.dumps(snap, default=str))
    return snap


def pick(master: pd.DataFrame, symbol: str, side: int, price: float, day: str) -> pd.Series | None:
    m = master[(master.SymbolRoot == symbol) & (master.ScripType == ("CE" if side > 0 else "PE")) & (master.Expiry > day)]
    if m.empty:
        return None
    e = m.Expiry.min()
    m = m[m.Expiry == e]
    return m.iloc[int(np.argmin((m.StrikeRate - price).abs().to_numpy()))]


def price_at(tk: pd.DataFrame, ts: float, side: str) -> tuple[float | None, float | None, str]:
    """``side`` ask: the first frame at/after ts within 300 s, else the last before; bid: the last frame at/before ts."""
    if side == "ask":
        after = tk[(tk.ts >= ts) & (tk.ts <= ts + 300)]
        if len(after):
            f = after.iloc[0]
            return (float(f.ask) if f.ask > 0 else float(f.ltp)), float(f.ts - ts), "ask" if f.ask > 0 else "ltp"
        before = tk[tk.ts < ts]
        if len(before):
            f = before.iloc[-1]
            return float(f.ltp), float(f.ts - ts), "stale_ltp"
        return None, None, "none"
    before = tk[tk.ts <= ts]
    if len(before):
        f = before.iloc[-1]
        return (float(f.bid) if f.bid > 0 else float(f.ltp)), float(f.ts - ts), "bid" if f.bid > 0 else "ltp"
    return None, None, "none"


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    days = sys.argv[2:]
    master = pd.read_csv(MASTER, dtype={"ScripCode": str}, low_memory=False)
    master = master[master.ScripType.isin(["CE", "PE"])]
    costs = CostModel(Settings(_env_file=None, data_dir=out / "data", engine_enabled=False))
    rows: list[dict] = []
    for day in days:
        snap = asyncio.run(replay(day, out))
        trades = snap["trades"]
        picks = {}
        for t in trades:
            side = 1 if t["side"] == "LONG" else -1
            p = pick(master, t["symbol"], side, t["entry"], day)
            if p is not None:
                picks[t["id"]] = p
        need = {str(p.ScripCode) for p in picks.values()}
        tk = pd.read_parquet(DD / "archive" / "ticks" / f"{day}.parquet", columns=["scrip_code", "ts", "ltp", "bid", "ask"])
        tk = tk[tk.scrip_code.isin(need)].sort_values("ts")
        by_code = {c: g.reset_index(drop=True) for c, g in tk.groupby("scrip_code")}
        n_leg = 0
        for t in trades:
            p = picks.get(t["id"])
            row = {"day": day, "sym": t["symbol"], "side": t["side"], "wall": t.get("wall"), "entry_bar": t["entry_bar"], "stock_bps": t["pnl_bps"], "stock_net_inr": t["net_inr"],
                   "exit_reason": t["exit_reason"], "bars_held": t["bars_held"]}
            if p is None:
                rows.append({**row, "status": "no_contract"})
                continue
            g = by_code.get(str(p.ScripCode))
            if g is None or g.empty:
                rows.append({**row, "status": "no_ticks", "strike": float(p.StrikeRate), "expiry": p.Expiry})
                continue
            e_px, e_age, e_how = price_at(g, t["opened_ts"], "ask")
            x_px, x_age, x_how = price_at(g, t["closed_ts"], "bid")
            if not e_px or not x_px:
                rows.append({**row, "status": "no_quote", "strike": float(p.StrikeRate), "expiry": p.Expiry})
                continue
            lot = int(p.LotSize)
            inst = Instrument(scrip_code=str(p.ScripCode), symbol=p.Name, segment=Segment.NSE_FO, kind=InstrumentKind.OPTION, lot_size=lot, expiry=p.Expiry, strike=float(p.StrikeRate), underlying=t["symbol"])
            ch = costs.round_trip(inst, e_px, x_px, lot, entry_side=OrderSide.BUY).total
            gross = (x_px - e_px) * lot
            n_leg += 1
            rows.append({**row, "status": "ok", "code": str(p.ScripCode), "name": p.Name, "strike": float(p.StrikeRate), "expiry": p.Expiry, "lot": lot,
                         "opt_entry": e_px, "opt_exit": x_px, "entry_age_s": e_age, "exit_age_s": x_age, "entry_how": e_how, "exit_how": x_how,
                         "frames": len(g), "pct": (x_px / e_px - 1) * 100, "gross_inr": gross, "charges_inr": ch, "net_inr": gross - ch,
                         "premium_inr": e_px * lot, "leverage": ((x_px / e_px - 1) * 100) / (t["pnl_bps"] / 100) if abs(t["pnl_bps"]) >= 1 else np.nan})
        sm = snap["summary"]
        print(f"{day}: stock trades {len(trades)} avg {sm['avg_pnl_bps']} bps net ₹{sm['sum_net_inr']:.0f} | option legs priced {n_leg}", file=sys.stderr)
    df = pd.DataFrame(rows)
    df.to_parquet(out / "option_legs.parquet")
    ok = df[df.status == "ok"]
    print("\n== the option leg, one lot per booked trade (nearest ATM, nearest expiry after the day; ask in, bid out)")
    print("statuses:", df.status.value_counts().to_dict())
    for day, g in ok.groupby("day"):
        st = df[df.day == day]
        print(f"{day}: legs {len(g):3d} | option: mean {g.pct.mean():+.1f} % median {g.pct.median():+.1f} % win {100 * (g.net_inr > 0).mean():.0f} % "
              f"sum net ₹{g.net_inr.sum():+,.0f} on ₹{g.premium_inr.sum():,.0f} premium | stock leg same trades: mean {g.stock_bps.mean():+.1f} bps, sum net ₹{g.stock_net_inr.sum():+,.0f} at ₹1L "
              f"| entry quote age median {g.entry_age_s.median():.0f} s, stale entries {100 * (g.entry_how == 'stale_ltp').mean():.0f} %")
    print(f"pooled: legs {len(ok)} | option mean {ok.pct.mean():+.1f} % median {ok.pct.median():+.1f} % win {100 * (ok.net_inr > 0).mean():.0f} % sum net ₹{ok.net_inr.sum():+,.0f} "
          f"on ₹{ok.premium_inr.sum():,.0f} premium ({100 * ok.net_inr.sum() / ok.premium_inr.sum():+.1f} % of premium) | stock sum net ₹{ok.stock_net_inr.sum():+,.0f}")
    for side, g in ok.groupby("side"):
        print(f"  {side}: legs {len(g)} option mean {g.pct.mean():+.1f} % win {100 * (g.net_inr > 0).mean():.0f} % sum net ₹{g.net_inr.sum():+,.0f} | stock mean {g.stock_bps.mean():+.1f} bps")
    for how, g in ok.groupby("exit_reason"):
        print(f"  exit {how}: legs {len(g)} option mean {g.pct.mean():+.1f} % | stock mean {g.stock_bps.mean():+.1f} bps")
    print("  realised leverage (option % per 1 % of stock): median", round(float(ok.leverage.median()), 1), "| winners", round(float(ok[ok.stock_bps > 0].leverage.median()), 1), "| losers", round(float(ok[ok.stock_bps < 0].leverage.median()), 1))
    big = ok.sort_values("net_inr", ascending=False)
    print("  best:", [(r.day[5:], r.sym, r.side, r.name, round(r.pct, 1), round(r.net_inr)) for r in big.head(5).itertuples()])
    print("  worst:", [(r.day[5:], r.sym, r.side, r.name, round(r.pct, 1), round(r.net_inr)) for r in big.tail(5).itertuples()])


if __name__ == "__main__":
    main()
