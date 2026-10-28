"""Replay one session through the engine's own ShadowRunner on the exchange's bars served by the running engine.

Same code path as live (store + runner + gateway in SHADOW), fed bucket by bucket across all symbols; the day's
levels come from the engine's 1d/30m series exactly as the live runner reads them. Writes sim/<day>.json.
"""
import asyncio, json, sys, urllib.request
from datetime import date
from pathlib import Path
import pandas as pd
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.exec.gateway import Gateway, Mode
from kotsin_nse.exec.paper import PaperMatcher
from kotsin_nse.market.session import session_close_ts, session_open_ts
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy.runner import ShadowRunner

S = Path(sys.argv[1]); DAY = sys.argv[2]; API = "http://localhost:8500"
d = date.fromisoformat(DAY); OPEN, CLOSE = session_open_ts(Segment.NSE_EQ, d), session_close_ts(Segment.NSE_EQ, d)
codes = pd.read_parquet("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive/bars/2026-10-27.parquet", columns=["symbol", "scrip_code"]).drop_duplicates("symbol")
insts = {r.symbol: Instrument(scrip_code=str(r.scrip_code), symbol=r.symbol, segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY) for r in codes.itertuples()}

def get(sym, tf, n):
    with urllib.request.urlopen(f"{API}/api/bars/{sym}?tf={tf}&n={n}", timeout=30) as r:
        return json.load(r)["bars"]

def ubar(inst, tf, b):
    return UnifiedBar(symbol=inst.symbol, scrip_code=inst.scrip_code, tf=tf, ts=int(b["ts"]), open=b["o"], high=b["h"], low=b["l"], close=b["c"],
                      volume=b["v"], source=BarSource.REST if b["source"] == "rest" else BarSource.LIVE, complete=True)

async def main():
    store = BarStore()
    today: list[UnifiedBar] = []
    missing = []
    for sym, inst in insts.items():
        try:
            d1, m30, m5 = get(sym, "1d", 80), get(sym, "30m", 40), get(sym, "5m", 100)
        except Exception as e:  # noqa: BLE001
            missing.append((sym, str(e)[:60])); continue
        store.seed(sym, "1d", [ubar(inst, "1d", b) for b in d1 if b["ts"] < OPEN])
        store.seed(sym, "30m", [ubar(inst, "30m", b) for b in m30 if b["ts"] < OPEN])
        today += [ubar(inst, "5m", b) for b in m5 if OPEN <= b["ts"] < CLOSE]
    today.sort(key=lambda b: (b.ts, b.symbol))
    s = Settings(_env_file=None, data_dir=S / "sim" / "data", engine_enabled=False, strategy_pdc="last")
    costs = CostModel(s)
    gw = Gateway(matcher=PaperMatcher(costs), mode=lambda: Mode.SHADOW, halted=lambda: (False, ""), book_for=lambda _c: None, ltp_for=lambda _c: None)
    tasks: list[asyncio.Task] = []
    def spawn(coro, name):
        t = asyncio.create_task(coro, name=name); tasks.append(t); return t
    r = ShadowRunner(s, store=store, gateway=gw, ledger=None, costs=costs, underlyings=insts, spawn=spawn)
    for b in today:
        store.close(b)
        await r.on_bar(b)
    await asyncio.gather(*tasks)
    snap = r.snapshot()
    (S / "sim" / f"{DAY}.json").write_text(json.dumps(snap, indent=1, default=str))
    from collections import Counter
    print(f"{DAY}: {len(insts)} symbols, {len(today)} 5m bars fed, missing {missing[:3]}")
    print("levels for", snap["with_levels"], "| stages:", Counter(st["stage"] for st in snap["stages"]).most_common())
    sm = snap["summary"]; print("summary:", {k: sm[k] for k in ("signals", "taken", "skipped", "closed", "wins", "losses", "avg_pnl_bps", "sum_gross_inr", "sum_net_inr", "sum_r")})
    print("by rule:", {k: (v["closed"], v["avg_pnl_bps"], v["win_pct"]) for k, v in sm["by_rule"].items()})
    print("\nsignals (bar = Kite label):")
    for g in sorted(snap["signals"], key=lambda x: x["ts"]):
        print(f"  {g['bar']} {g['symbol']:11} {g['side']:5} entry {g['entry']:<9} stop {round(g['stop'],2):<9} risk {g['risk_bps']:>5} bps  wall1 {g['walls'][0]['name'] if g['walls'] else '-'} {round(g['walls'][0]['price'],2) if g['walls'] else ''}  retest {g['retest_time']}  taken={g['taken']} {g['why']}")
    print("\ntrades:")
    for t in sorted(snap["trades"], key=lambda x: x["opened_ts"]):
        v = t["virtual"]
        print(f"  {t['entry_bar']} {t['symbol']:11} {t['side']:5} entry {t['entry']:<9} stop {round(t['initial_stop'],2):<9} exit {round(t['exit'],2):<9} {t['exit_reason']:5} {t['pnl_bps']:+7.1f} bps  mfe {t['mfe_bps']:+.0f} mae {t['mae_bps']:+.0f} | fixed_wall {v['fixed_wall']['pnl_bps']:+.1f} ({v['fixed_wall']['how']}) | 1:2 {v['fixed_1to2']['pnl_bps']:+.1f} ({v['fixed_1to2']['how']})  net ₹{t['net_inr']}")
asyncio.run(main())
