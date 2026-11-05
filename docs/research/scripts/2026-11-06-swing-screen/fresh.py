"""Where the bull turn and the long build-up have JUST started — the owner (6 Nov 2026): "find where bull and build up just
started". Per F&O stock, session by session over the last ten: the cash close, the futures OI from the bhavcopy, the
day's build-up label (price up/down with OI up/down), the board's bias at the close (the engine's fno archive), the
20-DMA. "Fresh" = the last one or two sessions are long build-up days after four that were not, or the close crossed
above the 20-DMA within the last two sessions, or the board's bias turned Bull within the last two sessions.
Usage: fresh.py OUT_DIR"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
from screen import BASE, DD, INDICES, WINDOW, archive_daily, bhav, history  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DAYS10 = ["2026-10-22", "2026-10-23", "2026-10-27", "2026-10-28", *WINDOW]  # the last ten sessions with bhavcopies


def main() -> None:
    out = Path(sys.argv[1])
    arch = pd.concat([archive_daily(d) for d in WINDOW])
    bh = pd.concat([bhav(d) for d in DAYS10]).set_index(["symbol", "day"])["oi"]
    bias = {}
    for d in WINDOW:
        f = pq.read_table(DD / "archive" / "fno" / f"{d}.parquet", columns=["symbol", "ts", "bias", "futures_state"]).to_pandas()
        f = f.dropna(subset=["bias"]).sort_values("ts").groupby("symbol").last()
        bias[d] = f
    sectors = dict(line.split("\t")[:2] for line in (Path(__file__).parent / "sectors.tsv").read_text().splitlines() if "\t" in line)
    rows = []
    for sym in sorted(arch["symbol"].unique()):
        if sym in INDICES:
            continue
        h = history(sym)
        w = arch[arch["symbol"] == sym][["day", "o", "h", "l", "c", "v"]]
        if h is None or len(h) < 30 or len(w) < 5:
            continue
        d = pd.concat([h, w]).reset_index(drop=True)
        d["dma20"] = d["c"].rolling(20).mean()
        d = d[d["day"].isin(DAYS10)].set_index("day")
        if len(d) < 8:
            continue
        labels, oi_chg, px_chg = {}, {}, {}
        days = list(d.index)
        for a, b in zip(days, days[1:], strict=False):
            try:
                o0, o1 = float(bh[(sym, a)]), float(bh[(sym, b)])
            except KeyError:
                continue
            p = d.loc[b, "c"] / d.loc[a, "c"] - 1
            o = o1 / o0 - 1 if o0 > 0 else np.nan
            oi_chg[b], px_chg[b] = o * 100, p * 100
            if np.isnan(o):
                continue
            labels[b] = ("LB" if o > 0.01 else "SC" if o < -0.01 else "flat") if p > 0 else ("SB" if o > 0.01 else "LU" if o < -0.01 else "flat")
        seq = [labels.get(x, "?") for x in days[1:]]
        last = seq[-2:]
        before = seq[-6:-2]
        fresh_lb = (last[-1] == "LB") and (sum(x == "LB" for x in last) >= 1) and (sum(x == "LB" for x in before) == 0)
        lb_days = 0
        for x in reversed(seq):
            if x == "LB":
                lb_days += 1
            else:
                break
        above = (d["c"] > d["dma20"]).tolist()
        cross = above[-1] and not above[-3] if len(above) >= 3 else False
        bs = [(bias[x].loc[sym, "bias"] if sym in bias[x].index else None) for x in WINDOW]
        bull_now = isinstance(bs[-1], str) and "Bull" in bs[-1]
        bull_turn = bull_now and not (isinstance(bs[-3], str) and "Bull" in bs[-3])
        start_day = next((days[1:][i] for i in range(len(seq) - lb_days, len(seq)) if seq[i] == "LB"), None) if lb_days else None
        oi_since = (float(bh.get((sym, days[-1]), np.nan)) / float(bh.get((sym, days[-1 - lb_days]), np.nan)) - 1) * 100 if lb_days else np.nan
        px_since = (d["c"].iloc[-1] / d["c"].iloc[-1 - lb_days] - 1) * 100 if lb_days else np.nan
        rows.append({"symbol": sym, "sector": sectors.get(sym, "?"), "close": round(d["c"].iloc[-1], 1), "seq": " ".join(seq[-6:]), "lb_days": lb_days,
                     "lb_start": start_day, "oi_since_pct": round(oi_since, 1) if not np.isnan(oi_since) else None, "px_since_pct": round(px_since, 1) if not np.isnan(px_since) else None,
                     "fresh_lb": fresh_lb, "dma20_cross": cross, "vs_dma20_pct": round((d["c"].iloc[-1] / d["dma20"].iloc[-1] - 1) * 100, 1),
                     "bias_path": " → ".join(str(x).replace("Strong ", "S") if x else "?" for x in bs[-3:]), "bull_turn": bull_turn,
                     "ret6_pct": round((d["c"].iloc[-1] / d.loc[BASE, "c"] - 1) * 100, 1) if BASE in d.index else None})
    D = pd.DataFrame(rows).set_index("symbol")
    D.to_csv(out / f"fresh-{WINDOW[-1]}.csv")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(f"== build-up by session (the last six of ten: {DAYS10[-6]} → {DAYS10[-1]}): LB long build-up, SC short covering, SB short build-up, LU long unwinding")
    f1 = D[D.fresh_lb].sort_values("oi_since_pct", ascending=False)
    print(f"\n== LONG BUILD-UP JUST STARTED (the last 1–2 sessions LB after four that were not): {len(f1)}")
    print(f1.to_string())
    f2 = D[D.dma20_cross & (D.ret6_pct > 0)].sort_values("vs_dma20_pct")
    print(f"\n== CROSSED ABOVE THE 20-DMA within the last two sessions: {len(f2)}")
    print(f2.to_string())
    f3 = D[D.bull_turn].sort_values("ret6_pct", ascending=False)
    print(f"\n== BOARD TURNED BULL within the last two sessions: {len(f3)}")
    print(f3.to_string())
    both = D[(D.fresh_lb | D.dma20_cross) & D.bull_turn]
    print(f"\n== ALL THREE FRESH (a fresh build-up or a 20-DMA cross, and the board just turned): {len(both)}")
    print(both.to_string())


if __name__ == "__main__":
    main()
