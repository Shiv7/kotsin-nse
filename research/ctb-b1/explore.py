"""The gap rule over the exploration window, on the STOCK: every FUDKII 09:15 trigger regenerated with engine A's own
code (graded F included), its official gap (the daily row's open against the previous close over ATR14 - verified
against the sheet), the B1 pin test, and what the stock did afterwards. Plus a control: every F&O name on every day,
gap and the move after 09:45 - with or without a trigger. The holdout (2025-10-01 ->) is not touched.

    cd ~/Downloads/kotsin/kotsin-nse/backend && uv run python ~/Downloads/kotsin/ctb-backtest/explore.py
"""

from __future__ import annotations

import csv
import time
from datetime import date
from pathlib import Path

import pandas as pd

from kotsin_nse.bars.indicators import atr_series
from kotsin_nse.bars.periods import monthly, previous_complete, weekly
from kotsin_nse.bars.pivots import classic_pivots, pivot_points
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction
from kotsin_nse.market.session import ist_day, session_open_ts
from kotsin_nse.research.backtest import BacktestContext
from kotsin_nse.strategy.b1 import OUTER_LEVELS, outer_ahead
from kotsin_nse.strategy.counter import KEY_LEVELS
from kotsin_nse.strategy.fudkii import Fudkii, FudkiiConfig

HERE = Path(__file__).resolve().parent
HIST = Path.cwd() / "data-live" / "history"
MASTER = Path.cwd() / "data-live" / "scripmaster" / "nse_fo-2026-10-20.csv"
START, END = date(2024, 4, 1), date(2025, 9, 30)  # HANDOFF §4 exploration window for intraday strategies
B1_GAP, B1_PIN, B1_PIN_OUTER, SCAN = 0.25, 0.5, 0.25, 2.0


def bars_of(df: pd.DataFrame, symbol: str, tf: str) -> list[UnifiedBar]:
    return [UnifiedBar(symbol=symbol, scrip_code=symbol, tf=tf, ts=int(r.ts), open=float(r.o), high=float(r.h),
                       low=float(r.l), close=float(r.c), volume=float(r.v), source=BarSource.REST, complete=True)
            for r in df.itertuples()]


def resample_30m(m5: pd.DataFrame) -> pd.DataFrame:
    d = m5.copy()
    day_open = d["ts"].map(lambda t: session_open_ts(Segment.NSE_EQ, ist_day(t)))
    d["b"] = day_open + ((d["ts"] - day_open) // 1800) * 1800
    g = d.groupby("b").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
    return g.rename(columns={"b": "ts"})


def main() -> None:
    roots = sorted({r["SymbolRoot"] for r in csv.DictReader(open(MASTER)) if r["ScripType"] in ("CE", "PE")})
    cfg = FudkiiConfig(grade_floor=False)
    rows: list[dict] = []
    t0 = time.time()
    for n, sym in enumerate(roots):
        p5, p1d = HIST / "5m" / f"{sym}.parquet", HIST / "1d" / f"{sym}.parquet"
        if not p5.exists() or not p1d.exists():
            continue
        m5 = pd.read_parquet(p5)
        m5["day"] = m5["ts"].map(lambda t: ist_day(t))
        m5 = m5[(m5["day"] >= date(2024, 1, 1)) & (m5["day"] <= END)]
        if m5.empty:
            continue
        b30 = bars_of(resample_30m(m5), sym, "30m")
        atr30_at = dict(zip((b.ts for b in b30), atr_series(b30, 14), strict=True))
        dailies = bars_of(pd.read_parquet(p1d), sym, "1d")
        dailies = [b for b in dailies if ist_day(b.ts) <= END]
        datr_at = dict(zip((ist_day(b.ts) for b in dailies), atr_series(dailies, 14), strict=True))
        d_by_day = {ist_day(b.ts): b for b in dailies}
        d_days = sorted(d_by_day)
        prev_of = {d: d_days[i - 1] for i, d in enumerate(d_days) if i > 0}
        weeks, months = weekly(dailies), monthly(dailies)
        m5_by_day = {d: g.sort_values("ts") for d, g in m5.groupby("day")}
        # 09:15-bar volume baseline: the mean of the previous 20 sessions' 09:15 bars
        open_vol = {ist_day(b.ts): b.volume for b in b30 if b.ts == session_open_ts(Segment.NSE_EQ, ist_day(b.ts))}
        ov_days = sorted(open_vol)
        store = BarStore(max_bars=4000)
        ctx = BacktestContext(store, {sym: dailies}, Segment.NSE_EQ)
        fud = Fudkii(cfg)
        for b in b30:
            store.close(b)
            day = ist_day(b.ts)
            if b.ts != session_open_ts(Segment.NSE_EQ, day) or not (START <= day <= END):
                continue
            prev_day = prev_of.get(day)
            today_row = d_by_day.get(day)
            if prev_day is None or today_row is None:
                continue
            prev, datr, atr30 = d_by_day[prev_day], datr_at.get(prev_day), atr30_at.get(b.ts)
            if not datr or not atr30:
                continue
            ctx.today = day
            out = fud.on_bar(ctx, b)
            sigs = [*out.signals, *out.triggers]
            sig = sigs[0] if sigs else None
            # the stock after 09:45, from the 09:45 close (the trigger's entry)
            after = m5_by_day.get(day)
            if after is None:
                continue
            after = after[after["ts"] >= b.ts + 1800]
            if after.empty:
                continue
            c0 = b.close
            hm = after["ts"].map(lambda t: time.strftime("%H:%M", time.gmtime(t + 19800)))
            at = lambda h: float(after[hm <= h]["c"].iloc[-1]) if (hm <= h).any() else None  # noqa: E731
            gap_raw = (today_row.open - prev.close) / datr  # + = gap up
            # the trigger's levels, as the engine builds them: daily from the previous session, weekly/monthly complete
            points = []
            if len([d for d in d_days if d < day]) >= 25:
                lv = classic_pivots(prev.high, prev.low, prev.close)
                if lv:
                    points += pivot_points(lv, "1d")
                for tf, periods in (("1wk", weeks), ("1mo", months)):
                    pc = previous_complete(periods, day)
                    if pc:
                        lv = classic_pivots(pc.high, pc.low, pc.close)
                        if lv:
                            points += pivot_points(lv, tf)
            idx = ov_days.index(day) if day in ov_days else -1
            base = [open_vol[d] for d in ov_days[max(0, idx - 20):idx]] if idx > 0 else []
            rel_vol = (open_vol[day] / (sum(base) / len(base))) if base and sum(base) > 0 else None
            row = {
                "day": day.isoformat(), "symbol": sym, "trigger": sig.direction.value if sig else None,
                "grade": (sig.grade or "F") if sig else None, "published": bool(sig and sig in out.signals),
                "c0945": c0, "open": today_row.open, "prev_close": prev.close, "datr": datr, "atr30": atr30,
                "gap_raw": gap_raw, "bar_up": b.close > b.open, "rel_vol": rel_vol,
                "stretch_raw": (c0 - prev.close) / datr,
                "c1015": at("10:15"), "c1115": at("11:15"), "c1315": at("13:15"), "cend": float(after["c"].iloc[-1]),
                "hi": float(after["h"].max()), "lo": float(after["l"].min()),
            }
            if sig is not None:
                bull = sig.direction is Direction.BULLISH
                sign = 1 if bull else -1
                gap = sign * gap_raw
                ahead = sorted(((p.label, round(sign * (p.price - c0) / atr30, 4)) for p in points
                                if p.label.split(".", 1)[-1] in KEY_LEVELS and 0 <= sign * (p.price - c0) <= SCAN * atr30),
                               key=lambda x: x[1])
                outer = outer_ahead(points, close=c0, atr30=atr30, bullish=bull, scan=SCAN)
                pins = [(lab, d) for lab, d in ahead if d <= B1_PIN] + [(lab, d) for lab, d in outer
                                                                        if lab.split(".", 1)[-1] in OUTER_LEVELS and d <= B1_PIN_OUTER]
                row.update(gap=gap, pivots_known=bool(points), pinned=bool(pins), pin=min(pins, key=lambda x: x[1])[0] if pins else None,
                           b1=bool(points) and gap >= B1_GAP and not pins)
            rows.append(row)
        if n % 20 == 0:
            print(f"{n}/{len(roots)} {sym} rows={len(rows)} {time.time() - t0:.0f}s", flush=True)
    df = pd.DataFrame(rows)
    df.to_parquet(HERE / "explore_2024_2025.parquet", index=False)
    print("saved", len(df), "symbol-days;", df.trigger.notna().sum(), "triggers;", int(df.b1.fillna(False).sum()), "B1 fades")


if __name__ == "__main__":
    main()
