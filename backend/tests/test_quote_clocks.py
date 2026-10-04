"""Two clocks (review, 2026-10-03): when we SAW a price — what every age guard asks — and when the
broker last TRADED it — only whether a print is new. Stage 1 wrote 5paisa's last-trade time into the
observation field, and with Stage 0's background re-quote running, a quiet held option read minutes
"old" and was left to the equity stop alone; a 5 s-cached older print replaced a fresh feed quote,
its book could no longer be confirmed, and an exit was refused against a stale book."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.domain import Direction, ExitDecision, ExitReason
from kotsin_nse.engine import Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote
from tests._books import held

from .test_entry_cutoff_and_carry import _close_trigger, _next_session
from .test_limit_orders import OPT as L_OPT
from .test_limit_orders import _engine as _limit_engine
from .test_limit_orders import _sig as _limit_sig
from .test_phase2_exit_safety import OPT, UND, _pos
from .test_quote_integrity import P720
from .test_quote_integrity import _engine as _feed_engine
from .test_wide_stop_shadow import UND as CARRY_UND
from .test_wide_stop_shadow import _rt_y_trigger


@pytest.fixture
def clock(monkeypatch):
    from datetime import datetime
    from datetime import time as dtime

    from kotsin_nse.market.session import IST, ist_today

    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _row(ltp: float, *, traded: float, bid: float = 0.0, bid_qty: int = 0) -> dict:
    return {"ltp": ltp, "bid": bid, "ask": 0.0, "bid_qty": bid_qty, "ask_qty": 0, "ts": time.time(), "traded_ts": traded, "volume": 0}


async def _paper(settings, monkeypatch) -> Engine:
    import kotsin_nse.engine as engine_mod

    monkeypatch.setattr(engine_mod, "past_force_flat", lambda *_a: False)
    e = Engine(settings)
    await e.start()
    await e.set_mode(Mode.PAPER)
    return e


@pytest.mark.asyncio
async def test_a_feed_frame_keeps_both_clocks(settings):
    e = Engine(settings)
    await e._on_tick({"scrip_code": OPT.scrip_code, "ltp": 0.55, "bid": 0.54, "ask": 0.56, "recv_ts": 1_000.0, "ts": 940.0,
                      "exch": "N", "exch_type": "D"})
    q = e.quotes[OPT.scrip_code]
    assert (q.ts, q.traded_ts) == (1_000.0, 940.0) and e._ltp_traded_ts[OPT.scrip_code] == 940.0


@pytest.mark.asyncio
async def test_a_quiet_held_option_marked_from_an_old_print_still_has_its_option_stop_checked(settings, monkeypatch):
    """The broker's last trade was five minutes ago. Stamped with that time the position was 'stale'
    and only the equity stop could act; stamped with when we asked, its own stop fires."""
    e = await _paper(settings, monkeypatch)
    try:
        pos = _pos()  # RT-X, option stop 0.60 — the 9 % hard floor is 0.546
        e.positions[pos.id] = pos
        e.wallets["FUDKII_RT_X"].commit(pos.entry * pos.qty, time.time())
        e.ltps[UND.scrip_code] = 187.9  # the stock is inside its stop: only the option side can act
        now = time.time()

        async def market_feed(_insts):
            return {}

        monkeypatch.setattr(e.rest, "market_feed", market_feed)
        e._apply_snapshot({OPT.scrip_code: _row(0.50, traded=now - 300)}, now)
        q = e.quotes[OPT.scrip_code]
        assert q.ts == pytest.approx(now, abs=1) and q.traded_ts == now - 300
        await e._manage_positions()
        assert pos.id not in e._stale_positions, "observed now: not stale"
        assert pos.status == "CLOSED" and pos.exit_reason == ExitReason.SL_OP.value, "the option's own hard floor fired"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_fresh_feed_quote_is_never_replaced_by_an_older_cached_print(settings):
    for live in (True, False):
        e = await _feed_engine(settings, live=live)
        now = time.time()
        e.positions["p1"] = _pos(id="p1", instrument=P720, qty=2600, entry=16.0, option_sl=15.0, option_targets=(17.0,))
        e.quotes["78427"] = Quote(ltp=15.9, bid=15.85, ask=15.95, ts=now - 2, traded_ts=now - 2)
        e.ltps["78427"], e._ltp_traded_ts["78427"] = 15.9, now - 2
        e._apply_snapshot({"78427": _row(15.6, traded=now - 7)}, now)  # 5paisa's cache: an older trade
        q = e.quotes["78427"]
        assert q.ltp == 15.9 and q.ts == now and e.ltps["78427"] == 15.9 and e._ltp_traded_ts["78427"] == now - 2, live
        if live:
            assert (q.bid, q.ask) == (15.85, 15.95) and e.snapshot_confirmed == 1, "a live feed: confirmed as held"
        else:
            assert (q.bid, q.ask) == (0.0, 0.0) and e.snapshot_held_marked == 1, "a silent feed: marked from the newest trade, no unvouched bid/ask"


@pytest.mark.asyncio
async def test_a_newer_broker_print_does_replace_the_held_quote(settings):
    e = await _feed_engine(settings, live=True)
    now = time.time()
    e.positions["p1"] = _pos(id="p1", instrument=P720, qty=2600, entry=16.0, option_sl=15.0, option_targets=(17.0,))
    e.quotes["78427"] = Quote(ltp=15.9, bid=15.85, ask=15.95, ts=now - 2, traded_ts=now - 2)
    e.ltps["78427"] = 15.9
    e._apply_snapshot({"78427": _row(15.6, traded=now - 1)}, now)
    q = e.quotes["78427"]
    assert (q.mid, q.src, q.ts, q.traded_ts) == (15.6, "snapshot", now, now - 1) and e.snapshot_held_marked == 1
    assert e.ltps["78427"] == 15.6 and e._ltp_traded_ts["78427"] == now - 1
    e2 = await _feed_engine(settings, live=True)  # not held: kept, and the choice waits for the feed
    e2.quotes["78427"] = Quote(ltp=15.9, bid=15.85, ask=15.95, ts=now - 2, traded_ts=now - 2)
    e2._apply_snapshot({"78427": _row(15.6, traded=now - 1)}, now)
    assert e2.quotes["78427"].bid == 15.85 and "78427" in e2._quote_outdated


@pytest.mark.asyncio
async def test_a_snapshot_book_is_fresh_and_a_stop_fills_against_it(settings, monkeypatch):
    """The re-quote stood a one-sided book behind the broker's bid, stamped with the last trade's
    time: born stale, and the paper matcher refused the exit (REJECTED_BOOK)."""
    e = await _paper(settings, monkeypatch)
    try:
        pos = _pos()
        e.positions[pos.id] = pos
        e.wallets["FUDKII_RT_X"].commit(pos.entry * pos.qty, time.time())
        now = time.time()
        e.books[OPT.scrip_code] = BookSnapshot(scrip_code=OPT.scrip_code, bids=[(0.53, 9000)], asks=[(0.56, 9000)], ts=now - 25)

        async def market_feed(_insts):
            return {OPT.scrip_code: _row(0.55, traded=now - 300, bid=0.54, bid_qty=60_000)}

        monkeypatch.setattr(e.rest, "market_feed", market_feed)
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_EQ, 0.55, pos.qty, "underlying breached"), now)
        assert e.books[OPT.scrip_code].age_ms(time.time()) < 1000
        assert pos.status == "CLOSED" and pos.exit_price == 0.54, "sold into the fresh one-sided book"
        orders = await e.ledger.rows_between("orders", 0, time.time() + 5)
        assert not [o for o in orders if o.get("decision") == "REJECTED_BOOK"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_resting_entry_never_fills_on_a_cached_broker_print(settings, clock):
    e = await _limit_engine(settings, clock)
    try:
        code = L_OPT.scrip_code
        await e._on_tick({"scrip_code": code, "ltp": 17.10, "bid": 16.95, "ask": 17.25, "recv_ts": clock[0], "ts": clock[0],
                          "exch": "N", "exch_type": "D"})
        await e._handle_signal(_limit_sig(clock), None)
        assert e._resting and not e.positions, "resting at the signal price"
        clock[0] += 2
        e._apply_snapshot({code: _row(17.00, traded=clock[0] - 7)}, clock[0])  # an older trade, below the limit
        await e._manage_positions()
        assert not e.positions and e.ltps[code] == 17.10, "a cached print is not a trade through the limit"
        clock[0] += 1
        await e._on_tick({"scrip_code": code, "ltp": 17.00, "bid": 16.95, "ask": 17.25, "recv_ts": clock[0], "ts": clock[0],
                          "exch": "N", "exch_type": "D"})
        await e._manage_positions()
        assert e.positions and all(p.entry == 17.10 for p in e.positions.values()), "a new print through it fills at the limit"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_carried_trigger_waits_for_a_print_traded_this_session(settings):
    """Yesterday's close re-sent at a subscribe after the open arrives now but traded yesterday."""
    from datetime import date

    from kotsin_nse.config import Segment
    from kotsin_nse.market.session import session_open_ts

    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        trig = _close_trigger(e, sig)
        await e._carry_queue(trig)
        open_ts = session_open_ts(Segment.NSE_EQ, _next_session(e, date.today()))
        e.ltps[CARRY_UND.scrip_code], e._ltp_traded_ts[CARRY_UND.scrip_code] = 1505.0, open_ts - 60
        await e._carry_tick(open_ts + 5)
        assert not e.positions and e._carry_pending, "an old trade arriving late is not the first print"
        e._ltp_traded_ts[CARRY_UND.scrip_code] = open_ts + 2
        await e._carry_tick(open_ts + 6)
        while e._decision_tasks:
            import asyncio

            await asyncio.gather(*list(e._decision_tasks), return_exceptions=True)
        assert len(held(e)) == 5 and not e._carry_pending
    finally:
        await e.stop()
