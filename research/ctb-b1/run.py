"""Replay the sheet's 13 trades on real 1-minute option candles and write the comparison workbook.

    cd ~/Downloads/kotsin/kotsin-nse/backend && uv run python ~/Downloads/kotsin/ctb-backtest/run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ladder import replay  # noqa: E402
from sheet import ROWS, load, raw_pivots, to_tick  # noqa: E402

import os
LENIENT = os.environ.get("LENIENT") == "1"
OUT = Path(__file__).resolve().parent / ("CTB_B1_replay_lenient.xlsx" if LENIENT else "CTB_B1_replay.xlsx")
# the sheet's rates (row 2); GST on brokerage + exchange + SEBI only (the header) - the sheet's formula also taxes
# stamp duty and STT, shown as a separate column
STAMP, EXCH, STT, SEBI, GST = 3e-05, 0.0003553, 0.001, 1e-06, 0.18


def charges(buy: float, sell: float, brokerage: float) -> dict[str, float]:
    stamp, exch, stt, sebi = STAMP * buy, EXCH * (buy + sell), STT * sell, SEBI * (buy + sell)
    gst = GST * (brokerage + exch + sebi)
    return {"brokerage": brokerage, "stamp": stamp, "exchange": exch, "stt": stt, "sebi": sebi, "gst": gst,
            "total": brokerage + stamp + exch + stt + sebi + gst, "gst_sheet_extra": GST * (stamp + stt)}


def main() -> None:
    out = []
    for r in ROWS:
        d1, m1 = load(r.option, "1d"), load(r.option, "1m")
        day = m1[m1["day"] == r.day].assign(hm=lambda d: d["dt"].str[11:16])
        piv = [to_tick(p) for p, _ in raw_pivots(d1, r.day) if to_tick(p) > r.entry]
        res = replay(day, r.entry, piv, lenient=LENIENT)
        buy = r.entry * r.lot * 4
        sell = res.revenue(r.lot)
        ch = charges(buy, sell, r.brokerage)
        gross = sell - buy
        net = gross - ch["total"]
        day_high = float(day[day["hm"] >= "09:45"]["h"].max())
        fills = "; ".join(f"{f.lots}L @{f.price:g} {f.hm} ({f.why})" for f in res.fills)
        out.append({
            "xl_row": r.xl_row, "day": r.day, "option": r.option.replace("and", "&"), "entry": r.entry,
            "lot_exchange": r.lot, "lot_sheet": r.lot_sheet,
            "pivots_above_entry": ", ".join(f"{p:g}" for p in piv[:6]),
            "T1_T2_T3_replay": ", ".join(f"{t:g}" for t in res.targets),
            "T_sheet": ", ".join(f"{t:g}" for t in r.t if t),
            "armed_at": res.armed_hm or "never",
            "peak_replay": res.peak if res.armed_hm else None, "peak_hm": res.peak_hm, "day_high_after_entry": day_high,
            "peak_sheet": r.peak,
            "fills": fills,
            "invested": buy, "revenue": sell, "gross": gross, **{f"chg_{k}": v for k, v in ch.items()},
            "net_replay": net, "net_sheet": r.sheet_net, "diff": net - r.sheet_net,
            "notes": "; ".join(res.notes),
        })
    df = pd.DataFrame(out)
    tot = {"invested": df["invested"].sum(), "revenue": df["revenue"].sum(), "gross": df["gross"].sum(),
           "net_replay": df["net_replay"].sum(), "net_sheet": df["net_sheet"].sum(), "diff": df["diff"].sum()}
    with pd.ExcelWriter(OUT) as xw:
        df.to_excel(xw, sheet_name="replay", index=False)
        pd.DataFrame([tot]).to_excel(xw, sheet_name="totals", index=False)
    pd.set_option("display.width", 250)
    print(df[["day", "option", "entry", "T1_T2_T3_replay", "T_sheet", "armed_at", "peak_replay", "peak_sheet", "net_replay", "net_sheet"]].to_string(index=False))
    print("\nFILLS")
    for x in out:
        print(f"  {x['option']:18s} {x['fills']}")
    print(f"\nTOTAL invested {tot['invested']:,.0f}  net replay {tot['net_replay']:,.0f} ({tot['net_replay']/tot['invested']:.1%})  net sheet {tot['net_sheet']:,.0f}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
