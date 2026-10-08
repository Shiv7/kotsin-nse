"""The depth check: paper fills against the book recorded with them, record-only (operator, 2026-10-09: "go ahead
build it n tell me where to view it").

The paper matcher fills a resting order WHOLE at its limit, with no queue and no share of the depth. On 108 of 206
depth-logged fills the book showed fewer lots at the price than we traded. The check reads the stored book and
reports how much was visible at the price, what the whole quantity would really have averaged, and how many
rupees kinder the paper fill was. The API shows it on /api/trades (``depthCheck``) and /api/depth-check. Nothing
in the order path may import it."""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

import httpx
import pytest

from kotsin_nse.api.routes import ENTRY_SOURCES, build_app
from kotsin_nse.config import Settings
from kotsin_nse.engine import Engine
from kotsin_nse.exec.depth_check import (
    THIN_BELOW,
    check_trades,
    depth_check,
    depth_check_for_order,
    depth_check_for_trade,
    index_orders,
    lot_size_from_tags,
    orders_for_trade,
    summarize,
)
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.market.session import IST, ist_today

# -- one fill against its book ------------------------------------------------------------------------


def test_a_resting_buy_the_ask_came_down_to_is_judged_on_the_book_at_the_fill_and_is_full():
    # ASTRAL 27 OCT PE 1400, FUDKII_RT_Y_F, 8 Oct 15:15: limit 26.00, the ask came down to 25.70
    c = depth_check("BUY", 1700, 26.0, 26.0, {"bids": [[25.25, 850]], "asks": [[25.7, 425], [25.75, 850], [25.8, 425], [25.85, 425], [25.9, 425]],
                                              "ageMs": 674}, 425, "fill")
    assert c["verdict"] == "full" and c["source"] == "fill" and c["ageMs"] == 674 and not c["stale"]
    assert c["visibleQty"] == 2550 and c["visibleLots"] == 6 and c["lots"] == 4 and c["shortfallLots"] == 0 and c["covered"] == 1.0
    assert c["realisticPrice"] == 26.0 and c["optimismRs"] == 0.0, "every lot fills at the limit: the paper was right"
    assert "enough for all 4 lots" in c["words"]


def test_a_partial_book_walks_the_rest_up_the_asks():
    c = depth_check("BUY", 1700, 26.0, 26.0, {"asks": [[26.0, 850], [26.05, 425], [26.1, 850]], "ageMs": 300}, 425, "fill")
    assert c["verdict"] == "partial" and c["covered"] == 0.5 and c["visibleLots"] == 2 and c["shortfallLots"] == 2
    # 2 lots at 26.00, the 3rd at 26.05, the 4th at 26.10
    assert c["realisticPrice"] == pytest.approx(26.0375) and c["optimismRs"] == pytest.approx(63.75)
    assert not c["beyondBook"] and "₹63.75 kinder" in c["words"]


def test_a_target_sell_on_a_book_with_no_bid_at_its_price_is_thin():
    c = depth_check("SELL", 750, 18.45, 18.45, {"bids": [[18.35, 750], [18.3, 750]], "asks": [[18.6, 750]], "ageMs": 474}, 750, "fill")
    assert c["verdict"] == "thin" and c["visibleQty"] == 0 and c["shortfallLots"] == 1 and c["covered"] == 0.0
    assert c["realisticPrice"] == 18.35 and c["optimismRs"] == pytest.approx(75.0), "the lot walks down to the 18.35 bid"
    assert c["touch"] == 18.35 and c["touchQty"] == 750
    assert THIN_BELOW == 0.5
    edge = depth_check("BUY", 4, 10.0, 10.0, {"asks": [[9.95, 1], [10.2, 10]]}, 1, "fill")
    assert edge["covered"] == 0.25 and edge["verdict"] == "thin", "a quarter shown: thin"


def test_no_book_is_said_and_never_guessed():
    for depth in (None, {}, {"asks": [], "bids": [[10.0, 5]]}, "not a book", 42):
        c = depth_check("BUY", 100, 10.0, 10.0, depth, 50, "fill")
        assert c["verdict"] == "no book" and c["optimismRs"] is None and c["realisticPrice"] is None and c["source"] is None
    assert depth_check("BUY", 100, 10.0, 10.0, {"bids": [[9.9, 100]]}, 50, "fill")["words"] == "the recorded book had no asks"
    assert depth_check("SELL", 100, 10.0, 10.0, None, 50, None)["words"] == "no book recorded"


def test_a_malformed_book_or_fill_never_raises():
    junk = {"asks": [["x", 1], [None], [26.0], {"price": 26.0, "qty": 425}, [-1, 5], [26.05, "nan"], "lvl", [float("inf"), 3]],
            "ageMs": "soon"}
    c = depth_check("BUY", 425, 26.0, 26.0, junk, 425, "fill")
    assert c["verdict"] == "full" and c["visibleQty"] == 425 and c["ageMs"] is None, "the one good level, in either shape"
    for args in (("BUY", None, 26.0), ("BUY", 0, 26.0), ("SELL", 100, None), ("SELL", "abc", "xyz"), (None, 100, 10.0)):
        side, qty, px = args
        assert depth_check(side, qty, px, px, junk, None, "fill")["verdict"] in {"no book", "full", "partial", "thin"}
    assert depth_check("BUY", 100, 26.0, 26.0, junk, "lots", "fill")["lotSize"] == 1, "a lot size that is not a number: units"
    assert depth_check_for_order({"status": "FILLED", "filled": 5, "avg_price": 1.0, "side": "BUY", "exec": "junk"}) is not None
    assert depth_check_for_order({"status": "FILLED", "exec": {"depthAtFill": {"asks": "x"}}})["verdict"] == "no book"
    for o in (None, "order", 3, [], {"status": "REJECTED", "filled": 0}, {"status": "SHADOW"}):
        assert depth_check_for_order(o) is None, "an order that did not fill has nothing to check"
    assert depth_check_for_trade({}, [None, "x", {"status": "FILLED"}])["verdict"] == "no book"  # type: ignore[list-item]


def test_buy_and_sell_are_mirror_images():
    buy = depth_check("BUY", 4, 100.0, 100.0, {"asks": [[100.0, 2], [101.0, 2]]}, 1, "fill")
    sell = depth_check("SELL", 4, 100.0, 100.0, {"bids": [[100.0, 2], [99.0, 2]]}, 1, "fill")
    assert buy["realisticPrice"] == 100.5 and sell["realisticPrice"] == 99.5
    assert buy["optimismRs"] == sell["optimismRs"] == 2.0, "positive: the paper was kinder either way"
    assert buy["verdict"] == sell["verdict"] == "partial" and buy["visibleQty"] == sell["visibleQty"] == 2
    # the wrong side of the book is never read: a buy ignores bids, a sell ignores asks
    assert depth_check("BUY", 4, 100.0, 100.0, {"bids": [[100.0, 99]], "asks": [[100.0, 4]]}, 1, "fill")["verdict"] == "full"
    assert depth_check("SELL", 4, 100.0, 100.0, {"asks": [[100.0, 99]], "bids": [[100.0, 4]]}, 1, "fill")["verdict"] == "full"
    # levels out of order are put in order: the best first
    shuffled = depth_check("SELL", 4, 100.0, 100.0, {"bids": [[99.0, 2], [100.0, 2]]}, 1, "fill")
    assert shuffled["realisticPrice"] == 99.5 and shuffled["touch"] == 100.0


def test_lots_are_whole_lots_the_book_can_fill():
    # 2,100 units shown on a 425 lot is 4 whole lots (4.94), not 5
    c = depth_check("BUY", 2125, 26.0, 26.0, {"asks": [[26.0, 2100], [26.05, 425]]}, 425, "fill")
    assert c["lots"] == 5 and c["visibleQty"] == 2100 and c["visibleLots"] == 4 and c["shortfallLots"] == 1
    assert c["verdict"] == "partial" and c["covered"] == pytest.approx(0.988, abs=1e-3)
    assert c["optimismRs"] == pytest.approx(25 * 0.05), "25 units over: 25 × 5 paise"
    odd = depth_check("BUY", 500, 10.0, 10.0, {"asks": [[10.0, 1000]]}, 425, "fill")
    assert odd["lots"] == 1.18 and odd["verdict"] == "full", "a quantity that is not whole lots is shown to two places"


def test_beyond_the_book_is_priced_at_the_last_level_and_flagged():
    c = depth_check("BUY", 500, 10.0, 10.0, {"asks": [[10.0, 100], [10.05, 100], [10.1, 100]]}, 50, "fill")
    assert c["beyondBook"] and c["beyondQty"] == 200
    # 100 at 10.00, 100 at 10.05, 100 at 10.10 and the 200 past the book at 10.10 as well
    assert c["realisticPrice"] == pytest.approx(10.07) and c["optimismRs"] == pytest.approx(35.0)
    assert "4 lots past the levels shown" in c["words"]
    # every level shown better than the limit, too few of them: what is past the book is never priced better than the paper
    best = depth_check("BUY", 1700, 26.0, 26.0, {"asks": [[25.7, 425], [25.75, 850]]}, 425, "fill")
    assert best["beyondBook"] and best["realisticPrice"] == 26.0 and best["optimismRs"] == 0.0 and best["verdict"] == "partial"
    sell = depth_check("SELL", 300, 10.0, 10.0, {"bids": [[9.9, 100]]}, 1, "fill")
    assert sell["realisticPrice"] == 9.9 and sell["optimismRs"] == pytest.approx(30.0) and sell["beyondQty"] == 200


def test_a_walked_order_is_judged_level_by_level_not_at_its_average():
    # SOLARINDS 19000 PE, RT-N stop-adaptive mirror, 8 Oct 15:00: the EOD exit crossed at its deadline and walked four bids
    bids = [[285.3, 50], [285.2, 50], [285.1, 50], [285.05, 50], [285.0, 50]]
    c = depth_check("SELL", 200, 285.16, None, {"bids": bids, "ageMs": 717}, 50, "cross")
    assert c["market"] and c["verdict"] == "full" and c["visibleLots"] == 4
    assert c["realisticPrice"] == pytest.approx(285.1625) and abs(c["optimismRs"]) < 1, "the matcher's own walk: only its rounding"
    short = depth_check("SELL", 300, 285.0, None, {"bids": bids[:2]}, 50, "place")
    assert short["verdict"] == "thin" and short["beyondQty"] == 200, "the walk past two levels: thin, the rest beyond the book"
    assert short["realisticPrice"] == pytest.approx((285.3 * 50 + 285.2 * 250) / 300), "a walk prices what is past the book at the last level"
    assert short["optimismRs"] < 0, "this paper fill sold for less than that book: harsher, said so"


def test_a_stale_book_is_flagged_and_a_multiplier_scales_the_rupees():
    c = depth_check("SELL", 750, 18.45, 18.45, {"bids": [[18.35, 750]], "ageMs": 8000}, 750, "fill")
    assert c["stale"] and "(the book was 8.0 s old)" in c["words"]
    mcx = depth_check("BUY", 2, 100.0, 100.0, {"asks": [[100.0, 1], [101.0, 5]]}, 1, "fill", multiplier=10)
    assert mcx["optimismRs"] == pytest.approx(10.0), "1 unit a rupee worse × 10 per point"


# -- a stored order, a trade ------------------------------------------------------------------------------

T0 = 1791452705.0
LOT = 425


def _order(oid: str, book: str, side: str, purpose: str, qty: int, price: float, ex: dict | None, *, pos: str | None = None,
           sid: str = "FUDKII-ASTRAL-1791450900-B", tag: str = "EN", ts: float = T0) -> dict:
    return {**({"exec": ex} if ex is not None else {}), "id": oid, "client_order_id": f"FII-RY-261008-151505-010-{tag}-ASTRAL-1400PE-L4",
            "strategy": book, "scrip_code": "55903", "symbol": "ASTRAL", "side": side, "purpose": purpose, "qty": qty, "mode": "PAPER",
            "status": "FILLED", "signal_id": sid, "position_id": pos, "reason": "", "ts": ts, "avg_price": price, "filled": qty,
            "charges": 0.0, "slippage_bps": 0.0, "broker_order_id": "", "note": ""}


def _entry(book: str = "FUDKII_RT_Y", ts: float = T0, **kw) -> dict:
    ex = {"kind": "entry", "limit": 26.0, "placedTs": ts, "waitS": 1.9, "filledTs": ts + 2, "fillPrice": 26.0,
          "bookAtFill": {"bid": 25.95, "ask": 26.0}, "depthAtFill": {"asks": [[26.0, 850], [26.05, 425], [26.1, 850]], "ageMs": 300},
          "depthAtPlace": {"asks": [[26.3, 5000]], "ageMs": 200}}
    return _order(f"ord-en-{book}", book, "BUY", "ENTRY", 1700, 26.0, ex, ts=ts, **kw)


def _exits(pos: str, book: str = "FUDKII_RT_Y", ts: float = T0) -> list[dict]:
    target = {"kind": "target", "limit": 27.0, "placedTs": ts + 2, "waitS": 300.0, "filledTs": ts + 302, "fillPrice": 27.0,
              "bookAtFill": {"bid": 27.0, "ask": 27.2}, "depthAtFill": {"bids": [[27.0, 2000]], "ageMs": 100}}
    cross = {"kind": "exit", "limit": 27.1, "placedTs": ts + 590, "waitS": 10.2, "crossedTs": ts + 600, "filledTs": ts + 600,
             "fillPrice": 26.85, "depthAtPlace": {"bids": [[27.5, 5000]], "ageMs": 50},
             "depthAtCross": {"bids": [[26.9, 425], [26.8, 425]], "ageMs": 400}}
    return [_order(f"ord-t1-{book}", book, "SELL", "EXIT", 850, 27.0, target, pos=pos, tag="T1V0", ts=ts + 2),
            _order(f"ord-x-{book}", book, "SELL", "EXIT", 850, 26.85, cross, pos=pos, tag="EOD1X", ts=ts + 600)]


def _trade(tid: str, pos: str, book: str = "FUDKII_RT_Y", opened: float = T0 + 2, **kw) -> dict:
    return {"id": tid, "position_id": pos, "strategy": book, "symbol": "ASTRAL 27 OCT 2026 PE 1400.00", "underlying": "ASTRAL", "qty": 1700,
            "entry": 26.0, "exit": 26.93, "gross": 1572.5, "net": 1400.0, "charges": 172.5, "r_multiple": 0.5, "exit_reason": "EOD",
            "opened_ts": opened, "closed_ts": opened + 598, "signal_id": "FUDKII-ASTRAL-1791450900-B", "multiplier": 1, **kw}


def test_a_stored_order_is_judged_on_the_book_at_its_fill_and_a_cross_on_the_book_it_walked():
    e = depth_check_for_order(_entry(), lot_size=LOT)
    assert e is not None and e["source"] == "fill" and e["verdict"] == "partial" and e["optimismRs"] == pytest.approx(63.75)
    assert e["purpose"] == "ENTRY" and e["kind"] == "entry" and e["filledTs"] == T0 + 2 and not e["market"] and not e["onPrint"]
    t1, x = (depth_check_for_order(o, lot_size=LOT) for o in _exits("pos-1"))
    assert t1 is not None and t1["source"] == "fill" and t1["verdict"] == "full" and t1["optimismRs"] == 0.0
    assert x is not None and x["source"] == "cross" and x["crossed"] and x["market"] and x["verdict"] == "full"
    assert x["realisticPrice"] == 26.85 and x["limit"] is None, "the cross went out with no limit: the old one does not judge it"


def test_the_book_at_placement_judges_only_a_fill_at_placement():
    ex = {"kind": "entry", "limit": 26.0, "waitS": 30.0, "depthAtPlace": {"asks": [[26.0, 5000]], "ageMs": 10}}
    late = depth_check_for_order(_order("o1", "FUDKII", "BUY", "ENTRY", 1700, 26.0, ex), lot_size=LOT)
    assert late is not None and late["verdict"] == "no book" and late["words"] == "only the book at placement, 30 s before the fill"
    quick = depth_check_for_order(_order("o2", "FUDKII", "BUY", "ENTRY", 1700, 26.0, {**ex, "waitS": 0.4}), lot_size=LOT)
    assert quick is not None and quick["source"] == "place" and quick["verdict"] == "full"
    market = {"kind": "exit", "limit": None, "why": "market — walked the book", "depthAtPlace": {"bids": [[26.0, 1700]], "ageMs": 5}}
    m = depth_check_for_order(_order("o3", "FUDKII", "SELL", "EXIT", 1700, 26.0, market, pos="p"), lot_size=LOT)
    assert m is not None and m["source"] == "place" and m["market"] and m["verdict"] == "full"
    old = depth_check_for_order(_order("o4", "FUDKII", "BUY", "ENTRY", 1700, 26.0, None), lot_size=LOT)
    assert old is not None and old["verdict"] == "no book" and old["words"] == "no book recorded", "an order from before 3 Oct 17:51"


def test_a_fill_on_a_trade_print_through_the_price_says_so():
    ex = {"kind": "target", "limit": 18.45, "waitS": 40.0, "bookAtFill": {"bid": 18.35, "ask": 18.6},
          "depthAtFill": {"bids": [[18.35, 750]], "ageMs": 474}}
    c = depth_check_for_order(_order("o", "FUDKII_RT_N", "SELL", "EXIT", 750, 18.45, ex, pos="p"), lot_size=750)
    assert c is not None and c["onPrint"] and c["verdict"] == "thin" and "filled on a trade printing through 18.45, the bid then 18.35" in c["words"]


def test_a_trade_sums_its_entry_and_every_exit_and_finds_its_lot_size_in_the_order_ids():
    orders = [_entry(), *_exits("pos-1")]
    assert lot_size_from_tags(_trade("t", "pos-1"), orders) == LOT, "L4 on a 1,700-unit position: 425 a lot"
    dc = depth_check_for_trade(_trade("t", "pos-1"), orders)
    assert dc["lotSize"] == LOT and dc["fills"] == 3 and dc["checked"] == 3 and dc["verdict"] == "partial" and dc["stale"] == 0
    assert dc["entry"]["optimismRs"] == pytest.approx(63.75) and dc["entry"]["verdict"] == "partial"
    assert dc["exit"]["fills"] == 2 and dc["exit"]["optimismRs"] == 0.0 and dc["exit"]["verdict"] == "full"
    assert dc["optimismRs"] == pytest.approx(63.75) and dc["entryFrom"] is None
    assert [o["kind"] for o in dc["exit"]["orders"]] == ["target", "exit"], "the exits in the order they filled"


def test_a_mirror_is_judged_on_the_entry_fill_it_copied():
    rt_y = [_entry("FUDKII_RT_Y"), *_exits("pos-1")]
    mirror_exit = _order("ord-sa", "FUDKII_RT_Y_SA", "SELL", "EXIT", 1700, 26.5,
                         {"kind": "exit", "limit": 26.5, "waitS": 3.0, "depthAtFill": {"bids": [[26.4, 1700]], "ageMs": 90}}, pos="pos-2")
    idx = index_orders([*rt_y, mirror_exit, {**_entry("FUDKII_RT_X"), "status": "REJECTED", "filled": 0}])
    mine, copied = orders_for_trade(_trade("t2", "pos-2", "FUDKII_RT_Y_SA"), idx, sources=ENTRY_SOURCES)
    assert copied == "FUDKII_RT_Y" and [o["id"] for o in mine] == ["ord-en-FUDKII_RT_Y", "ord-sa"]
    assert orders_for_trade(_trade("t3", "pos-3", "FUDKII_RT_X"), idx, sources=ENTRY_SOURCES) == ([], None), "a refused order is no fill"
    # the wide-stop shadow's mirror: two hops, W1_SA → W1 → RT-Y
    assert orders_for_trade(_trade("t4", "pos-4", "FUDKII_RT_Y_W1_SA"), idx, sources=ENTRY_SOURCES)[1] == "FUDKII_RT_Y"
    checks = check_trades([_trade("t1", "pos-1"), _trade("t2", "pos-2", "FUDKII_RT_Y_SA")], [*rt_y, mirror_exit],
                          positions=[{"id": "pos-2", "instrument": {"lot_size": LOT}}], sources=ENTRY_SOURCES)
    m = checks["t2"]
    assert m["entryFrom"] == "FUDKII_RT_Y" and m["entry"]["optimismRs"] == pytest.approx(63.75)
    assert m["exit"]["verdict"] == "thin" and m["exit"]["optimismRs"] == pytest.approx(170.0) and m["optimismRs"] == pytest.approx(233.75)
    s = summarize([_trade("t1", "pos-1"), _trade("t2", "pos-2", "FUDKII_RT_Y_SA")], checks, order=["FUDKII_RT_Y_SA", "FUDKII_RT_Y"])
    assert [b["book"] for b in s["books"]] == ["FUDKII_RT_Y_SA", "FUDKII_RT_Y"], "in the order asked for"
    assert s["total"] == {"trades": 2, "fills": 5, "checked": 5, "stale": 0, "optimismRs": pytest.approx(297.5),
                          "byVerdict": {"full": 2, "partial": 2, "thin": 1, "no book": 0}}
    assert [w["tradeId"] for w in s["worst"]] == ["t2", "t1"] and s["worst"][0]["entryRs"] == pytest.approx(63.75)


# -- the API: read at request time, nothing written ----------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(15, 30), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


async def _ledger(tmp_path, clock) -> Engine:
    s = Settings(_env_file=None, data_dir=tmp_path, db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", engine_enabled=False)
    e = Engine(s)
    await e.ledger.init()
    t0 = clock[0] - 3_600
    for o in [_entry("FUDKII_RT_Y", t0), *_exits("pos-1", ts=t0)]:
        await e.ledger.insert_order(o, "PAPER_FILLED")
    await e.ledger.insert_order(_order("ord-sa", "FUDKII_RT_Y_SA", "SELL", "EXIT", 1700, 26.5,
                                       {"kind": "exit", "limit": 26.5, "waitS": 3.0, "depthAtFill": {"bids": [[26.4, 1700]], "ageMs": 90}},
                                       pos="pos-2", tag="EOD0", ts=t0 + 590), "PAPER_FILLED")
    await e.ledger.insert_order(_order("ord-old", "FUDKII", "BUY", "ENTRY", 1700, 30.0, None, sid="SIG-OLD", tag="OLD",
                                       ts=t0 - 7 * 86_400), "PAPER_FILLED")
    for pid, book, opened in (("pos-1", "FUDKII_RT_Y", t0 + 2), ("pos-2", "FUDKII_RT_Y_SA", t0 + 2), ("pos-9", "FUDKII", t0 - 7 * 86_400)):
        await e.ledger.upsert_position({"id": pid, "strategy": book, "symbol": "ASTRAL", "scrip_code": "55903", "status": "CLOSED",
                                        "opened_ts": opened, "closed_ts": opened + 598, "instrument": {"lot_size": LOT}})
    await e.ledger.insert_trade(_trade("trd-1", "pos-1", "FUDKII_RT_Y", t0 + 2))
    await e.ledger.insert_trade(_trade("trd-2", "pos-2", "FUDKII_RT_Y_SA", t0 + 2))
    await e.ledger.insert_trade(_trade("trd-9", "pos-9", "FUDKII", t0 - 7 * 86_400, signal_id="SIG-OLD"))
    return e


def _client(e: Engine) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t")


@pytest.mark.asyncio
async def test_the_trades_listing_carries_each_trades_depth_check_and_writes_nothing(tmp_path, clock):
    e = await _ledger(tmp_path, clock)
    before = await e.ledger.counts()
    async with _client(e) as c:
        rows = {r["id"]: r for r in (await c.get("/api/trades?books=all")).json()}
    assert await e.ledger.counts() == before, "read at request time: no row written"
    one, mirror, old = rows["trd-1"]["depthCheck"], rows["trd-2"]["depthCheck"], rows["trd-9"]["depthCheck"]
    assert one["verdict"] == "partial" and one["optimismRs"] == pytest.approx(63.75) and one["lotSize"] == LOT
    assert one["entry"]["orders"][0]["visibleLots"] == 2 and one["entry"]["orders"][0]["shortfallLots"] == 2
    assert [o["source"] for o in one["exit"]["orders"]] == ["fill", "cross"]
    assert mirror["entryFrom"] == "FUDKII_RT_Y" and mirror["optimismRs"] == pytest.approx(233.75) and mirror["verdict"] == "thin"
    assert mirror["entryFromLabel"] == "FUDKII-RT-Y", "the page names the book whose fill the mirror copied"
    assert old["verdict"] == "no book" and old["optimismRs"] is None and old["fills"] == 1 and old["checked"] == 0
    assert rows["trd-1"]["strategy_label"] == "FUDKII-RT-Y", "the other served fields are untouched"
    async with _client(e) as c:
        trading = (await c.get("/api/trades?books=trading")).json()
    assert {r["id"] for r in trading} == {"trd-1", "trd-9"} and all("depthCheck" in r for r in trading)


@pytest.mark.asyncio
async def test_a_failing_depth_check_never_costs_the_trades_page(tmp_path, clock, monkeypatch):
    e = await _ledger(tmp_path, clock)

    def boom(*a, **k):
        raise RuntimeError("bad book")

    monkeypatch.setattr("kotsin_nse.exec.depth_check.check_trades", boom)
    async with _client(e) as c:
        r = await c.get("/api/trades")
    assert r.status_code == 200 and len(r.json()) == 3 and all(t["depthCheck"] is None for t in r.json())


@pytest.mark.asyncio
async def test_the_depth_check_route_sums_each_book(tmp_path, clock):
    e = await _ledger(tmp_path, clock)
    before = await e.ledger.counts()
    async with _client(e) as c:
        today = (await c.get("/api/depth-check")).json()
        start = (await c.get("/api/depth-check?since=start&books=all")).json()
        mirrors = (await c.get("/api/depth-check?since=today&books=mirrors")).json()
        bad = await c.get("/api/depth-check?since=yesterday")
    assert await e.ledger.counts() == before
    assert bad.status_code == 422
    assert today["scope"] == "today" and today["bookSet"] == "trading" and today["thinBelow"] == THIN_BELOW
    assert [b["book"] for b in today["books"]] == ["FUDKII_RT_Y"], "today's trading books: the old FUDKII trade closed a week ago"
    rt = today["books"][0]
    assert rt["label"] == "FUDKII-RT-Y" and rt["shadow"] is False and rt["trades"] == 1 and rt["fills"] == 3 and rt["checked"] == 3
    assert rt["byVerdict"] == {"full": 2, "partial": 1, "thin": 0, "no book": 0} and rt["optimismRs"] == pytest.approx(63.75)
    assert rt["worst"][0]["tradeId"] == "trd-1" and rt["worst"][0]["label"] == "FUDKII-RT-Y"
    assert [b["book"] for b in mirrors["books"]] == ["FUDKII_RT_Y_SA"] and mirrors["books"][0]["shadow"] is True
    books = {b["book"]: b for b in start["books"]}
    assert set(books) == {"FUDKII", "FUDKII_RT_Y", "FUDKII_RT_Y_SA"} and list(books)[0] == "FUDKII", "in the registry's order"
    assert books["FUDKII"]["byVerdict"]["no book"] == 1 and books["FUDKII"]["checked"] == 0 and books["FUDKII"]["worst"] == []
    assert start["total"]["trades"] == 3 and start["total"]["optimismRs"] == pytest.approx(297.5)
    assert [w["tradeId"] for w in start["worst"]] == ["trd-2", "trd-1"] and start["since"] == pytest.approx(clock[0] - 3_600 + 2 + 598 - 7 * 86_400)


@pytest.mark.asyncio
async def test_a_real_paper_entry_is_stored_with_the_book_the_check_reads(settings, clock):
    """An entry filled through the engine's own paper path stores the book that the check judges it on. The position
    keeps the paper fill: the check changes nothing."""
    from tests.test_limit_orders import OPT, _engine, _sig

    e = await _engine(settings, clock)
    try:
        def book(asks: list[tuple[float, int]]) -> None:
            from kotsin_nse.instrument.select import Quote

            e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=[(16.95, 50_000)], asks=asks, ts=clock[0])
            e.quotes[OPT.scrip_code] = Quote(ltp=17.0, bid=16.95, ask=asks[0][0], ts=clock[0])
            e.ltps[OPT.scrip_code] = 17.0

        book([(17.25, 50_000)])
        await e._handle_signal(_sig(clock), None)
        clock[0] += 5
        book([(17.05, 1_000), (17.20, 100_000)])  # the ask comes down through the limit, one lot deep
        await e._manage_positions()
        pos = next(p for p in e.positions.values() if p.strategy == "FUDKII")
        assert pos.entry == 17.10, "the paper fills the whole order at its limit"
        order = next(o for o in await e.ledger.rows_between("orders", 0, clock[0] + 1) if o["purpose"] == "ENTRY")
        c = depth_check_for_order(order, lot_size=OPT.lot_size)
        assert c is not None and c["source"] == "fill" and c["visibleQty"] == 1_000 and c["visibleLots"] == 1
        lots = pos.qty // OPT.lot_size
        assert c["lots"] == lots and c["shortfallLots"] == lots - 1
        assert c["optimismRs"] == pytest.approx((pos.qty - 1_000) * 0.10), "the lots past the first at 17.20, not the 17.10 limit"
        assert pos.entry == 17.10 and order["avg_price"] == 17.10, "record-only: the fill stands as the paper booked it"
    finally:
        await e.stop()


# -- record-only: nothing in the order path reaches the check ------------------------------------------------

PKG = Path(__file__).resolve().parents[1] / "kotsin_nse"


def _imports_depth_check(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith("depth_check") or any(a.name == "depth_check" for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(a.name.endswith("depth_check") for a in node.names):
            return True
    return False


def test_only_the_api_imports_the_depth_check():
    importers = {str(p.relative_to(PKG)) for p in PKG.rglob("*.py") if p.name != "depth_check.py" and _imports_depth_check(p)}
    assert importers == {"api/routes.py"}, importers
    tree = ast.parse((PKG / "exec" / "depth_check.py").read_text())
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in mods if m.startswith(("kotsin_nse", "."))], "the check reaches nothing of the engine: the standard library only"
    assert "depth_check" not in (PKG / "engine.py").read_text(), "the engine never names it"


def test_the_order_path_runs_without_the_depth_check_loaded():
    code = ("import sys, kotsin_nse.engine, kotsin_nse.exec.gateway, kotsin_nse.exec.paper, kotsin_nse.exec.resting, "
            "kotsin_nse.exec.live, kotsin_nse.exec.reconcile; print('kotsin_nse.exec.depth_check' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=PKG.parent, capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip() == "False", out.stderr[-500:]
