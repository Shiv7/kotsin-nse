"""The shadow straddle and its minute menu (strategy/straddle.py), and the runner's minute that records them."""

from __future__ import annotations

import asyncio
import json
from datetime import date

from kotsin_nse.bars.store import BarStore
from kotsin_nse.config import Settings
from kotsin_nse.fno.chain import black76
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy import straddle as stx

from .test_strategy import DAY, OPEN, _feed, _runner, _seed_daily, _ubar

EXPIRY = "2026-01-08"  # the Thursday after DAY (Monday 2026-01-05)
AT_10 = OPEN + 45 * 60
AT_1529 = OPEN + 6 * 3600 + 14 * 60


def chain_at(f: float, *, t: float, iv: float = 0.15, step: int = 50, lo: int = 23000, hi: int = 25800, age: float = 5.0) -> dict:
    """A flat-vol chain priced by Black-76 on the future ``f``: each leg's last trade, a bid half a point under it and
    an ask half a point over (a worthless option quoted at its price)."""
    out: dict = {}
    for k in range(lo, hi + 1, step):
        for kind in ("CE", "PE"):
            p = round(max(black76(f, k, t, iv, kind == "CE"), 0.05), 2)
            q = {"ltp": p, "bid": round(p - 0.5, 2) if p > 0.6 else p, "ask": round(p + 0.5, 2), "age_s": age}
            out.setdefault(float(k), {})[kind] = {"scrip_code": f"{k}{kind}", "name": f"NIFTY 08 JAN 2026 {kind} {k}.00", "quote": q}
    return out


def test_the_expiry_is_the_nearest_strictly_after_today_and_expiry_day_is_skipped():
    assert stx.pick_expiry(["2026-11-09", "2026-11-05", "2026-11-17"], "2026-11-05") == ("2026-11-09", True)
    assert stx.pick_expiry(["2026-11-09", "2026-11-17"], "2026-11-06") == ("2026-11-09", False)
    assert stx.pick_expiry(["2026-11-05"], "2026-11-05") == (None, True)
    assert stx.atm_strike(24402.0) == 24400.0 and stx.atm_strike(24426.0) == 24450.0
    # the clock: three days and change to the Thursday's 15:30, in years
    assert 3.2 / 365 < stx.expiry_t(EXPIRY, AT_10) < 3.3 / 365


def test_the_menu_prices_each_structure_from_the_chain():
    t = stx.expiry_t(EXPIRY, AT_10)
    f = 24420.0
    chain = chain_at(f, t=t)
    row = stx.menu_row(chain, now=AT_10, spot=24402.0, f=f, t=t, lot=65, wing_pts=300.0, iv_open=14.0, rv20=11.0)
    assert row["hm"] == "10:00" and row["atm"] == 24400.0
    assert abs(row["iv"] - 15.0) < 0.3 and abs(row["d_iv"] - 1.0) < 0.3 and row["rv20"] == 11.0 and abs(row["iv_over_rv"] - 1.36) < 0.05
    s = row["straddle"]
    bids = chain[24400.0]["CE"]["quote"]["bid"] + chain[24400.0]["PE"]["quote"]["bid"]
    assert s["k"] == 24400.0 and s["credit"] == round(bids, 2) and s["credit_inr"] == round(bids * 65)
    assert s["be"] == [round(24400 - s["credit"], 1), round(24400 + s["credit"], 1)] and 0.4 < s["p"] < 0.8 and "max_loss_inr" not in s
    assert row["strangle1"]["k"] == [24350.0, 24450.0] and row["strangle2"]["k"] == [24300.0, 24500.0]
    assert row["strangle2"]["credit"] < row["strangle1"]["credit"] < s["credit"]
    assert row["strangle2"]["p"] > row["strangle1"]["p"] > s["p"]  # wider breakevens, more of the distribution inside
    fly = row["fly"]
    assert fly["k"] == [24400.0, 24400.0, 24700.0, 24100.0] and fly["credit"] < s["credit"]
    assert fly["max_loss_inr"] == round((300 - fly["credit"]) * 65) and fly["p"] < s["p"]  # the wings cost: narrower breakevens
    # a flat surface: no skew, and the skew trade still priced (whichever side is a hair richer)
    assert abs(row["skew"]) < 0.5
    assert row["skew_trade"]["credit"] is not None and set(row["skew_trade"]) == {"sell", "buy", "credit", "iv_gap", "delta"}
    # the realised vol: a constant close is zero, a wiggle is not
    assert stx.realised_vol([100.0] * 30) == 0.0 and stx.realised_vol([100.0, 101.0] * 15) > 10  # a 1 % wiggle a day ≈ 16 % a year
    assert stx.realised_vol([100.0, 101.0]) is None


def test_the_record_sells_at_the_bid_and_buys_back_at_the_ask(tmp_path):
    costs = CostModel(Settings(_env_file=None, data_dir=tmp_path, engine_enabled=False))
    t0 = stx.expiry_t(EXPIRY, AT_10)
    f = 24420.0
    chain = chain_at(f, t=t0)
    rec = stx.open_record(chain, now=AT_10, day=DAY, spot=24402.0, f=f, expiry=EXPIRY, expiry_day_skipped=False, lot=65, wing_pts=300.0, cond={"x": 1})
    assert rec is not None and rec["status"] == "OPEN" and rec["strike"] == 24400.0 and rec["late"] is False and rec["days"] == 3
    assert rec["credit"] == round(chain[24400.0]["CE"]["quote"]["bid"] + chain[24400.0]["PE"]["quote"]["bid"], 2)
    assert rec["legs"]["CE"]["entry"] == chain[24400.0]["CE"]["quote"]["bid"] and rec["wings"]["CE"]["strike"] == 24700.0 and rec["wings"]["PE"]["strike"] == 24100.0
    assert rec["wings"]["CE"]["entry"] == chain[24700.0]["CE"]["quote"]["ask"]  # the wings are bought
    assert rec["fly_credit"] == round(rec["credit"] - rec["wings"]["CE"]["entry"] - rec["wings"]["PE"]["entry"], 2)
    assert rec["fly_max_loss_inr"] == round((300 - rec["fly_credit"]) * 65) and rec["cond"] == {"x": 1}
    # the afternoon: less time, lower vol — the straddle is cheaper to buy back
    t1 = stx.expiry_t(EXPIRY, AT_1529)
    later = chain_at(f, t=t1, iv=0.10)
    stx.mark_record(rec, later)
    assert rec["last_pnl_pts"] > 0 and rec["legs"]["CE"]["last"] == later[24400.0]["CE"]["quote"]["ask"]
    stx.close_record(rec, later, costs, now=AT_1529)
    assert rec["status"] == "CLOSED" and rec["how"] == "close" and rec["closed_hm"] == "15:29" and rec["stale_exit"] is False
    assert rec["debit"] == round(later[24400.0]["CE"]["quote"]["ask"] + later[24400.0]["PE"]["quote"]["ask"], 2)
    assert rec["pnl_pts"] == round(rec["credit"] - rec["debit"], 2) and abs(rec["gross_inr"] - rec["pnl_pts"] * 65) < 0.05
    assert rec["charges_inr"] > 0 and rec["net_inr"] == round(rec["gross_inr"] - rec["charges_inr"], 2)
    # the fly: the wings were bought and sold back lower, so it nets less than the naked straddle
    assert rec["fly"]["net_inr"] < rec["net_inr"] and rec["fly"]["pnl_pts"] < rec["pnl_pts"]
    assert stx.summary(rec)["menu_rows"] == 0 and "menu" not in stx.summary(rec)
    # closed without a chain (the day rolled with it open): settled at the last marks, and it says so
    rec2 = stx.open_record(chain, now=AT_10, day=DAY, spot=24402.0, f=f, expiry=EXPIRY, expiry_day_skipped=True, lot=65, wing_pts=300.0, cond={})
    stx.close_record(rec2, {}, costs, now=AT_1529, how="END")
    assert rec2["status"] == "CLOSED" and rec2["how"] == "END" and rec2["stale_exit"] is True and rec2["debit"] == rec2["credit"]
    # a leg without a usable quote (a pre-open snapshot): no record
    old = chain_at(f, t=t0, age=5000.0)
    assert stx.open_record(old, now=AT_10, day=DAY, spot=24402.0, f=f, expiry=EXPIRY, expiry_day_skipped=False, lot=65, wing_pts=300.0, cond={}) is None


async def test_the_runner_records_the_straddle_and_the_book_from_the_engines_minute(tmp_path):
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    _seed_daily(store, "NIFTY", "99")
    for i in range(46):  # the index's minutes from the open to 10:00
        store.close(_ubar("NIFTY", "99", "1m", OPEN + 60 * i, 24380.0, 24390.0, 24370.0, 24402.0 if i == 45 else 24385.0))
    r = _runner(tmp_path, store, strategy_sequence="level", strategy_straddle=True, strategy_covered=True)
    await _feed(r, store, [(104.0, 104.5, 103.5, 104.2)])  # the day rolls on its first bar
    f = 24420.0
    state = {"chain": chain_at(f, t=stx.expiry_t(EXPIRY, AT_10)), "f": f}

    def pick(symbol, spot):
        assert symbol == "NIFTY" and spot == 24402.0
        fq = {"ltp": state["f"], "bid": state["f"] - 1, "ask": state["f"] + 1, "age_s": 1.0}
        return {"expiry": EXPIRY, "expiry_day": False, "days": 3, "lot": 65, "strikes": state["chain"], "instruments": {},
                "future": {"scrip_code": "F1", "name": "NIFTY 27 JAN 2026 FUT", "expiry": "2026-01-27", "lot": 65, "ltp": state["f"], "quote": fq}}

    r.straddle_pick = pick
    r.option_mark = lambda code: None
    r.on_minute(OPEN + 44 * 60)  # 09:59: nothing before the hour
    assert r.straddle is None and r.menu == [] and r.book is None
    r.on_minute(AT_10)
    rec = r.straddle
    assert rec is not None and rec["status"] == "OPEN" and rec["strike"] == 24400.0 and rec["opened_hm"] == "10:00" and rec["late"] is False
    assert rec["cond"]["rv20"] == 0.0 and rec["cond"]["iv_open"] == rec["cond"]["iv"] and rec["cond"]["fno"] == {}
    assert len(r.menu) == 1 and r.menu[0]["d_iv"] == 0.0 and r.menu[0]["straddle"]["credit"] == rec["credit"]
    book = r.book
    assert book is not None and book["status"] == "OPEN" and book["future"]["entry"] == f + 1 and book["lot"] == 65
    assert book["call"]["delta"] <= 0.30 and book["call"]["strike"] >= 24400.0 and book["events"][0]["what"] == "buy_future"
    assert book["events"][0]["exposure_pct"] == round((f + 1) * 65 / 2_000_000 * 100, 1)
    # the close: a cheaper chain, the record settled, the book marked
    state["chain"] = chain_at(f, t=stx.expiry_t(EXPIRY, AT_1529), iv=0.10)
    r.on_minute(AT_1529)
    assert rec["status"] == "CLOSED" and rec["pnl_pts"] > 0 and rec["net_inr"] > 0 and rec["closed_hm"] == "15:29" and len(r.menu) == 2
    assert r.book["status"] == "OPEN" and r.book["equity"] > 2_000_000  # the call decayed, the future unchanged
    await asyncio.gather(*r._tasks)  # type: ignore[attr-defined]
    snap = json.loads((tmp_path / "strategy" / f"{DAY}.json").read_text())
    assert snap["straddle_on"] is True and snap["covered_on"] is True
    assert snap["straddle"]["status"] == "CLOSED" and snap["straddle"]["pnl_pts"] == rec["pnl_pts"] and len(snap["menu"]) == 2
    assert snap["covered"]["status"] == "OPEN" and snap["covered"]["calls_closed"] == 0 and len(snap["covered"]["events_today"]) == 2
    assert json.loads((tmp_path / "strategy" / "book.json").read_text())["status"] == "OPEN"
    # a restart inside the day: the record, the menu and the book come back
    r2 = _runner(tmp_path, store, strategy_sequence="level", strategy_straddle=True, strategy_covered=True)
    assert r2.book is not None and r2.book["call"]["strike"] == book["call"]["strike"]
    r2._roll(date.fromisoformat(DAY))
    assert r2.straddle is not None and r2.straddle["status"] == "CLOSED" and len(r2.menu) == 2 and r2._iv_open == r.menu[0]["iv"]
    assert r2.errors == 0 and r.errors == 0
