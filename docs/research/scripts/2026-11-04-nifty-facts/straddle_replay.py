"""Replay the engine's shadow straddle, its minute menu and the ₹20 lakh covered-call book on one archived day's REAL
option ticks (strategy/straddle.py, strategy/covered.py) — the check that the live code prices from the broker's quotes
the way it prices from the tests' synthetic chain (6 Nov 2026: it found the zero-age quote read as stale).
Usage: straddle_replay.py DAY EXPIRY FUT_AT_10 SPOT_AT_10   e.g. 2026-11-05 2026-11-09 24423.1 24402.2"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_facts as lf  # noqa: E402
from kotsin_nse.config import Settings  # noqa: E402
from kotsin_nse.risk.costs import CostModel  # noqa: E402
from kotsin_nse.strategy import covered as cv  # noqa: E402
from kotsin_nse.strategy import straddle as stx  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def main() -> None:
    D, EXP, f10, spot10 = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    exp_name = datetime.date.fromisoformat(EXP).strftime("%d %b %Y").upper()
    m = lf.master_for(D)
    m = m[m["Name"].astype(str).str.match(rf"^NIFTY {exp_name} (CE|PE) ")].copy()
    m["kind"] = m["Name"].str.extract(r" (CE|PE) ")[0]
    m["strike"] = m["Name"].str.extract(r" (CE|PE) ([\d.]+)")[1].astype(float)
    t = lf.read("ticks", D, [str(c) for c in m["ScripCode"]], ["scrip_code", "ts", "ltp", "bid", "ask"])
    t["scrip_code"] = t["scrip_code"].astype(str)
    t = t.sort_values("ts")

    def chain_at(hms: str) -> tuple[dict, float]:
        upto = datetime.datetime.combine(datetime.date.fromisoformat(D), datetime.time.fromisoformat(hms), IST).timestamp()
        out: dict = {}
        for r in m.itertuples():
            x = t[(t.scrip_code == str(r.ScripCode)) & (t.ts <= upto)].tail(1)
            if x.empty:
                continue
            q = {"ltp": float(x.ltp.iloc[0]), "bid": float(x.bid.iloc[0]) or None, "ask": float(x.ask.iloc[0]) or None, "age_s": float(upto - x.ts.iloc[0])}
            out.setdefault(float(r.strike), {})[r.kind] = {"scrip_code": str(r.ScripCode), "name": r.Name, "quote": q}
        return out, upto

    costs = CostModel(Settings(_env_file=None, engine_enabled=False))
    c10, now10 = chain_at("10:00:30")
    tt = stx.expiry_t(EXP, now10)
    print(f"== {D}: {len(c10)} strikes of {EXP} at 10:00; future {f10} spot {spot10}")
    row = stx.menu_row(c10, now=now10, spot=spot10, f=f10, t=tt, lot=65, wing_pts=300, iv_open=None, rv20=None)
    for k in ("atm", "iv", "skew"):
        print("  ", k, row[k])
    for k in ("straddle", "strangle1", "strangle2", "fly", "skew_trade"):
        print("  ", k, row[k])
    rec = stx.open_record(c10, now=now10, day=D, spot=spot10, f=f10, expiry=EXP, expiry_day_skipped=False, lot=65, wing_pts=300, cond={})
    print("OPEN ", {k: rec[k] for k in ("strike", "credit", "credit_inr", "fly_credit", "fly_max_loss_inr")})
    c15, now15 = chain_at("15:29:59")
    stx.mark_record(rec, c15)
    stx.close_record(rec, c15, costs, now=now15)
    print("CLOSE", {k: rec[k] for k in ("debit", "pnl_pts", "gross_inr", "charges_inr", "net_inr", "stale_exit")})
    print("FLY  ", rec["fly"])
    kw = {"costs": costs, "capital": 2e6, "margin": 220000.0, "call_delta": 0.30, "roll_delta": 0.60, "take_profit": 0.30, "max_dd": 0.05, "settle_hm": "15:20", "quote_of": None}

    def fut(px: float) -> dict:
        return {"scrip_code": "FUT", "name": "NIFTY FUT", "expiry": "", "lot": 65, "ltp": px, "quote": {"ltp": px, "bid": px - 1, "ask": px + 1, "age_s": 1.0}}

    book, ev = cv.step(None, now=now10, day=D, hm="10:00", spot=spot10, chain=c10, future=fut(f10), f=f10, t=tt, expiry=EXP, **kw)
    print("BOOK ", ev, {k: book["call"][k] for k in ("strike", "entry", "delta", "iv")}, "future", book["future"]["entry"])
    for hms in ("11:00:30", "12:00:30", "13:00:30", "14:00:30", "15:00:30", "15:29:59"):
        c, n = chain_at(hms)
        atm = c.get(stx.atm_strike(spot10)) or {}
        # the future's price at the hour from put-call parity of the opening ATM strike (no future ticks in this script)
        ce, pe = (atm.get("CE") or {}).get("quote"), (atm.get("PE") or {}).get("quote")
        px = round(stx.atm_strike(spot10) + (ce["ltp"] - pe["ltp"]), 1) if ce and pe else f10
        book, ev = cv.step(book, now=n, day=D, hm=hms[:5], spot=px - 20, chain=c, future=fut(px), f=px, t=stx.expiry_t(EXP, n), expiry=EXP, **kw)
        print(f"   {hms[:5]} f≈{px} {ev} call {book['call'] and (book['call']['strike'], book['call']['last'], book['call']['delta'])} equity {book['equity']} dd {book['dd_inr']}")


if __name__ == "__main__":
    main()
