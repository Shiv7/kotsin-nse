"""The tables behind ``docs/research/2026-10-26-dayread-sweep.md``, from ``dayread_sweep`` output.

Every forward return is in bps from the close of the bar on which the event became known. "Excess"
(``x_``) is minus the unconditional forward return from the same time bucket (the ``BASE`` rows);
``x_day`` is minus the SAME DAY's cross-section at that bucket (market-adjusted). t-statistics are
clustered by calendar day: stocks move together, so the day is the independent unit. ``run`` writes
``report.txt`` and ``report.json`` into the sweep directory and returns the sections.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.dataset as pds

BASE_BARS = np.array([0, 6, 12, 24, 36, 48, 60])
HOURS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
#: an equity intraday round trip in a liquid F&O name: charges ≈ 9 bps + the engine's 5 bps slippage a fill
COST_BPS = 15.0


def load(root: Path, sub: str, kind: str) -> pd.DataFrame:
    files = sorted((root / sub).glob(f"*.{kind}.parquet"))
    if not files:
        return pd.DataFrame()
    df = pds.dataset([str(f) for f in files], format="parquet").to_table().to_pandas(strings_to_categorical=True)
    for c in df.columns:
        if isinstance(df[c].dtype, pd.CategoricalDtype):
            df[c] = df[c].astype(str)
    return df


def bucket(i: np.ndarray) -> np.ndarray:
    return BASE_BARS[np.searchsorted(BASE_BARS, i, side="right") - 1]


def clustered(df: pd.DataFrame, col: str) -> dict[str, Any]:
    """Pooled mean, the mean of per-day means and its t over days, the hit rate."""
    if df.empty or df[col].notna().sum() == 0:
        return {"n": 0, "days": 0, "mean_bps": None, "day_mean_bps": None, "t": None, "hit_pct": None}
    g = df.groupby("day")[col].mean().dropna()
    m = g.mean()
    se = g.std(ddof=1) / np.sqrt(len(g)) if len(g) > 2 else np.nan
    return {"n": int(df[col].notna().sum()), "days": len(g), "mean_bps": round(float(df[col].mean()) * 100, 1), "day_mean_bps": round(float(m) * 100, 1),
            "t": round(float(m / se), 2) if se and se == se else None, "hit_pct": round(float((df[col] > 0).mean()) * 100, 1)}


def signed(df: pd.DataFrame, col: str, side: int) -> dict[str, Any]:
    return clustered(df.assign(v=df[col] * side), "v")


def _by_hour(df: pd.DataFrame, icol: str = "i") -> pd.Series:
    return (df[icol] // 12).astype(int).map(lambda h: HOURS[min(int(h), 6)])


def run(out: Path, data_dir: Path | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {}
    lines: list[str] = []
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)

    def section(name: str, obj: Any) -> None:
        report[name] = obj.reset_index().to_dict("records") if isinstance(obj, pd.DataFrame) else obj
        lines.append(f"\n## {name}")
        lines.append(obj.to_string() if isinstance(obj, pd.DataFrame) else json.dumps(obj, indent=1, default=str))
        print(lines[-2])
        print(lines[-1])

    ev, days, legs = load(out, "hist", "events"), load(out, "hist", "days"), load(out, "hist", "legs")
    if ev.empty:
        raise SystemExit(f"no sweep output under {out}/hist")
    ev["bucket"] = bucket(ev["i"].to_numpy())
    ev["hour"] = ev["i"] // 12
    base = ev[ev["kind"] == "BASE"]
    bl = base.groupby("i")[["r30", "r60", "rcl"]].mean()
    ev = ev.merge(bl.rename(columns=lambda c: f"b_{c}"), left_on="bucket", right_index=True, how="left")
    for c in ("r30", "r60", "rcl"):
        ev[f"x_{c}"] = ev[c] - ev[f"b_{c}"]
    ctx = days[["sym", "day", "gap_pct", "prev_ret", "vs_dma20", "vs_dma50", "adr_pct"]]

    section("coverage", {
        "stock_days": len(days), "symbols": int(days["sym"].nunique()), "from": days["day"].min(), "to": days["day"].max(),
        "events_per_day": round(len(ev[ev["kind"] != "BASE"]) / len(days), 1), "legs_per_day": round(len(legs) / len(days), 1),
        "trend_day_pct(close in top/bottom 15% of range)": round(((days["range_pos"] >= 0.85) | (days["range_pos"] <= 0.15)).mean() * 100, 1),
        "day_high_in_first_hour_pct": round((days["i_high"] < 12).mean() * 100, 1), "day_low_in_first_hour_pct": round((days["i_low"] < 12).mean() * 100, 1),
        "baseline_rcl_bps_by_time": {HOURS[int(i) // 12] if i else "09:15": round(v * 100, 1) for i, v in bl["rcl"].items()},
    })
    section("opening_range", {
        "low_in_first_30min_pct": round((days["i_low"] < 6).mean() * 100, 1), "high_in_first_30min_pct": round((days["i_high"] < 6).mean() * 100, 1),
        "both_in_first_30min(inside day)_pct": round(((days["i_low"] < 6) & (days["i_high"] < 6)).mean() * 100, 1),
        "neither_pct": round(((days["i_low"] >= 6) & (days["i_high"] >= 6)).mean() * 100, 1),
        "high_by_halfhour_pct": {int(k): float(v) for k, v in (days["i_high"] // 6).value_counts(normalize=True).sort_index().mul(100).round(1).items()},
        "low_by_halfhour_pct": {int(k): float(v) for k, v in (days["i_low"] // 6).value_counts(normalize=True).sort_index().mul(100).round(1).items()},
        "low_in_first_30min|gap_down<-0.5_pct": round((days[days["gap_pct"] < -0.5]["i_low"] < 6).mean() * 100, 1),
        "high_in_first_30min|gap_up>0.5_pct": round((days[days["gap_pct"] > 0.5]["i_high"] < 6).mean() * 100, 1),
    })

    # ---- level tests: survival, then the forward return --------------------------------------------
    lv = ev[ev["kind"].isin(["SUPPORT", "RESISTANCE", "BREAK_UP", "BREAK_DOWN"])].copy()
    rows = []
    for (k, level), g in lv[lv["kind"].isin(["SUPPORT", "RESISTANCE"])].groupby(["kind", "level"]):
        first, later = g[g["i"] < 12], g[g["i"] >= 12]
        sg = 1 if k == "SUPPORT" else -1
        rows.append({"kind": k, "level": level, "n": len(g), "gave_way_30m_pct": round(g["f6"].mean() * 100, 1), "gave_way_60m_pct": round(g["f12"].mean() * 100, 1),
                     "gave_way_by_close_pct": round(g["fcl"].mean() * 100, 1), "gave_way_30m_first_hour_pct": round(first["f6"].mean() * 100, 1),
                     "gave_way_30m_later_pct": round(later["f6"].mean() * 100, 1),
                     "signed_x_rcl_bps": round((g["x_rcl"] * sg).mean() * 100, 1), "day_mean_bps": signed(g, "x_rcl", sg)["day_mean_bps"], "t": signed(g, "x_rcl", sg)["t"],
                     "hit_pct": round(((g["rcl"] * sg) > 0).mean() * 100, 1)})
    section("level_holds", pd.DataFrame(rows).set_index(["kind", "level"]))
    rows = []
    for (k, level), g in lv[lv["kind"].isin(["BREAK_UP", "BREAK_DOWN"])].groupby(["kind", "level"]):
        sg = 1 if k == "BREAK_UP" else -1
        rows.append({"kind": k, "level": level, "n": len(g), "back_through_30m_pct(false break)": round(g["f6"].mean() * 100, 1), "back_through_60m_pct": round(g["f12"].mean() * 100, 1),
                     "signed_x_rcl_bps": round((g["x_rcl"] * sg).mean() * 100, 1), "t": signed(g, "x_rcl", sg)["t"], "cont_pct": round(((g["rcl"] * sg) > 0).mean() * 100, 1)})
    section("level_breaks", pd.DataFrame(rows).set_index(["kind", "level"]))
    sv = lv[lv["kind"].isin(["SUPPORT", "RESISTANCE"]) & (lv["f6"] == 0) & lv["r30"].notna()].copy()
    sv["from6"] = ((1 + sv["rcl"] / 100) / (1 + sv["r30"] / 100) - 1) * 100
    sv["x6"] = sv["from6"] - bl["rcl"].reindex(bucket((sv["i"] + 6).to_numpy())).to_numpy()
    sv["sg"] = np.where(sv["kind"] == "SUPPORT", 1, -1)
    rows = [{"kind": k, "level": level, **signed(g, "x6", int(g["sg"].iloc[0]))} for (k, level), g in sv.groupby(["kind", "level"])]
    section("held_30m_then_forward", pd.DataFrame(rows).set_index(["kind", "level"]))
    lvc = lv[lv["kind"].isin(["SUPPORT", "RESISTANCE"])].merge(ctx, on=["sym", "day"])
    lvc["sg"] = np.where(lvc["kind"] == "SUPPORT", 1, -1)
    lvc["v"] = lvc["x_rcl"] * lvc["sg"]
    lvc["trend"] = np.where(lvc["vs_dma20"] * lvc["sg"] > 0, "with trend", "against trend")
    rows = [{"kind": k, "trend": tr, **clustered(g, "v")} for (k, tr), g in lvc.groupby(["kind", "trend"])]
    section("level_holds_by_trend", pd.DataFrame(rows).set_index(["kind", "trend"]))

    # ---- breaks of structure ------------------------------------------------------------------------
    bos = ev[ev["kind"].isin(["BOS_UP", "BOS_DOWN"])].copy()
    bos["sgn"] = np.where(bos["kind"] == "BOS_UP", 1, -1)
    bos["s_x_rcl"] = bos["x_rcl"] * bos["sgn"]
    bos["s_rcl"] = bos["rcl"] * bos["sgn"]
    bos["hr"] = _by_hour(bos)
    rows = [{"kind": k, "hour": hr, "n": len(g), "signed_x_rcl_bps": round(g["s_x_rcl"].mean() * 100, 1), "t": clustered(g, "s_x_rcl")["t"],
             "continue_pct": round((g["s_rcl"] > 0).mean() * 100, 1)} for (k, hr), g in bos.groupby(["kind", "hr"])]
    section("bos_by_hour", pd.DataFrame(rows).set_index(["kind", "hour"]))
    fb = bos.sort_values("i").drop_duplicates(["sym", "day"]).merge(days[["sym", "day", "open", "close"]], on=["sym", "day"])
    section("first_bos", {"n": len(fb), "median_bar": int(fb["i"].median()), "up_pct": round((fb["kind"] == "BOS_UP").mean() * 100, 1),
                          "agrees_with_close_vs_open_pct": round((np.sign(fb["close"] - fb["open"]) == fb["sgn"]).mean() * 100, 1), **clustered(fb, "s_x_rcl")})
    sw = ev[ev["kind"].isin(["HH", "HL", "LH", "LL"])]
    for cut in (12, 24):
        cnt = sw[sw["i"] <= cut].groupby(["sym", "day", "kind"]).size().unstack(fill_value=0).reset_index()
        for k in ("HH", "HL", "LH", "LL"):
            if k not in cnt:
                cnt[k] = 0
        cnt["cls"] = np.select([(cnt["HH"] > 0) & (cnt["HL"] > 0) & (cnt["LL"] == 0) & (cnt["LH"] == 0),
                                (cnt["LL"] > 0) & (cnt["LH"] > 0) & (cnt["HH"] == 0) & (cnt["HL"] == 0)], ["up: HH+HL only", "down: LL+LH only"], "mixed")
        m = base[base["i"] == cut][["sym", "day", "rcl", "r60"]].merge(cnt[["sym", "day", "cls"]], on=["sym", "day"], how="left").fillna({"cls": "no swings yet"})
        rows = [{"structure": cls, "days": len(g), "share_pct": round(len(g) / len(m) * 100, 1), "rcl_bps": round(g["rcl"].mean() * 100, 1), "t": clustered(g, "rcl")["t"],
                 "close_higher_pct": round((g["rcl"] > 0).mean() * 100, 1)} for cls, g in m.groupby("cls")]
        section(f"structure_known_by_{HOURS[cut // 12]}", pd.DataFrame(rows).set_index("structure"))

    # ---- the owner's sequence at bar level ----------------------------------------------------------
    sq = load(out, "hist", "seq")
    for col, icol in (("rcl_os", "i_os"), ("rcl_bos", "i_bos"), ("rcl_brk", "i_brk"), ("rcl_rt", "i_rt")):
        ok = sq[icol].notna()
        sq.loc[ok, f"x_{col}"] = sq.loc[ok, col] - bl["rcl"].reindex(bucket(sq.loc[ok, icol].to_numpy().astype(int))).to_numpy()
    for side, name in ((1, "bullish"), (-1, "bearish")):
        s = sq[sq["side"] == side]
        b = s[s["i_bos"].notna()]
        c_ = s[s["i_brk"].notna()]
        held, failed, none = s[s["result"] == "held"], s[s["result"] == "failed"], s[s["result"] == "none"]
        section(f"owner_sequence_{name}", {
            "A_open_test_under_CPR_by_0945": {**signed(s, "x_rcl_os", side), "share_of_days_pct": round(len(s) / len(days) * 100, 1), "by_level": {k: int(v) for k, v in s.groupby("lvl_os").size().items()}},
            "B_BOS_within_1h": {**signed(b, "x_rcl_bos", side), "share_of_A_pct": round(len(b) / max(1, len(s)) * 100, 1), "median_bar": float(b["i_bos"].median()) if len(b) else None},
            "C_close_through_CPR": {**signed(c_, "x_rcl_brk", side), "share_of_B_pct": round(len(c_) / max(1, len(b)) * 100, 1), "median_bar": float(c_["i_brk"].median()) if len(c_) else None},
            "D_first_return_to_CPR": {
                "held": len(held), "failed": len(failed), "never_returned": len(none),
                "held_still_holding_30m_later_pct": round(held["surv6"].mean() * 100, 1) if len(held) else None,
                "median_retest_bar": float(held["i_rt"].median()) if len(held) else None,
                "held_forward": {**signed(held, "x_rcl_rt", side), "r60_signed_bps": round((held["r60_rt"] * side).mean() * 100, 1) if len(held) else None,
                                 "mfe_bps": round((held["mfe_rt"] if side > 0 else -held["mae_rt"]).mean() * 100, 1) if len(held) else None,
                                 "mae_bps": round((held["mae_rt"] if side > 0 else -held["mfe_rt"]).mean() * 100, 1) if len(held) else None},
                "failed_forward": signed(failed, "x_rcl_rt", side),
                "never_returned_forward_from_break(hindsight)": signed(none, "x_rcl_brk", side),
            },
        })
        hm = held.merge(ctx, on=["sym", "day"])
        rows = []
        for cname, mask in (("above 20-DMA", hm["vs_dma20"] > 0), ("below 20-DMA", hm["vs_dma20"] <= 0), ("above 50-DMA", hm["vs_dma50"] > 0), ("below 50-DMA", hm["vs_dma50"] <= 0),
                            ("prev day up", hm["prev_ret"] > 0), ("prev day down", hm["prev_ret"] <= 0), ("gap down <-0.3%", hm["gap_pct"] < -0.3),
                            ("gap flat", hm["gap_pct"].abs() <= 0.3), ("gap up >0.3%", hm["gap_pct"] > 0.3), ("with the trend (20-DMA)", hm["vs_dma20"] * side > 0),
                            ("against the trend (20-DMA)", hm["vs_dma20"] * side <= 0)):
            rows.append({"context": cname, **signed(hm[mask], "x_rcl_rt", side)})
        section(f"owner_sequence_{name}_held_by_context", pd.DataFrame(rows).set_index("context"))
        if len(held):
            hm["hour"] = _by_hour(hm, "i_rt")
            rows = [{"hour": hr, **signed(g, "x_rcl_rt", side)} for hr, g in hm.groupby("hour")]
            section(f"owner_sequence_{name}_held_by_hour", pd.DataFrame(rows).set_index("hour"))

    # ---- gaps, initial balance, legs ----------------------------------------------------------------
    days["gap_b"] = pd.cut(days["gap_pct"], [-99, -1, -0.3, 0.3, 1, 99], labels=["<-1%", "-1..-0.3", "flat", "0.3..1", ">1%"])
    days["co"] = (days["close"] / days["open"] - 1) * 100
    rows = [{"gap": gb, "days": len(g), "close>open_pct": round((g["co"] > 0).mean() * 100, 1), "open_to_close_bps": round(g["co"].mean() * 100, 1)} for gb, g in days.groupby("gap_b", observed=True)]
    section("gaps", pd.DataFrame(rows).set_index("gap"))
    ib = days[days["ib_break"] != ""]
    section("initial_balance", {
        "days_with_break_pct": round(len(ib) / len(days) * 100, 1), "break_up_share_pct": round((ib["ib_break"] == "up").mean() * 100, 1),
        "close_beyond_IBH|break_up_pct": round((ib[ib["ib_break"] == "up"]["close_vs_ib"] == 1).mean() * 100, 1),
        "close_beyond_IBL|break_down_pct": round((ib[ib["ib_break"] == "down"]["close_vs_ib"] == -1).mean() * 100, 1),
    })
    lg = legs.merge(days[["sym", "day", "adr", "close"]], on=["sym", "day"])
    lg["pct_adr"] = lg["pct"].abs() / (lg["adr"] / lg["close"] * 100)
    closed = lg[lg["closed"] & lg["next_pct"].notna()].copy()
    closed["retrace"] = -closed["next_pct"] / closed["pct"]
    ht = load(out, "hist", "legs_htf")
    ht["retrace"] = -ht["next_pct"] / ht["pct"]
    rows = [{"tf": "5m", "n": len(closed), "median_retrace_pct": round(closed["retrace"].median() * 100), "retrace>50%_pct": round((closed["retrace"] > 0.5).mean() * 100, 1), "retrace>100%_pct": round((closed["retrace"] > 1).mean() * 100, 1)}]
    rows += [{"tf": tf, "n": len(g), "median_retrace_pct": round(g["retrace"].median() * 100), "retrace>50%_pct": round((g["retrace"] > 0.5).mean() * 100, 1), "retrace>100%_pct": round((g["retrace"] > 1).mean() * 100, 1)} for tf, g in ht.groupby("tf")]
    section("structure_noise_by_timeframe", pd.DataFrame(rows).set_index("tf"))
    section("legs_per_day", {"median": float(days["n_legs"].median()), "p10": float(days["n_legs"].quantile(0.1)), "p90": float(days["n_legs"].quantile(0.9)),
                             "largest_leg_pct_of_ADR_median": round(float(lg.groupby(["sym", "day"])["pct_adr"].max().median()) * 100)})

    # ---- outer pivots, honestly: market-adjusted, the crowd as known at the time, the Nifty so far ----
    sel = ev[(((ev["kind"] == "SUPPORT") & ev["level"].isin(["S1", "S2"])) | ((ev["kind"] == "RESISTANCE") & ev["level"].isin(["R1", "R2"]))) & ~ev["sym"].isin(INDICES)].copy()
    sel["sg"] = np.where(sel["kind"] == "SUPPORT", 1, -1)
    bd = base[~base["sym"].isin(INDICES)].groupby(["day", "i"])["rcl"].mean().rename("bd_rcl")
    sel = sel.merge(bd, left_on=["day", "bucket"], right_index=True, how="left")
    sel["x_day"] = (sel["rcl"] - sel["bd_rcl"]) * sel["sg"]
    sel["raw"] = sel["rcl"] * sel["sg"]
    sel["net"] = sel["raw"] - COST_BPS / 100
    sel = sel.sort_values(["day", "kind", "i"])
    sel["day_total"] = sel.groupby(["day", "kind"])["sym"].transform("nunique")
    sel["seen_sofar"] = sel.groupby(["day", "kind"]).cumcount() + 1
    first = sel.sort_values("i").drop_duplicates(["sym", "day", "kind"]).copy()
    nifty_p = (data_dir / "history" / "5m" / "NIFTY.parquet") if data_dir else None
    if nifty_p and nifty_p.exists():
        nf = pd.read_parquet(nifty_p).drop_duplicates("ts").sort_values("ts")
        ist = nf["ts"].to_numpy(np.int64) + 19800
        secs = ist % 86400
        mask = (secs >= 9 * 3600 + 15 * 60) & (secs < 15 * 3600 + 30 * 60)
        nf = nf[mask].copy()
        nf["day"] = pd.to_datetime((ist[mask] // 86400) * 86400, unit="s").strftime("%Y-%m-%d")
        nf["i"] = ((secs[mask] - (9 * 3600 + 15 * 60)) // 300).astype(int)
        nf["nifty_sofar"] = (nf["c"] / nf.groupby("day")["o"].transform("first") - 1) * 100
        first = first.merge(nf[["day", "i", "nifty_sofar"]], on=["day", "i"], how="left")
    else:
        first["nifty_sofar"] = np.nan
    first["crowd_total(hindsight)"] = pd.cut(first["day_total"], [0, 10, 30, 80, 999], labels=["1-10", "11-30", "31-80", "81+"])
    first["crowd_so_far(known)"] = pd.cut(first["seen_sofar"], [0, 5, 15, 40, 9999], labels=["1-5", "6-15", "16-40", "41+"])
    first["nifty_so_far(known)"] = pd.cut(first["nifty_sofar"], [-99, -0.5, -0.15, 0.15, 0.5, 99], labels=["<-0.5%", "-0.5..-0.15", "flat", "0.15..0.5", ">0.5%"])
    for by in ("crowd_total(hindsight)", "crowd_so_far(known)", "nifty_so_far(known)"):
        rows = []
        for (k, key), g in first.groupby(["kind", by], observed=True):
            rows.append({"kind": k, by: key, **clustered(g, "x_day"), "raw_bps": round(g["raw"].mean() * 100, 1), "net_bps": round(g["net"].mean() * 100, 1)})
        section(f"outer_pivots_by_{by}", pd.DataFrame(rows).set_index(["kind", by]))
    alone = first[(first["seen_sofar"] <= 15) & (first["nifty_sofar"].abs() < 0.5)]
    rows = []
    for k, g in alone.groupby("kind"):
        pnl = g.groupby("day")["net"].mean()
        rows.append({"kind": k, **clustered(g, "x_day"), "raw_bps": round(g["raw"].mean() * 100, 1), "net_bps": round(g["net"].mean() * 100, 1),
                     "daily_net_t": round(float(pnl.mean() / pnl.std() * np.sqrt(len(pnl))), 2) if len(pnl) > 2 else None, "events_per_day": round(float(g.groupby("day").size().mean()), 1),
                     "gave_way_30m_pct": round(g["f6"].mean() * 100, 1), "gave_way_by_close_pct": round(g["fcl"].mean() * 100, 1)})
    section("outer_pivots_alone_rule(at most 15 seen so far, |Nifty so far| < 0.5%)", pd.DataFrame(rows).set_index("kind"))

    # ---- OI on the archive days (description) -------------------------------------------------------
    al, ad = load(out, "arch", "legs"), load(out, "arch", "days")
    if not al.empty:
        al["hr"] = _by_hour(al, "i1")
        cl = al[al["closed"] & (al["build_up"] != "")]
        rows = [{"dir": dr, "build_up": b, "n": len(g), "share_pct": round(len(g) / len(cl[cl["dir"] == dr]) * 100, 1), "median_leg_pct": round(float(g["pct"].abs().median()), 2),
                 "next_leg_retrace_median_pct": round(float((-g["next_pct"] / g["pct"]).median()) * 100)} for (dr, b), g in cl.groupby(["dir", "build_up"])]
        section("oi_legs(3 days, description)", pd.DataFrame(rows).set_index(["dir", "build_up"]))
        for dr in ("up", "down"):
            mix = cl[cl["dir"] == dr].groupby(["hr", "build_up"]).size().unstack(fill_value=0)
            section(f"oi_{dr}_leg_mix_by_hour_pct", (mix.div(mix.sum(axis=1), axis=0) * 100).round(0))
        section("oi_day_build_mix", ad.groupby(["day", "day_build"]).size().unstack(fill_value=0))

    (out / "report.txt").write_text("\n".join(lines))
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    return report
