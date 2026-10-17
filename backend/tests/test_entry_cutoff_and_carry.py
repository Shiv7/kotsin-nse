"""The NSE last-entry minute, the 15:14 flatten, the 14:45 bar's triggers recorded and never traded, and the
carry of a trigger decided at the close to the next session's open.

Operator, 2026-10-17: the last NSE entry minute is 15:10 for every book (the live gateway's own cutoff);
"if we are in trade till 15:14IST, then we exit at the first avenue before 15:20IST" — every NSE book,
the graded-F shadow and every stop-rule mirror flatten from 15:14 (the shadow's 15:22 / 15:24 removed);
and the 14:45 bar's triggers, decided at 15:15 after the cutoff: "record them but dont trade as of now.
let it show on the list". Kept from 2026-09-29: for the triggers FUDKII creates at 15:30 on the last bar,
"Carry to next 09:15 open" (the graded-F shadow bought at 15:30, after the close, that day)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as dtime

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import (
    EOD_PLAN_HM,
    FORCE_FLAT_HM,
    IN_TREND_BOOKS,
    LAST_ENTRY_HM,
    NSE_FORCED_FILL_HM,
    NSE_LAST_ENTRY_HM,
    NSE_OUT_BY_HM,
    Engine,
)
from kotsin_nse.market.session import IST, from_ist, past_force_flat, session_open_ts, spec
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey
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


def test_every_book_enters_until_1510_and_mcx_is_untouched(settings, entry_cutoff):
    e = Engine(settings)
    books = ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y", "FUKAA", "FUDKII_RT_Y_F",
             "FUDKII_RT_Y_F_SE", "FUDKII_RT_Y_F_SA", "FUDKII_RT_Y_W1", "FUDKII_CT_M", "FUDKII_CT_Y_F")
    for book in books:
        assert e._past_last_entry(book, UND, now=_at("15:10:59")) is None, f"{book}: the 15:10 minute is the last, inclusive"
        why = e._past_last_entry(book, UND, now=_at("15:11:00"))
        assert why is not None and why.startswith("past ") and "15:10" in why and "flattened from 15:14" in why, book
    assert e._past_last_entry("FUDKII_RT_Y_F", UND, now=_at("15:15:00")) is not None, "the graded-F shadow's 15:22 is gone"
    crude = Instrument("477176", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, underlying="CRUDEOIL")
    assert e._past_last_entry("FUDKII_RT_MCX", crude, now=_at("23:10:00")) is None, "MCX keeps its own session"


def test_every_nse_book_flattens_from_1514_out_before_1520_and_mcx_at_2320(settings):
    e = Engine(settings)
    for seg in (Segment.NSE_EQ, Segment.NSE_FO, Segment.NSE_IDX):
        assert spec(seg).force_flat == dtime(15, 14) and spec(seg).close == dtime(15, 30), "the close (bars, signals) stays"
        assert not past_force_flat(seg, _at("15:13:59")) and past_force_flat(seg, _at("15:14:00")), "the session's"
        for book in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_Y", "FUDKII_CT_Y", "FUDKII_RT_Y_F", "FUDKII_RT_Y_F_SE",
                     "FUDKII_RT_Y_F_SA", "FUDKII_RT_Y_W1", "FUDKII_CT_M_SE"):
            assert not e._past_force_flat(book, seg, _at("15:13:59")) and e._past_force_flat(book, seg, _at("15:14:00")), book
            assert e._force_flat_hm(book, seg) == "15:14", book
    assert not e._past_force_flat("FUDKII_RT_MCX", Segment.MCX_FO, _at("23:19:59")) and e._past_force_flat("FUDKII_RT_MCX", Segment.MCX_FO, _at("23:20:00"))
    assert e._force_flat_hm("FUDKII_RT_MCX", Segment.MCX_FO) == "23:20"
    # one clock for every NSE book: no book of its own later, and the order the rule needs
    assert LAST_ENTRY_HM == {} and FORCE_FLAT_HM == {}
    assert NSE_LAST_ENTRY_HM == "15:10" <= settings.live_entry_cutoff_ist, "never later than the live gateway's own cutoff"
    assert NSE_LAST_ENTRY_HM < f"{spec(Segment.NSE_EQ).force_flat:%H:%M}" < NSE_FORCED_FILL_HM < NSE_OUT_BY_HM
    assert (NSE_FORCED_FILL_HM, NSE_OUT_BY_HM, EOD_PLAN_HM) == ("15:19", "15:20", "15:13")
    assert EOD_PLAN_HM < f"{spec(Segment.NSE_EQ).force_flat:%H:%M}", "the record-only plan comes before the flatten"


@pytest.mark.asyncio
async def test_past_1510_every_book_refuses_and_the_graded_f_shadow_too(settings, monkeypatch):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        clock = [_at("15:11:00")]
        monkeypatch.setattr(Engine, "_past_last_entry", lambda self, b, u, *, now=None: REAL_PAST_LAST_ENTRY(self, b, u, now=clock[0]))
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        assert not e.positions, "no in-trend book buys at 15:11"
        rows = [r for r in await e.ledger.rows_between("signals", 0, time.time() + 60) if r["signal_id"] == sig.signal_id]
        assert rows and rows[-1]["decision"] == "PAST_ENTRY_CUTOFF" and "15:10" in rows[-1]["decision_reason"]
        ev = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "rt_twin.skipped"]
        assert {r["book"] for r in ev if r["gate"] == "entry_cutoff"} >= {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}

        f = replace(_unpublished(replace(sig, stop=1440.0)), ts=sig.ts + 1800)  # another trigger, graded F
        await e._handle_unpublished(f, None)
        assert not held(e), "the graded-F shadow's own 15:22 is gone (operator, 2026-10-17)"
        ev = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "rt_twin.skipped"]
        assert any(r["book"] == "FUDKII_RT_Y_F" and r["gate"] == "entry_cutoff" for r in ev)

        clock[0] = _at("15:10:59")  # the last minute is still open, for the graded-F shadow too
        g = replace(f, ts=f.ts + 1800, symbol="RELIANCE")
        await e._handle_unpublished(g, None)
        assert [p.strategy for p in held(e)] == ["FUDKII_RT_Y_F"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_entry_whose_gates_ran_past_the_cutoff_is_not_sent(settings, monkeypatch):
    """A gated book's volume read waits on the broker: decided at 15:10:58, its order would go at 15:11:02. The
    minute is read again before the order (operator, 2026-10-17: nothing is bought after 15:10)."""
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        clock = [_at("15:10:58")]
        monkeypatch.setattr(Engine, "_past_last_entry", lambda self, b, u, *, now=None: REAL_PAST_LAST_ENTRY(self, b, u, now=clock[0]))

        async def slow_gates(key, s, underlying, vol):
            clock[0] = _at("15:11:02")
            return None

        monkeypatch.setattr(e, "_book_gates", slow_gates)
        out = await e._handle_signal(sig, None, books=(StrategyKey.FUDKII_RT_X,))
        assert out["FUDKII_RT_X"]["decision"] == "PAST_ENTRY_CUTOFF" and not e.positions
        assert e.wallets["FUDKII_RT_X"].deployed == 0
    finally:
        await e.stop()


# -- the 14:45 bar's triggers: recorded and listed, never traded, never carried -------------------


def _today_at(hm: str) -> int:
    return int(from_ist(datetime.combine(date.today(), dtime.fromisoformat(hm), tzinfo=IST)))


def test_only_a_trigger_decided_between_the_cutoff_and_the_close_is_recorded_only(settings):
    e = Engine(settings)
    e.underlyings["RELIANCE"] = UND
    base = Signal(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BULLISH, ts=_today_at("14:45"),
                  entry=1500.0, stop=1490.0, targets=(1530.0,))
    why = e._after_entry_cutoff(base)
    assert why is not None and why.startswith("after the 15:10 NSE entry cutoff — recorded only")
    assert "decided at 15:15" in why and "not traded, not carried" in why
    assert e._after_entry_cutoff(replace(base, ts=_today_at("14:15"))) is None, "decided at 14:45: traded as ever"
    assert e._after_entry_cutoff(replace(base, ts=_today_at("15:15"))) is None, "the close's bar is carried, not this"
    assert e._after_close(replace(base, ts=_today_at("15:15")))
    e.underlyings["CRUDEOIL"] = Instrument("477176", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, underlying="CRUDEOIL")
    assert e._after_entry_cutoff(replace(base, symbol="CRUDEOIL")) is None, "MCX keeps its own session"


@pytest.mark.asyncio
async def test_a_1445_trigger_decided_at_1515_is_recorded_and_listed_never_traded_nor_carried(settings, monkeypatch):
    import httpx

    from kotsin_nse.api.routes import build_app
    from kotsin_nse.bars.unified import UnifiedBar
    from kotsin_nse.instrument.select import Selection
    from kotsin_nse.strategy.base import Outcome

    from .test_limit_orders import UND as TATA

    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        e.underlyings["TATASTEEL"] = TATA
        pub = replace(sig, ts=_today_at("14:45"))
        e._breadth_at[pub.signal_id] = {"share": 0.7, "names": 200}
        f = _unpublished(replace(sig, symbol="TATASTEEL", ts=_today_at("14:45"), entry=187.0, stop=180.0, targets=(195.0,)))
        bar = UnifiedBar("RELIANCE", "2885", "30m", pub.ts, 1495.0, 1502.0, 1494.0, 1500.0, 50_000.0, complete=True)
        selected: list[str] = []

        async def select(underlying, s, *, tape=True):
            selected.append(s.symbol)
            raise AssertionError("no strike is chosen for a trigger recorded after the cutoff")

        e._select_instrument = select  # type: ignore[method-assign]
        monkeypatch.setattr(e.fudkii, "on_bar", lambda _ctx, _bar: Outcome(signals=[pub], triggers=[f]))
        monkeypatch.setattr(e.fukaa, "on_signal", lambda *_a: Outcome())
        monkeypatch.setattr(e.fukaa, "on_bar", lambda *_a: Outcome())
        await e._decide(bar)

        assert not e.positions and not e._resting and not selected, "nothing is bought, nothing rests, no broker read"
        rows = {r["signal_id"]: r for r in await e.ledger.rows_between("signals", 0, time.time() + AHEAD)}
        assert rows[pub.signal_id]["decision"] == "RECORDED_ONLY"
        assert rows[pub.signal_id]["decision_reason"].startswith("after the 15:10 NSE entry cutoff — recorded only")
        assert rows[f.signal_id]["decision"] == "NOT_PUBLISHED", "an unpublished trigger keeps FUDKII's own grade on its row"
        ev = await e.ledger.rows_between("events", 0, time.time() + AHEAD)
        rec = {r["signal_id"]: r for r in ev if r.get("kind") == "cutoff.recorded"}
        assert set(rec) == {pub.signal_id, f.signal_id} and rec[pub.signal_id]["cutoff"] == "15:10"
        assert set(rec[pub.signal_id]["books"]) == {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y"}
        assert set(rec[f.signal_id]["books"]) == {"FUDKII_RT_Y_F", "FUDKII_RT_Y", "FUDKII_RT_X"}
        skips = {(r["book"], r["signal_id"]) for r in ev if r.get("kind") == "rt_twin.skipped" and r["gate"] == "entry_cutoff"}
        assert {(b, pub.signal_id) for b in rec[pub.signal_id]["books"]} <= skips
        assert {(b, f.signal_id) for b in rec[f.signal_id]["books"]} <= skips
        assert not [r for r in ev if r.get("kind") == "carry.queued"], "not carried to the next open"

        # on the list: every book's card reads RECORDED ONLY with the reason, and no TAKE
        today = date.today()
        for book in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y"):
            card = next(c for c in (await e.book_cards(book, today))["cards"] if c["signalId"] == pub.signal_id)
            assert card["state"] == "RECORDED_ONLY", (book, card["state"])
            assert card["recordedOnly"].startswith("after the 15:10") and card["cta"]["enabled"] is False, book
        shadow = next(c for c in (await e.book_cards("FUDKII_RT_Y_F", today))["cards"] if c["signalId"] == f.signal_id)
        assert shadow["state"] == "RECORDED_ONLY" and shadow["cta"]["enabled"] is False
        parent_f = next(c for c in (await e.book_cards("FUDKII", today))["cards"] if c["signalId"] == f.signal_id)
        assert parent_f["state"] == "NOT_PUBLISHED", "FUDKII would not have traded its own F either"
        # ... and on the Signals page's list (the API it reads), with the alert card adopted as any trigger's
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t") as c:
            listed = {r["signal_id"]: r for r in (await c.get("/api/signals")).json()}
            assert listed[pub.signal_id]["decision"] == "RECORDED_ONLY"
            assert "recorded only" in listed[pub.signal_id]["decision_reason"]
            assert f.signal_id in {r["signal_id"] for r in (await c.get("/api/signals", params={"unpublished": "true"})).json()}
            today_list = (await c.get("/api/fudkii/today")).json()["signals"]
            assert any(r["signal_id"] == pub.signal_id and r["decision"] == "RECORDED_ONLY" for r in today_list)
        assert any((a.get("evidence") or {}).get("signalId") == pub.signal_id for a in e.alerts.feed("FUDKII_RT")), "the alert row"

        # an operator TAKE on the card after the cutoff is refused like any entry past it
        async def priced(underlying, s, *, tape=True):
            return Selection(_opt, premium=20.0, reason="ok", spread_pct=0.5)

        e._select_instrument = priced  # type: ignore[method-assign]
        monkeypatch.setattr(Engine, "_past_last_entry", lambda self, b, u, *, now=None: REAL_PAST_LAST_ENTRY(self, b, u, now=_at("15:15:05")))
        out = await e.operator_take("FUDKII_RT_Y", pub.signal_id)
        assert out["entered"] is False and out["decision"] == "PAST_ENTRY_CUTOFF" and not e.positions
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_resting_nse_entry_is_cancelled_at_the_entry_cutoff_never_filled_after_it(settings, monkeypatch):
    from .test_limit_orders import _book, _engine, _sig

    clock = [_at("15:10:50", date.today())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(Engine, "_past_last_entry", lambda self, b, u, *, now=None: REAL_PAST_LAST_ENTRY(self, b, u, now=clock[0]))
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        sig = _sig(clock)
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        assert {r.intent.strategy for r in e._resting.values() if r.kind == "entry"} == {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}
        clock[0] = _at("15:11:00", date.today())
        _book(e, 16.95, 17.10, clock[0])  # the ask comes down to the limit — after the cutoff
        await e._manage_positions()
        assert not e._resting and not e.positions, "cancelled at the cutoff, never filled after it"
        missed = [r for r in await e.ledger.rows_between("events", 0, clock[0] + 60) if r.get("kind") == "limit.missed"]
        assert len(missed) == 4 and all(r["why"].startswith("cancelled at the entry cutoff") for r in missed)
        rows = [r for r in await e.ledger.rows_between("signals", 0, clock[0] + 86_400) if r["signal_id"] == sig.signal_id]
        assert rows[-1]["decision"] == "LIMIT_UNFILLED" and "entry cutoff" in rows[-1]["decision_reason"]
        assert e.wallets["FUDKII"].deployed == 0, "the hold is released"
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
        e._ltp_ts[UND.scrip_code] = open_ts + 2
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
