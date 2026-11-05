"""Option structures on the fresh bull-turn names, priced from NSE's F&O bhavcopy at Thursday's close — the owner (6 Nov
2026): "can we make some option strategy on these stocks?". Per stock: the monthly expiry, lot, the future's close, the ATM
implied volatility (Black-76 from the ATM call and put closes) against the 20-session realised, and two defined-risk
bullish structures: the bull call spread (buy ATM, sell one and two strikes up: debit, max profit, breakeven, the market's
own probability of finishing above the breakeven) and the bull put spread (sell one strike below ATM, buy two below:
credit, max loss, probability of keeping it), with the traded volume at the strikes used (liquidity). Closes are the
exchange's settlement prices, not live bids and asks: a live spread costs more. Facts; the choice is the owner's.
Usage: option_structures.py SYMBOL [SYMBOL ...]"""
from __future__ import annotations

import datetime
import math
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/docs/research/scripts/2026-11-04-nifty-facts")
import live_facts as lf  # noqa: E402
from kotsin_nse.fno.chain import YEAR_S, black76, implied_vol  # noqa: E402
from kotsin_nse.strategy.straddle import p_between, realised_vol  # noqa: E402
from screen import DD, WINDOW, archive_daily, history  # noqa: E402

DAY = WINDOW[-1]
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def main() -> None:
    bh = pq.read_table(DD / "oi_daily" / f"{DAY}.parquet").to_pandas()
    m = lf.master_for(DAY)
    # the lot by root: the master's future rows (ScripType XX) for "SYMBOL DD MON YYYY"
    fut_rows = m[m["ScripType"].astype(str) == "XX"]
    lots = {str(n).split(" ")[0]: int(l) for n, l in zip(fut_rows["Name"], fut_rows["LotSize"], strict=False)}
    arch = archive_daily(DAY).set_index("symbol")
    now = datetime.datetime(2026, 11, 6, 9, 15, tzinfo=IST).timestamp()
    rows = []
    for sym in sys.argv[1:]:
        f = bh[(bh.symbol == sym) & (bh.kind == "FUTSTK")].sort_values("expiry")
        o = bh[(bh.symbol == sym) & (bh.kind == "OPTSTK")]
        if f.empty or o.empty or sym not in arch.index:
            print(sym, "no data")
            continue
        exp = str(f["expiry"].iloc[0])
        fut = float(f["close"].iloc[0])
        spot = float(arch.loc[sym, "c"])
        lot = int(lots.get(sym, 0) or 0)
        oe = o[o["expiry"].astype(str) == exp]
        ks = sorted(float(k) for k in oe["strike"].unique())
        d = datetime.date.fromisoformat(exp)
        t = max(datetime.datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp() - now, 60) / YEAR_S
        atm = min(ks, key=lambda k: abs(k - spot))
        i = ks.index(atm)

        def px(k: float, kind: str) -> tuple[float, float]:
            r = oe[(oe.strike == k) & (oe.option_type == kind)]
            return (float(r["close"].iloc[0]), float(r["volume"].iloc[0])) if len(r) else (float("nan"), 0.0)

        ce, pe = px(atm, "CE")[0], px(atm, "PE")[0]
        ivs = [v for v in (implied_vol(ce, fut, atm, t, True), implied_vol(pe, fut, atm, t, False)) if v is not None]
        sigma = sum(ivs) / len(ivs) if ivs else None
        h = history(sym)
        w = arch.loc[[sym]] if sym in arch.index else None
        closes = [*h["c"].tolist(), *[float(archive_daily(dd).set_index("symbol").loc[sym, "c"]) for dd in WINDOW if sym in archive_daily(dd).set_index("symbol").index]] if h is not None else []
        rv = realised_vol(closes) if closes else None
        row = {"symbol": sym, "spot": spot, "fut": fut, "expiry": exp, "days": (d - datetime.date(2026, 11, 6)).days, "lot": lot, "atm": atm,
               "iv": round(sigma * 100, 1) if sigma else None, "rv20": rv, "iv_over_rv": round(sigma * 100 / rv, 2) if sigma and rv else None,
               "straddle_pct": round((ce + pe) / spot * 100, 2) if ce == ce and pe == pe else None}
        for n in (1, 2):
            if i + n < len(ks):
                k2 = ks[i + n]
                c1, v1 = px(atm, "CE"); c2, v2 = px(k2, "CE")
                debit = c1 - c2
                be = atm + debit
                row[f"bcs{n}"] = {"k": (atm, k2), "debit": round(debit, 2), "cost_inr": round(debit * lot), "max_profit_inr": round(((k2 - atm) - debit) * lot),
                                  "be": round(be, 1), "be_pct": round((be / spot - 1) * 100, 2), "rr": round(((k2 - atm) - debit) / debit, 2) if debit > 0 else None,
                                  "p_above_be": p_between(fut, be, None, sigma, t), "vol": (int(v1), int(v2))}
        if i - 2 >= 0:
            k1, k2 = ks[i - 1], ks[i - 2]
            p1, v1 = px(k1, "PE"); p2, v2 = px(k2, "PE")
            credit = p1 - p2
            width = k1 - k2
            row["bps"] = {"k": (k1, k2), "credit": round(credit, 2), "credit_inr": round(credit * lot), "max_loss_inr": round((width - credit) * lot),
                          "be": round(k1 - credit, 1), "be_pct": round(((k1 - credit) / spot - 1) * 100, 2),
                          "p_keep": p_between(fut, k1 - credit, None, sigma, t), "vol": (int(v1), int(v2))}
        rows.append(row)
    pd.set_option("display.width", 250)
    print(f"== {DAY} close, NSE bhavcopy settlement prices; structures on the monthly expiry; the probabilities are the option prices' own (lognormal under the ATM IV)")
    for r in rows:
        print(f"\n{r['symbol']}: spot {r['spot']} fut {r['fut']} expiry {r['expiry']} ({r['days']} d) lot {r['lot']} | ATM {r['atm']} IV {r['iv']} rv20 {r['rv20']} IV/RV {r['iv_over_rv']} straddle {r['straddle_pct']} % of spot")
        for key, label in (("bcs1", "bull call spread, one strike"), ("bcs2", "bull call spread, two strikes"), ("bps", "bull put spread")):
            if key in r:
                print(f"   {label:30s} {r[key]}")


if __name__ == "__main__":
    main()
