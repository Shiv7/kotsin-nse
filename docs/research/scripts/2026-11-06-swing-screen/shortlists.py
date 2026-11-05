"""The swing shortlists from the screen's CSV (screen.py): the clean leaders, the long side (ahead of NIFTY, a clean move of
2–6 ADR with a pullback under 2.5 ADR, above the 20-DMA, new money in the futures or a bull board with the future in long
build-up), the short side (price down with OI up, below the 20-DMA, a bear board). Facts, not verdicts.
Usage: shortlists.py screen-<day>.csv"""
from __future__ import annotations

import sys

import pandas as pd

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)
D = pd.read_csv(sys.argv[1]).set_index("symbol")
cols = ["sector", "close", "ret6_pct", "move_adr", "rs6_pct", "up_days", "eff", "pullback_adr", "vs_dma20_pct", "vs_dma50_pct", "vs_20d_high_pct", "oi6_pct", "build_up", "vol6_over_20", "bias", "fut_state"]
clean = D[(D.eff >= 0.7) & (D.up_days >= 5) & (D.pullback_adr <= 2.0) & (D.move_adr >= 2) & (D.rs6_pct > 0)].sort_values("rs6_pct", ascending=False)
print(f"== CLEAN LEADERS (efficiency ≥ 0.7, 5+ up days, pullback ≤ 2 ADR, ≥ 2 ADR, ahead of NIFTY): {len(clean)}")
print(clean[cols].to_string())
long = D[(D.rs6_pct > 0) & (D.eff >= 0.6) & (D.move_adr.between(2, 6)) & (D.pullback_adr <= 2.5) & (D.vs_dma20_pct > 0)
         & ((D.build_up == "long build-up") | (D.bias.str.contains("Bull", na=False) & (D.fut_state == "LB")))].copy()
long["why"] = long.apply(lambda r: ("new money" if r.build_up == "long build-up" else "short covering") + (", bull board" if isinstance(r.bias, str) and "Bull" in r.bias else ""), axis=1)
long = long.sort_values(["build_up", "rs6_pct"], ascending=[True, False])
print(f"\n== LONG SIDE: {len(long)}")
print(long[cols + ["why"]].to_string())
short = D[(D.build_up == "short build-up") & (D.vs_dma20_pct < 0) & (D.bias.str.contains("Bear", na=False))].sort_values("rs6_pct")
print(f"\n== SHORT SIDE: {len(short)}")
print(short[cols].to_string())
print("\nvolume ≥ 1.3× the 20-session average on the long side:", long[long.vol6_over_20 >= 1.3].index.tolist())
print("below the 50-DMA on the long side (a bounce, not a trend):", long[long.vs_dma50_pct < 0].index.tolist())
