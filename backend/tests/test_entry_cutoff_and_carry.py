"""The NSE last-entry minutes, the 15:24 force-flat, and the carry of a trigger decided at the close to
the next session's open (operator, 2026-09-29: "3:15PM IST for NSE trading for all NSE trades for
all strategies except for MCX ones, FUDKII-RT-Y-F. last entry time for FUDKII-RT-Y-F will be before
3:23PM and exit latest by 3:25PM IST" — and, for the triggers FUDKII creates at 15:30 on the last
bar, "Carry to next 09:15 open"). The graded-F shadow bought at 15:30, after the close, that day."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as dtime

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import IN_TREND_BOOKS, Engine
from kotsin_nse.market.session import IST, from_ist, past_force_flat, session_open_ts
from tests._books import held

from .conftest import REAL_PAST_LAST_ENTRY
from .test_rt_y_cap_and_graded_f import _unpublished
from .test_wide_stop_shadow import UND, _rt_y_trigger

DAY = date(2026, 9, 29)  # a Tuesday



#: how far ahead the ledger is read: a carried trigger is stamped at the NEXT session's open, which a
#: Friday or a holiday eve puts more than two days away (the 2 Oct 2026 holiday broke a 2-day window)
AHEAD = 10 * 86_400

def _at(hms: str, d: date = DAY) -> float:
    return from_ist(datetime.combine(d, dtime.fromisoformat(hms), tzinfo=IST))


def _next_session(e: Engine, d: date) -> date:
    nd = d + timedelta(days=1)
    while not e.calendar.is_trading_day(nd):
        nd += timedelta(days=1)
    return nd


# -- the last entry minutes and the force-flat ----------------------------------------------------


def test_every_book_enters_until_1515_the_graded_f_shadow_until_1522_and_mcx_is_untouched(settings, entry_cutoff):
    e = Engine(settings)
    for book in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y", "FUKAA"):
        assert e._past_last_entry(book, UND, now=_at("15:15:59")) is None, book
        assert e._past_last_entry(book, UND, now=_at("15:16:00")).startswith("past "), book
    assert e._past_last_entry("FUDKII_RT_Y_F", UND, now=_at("15:22:59")) is None, "before 3:23PM"
    why = e._past_last_entry("FUDKII_RT_Y_F", UND, now=_at("15:23:00"))
    assert why is not None and "15:22" in why and "flattened from 15:24" in why
    assert "flattened from 15:20" in e._past_last_entry("FUDKII_RT_Y", UND, now=_at("15:16:00"))
    crude = Instrument("477176", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, underlying="CRUDEOIL")
    assert e._past_last_entry("FUDKII_RT_MCX", crude, now=_at("23:10:00")) is None, "MCX keeps its own session"


def test_every_book_flattens_at_1520_and_the_graded_f_shadow_at_1524_out_by_1525(settings):
    e = Engine(settings)
    for seg in (Segment.NSE_EQ, Segment.NSE_FO, Segment.NSE_IDX):
        assert not past_force_flat(seg, _at("15:19:59")) and past_force_flat(seg, _at("15:20:00")), "the session's"
        for book in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_Y", "FUDKII_CT_Y"):
            assert not e._past_force_flat(book, seg, _at("15:19:59")) and e._past_force_flat(book, seg, _at("15:20:00"))
        assert not e._past_force_flat("FUDKII_RT_Y_F", seg, _at("15:23:59")) and e._past_force_flat("FUDKII_RT_Y_F", seg, _at("15:24:00"))
    assert not e._past_force_flat("FUDKII_RT_MCX", Segment.MCX_FO, _at("23:19:59")) and e._past_force_flat("FUDKII_RT_MCX", Segment.MCX_FO, _at("23:20:00"))


@pytest.mark.asyncio
async def test_past_1515_the_books_refuse_and_the_graded_f_shadow_still_enters_until_1522(settings, monkeypatch):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        clock = [_at("15:18:00")]
        monkeypatch.setattr(Engine, "_past_last_entry", lambda self, b, u, *, now=None: REAL_PAST_LAST_ENTRY(self, b, u, now=clock[0]))
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        assert not e.positions, "no in-trend book buys at 15:18"
        rows = [r for r in await e.ledger.rows_between("signals", 0, time.time() + 60) if r["signal_id"] == sig.signal_id]
        assert rows and rows[-1]["decision"] == "PAST_ENTRY_CUTOFF"
        ev = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "rt_twin.skipped"]
        assert {r["book"] for r in ev if r["gate"] == "entry_cutoff"} >= {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}

        f = replace(_unpublished(replace(sig, stop=1440.0)), ts=sig.ts + 1800)  # another trigger, graded F
        await e._handle_unpublished(f, None)
        assert [p.strategy for p in held(e)] == ["FUDKII_RT_Y_F"], "the graded-F shadow buys until 15:22"

        clock[0] = _at("15:23:00")
        g = replace(f, ts=f.ts + 1800, symbol="RELIANCE")
        await e._handle_unpublished(g, None)
        assert len(held(e)) == 1, "nothing from 15:23"
        ev = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "rt_twin.skipped"]
        assert any(r["book"] == "FUDKII_RT_Y_F" and r["gate"] == "entry_cutoff" for r in ev)
    finally:
        await e.stop()


# -- the carry to the next open -------------------------------------------------------------------


def _close_trigger(e: Engine, sig):
    """``sig`` on today's last bar — 15:15, complete at 15:30 — with the breadth it was logged with
    (the bar-close path logs every trigger's before any route runs)."""
    trig = replace(sig, ts=int(from_ist(datetime.combine(date.today(), dtime(15, 15), tzinfo=IST))))
    e._breadth_at[trig.signal_id] = e._breadth_at[sig.signal_id]
    return trig


@pytest.mark.asyncio
async def test_only_the_last_bar_of_the_nse_session_is_carried(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        assert e._after_close(_close_trigger(e, sig))
        last = _close_trigger(e, sig)
        assert not e._after_close(replace(last, ts=last.ts - 1800)), "the 14:45 bar is decided at 15:15"
        e.underlyings["CRUDEOIL"] = Instrument("477176", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, underlying="CRUDEOIL")
        assert not e._after_close(replace(_close_trigger(e, sig), symbol="CRUDEOIL")), "MCX is not carried"
    finally:
        await e.stop()


async def _queue_and_open(e: Engine, trig, first_print: float | None, *, after_s: float = 5.0) -> float:
    """Queue ``trig`` as the close did, then run the carry at the next session's open."""
    await e._carry_queue(trig)
    nd = _next_session(e, date.today())
    open_ts = session_open_ts(Segment.NSE_EQ, nd)
    if first_print is not None:
        e.ltps[UND.scrip_code] = first_print
        e._ltp_traded_ts[UND.scrip_code] = open_ts + 2
    await e._carry_tick(open_ts + after_s)
    while e._decision_tasks:
        await asyncio.gather(*list(e._decision_tasks), return_exceptions=True)
    return open_ts


@pytest.mark.asyncio
async def test_a_published_trigger_decided_at_the_close_is_carried_not_traded(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        trig = _close_trigger(e, sig)
        await e._carry_queue(trig)
        assert not e.positions, "nothing is bought after the close"
        rows = [r for r in await e.ledger.rows_between("signals", 0, time.time() + 86_400) if r["signal_id"] == trig.signal_id]
        assert rows[-1]["decision"] == "CARRIED" and "09:15" in rows[-1]["decision_reason"]
        q = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "carry.queued"]
        assert q and q[0]["signal"]["signal_id"] == trig.signal_id and q[0]["breadth"]["share"] == 0.7, "kept for a restart overnight"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_carried_trigger_enters_the_in_trend_books_on_its_first_print_at_the_next_open(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        trig = _close_trigger(e, sig)  # close 1500, stop 1490, T1 1530
        open_ts = await _queue_and_open(e, trig, 1505.0)
        books = sorted(p.strategy for p in held(e))
        assert books == ["FUDKII", "FUDKII_RT_N", "FUDKII_RT_X", "FUDKII_RT_Y", "FUDKII_RT_Y_W1"], "routed as it would have been at the close"
        carried = e._signals_today[next(iter(e.positions.values())).signal_id]
        assert carried.ts == int(open_ts) - 1800 and carried.entry == 1505.0, "the 08:45 slot (its card reads 09:15), entered at the open"
        assert carried.context["carried"] == {"from": trig.signal_id, "firstPrint": 1505.0, "where": "in favour", "prevClose": 1500.0}
        done = [r for r in await e.ledger.rows_between("events", 0, time.time() + AHEAD) if r.get("kind") == "carry.done"]
        assert len(done) == 1 and done[0]["where"] == "in favour"
        e._carry_day = None  # a restart during the open
        await e._carry_tick(open_ts + 10)
        assert len(held(e)) == 5 and not e._carry_pending, "never entered twice"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_carried_trigger_that_opens_in_the_zone_enters(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        await _queue_and_open(e, _close_trigger(e, sig), 1495.0)  # between the stop 1490 and the close 1500
        assert e.positions and e._signals_today[next(iter(e.positions.values())).signal_id].context["carried"]["where"] == "in the zone"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_carried_trigger_that_opens_through_its_stop_is_dropped(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        await _queue_and_open(e, _close_trigger(e, sig), 1488.0)  # opened through the stop
        assert not e.positions
        dropped = [r for r in await e.ledger.rows_between("events", 0, time.time() + AHEAD) if r.get("kind") == "carry.dropped"]
        assert len(dropped) == 1 and "through the stop" in dropped[0]["why"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_carried_trigger_whose_stock_does_not_print_expires(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        open_ts = await _queue_and_open(e, _close_trigger(e, sig), None)
        assert len(e._carry_pending) == 1, "waits for the first print"
        await e._carry_tick(open_ts + 301)
        assert not e._carry_pending and not e.positions
        exp = [r for r in await e.ledger.rows_between("events", 0, time.time() + AHEAD) if r.get("kind") == "carry.expired"]
        assert len(exp) == 1
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_unpublished_carried_trigger_goes_to_the_graded_f_shadow_alone(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        trig = _unpublished(replace(_close_trigger(e, sig), stop=1440.0))
        await _queue_and_open(e, trig, 1505.0)
        assert [p.strategy for p in held(e)] == ["FUDKII_RT_Y_F"]
        carried_id = next(iter(e.positions.values())).signal_id
        rows = [r for r in await e.ledger.rows_between("signals", 0, time.time() + AHEAD) if r["signal_id"] == carried_id]
        assert rows and rows[-1]["decision"] == "NOT_PUBLISHED" and rows[-1]["decision_reason"].startswith("carried")
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_carried_trigger_reads_carried_on_the_tabs_of_the_books_it_goes_to(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        trig = _close_trigger(e, sig)
        await e._carry_queue(trig)
        f = _unpublished(replace(trig, symbol="RELIANCE", ts=trig.ts - 86_400))  # yesterday's, not today's card
        assert f.signal_id != trig.signal_id
        today = date.today()
        y = (await e.book_cards("FUDKII_RT_Y", today))["cards"]
        assert [c["state"] for c in y] == ["CARRIED"] and y[0]["cta"]["enabled"] is False
        p = (await e.book_cards("FUDKII", today))["cards"]
        assert [c["state"] for c in p] == ["CARRIED"], "the parent's own decision"
        assert (await e.book_cards("FUDKII_CT_X", today))["cards"][0]["state"] != "CARRIED", "the fade books are not offered it"
    finally:
        await e.stop()
