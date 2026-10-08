"""The depth check (operator, 2026-10-09: "go ahead build it n tell me where to view it"): each paper fill against
the five-level book recorded beside it — how many lots were on offer at the price, what the same fill would have
cost against that book, and how much kinder the paper fill was. RECORD-ONLY: read from the stored orders when a
page asks, never stored, and nothing in the order path imports or calls it."""

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

import kotsin_nse
from kotsin_nse.api.routes import build_app
from kotsin_nse.exec import depth_check as dc
from kotsin_nse.exec.depth_check import (
    depth_check,
    depth_check_for_order,
    depth_check_for_trade,
    lot_size_of,
    orders_for_trades,
)
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.strategy.keys import StrategyKey
from tests._books import held
from tests.test_limit_orders import _book, _engine, _sig

# -- one fill against one book, pure ---------------------------------------------------------------------------


def test_a_book_that_holds_every_lot_at_the_price_is_full_and_costs_nothing():
    c = depth_check("BUY", 1700, 26.0, 26.0, {"asks": [[25.7, 425], [25.75, 850], [25.8, 425], [26.3, 850]], "bids": [], "ageMs": 621},
                    425, "fill")
    assert c["verdict"] == "full" and c["source"] == "fill" and c["ageMs"] == 621
    assert c["visibleQty"] == 1700 and c["visibleLots"] == 4 and c["covered"] == 1.0 and c["shortfallLots"] == 0
    assert c["realisticPrice"] == 26.0 and c["optimismRs"] == 0.0 and not c["beyondBook"]
    assert "held the whole order" in c["note"]


def test_a_sell_with_half_its_lots_on_the_bid_walks_the_rest_down_the_bids():
    c = depth_check("SELL", 1700, 26.4, 26.4, {"bids": [[26.2, 850], [26.4, 850], [26.3, 425]], "asks": [[26.75, 1700]]}, 425, "fill")
    assert c["verdict"] == "partial" and c["visibleLots"] == 2 and c["shortfallLots"] == 2 and c["covered"] == 0.5
    # 850 at 26.40, then 425 at 26.30 and 425 at 26.20 (the levels are sorted, best first, whatever order they came in)
    assert c["realisticPrice"] == pytest.approx(26.325) and c["optimismRs"] == pytest.approx(127.5)


def test_nothing_on_offer_at_the_price_is_thin_and_the_whole_order_walks():
    c = depth_check("BUY", 32000, 2.16, 2.16, {"asks": [[2.18, 8000], [2.20, 24000]], "ageMs": 517}, 8000, "fill")
    assert c["verdict"] == "thin" and c["visibleQty"] == 0 and c["visibleLots"] == 0 and c["shortfallLots"] == 4
    assert c["realisticPrice"] == pytest.approx(2.195) and c["optimismRs"] == pytest.approx(1120.0)
    assert c["note"].startswith("0 lots on offer at 2.16 or better for 4")


def test_no_book_on_the_side_that_trades_is_no_book_and_counts_nothing():
    for depth, note in ((None, "no book recorded"), ({}, "no book recorded"), ({"bids": [[26.0, 425]], "asks": []}, "the book had no asks")):
        c = depth_check("BUY", 425, 26.0, 26.0, depth, 425, "fill")
        assert c["verdict"] == "no book" and c["optimismRs"] == 0.0 and c["realisticPrice"] is None and c["note"] == note
    assert depth_check("BUY", 425, 26.0, 26.0, None, 425, "fill")["source"] is None, "no book: no source"


def test_a_malformed_book_never_raises_and_keeps_only_the_good_levels():
    bad = {"asks": [["x", 5], [26.0], None, [26.0, -5], [float("nan"), 10], "26.0", [True, 425], [26.0, 425]], "ageMs": "old"}
    c = depth_check("BUY", 425, 26.0, 26.0, bad, 425, "fill")
    assert c["verdict"] == "full" and c["visibleQty"] == 425 and c["ageMs"] is None
    for args in (("BUY", None, 26.0), ("BUY", 425, None), ("BUY", 425, -1.0), ("", 425, 26.0), ("HOLD", 425, 26.0)):
        assert depth_check(*args, 26.0, bad, 425, "fill")["verdict"] == "no book"
    for depth in ("garbage", [1, 2], 7, {"asks": "x"}, {"asks": {"26": 425}}):
        assert depth_check("BUY", 425, 26.0, 26.0, depth, 425, "fill")["verdict"] == "no book"
    for lot in (None, 0, -3, float("inf"), "425", True):
        assert depth_check("BUY", 425, 26.0, 26.0, {"asks": [[26.0, 425]]}, lot, "fill")["verdict"] == "full"  # type: ignore[arg-type]
    assert depth_check("BUY", 425, 26.0, 26.0, {"asks": [[26.1, 425]]}, 425, "fill", multiplier="x")["optimismRs"] == pytest.approx(42.5)  # type: ignore[arg-type]


def test_a_buy_and_a_sell_on_mirrored_books_are_judged_alike():
    buy = depth_check("BUY", 3000, 10.0, 10.0, {"asks": [[10.0, 1000], [10.5, 1000]]}, 1000, "fill")
    sell = depth_check("SELL", 3000, 10.0, 10.0, {"bids": [[10.0, 1000], [9.5, 1000]]}, 1000, "fill")
    for k in ("verdict", "visibleLots", "shortfallLots", "covered", "beyondBook", "beyondQty", "optimismRs"):
        assert buy[k] == sell[k], k
    assert buy["realisticPrice"] - 10.0 == pytest.approx(10.0 - sell["realisticPrice"])
    assert buy["optimismRs"] > 0, "positive: the paper fill was kinder than the book"


def test_a_part_lot_on_the_book_is_not_a_lot():
    c = depth_check("BUY", 850, 26.0, 26.0, {"asks": [[26.0, 637], [26.1, 1000]]}, 425, "fill")
    assert c["verdict"] == "partial" and c["visibleQty"] == 637 and c["visibleLots"] == 1 and c["shortfallLots"] == 1
    c = depth_check("BUY", 850, 26.0, 26.0, {"asks": [[26.0, 300], [26.1, 1000]]}, 425, "fill")
    assert c["verdict"] == "thin" and c["visibleLots"] == 0 and c["shortfallLots"] == 2, "less than one whole lot at the price"
    assert c["lots"] == 2 and c["lotSize"] == 425


def test_what_the_five_levels_cannot_hold_is_priced_at_the_last_one_and_flagged():
    c = depth_check("BUY", 1000, 10.0, 10.0, {"asks": [[10.0, 200], [10.1, 300]]}, 1, "fill")
    assert c["beyondBook"] and c["beyondQty"] == 500 and c["realisticPrice"] == pytest.approx(10.08)  # 200 @ 10, 800 @ 10.1
    assert "500 units beyond the five levels, priced at 10.1" in c["note"]
    # every level at or better than the fill: the rest is priced at the fill, never kinder than it
    c = depth_check("BUY", 300, 10.0, 10.0, {"asks": [[9.9, 100]]}, 1, "fill")
    assert c["beyondBook"] and c["realisticPrice"] == 10.0 and c["optimismRs"] == 0.0 and c["verdict"] == "partial"
    c = depth_check("SELL", 300, 10.0, 10.0, {"bids": [[10.2, 100]]}, 1, "fill")
    assert c["realisticPrice"] == 10.0 and c["optimismRs"] == 0.0


def test_a_walked_fill_is_priced_level_by_level_as_the_matcher_walked_it():
    book = {"bids": [[10.0, 400], [9.9, 400], [9.8, 400]]}
    c = depth_check("SELL", 1200, 9.9, None, book, 400, "fill")
    assert c["walked"] and c["verdict"] == "full" and c["realisticPrice"] == 9.9 and c["optimismRs"] == 0.0
    assert c["visibleLots"] == 3, "a walk takes every level, each at its own price"
    c = depth_check("SELL", 2000, 9.9, None, book, 400, "fill")  # two lots more than the recorded levels hold
    assert c["verdict"] == "partial" and c["beyondBook"] and c["shortfallLots"] == 2
    assert c["realisticPrice"] == pytest.approx(9.86) and c["optimismRs"] == pytest.approx(80.0)


def test_the_rupees_scale_with_the_contract_multiplier():
    c = depth_check("BUY", 10, 100.0, 100.0, {"asks": [[101.0, 10]]}, 1, "fill", multiplier=1000)
    assert c["optimismRs"] == pytest.approx(10_000.0)


# -- a stored order, a trade -------------------------------------------------------------------------------------


def _order(oid: str, purpose: str, side: str, qty: int, price: float | None, ts: float, *, exec_: dict | None = None,
           strategy: str = "FUDKII_RT_Y", position_id: str | None = None, signal_id: str = "s1", status: str = "FILLED") -> dict:
    tag = "EN" if purpose == "ENTRY" else oid
    return {"id": oid, "client_order_id": f"FII-RTY-261009-110000-001-{tag}-TATASTEEL-190CE-L4", "strategy": strategy,
            "symbol": "TATASTEEL 29 SEP 2026 CE 190.00", "scrip_code": "153805", "side": side, "purpose": purpose, "qty": qty,
            "mode": "PAPER", "status": status, "signal_id": signal_id, "position_id": position_id, "reason": f"{oid} reason", "ts": ts,
            "avg_price": price if status == "FILLED" else None, "filled": qty if status == "FILLED" else 0, "charges": 0.0,
            "slippage_bps": 0.0, "broker_order_id": None, "note": "", **({"exec": exec_} if exec_ else {})}


def _trade_orders(ts: float, *, strategy: str = "FUDKII_RT_Y", pos: str = "pos-1") -> list[dict]:
    """RT-Y's 4-lot entry with 2 lots at its price, a T1 lot touched by a print over a lower bid, a 3-lot cross."""
    entry = _order("o-en", "ENTRY", "BUY", 4000, 17.10, ts - 30, strategy=strategy, exec_={
        "kind": "entry", "limit": 17.10, "fillPrice": 17.10, "filledTs": ts - 25,
        "depthAtPlace": {"bids": [[16.95, 9000]], "asks": [[17.25, 9000]], "ageMs": 300},
        "depthAtFill": {"bids": [[16.95, 9000]], "asks": [[17.05, 1000], [17.10, 1000], [17.15, 2000]], "ageMs": 400}})
    t1 = _order("o-t1", "EXIT", "SELL", 1000, 18.0, ts - 10, strategy=strategy, position_id=pos, exec_={
        "kind": "target", "limit": 18.0, "fillPrice": 18.0, "depthAtFill": {"bids": [[17.95, 5000]], "asks": [[18.05, 100]], "ageMs": 900}})
    cross = _order("o-x", "EXIT", "SELL", 3000, 17.5, ts, strategy=strategy, position_id=pos, exec_={
        "kind": "exit", "limit": 17.8, "crossedTs": ts, "fillPrice": 17.5, "depthAtPlace": {"bids": [[17.7, 100]], "asks": [], "ageMs": 50},
        "depthAtCross": {"bids": [[17.6, 1000], [17.5, 1000], [17.4, 1000]], "asks": [[17.9, 500]], "ageMs": 200}})
    cancelled = _order("o-t2", "EXIT", "SELL", 1000, None, ts - 5, strategy=strategy, position_id=pos, status="CANCELLED",
                       exec_={"kind": "target", "limit": 18.5, "depthAtPlace": {"bids": [[17.0, 1]], "asks": [], "ageMs": 1}})
    return [entry, t1, cross, cancelled]


def test_an_order_is_judged_on_the_book_at_its_fill_then_the_book_it_crossed_then_the_book_at_placement():
    entry, t1, cross, cancelled = _trade_orders(1_000_000.0)
    e = depth_check_for_order(entry)
    assert e is not None and e["source"] == "fill" and e["bookField"] == "depthAtFill" and not e["walked"]
    assert e["lotSize"] == 1000, "an entry's lot is read off its own id: 4000 units, L4"
    assert e["verdict"] == "partial" and e["visibleLots"] == 2 and e["optimismRs"] == pytest.approx(100.0)
    x = depth_check_for_order(cross, lot_size=1000)
    assert x is not None and x["source"] == "fill" and x["bookField"] == "depthAtCross" and x["walked"] and x["optimismRs"] == 0.0
    placed = {**t1, "exec": {k: v for k, v in t1["exec"].items() if k != "depthAtFill"} | {"depthAtPlace": {"bids": [[18.0, 1000]]}}}
    p = depth_check_for_order(placed, lot_size=1000)
    assert p is not None and p["source"] == "place" and p["verdict"] == "full"
    assert depth_check_for_order(cancelled) is None, "an order that did not fill is not a fill to check"
    assert depth_check_for_order({**t1, "exec": None})["verdict"] == "no book"  # type: ignore[index]
    assert depth_check_for_order("nonsense") is None and depth_check_for_order({}) is None


def test_a_trade_sums_its_entry_and_every_exit_and_carries_the_worst_verdict():
    ts = 1_000_000.0
    trade = {"id": "trd-1", "position_id": "pos-1", "strategy": "FUDKII_RT_Y", "signal_id": "s1", "qty": 4000, "multiplier": 1,
             "opened_ts": ts - 25, "closed_ts": ts}
    d = depth_check_for_trade(trade, _trade_orders(ts))
    assert d["lotSize"] == 1000 and d["fills"] == 3 and d["checked"] == 3 and d["verdict"] == "thin"
    assert d["entry"]["optimismRs"] == pytest.approx(100.0) and d["entry"]["verdict"] == "partial"
    assert d["exit"]["optimismRs"] == pytest.approx(50.0), "T1: one lot sold at 18.00 over a 17.95 bid; the cross walked as recorded"
    assert d["exit"]["byVerdict"] == {"full": 1, "partial": 0, "thin": 1, "no book": 0} and d["exit"]["shortfallLots"] == 1
    assert d["exit"]["paperPrice"] == pytest.approx((18.0 * 1000 + 17.5 * 3000) / 4000)
    assert d["optimismRs"] == pytest.approx(150.0)
    assert depth_check_for_trade({"id": "t", "qty": 4000}, [])["verdict"] == "no book"
    assert lot_size_of(4000, [{"client_order_id": "x-T1V1-TATASTEEL-190CE-L4"}]) == 1000, "the tag is the position's lots"
    assert lot_size_of(4000, [{"client_order_id": "pos-1|EXIT|0|SL-EQ"}]) is None


def test_a_book_that_opens_on_another_books_fill_takes_that_books_entry():
    ts = 1_000_000.0
    real = _trade_orders(ts)
    mirror_exit = _order("o-sa", "EXIT", "SELL", 4000, 17.3, ts + 60, strategy="FUDKII_RT_Y_SA", position_id="pos-2",
                         exec_={"kind": "exit", "limit": 17.3, "depthAtFill": {"bids": [[17.3, 9000]]}})
    trades = [{"id": "t-real", "position_id": "pos-1", "strategy": "FUDKII_RT_Y", "signal_id": "s1", "opened_ts": ts - 25},
              {"id": "t-sa", "position_id": "pos-2", "strategy": "FUDKII_RT_Y_SA", "signal_id": "s1", "opened_ts": ts - 25},
              {"id": "t-other", "position_id": "pos-3", "strategy": "FUDKII_RT_Y", "signal_id": "s2", "opened_ts": ts}]
    mine = orders_for_trades(trades, [*real, mirror_exit], lambda b: [b, "FUDKII_RT_Y"] if b.endswith("_SA") else [b])
    assert [o["id"] for o in mine["t-real"]] == ["o-en", "o-t1", "o-x", "o-t2"]
    assert [o["id"] for o in mine["t-sa"]] == ["o-en", "o-sa"], "the mirror's entry is its source's fill"
    assert mine["t-other"] == [], "another signal's trade takes nothing"


# -- served -------------------------------------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _client(e):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t")


@pytest.mark.asyncio
async def test_the_trades_rows_carry_a_depth_check_and_the_summary_counts_each_book(settings, clock):
    e = await _engine(settings, clock)
    try:
        async with _client(e) as c:
            empty = (await c.get("/api/depth-check", params={"since": "start"})).json()
            assert empty["since"] is None and empty["books"] == [] and empty["total"] is None, "no depth book stored yet"
        ts = clock[0] - 600
        for o in _trade_orders(ts):
            await e.ledger.insert_order(o, "PAPER_FILLED" if o["status"] == "FILLED" else "TARGET_CANCELLED")
        await e.ledger.insert_order(_order("o-sa", "EXIT", "SELL", 4000, 17.3, ts + 60, strategy="FUDKII_RT_Y_SA", position_id="pos-2",
                                           exec_={"kind": "exit", "limit": 17.3, "depthAtFill": {"bids": [[17.3, 9000]]}}), "PAPER_FILLED")
        base = {"symbol": "TATASTEEL 29 SEP 2026 CE 190.00", "underlying": "TATASTEEL", "signal_id": "s1", "qty": 4000, "multiplier": 1,
                "opened_ts": ts - 25, "gross": 0.0, "charges": 0.0, "net": -500.0, "r_multiple": 0.0, "exit_reason": "SL-EQ"}
        await e.ledger.insert_trade({**base, "id": "trd-y", "position_id": "pos-1", "strategy": "FUDKII_RT_Y", "closed_ts": ts})
        await e.ledger.insert_trade({**base, "id": "trd-sa", "position_id": "pos-2", "strategy": "FUDKII_RT_Y_SA", "closed_ts": ts + 60})
        async with _client(e) as c:
            rows = {r["id"]: r for r in (await c.get("/api/trades", params={"books": "all"})).json()}
            today = (await c.get("/api/depth-check", params={"since": "today"})).json()
            every = (await c.get("/api/depth-check", params={"since": "start", "books": "all"})).json()
            assert (await c.get("/api/depth-check", params={"since": "yesterday"})).status_code == 422
            assert (await c.get("/api/depth-check", params={"books": "nonsense"})).status_code == 422
        y = rows["trd-y"]["depthCheck"]
        assert y["optimismRs"] == pytest.approx(150.0) and y["verdict"] == "thin" and y["fills"] == 3
        assert [o["bookField"] for o in y["entry"]["orders"] + y["exit"]["orders"]] == ["depthAtFill", "depthAtFill", "depthAtCross"]
        sa = rows["trd-sa"]["depthCheck"]
        assert sa["entry"]["optimismRs"] == pytest.approx(100.0) and sa["exit"]["verdict"] == "full", "the mirror: RT-Y's entry, its own exit"
        assert [b["book"] for b in today["books"]] == ["FUDKII_RT_Y"], "the trading books by default"
        b = today["books"][0]
        assert (b["trades"], b["fills"], b["checked"], b["optimismRs"]) == (1, 3, 3, 150.0)
        assert b["byVerdict"] == {"full": 1, "partial": 1, "thin": 1, "no book": 0} and b["worst"][0]["id"] == "trd-y"
        assert b["label"] == StrategyKey.FUDKII_RT_Y.display_name and b["shadow"] is False
        assert every["since"] == pytest.approx(ts - 30), "since the first order that carried a depth book"
        assert [b["book"] for b in every["books"]] == ["FUDKII_RT_Y", "FUDKII_RT_Y_SA"] and every["books"][1]["shadow"] is True
        assert every["total"]["fills"] == 5 and every["total"]["optimismRs"] == pytest.approx(250.0)
    finally:
        await e.stop()


# -- record-only ---------------------------------------------------------------------------------------------------

#: the read side: the only code that may reach the depth check
_READERS = {"api/routes.py"}
_NAMES = {"depth_check", "depth_check_for_order", "depth_check_for_trade", "orders_for_trades", "book_summary", "lot_size_of"}


def test_nothing_in_the_order_path_imports_or_calls_the_depth_check():
    root = Path(kotsin_nse.__file__).parent
    importers, offenders = set(), []
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root).as_posix()
        if rel == "exec/depth_check.py":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                if "depth_check" in (node.module or "") or any(a.name in _NAMES for a in node.names):
                    importers.add(rel)
            elif isinstance(node, ast.Import) and any("depth_check" in a.name for a in node.names):
                importers.add(rel)
            elif rel not in _READERS and isinstance(node, ast.Name | ast.Attribute):
                if (node.id if isinstance(node, ast.Name) else node.attr) in _NAMES:
                    offenders.append(f"{rel}:{node.lineno}")
    assert importers == _READERS, f"only the API reads the depth check, not {importers - _READERS}"
    assert not offenders, offenders
    # and at run time: the engine and every order module load without it
    code = ("import sys, kotsin_nse.engine, kotsin_nse.exec.gateway, kotsin_nse.exec.resting, kotsin_nse.exec.paper, "
            "kotsin_nse.exec.live, kotsin_nse.exec.reconcile, kotsin_nse.risk.exits, kotsin_nse.ledger.db; "
            "sys.exit(int('kotsin_nse.exec.depth_check' in sys.modules))")
    assert subprocess.run([sys.executable, "-c", code], cwd=root.parent, check=False).returncode == 0


@pytest.mark.asyncio
async def test_an_order_fills_and_a_trade_closes_with_the_depth_check_broken(settings, clock, monkeypatch):
    """Every function of the module raising: the engine still rests, fills, exits and books — it never calls them.
    Then, the module back, the closed trade is served with its check from the orders the engine stored."""
    def boom(*_a, **_k):
        raise AssertionError("the order path called the depth check")

    with monkeypatch.context() as m:
        for name in _NAMES:
            m.setattr(dc, name, boom)
        e = await _engine(settings, clock)
        try:
            _book(e, 16.95, 17.25, clock[0])
            sig = _sig(clock)
            await e._handle_signal(sig, None, books=(StrategyKey.FUDKII,))
            clock[0] += 22
            _book(e, 16.95, 17.10, clock[0])  # the ask comes down to our bid
            await e._manage_positions()
            (pos,) = [p for p in held(e) if p.strategy == "FUDKII"]
            await e.operator_skip("FUDKII", sig.signal_id)
            for _ in range(3):
                clock[0] += 16
                _book(e, 16.95, 17.10, clock[0])
                await e._manage_positions()
            assert pos.status == "CLOSED"
        except BaseException:
            await e.stop()
            raise
    try:
        async with _client(e) as c:
            (row,) = [r for r in (await c.get("/api/trades")).json() if r["strategy"] == "FUDKII"]
        d = row["depthCheck"]
        assert d["entry"]["fills"] == 1 and d["entry"]["orders"][0]["source"] == "fill" and d["entry"]["verdict"] == "full"
        assert d["exit"]["fills"] >= 1 and d["lotSize"] == 1000 and d["optimismRs"] == pytest.approx(0.0), "a deep book: paper was right"
    finally:
        await e.stop()
