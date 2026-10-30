"""T1 placebo: the same cross-sectional rank IC with each reading shuffled within its minute (3 full days)."""
from datetime import date
from pathlib import Path
import numpy as np, pandas as pd
from kotsin_nse.research.micro_ic import day_frame, SIGNALS
rng = np.random.default_rng(7)
archive = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/archive")
sigs = ["wimb", "imbalance_top5", "micro_bps", "qi", "ofi_per_depth", "delta_share"]
rows = []
for d in (date(2026, 10, 27), date(2026, 10, 29), date(2026, 10, 30)):
    f = day_frame(archive, d)
    if f is None:
        continue
    cols = [c for c in f.columns if c.startswith("fwd_") or c.startswith("ret_")]
    print(d, "frame", f.shape, "forward cols", cols[:6], "sig cols present", [s for s in sigs if s in f.columns])
    for h in (5, 30):
        ycol = next((c for c in f.columns if c.endswith(f"_{h}") or c == f"fwd{h}" or c == f"ret_{h}" or c == f"fwd_{h}"), None)
        if ycol is None:
            continue
        for s in sigs:
            if s not in f.columns:
                continue
            real, fake = [], []
            for _, g in f.dropna(subset=[s, ycol]).groupby("ts"):
                if len(g) < 30:
                    continue
                real.append(g[s].rank().corr(g[ycol].rank()))
                fake.append(pd.Series(rng.permutation(g[s].values)).rank().corr(g[ycol].rank(numeric_only=True).reset_index(drop=True)))
            rows.append({"day": str(d), "sig": s, "h": h, "ic": np.nanmean(real), "ic_t": np.nanmean(real) / (np.nanstd(real) / np.sqrt(len(real))), "placebo_ic": np.nanmean(fake), "placebo_t": np.nanmean(fake) / (np.nanstd(fake) / np.sqrt(len(fake))), "minutes": len(real)})
r = pd.DataFrame(rows)
print(r.round(4).to_string(index=False))
print("\n== pooled over the three days")
print(r.groupby(["sig", "h"]).agg(ic=("ic", "mean"), placebo_ic=("placebo_ic", "mean"), min_ic_t=("ic_t", "min"), max_placebo_t=("placebo_t", lambda x: x.abs().max())).round(4).to_string())
