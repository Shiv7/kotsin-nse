"""The trader's stack on every F&O stock for one archived day (owner, 3 Nov 04:10: "do at least 50 companies the same
process … check where the 1 hour, 4 hour, daily, weekly, monthly candle had a retest and volume node, then tell me what we get").
Per stock: the opening flush and its absorption, the first pullback and what it held above, the futures state and the put
wall's move in the first half hour, OI building through the midday balance, the day's volume nodes; and the higher frames
from the history cache — 1h / 4h / daily / weekly / monthly swings and order blocks, the 20- and 60-session volume nodes —
and which of them the day's open, low and first pullback sat on. Then the names grouped by how the day ended.
Usage: stack_day.py OUT_DIR 2026-11-02
"""
from __future__ import annotations

import datetime
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from level_retest import bars_by_day  # noqa: E402

from kotsin_nse.instrument.universe import INDEX_ROOTS  # noqa: E402
from kotsin_nse.scan.replay import guarded_adr  # noqa: E402
from kotsin_nse.strategy import cpr_retest as cr  # noqa: E402

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def hm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def swings(h: np.ndarray, lo: np.ndarray, side: int = 2) -> tuple[list[int], list[int]]:
    hi, li = [], []
    for i in range(side, len(h) - side):
        if h[i] == h[i - side : i + side + 1].max():
            hi.append(i)
        if lo[i] == lo[i - side : i + side + 1].min():
            li.append(i)
    return hi, li


def order_blocks(o, h, lo, c, look: int = 10) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Bullish OB = the last down candle before a close above the prior ``look``-bar high; bearish mirrored. Zones (low, high)."""
    bull, bear = [], []
    n = len(c)
    for i in range(look + 1, n):
        if c[i] > h[i - look : i].max():
            k = next((j for j in range(i - 1, max(i - 6, 0), -1) if c[j] < o[j]), None)
            if k is not None:
                bull.append((float(lo[k]), float(h[k])))
        if c[i] < lo[i - look : i].min():
            k = next((j for j in range(i - 1, max(i - 6, 0), -1) if c[j] > o[j]), None)
            if k is not None:
                bear.append((float(lo[k]), float(h[k])))
    return bull, bear


def resample(df: pd.DataFrame, sec: int) -> pd.DataFrame:
    d = df.copy()
    d["s"] = ((d.ts + 19800) // sec) * sec - 19800
    return d.groupby("s").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index().rename(columns={"s": "ts"})


def nodes(df: pd.DataFrame, px: float, k: int = 3) -> list[float]:
    """The ``k`` highest-volume price bins (0.25 % of price wide) of the bars given."""
    if df.empty:
        return []
    w = max(px * 0.0025, 0.05)
    b = (df.c // w) * w
    vp = df.groupby(b).v.sum().sort_values(ascending=False)
    return [float(x) + w / 2 for x in vp.index[:k]]


_ARCH: dict[str, dict[str, pd.DataFrame]] = {}


def archive_day(d: str) -> dict[str, pd.DataFrame]:
    """The feed's 1m archive of a session (continuous trading only), by symbol — the history cache ends 28 Oct 2026, so
    the sessions after it come from here for the higher frames and the previous day's levels."""
    if d not in _ARCH:
        a = pd.read_parquet(DD / "archive" / "bars" / f"{d}.parquet")
        a = a[a["source"] != "partial"]
        a = a[[hm(x) < "15:15" for x in a.ts]]
        _ARCH[d] = {sym: g.sort_values("ts")[["ts", "o", "h", "l", "c", "v"]] for sym, g in a.groupby("symbol")}
    return _ARCH[d]


ARCH_DAYS = ["2026-10-27", "2026-10-29", "2026-10-30", "2026-11-02"]


def htf_levels(sym: str, day: date, px: float) -> tuple[list[tuple[str, str, float]], dict]:
    """Every higher-frame reference before ``day``: (tf, name, price)."""
    out: list[tuple[str, str, float]] = []
    info: dict = {}
    p5 = DD / "history" / "5m" / f"{sym}.parquet"
    p1 = DD / "history" / "1d" / f"{sym}.parquet"
    if not p1.exists():
        return out, info
    d1 = pd.read_parquet(p1).drop_duplicates("ts", keep="last").sort_values("ts")
    d1["day"] = [date.fromordinal(date(1970, 1, 1).toordinal() + int((t + 19800) // 86400)) for t in d1.ts]
    d1 = d1[(d1.day < day) & (d1.h > 0)].drop_duplicates("day", keep="last")
    extra5 = []
    for ad in ARCH_DAYS:
        add = date.fromisoformat(ad)
        if add >= day or add in set(d1.day):
            continue
        g = archive_day(ad).get(sym)
        if g is None or g.empty:
            continue
        d1 = pd.concat([d1, pd.DataFrame([{"ts": int(g.ts.iloc[0]), "o": float(g.o.iloc[0]), "h": float(g.h.max()), "l": float(g.l.min()), "c": float(g.c.iloc[-1]), "v": float(g.v.sum()), "day": add}])], ignore_index=True)
        extra5.append(resample(g, 300))
    d1 = d1.sort_values("ts")
    if len(d1) < 30:
        return out, info
    # daily
    dd = d1.tail(120).reset_index(drop=True)
    hi, li = swings(dd.h.to_numpy(), dd.l.to_numpy())
    out += [("daily", "swing_high", float(dd.h[i])) for i in hi[-5:]] + [("daily", "swing_low", float(dd.l[i])) for i in li[-5:]]
    bull, bear = order_blocks(dd.o.to_numpy(), dd.h.to_numpy(), dd.l.to_numpy(), dd.c.to_numpy())
    out += [("daily", "ob_bull", (a + b) / 2) for a, b in bull[-2:]] + [("daily", "ob_bear", (a + b) / 2) for a, b in bear[-2:]]
    out += [("daily", "dma20", float(dd.c.tail(20).mean())), ("daily", "dma50", float(dd.c.tail(50).mean()))]
    prev = dd.iloc[-1]
    lv = cr.day_levels(float(prev.h), float(prev.l), float(prev.c))
    out += [("daily", k, float(v)) for k, v in lv.items() if k in ("P", "TC", "BC", "R1", "S1", "R2", "S2", "PDH", "PDL")]
    info["prev_close"] = float(prev.c)
    info["adr"] = guarded_adr(dd.h.tail(60).tolist(), dd.l.tail(60).tolist(), dd.c.tail(60).tolist()) or px * 0.02
    info["above_dma20"] = px > out[-0 - 1][2] if False else px > float(dd.c.tail(20).mean())
    info["ret5_pct"] = (float(dd.c.iloc[-1]) / float(dd.c.iloc[-6]) - 1) * 100 if len(dd) > 6 else np.nan
    info["ret20_pct"] = (float(dd.c.iloc[-1]) / float(dd.c.iloc[-21]) - 1) * 100 if len(dd) > 21 else np.nan
    info["d_high_60"] = float(dd.h.tail(60).max())
    # weekly and monthly from the daily
    dd["wk"] = [d.isocalendar()[:2] for d in dd.day]
    wk = dd.groupby("wk").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index(drop=True)
    if len(wk) >= 8:
        hi, li = swings(wk.h.to_numpy(), wk.l.to_numpy())
        out += [("weekly", "swing_high", float(wk.h[i])) for i in hi[-3:]] + [("weekly", "swing_low", float(wk.l[i])) for i in li[-3:]]
        bull, bear = order_blocks(wk.o.to_numpy(), wk.h.to_numpy(), wk.l.to_numpy(), wk.c.to_numpy(), look=4)
        out += [("weekly", "ob_bull", (a + b) / 2) for a, b in bull[-1:]] + [("weekly", "ob_bear", (a + b) / 2) for a, b in bear[-1:]]
        w = wk.iloc[-1]
        wp = cr.weekly_pivots([(float(w.h), float(w.l), float(w.c))]) or {}
        out += [("weekly", k, float(v)) for k, v in wp.items()]
        out += [("weekly", "prev_week_high", float(w.h)), ("weekly", "prev_week_low", float(w.l))]
    dd["mo"] = [(d.year, d.month) for d in dd.day]
    mo = dd.groupby("mo").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last")).reset_index(drop=True)
    if len(mo) >= 3:
        m = mo.iloc[-1]
        out += [("monthly", "prev_month_high", float(m.h)), ("monthly", "prev_month_low", float(m.l)), ("monthly", "P", (float(m.h) + float(m.l) + float(m.c)) / 3)]
        out += [("monthly", "high_6m", float(mo.h.tail(6).max())), ("monthly", "low_6m", float(mo.l.tail(6).min()))]
    # 1h and 4h from the 5m cache, and the volume nodes
    if p5.exists():
        f5 = pd.read_parquet(p5)
        f5 = f5[(f5.ts + 19800) // 86400 < (date(day.year, day.month, day.day).toordinal() - date(1970, 1, 1).toordinal())]
        if extra5:
            f5 = pd.concat([f5, *extra5], ignore_index=True).drop_duplicates("ts", keep="last").sort_values("ts")
        f5 = f5.tail(75 * 60)
        h1 = resample(f5.tail(75 * 15), 3600)
        if len(h1) > 20:
            hi, li = swings(h1.h.to_numpy(), h1.l.to_numpy())
            out += [("1h", "swing_high", float(h1.h[i])) for i in hi[-4:]] + [("1h", "swing_low", float(h1.l[i])) for i in li[-4:]]
            bull, bear = order_blocks(h1.o.to_numpy(), h1.h.to_numpy(), h1.l.to_numpy(), h1.c.to_numpy())
            out += [("1h", "ob_bull", (a + b) / 2) for a, b in bull[-2:]] + [("1h", "ob_bear", (a + b) / 2) for a, b in bear[-2:]]
        h4 = resample(f5.tail(75 * 40), 4 * 3600)
        if len(h4) > 20:
            hi, li = swings(h4.h.to_numpy(), h4.l.to_numpy())
            out += [("4h", "swing_high", float(h4.h[i])) for i in hi[-3:]] + [("4h", "swing_low", float(h4.l[i])) for i in li[-3:]]
            bull, bear = order_blocks(h4.o.to_numpy(), h4.h.to_numpy(), h4.l.to_numpy(), h4.c.to_numpy())
            out += [("4h", "ob_bull", (a + b) / 2) for a, b in bull[-2:]] + [("4h", "ob_bear", (a + b) / 2) for a, b in bear[-2:]]
        out += [("node20", f"hvn{i + 1}", v) for i, v in enumerate(nodes(f5.tail(75 * 20), px))]
        out += [("node60", f"hvn{i + 1}", v) for i, v in enumerate(nodes(f5.tail(75 * 60), px))]
    return out, info


def nearest(levels, px: float, adr: float, within: float = 0.25):
    best = None
    for tf, name, p in levels:
        d = abs(px - p) / adr
        if d <= within and (best is None or d < best[2]):
            best = (tf, name, d)
    return best


def main() -> None:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    D = sys.argv[2]; day = date.fromisoformat(D)
    b = pd.read_parquet(DD / "archive" / "bars" / f"{D}.parquet"); b["hm"] = [hm(x) for x in b.ts]; b = b[b.hm < "15:15"]
    fo = pd.read_parquet(DD / "archive" / "fno" / f"{D}.parquet"); fo["hm"] = [hm(x) for x in fo.ts]
    rows = []
    for sym, g in b.groupby("symbol"):
        if sym in INDEX_ROOTS:
            continue
        g = g.sort_values("ts")
        m1 = g[["ts", "o", "h", "l", "c", "v"]]
        m5 = resample(m1, 300); m15 = resample(m1, 900)
        m5["hm"] = [hm(x) for x in m5.ts]
        if len(m5) < 60:
            continue
        o0 = float(m5.o.iloc[0]); close = float(m5.c.iloc[-1]); dh, dl = float(m5.h.max()), float(m5.l.min())
        levels, info = htf_levels(sym, day, o0)
        if not info:
            continue
        adr = info["adr"]; pc = info["prev_close"]
        # the direction as a trader sees it at the first pullback: the 10:00 close against the open (no hindsight)
        c10 = float(m5[m5.hm <= "09:55"].c.iloc[-1])
        sd = 1 if c10 >= o0 else -1
        up = sd > 0
        oc = (close / o0 - 1) * 100
        fwd = (close - c10) / o0 * 100 * sd  # from 10:00 to the close, in the 10:00 direction
        gap = (o0 / pc - 1) * 100
        # the flush and its absorption: the first 15m bar
        f = m15.iloc[0]
        f_rng = (f.h - f.l) / adr
        f_pos = (f.c - f.l) / (f.h - f.l) if f.h > f.l else 0.5
        f_vol_share = float(f.v / m1.v.sum()) if m1.v.sum() > 0 else np.nan
        # first pullback: after the first 5m swing high (up) / low (down) in bars 2..20, the next swing low (high)
        h5, l5, c5 = m5.h.to_numpy(), m5.l.to_numpy(), m5.c.to_numpy()
        hi, li = swings(h5, l5)
        pb = None
        if up:
            fh = next((i for i in hi if 2 <= i <= 24), None)
            if fh is not None:
                nl = next((i for i in li if i > fh and i <= fh + 24), None)
                if nl is not None:
                    pb = (nl, float(l5[nl]))
        else:
            fl = next((i for i in li if 2 <= i <= 24), None)
            if fl is not None:
                nh = next((i for i in hi if i > fl and i <= fl + 24), None)
                if nh is not None:
                    pb = (nh, float(h5[nh]))
        pb_px = pb[1] if pb else np.nan
        pb_held = (pb_px > f.l) if (pb and up) else ((pb_px < f.h) if pb else np.nan)
        poc_first15 = nodes(m1[m1.ts < m1.ts.iloc[0] + 900], o0, 1)
        poc15 = poc_first15[0] if poc_first15 else np.nan
        pb_above_poc = (pb_px > poc15) if (pb and up and poc_first15) else ((pb_px < poc15) if (pb and poc_first15) else np.nan)
        day_nodes = nodes(m1, o0, 3)
        # the board
        fs = fo[fo.symbol == sym].sort_values("ts")
        def at(t):
            x = fs[fs.hm <= t]
            return x.iloc[-1] if len(x) else None
        a0, a45, a11, a14, a15 = at("09:18"), at("09:45"), at("11:00"), at("14:00"), at("15:14")
        fut_with_0945 = (a45.futures_state == ("LB" if up else "SB")) if a45 is not None else np.nan
        fut_with_1100 = (a11.futures_state == ("LB" if up else "SB")) if a11 is not None else np.nan
        wall_step = np.nan
        if a0 is not None and a11 is not None:
            wall_step = (a11.x_put_wall > a0.x_put_wall) if up else (a11.x_call_wall < a0.x_call_wall)
        oi_bal = np.nan; px_bal = np.nan
        if a11 is not None and a14 is not None and a11.fut_oi and a14.fut_oi:
            oi_bal = (a14.fut_oi / a11.fut_oi - 1) * 100 * sd
            px_bal = abs(float(m5[m5.ts <= a14.ts].c.iloc[-1]) / float(m5[m5.ts <= a11.ts].c.iloc[-1]) - 1) * 100 / (adr / o0 * 100)
        pcr0 = a0.x_pcr_oi if a0 is not None else np.nan; pcr1 = a15.x_pcr_oi if a15 is not None else np.nan
        # where the day's key prices sat on the higher frames
        key_low = dl if up else dh
        n_open = nearest(levels, o0, adr); n_low = nearest(levels, key_low, adr); n_pb = nearest(levels, pb_px, adr) if pb else None
        # breakout context: how many HTF swing highs (lows) the open already sat beyond
        sh = [p for tf, n, p in levels if n == "swing_high"]; sl = [p for tf, n, p in levels if n == "swing_low"]
        beyond = (sum(1 for p in sh if o0 > p) / len(sh)) if (up and sh) else ((sum(1 for p in sl if o0 < p) / len(sl)) if sl else np.nan)
        rng_ = np.random.default_rng(7)
        rp = rng_.uniform(dl, dh, 20)
        density = float(np.mean([nearest(levels, float(x), adr) is not None for x in rp]))
        rows.append({"sym": sym, "oc_pct": oc, "fwd_pct": fwd, "dir10": sd, "mv10_pct": (c10 / o0 - 1) * 100 * sd, "density": density,
                     "gap_pct": gap, "range_adr": (dh - dl) / adr, "close_pos": (close - dl) / (dh - dl) if dh > dl else 0.5,
                     "late_pct": (close / float(m5[m5.hm <= "14:00"].c.iloc[-1]) - 1) * 100 * sd if len(m5[m5.hm <= "14:00"]) else np.nan,
                     "flush_rng_adr": f_rng, "flush_abs": (f_pos >= 0.6) if up else (f_pos <= 0.4), "flush_vol_share": f_vol_share,
                     "pb_held": pb_held, "pb_above_poc": pb_above_poc, "pb_hm": hm(m5.ts.iloc[pb[0]]) if pb else "",
                     "fut_with_0945": fut_with_0945, "fut_with_1100": fut_with_1100, "wall_step": wall_step, "oi_bal_with": oi_bal, "px_bal_adr": px_bal,
                     "oi_build_flat": (oi_bal > 0.3 and px_bal < 0.3) if not (np.isnan(oi_bal) or np.isnan(px_bal)) else np.nan,
                     "pcr_move": (pcr1 - pcr0) * sd if not (np.isnan(pcr0) or np.isnan(pcr1)) else np.nan,
                     "open_on": f"{n_open[0]}:{n_open[1]}" if n_open else "", "low_on": f"{n_low[0]}:{n_low[1]}" if n_low else "", "pb_on": f"{n_pb[0]}:{n_pb[1]}" if n_pb else "",
                     "open_beyond_htf": beyond, "above_dma20": info.get("above_dma20"), "ret5": info.get("ret5_pct"), "ret20": info.get("ret20_pct"),
                     "near_60d_high": (o0 / info["d_high_60"] - 1) * 100 if info.get("d_high_60") else np.nan,
                     "nodes": ",".join(f"{x:.0f}" for x in day_nodes)})
    T = pd.DataFrame(rows)
    T.to_csv(out / f"stack-{D}.csv", index=False)
    T["grp"] = pd.cut(T.fwd_pct, [-99, -0.75, 0.5, 1.5, 99], labels=["reversed (< −0.75 %)", "flat", "continued (+0.5 to +1.5 %)", "ran (> +1.5 %)"])
    print(f"{D}: {len(T)} stocks | direction at 10:00: up {int((T.dir10 > 0).sum())} down {int((T.dir10 < 0).sum())} | from 10:00 to the close in that direction: {T.grp.value_counts().to_dict()}")
    print(f"   level density control: a random price inside the day's range sits within 0.25 ADR of some level {100 * T.density.mean():.0f} % of the time")
    cols = ["flush_abs", "pb_held", "pb_above_poc", "fut_with_0945", "fut_with_1100", "wall_step", "oi_build_flat", "above_dma20"]
    s = T.groupby("grp", observed=True)[cols].mean().mul(100).round(0)
    s["n"] = T.groupby("grp", observed=True).size()
    s["gap_pct"] = T.groupby("grp", observed=True).gap_pct.median().round(2)
    s["mv10_pct"] = T.groupby("grp", observed=True).mv10_pct.median().round(2)
    s["range_adr"] = T.groupby("grp", observed=True).range_adr.median().round(2)
    s["late_pct"] = T.groupby("grp", observed=True).late_pct.median().round(2)
    s["oi_bal_with"] = T.groupby("grp", observed=True).oi_bal_with.median().round(2)
    s["pcr_move"] = T.groupby("grp", observed=True).pcr_move.median().round(2)
    s["open_beyond_htf"] = T.groupby("grp", observed=True).open_beyond_htf.median().round(2)
    s["ret20"] = T.groupby("grp", observed=True).ret20.median().round(1)
    print("\n== share (%) of each group with the condition, and medians"); print(s.to_string())
    print("\n== where the day's extreme (low for an up day, high for a down day) sat on the higher frames, by group (within 0.25 ADR)")
    for gname, g in T.groupby("grp", observed=True):
        tf = g.low_on.str.split(":").str[0].replace("", "none").value_counts(normalize=True).mul(100).round(0)
        print(f"  {gname}: {tf.to_dict()}")
    print("\n== where the first pullback sat, by group")
    for gname, g in T.groupby("grp", observed=True):
        tf = g.pb_on.str.split(":").str[0].replace("", "none").value_counts(normalize=True).mul(100).round(0)
        print(f"  {gname}: {tf.to_dict()}")
    print("\n== the stack (flush absorbed + futures with by 09:45 + pullback held above the first-15-min POC): share and outcome")
    T["stack3"] = T.flush_abs.astype(bool) & T.fut_with_0945.astype(bool) & T.pb_above_poc.astype(bool)
    for k, g in T.groupby("stack3"):
        print(f"  stack {k}: n {len(g)}, 10:00→close in the 10:00 direction: median {g.fwd_pct.median():+.2f} %, mean {g.fwd_pct.mean():+.2f} %, share > +0.5 %: {100 * (g.fwd_pct > 0.5).mean():.0f} %, reversed < −0.75 %: {100 * (g.fwd_pct < -0.75).mean():.0f} %")
    T["stack4"] = T.stack3 & (T.oi_build_flat == True)  # noqa: E712
    for k, g in T.groupby("stack4"):
        print(f"  stack+OI build through the balance {k}: n {len(g)}, 14:00→close median {g.late_pct.median():+.2f} %, share > +0.5 %: {100 * (g.late_pct > 0.5).mean():.0f} %")
    print("\n== the names with the full stack:")
    print(T[T.stack4][["sym", "dir10", "mv10_pct", "fwd_pct", "oc_pct", "late_pct", "pb_hm", "low_on", "pb_on", "oi_bal_with", "nodes"]].round(2).sort_values("fwd_pct", ascending=False).to_string(index=False))
    print("\n== the biggest movers either way:")
    cols2 = ["sym", "dir10", "mv10_pct", "fwd_pct", "flush_abs", "pb_above_poc", "fut_with_0945", "wall_step", "oi_build_flat", "low_on", "pb_on", "open_beyond_htf", "ret20"]
    print(T.sort_values("fwd_pct")[cols2].round(2).head(8).to_string(index=False))
    print(T.sort_values("fwd_pct", ascending=False)[cols2].round(2).head(12).to_string(index=False))


if __name__ == "__main__":
    main()
