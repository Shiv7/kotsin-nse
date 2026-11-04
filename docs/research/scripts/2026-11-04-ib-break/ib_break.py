"""PRE-REGISTERED 4 Nov 2026 22:05 IST, before any run — the first-hour continuation rule, built from the fact base
(docs/research/scripts/2026-11-04-nifty-facts): the first hour's direction matches the day's close direction 73 % of the
time on the Nifty-50 stocks, an initial-balance (IB) break closes beyond the IB 55 % of the time, the day's extremes form at
the open or the close, and no wall adds information at the 30–60-minute horizon. So: no walls, no retest — the day's
direction from its first hour, the entry on the IB break in that direction, the stop inside the IB, the exit at the close.

Rule (one set of numbers, fixed here):
  universe   Nifty-50 constituents with history (data-live/universe-nifty50.txt, indices excluded), 5m bars 09:15–15:30.
  IB         the first twelve bars (09:15–10:15): IBH = their high, IBL = their low, MID = (IBH + IBL) / 2.
  direction  sign(close of bar 12 − the day's open). V3 additionally requires |that move| ≥ 0.2 ADR (ADR = the engine's
             guarded 60-session average daily range).
  signal     the first bar i ≥ 12, starting at or before 14:25, whose close is beyond the IB on the direction's side
             (long: close > IBH; short: close < IBL). Fill at the next bar's open.
  stop       V1, V3: MID. V2: the opposite IB edge. A bar whose low (long) / high (short) touches the stop exits at the
             stop, or at the bar's open when the open is already through it.
  exit       the 15:10 bar's close when not stopped (the research convention for a close exit).
  cap        two trades a day across the universe, first by signal bar, ties by the larger first-hour move in ADR.
  size       ₹1,00,000 notional a trade; costs 15 bps a round trip (the retest research's figure).
  windows    development 2024-01-01 → 2025-09-30 (in-sample: the rule is derived from facts on this window);
             forward 2026-10-17 → 2026-11-04 (after the spent holdout; from the history to 10-28 and the archive's 1m
             bars after; a dozen sessions, a description); today = 2026-11-04 shown trade by trade.
  bar        net per trade > 0 with t ≥ 2 on the development window and positive in both 2024 and 2025 → ten shadow
             sessions. Nothing goes live from this file.
  variants   V1, V2, V3 are all declared here and all reported; no other knob is turned.
Usage: ib_break.py OUT_DIR
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from kotsin_nse.scan.replay import guarded_adr

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
DEV0, DEV1 = datetime.date(2024, 1, 1), datetime.date(2025, 9, 30)
FWD0, FWD1 = datetime.date(2026, 10, 17), datetime.date(2026, 11, 4)
HIST_END = datetime.date(2026, 10, 28)
COST = 15.0
NOTIONAL = 100_000.0
CAP = 2
LAST_SIGNAL_BAR = 62  # 09:15 + 62×5 min = 14:25
CLOSE_BAR = 71  # 15:10
MIN_MOVE_V3 = 0.2
VARIANTS = ("V1", "V2", "V3")


def load_hist(sym: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    m5 = pd.read_parquet(DD / "history" / "5m" / f"{sym}.parquet").drop_duplicates("ts").sort_values("ts")
    t = pd.to_datetime(m5.ts + 19800, unit="s")
    m5["d"] = t.dt.date; m5["hm"] = t.dt.strftime("%H:%M")
    m5 = m5[(m5.hm >= "09:15") & (m5.hm < "15:30")]
    d1 = pd.read_parquet(DD / "history" / "1d" / f"{sym}.parquet").sort_values("ts")
    d1["d"] = pd.to_datetime(d1.ts + 19800, unit="s").dt.date
    d1["final"] = ((d1.ts + 19800) % 86400 == 0)
    d1 = d1.sort_values(["d", "final"]).drop_duplicates("d", keep="last")
    return m5, d1


def archive_5m(day: datetime.date, syms: list[str]) -> dict[str, pd.DataFrame]:
    f = DD / "archive" / "bars" / f"{day.isoformat()}.parquet"
    if not f.exists():
        return {}
    ab = pd.read_parquet(f, columns=["symbol", "ts", "o", "h", "l", "c"])
    ab = ab[ab.symbol.isin(syms)].copy()
    t = pd.to_datetime(ab.ts + 19800, unit="s")
    ab["hm"] = t.dt.strftime("%H:%M")
    ab = ab[(ab.hm >= "09:15") & (ab.hm < "15:30")]
    ab["s"] = ((ab.ts + 19800) // 300) * 300 - 19800
    out = {}
    for sym, g in ab.groupby("symbol"):
        g5 = g.groupby("s").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last")).reset_index()
        g5["hm"] = [datetime.datetime.fromtimestamp(x, datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%H:%M") for x in g5.s]
        out[sym] = g5
    return out


def day_signal(o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, adr: float, variant: str) -> dict | None:
    if len(c) < CLOSE_BAR + 1:
        return None
    ibh, ibl = float(h[:12].max()), float(lo[:12].min()); mid = (ibh + ibl) / 2
    move = (float(c[11]) - float(o[0])) / adr
    d = int(np.sign(move))
    if d == 0 or (variant == "V3" and abs(move) < MIN_MOVE_V3):
        return None
    for i in range(12, LAST_SIGNAL_BAR + 1):
        if (d == 1 and c[i] > ibh) or (d == -1 and c[i] < ibl):
            entry = float(o[i + 1])
            stop = mid if variant in ("V1", "V3") else (ibl if d == 1 else ibh)
            how, ex_i, ex_px = "close", CLOSE_BAR, float(c[CLOSE_BAR])
            for j in range(i + 1, CLOSE_BAR + 1):
                if d == 1 and lo[j] <= stop:
                    how, ex_i, ex_px = "stop", j, min(float(o[j]), stop); break
                if d == -1 and h[j] >= stop:
                    how, ex_i, ex_px = "stop", j, max(float(o[j]), stop); break
            gross = d * (ex_px - entry) / entry * 1e4
            return {"dir": d, "sig_bar": i, "entry_bar": i + 1, "entry": entry, "stop": stop, "ibh": ibh, "ibl": ibl, "ib_adr": (ibh - ibl) / adr, "move_adr": move,
                    "exit_bar": ex_i, "exit": ex_px, "how": how, "gross_bps": gross, "net_bps": gross - COST, "risk_bps": abs(entry - stop) / entry * 1e4}
    return None


def hm_of(bar: int) -> str:
    m = 9 * 60 + 15 + 5 * bar
    return f"{m // 60:02d}:{m % 60:02d}"


def run(days_by_sym: dict[str, dict], adr_by_sym_day, variant: str) -> pd.DataFrame:
    rows = []
    for sym, days in days_by_sym.items():
        for D, g in days.items():
            adr = adr_by_sym_day(sym, D)
            if not adr:
                continue
            s = day_signal(g["o"].to_numpy(float), g["h"].to_numpy(float), g["l"].to_numpy(float), g["c"].to_numpy(float), adr, variant)
            if s:
                s.update(sym=sym, d=D, adr=adr); rows.append(s)
    T = pd.DataFrame(rows)
    if T.empty:
        return T
    T["abs_move"] = T.move_adr.abs()
    T = T.sort_values(["d", "sig_bar", "abs_move"], ascending=[True, True, False])
    T["rank"] = T.groupby("d").cumcount() + 1
    T = T[T["rank"] <= CAP].copy()
    T["inr"] = T.net_bps * NOTIONAL / 1e4
    T["entry_hm"] = T.entry_bar.map(hm_of); T["exit_hm"] = T.exit_bar.map(hm_of)
    return T


def summary(T: pd.DataFrame, name: str) -> str:
    if T.empty:
        return f"  {name}: no trades"
    n = len(T); net = T.net_bps; t = net.mean() / net.std(ddof=1) * np.sqrt(n) if n > 1 and net.std(ddof=1) > 0 else float("nan")
    daily = T.groupby("d").inr.sum(); eq = daily.cumsum(); dd = (eq - eq.cummax()).min()
    return (f"  {name}: {n} trades on {T.d.nunique()} days ({n / T.d.nunique():.2f}/day), win {100 * (T.gross_bps > 0).mean():.0f} %, "
            f"gross {T.gross_bps.mean():+.1f} bps, net {net.mean():+.1f} bps (t {t:.2f}), median net {net.median():+.1f}, "
            f"stopped {100 * (T.how == 'stop').mean():.0f} %, avg win {T[net > 0].net_bps.mean():+.0f} / avg loss {T[net <= 0].net_bps.mean():+.0f}, "
            f"total ₹{T.inr.sum():+,.0f} at ₹1L a trade, worst day ₹{daily.min():+,.0f}, max drawdown ₹{dd:+,.0f}, longs {int((T.dir == 1).sum())} / shorts {int((T.dir == -1).sum())}")


def main() -> None:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    syms = [s for s in (DD / "universe-nifty50.txt").read_text().split() if s.isupper() and "NIFTY" not in s and (DD / "history" / "5m" / f"{s}.parquet").exists()]
    H: dict[str, pd.DataFrame] = {}; D1: dict[str, pd.DataFrame] = {}
    dev: dict[str, dict] = {}; fwd: dict[str, dict] = {}
    for sym in syms:
        m5, d1 = load_hist(sym); H[sym] = m5; D1[sym] = d1
        dev[sym] = {D: g for D, g in m5.groupby("d") if DEV0 <= D <= DEV1 and len(g) >= 72}
        fwd[sym] = {D: g for D, g in m5.groupby("d") if FWD0 <= D <= HIST_END and len(g) >= 72}
    # the archive's 1m bars for the sessions after the history ends
    day = HIST_END + datetime.timedelta(days=1)
    while day <= FWD1:
        for sym, g5 in archive_5m(day, syms).items():
            if len(g5) >= 72:
                fwd.setdefault(sym, {})[day] = g5
        day += datetime.timedelta(days=1)
    cache: dict[tuple[str, datetime.date], float | None] = {}

    def adr_for(sym: str, D: datetime.date) -> float | None:
        k = (sym, D)
        if k not in cache:
            before = D1[sym][D1[sym].d < D]
            cache[k] = guarded_adr(before.h.tolist(), before.l.tolist(), before.c.tolist()) if len(before) >= 61 else None
        return cache[k]

    print(f"== universe {len(syms)} stocks; development {DEV0} → {DEV1}; forward {FWD0} → {FWD1} (history to {HIST_END}, archive bars after); cost {COST:.0f} bps, ₹{NOTIONAL:,.0f} a trade, cap {CAP}/day")
    for v in VARIANTS:
        T = run(dev, adr_for, v); T.to_csv(out / f"trades-dev-{v}.csv", index=False)
        print(f"\n== {v}: stop {'IB midpoint' if v != 'V2' else 'opposite IB edge'}{', first-hour move ≥ 0.2 ADR' if v == 'V3' else ''}")
        print(summary(T, "development"))
        for y, s in T.groupby(pd.to_datetime(T.d).dt.year):
            print(summary(s, f"  {y}"))
        for h_, s in T.groupby(np.where(pd.to_datetime(T.d) < pd.Timestamp("2024-11-15"), "first half", "second half")):
            print(summary(s, f"  {h_}"))
        for b_, s in T.groupby(pd.cut(T.entry_bar, [12, 18, 24, 36, 48, 64], labels=["10:20-10:45", "10:50-11:15", "11:20-12:15", "12:20-13:15", "13:20-14:30"])):
            print(summary(s, f"  entry {b_}"))
        for b_, s in T.groupby(pd.cut(T.abs_move, [0, 0.1, 0.2, 0.4, 9], labels=["move <0.1 ADR", "0.1-0.2", "0.2-0.4", ">0.4"])):
            print(summary(s, f"  {b_}"))
        for b_, s in T.groupby(pd.cut(T.ib_adr, [0, 0.4, 0.6, 0.9, 9], labels=["IB <0.4 ADR", "0.4-0.6", "0.6-0.9", ">0.9"])):
            print(summary(s, f"  {b_}"))
        F = run(fwd, adr_for, v); F.to_csv(out / f"trades-fwd-{v}.csv", index=False)
        print(summary(F, "forward 17 Oct → 4 Nov 2026 (description)"))
        if not F.empty:
            print(F[["d", "sym", "dir", "entry_hm", "entry", "stop", "exit_hm", "exit", "how", "gross_bps", "net_bps", "inr", "move_adr", "ib_adr"]].round(2).to_string(index=False))


if __name__ == "__main__":
    main()
