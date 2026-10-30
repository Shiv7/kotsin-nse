"""T4 — the base-rule instances on the dev window split by NIFTY's position in its day range at the entry bar
(pre-registered 2026-10-30): against = short with position ≤ 0.2 or long with position ≥ 0.8.

Instances: variants/dev/instances.parquet (7,138, base rule, 2024-01-01 → 2025-09-30). NIFTY: the 5m research
cache. Net = base_pnl − 15 bps. t by day.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

S = Path("/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants")
IST = timezone(timedelta(hours=5, minutes=30))
inst = pd.read_parquet(S / "dev" / "instances.parquet")
inst = inst[inst["base_pnl"].notna()].copy()
inst["net"] = inst["base_pnl"] - 15
n = pd.read_parquet("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live/history/5m/NIFTY.parquet").sort_values("ts")
n["day"] = n["ts"].map(lambda t: datetime.fromtimestamp(int(t), IST).date().isoformat())
n["hm"] = n["ts"].map(lambda t: datetime.fromtimestamp(int(t), IST).strftime("%H:%M"))
n["hi"] = n.groupby("day")["h"].cummax()
n["lo"] = n.groupby("day")["l"].cummin()
n["pos"] = (n["c"] - n["lo"]) / (n["hi"] - n["lo"]).replace(0, np.nan)
pos = n.set_index(["day", "hm"])["pos"]
inst["day"] = inst["day"].astype(str)
inst["npos"] = [pos.get((d, h), np.nan) for d, h in zip(inst["day"], inst["j_hm"])]
print(f"instances {len(inst)}, with NIFTY position {inst['npos'].notna().sum()}")
k = inst.dropna(subset=["npos"]).copy()
k["against"] = ((k["side"] < 0) & (k["npos"] <= 0.2)) | ((k["side"] > 0) & (k["npos"] >= 0.8))
k["year"] = k["day"].str[:4]


def score(d: pd.DataFrame, tag: str) -> None:
    byday = d.groupby("day")["net"].mean()
    t = byday.mean() / (byday.std() / np.sqrt(len(byday))) if len(byday) > 1 else float("nan")
    yrs = d.groupby("year")["net"].mean().round(1).to_dict()
    print(f"  {tag:34} n {len(d):5}  net {d['net'].mean():+6.1f}  win {(d['net'] > 0).mean():.0%}  t {t:+.2f}  years {yrs}  exits {d['base_how'].value_counts().to_dict()}")


print("\n== baseline vs the pre-registered split")
score(k, "all")
score(k[~k["against"]], "kept (with / neutral)")
score(k[k["against"]], "dropped (against: at NIFTY's extreme)")
print("\n== by side")
for side, name in ((1, "long"), (-1, "short")):
    d = k[k["side"] == side]
    score(d, f"{name} all")
    score(d[~d["against"]], f"{name} kept")
    score(d[d["against"]], f"{name} against")
print("\n== NIFTY position deciles at the entry (shorts: low position = NIFTY at its low)")
k["bucket"] = pd.cut(k["npos"], [0, 0.2, 0.4, 0.6, 0.8, 1.0], include_lowest=True)
print(k.groupby(["side", "bucket"], observed=True)["net"].agg(["size", "mean"]).round(1).to_string())
d = k[k["against"]]
ok = len(d) >= 100 and d["net"].mean() <= -10
kept, allm = k[~k["against"]], k
byday = lambda x: x.groupby("day")["net"].mean()
tk, ta = byday(kept).mean() / (byday(kept).std() / np.sqrt(byday(kept).size)), byday(allm).mean() / (byday(allm).std() / np.sqrt(byday(allm).size))
yrs_k, yrs_a = kept.groupby("year")["net"].mean(), allm.groupby("year")["net"].mean()
print(f"\n== decision bar: dropped slice ≥100 and ≤ −10: {ok}; kept − all = {kept['net'].mean() - allm['net'].mean():+.1f} bps (≥ 3?), t {tk:+.2f} vs {ta:+.2f} (≥ +0.3?), years {(yrs_k - yrs_a).round(1).to_dict()} (both same sign?)")
