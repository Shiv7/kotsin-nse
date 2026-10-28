import sys, importlib.util
from datetime import date
from pathlib import Path
import numpy as np, pandas as pd
from kotsin_nse.market.fo_universe import load as load_members
base = "/private/tmp/claude-501/-Users-shivendrapratap-Downloads-kotsin-kotsin-nse/2bae6dc3-b199-49f6-a1aa-3825e7313e55/scratchpad/variants"
sys.argv = ["mtf_all.py", base + "/d2425", "1d"]
spec = importlib.util.spec_from_file_location("mtf_all", base + "/mtf_all.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
syms = sorted(p.stem for p in (m.DATA / "history" / "1d").glob("*.parquet") if p.stem not in m.INDEX_ROOTS and p.stem != "CRUDEOIL")
m.START, m.END = date(2024, 1, 1), date(2025, 9, 30)
d2 = m.run_tf("1d", syms); d2.to_parquet(base + "/d2425/mtf_1d.parquet", index=False)
d1 = pd.read_parquet(base + "/d2123/mtf_1d.parquet")
fm, renames = load_members(Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live"))
print("membership covers", fm.first, "…", fm.last, "| renames", renames)
E = date(1970, 1, 1).toordinal()
def prep(d, a, b):
    d = d.copy(); d["eday"] = ((d.entry_ts + 19800) // 86400).astype(int)
    d = d[(d.eday >= a.toordinal() - E) & (d.eday <= b.toordinal() - E)].copy()
    d["day_"] = d.eday.map(lambda x: date.fromordinal(E + x)); d["year"] = d.day_.map(lambda x: x.year)
    def mem(r):
        try: return fm.member(r.sym, r.day_)
        except ValueError: return None
    d["member"] = [mem(r) for r in d.itertuples()]
    return d
d1 = prep(d1, date(2021, 1, 1), date(2023, 12, 31)); d2 = prep(d2, date(2024, 1, 1), date(2025, 9, 30))
def st(x, days):
    x = x.dropna(); days = days[x.index]; net = x - 15; dm = net.groupby(days).mean()
    t = dm.mean() / dm.std() * np.sqrt(len(dm)) if len(dm) > 2 else float("nan")
    return f"n={len(x):5d} gross={x.mean():7.1f} net={net.mean():7.1f} win={100*(x>0).mean():3.0f}% t={t:5.2f}"
for label, d in (("2021-2023", d1), ("2024-2025/09", d2)):
    print(f"\n== {label}: trades {len(d)}, membership known {int(d.member.notna().sum())}, members at entry {int((d.member == True).sum())}")
    for c in ("order_block", "placebo_order_block"):
        for who, mm in (("all names", d.member.notna()), ("member on the day", d.member == True), ("not yet a member", d.member == False)):
            g = d[(d.concept == c) & mm]
            if not len(g): continue
            print(f"  {c:20s} {who:18s} 1:2 ", st(g.fixed_1to2, g.eday), "| by year", {int(y): round(float(gg.fixed_1to2.mean()) - 15, 1) for y, gg in g.groupby('year')}, "| L/S", round(float(g[g.side > 0].fixed_1to2.mean()) - 15, 1), round(float(g[g.side < 0].fixed_1to2.mean()) - 15, 1) if (g.side < 0).any() else None)
g = pd.concat([d1, d2], ignore_index=True); g = g[(g.concept == "order_block") & (g.member == True)]
print("\n== five years, members only, order block fixed 1:2:", st(g.fixed_1to2, g.eday), "| by year:", {int(y): (len(gg), round(float(gg.fixed_1to2.mean()) - 15, 1)) for y, gg in g.groupby('year')})
dm = (g.fixed_1to2 - 15).groupby(g.eday).mean(); print(f"   per-day mean net {dm.mean():.1f}, days {len(dm)}, positive days {100*(dm>0).mean():.0f}%, cum {dm.sum():.0f} bps, max drawdown {(dm.cumsum()-dm.cumsum().cummax()).min():.0f} bps; risk median {g.risk_bps.median():.0f} bps, held mean {g.fixed_1to2_bars.mean():.1f} sessions")
print("   exits:", g.fixed_1to2_how.value_counts(normalize=True).round(2).to_dict(), "| trail rule same trades:", st(g.wall_then_trail, g.eday))
pd.concat([d1, d2], ignore_index=True).to_parquet(base + "/daily_ob_5y.parquet", index=False)
