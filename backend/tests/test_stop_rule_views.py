"""The stop rules on the Overview and Trades pages (operator, 2026-10-04: "do i also see the bifurcation in overview?
per combination trade? active trades? etc").

Overview: each open trade with its two stop-rule mirrors beneath it — open with where their stop stands, or closed
with the exit — a mirror the real trade no longer holds listed apart with the real trade's exit, the mirrors' purses
off the Wallets table, and a book × rule grid. Trades: the ledger and its totals by book set — summing a mirror in
would count its book's trade three times — each row naming its stop rule."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import httpx
import pytest

from kotsin_nse.api.routes import build_app
from kotsin_nse.api.shadow import stop_rules_summary
from kotsin_nse.committee.service import REAL_TRADES
from kotsin_nse.engine import IN_TREND_BOOKS
from kotsin_nse.ledger.db import trades
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.strategy.keys import STOP_MIRRORS, StrategyKey, stop_rule_of
from tests.test_limit_orders import UND, _book, _engine, _sig


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _client(e):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t")


def test_a_book_names_its_source_and_rule():
    assert stop_rule_of("FUDKII_RT_Y_SA") == ("FUDKII_RT_Y", "A")
    assert stop_rule_of("FUDKII_RT_Y_W1_SE") == ("FUDKII_RT_Y_W1", "E")
    assert stop_rule_of("FUDKII_RT_Y") == ("FUDKII_RT_Y", "current")
    assert stop_rule_of("FUDKII_RT") == ("FUDKII_RT", "current"), "a retired key: itself"


@pytest.mark.asyncio
async def test_the_overview_shows_each_open_trade_with_its_mirrors_and_the_mirrors_its_real_trade_left_behind(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=(StrategyKey.FUDKII_RT_Y,))
        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])
        e.ltps[UND.scrip_code] = 185.2  # near its stop, so the breach below is marginal, not a fast fall (≥ 0.35 % in 60 s)
        await e._manage_positions()
        clock[0] += 1
        _book(e, 16.95, 17.10, clock[0])
        await e._manage_positions()  # the mirrors' first stock print, inside (a first print already through sells at once)
        for _ in range(5):  # the stock a hair through its 185.00 stop for 5 s: both mirrors confirming, neither sold
            clock[0] += 1
            _book(e, 16.95, 17.10, clock[0])
            e.ltps[UND.scrip_code] = 184.97
            await e._manage_positions()
        async with _client(e) as c:
            o = (await c.get("/api/overview")).json()
            g = (await c.get("/api/stop-rules", params={"since": "today"})).json()
        # RT-Y's current stop took the touch at once (the stock's stop confirms its option stop, no grace); its
        # mirrors are still confirming; the wide-stop shadow, 1 % further, is inside — with its own two mirrors
        assert [p["strategy"] for p in o["positions"]] == ["FUDKII_RT_Y_W1"], "a mirror is not a position of its own here"
        assert o["mirrors_open"] == 4
        assert not any(w["strategy"].endswith(("_SE", "_SA")) for w in o["wallets"]), "the mirrors' purses are in the grid"
        w1 = o["positions"][0]
        assert {r["rule"]: (r["status"], r["live"]["through"], r["live"]["equitySl"]) for r in w1["stopRules"]} == {
            "E": ("OPEN", None, w1["equity_sl"]), "A": ("OPEN", None, w1["equity_sl"])}
        alone = {m["rule"]: m for m in o["mirrors_alone"]}
        assert set(alone) == {"E", "A"} and all(m["source"] == "FUDKII_RT_Y" for m in alone.values())
        for m in alone.values():
            assert m["realExit"]["exitReason"] == "SL-EQ" and m["realExit"]["byOperator"] is False and m["realExit"]["net"] is not None
            live = m["stopRule"]["live"]
            assert live["equitySl"] == 185.0 and live["premiumCap"] == pytest.approx(round(m["entry"] * 0.75 / 0.05) * 0.05)
            assert live["through"]["seconds"] >= 4 and live["through"]["area"] > 0
        assert alone["A"]["stopRule"]["live"]["through"]["areaNeeded"] == 1.0 and alone["E"]["stopRule"]["live"]["through"]["areaNeeded"] is None
        cells = next(b for b in g["books"] if b["book"] == "FUDKII_RT_Y")["cells"]
        assert {r: (cells[r]["open"], cells[r]["waiting"], cells[r]["closed"]) for r in ("current", "E", "A")} == {
            "current": (0, 1, 0), "E": (1, 0, 0), "A": (1, 0, 0)}, "counted once closed under all three"
        assert cells["E"]["openGross"] is not None and g["total"]["E"]["open"] == 1, "the wide shadow is not in the trading total"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_mirror_still_running_after_the_operator_closed_the_real_trade_is_listed_with_that_exit(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        sig = _sig(clock)
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])
        e.ltps[UND.scrip_code] = 187.0
        await e._manage_positions()
        await e.operator_skip("FUDKII_RT_X", sig.signal_id)
        clock[0] += 16
        _book(e, 16.95, 17.10, clock[0])
        await e._manage_positions()
        async with _client(e) as c:
            o = (await c.get("/api/overview")).json()
        assert "FUDKII_RT_X" not in {p["strategy"] for p in o["positions"]}
        alone = {m["rule"]: m for m in o["mirrors_alone"]}
        assert set(alone) == {"E", "A"} and all(m["source"] == "FUDKII_RT_X" for m in alone.values())
        assert alone["E"]["realExit"]["byOperator"] is True and alone["E"]["realExit"]["net"] is not None
        assert alone["A"]["stopRule"]["status"] == "OPEN" and alone["A"]["stopRule"]["live"]["equitySl"] == 185.0
        held = [p for p in o["positions"] if p["strategy"] != "FUDKII_RT_Y_W1"]
        assert held and all({r["rule"] for r in p["stopRules"]} == {"E", "A"} for p in held), "every other book's trade carries its mirrors"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_trades_page_sums_one_book_set_so_a_trade_is_never_counted_three_times(settings, clock):
    e = await _engine(settings, clock)
    try:
        for i, (book, net) in enumerate((("FUDKII_RT_Y", -4080.0), ("FUDKII_RT_Y_SE", -6600.0), ("FUDKII_RT_Y_SA", 2100.0), ("FUDKII_RT_Y_W1", 900.0))):
            await e.ledger.insert_trade({"id": f"trd-{i}", "position_id": f"pos-{i}", "strategy": book, "symbol": "X", "underlying": "X",
                                         "closed_ts": clock[0] + i, "gross": net + 100, "net": net, "charges": 100.0, "r_multiple": 0.0, "exit_reason": "SL-EQ"})
        async with _client(e) as c:
            def get(path, books):
                return c.get(path, params={"books": books})
            trading = (await get("/api/trades", "trading")).json()
            mirrors = (await get("/api/trades", "mirrors")).json()
            assert [t["strategy"] for t in trading] == ["FUDKII_RT_Y"] and trading[0]["stop_rule"] == "current"
            assert {(t["strategy"], t["stop_rule"], t["source_book"]) for t in mirrors} == {
                ("FUDKII_RT_Y_SE", "E", "FUDKII_RT_Y"), ("FUDKII_RT_Y_SA", "A", "FUDKII_RT_Y")}
            assert [t["strategy"] for t in (await get("/api/trades", "shadows")).json()] == ["FUDKII_RT_Y_W1"]
            assert len((await c.get("/api/trades")).json()) == 4, "an older caller still gets every row"
            p = (await get("/api/pnl", "trading")).json()
            assert p["trades"] == 1 and p["net"] == -4080.0
            assert (await c.get("/api/pnl")).json()["trades"] == 4
            assert (await get("/api/pnl", "nonsense")).status_code == 422
            assert (await c.get("/api/stop-rules", params={"since": "yesterday"})).status_code == 422
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_grid_since_the_start_is_empty_until_a_mirror_has_traded(settings, clock):
    e = await _engine(settings, clock)
    try:
        async with _client(e) as c:
            g = (await c.get("/api/stop-rules", params={"since": "start"})).json()
        assert g["since"] is None and g["books"] == [] and g["total"] is None
    finally:
        await e.stop()


def test_the_grid_counts_open_and_waiting_trades_and_totals_the_trading_books_apart():
    def pos(pid, book, sid, status="CLOSED"):
        return {"id": pid, "strategy": book, "signal_id": sid, "status": status, "opened_ts": 1.0, "exit_reason": "SL-OP"}

    positions = [
        pos("a", "FUDKII_RT_Y", "s1"), pos("b", "FUDKII_RT_Y_SE", "s1"), pos("c", "FUDKII_RT_Y_SA", "s1"),
        pos("d", "FUDKII_RT_Y", "s2"), pos("e", "FUDKII_RT_Y_SE", "s2", status="OPEN"), pos("f", "FUDKII_RT_Y_SA", "s2"),
        pos("g", "FUDKII_RT_Y_W1", "s1"), pos("h", "FUDKII_RT_Y_W1_SE", "s1"), pos("i", "FUDKII_RT_Y_W1_SA", "s1"),
    ]
    trades = [{"position_id": k, "net": v, "exit_reason": "SL-OP"} for k, v in
              {"a": -100.0, "b": -200.0, "c": 50.0, "d": -10.0, "f": -10.0, "g": 400.0, "h": 400.0, "i": 400.0}.items()]
    sr = stop_rules_summary(positions=positions, trades=trades)
    y = sr["books"]["FUDKII_RT_Y"]
    assert (y["current"]["waiting"], y["E"]["open"], y["A"]["waiting"]) == (1, 1, 1), "s2: closed under two rules, open under E"
    assert sr["total"]["current"]["net"] == 300.0 and sr["total_trading"]["current"]["net"] == -100.0, "the wide-stop shadow is not a strategy's"
    assert {k.value for k in STOP_MIRRORS} >= {"FUDKII_RT_Y_W1_SE", "FUDKII_RT_Y_W1_SA"}


@pytest.mark.asyncio
async def test_the_ledger_is_newest_first_by_close_and_the_committee_never_counts_a_mirror(settings, clock):
    """A trade's id is random (uuid4): ordering by it served an arbitrary 200 on the Trades page, and an
    arbitrary 1000 to its totals once the ledger is that long. The committee reads real trades only."""
    e = await _engine(settings, clock)
    try:
        books = ("FUDKII", "FUDKII_SE", "FUDKII_RT_X", "FUDKII_SA", "FUDKII_RT_Y")
        for i, book in enumerate(books):  # ids in the reverse order of their closes
            await e.ledger.insert_trade({"id": f"trd-{9 - i}", "position_id": f"pos-{i}", "strategy": book, "symbol": "X", "underlying": "X",
                                         "closed_ts": clock[0] + i, "gross": 0.0, "net": 0.0, "charges": 0.0, "r_multiple": 0.0,
                                         "exit_reason": "EOD", "signal_id": "s1"})
        async with _client(e) as c:
            rows = (await c.get("/api/trades", params={"books": "all", "limit": 3})).json()
        assert [r["strategy"] for r in rows] == ["FUDKII_RT_Y", "FUDKII_SA", "FUDKII_RT_X"], "the latest three closes"
        real = await e.ledger.recent(trades, 50, order_col="closed_ts", where=REAL_TRADES)
        assert [r["strategy"] for r in real] == ["FUDKII_RT_Y", "FUDKII_RT_X", "FUDKII"]
        t = await e.ledger.trade_for_signal("s1", exclude=("FUDKII_SE", "FUDKII_SA"))
        assert t is not None and t["strategy"] == "FUDKII_RT_Y"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_mirrors_close_sends_nothing_to_the_review_committee(settings, clock):
    e = await _engine(settings, clock)
    try:
        sent: list[str] = []
        e.committee.on_trade_closed = lambda t: sent.append(t["strategy"])  # type: ignore[method-assign]
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=(StrategyKey.FUDKII_RT_Y,))
        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])
        e.ltps[UND.scrip_code] = 186.0
        await e._manage_positions()
        clock[0] += 1
        _book(e, 16.95, 17.10, clock[0])
        await e._manage_positions()
        clock[0] += 1
        _book(e, 16.95, 17.10, clock[0])
        e.ltps[UND.scrip_code] = 182.0  # decisively through every stop: RT-Y, its mirrors, the wide shadow's
        await e._manage_positions()
        closed = await e.ledger.recent(trades, 50)
        assert {r["strategy"] for r in closed} >= {"FUDKII_RT_Y", "FUDKII_RT_Y_SE", "FUDKII_RT_Y_SA"}
        assert not any(s.endswith(("_SE", "_SA")) for s in sent) and "FUDKII_RT_Y" in sent
    finally:
        await e.stop()


def test_the_max_drawdown_is_the_deepest_fall_from_the_best_point_so_far_the_entry_first():
    """Operator, 2026-10-04: "max drawdown since the time it has been trading ... in each active trade row"."""
    from kotsin_nse.engine import _position_from_json, _position_json
    from kotsin_nse.risk.exits import ExitEngine
    from kotsin_nse.risk.limits import RT_Y_LIMITS
    from tests.test_equity_stop import _pos

    eng, pos = ExitEngine(RT_Y_LIMITS), _pos(entry=12.0, option_sl=9.0)  # 1R = 3.00
    dd = []
    for bid in (11.4, 13.5, 12.75, 11.7, 14.0):
        eng._track(pos, bid + 0.1, bid)
        dd.append(round(pos.max_dd_r, 3))
    # 11.4: 0.2R under the entry, the first best · 13.5 (+0.5R): a peak · 12.75: 0.25R off it · 11.7: 0.6R off it, though
    # only 0.1R under the entry · 14.0: a new peak never shrinks the drawdown already seen
    assert dd == [0.2, 0.2, 0.25, 0.6, 0.6]
    assert pos.max_dd_inr() == pytest.approx(-0.6 * 3.0 * 2000)
    assert _position_from_json(_position_json(pos)).max_dd_r == pytest.approx(0.6), "it survives a restart"
    old = _position_json(pos)
    old.pop("max_dd_r")
    assert _position_from_json(old).max_dd_r == pytest.approx(-pos.mae_r), "a position saved before it: its MAE"


@pytest.mark.asyncio
async def test_each_active_trade_row_and_its_open_mirrors_carry_their_max_drawdown(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=(StrategyKey.FUDKII_RT_X,))
        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])
        e.ltps[UND.scrip_code] = 186.5
        await e._manage_positions()
        for bid in (16.60, 16.90):  # down 0.35 below the entry, back up
            clock[0] += 1
            _book(e, bid, bid + 0.15, clock[0])
            await e._manage_positions()
        async with _client(e) as c:
            o = (await c.get("/api/overview")).json()
        x = next(p for p in o["positions"] if p["strategy"] == "FUDKII_RT_X")
        assert x["max_dd_r"] > 0 and x["max_dd_inr"] < 0
        for r in x["stopRules"]:
            assert r["live"]["maxDdR"] > 0 and r["live"]["maxDdInr"] < 0
    finally:
        await e.stop()
