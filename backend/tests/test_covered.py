"""The ₹20 lakh covered-call book (strategy/covered.py): opening, the roll-up, the take-profit, the expiry settle and the
drawdown halt, every one of them from the chain's own quotes."""

from __future__ import annotations

from kotsin_nse.config import Settings
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy import covered as cv
from kotsin_nse.strategy import straddle as stx

from .test_straddle import AT_10, DAY, EXPIRY, chain_at

CAPITAL = 2_000_000.0


def _future(f: float) -> dict:
    return {"scrip_code": "F1", "name": "NIFTY 27 JAN 2026 FUT", "expiry": "2026-01-27", "lot": 65, "ltp": f,
            "quote": {"ltp": f, "bid": f - 1, "ask": f + 1, "age_s": 1.0}}


def _kw(tmp_path) -> dict:
    return {"costs": CostModel(Settings(_env_file=None, data_dir=tmp_path, engine_enabled=False)), "capital": CAPITAL, "margin": 220_000.0,
            "call_delta": 0.30, "roll_delta": 0.60, "take_profit": 0.30, "max_dd": 0.05, "settle_hm": "15:20", "quote_of": None}


def test_the_call_is_the_lowest_strike_at_or_under_the_delta():
    t = stx.expiry_t(EXPIRY, AT_10)
    chain = chain_at(24420.0, t=t)
    c = cv.choose_call(chain, spot=24402.0, f=24420.0, t=t, max_delta=0.30)
    assert c is not None and c["kind"] == "CE" and c["delta"] <= 0.30 and c["strike"] >= 24400.0
    below = [k for k in chain if 24400.0 <= k < c["strike"]]
    assert below, "a strike nearer the money was skipped for its delta"
    assert c["entry"] == chain[c["strike"]]["CE"]["quote"]["bid"] and abs(c["iv"] - 15.0) < 0.3 and c["theta"] < 0
    assert cv.choose_call(chain, spot=24402.0, f=24420.0, t=t, max_delta=0.0) is None


def test_the_book_opens_rolls_up_takes_profit_and_halts_on_the_drawdown(tmp_path):
    kw = _kw(tmp_path)
    t = stx.expiry_t(EXPIRY, AT_10)
    f = 24420.0
    book, ev = cv.step(None, now=AT_10 - 60, day=DAY, hm="09:59", spot=24402.0, chain=chain_at(f, t=t), future=_future(f), f=f, t=t, expiry=EXPIRY, **kw)
    assert book["status"] == "FLAT" and ev == []  # not before the hour
    book, ev = cv.step(book, now=AT_10, day=DAY, hm="10:00", spot=24402.0, chain=chain_at(f, t=t), future=_future(f), f=f, t=t, expiry=EXPIRY, **kw)
    assert book["status"] == "OPEN" and ev == ["buy_future", "sell_call"] and book["opened_day"] == DAY
    assert book["future"]["entry"] == f + 1 and book["lot"] == 65 and book["call"]["delta"] <= 0.30 and book["call"]["expiry"] == EXPIRY
    k0, premium0 = book["call"]["strike"], book["call"]["entry"]
    # the bid-ask paid on both legs is the opening mark-to-market
    assert book["equity"] < CAPITAL and book["unrealised_inr"] == round((f - (f + 1)) * 65 + (premium0 - book["call"]["last"]) * 65, 2)
    # R1: the index jumps 500 points — the call runs in the money (delta ≥ 0.60), is bought back and rolled up
    f2 = f + 500
    book, ev = cv.step(book, now=AT_10 + 600, day=DAY, hm="10:10", spot=f2 - 20, chain=chain_at(f2, t=t), future=_future(f2), f=f2, t=t, expiry=EXPIRY, **kw)
    assert ev == ["close_call", "sell_call"] and book["calls_closed"][-1]["how"] == "roll_up" and book["call"]["strike"] > k0
    rolled = book["calls_closed"][-1]
    assert rolled["exit"] == chain_at(f2, t=t)[k0]["CE"]["quote"]["ask"] and rolled["net_inr"] < 0 and rolled["charges_inr"] > 0
    assert book["realised_inr"] == rolled["net_inr"] and book["equity"] > CAPITAL  # the future's 500 points outran the call
    # R2: the premium collapses (vol 3 %) — the call trades under 30 % of its sale: banked, a fresh one sold
    chain3 = chain_at(f2, t=t, iv=0.03)
    book, ev = cv.step(book, now=AT_10 + 1200, day=DAY, hm="10:20", spot=f2 - 20, chain=chain3, future=_future(f2), f=f2, t=t, expiry=EXPIRY, **kw)
    assert ev == ["close_call", "sell_call"] and book["calls_closed"][-1]["how"] == "take_profit" and book["calls_closed"][-1]["net_inr"] > 0
    assert book["call"]["entry"] == chain3[book["call"]["strike"]]["CE"]["quote"]["bid"]
    # a quiet minute: a mark, no event
    book, ev = cv.step(book, now=AT_10 + 1260, day=DAY, hm="10:21", spot=f2 - 20, chain=chain3, future=_future(f2), f=f2, t=t, expiry=EXPIRY, **kw)
    assert ev == [] and book["marks"][DAY]["equity"] == book["equity"]
    # R5: the index falls 2,000 points — more than 5 % of the capital below the peak: both legs closed, halted for the day
    f3 = f2 - 2000
    book, ev = cv.step(book, now=AT_10 + 1800, day=DAY, hm="10:30", spot=f3, chain=chain_at(f3, t=t), future=_future(f3), f=f3, t=t, expiry=EXPIRY, **kw)
    assert ev[-1] == "halt" and book["status"] == "HALTED" and book["future"] is None and book["call"] is None and book["halted_day"] == DAY
    assert book["dd_inr"] <= -0.05 * CAPITAL and book["equity"] == round(CAPITAL + book["realised_inr"], 2) and book["unrealised_inr"] == 0.0
    assert book["calls_closed"][-1]["how"] == "drawdown" and book["events"][-2]["what"] == "close_future"
    book, ev = cv.step(book, now=AT_10 + 1860, day=DAY, hm="10:31", spot=f3, chain=chain_at(f3, t=t), future=_future(f3), f=f3, t=t, expiry=EXPIRY, **kw)
    assert ev == [] and book["status"] == "HALTED"
    # the next session it reopens
    book, ev = cv.step(book, now=AT_10 + 86400, day="2026-01-06", hm="10:00", spot=f3, chain=chain_at(f3, t=t), future=_future(f3), f=f3, t=t, expiry=EXPIRY, **kw)
    assert ev == ["buy_future", "sell_call"] and book["status"] == "OPEN" and book["opened_day"] == DAY and book["future"]["opened_day"] == "2026-01-06"
    v = cv.view(book, "2026-01-06")
    # four calls closed: the roll-up, the take-profit, the one that decayed on the fall, and the drawdown's
    assert v["status"] == "OPEN" and v["calls_closed"] == 4 and len(v["events_today"]) == 2 and v["days"] == 2 and v["premium_collected_inr"] == round(
        sum(c["gross_inr"] for c in book["calls_closed"]), 2)


def test_the_call_settles_on_its_own_expiry_day_and_the_future_rolls(tmp_path):
    kw = _kw(tmp_path)
    t = stx.expiry_t(EXPIRY, AT_10)
    f = 24420.0
    book, _ = cv.step(None, now=AT_10, day=DAY, hm="10:00", spot=24402.0, chain=chain_at(f, t=t), future=_future(f), f=f, t=t, expiry=EXPIRY, **kw)
    k0 = book["call"]["strike"]
    # the expiry day: the engine's chain is already the following week's; the expiring call is quoted by the marker
    t2 = stx.expiry_t("2026-01-15", AT_10 + 3 * 86400)
    next_chain = chain_at(f, t=t2)
    # the call still holds 60 % of its premium (near the money): not a take-profit, so it waits for the settle
    mid = round(0.6 * book["call"]["entry"], 2)
    last = {"ltp": mid, "bid": mid - 0.5, "ask": mid + 0.5, "age_s": 2.0}
    kw2 = {**kw, "quote_of": lambda code: last if code == f"{int(k0)}CE" else None}
    book, ev = cv.step(book, now=AT_10 + 3 * 86400 + 5 * 3600, day=EXPIRY, hm="15:00", spot=f, chain=next_chain, future=_future(f), f=f, t=t2, expiry="2026-01-15", **kw2)
    assert ev == [] and book["call"]["strike"] == k0 and book["call"]["last"] == mid + 0.5  # marked off the marker, not settled before 15:20
    book, ev = cv.step(book, now=AT_10 + 3 * 86400 + 5 * 3600 + 1200, day=EXPIRY, hm="15:20", spot=f, chain=next_chain, future=_future(f), f=f, t=t2, expiry="2026-01-15", **kw2)
    assert ev == ["close_call"] and book["calls_closed"][-1]["how"] == "expiry" and book["calls_closed"][-1]["exit"] == mid + 0.5 and book["call"] is None
    # after LAST_OPEN_HM no new call; the next morning one is sold on the new expiry
    book, ev = cv.step(book, now=AT_10 + 4 * 86400, day="2026-01-09", hm="10:00", spot=f, chain=next_chain, future=_future(f), f=f, t=t2, expiry="2026-01-15", **kw)
    assert ev == ["sell_call"] and book["call"]["expiry"] == "2026-01-15"
    # R4: the engine's front future changes — the leg is re-based and the roll realised
    fut2 = {**_future(f + 40), "scrip_code": "F2", "name": "NIFTY 24 FEB 2026 FUT", "expiry": "2026-02-24"}
    book, ev = cv.step(book, now=AT_10 + 4 * 86400 + 60, day="2026-01-09", hm="10:01", spot=f, chain=next_chain, future=fut2, f=f + 40, t=t2, expiry="2026-01-15", **kw)
    assert ev == ["roll_future"] and book["future"]["scrip_code"] == "F2" and book["future"]["entry"] == f + 41
    assert book["events"][-2]["what"] == "close_future" and book["events"][-2]["how"] == "roll" and book["events"][-2]["exit"] == f
