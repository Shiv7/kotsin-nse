"""The active-trades page (operator, 2026-10-09: "in the dashabpord have a dedicated page for all actove trades ... each card
with that OTM's orderbook refreshed last time ... if it is older than our threshold, flag it on the card and also in reason
for exit: that orderbook was stale, metion lots, mention strategyname, engine name, charges so far , slipage cost if any,
potental charges at exit and approx slipage at every refresh rate if we had to exit at LTP").

/api/positions/live: one row per open position of every book, the mirrors and the shadows too, with the book's age against
the paper matcher's own limit, the money so far and an exit now would cost, the targets and the trigger. /api/trades: the
exits refused for a stale book, beside the exit reason. Record-only: nothing in the order path imports api/live_trades.py."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

import httpx
import pytest

from kotsin_nse.api import live_trades as lt
from kotsin_nse.api import peer
from kotsin_nse.api.routes import build_app
from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind, OrderSide
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.strategy.keys import StrategyKey
from tests.test_limit_orders import OPT, UND, _book, _engine, _sig


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _client(e):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t")


#: LTF 27 OCT 2026 CE 270, as engine B recorded it on 9 Oct 2026: the depth walked by RT-Y's refused HALT exit at 12:37:52
#: (``exec.depthAtCross``, 45.6 s old) and RT-Y's entry trail at 10:15 — recorded prices, nothing made up
LTF_BIDS = [(4.65, 11250), (4.6, 15750), (4.55, 15750), (4.5, 15750), (4.45, 9000)]
LTF_ENTRY = {"kind": "entry", "limit": 5.3, "ref": 5.325, "bookAtPlace": {"bid": 5.3, "ask": 5.4}, "fillPrice": 5.3, "waitS": 29.808,
             "why": "at the signal price, inside the book", "outcome": "filled at the limit"}
LTF_REFUSED = "cross failed: book for LTF is 45629 ms old (limit 6000)"


async def _open_rt_y(e, clock) -> None:
    """RT-Y fills on TATASTEEL 190 CE: RT-Y, its two stop-rule mirrors, the wide-stop shadow and its two mirrors open."""
    _book(e, 16.95, 17.25, clock[0])
    await e._handle_signal(_sig(clock), None, books=(StrategyKey.FUDKII_RT_Y,))
    clock[0] += 22
    _book(e, 16.95, 17.10, clock[0])
    e.ltps[UND.scrip_code] = 187.4
    await e._manage_positions()


# -- the pieces --------------------------------------------------------------------------------------------------------


def test_a_stale_book_refusal_is_read_with_its_age_and_limit():
    assert lt.refusal_age(LTF_REFUSED) == (45629.0, 6000.0)
    assert lt.refusal_age("book for X is 12545 ms old") == (12545.0, None)
    assert lt.refusal_age("X: no bid to trade against") == (None, None)
    rows = [
        {"purpose": "EXIT", "status": "REJECTED", "position_id": "p1", "ts": 10.0, "exec": {"outcome": LTF_REFUSED}},
        {"purpose": "EXIT", "status": "REJECTED", "position_id": "p1", "ts": 12.0, "exec": {"outcome": "not filled"},
         "note": "book for LTF is 33190 ms old (limit 6000)"},
        {"purpose": "EXIT", "status": "REJECTED", "position_id": "p1", "ts": 13.0, "exec": {"outcome": "cross failed: no bid"}},
        {"purpose": "EXIT", "status": "FILLED", "position_id": "p1", "ts": 14.0, "exec": {"outcome": LTF_REFUSED}},
        {"purpose": "ENTRY", "status": "REJECTED", "position_id": "p1", "ts": 15.0, "exec": {"outcome": LTF_REFUSED}},
    ]
    assert lt.stale_refusals(rows) == {"p1": {"count": 2, "maxAgeMs": 45629.0, "limitMs": 6000.0, "lastTs": 12.0}}
    # a later poll's new refusals are added in, never counted twice
    have = lt.stale_refusals(rows)
    lt.merge_refusals(have, {"p1": {"count": 1, "maxAgeMs": 9000.0, "limitMs": 6000.0, "lastTs": 20.0},
                             "p2": {"count": 1, "maxAgeMs": 7000.0, "limitMs": None, "lastTs": 21.0}})
    assert have == {"p1": {"count": 3, "maxAgeMs": 45629.0, "limitMs": 6000.0, "lastTs": 20.0},
                    "p2": {"count": 1, "maxAgeMs": 7000.0, "limitMs": None, "lastTs": 21.0}}


def test_an_exit_now_walks_the_recorded_bids_for_every_lot():
    # 9,000 of LTF 270 CE into its 12:37:52 book: 4.65 holds all of it
    w = lt.exit_now(LTF_BIDS, 9000, sell=True, ltp=4.6, mid=4.575, touch=4.65)
    assert w["basis"] == "depth" and w["avg"] == 4.65 and w["walk"]["levels"] == 1 and w["walk"]["beyondQty"] == 0
    assert w["vsLtpRs"] == pytest.approx((4.6 - 4.65) * 9000) and w["vsTouchRs"] == 0.0
    # 54,000 (24 lots) takes four levels: 11,250 @ 4.65 + 15,750 @ 4.60 + 15,750 @ 4.55 + 11,250 @ 4.50
    w = lt.exit_now(LTF_BIDS, 54000, sell=True, ltp=4.6, mid=4.575, touch=4.65)
    avg = (11250 * 4.65 + 15750 * 4.6 + 15750 * 4.55 + 11250 * 4.5) / 54000
    assert w["avg"] == pytest.approx(avg, abs=1e-4) and w["walk"]["levels"] == 4
    assert w["vsMidRs"] == pytest.approx((4.575 - avg) * 54000, abs=1)
    # more than the five levels hold: the rest at the last level shown, flagged
    w = lt.exit_now(LTF_BIDS, 80000, sell=True, ltp=4.6, mid=4.575, touch=4.65)
    assert w["walk"]["beyondQty"] == 80000 - 67500 and w["walk"]["inBook"] == 67500
    # no depth: the bid alone; no bid either: nothing to price
    assert lt.exit_now([], 9000, sell=True, ltp=4.6, mid=4.575, touch=4.5)["basis"] == "touch"
    assert lt.exit_now([], 9000, sell=True, ltp=4.6, mid=None, touch=None)["basis"] == "none"
    assert lt.exit_now(LTF_BIDS, 9000, sell=True, ltp=4.6, mid=None, touch=None, moved=True)["basis"] == "none", "a stale book alone prices nothing"
    # the matcher's ceiling: one cross takes only what sits within 10 % under the touch
    w = lt.walk_all([(10.0, 1000), (8.5, 5000)], 3000, sell=True)
    assert w["matcherQty"] == 1000 and w["avg"] == pytest.approx((1000 * 10 + 2000 * 8.5) / 3000, abs=1e-4)


#: ABCAPITAL 27 OCT 2026 PE 365, as engine B held it on 9 Oct 2026 at 10:03:13.2: the depth book 9.5 s old (stale against 6 s),
#: the quote 4.30 / 4.45. CT-Y-F's SL-EQ sold its 12,400 at 4.26 a quarter of a second later
ABCAP_BIDS = [(4.3, 3100), (4.25, 15500), (4.2, 6200), (4.15, 18600), (4.1, 31000)]


def test_on_a_stale_book_an_exit_now_walks_its_shape_moved_to_the_quotes_bid():
    w = lt.exit_now(ABCAP_BIDS, 12400, sell=True, ltp=4.4, mid=4.375, touch=4.3, moved=True)
    assert w["basis"] == "shape" and w["avg"] == pytest.approx((3100 * 4.3 + 9300 * 4.25) / 12400) and w["avg"] == 4.2625
    assert w["vsTouchRs"] == pytest.approx((4.3 - 4.2625) * 12400), "₹465 the quote's bid alone would not show"
    # the shape is moved, not its prices: a quote bid 0.10 lower walks the same spacing from there
    w = lt.exit_now(ABCAP_BIDS, 12400, sell=True, ltp=4.4, mid=4.275, touch=4.2, moved=True)
    assert w["avg"] == pytest.approx(4.1625) and w["walk"]["touch"] == 4.2


def test_a_target_share_is_walked_on_the_books_shape_moved_to_the_rung():
    # RT-Y's T1 5.78 share of one lot (2,250) on LTF's book: 11,250 at the touch, so all at the rung
    s = lt.at_rung(LTF_BIDS, 5.78, 2250)
    assert s["avg"] == 5.78 and s["rs"] == 0.0 and s["touchQty"] == 11250
    # a share past the touch walks the moved levels: 20,000 = 11,250 @ 5.78 + 8,750 @ 5.73
    s = lt.at_rung(LTF_BIDS, 5.78, 20000)
    assert s["avg"] == pytest.approx((11250 * 5.78 + 8750 * 5.73) / 20000, abs=1e-4)
    assert s["rs"] == pytest.approx(8750 * 0.05, abs=0.01)
    assert lt.at_rung([], 5.78, 2250) is None
    # an RT-N rung is not on the tick (5.78333…): a share wholly at the touch still gives up exactly nothing
    s = lt.at_rung(LTF_BIDS, 5.3 * 1.09119496855, 2250)
    assert s["rs"] == 0.0 and str(s["rs"]) == "0.0", "never −₹0.08 from rounding, never -0.0"
    s = lt.at_rung(LTF_BIDS, 5.783333333333, 20000)
    assert s["rs"] == pytest.approx(8750 * 0.05, abs=0.005)


def test_the_entry_slippage_is_against_the_signal_premium_and_the_mid_at_placement():
    s = lt.entry_slippage(LTF_ENTRY, qty=9000)
    assert s["vsRefRs"] == pytest.approx((5.3 - 5.325) * 9000) and s["vsMidRs"] == pytest.approx((5.3 - 5.35) * 9000)
    assert s["midAtPlace"] == 5.35 and s["vsRefPct"] == pytest.approx(-0.47, abs=0.01)
    assert lt.entry_slippage(None, qty=9000) is None
    sold = lt.exit_slices_slippage([{"reason": "TARGET", "qty": 2250, "fillPrice": 5.78, "bookAtPlace": {"bid": 5.7, "ask": 5.9}}])
    assert sold[0]["vsMidRs"] == pytest.approx((5.8 - 5.78) * 2250)


def test_neutral_is_an_open_pnl_within_its_charges():
    assert lt.pnl_state(-150.0, 200.0) == "neutral" and lt.pnl_state(150.0, 200.0) == "neutral"
    assert lt.pnl_state(250.0, 200.0) == "profit" and lt.pnl_state(-250.0, 200.0) == "loss"
    assert lt.pnl_state(200.0, 200.0) == "neutral" and lt.pnl_state(-200.0, 200.0) == "neutral", "the band's edge is neutral"
    assert lt.pnl_state(200.01, 200.0) == "profit"
    assert lt.pnl_state(None, 200.0) is None


def test_a_book_is_stale_only_past_the_matchers_limit():
    # the matcher refuses a book older than its limit (``age > limit``): one exactly at it still trades
    assert lt.book_state(6000.0, 6000.0, True) == "fresh" and lt.book_state(6000.5, 6000.0, True) == "stale"
    assert lt.book_state(9000.0, 10000.0, True) == "fresh" and lt.book_state(None, 6000.0, False) == "none"


def test_the_market_is_aligned_when_more_names_go_the_trades_way_than_at_the_trigger():
    t, n = {"with": 20, "against": 28, "flat": 0, "names": 48}, {"with": 31, "against": 17, "flat": 0, "names": 48}
    assert lt.market_aligned(t, n)["aligned"] == "yes"
    assert lt.market_aligned(n, t)["aligned"] == "no"
    assert lt.market_aligned(None, n)["aligned"] == "unknown", "never a guess"
    same = lt.market_aligned(t, dict(t))
    assert same["aligned"] == "unchanged" and "the same share as at the trigger (20 of 48)" in same["why"]
    # 24 of 48 then and 24 of 48 now (one name fewer read): the same share, so unchanged
    assert lt.market_aligned({"with": 24, "against": 24, "flat": 0, "names": 48}, {"with": 24, "against": 24, "flat": 0, "names": 48})["aligned"] == "unchanged"
    assert lt.flip(t) == {"with": 28, "against": 20, "flat": 0, "names": 48}


def test_a_closed_trades_stale_book_is_read_from_its_refused_and_filled_exits():
    orders = [
        {"purpose": "EXIT", "status": "REJECTED", "position_id": "p1", "ts": 100.0, "exec": {"outcome": "cross failed: book for X is 12545 ms old (limit 6000)"}},
        {"purpose": "EXIT", "status": "REJECTED", "position_id": "p1", "ts": 102.0, "exec": {"outcome": "cross failed: book for X is 14800 ms old (limit 6000)"}},
        {"purpose": "EXIT", "status": "FILLED", "position_id": "p1", "ts": 104.0, "exec": {"limit": 4.6, "filledTs": 105.0, "depthAtFill": {"ageMs": 614}}},
        {"purpose": "EXIT", "status": "FILLED", "position_id": "p2", "ts": 104.0, "exec": {"limit": 4.2, "crossedTs": 104.0, "depthAtCross": {"ageMs": 7200}}},
        {"purpose": "EXIT", "status": "FILLED", "position_id": "p3", "ts": 104.0, "exec": {"limit": 4.6, "filledTs": 104.0, "depthAtFill": {"ageMs": 900}}},
        # a resting limit filled on a touch: priced on the quote or a fresh print, its depth snapshot 9.4 s old (trd-28748f3d44eb)
        {"purpose": "EXIT", "status": "FILLED", "position_id": "p4", "ts": 104.0, "exec": {"limit": 7.1, "filledTs": 106.0, "bookAtFill": {"bid": 7.1, "ask": 7.2},
                                                                                         "depthAtFill": {"ageMs": 9400}, "outcome": "filled at the limit"}},
        {"purpose": "EXIT", "status": "FILLED", "position_id": "p5", "ts": 104.0, "exec": {"limit": None, "depthAtPlace": {"ageMs": 6500}}},
    ]
    trades = [{"id": "t1", "position_id": "p1", "closed_ts": 105.0}, {"id": "t2", "position_id": "p2"}, {"id": "t3", "position_id": "p3"},
              {"id": "t4", "position_id": "p4"}, {"id": "t5", "position_id": "p5"}]
    out = lt.stale_book_for_trades(trades, orders, limit_ms_at=lambda ts: 6000.0)
    assert out["t1"]["stale"] and out["t1"]["refused"] == 2 and out["t1"]["maxAgeMs"] == 14800.0 and not out["t1"]["fillStale"]
    assert out["t1"]["words"] == "order book stale (2 exits refused, 14.8 s)"
    assert out["t2"]["fillStale"] and out["t2"]["fillHow"] == "cross" and out["t2"]["words"] == "order book stale (crossed on a 7.2 s old book, limit 6 s)"
    assert not out["t3"]["stale"] and out["t3"]["words"] == "" and out["t3"]["depthAgeAtFillMs"] == 900.0 and out["t3"]["fillAgeMs"] is None
    t4 = out["t4"]
    assert not t4["stale"] and not t4["fillStale"] and t4["fillHow"] == "resting" and t4["depthAgeAtFillMs"] == 9400.0, "the quote priced it"
    assert out["t5"]["fillStale"] and out["t5"]["words"] == "order book stale (sold at market on a 6.5 s old book, limit 6 s)"
    # a book stamped a hair after our clock (seen in the exec trails) is 0 s old
    assert lt.fill_book_age({"limit": 4.2, "depthAtCross": {"ageMs": -120}}) == (0.0, "cross")
    assert lt.fill_book_age({"limit": 4.2, "depthAtPlace": {"ageMs": 900}}) == (None, None), "a limit's book at placement priced nothing"


# -- the route ---------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_open_position_is_a_row_with_its_book_variant_lots_and_money(settings, clock):
    (settings.data_dir / peer.FILE).write_text(json.dumps({"name": "phase 35"}))
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        clock[0] += 2
        async with _client(e) as c:
            d = (await c.get("/api/positions/live")).json()
        rows = {r["book"]: r for r in d["rows"]}
        assert set(rows) == {"FUDKII_RT_Y", "FUDKII_RT_Y_SE", "FUDKII_RT_Y_SA", "FUDKII_RT_Y_W1", "FUDKII_RT_Y_W1_SE", "FUDKII_RT_Y_W1_SA"}
        assert d["engine"] == "phase 35" and all(r["engine"] == "phase 35" for r in d["rows"])
        assert d["bookLimit"]["nowMs"] == 6000.0 and d["recordOnly"] is True and d["skipped"] == []
        y, se, w1, w1sa = rows["FUDKII_RT_Y"], rows["FUDKII_RT_Y_SE"], rows["FUDKII_RT_Y_W1"], rows["FUDKII_RT_Y_W1_SA"]
        assert (y["variant"], y["mirror"], y["shadow"], y["entryFrom"]) == ("current", False, False, None)
        assert (se["variant"], se["mirror"], se["shadow"], se["source_book"], se["entryFrom"]) == ("E", True, True, "FUDKII_RT_Y", "FUDKII_RT_Y")
        assert (w1["variant"], w1["mirror"], w1["shadow"], w1["entryFrom"]) == ("current", False, True, "FUDKII_RT_Y")
        assert w1["entryFromLabel"] == "FUDKII-RT-Y", "RT-Y's fill, named as RT-Y"
        assert (w1sa["variant"], w1sa["source_book"], w1sa["sourceShadow"]) == ("A", "FUDKII_RT_Y_W1", True)
        assert y["bookLabel"] == "FUDKII-RT-Y" and se["bookLabel"] == "FUDKII-RT-Y · stop E (shadow)"
        assert (y["lots"], y["lotSize"], y["qtyRemaining"], y["contract"]) == (4.0, 1000, 4000, OPT.name)
        assert y["entry"] == 17.10 and y["openedHm"] == "11:00" and y["triggerDirection"] == "BULLISH"
        # the book: 2 s old against the matcher's 6 s
        assert y["orderBook"] == {**y["orderBook"], "state": "fresh", "ageS": 2.0, "limitS": 6.0, "stale": False}
        assert y["orderBook"]["depth"]["bids"] == [[16.95, 50000]]
        # the money: the open lots at the bid, the charges paid and those an exit now would pay, from the charge model
        assert y["pnl"]["basis"] == "bid" and y["pnl"]["unrealised"] == pytest.approx((16.95 - 17.10) * 4000)
        exit_ch = e.costs.leg(OPT, OrderSide.SELL, 16.95, 4000).total
        assert y["money"]["exitCharges"]["total"] == pytest.approx(round(exit_ch, 2))
        pos = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        assert y["money"]["chargesSoFar"]["total"] == pytest.approx(round(pos.entry_charges, 2))
        assert y["pnl"]["band"] == pytest.approx(round(pos.entry_charges + exit_ch, 2), abs=0.01)
        assert y["pnl"]["state"] == "loss", "−₹600 is past the ₹208 the charges come to"
        assert y["money"]["exitNow"]["basis"] == "depth" and y["money"]["exitNow"]["avg"] == 16.95
        assert y["money"]["slippage"]["entry"]["vsRefRs"] == 0.0, "filled at the signal price"
        assert se["money"]["slippage"]["entry"]["copiedFrom"] == "FUDKII_RT_Y" and se["money"]["slippage"]["entry"]["fill"] == 17.10
        # the targets and the trade map
        assert [t["price"] for t in y["targets"]["equity"]] == [190.0, 192.0] and y["targets"]["equityStop"] == 185.0
        assert y["map"]["entry"] == 187.0 and y["map"]["live"]["underlying"] == 187.4 and y["map"]["position"]["entry"] == 17.10
        assert w1["map"]["stop"] < y["map"]["stop"], "the wide stop's own stop, not RT-Y's"
        # which way: the stock 0.21 % up from the trigger close, with the trade; no NIFTY 50 here, so the market is unknown
        assert y["winning"]["direction"] == "BULLISH" and y["winning"]["vsTrade"] == "with"
        assert y["winning"]["reasons"][0].startswith("stock +0.21% from the trigger close 187.00")
        assert y["market"]["aligned"] == "unknown"
        assert d["tookMs"] < 250
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_book_older_than_the_limit_is_stale_and_no_book_says_so(settings, clock):
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        clock[0] += 9  # the book is 9 s old against 6 s; the quote, 9 s, is fresh enough for the exit loop (60 s)
        async with _client(e) as c:
            y = next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")
            assert y["orderBook"]["state"] == "stale" and y["orderBook"]["stale"] and y["orderBook"]["ageS"] == 9.0
            ex = y["money"]["exitNow"]
            assert ex["basis"] == "shape" and ex["bookState"] == "stale" and ex["feed"] == "quote", "its shape under the quote's bid"
            assert ex["avg"] == 16.95 and ex["mid"] == pytest.approx(17.025)
            del e.books[OPT.scrip_code]
            y = next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")
            assert y["orderBook"]["state"] == "none" and y["orderBook"]["depth"] is None and y["orderBook"]["ageS"] is None
            assert y["money"]["exitNow"]["basis"] == "touch" and y["money"]["exitNow"]["avg"] == 16.95, "the quote's bid"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_deep_book_is_walked_and_each_target_share_estimated(settings, clock):
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        pos = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        pos.option_targets = (18.0, 19.0, 20.0)  # an own ladder, as RT-Y stamps from the contract's pivots
        bids = [(16.95, 1000), (16.90, 2000), (16.80, 5000)]
        e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=bids, asks=[(17.10, 5000)], ts=clock[0])
        async with _client(e) as c:
            y = next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")
        avg = (1000 * 16.95 + 2000 * 16.90 + 1000 * 16.80) / 4000
        ex = y["money"]["exitNow"]
        assert ex["avg"] == pytest.approx(avg, abs=1e-4) and ex["walk"]["levels"] == 3
        assert ex["vsTouchRs"] == pytest.approx((16.95 - avg) * 4000, abs=0.01)
        t = y["targets"]["option"]
        assert [(x["rung"], x["price"], x["share"], x["qty"]) for x in t] == [(1, 18.0, "1 lot", 1000), (2, 19.0, "1 lot", 1000), (3, 20.0, "the rest", 2000)]
        assert t[0]["slippage"]["avg"] == 18.0 and t[0]["slippage"]["rs"] == 0.0, "one lot sits at the touch"
        assert t[2]["slippage"]["avg"] == pytest.approx((1000 * 20.0 + 1000 * 19.95) / 2000) and t[2]["slippage"]["rs"] == pytest.approx(50.0)
        assert y["money"]["exitCharges"]["total"] == pytest.approx(round(e.costs.leg(OPT, OrderSide.SELL, 16.95, 4000).total, 2))
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_one_bad_row_is_named_and_never_costs_the_page(settings, clock, monkeypatch):
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        real = e._exit_plan

        def broken(key, *a, **k):
            if key is StrategyKey.FUDKII_RT_Y_SE:
                raise RuntimeError("boom")
            return real(key, *a, **k)

        monkeypatch.setattr(e, "_exit_plan", broken)
        async with _client(e) as c:
            r = await c.get("/api/positions/live")
        assert r.status_code == 200
        d = r.json()
        assert "FUDKII_RT_Y_SE" not in {x["book"] for x in d["rows"]} and len(d["rows"]) == 5
        assert d["skipped"] == [{"positionId": d["skipped"][0]["positionId"], "book": "FUDKII_RT_Y_SE", "error": "RuntimeError: boom"}]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_exits_refused_on_a_stale_book_are_counted_on_the_open_card(settings, clock):
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        pos = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        for i, age in enumerate((33190, 45629)):
            await e.ledger.insert_order({"id": f"o{i}", "client_order_id": f"c{i}", "strategy": pos.strategy, "symbol": "TATASTEEL",
                                         "scrip_code": OPT.scrip_code, "purpose": "EXIT", "status": "REJECTED", "mode": "PAPER", "ts": clock[0] + i,
                                         "position_id": pos.id, "exec": {"outcome": f"cross failed: book for TATASTEEL is {age} ms old (limit 6000)"}}, "REJECTED_BOOK")
        clock[0] += 3
        async with _client(e) as c:
            rows = {r["book"]: r for r in (await c.get("/api/positions/live")).json()["rows"]}
        assert rows["FUDKII_RT_Y"]["staleExits"] == {"count": 2, "maxAgeMs": 45629.0, "limitMs": 6000.0, "lastTs": clock[0] - 2}
        assert rows["FUDKII_RT_Y_SE"]["staleExits"] is None, "a mirror's refusals are its own"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_trigger_and_the_market_are_read_from_the_ledger_and_the_n50(settings, clock, monkeypatch):
    e = await _engine(settings, clock)
    try:
        sig = _sig(clock)
        await e.ledger.insert_signal(sig.to_json(), "PAPER_FILLED", "")
        await e.ledger.event("counter.route", {"signal_id": sig.signal_id, "symbol": "TATASTEEL", "route": "TREND", "legs": [
            {"name": "future", "close": 187.6, "atr": 1.2, "levels": {"1d.R1": 189.0, "1d.S1": 185.5, "1d.R2": 191.0}}]})
        await _open_rt_y(e, clock)
        fut = "FUT1"
        e._front_code[("TATASTEEL", ist_today())] = fut  # as the OI handler resolves it during the session
        e.ltps[fut] = 188.4
        reads = {None: {"with": 30, "against": 18, "flat": 0, "names": 48}, int(sig.ts): {"with": 22, "against": 26, "flat": 0, "names": 48}}
        monkeypatch.setattr(e, "n50_alignment", lambda direction, at_ts=None: reads[at_ts])
        async with _client(e) as c:
            y = next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")
        assert y["triggerId"] == sig.signal_id and y["triggerTs"] == sig.ts and y["map"]["rr"] == 1.5
        assert y["market"]["aligned"] == "yes" and "30 of 48" in y["market"]["why"]
        assert [t["price"] for t in y["targets"]["future"]] == [189.0, 191.0] and y["targets"]["futureClose"] == 187.6
        assert y["map"]["futLevels"]["behind"] == {"label": "1d.S1", "price": 185.5}
        why = y["winning"]["reasons"]
        assert any(r.startswith("future +0.43% from its close on the trigger bar 187.60") for r in why)
        assert any(r.startswith("NIFTY 50: 30 of 48 names past their open up, from 22 at the trigger") for r in why)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_closed_trades_exit_reason_carries_its_stale_book(settings, clock):
    e = await _engine(settings, clock)
    try:
        t0 = clock[0]
        await e.ledger.insert_trade({"id": "trd-1", "position_id": "pos-1", "strategy": "FUDKII_RT_Y", "symbol": "LTF 27 OCT 2026 CE 270.00",
                                     "underlying": "LTF", "opened_ts": t0 - 600, "closed_ts": t0 + 30, "gross": -6300.0, "net": -6448.0,
                                     "charges": 148.0, "r_multiple": -0.5, "exit_reason": "HALT"})
        for i, (status, ex) in enumerate((("REJECTED", {"outcome": "cross failed: book for LTF is 33190 ms old (limit 6000)"}),
                                          ("REJECTED", {"outcome": LTF_REFUSED}),
                                          ("FILLED", {"limit": 4.6, "filledTs": t0 + 30, "depthAtFill": {"ageMs": 614}}))):
            await e.ledger.insert_order({"id": f"o{i}", "client_order_id": f"c{i}", "strategy": "FUDKII_RT_Y", "symbol": "LTF", "scrip_code": "90713",
                                         "purpose": "EXIT", "status": status, "mode": "PAPER", "ts": t0 + i * 10, "position_id": "pos-1", "exec": ex}, status)
        async with _client(e) as c:
            plain = (await c.get("/api/trades")).json()[0]
            asked = (await c.get("/api/trades", params={"stale": 1})).json()[0]
            page = (await c.get("/api/trades", params={"depth": 1})).json()[0]
        assert "staleBook" not in plain, "only a caller that asks pays for it"
        assert asked["exit_reason"] == "HALT", "the exit reason itself is never changed"
        sb = asked["staleBook"]
        assert sb["stale"] and sb["refused"] == 2 and sb["maxAgeMs"] == 45629.0 and sb["depthAgeAtFillMs"] == 614.0 and not sb["fillStale"]
        assert sb["words"] == "order book stale (2 exits refused, 45.6 s)"
        assert page["staleBook"] == sb and "depthCheck" in page
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_twins_active_trades_are_read_through_the_forwarder(tmp_path, monkeypatch, settings, clock):
    (settings.data_dir / peer.FILE).write_text(json.dumps({"name": "phase 35"}))
    b = await _engine(settings, clock)
    try:
        await _open_rt_y(b, clock)
        twin_app = build_app(b)
        d = tmp_path / "a"
        d.mkdir()
        (d / peer.FILE).write_text(json.dumps({"name": "phase 34", "peer": {"name": "phase 35", "url": "http://127.0.0.1:8501"}}))
        a = build_app(type(b)(settings.model_copy(update={"data_dir": d, "db_url": f"sqlite+aiosqlite:///{d}/a.db"})))
        seen: list[httpx.URL] = []

        class Twin(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                seen.append(request.url)
                assert request.method == "GET"
                return await httpx.ASGITransport(app=twin_app).handle_async_request(request)

        monkeypatch.setattr(peer, "_client", lambda: httpx.AsyncClient(transport=Twin(), timeout=5))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") as c:
            r = await c.get("/api/peer/positions/live")
        assert r.status_code == 200 and seen[-1].path == "/api/positions/live"
        body = r.json()
        assert body["engine"] == "phase 35" and len(body["rows"]) == 6 and all(x["engine"] == "phase 35" for x in body["rows"])
    finally:
        await b.stop()


#: CONCOR 27 OCT 2026 PE 435 as engine B held it on 8 Oct 2026 at 12:34:03.6, set on the test contract: the depth book
#: 0.5 s old at 11.10 / 11.45 while the quote read 10.35 / 10.65 (mid 10.50). The exit loop marked the position on the
#: quote; RT-Y-F's stop-E mirror sold on TRAIL 0.03 s later at 10.45
CONCOR_BIDS = [(11.1, 1250), (11.05, 1250), (11.0, 2500), (10.95, 1250), (10.9, 1250)]
CONCOR_ASKS = [(11.45, 2500), (11.5, 3750), (11.55, 1250), (11.65, 2500), (11.7, 6250)]


@pytest.mark.asyncio
async def test_bid_ask_and_mid_are_the_quotes_and_an_exit_now_is_measured_on_the_book_it_walks(settings, clock):
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=CONCOR_BIDS, asks=CONCOR_ASKS, ts=clock[0] - 0.5)
        e.quotes[OPT.scrip_code] = Quote(ltp=10.5, bid=10.35, ask=10.65, ts=clock[0] - 3.5)
        e.ltps[OPT.scrip_code] = 10.5
        async with _client(e) as c:
            y = next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")
        # one feed for the four: the quote, which the exit loop judges the stop on
        assert (y["quote"]["ltp"], y["quote"]["bid"], y["quote"]["ask"], y["quote"]["mid"]) == (10.5, 10.35, 10.65, 10.5)
        # the open lots marked as risk/exits marks the excursions: at the quote's bid
        assert y["pnl"]["basis"] == "bid" and y["pnl"]["unrealised"] == pytest.approx((10.35 - 17.10) * 4000)
        assert y["money"]["exitCharges"]["price"] == 10.35
        # an exit now walks the fresh book, and is measured on that book's own touch and mid: never the quote's mid
        ex = y["money"]["exitNow"]
        avg = (1250 * 11.1 + 1250 * 11.05 + 1500 * 11.0) / 4000
        assert ex["basis"] == "depth" and ex["feed"] == "book" and ex["avg"] == pytest.approx(avg, abs=1e-4)
        assert ex["mid"] == pytest.approx(11.275) and ex["vsMidRs"] == pytest.approx((11.275 - avg) * 4000, abs=0.01) and ex["vsMidRs"] > 0
        assert ex["touch"] == 11.1 and ex["quoteTouch"] == 10.35, "the quote's bid beside it, where the two disagree"
        assert y["orderBook"]["bid"] == 11.1 and y["orderBook"]["ask"] == 11.45
        # net if closed now: from the walk's value, not its 4-place average
        pos = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        value = 1250 * 11.1 + 1250 * 11.05 + 1500 * 11.0
        want = (value - 17.10 * 4000) - pos.entry_charges - e.costs.leg(OPT, OrderSide.SELL, value / 4000, 4000).total
        assert y["pnl"]["netIfExitNow"] == pytest.approx(round(want, 2), abs=0.005)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_opening_window_judges_a_book_against_its_own_limit(settings, clock):
    """10 s from 09:00 to 09:55, 6 s after, both read from the settings through the matcher: a 9 s old book is fresh at
    09:30 and stale at 11:00."""
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        for hm, state, limit in (((9, 30), "fresh", 10.0), ((11, 0), "stale", 6.0)):
            clock[0] = datetime.combine(ist_today(), dtime(*hm), tzinfo=IST).timestamp()
            e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=[(16.95, 50_000)], asks=[(17.10, 50_000)], ts=clock[0] - 9)
            e.quotes[OPT.scrip_code] = Quote(ltp=17.0, bid=16.95, ask=17.10, ts=clock[0] - 1)
            async with _client(e) as c:
                d = (await c.get("/api/positions/live")).json()
            y = next(r for r in d["rows"] if r["book"] == "FUDKII_RT_Y")
            assert (y["orderBook"]["state"], y["orderBook"]["limitS"], d["bookLimit"]["nowMs"]) == (state, limit, limit * 1000), hm
            assert y["money"]["exitNow"]["basis"] == ("depth" if state == "fresh" else "shape"), hm
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_closed_cross_is_judged_against_the_limit_at_its_own_time(settings, clock):
    """The closed side reads the matcher's limit at the cross's instant: an 8 s old book crossed at 09:30 (limit 10 s) was
    not stale; the same at 11:00 (limit 6 s) was."""
    e = await _engine(settings, clock)
    try:
        for i, hm in enumerate(((9, 30), (11, 0))):
            at = datetime.combine(ist_today(), dtime(*hm), tzinfo=IST).timestamp()
            await e.ledger.insert_trade({"id": f"trd-{i}", "position_id": f"pos-{i}", "strategy": "FUDKII_RT_Y", "symbol": "LTF 27 OCT 2026 CE 270.00",
                                         "underlying": "LTF", "opened_ts": at - 600, "closed_ts": at + 1, "gross": -100.0, "net": -200.0,
                                         "charges": 100.0, "r_multiple": -0.1, "exit_reason": "SL-EQ"})
            await e.ledger.insert_order({"id": f"o{i}", "client_order_id": f"c{i}", "strategy": "FUDKII_RT_Y", "symbol": "LTF", "scrip_code": "90713",
                                         "purpose": "EXIT", "status": "FILLED", "mode": "PAPER", "ts": at, "position_id": f"pos-{i}",
                                         "exec": {"limit": 4.2, "crossedTs": at, "depthAtCross": {"ageMs": 8000}, "filledTs": at}}, "FILLED")
        async with _client(e) as c:
            rows = {t["id"]: t["staleBook"] for t in (await c.get("/api/trades", params={"stale": 1})).json()}
        assert not rows["trd-0"]["fillStale"] and rows["trd-0"]["fillLimitMs"] == 10_000.0
        assert rows["trd-1"]["fillStale"] and rows["trd-1"]["fillLimitMs"] == 6_000.0
        assert rows["trd-1"]["words"] == "order book stale (crossed on a 8.0 s old book, limit 6 s)"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_poll_before_the_contract_list_loads_never_decides_the_front_future(settings, clock):
    """After a restart the API serves while the contract list is still loading (main.py). The engine's own front-future
    read saves what it finds, None too, and its OI handler keeps to it for the day: the page only peeks (review, 2026-10-09)."""
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        async with _client(e) as c:
            r = await c.get("/api/positions/live")
        assert r.status_code == 200 and r.json()["count"] == 6
        assert ("TATASTEEL", ist_today()) not in e._front_code, "the page saved nothing"
        fut = Instrument("777001", "TATASTEEL", Segment.NSE_FO, InstrumentKind.FUTURE, name="TATASTEEL 24 NOV 2026 FUT",
                         expiry="2026-11-24", underlying="TATASTEEL")
        e.catalogue_loader.catalogue.futures_by_symbol["TATASTEEL"].append(fut)  # the list loads
        e._future_to_underlying[fut.scrip_code] = "TATASTEEL"
        seen: list[str] = []
        e.aggregator.set_oi = lambda code, **kw: seen.append(code)  # type: ignore[method-assign]
        await e._on_oi({"scrip_code": fut.scrip_code, "open_interest": 1_000_000, "oi_change_pct": 0.0, "recv_ts": clock[0]})
        assert seen == [UND.scrip_code], "the stock's bars still get their front month's OI"
        assert lt.front_future_peek(e, "TATASTEEL") == fut.scrip_code
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_value_that_is_not_a_number_never_fails_the_page(settings, clock):
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        e.ltps[UND.scrip_code] = float("nan")  # a bad print on the stock
        async with _client(e) as c:
            r = await c.get("/api/positions/live")
        assert r.status_code == 200
        d = r.json()
        assert d["count"] == 6 and d["rows"][0]["map"]["live"]["underlying"] is None
        assert any("not a number" in n for n in d["notes"])
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_refused_exits_are_read_as_they_are_stored_and_never_counted_twice(settings, clock):
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        pos = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")

        async def refuse(i: int, age: int) -> None:
            await e.ledger.insert_order({"id": f"o{i}", "client_order_id": f"c{i}", "strategy": pos.strategy, "symbol": "TATASTEEL",
                                         "scrip_code": OPT.scrip_code, "purpose": "EXIT", "status": "REJECTED", "mode": "PAPER", "ts": clock[0] + i,
                                         "position_id": pos.id, "exec": {"outcome": f"cross failed: book for TATASTEEL is {age} ms old (limit 6000)"}}, "REJECTED_BOOK")

        async with _client(e) as c:
            async def y() -> dict:
                return next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")

            await refuse(0, 9000)
            assert (await y())["staleExits"]["count"] == 1
            assert (await y())["staleExits"]["count"] == 1, "a second poll reads nothing new"
            await refuse(1, 12000)
            # both pages poll at once: one read at a time, so the new row is added once
            both = await asyncio.gather(c.get("/api/positions/live"), c.get("/api/positions/live"))
            for r in both:
                got = next(x for x in r.json()["rows"] if x["book"] == "FUDKII_RT_Y")["staleExits"]
                assert (got["count"], got["maxAgeMs"]) == (2, 12000.0)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_future_oi_backs_the_winning_way_only_with_the_price_over_the_same_window(settings, clock, monkeypatch):
    """OI added since the previous close says nothing of a direction alone: it backs "up" when the stock is also up from that
    close (a long build-up), and only on OI printed in today's session (review, 2026-10-09)."""
    from kotsin_nse.bars.oi_read import OiReading
    from kotsin_nse.bars.unified import BarSource, UnifiedBar
    from kotsin_nse.market.session import session_open_ts

    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)  # the stock 187.40, 0.21 % up from the trigger close 187.00: winning up, with the trade
        monkeypatch.setattr(e.calendar, "is_trading_day", lambda d: True)
        monkeypatch.setattr(e, "oi_reading", lambda symbol, **k: OiReading(change_pct=3.2, contracts=("FUT1",)))
        opened = session_open_ts(Segment.NSE_FO, ist_today())
        yday = int(datetime.combine(ist_today(), dtime(0, 0), tzinfo=IST).timestamp()) - 86_400

        def prev(close: float) -> None:
            e.store.seed("TATASTEEL", "1d", [UnifiedBar(symbol="TATASTEEL", scrip_code=UND.scrip_code, tf="1d", ts=yday, open=close, high=close,
                                                        low=close, close=close, volume=1.0, source=BarSource.REST, complete=True)])

        async def reasons() -> list[str]:
            async with _client(e) as c:
                return next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")["winning"]["reasons"]

        e._fut_oi["FUT1"] = (1_000_000.0, opened + 60)
        prev(186.0)  # the stock +0.75 % from the previous close: the same way
        assert any(r.startswith("future OI +3.2% since the previous close and the stock +0.75% from that close 186.00: long build-up") for r in await reasons())
        prev(188.0)  # the stock down from the previous close: the OI says nothing for "up"
        assert not any("OI" in r for r in await reasons())
        prev(186.0)
        e._fut_oi["FUT1"] = (1_000_000.0, opened - 600)  # yesterday's level re-sent at a subscribe, before today's open
        assert not any("OI" in r for r in await reasons())
    finally:
        await e.stop()


# -- record-only: nothing in the order path reaches it -----------------------------------------------------------------

PKG = Path(__file__).resolve().parents[1] / "kotsin_nse"


def _imports_live_trades(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith("live_trades") or any(a.name == "live_trades" for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(a.name.endswith("live_trades") for a in node.names):
            return True
    return False


def test_only_the_api_routes_import_the_active_trades_reads():
    importers = {p.relative_to(PKG).as_posix() for p in PKG.rglob("*.py") if p.name != "live_trades.py" and _imports_live_trades(p)}
    assert importers == {"api/routes.py"}, importers


def test_the_order_path_never_loads_the_active_trades_reads():
    code = ("import sys, kotsin_nse.engine, kotsin_nse.exec.gateway, kotsin_nse.exec.paper, kotsin_nse.exec.resting, "
            "kotsin_nse.risk.exits; print('kotsin_nse.api.live_trades' in sys.modules, any(m.startswith('kotsin_nse.api') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=PKG.parent).stdout.split()
    assert out == ["False", "False"], out


@pytest.mark.asyncio
async def test_a_contract_with_no_bid_is_marked_on_its_last_trade(settings, clock):
    """No depth book and a quote with no bid (a quiet strike): the open lots are marked on the last trade, as the engine marks
    its excursions, and an exit now has nothing to sell into."""
    e = await _engine(settings, clock)
    try:
        await _open_rt_y(e, clock)
        del e.books[OPT.scrip_code]
        e.quotes[OPT.scrip_code] = Quote(ltp=17.0, bid=0.0, ask=0.0, ts=clock[0])
        e.ltps[OPT.scrip_code] = 17.0
        async with _client(e) as c:
            y = next(r for r in (await c.get("/api/positions/live")).json()["rows"] if r["book"] == "FUDKII_RT_Y")
        assert y["pnl"]["basis"] == "last" and y["pnl"]["unrealised"] == pytest.approx((17.0 - 17.10) * 4000)
        assert y["money"]["exitNow"]["basis"] == "none" and y["orderBook"]["state"] == "none"
    finally:
        await e.stop()
