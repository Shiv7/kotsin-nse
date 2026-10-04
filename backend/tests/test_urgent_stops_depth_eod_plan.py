"""Exits read the book (operator, 2026-10-03): "place order smartly not blindly".

* an urgent stop — the stock through its stop, the option falling fast, or a tight book — sells into
  the bid at once; a calm option stop on a wide book still rests at the mid and walks; the trail,
  targets and 15:20 keep their walk;
* every order's trail carries five levels of depth, price and quantity ("check lots on sell");
* at 15:15 each open NSE position's distance to its stop line and next target is RECORDED with what the
  operator's rule would do ("if at 15:15 we are waiting for SL which is very near, we should exit asap,
  in case the SL is away, then we wait if the target is close by") — never acted on."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.domain import Direction, ExitDecision, ExitReason, Position, PosSide
from kotsin_nse.engine import EOD_PLAN_HM, EOD_PLAN_NEAR_PCT
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.exec.resting import LimitPolicy, urgent_stop
from kotsin_nse.instrument.select import Quote
from kotsin_nse.market.session import IST, ist_today
from tests.test_limit_orders import OPT, UND, _book, _engine, _rows, _sig


def _at(h: int, m: int, s: int = 0) -> float:
    return datetime.combine(ist_today(), dtime(h, m, s), tzinfo=IST).timestamp()


@pytest.fixture
def clock(monkeypatch):
    now = [_at(11, 0)]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _pos(clock, *, pid="p1", strategy="FUDKII_RT_X", qty=2750, option_sl=9.0, targets=(), entry=12.0) -> Position:
    return Position(id=pid, strategy=strategy, instrument=OPT, underlying=UND, side=PosSide.LONG, qty=qty, entry=entry,
                    opened_ts=clock[0] - 600, signal_id=f"s-{pid}", direction=Direction.BULLISH, equity_entry=187.0,
                    equity_sl=185.0, option_sl=option_sl, option_targets=targets, qty_remaining=qty)


def _deep(e, bids: list[tuple[float, int]], asks: list[tuple[float, int]], now: float) -> None:
    e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=bids, asks=asks, ts=now)
    e.quotes[OPT.scrip_code] = Quote(ltp=bids[0][0], bid=bids[0][0], ask=asks[0][0], ts=now)
    e.ltps[OPT.scrip_code] = bids[0][0]


# -- the rule, pure ---------------------------------------------------------------------------------


def test_which_stops_are_urgent():
    # 2026-10-04: EVERY stop sells at its trigger. The finer 3 Oct rules below are what the switch falls back to.
    for reason in (ExitReason.SL_EQ, ExitReason.SL_OP):
        assert urgent_stop(reason, 10.0, 11.0, None, LimitPolicy()) == "a stop sells at its trigger"
    for reason in (ExitReason.TRAIL, ExitReason.TARGET, ExitReason.EOD, ExitReason.HALT):
        assert urgent_stop(reason, 10.0, 10.05, -9.0, LimitPolicy()) is None, f"{reason} keeps its walk"
    pol = LimitPolicy(exit_stops_at_bid=False)
    assert urgent_stop(ExitReason.SL_EQ, 10.0, 11.0, None, pol) == "the stock has broken its stop"
    assert "falling fast (-2.5% in 30 s)" in urgent_stop(ExitReason.SL_OP, 10.0, 11.0, -2.5, pol)
    assert urgent_stop(ExitReason.SL_OP, 10.0, 10.10, None, pol) == "a tight book (10 / 10.1)", "two ticks wide"
    assert urgent_stop(ExitReason.SL_OP, 10.0, 11.0, -1.0, pol) is None, "calm and wide: rest at the mid and walk"
    for reason in (ExitReason.TRAIL, ExitReason.TARGET, ExitReason.EOD, ExitReason.HALT):
        assert urgent_stop(reason, 10.0, 10.05, -9.0, pol) is None, f"{reason} keeps its walk"
    assert urgent_stop(ExitReason.SL_EQ, 10.0, 11.0, None, LimitPolicy(exit_stops_at_bid=False, exit_urgent_stops=False)) is None, "switched off"


# -- the engine ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stock_stop_sells_through_the_bid_at_once_for_every_lot_and_logs_the_depth(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = _pos(clock)
        e.positions[pos.id] = pos
        e.wallets["FUDKII_RT_X"].commit(12.0 * 2750, clock[0])
        _deep(e, [(10.0, 1000), (9.95, 1000), (9.9, 5000)], [(11.0, 5000)], clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_EQ, 10.5, 2750, "underlying breached"), clock[0])
        assert e._exit_resting(pos.id) is None, "nothing rests at the mid"
        assert pos.status == "CLOSED"
        x = pos.exec_log["exits"][-1]
        assert x["outcome"] == "sold into the bid at once — a stop sells at its trigger" and x["waitS"] == 0
        assert "sold into the bid at once" in x["why"] and x["bookAtPlace"] == {"bid": 10.0, "ask": 11.0}
        assert x["depthAtPlace"]["bids"][:2] == [[10.0, 1000], [9.95, 1000]] and x["depthAtCross"]["asks"] == [[11.0, 5000]]
        assert 9.9 < pos.exit_price < 10.0, "2,750 lots walk past the 1,000 at the top bid"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_option_stop_falling_fast_sells_at_once_and_a_calm_one_still_walks(settings, clock):
    """The 3 Oct rule, behind the switch (2026-10-04: with it on, every stop sells at its trigger —
    test_mae_and_stop_execution.py)."""
    e = await _engine(settings.model_copy(update={"paper_limit_exit_stops_at_bid": False}), clock)
    try:
        fast, calm = _pos(clock, pid="fast"), _pos(clock, pid="calm")
        for p in (fast, calm):
            e.positions[p.id] = p
        e._note_mid(OPT.scrip_code, clock[0] - 30, 12.0)
        _book(e, 10.0, 11.0, clock[0])  # mid 10.5: −12.5 % in 30 s
        await e._exit(fast, ExitDecision(fast.id, ExitReason.SL_OP, 10.5, 2750, "option mid held below"), clock[0])
        x = fast.exec_log["exits"][-1]
        assert fast.status == "CLOSED" and "falling fast (-12.5% in 30 s)" in x["outcome"]
        assert x["momentum"][0]["runPct"] == -12.5
        e._mid_hist.clear()
        e._note_mid(OPT.scrip_code, clock[0] - 30, 10.55)  # −0.5 %: calm, and 20 ticks wide
        await e._exit(calm, ExitDecision(calm.id, ExitReason.SL_OP, 10.5, 2750, "option mid held below"), clock[0])
        r = e._exit_resting(calm.id)
        assert r is not None and r.limit == 10.5 and r.deadline_s == 15 and calm.status == "OPEN"
        assert r.depth_at_place["bids"] == [[10.0, 50_000]]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_trail_keeps_its_walk_however_fast_the_option_falls(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = _pos(clock)
        e.positions[pos.id] = pos
        e._note_mid(OPT.scrip_code, clock[0] - 30, 12.0)
        _book(e, 10.0, 10.5, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.TRAIL, 10.0, 2750, "give-back line"), clock[0])
        r = e._exit_resting(pos.id)
        assert r is not None and r.deadline_s == 45, "a trail walks from the mid for 45 s, as before"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_held_options_mid_is_kept_a_read_a_second_for_two_minutes(settings, clock):
    e = await _engine(settings, clock)
    try:
        t = clock[0]
        for k in range(200):
            e._note_mid(OPT.scrip_code, t + k * 0.5, 10.0 + k * 0.01)
        h = e._mid_hist[OPT.scrip_code]
        assert len(h) <= 121 and h[-1][0] - h[0][0] <= 120
        assert e._option_fall(OPT.scrip_code, t + 99.5, 10.0) is not None
        assert e._option_fall("nobody", t, 10.0) is None
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_entry_records_five_levels_of_depth_at_the_order_and_at_the_fill(settings, clock):
    e = await _engine(settings, clock)
    try:
        bids = [(17.00 - 0.05 * k, 650 * (k + 1)) for k in range(7)]
        asks = [(17.25 + 0.05 * k, 650 * (k + 1)) for k in range(7)]
        _deep(e, bids, asks, clock[0])
        await e._handle_signal(_sig(clock), None)
        clock[0] += 3
        _deep(e, [(16.95, 650)], [(17.10, 3000)], clock[0])  # the ask comes down to the limit
        await e._manage_positions()
        parent = next(p for p in e.positions.values() if p.strategy == "FUDKII")
        en = parent.exec_log["entry"]
        assert len(en["depthAtPlace"]["bids"]) == 5 and en["depthAtPlace"]["asks"][0] == [17.25, 650]
        assert en["depthAtFill"]["asks"] == [[17.10, 3000]]
        orders = [o for o in await _rows(e, "orders") if o.get("purpose") == "ENTRY" and o.get("strategy") == "FUDKII"]
        assert orders and "depthAtPlace" in orders[-1]["exec"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_at_1515_each_open_position_is_planned_and_recorded_never_acted_on(settings, clock):
    clock[0] = _at(15, 15, 5)
    e = await _engine(settings, clock)
    try:
        near_stop = _pos(clock, pid="near", option_sl=10.3)                          # mid 10.5: 1.9 % above its stop
        near_tgt = _pos(clock, pid="tgt", strategy="FUDKII", option_sl=8.0, targets=(10.75,))  # target 2.4 % away
        far = _pos(clock, pid="far", strategy="FUDKII_RT_Y_F", option_sl=8.0)         # neither: flattened later
        for p in (near_stop, near_tgt, far):
            e.positions[p.id] = p
        _book(e, 10.45, 10.55, clock[0])
        e._eod_plan_day = ""  # the engine's own loop may already have looked at 15:15 with nothing open
        await e._record_eod_plan(clock[0])
        await e._record_eod_plan(clock[0] + 30)  # once a session
        ev = {x["positionId"]: x for x in await _rows(e, "events") if x.get("kind") == "eod.plan"}
        assert set(ev) == {"near", "tgt", "far"} and all(x["recordOnly"] for x in ev.values())
        assert ev["near"]["verdict"].startswith("exit now — the stop line 10.3 is 1.9% away") and ev["near"]["stopPct"] == 1.9
        assert ev["tgt"]["verdict"] == "wait for the target 10.75, 2.4% away" and ev["tgt"]["target"] == 10.75
        assert ev["far"]["verdict"] == "flatten at 15:24 as now", "RT-Y-F flattens later than the session"
        assert ev["near"]["nearPct"] == EOD_PLAN_NEAR_PCT == 3.0 and EOD_PLAN_HM == "15:15"
        assert all(p.status == "OPEN" for p in (near_stop, near_tgt, far)) and not e._exit_resting("near"), "a record, not an order"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_before_1515_nothing_is_recorded(settings, clock):
    clock[0] = _at(15, 14, 59)
    e = await _engine(settings, clock)
    try:
        e.positions["p"] = _pos(clock, pid="p")
        _book(e, 10.45, 10.55, clock[0])
        await e._record_eod_plan(clock[0])
        assert not [x for x in await _rows(e, "events") if x.get("kind") == "eod.plan"] and e._eod_plan_day == ""
    finally:
        await e.stop()
