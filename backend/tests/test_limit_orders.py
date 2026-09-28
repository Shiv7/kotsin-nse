"""Paper limit orders, and every book entering a trigger on its own order (operator, 2026-09-26).

"put limit orders at entry price of that OTM and check if Bid-Ask has that number, if yes, wait, if
not then place order on the mid of bid-ask in favour of the trade" · "note the signal … limit-order
placed … and order executed time stamp" · "when the parent hits its 15% limit then the twins keep
trading"."""

from __future__ import annotations

import re
import time
from dataclasses import replace
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
    OptionType,
    OrderSide,
    Position,
    PosSide,
)
from kotsin_nse.engine import IN_TREND_BOOKS, Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.exec.resting import LimitPolicy, entry_limit, exit_limit, fills
from kotsin_nse.instrument.select import Quote, Selection, estimate_delta, map_levels_to_option
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.risk.exposure import ExposureVerdict
from kotsin_nse.risk.limits import (
    CT_X_LIMITS,
    CT_Y_LIMITS,
    RT_N_LIMITS,
    RT_X_LIMITS,
    RT_Y_LIMITS,
    RT_Y_W1_LIMITS,
    RiskLimits,
)
from kotsin_nse.risk.sizing import SizingResult
from kotsin_nse.risk.wallet import Wallet
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey

UND = Instrument("3499", "TATASTEEL", Segment.NSE_EQ, InstrumentKind.EQUITY, name="TATASTEEL", tick_size=0.01, underlying="TATASTEEL")
OPT = Instrument("153805", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, name="TATASTEEL 29 SEP 2026 CE 190.00",
                 lot_size=1000, tick_size=0.05, strike=190.0, option_type=OptionType.CE, underlying="TATASTEEL")


# -- the policy, pure ------------------------------------------------------------------------------


def test_an_entry_rests_at_the_signal_price_inside_the_book_else_at_the_mid():
    assert entry_limit(17.10, 16.95, 17.25) == (17.10, "at the signal price, inside the book")
    px, why = entry_limit(17.10, 17.30, 17.60)  # the market ran up past the signal price
    assert px == 17.45 and "mid" in why, "a buy never starts at the ask"
    assert entry_limit(17.10, 16.40, 16.60)[0] == 16.50, "ran down: the mid, not a limit above the ask"


def test_an_exit_starts_at_the_mid_and_walks_to_the_bid_by_its_deadline():
    assert exit_limit(10.0, 11.0, 0, 15) == 10.5
    assert exit_limit(10.0, 11.0, 7.5, 15) == 10.25
    assert exit_limit(10.0, 11.0, 15, 15) == 10.0
    pol = LimitPolicy()
    assert pol.exit_deadline(ExitReason.SL_EQ) == 15 and pol.exit_deadline(ExitReason.EOD) == 10
    assert pol.exit_deadline(ExitReason.TARGET) == 45, "a stop crosses sooner than a target"


def test_a_resting_limit_fills_when_the_touch_or_a_trade_comes_through_it():
    assert fills(True, 17.10, 16.95, 17.10, None), "the ask came down to the bid"
    assert fills(True, 17.10, 16.95, 17.25, 17.05), "a trade printed below the buy"
    assert not fills(True, 17.10, 16.95, 17.25, 17.10), "a print AT the limit is not through it"
    assert fills(False, 10.5, 10.5, 11.0, None) and fills(False, 10.5, 10.2, 11.0, 10.6)
    assert not fills(False, 10.5, 10.2, 11.0, 10.4)


# -- the engine ------------------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _book(e: Engine, bid: float, ask: float, now: float, ltp: float | None = None) -> None:
    e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=[(bid, 50_000)], asks=[(ask, 50_000)], ts=now)
    e.quotes[OPT.scrip_code] = Quote(ltp=ltp or (bid + ask) / 2, bid=bid, ask=ask, ts=now)
    e.ltps[OPT.scrip_code] = ltp or (bid + ask) / 2


async def _engine(settings, clock, *, ref: float = 17.10, limit_orders: bool = True) -> Engine:
    e = Engine(settings.model_copy(update={"paper_limit_orders": limit_orders}))
    await e.start()
    await e.set_mode(Mode.PAPER)
    e.underlyings["TATASTEEL"] = UND

    async def select(underlying, sig, *, tape=True):
        return Selection(OPT, premium=ref, reason="ok", spread_pct=1.0)

    e._select_instrument = select  # type: ignore[method-assign]
    return e


def _sig(clock) -> Signal:
    ts = int(datetime.combine(ist_today(), dtime(10, 15), tzinfo=IST).timestamp())
    return Signal(strategy=StrategyKey.FUDKII, symbol="TATASTEEL", direction=Direction.BULLISH, ts=ts, entry=187.0,
                  stop=185.0, targets=(190.0, 192.0), grade="A", rr=1.5, reason="ST flip UP + close above upper band")


async def _rows(e: Engine, table: str) -> list[dict]:
    return await e.ledger.rows_between(table, 0, time.time() + 86_400)


@pytest.mark.asyncio
async def test_every_in_trend_book_rests_and_fills_its_own_entry_with_every_timestamp(settings, clock):
    """Operator, 2026-09-26: "a fudkii signal goes to all variants at the same time at once. each
    variant and twin assess it at the same time in parallel" — each book its own order, none a copy."""
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        sig = _sig(clock)
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        assert not e.positions
        resting = {r.intent.strategy: r for r in e._resting.values() if r.kind == "entry"}
        assert set(resting) == {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}, "one working order per book"
        assert all(r.limit == 17.10 and r.why.startswith("at the signal price") for r in resting.values())
        # operator, 2026-09-26: "all strategy names have its code in the order ID"
        ids = {b: r.intent.client_order_id for b, r in resting.items()}
        assert re.fullmatch(r"FII-P-\d{6}-110000-\d{3}-EN-TATASTEEL-190CE-L\d+", ids["FUDKII"]), ids["FUDKII"]
        assert re.fullmatch(r"FII-RTX-\d{6}-110000-\d{3}-EN-TATASTEEL-190CE-L\d+", ids["FUDKII_RT_X"])
        assert len({i[:38] for i in ids.values()}) == 4, "unique within the 38 characters the broker keeps"
        assert (await _rows(e, "signals"))[0]["decision"] == "RESTING"
        cards = (await e.book_cards("FUDKII"))["cards"]
        assert cards[0]["state"] == "PENDING" and cards[0]["pending"]["limit"] == 17.10 and cards[0]["cta"]["enabled"] is False
        rt = (await e.book_cards("FUDKII_RT_X"))["cards"][0]
        assert rt["state"] == "PENDING" and rt["pending"]["book"] == "FUDKII_RT_X", "RT-X waits on its OWN order"
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)  # the same trigger again: never a second order
        assert len(e._resting) == 4
        with pytest.raises(RuntimeError, match="resting"):
            await e.operator_take("FUDKII_RT_X", sig.signal_id)

        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])  # the ask comes down to our bid
        await e._manage_positions()
        by = {p.strategy: p for p in e.positions.values()}
        assert {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_RT_Y_W1"} == set(by), "four entries and RT-Y's shadow"
        parent = by["FUDKII"]
        assert all(p.entry == 17.10 and p.signal_id == sig.signal_id for p in by.values())
        assert not [r for r in e._resting.values() if r.kind == "entry"]
        t1 = e._target_resting(parent.id)
        assert t1 is not None and 0 <= t1.limit - parent.option_targets[0] < 0.05, "its T1 sell rests from the fill on, on the tick"
        a = parent.exec_log["entry"]
        assert a["placedTs"] == pytest.approx(clock[0] - 22) and a["filledTs"] == clock[0] and a["waitS"] == pytest.approx(22)
        assert a["signalTs"] <= a["placedTs"] and a["bookAtPlace"] == {"bid": 16.95, "ask": 17.25} and a["bookAtFill"]["ask"] == 17.10
        for book in ("FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"):
            assert by[book].exec_log["entry"]["filledTs"] == clock[0] and by[book].exec_log is not parent.exec_log
            assert "twin of" not in by[book].note, "its own entry, not a copy of the parent's"
        sigrow = (await _rows(e, "signals"))[0]
        assert sigrow["decision"] == "PAPER_FILLED", "the RESTING row is settled, not left behind"
        entries = [o for o in await _rows(e, "orders") if o["purpose"] == "ENTRY"]
        assert {o["strategy"] for o in entries} == {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}
        assert all(o["status"] == "FILLED" and o["exec"]["fillPrice"] == 17.10 for o in entries)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_signal_price_that_left_the_book_rests_at_the_mid_and_a_print_through_fills_it(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 17.30, 17.60, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        assert r.limit == 17.45 and "mid" in r.why
        clock[0] += 3
        _book(e, 17.30, 17.60, clock[0], ltp=17.40)  # a trade printed below our bid
        await e._manage_positions()
        assert next(p for p in e.positions.values() if p.strategy == "FUDKII").entry == 17.45
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_unfilled_entry_holds_30_s_follows_the_mid_under_the_cap_and_is_missed_at_60_s(settings, clock):
    """"yes after 30s wait" (the limit moves to the mid only after 30 s) and "no more than 3% above the
    signal price of the option" — ref 17.10, so never above 17.60."""
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        w = e.wallets["FUDKII"]
        before = w.available
        await e._handle_signal(_sig(clock), None)
        assert w.available < before, "the outlay is held while the order rests"
        r = next(iter(e._resting.values()))
        t0 = clock[0]
        for dt in (5, 10, 20):  # the market drifts up past the signal price inside the hold: the limit stays
            clock[0] = t0 + dt
            _book(e, 17.30, 17.70, clock[0])
            await e._manage_positions()
        assert r.limit == 17.10 and not r.reprices, "no move in the first 30 s"
        clock[0] = t0 + 32
        _book(e, 17.30, 17.70, clock[0])
        await e._manage_positions()
        assert r.limit == 17.50 and r.reprices, "after the hold it follows the mid (17.50, under the cap)"
        assert not r.momentum, "no race: the switch is off by default"
        clock[0] = t0 + 45
        _book(e, 17.60, 18.00, clock[0])  # the mid 17.80 is over the cap: the limit sits AT the cap
        await e._manage_positions()
        assert r.limit == 17.60
        clock[0] = t0 + 61
        _book(e, 17.70, 18.10, clock[0])
        await e._manage_positions()
        assert not e.positions and not e._resting
        assert w.available == pytest.approx(before), "a missed entry holds nothing"
        s = (await _rows(e, "signals"))[0]
        assert s["decision"] == "LIMIT_UNFILLED" and "not filled in 60 s" in s["decision_reason"] and "the option +" in s["decision_reason"]
        ev = [x for x in await _rows(e, "events") if x.get("kind") == "limit.missed"]
        assert ev and ev[0]["why"].startswith("limit not filled")
        order = (await _rows(e, "orders"))[0]
        assert order["status"] == "CANCELLED" and order["exec"]["reprices"][-1][1] == 17.60
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_racing_option_is_bought_at_30_s_within_the_cap(settings, clock):
    """The race is a switch, off by default (it lost money in the replay); switched on, it works so."""
    e = await _engine(settings, clock)
    e.limit_policy = replace(e.limit_policy, entry_race_pct=1.0)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        t0 = clock[0]
        clock[0] = t0 + 31
        _book(e, 17.30, 17.50, clock[0])  # mid 17.40 = +1.75% ≥ 1%; ask 17.50 ≤ cap 17.60
        await e._manage_positions()
        p = next(p for p in e.positions.values() if p.strategy == "FUDKII")
        assert p.entry == 17.50 and "racing" in p.exec_log["entry"]["outcome"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_entry_is_placed_at_the_cap_when_the_option_has_already_run_past_it(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 18.00, 18.40, clock[0])  # mid 18.20: +6.4% before the order is even placed
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        assert r.limit == 17.60 and "capped" in r.why
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_book_that_halts_while_its_entry_rests_is_cancelled_not_filled(settings, clock):
    """Audit, 2026-09-26: a wallet halted while its limit rested still bought (the halt was read only
    when the order was placed)."""
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        _halt(e.wallets["FUDKII"], drawdown="DRAWDOWN 15.40%")
        clock[0] += 9
        _book(e, 16.95, 17.10, clock[0])  # the fill would come now
        await e._manage_positions()
        assert "FUDKII" not in {p.strategy for p in e.positions.values()}, "the halted book buys nothing"
        assert {"FUDKII_RT_X", "FUDKII_RT_N"} <= {p.strategy for p in e.positions.values()}, "the others fill"
        s = (await _rows(e, "signals"))[0]
        assert s["decision"] == "LIMIT_UNFILLED" and "FUDKII halted — DRAWDOWN 15.40%" in s["decision_reason"]
        assert e.wallets["FUDKII"].deployed == 0.0
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_no_book_enters_a_trigger_whose_stop_the_underlying_is_already_through(settings, clock):
    """SBILIFE, 2026-09-24 09:45: the stop 10 paise under the close; the stock printed through it and
    every book was stopped out 67 ms after the fill."""
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        e.ltps[UND.scrip_code] = 184.90  # the stop is 185.00: already through
        await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        assert not e._resting and not e.positions
        s = (await _rows(e, "signals"))[0]
        assert s["decision"] == "STOP_BREACHED" and "184.9 through its stop 185" in s["decision_reason"]
        skips = {x["book"]: x for x in await _rows(e, "events") if x.get("kind") == "rt_twin.skipped"}
        assert set(skips) == {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"} and all(x["gate"] == "stop_breached" for x in skips.values())
        # and a resting entry is cancelled the moment the stock goes through the stop
        e.ltps[UND.scrip_code] = 187.0
        sig2 = replace(_sig(clock), ts=_sig(clock).ts + 1800)  # the next bar's trigger
        await e._handle_signal(sig2, None)
        assert e._resting
        clock[0] += 3
        e.ltps[UND.scrip_code] = 184.95
        await e._manage_positions()
        assert not e._resting and not e.positions
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_exit_rests_at_the_mid_walks_to_the_bid_and_crosses_at_its_deadline(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = Position(id="p1", strategy="FUDKII_RT_X", instrument=OPT, underlying=UND, side=PosSide.LONG, qty=2750, entry=17.0,
                       opened_ts=clock[0] - 600, signal_id="s1", direction=Direction.BULLISH, equity_entry=187.0, equity_sl=185.0,
                       option_sl=15.0)
        e.positions[pos.id] = pos
        e.wallets["FUDKII_RT_X"].commit(17.0 * 2750, clock[0])
        _book(e, 10.0, 11.0, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_EQ, 10.5, 2750, "SL-EQ"), clock[0])
        r = e._exit_resting(pos.id)
        assert r is not None and r.limit == 10.5 and r.deadline_s == 15
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_EQ, 10.5, 2750, "SL-EQ"), clock[0])  # the loop decides again
        assert len(e._resting) == 1, "one exit in flight — the resting one, repriced, never a second SELL"
        t0 = clock[0]
        clock[0] = t0 + 6
        _book(e, 10.0, 11.0, clock[0])
        await e._manage_positions()
        assert e._exit_resting(pos.id).limit < 10.5, "walked toward the bid"
        clock[0] = t0 + 16
        _book(e, 10.0, 11.0, clock[0])
        await e._manage_positions()
        assert pos.status == "CLOSED" and pos.exit_price == 10.0, "crossed at the bid at 15 s"
        x = pos.exec_log["exits"][-1]
        assert x["crossedTs"] == clock[0] and x["reason"] == "SL-EQ" and "crossed" in x["outcome"] and x["reprices"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_resting_target_exit_is_replaced_by_a_stop(settings, clock):
    e = await _engine(settings, clock)
    try:
        pos = Position(id="p2", strategy="FUDKII_RT_X", instrument=OPT, underlying=UND, side=PosSide.LONG, qty=5500, entry=17.0,
                       opened_ts=clock[0] - 600, signal_id="s2", direction=Direction.BULLISH, option_sl=15.0)
        e.positions[pos.id] = pos
        _book(e, 18.0, 19.0, clock[0])
        await e._exit(pos, ExitDecision(pos.id, ExitReason.TARGET, 18.5, 2750, "T1"), clock[0])
        assert e._exit_resting(pos.id).deadline_s == 45
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_OP, 18.5, 5500, "hard floor"), clock[0])
        r = e._exit_resting(pos.id)
        assert r.deadline_s == 15 and r.intent.qty == 5500 and len(e._resting) == 1
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_limit_orders_off_and_live_modes_keep_the_immediate_walk(settings, clock):
    e = await _engine(settings, clock, limit_orders=False)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        assert not e._resting and next(p for p in e.positions.values() if p.strategy == "FUDKII").entry == 17.25, "walked to the ask"
        assert next(p for p in e.positions.values() if p.strategy == "FUDKII").exec_log["entry"]["why"].startswith("market")
    finally:
        await e.stop()
    e2 = await _engine(settings, clock)
    try:
        e2.mode = lambda: Mode.SHADOW  # type: ignore[method-assign]
        assert e2._limit_mode() is False, "SHADOW places nothing, rests nothing"
        # a LIVE session's PAPER books (the RT-Y wide shadow, FUKAA) keep the paper limit rules — a live
        # book's orders go to the broker by their venue, never through this (tests/test_live_orders.py)
        e2.mode = lambda: Mode.LIVE  # type: ignore[method-assign]
        assert e2._limit_mode() is True
    finally:
        await e2.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("ask_at_deadline", "filled"), [(17.60, True), (18.50, False)])  # ref 17.10: +2.9% inside a 3% chase, +8.2% outside
async def test_an_unfilled_entry_takes_the_ask_at_the_deadline_only_within_the_chase(settings, clock, ask_at_deadline, filled):
    """The misses were the fast winners; a chase buys the ask at the deadline if it is still close."""
    e = await _engine(settings, clock)
    e.limit_policy = replace(e.limit_policy, entry_chase_pct=3.0)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        assert e._resting
        clock[0] += 61
        _book(e, ask_at_deadline - 0.15, ask_at_deadline, clock[0])
        await e._manage_positions()
        parent = [p for p in e.positions.values() if p.strategy == "FUDKII"]
        assert bool(parent) is filled
        if filled:
            assert parent[0].entry == ask_at_deadline and "crossed at the ask" in parent[0].exec_log["entry"]["outcome"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_parent_buys_four_lots_at_most_and_keeps_its_own_targets(settings, clock):
    """Operator, 2026-09-26: "how can we ever buy 30 lots of the same trade?" (uncapped it bought up to
    83 in the Sep replay) and "the parents' targets come from that parent's own logic and strategy and
    not borrowed or adopted from its variants or twins"."""
    e = await _engine(settings, clock, limit_orders=False)
    try:
        assert e.limits.max_lots == 4 and not e.limits.targets_from_own_ladder
        # the option's own ladder (LegPivotLoader, stubbed) is the TWINS' — never the parent's
        e._own_ladder_for = lambda symbol, inst, entry, lim: ((19.5, 22.0, 24.0), 0.2, "own mtf ladder (test)")  # type: ignore[method-assign]
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        parent = next(p for p in e.positions.values() if p.strategy == "FUDKII")
        assert parent.qty <= 4 * parent.instrument.lot_size
        d = estimate_delta(spot=187.0, strike=OPT.strike, option_type=OPT.option_type)
        _, projected = map_levels_to_option(equity_entry=187.0, equity_stop=185.0, equity_targets=(190.0, 192.0), option_premium=17.10, delta=d)
        assert parent.option_targets == projected, "the parent's own equity targets, projected through delta"
        assert "own mtf ladder" not in parent.note
        rtx = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X")
        assert rtx.option_targets == (19.5, 22.0, 24.0), "a twin keeps its own ladder"
    finally:
        await e.stop()


# -- every wallet its own halts (operator, 2026-09-26) ---------------------------------------------


def test_every_wallet_halts_at_ten_percent_down_on_the_day_and_that_halt_clears_each_morning():
    """"we agree on the 10% daily-loss halt for each separate wallet, which resets every morning. The
    15% drawdown halt currently stays on day after day until someone resets that particular wallet"."""
    lim = RiskLimits()
    assert lim.daily_loss_limit_pct == 10.0 and lim.max_drawdown_pct == 15.0
    assert all(x.daily_loss_limit_pct == 10.0 and x.max_drawdown_pct == 15.0
               for x in (RT_X_LIMITS, RT_N_LIMITS, RT_Y_LIMITS, CT_X_LIMITS, CT_Y_LIMITS, RT_Y_W1_LIMITS))
    t0 = datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()
    w = Wallet.new("FUDKII_RT_Y", 1_000_000, t0)
    w.apply_close(-95_000, t0)
    assert w.check_breakers(lim, t0) is None, "−9.5% on the day trades on (it was 3%)"
    w.apply_close(-10_000, t0)
    assert w.check_breakers(lim, t0).startswith("DAILY_LOSS")
    assert w.rollover(t0 + 86_400) and not w.halted, "the daily halt clears the next morning"
    w.apply_close(-60_000, t0 + 86_400)  # 16.5% below the peak, 6.7% on the day
    assert w.check_breakers(lim, t0 + 86_400).startswith("DRAWDOWN")
    w.rollover(t0 + 2 * 86_400)
    assert w.halted, "the drawdown halt stays until someone resets that wallet"


@pytest.mark.asyncio
async def test_each_wallet_is_checked_against_its_own_books_limits(settings, clock):
    """The breaker read the PARENT's limits for every wallet; each book now answers to its own."""
    e = await _engine(settings, clock, limit_orders=False)
    try:
        assert e.limits_for("FUDKII") is e.limits and e.limits_for("FUKAA") is e.limits
        assert e.limits_for("FUDKII_RT_Y") is e._exits_by_strategy["FUDKII_RT_Y"].limits
        # RT-Y alone on a tighter daily limit (4 %): its wallet halts; RT-X's, the same loss, does not
        e._exits_by_strategy["FUDKII_RT_Y"].limits = replace(RT_Y_LIMITS, daily_loss_limit_pct=4.0)
        _book(e, 10.0, 11.0, clock[0])
        for book in ("FUDKII_RT_Y", "FUDKII_RT_X"):
            w = e.wallets[book]
            w.day_start_balance = w.balance / 0.97  # already 3 % down on the day
            pos = Position(id=f"p-{book}", strategy=book, instrument=OPT, underlying=UND, side=PosSide.LONG, qty=2750, entry=17.0,
                           opened_ts=clock[0] - 600, signal_id=f"s-{book}", direction=Direction.BULLISH, equity_entry=187.0,
                           equity_sl=185.0, option_sl=15.0)
            e.positions[pos.id] = pos
            w.commit(17.0 * 2750, clock[0])
            await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_EQ, 10.5, 2750, "SL-EQ"), clock[0])
            assert pos.status == "CLOSED"
        assert e.wallets["FUDKII_RT_Y"].halted and e.wallets["FUDKII_RT_Y"].halt_reason.startswith("DAILY_LOSS")
        assert not e.wallets["FUDKII_RT_X"].halted, "about −5 % on the day is inside RT-X's own 10 %"
    finally:
        await e.stop()


# -- the parent's own reasons never stop the twins (operator, 2026-09-26) --------------------------


def _halt(w: Wallet, *, drawdown: str = "", daily: str = "") -> None:
    w.drawdown_halt, w.daily_halt = drawdown, daily
    w._sync_halt()


def _skip_exposure(e: Engine, monkeypatch) -> None:
    monkeypatch.setattr(e.exposure, "check", lambda **kw: ExposureVerdict(False, "FUDKII holds 30 positions"))


def _skip_wallet(e: Engine, monkeypatch) -> None:
    real = Wallet.reserve
    monkeypatch.setattr(Wallet, "reserve", lambda self, amount, now: False if self.strategy == "FUDKII" else real(self, amount, now))


def _skip_sized_out(e: Engine, monkeypatch) -> None:
    w = e.wallets["FUDKII"]
    w.deployed = w.balance - 20_000  # ₹20,000 left: below one lot at ₹47,025


def _skip_costs(e: Engine, monkeypatch) -> None:
    """The parent's own costs-against-T1 rule — the twins never had it (operator: "the twins decide for themselves")."""
    import kotsin_nse.engine as eng

    real = eng.size_position

    def sized(**kw):
        if kw["option_target1"] is not None:  # only the δ-projected parent passes a T1
            return SizingResult(0, 0, 0.0, 0.0, 0.6, "costs are 60% of the move to T1", declined="cost")
        return real(**kw)

    monkeypatch.setattr(eng, "size_position", sized)


def _skip_drawdown(e: Engine, monkeypatch) -> None:
    _halt(e.wallets["FUDKII"], drawdown="DRAWDOWN 15.20%")


def _skip_daily(e: Engine, monkeypatch) -> None:
    _halt(e.wallets["FUDKII"], daily="DAILY_LOSS -10.10%")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_orders", [True, False])
@pytest.mark.parametrize(("skip", "decision"), [
    (_skip_drawdown, "WALLET_HALTED"), (_skip_daily, "WALLET_HALTED"), (_skip_sized_out, "NOT_SIZED"),
    (_skip_exposure, "EXPOSURE"), (_skip_wallet, "WALLET"), (_skip_costs, "NOT_SIZED"),
])
async def test_a_parent_that_does_not_trade_for_its_own_reasons_costs_the_twins_nothing(settings, clock, monkeypatch, skip, decision, limit_orders):
    """"let the fudkii wallet pause only for execution of trades not triggers for trades (which get
    consumed by all variants and twins)" · "the twins decide for themselves"."""
    e = await _engine(settings, clock, limit_orders=limit_orders)
    try:
        skip(e, monkeypatch)
        w = e.wallets["FUDKII"]
        before = w.to_json()
        _book(e, 16.95, 17.25, clock[0])
        sig = _sig(clock)
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        s = (await _rows(e, "signals"))[0]
        assert s["decision"] == decision, "the parent's row is the parent's own decision"
        if limit_orders:
            assert "FUDKII" not in {r.intent.strategy for r in e._resting.values()}
            clock[0] += 9
            _book(e, 16.95, 17.10, clock[0])
            await e._manage_positions()
        assert not [p for p in e.positions.values() if p.strategy == "FUDKII"], "the parent books nothing"
        assert {"FUDKII_RT_X", "FUDKII_RT_N"} <= {p.strategy for p in e.positions.values()}
        assert all(p.signal_id == sig.signal_id for p in e.positions.values())
        after = w.to_json()
        assert (after["balance"], after["charges_paid"]) == (before["balance"], before["charges_paid"])
        assert not [o for o in await _rows(e, "orders") if o["strategy"] == "FUDKII"], "no parent order: nothing of the parent's traded"
        assert (await e.book_cards("FUDKII"))["cards"][0]["state"] == decision
        assert (await e.book_cards("FUDKII_RT_X"))["cards"][0]["state"] == "OPEN"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_trigger_the_contract_rules_out_enters_nobody(settings, clock):
    """What belongs to the trigger or its contract stops every book: here no contract."""
    e = await _engine(settings, clock, limit_orders=False)
    try:
        from kotsin_nse.instrument.select import Selection as Sel

        async def none(underlying, sig, *, tape=True):
            return Sel(None, premium=0.0, reason="no strike within the spread cap")

        e._select_instrument = none  # type: ignore[method-assign]
        await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        assert not e.positions and not e._resting
        assert (await _rows(e, "signals"))[0]["decision"] == "NO_INSTRUMENT"
        assert (await e.book_cards("FUDKII_RT_X"))["cards"][0]["state"] == "NO_FILL"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_each_book_sizes_from_its_own_purse_and_places_its_own_order(settings, clock):
    """"the 4 lots cap is for each strategy individually" · "even if the parent filled 1 lot due to
    lack of funds, twin or a variant strategy can still go ahead and fill 4 lots since that respective
    wallet might have funds" — on its OWN order, so its fill walks the book for its own size."""
    e = await _engine(settings, clock, limit_orders=False)
    try:
        w = e.wallets["FUDKII"]
        w.deployed = w.balance - 60_000  # the parent's purse holds ₹60,000; four lots cost ₹68,400
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        # 4 lots or nothing (operator, 2026-09-27: "take 4 lots min as long as it is less than 75,000")
        assert not any(p.strategy == "FUDKII" for p in e.positions.values()), "the parent cannot pay for 4 lots: no trade"
        row = next(s for s in await _rows(e, "signals") if s["strategy"] == "FUDKII")
        assert row["decision"] == "NOT_SIZED" and "4 lots cost ₹68,400 > ₹60,000 left in the purse" in row["decision_reason"]
        orders = {o["strategy"]: o for o in await _rows(e, "orders") if o["purpose"] == "ENTRY"}
        for book in ("FUDKII_RT_X", "FUDKII_RT_N"):
            twin = next(p for p in e.positions.values() if p.strategy == book)
            assert twin.qty == 4 * OPT.lot_size, f"{book} buys its own 4 lots from its own purse"
            code = {"FUDKII_RT_X": "RTX", "FUDKII_RT_N": "RTN"}[book]
            assert orders[book]["qty"] == twin.qty
            assert re.fullmatch(rf"FII-{code}-\d{{6}}-\d{{6}}-\d{{3}}-EN-TATASTEEL-190CE-L4", orders[book]["client_order_id"])
            assert twin.entry_charges == pytest.approx(e.costs.leg(OPT, OrderSide.BUY, twin.entry, twin.qty).total, abs=0.01)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_live_session_sends_every_fudkii_books_own_order_to_the_broker(settings, clock):
    """Operator, 2026-09-27: "all fudkii strategies are live … each must have its dedicated orderbook"
    — in a LIVE mode each FUDKII book sends its OWN real order (its own id, its own fill); the RT-Y
    wide shadow opens on RT-Y's fill and stays paper: its exit never reaches the broker."""
    from .fake_broker import FakeBroker

    e = await _engine(settings, clock, limit_orders=False)
    try:
        fake = FakeBroker()
        e.live_orders.rest = fake
        e.rest.margin = fake.margin  # type: ignore[method-assign]
        await e.set_mode(Mode.LIVE, armed_minutes=60)
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        placed = [c[1] for c in fake.calls if c[0] == "place"]
        assert sorted(cid.split("-")[1] for cid in placed) == ["P", "RTN", "RTX", "RTY"], "one real order per book"
        for r in list(e._resting.values()):
            fake.fill(r.intent.client_order_id, r.intent.qty, 17.10)
        clock[0] += 1
        await e._advance_resting(clock[0])
        live = {p.strategy for p in e.positions.values() if p.venue == "live"}
        assert live == {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}
        shadow = [p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y_W1"]
        assert shadow and all(p.venue == "paper" for p in shadow), "the wide shadow is paper-only"
        n = len(fake.calls)
        w1 = shadow[0]
        await e._exit(w1, ExitDecision(w1.id, ExitReason.SL_EQ, 16.0, w1.qty, "SL-EQ"), clock[0])
        assert w1.status == "CLOSED" and not [c for c in fake.calls[n:] if c[0] == "place"], "never at the venue"
        rtx = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X")
        await e._exit(rtx, ExitDecision(rtx.id, ExitReason.SL_EQ, 16.0, rtx.qty, "SL-EQ"), clock[0])
        assert [c[2] for c in fake.calls[n:] if c[0] == "place"] == ["SELL"], "a live book's exit goes to the broker"
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("row", "why"), [
    ({"NetAvailableMargin": 20_000.0}, "< this entry's"),        # short of the entry's ₹47,025
    ({"SomethingElse": 1e9}, "no margin field"),                  # a field we do not recognise: fail closed
    (RuntimeError("V4/Margin down"), "broker funds unknown"),     # no answer: fail closed
])
async def test_a_live_entry_the_broker_account_cannot_pay_for_is_refused_and_not_counted(settings, clock, row, why):
    """Every live book draws on the ONE broker account: a paper purse proves nothing about it."""
    e = await _engine(settings, clock, limit_orders=False)
    try:
        sent = []

        async def live(intent, *, ctx):
            sent.append(intent.strategy)
            return e.gateway.submit_paper(intent)

        async def margin():
            if isinstance(row, Exception):
                raise row
            return row

        e.gateway.submit_live = live  # type: ignore[method-assign]
        e.rest.margin = margin  # type: ignore[method-assign]
        e.mode = lambda: Mode.LIVE  # type: ignore[method-assign]
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        assert sent == [] and not e.positions
        s = (await _rows(e, "signals"))[0]
        assert s["decision"] == "REJECTED_CAP" and why in s["decision_reason"]
        assert e.gateway.rejects_by_book.get("FUDKII", 0) == 0, "a refusal for money is not a gateway failure"
        assert e.wallets["FUDKII"].deployed == 0.0, "the hold is released"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_broker_reconcile_sums_the_live_books_per_contract_and_ignores_paper_books(settings):
    """Audit, 2026-09-26: every paper book's position was sent to the venue reconcile as if it were at
    the broker — a PHANTOM that froze entries within 60 s. And two live books on one strike are ONE
    net position at the broker."""
    from kotsin_nse.exec.reconcile import Reconciler

    e = Engine(settings)
    try:
        now = time.time()
        for book, qty in (("FUDKII", 2750), ("FUDKII_RT_X", 11000)):
            e.positions[f"p-{book}"] = Position(id=f"p-{book}", strategy=book, instrument=OPT, underlying=UND, side=PosSide.LONG,
                                                qty=qty, entry=17.0, opened_ts=now, signal_id="s", direction=Direction.BULLISH)
        e.positions["p-FUDKII"].venue = "live"  # bought at the broker; RT-X's lots are paper
        assert [p.strategy for p in e._venue_positions()] == ["FUDKII"], "only the LIVE-venue lots are at the broker"

        class Rest:
            def __init__(self, rows):
                self.rows = rows

            async def net_positions(self):
                return self.rows

        r = Reconciler(Rest([{"scrip_code": OPT.scrip_code, "net_qty": 2750, "symbol": "TATASTEEL"}]))
        rep = await r.run(e._venue_positions(), at_venue=True)
        assert not rep.mismatches and not r.frozen, "the paper RT-X position is no PHANTOM"
        e.positions["p-FUDKII_RT_X"].venue = "live"  # every FUDKII book trades live
        r2 = Reconciler(Rest([{"scrip_code": OPT.scrip_code, "net_qty": 13750, "symbol": "TATASTEEL"}]))
        rep2 = await r2.run(e._venue_positions(), at_venue=True)
        assert not rep2.mismatches, "2,750 + 11,000 on one strike = the broker's 13,750"
    finally:
        await e.stop()


# -- order ids, TAKEs, holds (operator and audit, 2026-09-26: "the idea is to ensure that we stop making errors now") --


@pytest.mark.asyncio
async def test_every_order_of_a_position_carries_its_book_and_entry_and_is_unique_within_38_characters(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        clock[0] += 5
        _book(e, 16.95, 17.10, clock[0])
        await e._manage_positions()
        rtx = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X")
        ref = rtx.exec_log["ref"]
        assert re.fullmatch(r"FII-RTX-\d{6}-110000-\d{3}", ref)
        parent = next(p for p in e.positions.values() if p.strategy == "FUDKII")
        tgt = e._target_resting(parent.id)  # the parent's T1 (RT-X has no own ladder in this offline engine)
        assert tgt.intent.client_order_id.startswith(f"{parent.exec_log['ref']}-T1V1-")
        from kotsin_nse.engine import exit_client_order_id

        d = ExitDecision(rtx.id, ExitReason.SL_EQ, 16.0, rtx.qty, "SL-EQ")
        a0, a1, x1 = exit_client_order_id(rtx, d), exit_client_order_id(rtx, d, 1), exit_client_order_id(rtx, d, 1, cross=True)
        assert a0 == exit_client_order_id(rtx, d), "a retry of the same exit is the same order"
        assert a0.startswith(f"{ref}-SLE0-") and a1.startswith(f"{ref}-SLE0R1-") and x1.startswith(f"{ref}-SLE0R1X-")
        every = [o["client_order_id"] for o in await _rows(e, "orders")] + [tgt.intent.client_order_id, a0, a1, x1]
        assert len({i[:38] for i in every}) == len(set(every)), "no two orders share the broker's 38 characters"
        assert all(re.fullmatch(r"[A-Z0-9-]+", i) for i in every), "letters, digits and hyphens only"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_order_numbers_survive_a_restart(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.10, clock[0])
        await e._handle_signal(_sig(clock), None)  # marketable: filled and written at once
        first = next(o["client_order_id"] for o in await _rows(e, "orders"))
        e._order_seq.clear()  # what a restart forgets …
        await e._load_order_seq()  # … and reads back from the ledger
        assert e._order_seq[("FUDKII", first.split("-")[2])] == 1
        assert e._order_ref("FUDKII", clock[0]).endswith("-002"), "numbering carries on"
        # and a number reused (an order resting at a restart is not written yet) is still a new id:
        # a restart takes minutes, and the time is part of the id
        e._order_seq.clear()
        assert e._order_ref("FUDKII", clock[0] + 150) != first[:23]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_take_is_never_dropped_in_silence_nor_refused_as_a_duplicate(settings, clock):
    """Audit, 2026-09-26: a FUDKII TAKE kept the trigger's signal id; while the RT books' entries
    rested on it, the signal-wide guard dropped the take with no row and no reason. And a take after
    the book's own missed entry reused that order's id and was refused as a duplicate."""
    e = await _engine(settings, clock)
    try:
        sig = _sig(clock)
        e._signals_today[sig.signal_id] = sig
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        # FUDKII's own entry misses; the RT books' entries are still resting on the same trigger
        fud = next(r for r in e._resting.values() if r.intent.strategy == "FUDKII")
        await e._entry_missed(fud, clock[0], 16.95, 17.25, "limit not filled in 60 s")
        assert {r.intent.strategy for r in e._resting.values()} == {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}
        out = await e.operator_take("FUDKII", sig.signal_id)
        assert out["decision"] == "RESTING" and out["reason"].startswith("limit 17.1"), out
        take = next(r for r in e._resting.values() if r.intent.strategy == "FUDKII")
        assert "-TK-" in take.intent.client_order_id and take.intent.client_order_id != fud.intent.client_order_id
        with pytest.raises(RuntimeError, match="resting"):
            await e.operator_take("FUDKII", sig.signal_id)  # its own entry is working: refused, with the reason
        # the same trigger re-run without a take: never a second order for anyone
        before = len(e._resting)
        again = await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        assert len(e._resting) == before and all(v["decision"] == "ALREADY_HANDLED" for v in again.values())
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_error_after_the_hold_releases_it_and_a_hold_at_the_broker_is_counted(settings, clock, monkeypatch):
    e = await _engine(settings, clock)
    try:
        w = e.wallets["FUDKII"]
        _book(e, 16.95, 17.25, clock[0])

        def boom(intent):
            raise RuntimeError("gateway fell over")

        monkeypatch.setattr(e.gateway, "place_limit", boom)
        out = await e._handle_signal(_sig(clock), None)
        assert out["FUDKII"]["decision"] == "ERROR" and w.deployed == 0.0, "the hold went back to the purse"
    finally:
        await e.stop()
    e2 = await _engine(settings, clock, limit_orders=False)
    try:
        w2 = e2.wallets["FUDKII"]
        seen = {}
        real = e2._submit

        async def slow_submit(intent, **kw):
            # while the order is "at the broker": the money check must see the hold
            seen["expected"] = e2._deployed_expected("FUDKII", w2)
            seen["deployed"] = w2.deployed
            return await real(intent, **kw)

        monkeypatch.setattr(e2, "_submit", slow_submit)
        _book(e2, 16.95, 17.25, clock[0])
        await e2._handle_signal(_sig(clock), None)
        assert seen["expected"] == pytest.approx(seen["deployed"]) and seen["deployed"] > 0, "an in-flight hold is not a drift"
        assert not e2._inflight_holds
    finally:
        await e2.stop()


@pytest.mark.asyncio
async def test_an_engine_halt_refuses_every_book_before_any_order_and_counts_nothing(settings, clock):
    e = await _engine(settings, clock)
    try:
        await e.set_halt(True, "operator")
        _book(e, 16.95, 17.25, clock[0])
        out = await e._handle_signal(_sig(clock), None, books=IN_TREND_BOOKS)
        assert {v["decision"] for v in out.values()} == {"ENGINE_HALTED"}
        assert not await _rows(e, "orders"), "no order reached the gateway"
        assert not any(e.gateway.rejects_by_book.values())
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_ct_x_keeps_its_costs_against_t1_test_and_ct_y_on_its_fade_does_not(settings, clock, monkeypatch):
    """Audit, 2026-09-26: own-ladder books stopped passing a T1, so CT-X — which owns its fade and
    always had the test — silently lost it. The owner keeps it; a book taking another's signal never had it."""
    import kotsin_nse.engine as eng

    e = await _engine(settings, clock, limit_orders=False)
    try:
        seen = {}
        real = eng.size_position

        def spy(**kw):
            seen.setdefault(kw["limits"].own_ladder, []).append(kw["option_target1"])
            return real(**kw)

        monkeypatch.setattr(eng, "size_position", spy)
        e._exits_by_strategy  # noqa: B018
        fade = Signal(strategy=StrategyKey.FUDKII_CT_X, symbol="TATASTEEL", direction=Direction.BULLISH, ts=_sig(clock).ts,
                      entry=187.0, stop=185.0, targets=(190.0,), reason="COUNTER fade", source_signal_id="x")
        _book(e, 16.95, 17.25, clock[0])
        from kotsin_nse.engine import FADE_BOOKS

        await e._handle_signal(fade, None, books=FADE_BOOKS)
        t1s = seen[True]
        assert len(t1s) == 2 and sorted(t1s, key=lambda x: x is None) [0] is not None and None in t1s, "CT-X passes its T1; CT-Y none"
    finally:
        await e.stop()


def test_the_entry_card_keeps_each_books_fill():
    from kotsin_nse.alerts.engine import AlertEngine

    a = AlertEngine.__new__(AlertEngine)
    from types import SimpleNamespace

    row = SimpleNamespace(kind="ENTRY", evidence={"signalId": "s1"}, card=None, fired_at=100.0)
    a.alerts, a.dirty = {"FUDKII_RT": [row]}, False
    a.mark_entered("s1", ts=101.0, price=17.1, qty=2750, book="FUDKII")
    a.mark_entered("s1", ts=102.0, price=17.1, qty=11000, book="FUDKII_RT_X")
    assert row.card["entered"]["book"] == "FUDKII" and row.card["entered"]["qty"] == 2750, "the first fill, not the last"
    assert set(row.card["entries"]) == {"FUDKII", "FUDKII_RT_X"} and row.card["entries"]["FUDKII_RT_X"]["qty"] == 11000
