"""The NSE close (operator, 2026-10-17): "if we are in trade till 15:14IST, then we exit at the first avenue
before 15:20IST".

* 15:14:00 — every NSE position of every book (the FUDKII family, the RT and CT books, the graded-F shadow,
  every stop-rule mirror, an operator TAKE's) is flattened: sold into the bid at once, no 10 s walk from the
  mid; a refused try goes again every 2 s, not on the 2-4-8-16-30 s backoff;
* 15:19:00 — whatever is still open (a stale book, no quote, every try refused) is filled on PAPER at its last
  known bid, else its last known price, flagged ``eodForced`` and alarmed (a log error, a ledger event,
  Telegram, a failing ``nse_out_by`` health line). A LIVE book's position is never paper-filled;
* 15:20:00 — an NSE position still open, or an NSE exit filling, alarms.

MCX keeps its own clock (flatten 23:20, the walk, the backoff) and none of these alarms.

Engine B runs the same rule as engine A, on the same times, flags and alarms; what is B's own: the last
price's age reads B's two clocks (when we saw it, else when it traded), and the day roll prunes the
deadline's state (``_roll_day_state``)."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    ExitDecision,
    ExitReason,
    Instrument,
    InstrumentKind,
    Position,
    PosSide,
)
from kotsin_nse.engine import EOD_RETRY_S, NSE_EOD_AT_ONCE, Engine, exit_client_order_id
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote
from kotsin_nse.market.session import (
    IST,
    NSE_FORCED_FILL,
    NSE_OUT_BY,
    held_overnight,
    ist_today,
    past_force_flat,
    past_nse_forced_fill,
    past_nse_out_by,
    spec,
)
from tests.test_limit_orders import OPT, UND, _engine, _rows

CRUDE = Instrument("477176", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, name="CRUDEOIL 19 NOV 2026", lot_size=100,
                   tick_size=1.0, underlying="CRUDEOIL")
NSE_BOOKS = ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_Y_F", "FUDKII_RT_Y_F_SE", "FUDKII_CT_Y", "FUDKII_RT_Y_W1_SA")


def _at(h: int, m: int, s: int = 0) -> float:
    return datetime.combine(ist_today(), dtime(h, m, s), tzinfo=IST).timestamp()


@pytest.fixture
def clock(monkeypatch):
    now = [_at(15, 13, 59)]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _pos(e: Engine, clock, *, pid: str = "p1", strategy: str = "FUDKII_RT_X", qty: int = 2000, inst: Instrument = OPT,
         und: Instrument = UND, entry: float = 10.0) -> Position:
    """A held NSE option (entry 10.00, stop 8.00 — nowhere near) with its book's money committed."""
    pos = Position(id=pid, strategy=strategy, instrument=inst, underlying=und, side=PosSide.LONG, qty=qty, entry=entry,
                   opened_ts=clock[0] - 1800, signal_id=f"s-{pid}", direction=Direction.BULLISH, equity_entry=187.0,
                   equity_sl=185.0, option_sl=8.0, qty_remaining=qty)
    e.positions[pos.id] = pos
    e.wallets[strategy].commit(entry * qty * inst.multiplier, clock[0])
    return pos


def _quote(e: Engine, bid: float, ask: float, *, book_ts: float, quote_ts: float | None = None, code: str = OPT.scrip_code) -> None:
    e.books[code] = BookSnapshot(code, bids=[(bid, 50_000)], asks=[(ask, 50_000)], ts=book_ts)
    e.quotes[code] = Quote(ltp=(bid + ask) / 2, bid=bid, ask=ask, ts=book_ts if quote_ts is None else quote_ts)
    e.ltps[code] = (bid + ask) / 2
    e.ltps[UND.scrip_code] = 187.0  # the stock where it was bought: no equity stop


async def _events(e: Engine, kind: str) -> list[dict]:
    return [r for r in await _rows(e, "events") if r.get("kind") == kind]


# -- the clock, pure --------------------------------------------------------------------------------


def test_the_nse_deadline_is_1514_1519_1520_and_mcx_keeps_its_own():
    for seg in (Segment.NSE_EQ, Segment.NSE_FO, Segment.NSE_IDX):
        assert not past_force_flat(seg, _at(15, 13, 59)) and past_force_flat(seg, _at(15, 14))
        assert not past_nse_forced_fill(seg, _at(15, 18, 59)) and past_nse_forced_fill(seg, _at(15, 19))
        assert not past_nse_out_by(seg, _at(15, 19, 59)) and past_nse_out_by(seg, _at(15, 20))
    assert (NSE_FORCED_FILL, NSE_OUT_BY) == (dtime(15, 19), dtime(15, 20))
    assert spec(Segment.MCX_FO).force_flat == dtime(23, 20) and not past_force_flat(Segment.MCX_FO, _at(15, 25))
    assert not past_nse_forced_fill(Segment.MCX_FO, _at(23, 59)) and not past_nse_out_by(Segment.MCX_FO, _at(23, 59))


def test_a_position_opened_on_an_earlier_day_is_past_both_nse_deadlines_at_once():
    """Review, 2026-10-17: the deadlines read the time of day only, so yesterday's NSE position (an engine down over
    the close, a SHADOW or LIVE book's lots) was a normal position until 15:19 the next day. Opened on an earlier IST
    day, it is past both from the first read; MCX keeps its own session."""
    yesterday = _at(14, 40) - 86_400
    for seg in (Segment.NSE_EQ, Segment.NSE_FO, Segment.NSE_IDX):
        assert past_nse_forced_fill(seg, _at(0, 45), yesterday) and past_nse_out_by(seg, _at(9, 20), yesterday)
        assert not past_nse_forced_fill(seg, _at(9, 20), _at(9, 16)) and not past_nse_out_by(seg, _at(9, 20), _at(9, 16))
        assert not past_nse_out_by(seg, _at(9, 20)), "without the opening time: the time of day, as before"
    assert not past_nse_forced_fill(Segment.MCX_FO, _at(9, 20), yesterday) and not past_nse_out_by(Segment.MCX_FO, _at(9, 20), yesterday)
    assert held_overnight(_at(0, 30), yesterday) and not held_overnight(_at(0, 30), 0.0)


def test_a_forced_exit_has_its_own_order_id():
    pos = Position(id="p", strategy="FUDKII_RT_X", instrument=OPT, underlying=UND, side=PosSide.LONG, qty=2000, entry=10.0,
                   opened_ts=0.0, signal_id="s", direction=Direction.BULLISH, exec_log={"ref": "FII-RTX-261017-145000-003"})
    d = ExitDecision("p", ExitReason.EOD, 9.9, 2000)
    assert exit_client_order_id(pos, d, 2, forced=True) == "FII-RTX-261017-145000-003-EOD0R2F-TATASTEEL-190CE-L2"
    assert exit_client_order_id(pos, d, 2) == "FII-RTX-261017-145000-003-EOD0R2-TATASTEEL-190CE-L2"
    pos.exec_log = {}
    assert exit_client_order_id(pos, d, forced=True) == "p|EXIT|0|EOD|F"


# -- 15:14: at once, every book -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_1514_every_nse_position_sells_into_the_bid_at_once(settings, clock, nse_deadline):
    e = await _engine(settings, clock)
    try:
        held = [_pos(e, clock, pid=f"p{i}", strategy=b) for i, b in enumerate(NSE_BOOKS)]
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._manage_positions()
        assert all(p.status == "OPEN" for p in held) and not [r for r in e._resting.values() if r.kind == "exit"], "15:13:59: nothing"

        clock[0] = _at(15, 14)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._manage_positions()
        assert all(p.status == "CLOSED" and p.exit_reason == "EOD" and p.exit_price == 9.95 for p in held), \
            "15:14:00: every book, the graded-F shadow and the mirrors, sold at the bid in the same second"
        assert not e._resting, "nothing rests at the mid"
        for p in held:
            x = p.exec_log["exits"][-1]
            assert x["filledTs"] == _at(15, 14) and NSE_EOD_AT_ONCE in x["why"]
        assert not [r for r in await _rows(e, "events") if str(r.get("kind", "")).startswith("eod.")], "out in time: no alarm"
        assert e._eod_check().ok
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_mcx_flatten_keeps_its_walk_and_its_backoff(settings, clock):
    clock[0] = _at(23, 20)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock, pid="m", strategy="FUDKII_RT_MCX", qty=100, inst=CRUDE, und=CRUDE, entry=5000.0)
        _quote(e, 5000.0, 5004.0, book_ts=clock[0], code=CRUDE.scrip_code)
        eod = ExitDecision(pos.id, ExitReason.EOD, 5002.0, 100, "segment force-flat")
        await e._exit(pos, eod, clock[0])
        r = e._exit_resting(pos.id)
        assert r is not None and r.deadline_s == 10 and pos.status == "OPEN", "MCX's 23:20 close rests at the mid and walks"
        assert e._exit_retry_s(pos, eod, 3, clock[0]) == 8.0, "MCX keeps the 2-4-8-16-30 s backoff"
        nse = _pos(e, clock, pid="n")
        manual = ExitDecision(nse.id, ExitReason.MANUAL, 9.9, 2000)
        assert e._exit_retry_s(nse, ExitDecision(nse.id, ExitReason.EOD, 9.9, 2000), 3, clock[0]) == EOD_RETRY_S == 2.0
        assert e._exit_retry_s(nse, manual, 3, _at(15, 13, 59)) == 8.0, "before the flatten an exit keeps the backoff"
        assert e._exit_retry_s(nse, manual, 3, _at(15, 14)) == 2.0, "from it, any NSE exit retries every 2 s"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_from_1514_no_nse_exit_rests_a_halt_or_an_earlier_trail_sells_at_once(settings, clock):
    clock[0] = _at(15, 13, 55)
    e = await _engine(settings, clock)
    try:
        trail = _pos(e, clock, pid="t")
        halt = _pos(e, clock, pid="h", strategy="FUDKII_RT_Y")
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._exit(trail, ExitDecision(trail.id, ExitReason.TRAIL, 10.0, 2000, "give-back"), clock[0])
        assert e._exit_resting(trail.id).deadline_s == 45, "15:13:55: a trail still rests at the mid and walks"
        clock[0] = _at(15, 14)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._exit(trail, ExitDecision(trail.id, ExitReason.TRAIL, 10.0, 2000, "give-back"), clock[0])
        assert trail.status == "CLOSED" and trail.exit_price == 9.95 and not e._resting, "15:14: replaced by the sale at the bid"
        await e._exit(halt, ExitDecision(halt.id, ExitReason.HALT, 10.0, 2000, "halted"), clock[0])
        assert halt.status == "CLOSED" and halt.exit_price == 9.95, "a halt that beats the EOD rule to it sells at once too"
        assert NSE_EOD_AT_ONCE in halt.exec_log["exits"][-1]["why"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_refused_nse_flatten_tries_again_every_two_seconds(settings, clock, nse_deadline):
    clock[0] = _at(15, 14)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        _quote(e, 9.95, 10.05, book_ts=clock[0] - 40, quote_ts=clock[0])  # the depth book 40 s old: the matcher refuses it
        await e._manage_positions()
        assert pos.status == "OPEN" and e._exit_retry_at[pos.id] - clock[0] == 2.0
        clock[0] += 1
        await e._manage_positions()
        clock[0] += 1
        e.quotes[OPT.scrip_code] = Quote(ltp=10.0, bid=9.95, ask=10.05, ts=clock[0])
        await e._manage_positions()
        assert pos.status == "OPEN" and e._exit_retry_at[pos.id] - clock[0] == 2.0, "not 4 s: the second refusal waits 2 s too"
        refused = [o for o in await _rows(e, "orders") if o.get("purpose") == "EXIT" and o.get("decision") == "REJECTED_BOOK"]
        assert len(refused) == 2 and len({o["client_order_id"] for o in refused}) == 2, "a try at 15:14:00 and 15:14:02, none between"
        clock[0] += 2
        _quote(e, 9.90, 10.00, book_ts=clock[0])  # the book is back
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.exit_price == 9.90 and pos.id not in e._exit_fail_note
        assert not await _events(e, "eod.forced_fill"), "out by the flatten itself, before 15:19"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_contract_with_no_last_price_still_takes_the_bid_at_1514(settings, clock, nse_deadline):
    clock[0] = _at(15, 14)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=[(9.80, 50_000)], asks=[(10.20, 50_000)], ts=clock[0])
        assert OPT.scrip_code not in e.ltps
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.exit_price == 9.80 and pos.exit_reason == "EOD", \
            "the flatten no longer skips a contract that has not printed: the book's bid is its first avenue"
    finally:
        await e.stop()


# -- 15:19: the forced fill ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_1519_a_position_still_open_is_paper_filled_at_the_last_known_bid_flagged_and_alarmed(settings, clock, nse_deadline):
    clock[0] = _at(15, 18, 58)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock, strategy="FUDKII_RT_Y_F")
        e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=[(9.80, 50_000)], asks=[(10.30, 50_000)], ts=clock[0] - 90)
        e.quotes[OPT.scrip_code] = Quote(ltp=9.9, bid=9.85, ask=10.25, ts=clock[0] - 30)  # the newer of the two
        e.ltps[OPT.scrip_code] = 9.9
        e.ltps[UND.scrip_code] = 187.0
        await e._manage_positions()
        assert pos.status == "OPEN", "15:18:58: the flatten's try is refused (the book is 90 s old)"
        assert e._eod_check().ok

        clock[0] = _at(15, 19)
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.exit_reason == "EOD" and pos.exit_price == 9.85, "the last known bid"
        flag = pos.exec_log["eodForced"]
        assert flag["priceFrom"] == "bid" and flag["priceAgeS"] == 32.0 and flag["ist"] == "15:19:00"
        assert "is 9" in flag["cause"] and "ms old" in flag["cause"], "the matcher's own refusal is kept as the cause"
        x = pos.exec_log["exits"][-1]
        assert x["eodForced"] == flag and x["fillPrice"] == 9.85
        (order,) = [o for o in await _rows(e, "orders") if o.get("purpose") == "EXIT" and o.get("decision") == "PAPER_FILLED"]
        assert order["note"].startswith("forced fill 15:19, stale book") and "last known bid 9.85" in order["note"]
        assert order["client_order_id"].endswith("|F") or "F-" in order["client_order_id"]
        (trade,) = await _rows(e, "trades")
        assert trade["exit_reason"] == "EOD" and trade["eod_forced"]["price"] == 9.85, "the trade carries the flag"
        (alarm,) = await _events(e, "eod.forced_fill")
        assert alarm["book"] == "FUDKII_RT_Y_F" and alarm["qty"] == 2000 and alarm["priceFrom"] == "bid"
        assert not await _events(e, "eod.exit_after_deadline") and not await _events(e, "eod.still_open"), "15:19:00 is in time"
        chk = e._eod_check()
        assert not chk.ok and "1 forced fill" in chk.detail and "TATASTEEL" in chk.detail, "the health line is red for the day"
        assert e.wallets["FUDKII_RT_Y_F"].deployed == 0
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_forced_fill_falls_back_to_the_last_price_then_the_entry(settings, clock, nse_deadline):
    clock[0] = _at(15, 19, 5)
    e = await _engine(settings, clock)
    try:
        a = _pos(e, clock, pid="a")
        e.ltps[OPT.scrip_code] = 9.7  # a last price, never a bid
        # two clocks: with no observation of the price held, its age is the broker's trade time ...
        e._ltp_traded_ts[OPT.scrip_code] = clock[0] - 50
        assert e._last_known_exit_price(a, clock[0]) == (9.7, "last price", 50.0)
        # ... and a REST snapshot holding it (0/0: prices nothing) says when WE last saw it
        e.quotes[OPT.scrip_code] = Quote(ltp=9.7, bid=0.0, ask=0.0, ts=clock[0] - 20, src="snapshot", traded_ts=clock[0] - 50)
        assert e._last_known_exit_price(a, clock[0]) == (9.7, "last price", 20.0)
        e.quotes[OPT.scrip_code] = Quote(ltp=9.6, bid=0.0, ask=0.0, ts=clock[0] - 5, src="snapshot", traded_ts=clock[0] - 70)
        assert e._last_known_exit_price(a, clock[0]) == (9.7, "last price", 50.0), "a quote of another price is not its sighting"
        del e.quotes[OPT.scrip_code]
        await e._manage_positions()
        assert a.status == "CLOSED" and a.exit_price == 9.7 and a.exec_log["eodForced"]["priceFrom"] == "last price"

        other = Instrument("153806", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, name="TATASTEEL 29 SEP 2026 CE 195.00",
                           lot_size=1000, tick_size=0.05, strike=195.0, underlying="TATASTEEL")
        b = _pos(e, clock, pid="b", inst=other, entry=4.0)  # nothing ever priced it
        await e._manage_positions()
        assert b.status == "CLOSED" and b.exit_price == 4.0
        assert b.exec_log["eodForced"]["priceFrom"].startswith("entry") and "no last price" in b.exec_log["eodForced"]["cause"]
        assert len(await _events(e, "eod.forced_fill")) == 2
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_live_book_position_is_never_paper_filled_and_shadow_places_nothing(settings, clock, nse_deadline):
    e = await _engine(settings, clock)
    try:
        live = _pos(e, clock, pid="live", strategy="FUDKII")
        twin = _pos(e, clock, pid="twin", strategy="FUDKII_RT_X")
        e._mode, e._armed_until = Mode.LIVE, clock[0] + 3_600
        assert not e._fills_on_paper(live) and e._fills_on_paper(twin), "a LIVE book's lots are at the broker"
        e._mode = Mode.SHADOW
        assert not e._fills_on_paper(live) and not e._fills_on_paper(twin)
        await e.set_mode(Mode.PAPER)
        assert e._fills_on_paper(live) and e._fills_on_paper(twin)

        await e.set_mode(Mode.SHADOW)
        clock[0] = _at(15, 19)
        await e._manage_positions()
        await e._manage_positions()
        assert live.status == "OPEN" and twin.status == "OPEN", "SHADOW places nothing — and never paper-fills"
        assert {r["positionId"] for r in await _events(e, "eod.not_forced")} == {"live", "twin"}
        clock[0] = _at(15, 20)
        await e._manage_positions()
        await e._manage_positions()
        opened = await _events(e, "eod.still_open")
        assert sorted(r["positionId"] for r in opened) == ["live", "twin"], "once each, however many passes"
        assert all(r["at"] == "15:20:00" for r in opened)
        chk = e._eod_check()
        assert not chk.ok and "still open" in chk.detail
    finally:
        await e.stop()


# -- 15:20: late alarms -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_1520_an_open_nse_position_alarms_and_so_does_its_late_fill(settings, clock, nse_deadline):
    clock[0] = _at(15, 20)  # e.g. the engine came back after the deadline
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        _quote(e, 9.95, 10.05, book_ts=clock[0] - 120, quote_ts=clock[0] - 120)
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.exec_log["eodForced"]["price"] == 9.95
        kinds = sorted(str(r["kind"]) for r in await _rows(e, "events") if str(r.get("kind", "")).startswith("eod."))
        assert kinds == ["eod.exit_after_deadline", "eod.forced_fill", "eod.still_open"]
        (late,) = await _events(e, "eod.exit_after_deadline")
        assert late["reason"] == "EOD" and late["at"] == "15:20:00" and late["qty"] == 2000
        chk = e._eod_check()
        assert not chk.ok and "exit after deadline" in chk.detail and "still open" in chk.detail
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_stop_filled_at_1519_59_is_in_time_and_mcx_never_alarms(settings, clock, nse_deadline):
    clock[0] = _at(15, 13)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        clock[0] = _at(15, 19, 59)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 9.95, 2000, "stop", level=10.0, trigger_price=9.95,
                                        trigger_on="option mid"), clock[0])
        assert pos.status == "CLOSED" and not await _events(e, "eod.exit_after_deadline")

        clock[0] = _at(15, 25)
        crude = _pos(e, clock, pid="m", strategy="FUDKII_RT_MCX", qty=100, inst=CRUDE, und=CRUDE, entry=5000.0)
        _quote(e, 5000.0, 5004.0, book_ts=clock[0], code=CRUDE.scrip_code)
        e.ltps[CRUDE.scrip_code] = 5002.0
        await e._manage_positions()
        assert crude.status == "OPEN" and "eodForced" not in crude.exec_log, "MCX trades to 23:30: nothing at 15:19 / 15:20"
        assert not [r for r in await _rows(e, "events") if str(r.get("kind", "")).startswith("eod.")]
        assert e._eod_check().ok
    finally:
        await e.stop()


# -- engine B: per-day state in one place -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_day_roll_drops_yesterdays_deadline_state(settings, clock):
    """B resets per-day state in one place (``_roll_day_state``, review 2026-10-03: maps keyed by time were never
    pruned). Yesterday's alarm rows and raised kinds go — the alarms are once a position, kind and day, so a position
    still held alarms again on the new day (review, 2026-10-17) — and a closed position's last refusal goes."""
    e = await _engine(settings, clock)
    try:
        _pos(e, clock, pid="held")
        today = ist_today().isoformat()
        e._eod_alarmed = {("eod.still_open", "held", "2026-10-16"), ("eod.forced_fill", "gone", "2026-10-16"),
                          ("eod.not_forced", "held", today)}
        e._eod_alarms = [{"kind": "eod.forced_fill", "positionId": "gone", "day": "2026-10-16", "symbol": "TATASTEEL",
                          "book": "FUDKII_RT_X", "at": "15:19:00"}]
        e._exit_fail_note = {"held": "book 9000 ms old", "gone": "book 12000 ms old"}
        e._exit_failed_at = {"held": 1.0, "gone": 2.0}
        e._roll_day_state(ist_today())
        assert e._eod_alarms == [] and e._eod_alarmed == {("eod.not_forced", "held", today)}
        assert e._exit_fail_note == {"held": "book 9000 ms old"} and e._exit_failed_at == {"held": 1.0}
        assert e._eod_check().ok
    finally:
        await e.stop()


# -- review, 2026-10-17: the flatten's first try, a later day, a restart, a slow pass ----------------------


@pytest.mark.asyncio
async def test_a_backoff_from_before_1514_never_holds_the_flatten(settings, clock, nse_deadline):
    """A stop refused on a stale book every pass from 15:13:05 backs off 2-4-8-16-30 s, to 15:14:05. Before the
    flatten that backoff holds; the flatten's first try at 15:14:00 does not wait on it — the book is back, it sells."""
    clock[0] = _at(15, 13, 5)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        sl = ExitDecision(pos.id, ExitReason.SL_OP, 9.9, 2000, "stop", level=10.0, trigger_price=9.9, trigger_on="option mid")
        for s in range(5, 60):
            clock[0] = _at(15, 13, s)
            _quote(e, 9.95, 10.05, book_ts=clock[0] - 40, quote_ts=clock[0])  # the depth book 40 s old: refused
            e._exit_quote_ts[OPT.scrip_code] = clock[0]  # no REST refresh in the test
            await e._exit(pos, sl, clock[0])
        assert pos.status == "OPEN" and e._exit_retry_at[pos.id] == _at(15, 14, 5), "the 30 s backoff from the 15:13:35 try"
        refused = len([o for o in await _rows(e, "orders") if o.get("purpose") == "EXIT"])
        clock[0] = _at(15, 13, 59)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._exit(pos, sl, clock[0])
        assert pos.status == "OPEN" and len([o for o in await _rows(e, "orders") if o.get("purpose") == "EXIT"]) == refused, \
            "before the flatten the stop keeps its backoff"

        clock[0] = _at(15, 14)
        _quote(e, 9.95, 10.05, book_ts=clock[0])  # the book is back with a bid
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.exit_price == 9.95 and pos.closed_ts == _at(15, 14), \
            "15:14:00: sold at once, not at 15:14:05"
        assert pos.id not in e._exit_failed_at and pos.id not in e._exit_retry_at
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_backoff_from_a_refusal_after_1514_still_waits_two_seconds(settings, clock, nse_deadline):
    """The flatten's own refusals keep the 2 s wait: only a backoff from before 15:14 is passed over."""
    clock[0] = _at(15, 14)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        _quote(e, 9.95, 10.05, book_ts=clock[0] - 40, quote_ts=clock[0])
        await e._manage_positions()
        assert pos.status == "OPEN" and e._exit_failed_at[pos.id] == _at(15, 14) and e._exit_retry_at[pos.id] == _at(15, 14, 2)
        clock[0] = _at(15, 14, 1)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._manage_positions()
        assert pos.status == "OPEN", "15:14:01: inside the flatten's own 2 s wait"
        clock[0] = _at(15, 14, 2)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.closed_ts == _at(15, 14, 2)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_nse_position_still_open_on_a_later_day_alarms_again_and_the_line_is_red(settings, clock, nse_deadline):
    """SHADOW places nothing, so a position carried in stays open: it alarmed once on day 1 and was silent from then on,
    with the line green. The alarms are once a day: on day 2 it alarms at the first pass (opened the day before), and
    again at 15:20; the health line is red."""
    clock[0] = _at(15, 20)
    e = await _engine(settings, clock)
    try:
        await e.set_mode(Mode.SHADOW)
        pos = _pos(e, clock)
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._manage_positions()
        assert [r["day"] for r in await _events(e, "eod.still_open")] == [ist_today().isoformat()]
        clock[0] = _at(9, 30) + 86_400  # the next day, the market open, the position still held
        _quote(e, 9.95, 10.05, book_ts=clock[0])
        await e._manage_positions()
        await e._manage_positions()
        still = await _events(e, "eod.still_open")
        assert len(still) == 2 and still[-1]["at"] == "09:30:00" and "held overnight" in still[-1]["detail"], \
            "a position opened the day before is past the deadline from the first pass, once a day"
        assert {r["positionId"] for r in await _events(e, "eod.not_forced")} == {"p1"} and len(await _events(e, "eod.not_forced")) == 2
        chk = e._eod_check()
        assert not chk.ok and "still open" in chk.detail and pos.status == "OPEN"
        clock[0] = _at(15, 20) + 86_400
        await e._manage_positions()
        assert len(await _events(e, "eod.still_open")) == 2, "already alarmed today"
        assert not e._eod_check().ok
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_paper_position_from_an_earlier_day_is_forced_out_at_the_first_pass(settings, clock, nse_deadline):
    """The engine was down over the close and came back after midnight: yesterday's NSE position is filled on paper at
    its last known bid at once, flagged and alarmed — not managed as a normal position until 15:19."""
    clock[0] = _at(0, 45)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        pos.opened_ts = _at(14, 40) - 86_400  # bought yesterday
        _quote(e, 9.95, 10.05, book_ts=_at(15, 13) - 86_400)  # its last book, from yesterday's close
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.exit_reason == "EOD" and pos.exit_price == 9.95
        flag = pos.exec_log["eodForced"]
        assert flag["ist"] == "00:45:00" and flag["cause"].startswith("held from") and "past that day's 15:20" in flag["cause"]
        kinds = sorted(str(r["kind"]) for r in await _rows(e, "events") if str(r.get("kind", "")).startswith("eod."))
        assert kinds == ["eod.exit_after_deadline", "eod.forced_fill", "eod.still_open"]
        (late,) = await _events(e, "eod.exit_after_deadline")
        assert "held overnight" in late["detail"]
        assert not e._eod_check().ok and e.wallets["FUDKII_RT_X"].deployed == 0
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_out_by_line_stays_red_after_a_restart_the_same_day(settings, clock, nse_deadline):
    """A forced fill at 15:19 turns the line red for the day; a restart that evening (a deploy after the close, a crash)
    reads today's alarms back from the ledger, so the line stays red — and a position is not alarmed twice."""
    clock[0] = _at(15, 19)
    e = await _engine(settings, clock)
    try:
        pos = _pos(e, clock)
        e.ltps[OPT.scrip_code] = 9.7
        await e._manage_positions()
        assert pos.status == "CLOSED" and not e._eod_check().ok
    finally:
        await e.stop()

    clock[0] = _at(16, 5)
    again = await _engine(settings, clock)
    try:
        chk = again._eod_check()
        assert not chk.ok and "1 forced fill" in chk.detail and "TATASTEEL" in chk.detail and "15:19:00" in chk.detail, chk.detail
        assert ("eod.forced_fill", "p1", ist_today().isoformat()) in again._eod_alarmed
        await again._eod_alarm("eod.forced_fill", pos, clock[0], "again")
        assert len(await _events(again, "eod.forced_fill")) == 1, "already raised today, before the restart"
    finally:
        await again.stop()

    clock[0] = _at(9, 0) + 86_400  # the next day: yesterday's alarms are not today's
    nxt = await _engine(settings, clock)
    try:
        assert nxt._eod_check().ok and not nxt._eod_alarms
    finally:
        await nxt.stop()


@pytest.mark.asyncio
async def test_a_forced_fill_made_after_1520_is_stamped_when_made_and_alarms(settings, clock, nse_deadline):
    """Slow REST snapshots in the 15:19 pass (stale books with REST trouble are why the forced fill exists): each fill
    carries the clock when it was made, not the pass's start, and one made after 15:20:00 raises both late alarms."""
    clock[0] = _at(15, 19)
    e = await _engine(settings, clock)
    try:
        other = Instrument("153806", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, name="TATASTEEL 29 SEP 2026 CE 195.00",
                           lot_size=1000, tick_size=0.05, strike=195.0, underlying="TATASTEEL")
        a = _pos(e, clock, pid="a")
        b = _pos(e, clock, pid="b", inst=other, entry=4.0)
        e.ltps[OPT.scrip_code] = 9.7
        e.ltps[other.scrip_code] = 3.9

        async def slow_refresh(inst, now):  # a REST snapshot that takes 35 s of wall clock (many 3 s timeouts)
            clock[0] += 35

        e._refresh_exit_book = slow_refresh  # type: ignore[method-assign]
        await e._manage_positions()
        assert a.status == "CLOSED" and a.exec_log["eodForced"]["ist"] == "15:19:35" and a.closed_ts == _at(15, 19, 35)
        assert b.status == "CLOSED" and b.exec_log["eodForced"]["ist"] == "15:20:10" and b.closed_ts == _at(15, 20, 10)
        (trade_b,) = [t for t in await _rows(e, "trades") if t["position_id"] == "b"]
        assert trade_b["eod_forced"]["ist"] == "15:20:10"
        late = await _events(e, "eod.exit_after_deadline")
        still = await _events(e, "eod.still_open")
        assert [r["positionId"] for r in late] == ["b"] and late[0]["at"] == "15:20:10"
        assert [r["positionId"] for r in still] == ["b"] and still[0]["at"] == "15:20:10"
        assert not e._eod_check().ok
    finally:
        await e.stop()
