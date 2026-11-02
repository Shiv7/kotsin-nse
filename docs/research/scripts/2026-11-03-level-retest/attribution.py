"""What made the any-wall book lose on Tue 27 Oct and win on Mon 2 Nov — trade-level attribution over the four replayed
sessions (`option_leg_replay.py` output: <out>/<day>.json and option_legs.parquet) joined with the archives: breadth at
the entry bar (share of the universe above its open), the F&O board and the order flow at the entry minute, the option's
spread at entry. Descriptive — four days, so day-clustered; anything that looks like an edge goes to a pre-registration.
Usage: uv run python attribution.py OUT_DIR 2026-10-27 2026-10-29 2026-10-30 2026-11-02
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DD = Path("/Users/shivendrapratap/Downloads/kotsin/kotsin-nse/backend/data-live")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
COST = 13.0


def hm(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def tbl(df: pd.DataFrame, by, label: str, col: str = "pnl_bps", min_n: int = 1) -> None:
    g = df.groupby(by, observed=True)
    out = g.agg(n=(col, "size"), bps=(col, "mean"), win=(col, lambda x: 100 * (x > 0).mean()))
    if "opt_pct" in df:
        out["opt_pct"] = g.opt_pct.mean()
    out = out[out.n >= min_n]
    print(f"\n-- {label}")
    print(out.round(1).to_string())


def main() -> None:
    out = Path(sys.argv[1])
    days = sys.argv[2:]
    rows = []
    breadth = {}
    nifty = {}
    sym_code = {}
    for day in days:
        snap = json.load(open(out / f"{day}.json"))
        for t in snap["trades"]:
            tg = t.get("tags") or {}
            rows.append({"day": day, "sym": t["symbol"], "side": t["side"], "wall": t.get("wall"), "wtype": t.get("wtype"), "conf": t.get("conf"),
                         "entry_bar": t["entry_bar"], "opened_ts": t["opened_ts"], "closed_ts": t["closed_ts"], "entry": t["entry"], "risk_bps": t["risk_bps"],
                         "reward_bps": t["reward_bps"], "w1": t["target_name"], "exit_reason": t["exit_reason"], "pnl_bps": t["pnl_bps"], "net_inr": t["net_inr"],
                         "bars_held": t["bars_held"], "mfe": t["mfe_bps"], "mae": t["mae_bps"], "move_adr": tg.get("move_adr"), "nifty_move": tg.get("nifty_move_pct"),
                         "range_pos": tg.get("range_pos"), "xs_pct": tg.get("xs_pct"), "xs_top20": tg.get("xs_top20"), "rel_al": tg.get("rel_al"),
                         "st60_al": tg.get("st60_al"), "dma20_al": tg.get("dma20_al"), "ret5_pct": tg.get("ret5_pct"), "ib_al": tg.get("ib_al"), "prev_range_pos": tg.get("prev_range_pos")})
        b = pd.read_parquet(DD / "archive" / "bars" / f"{day}.parquet", columns=["symbol", "scrip_code", "ts", "o", "c"])
        b["hm"] = [hm(x) for x in b.ts]
        b = b[b.hm < "15:15"].sort_values("ts")
        sym_code.update(dict(zip(b.symbol, b.scrip_code, strict=False)))
        opn = b.groupby("symbol").o.first()
        b["slot"] = (b.ts // 300) * 300
        last = b.groupby(["symbol", "slot"]).c.last().reset_index()
        last["up"] = last.c.to_numpy() > opn.reindex(last.symbol).to_numpy()
        br = last.groupby("slot").up.mean()
        breadth[day] = br
        n = b[b.symbol == "NIFTY"]
        nifty[day] = (float(n.o.iloc[0]), float(n.c.iloc[-1]), {s: float(n[n.slot == s].c.iloc[-1]) for s in n.slot.unique() if hm(s) in ("10:15", "12:15", "14:15")})
    T = pd.DataFrame(rows)
    T["entry_slot"] = ((T.opened_ts - 300) // 300) * 300  # the entry bar's open slot
    T["breadth"] = [float(breadth[d].get(s, np.nan)) for d, s in zip(T.day, T.entry_slot, strict=False)]
    T["sd"] = np.where(T.side == "LONG", 1, -1)
    T["breadth_with"] = np.where(T.sd > 0, T.breadth, 1 - T.breadth)  # the share of the universe moving the trade's way
    T["clock"] = pd.cut(T.opened_ts - T.groupby("day").opened_ts.transform("min"), [-1, 1800, 3600, 7200, 10800, 1e9], labels=["09:30–09:45", "–10:15", "–11:15", "–12:15", "after"])
    T["risk_b"] = pd.cut(T.risk_bps, [0, 50, 80, 120, 1e9], labels=["≤50", "50–80", "80–120", ">120"])
    T["room_R"] = T.reward_bps / T.risk_bps
    T["mfe_R"] = T.mfe / T.risk_bps
    # the option legs
    L = pd.read_parquet(out / "option_legs.parquet")
    L = L[L.status == "ok"][["day", "sym", "side", "entry_bar", "code", "pct", "net_inr", "opt_entry", "opt_exit", "entry_age_s", "entry_how", "premium_inr", "leverage"]]
    L = L.rename(columns={"pct": "opt_pct", "net_inr": "opt_net", "code": "opt_code"})
    T = T.merge(L, on=["day", "sym", "side", "entry_bar"], how="left")
    # the board and the flow at the entry minute
    fb, fl = [], []
    for day in days:
        f = pd.read_parquet(DD / "archive" / "fno" / f"{day}.parquet", columns=["symbol", "ts", "bias", "score", "futures_state", "puts_state", "calls_state", "x_pcr_oi", "x_iv", "premium"]).sort_values("ts")
        w = pd.read_parquet(DD / "archive" / "flow" / f"{day}.parquet", columns=["code", "ts", "delta", "cum_delta", "ofi", "imbalance", "qi", "micro_bps", "spread_bps"]).sort_values("ts")
        sub = T[T.day == day].sort_values("opened_ts")
        m = pd.merge_asof(sub[["day", "sym", "side", "entry_bar", "opened_ts"]].sort_values("opened_ts"), f.rename(columns={"symbol": "sym", "ts": "fts"}).sort_values("fts"),
                          left_on="opened_ts", right_on="fts", by="sym", direction="backward", tolerance=900)
        fb.append(m)
        sub2 = sub.assign(code=sub.sym.map(sym_code)).sort_values("opened_ts")
        m2 = pd.merge_asof(sub2[["day", "sym", "side", "entry_bar", "opened_ts", "code"]], w.rename(columns={"ts": "wts"}).sort_values("wts"), left_on="opened_ts", right_on="wts", by="code", direction="backward", tolerance=900)
        fl.append(m2)
    FB = pd.concat(fb).drop(columns=["opened_ts", "fts"])
    FL = pd.concat(fl).drop(columns=["opened_ts", "wts", "code"])
    T = T.merge(FB, on=["day", "sym", "side", "entry_bar"], how="left").merge(FL, on=["day", "sym", "side", "entry_bar"], how="left")
    T["board_with"] = np.where(T.sd > 0, T.score, -T.score)  # the board's score signed the trade's way
    T["fut_with"] = [("LB" if s > 0 else "SB") == f if isinstance(f, str) else np.nan for s, f in zip(T.sd, T.futures_state, strict=False)]
    T["sc_to_lb"] = [(f == "LB") if s > 0 else (f == "SB") for s, f in zip(T.sd, T.futures_state, strict=False)]
    T["delta_with"] = np.sign(T.cum_delta) * T.sd
    T["qi_with"] = T.qi * T.sd
    T["opt_spread_b"] = pd.cut(T.entry_age_s, [-1e9, 30, 120, 1e9], labels=["quote ≤30 s", "30–120 s", "older"])
    # option spread at entry from the ticks (the frame used)
    sp = []
    for day in days:
        legs = T[(T.day == day) & T.opt_code.notna()]
        tk = pd.read_parquet(DD / "archive" / "ticks" / f"{day}.parquet", columns=["scrip_code", "ts", "bid", "ask"])
        tk = tk[tk.scrip_code.isin(set(legs.opt_code))].sort_values("ts")
        for r in legs.itertuples():
            g = tk[(tk.scrip_code == r.opt_code) & (tk.ts >= r.opened_ts) & (tk.ts <= r.opened_ts + 300)]
            if len(g) and g.iloc[0].bid > 0 and g.iloc[0].ask > 0:
                sp.append((r.day, r.sym, r.side, r.entry_bar, (g.iloc[0].ask - g.iloc[0].bid) / ((g.iloc[0].ask + g.iloc[0].bid) / 2) * 100))
    SP = pd.DataFrame(sp, columns=["day", "sym", "side", "entry_bar", "opt_spread_pct"])
    T = T.merge(SP, on=["day", "sym", "side", "entry_bar"], how="left")
    T.to_parquet(out / "attribution.parquet")

    print("== the days")
    for day in days:
        sub = T[T.day == day]
        o, c, path = nifty[day]
        br = breadth[day]
        bk = {hm(s): round(100 * v) for s, v in br.items() if hm(s) in ("09:30", "10:15", "12:15", "14:15", "15:10")}
        print(f"{day}: NIFTY {o:.0f}→{c:.0f} ({(c / o - 1) * 100:+.2f} %), breadth above open {bk} | trades {len(sub)} long {int((sub.sd > 0).sum())} short {int((sub.sd < 0).sum())} "
              f"| mean {sub.pnl_bps.mean():+.1f} bps, win {100 * (sub.pnl_bps > 0).mean():.0f} % | exits {sub.exit_reason.value_counts().to_dict()} | option mean {sub.opt_pct.mean():+.1f} %")
    for day in ("2026-10-27", "2026-11-02"):
        sub = T[T.day == day]
        print(f"\n==== {day}")
        tbl(sub, "side", "by side")
        tbl(sub, "wtype", "by wall type", min_n=8)
        tbl(sub, "clock", "by entry clock")
        tbl(sub, "exit_reason", "by exit")
        tbl(sub, "risk_b", "by stop distance")
        tbl(sub, pd.cut(sub.breadth_with, [0, 0.4, 0.5, 0.6, 0.7, 1.0]), "breadth moving the trade's way at entry")
        tbl(sub, pd.cut(sub.move_adr.astype(float), [-1, 0.3, 0.6, 1.0, 9]), "stock's move from the open at entry (ADR)")
        tbl(sub, "fut_with", "the future's state with the trade (LB for a long, SB for a short) at entry")
        tbl(sub, "bias", "board bias at entry", min_n=8)
        tbl(sub, np.sign(sub.delta_with).fillna(0), "cumulative delta signed the trade's way at entry (−1/0/+1)")
        tbl(sub, pd.cut(sub.qi_with.astype(float), [-1, -0.2, 0.2, 1]), "queue imbalance the trade's way at entry")
    print("\n==== pooled, four days")
    tbl(T, ["side"], "by side")
    tbl(T, pd.cut(T.breadth_with, [0, 0.4, 0.5, 0.6, 0.7, 1.0]), "breadth moving the trade's way at entry")
    tbl(T, "clock", "by entry clock")
    tbl(T, "risk_b", "by stop distance")
    tbl(T, pd.cut(T.room_R, [0, 0.5, 1, 2, 99]), "first wall's room in R")
    tbl(T, "fut_with", "future's state with the trade at entry")
    tbl(T, pd.cut(T.board_with.astype(float), [-9, -2, 0, 2, 9]), "board score signed the trade's way")
    tbl(T, np.sign(T.delta_with).fillna(0), "cumulative delta the trade's way")
    tbl(T, pd.cut(T.qi_with.astype(float), [-1, -0.2, 0.2, 1]), "queue imbalance the trade's way")
    tbl(T, "xs_top20", "stock in the day's top-20 movers at entry")
    tbl(T, pd.cut(T.nifty_move.astype(float) * T.sd, [-9, -0.3, 0, 0.3, 9]), "NIFTY's move from its open, signed the trade's way")
    print("\n-- exit management (pooled)")
    st = T[T.exit_reason == "stop"]
    print(f"stops {len(st)}: mean {st.pnl_bps.mean():+.1f} bps; had an MFE ≥ 0.5R before the stop {100 * (st.mfe_R >= 0.5).mean():.0f} %, ≥ 1R {100 * (st.mfe_R >= 1).mean():.0f} %; median bars held {st.bars_held.median():.0f}")
    tr = T[T.exit_reason == "trail"]
    print(f"trails {len(tr)}: mean {tr.pnl_bps.mean():+.1f} bps; give-back from the MFE median {(tr.mfe - tr.pnl_bps).median():.0f} bps ({100 * ((tr.mfe - tr.pnl_bps) / tr.mfe).median():.0f} % of the MFE)")
    cl = T[T.exit_reason == "close"]
    print(f"closes {len(cl)}: mean {cl.pnl_bps.mean():+.1f} bps; MFE median {cl.mfe.median():.0f} bps")
    print("\n-- the option leg's cost (pooled)")
    ok = T[T.opt_pct.notna()]
    X = np.vstack([np.ones(len(ok)), ok.pnl_bps / 100]).T
    a, b = np.linalg.lstsq(X, ok.opt_pct, rcond=None)[0]
    print(f"option % ≈ {a:+.2f} + {b:.1f} × stock % (n {len(ok)}): the intercept is what a flat stock trade costs in premium")
    for day in days:
        o = ok[ok.day == day]
        Xd = np.vstack([np.ones(len(o)), o.pnl_bps / 100]).T
        ad, bd = np.linalg.lstsq(Xd, o.opt_pct, rcond=None)[0]
        print(f"  {day}: {ad:+.2f} + {bd:.1f} × stock %, spread at entry median {o.opt_spread_pct.median():.1f} % of premium (p75 {o.opt_spread_pct.quantile(.75):.1f} %)")
    tbl(ok, pd.cut(ok.opt_spread_pct, [0, 1, 2, 4, 8, 100]), "option spread at entry (% of premium)", col="opt_pct")
    tbl(ok, pd.cut(ok.premium_inr, [0, 5000, 15000, 40000, 1e9]), "premium per lot (₹)", col="opt_pct")
    tbl(ok, "entry_how", "how the option entry was priced", col="opt_pct")
    flat = ok[ok.pnl_bps.abs() < 15]
    print(f"\nflat stock trades (|bps| < 15): {len(flat)} legs, option mean {flat.opt_pct.mean():+.1f} %, median {flat.opt_pct.median():+.1f} % — the round trip's cost alone")


if __name__ == "__main__":
    main()
