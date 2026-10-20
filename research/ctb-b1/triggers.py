"""Every FUDKII trigger on the 09:15 candle in October 2026, regenerated from the cached stock bars with engine A's own
FUDKII, graded F included (CT-B1 reads them all), each judged by engine A's B1 rule (strategy/b1.py) with the official
gap read from the daily row's open, and the fade strike anchored as CT-B1 plans it (close -/+ 1 ATR30, nearest listed
OTM strike of the fade's side on the October expiry).

    cd ~/Downloads/kotsin/kotsin-nse/backend && uv run python ~/Downloads/kotsin/ctb-backtest/triggers.py
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

from kotsin_nse.bars.indicators import atr
from kotsin_nse.bars.official_open import official_gap
from kotsin_nse.bars.periods import monthly, previous_complete, weekly
from kotsin_nse.bars.pivots import classic_pivots, pivot_points
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction
from kotsin_nse.market.session import ist_day, session_open_ts
from kotsin_nse.research.backtest import BacktestContext
from kotsin_nse.strategy.b1 import b1_read, outer_ahead
from kotsin_nse.strategy.counter import KEY_LEVELS
from kotsin_nse.strategy.fudkii import Fudkii, FudkiiConfig

HERE = Path(__file__).resolve().parent
HIST = Path.cwd() / "data-live" / "history"
MASTER = Path.cwd() / "data-live" / "scripmaster" / "nse_fo-2026-10-20.csv"
START, END = date(2026, 10, 1), date(2026, 10, 20)
PIVOT_SCAN_ATR = 2.0  # engine.py
OCT_EXPIRY = "2026-10-27"


def bars_of(df: pd.DataFrame, symbol: str, tf: str) -> list[UnifiedBar]:
    return [UnifiedBar(symbol=symbol, scrip_code=symbol, tf=tf, ts=int(r.ts), open=float(r.o), high=float(r.h),
                       low=float(r.l), close=float(r.c), volume=float(r.v), source=BarSource.REST, complete=True)
            for r in df.itertuples()]


def resample_30m(m5: pd.DataFrame) -> pd.DataFrame:
    """5m -> 30m on the 09:15 grid (the same closes FUDKII reads live)."""
    d = m5.copy()
    day_open = d["ts"].map(lambda t: session_open_ts(Segment.NSE_EQ, ist_day(t)))
    d["b"] = day_open + ((d["ts"] - day_open) // 1800) * 1800
    g = d.groupby("b").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
    return g.rename(columns={"b": "ts"})


def strikes_from_master() -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], set[float]] = defaultdict(set)
    for r in csv.DictReader(open(MASTER)):
        if r["ScripType"] in ("CE", "PE") and r["Expiry"].startswith(OCT_EXPIRY):
            out[(r["SymbolRoot"], r["ScripType"])].add(float(r["StrikeRate"]))
    return {k: sorted(v) for k, v in out.items()}


def main() -> None:
    strikes = strikes_from_master()
    roots = sorted({s for s, _ in strikes})
    cfg = FudkiiConfig(grade_floor=False)  # CT-B1 reads graded-F triggers too
    rows: list[dict] = []
    for sym in roots:
        p5, p1d = HIST / "5m" / f"{sym}.parquet", HIST / "1d" / f"{sym}.parquet"
        if not p5.exists() or not p1d.exists():
            continue
        # the broker's own 30m bars where the cache has them (fetched through 20 Oct), the 5m resample before that
        m30 = resample_30m(pd.read_parquet(p5))
        p30 = HIST / "30m" / f"{sym}.parquet"
        if p30.exists():
            broker = pd.read_parquet(p30)
            m30 = pd.concat([m30[m30["ts"] < broker["ts"].min()], broker], ignore_index=True).sort_values("ts")
        b30 = bars_of(m30, sym, "30m")
        dailies = bars_of(pd.read_parquet(p1d), sym, "1d")
        days = sorted({ist_day(b.ts) for b in b30 if START <= ist_day(b.ts) <= END})
        for day in days:
            open_ts = int(session_open_ts(Segment.NSE_EQ, day))
            upto = [b for b in b30 if b.ts <= open_ts]
            if not upto or upto[-1].ts != open_ts or len(upto) < cfg.warm_bars + 5:
                continue
            store = BarStore(max_bars=4000)
            for b in upto[-(cfg.warm_bars + 10):]:
                store.close(b)
            ctx = BacktestContext(store, {sym: dailies}, Segment.NSE_EQ)
            ctx.today = day
            out = Fudkii(cfg).on_bar(ctx, upto[-1])
            for sig in [*out.signals, *out.triggers]:
                bull = sig.direction is Direction.BULLISH
                sign = 1 if bull else -1
                prior = [b for b in dailies if ist_day(b.ts) < day]
                today_row = next((b for b in dailies if ist_day(b.ts) == day), None)
                prev = prior[-1] if prior else None
                datr = atr(prior, 14) if len(prior) > 15 else None
                atr30 = atr(upto[-60:], 14) or 0.0
                # the engine's classic levels: daily from the previous session, weekly / monthly from the completed periods
                points = []
                if prev is not None and len(prior) >= 25:
                    lv = classic_pivots(prev.high, prev.low, prev.close)
                    if lv:
                        points += pivot_points(lv, "1d")
                    for tf, periods in (("1wk", weekly(prior)), ("1mo", monthly(prior))):
                        pc = previous_complete(periods, day)
                        if pc:
                            lv = classic_pivots(pc.high, pc.low, pc.close)
                            if lv:
                                points += pivot_points(lv, tf)
                close = float(sig.entry)
                ahead = sorted(((p.label, round(sign * (p.price - close) / atr30, 4)) for p in points
                                if p.label.split(".", 1)[-1] in KEY_LEVELS and 0 <= sign * (p.price - close) <= PIVOT_SCAN_ATR * atr30),
                               key=lambda x: x[1]) if atr30 > 0 else []
                gap = official_gap(today_row.open, prev.close, datr, bullish=bull) if (today_row and prev and datr) else None
                tctx = {"openBar": True, "gapOfficial": round(gap, 6) if gap is not None else None,
                        "gapDatr": round(sign * (upto[-1].open - prev.close) / datr, 3) if (prev and datr) else None,
                        "gapOpenSrc": "daily_open", "officialOpen": today_row.open if today_row else None,
                        "officialClose": prev.close if prev else None, "dAtr": datr, "atr30": round(atr30, 4),
                        "pivotsAheadAtr": [[lab, d] for lab, d in ahead]}
                outer = outer_ahead(points, close=close, atr30=atr30, bullish=bull, scan=PIVOT_SCAN_ATR)
                read = b1_read(tctx, close=close, bullish=bull, zones=ctx.zones(sym), pivots_known=bool(points), outer=outer)
                side = "PE" if bull else "CE"
                anchor = close - sign * atr30  # the fade's T1, 1 ATR30 the fade's way (b1_plan)
                listed = strikes.get((sym, side), [])
                otm = [k for k in listed if (k < close if side == "PE" else k > close)]
                strike = min(otm, key=lambda k: abs(k - anchor)) if otm else None
                conv = read["conviction"]
                rows.append({
                    "day": day.isoformat(), "symbol": sym, "trigger": sig.direction.value, "grade": sig.grade or "F",
                    "published": sig in out.signals, "close_0945": close, "atr30": round(atr30, 3), "dATR": round(datr, 3) if datr else None,
                    "gap_official": tctx["gapOfficial"], "gap_first_bar": tctx["gapDatr"],
                    "b1_take": read["take"], "b1_gate": read["gate"], "b1_why": read["why"],
                    "pins": "; ".join(f"{lab} +{d:.2f}" for lab, d in read["pins"]),
                    "conviction": conv["total"], "conv_gap": conv["points"]["gap"], "conv_stretch": conv["points"]["stretch"],
                    "conv_room": conv["points"]["room"], "conv_trigwall": conv["points"]["trigWall"],
                    "fade_side": side, "fade_anchor": round(anchor, 2), "fade_strike": strike,
                })
    df = pd.DataFrame(rows).sort_values(["day", "symbol"])
    df.to_csv(HERE / "triggers_oct.csv", index=False)
    pd.set_option("display.width", 250)
    print(f"{len(df)} FUDKII 09:15 triggers, {df.b1_take.sum()} B1 fades, over {df.day.nunique()} days, {df.symbol.nunique()} names")
    print(df.groupby("day").agg(triggers=("symbol", "count"), fades=("b1_take", "sum")).T.to_string())
    print("\nsheet rows:")
    sheet = {("2026-10-14", "JSWENERGY"), ("2026-10-15", "ENRIN"), ("2026-10-15", "GVT&D"), ("2026-10-15", "BANKINDIA"), ("2026-10-15", "AXISBANK"),
             ("2026-10-16", "GODREJCP"), ("2026-10-16", "SWIGGY"), ("2026-10-16", "VOLTAS"), ("2026-10-19", "PNB"), ("2026-10-19", "AXISBANK"),
             ("2026-10-19", "PREMIERENE"), ("2026-10-19", "SBICARD"), ("2026-10-19", "UNIONBANK"), ("2026-10-19", "UNITDSPR")}
    hit = df[[(d, s) in sheet for d, s in zip(df.day, df.symbol)]]
    print(hit[["day", "symbol", "trigger", "grade", "gap_official", "b1_take", "b1_gate", "conviction", "fade_side", "fade_strike"]].to_string(index=False))
    print("missing from regeneration:", sorted(sheet - set(zip(hit.day, hit.symbol))))


if __name__ == "__main__":
    main()
