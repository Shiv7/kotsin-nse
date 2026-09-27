"""Phase 2: exits that can always be made, and safety systems that never close a book by accident.

Each test reproduces a failure seen in the ledger or logs on 2026-09-24/25:
* 09-24 09:45 — three stale-book ENTRY rejections tripped the breaker, which force-closed every book;
* 09-25 14:45-14:46 — a quiet put's EXIT retries ran the breaker count to 10 of 12;
* 09-25 14:30/14:39 — a stale option quote made the loop skip TATASTEEL, equity stop and all;
* 09-24 09:46 — PNBHOUSING's exit filled 14,300 of 16,250 and the position was booked fully closed;
* the RT books' stop-wait returned early and could skip the halt, the daily-loss exit and 15:20.
"""

from __future__ import annotations

import time

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    ExitReason,
    Fill,
    Instrument,
    InstrumentKind,
    OptionType,
    OrderIntent,
    OrderSide,
    Position,
    PosSide,
    Purpose,
)
from kotsin_nse.risk.exits import ExitEngine, MarketView
from kotsin_nse.risk.limits import RT_X_LIMITS, RT_Y_LIMITS, RiskLimits

OPT = Instrument("153805", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=2750,
                 tick_size=0.01, strike=185.0, option_type=OptionType.PE, underlying="TATASTEEL")
UND = Instrument("3499", "TATASTEEL", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TATASTEEL")


def _pos(strategy="FUDKII_RT_X", qty=16500, **kw) -> Position:
    base = dict(id="p1", strategy=strategy, instrument=OPT, underlying=UND, side=PosSide.LONG, qty=qty,
                entry=0.73, opened_ts=time.time() - 600, signal_id="FUDKII-TATASTEEL-1-B",
                direction=Direction.BEARISH, equity_entry=187.25, equity_sl=188.6, option_sl=0.60,
                option_targets=(0.99, 1.27))
    base.update(kw)
    return Position(**base)


def _view(**kw) -> MarketView:
    base = dict(option_ltp=0.55, underlying_ltp=187.9, now=time.time(), bars_held=0, past_force_flat=False,
                option_mid=0.55, spread_pct=0.02, quote_ok=True)
    base.update(kw)
    return MarketView(**base)


# -- the exit rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize("limits", [RT_X_LIMITS, RT_Y_LIMITS])
def test_the_stop_wait_can_no_longer_hide_the_force_flat_the_halt_or_the_daily_loss(limits):
    """Mid under the option stop, not yet sustained: the RT books used to return None right there,
    before the backstops were ever checked."""
    e = ExitEngine(limits)
    pos = _pos(option_sl=0.60)
    assert e.evaluate(pos, _view(option_mid=0.55, option_ltp=0.55)) is None, "the stop itself still waits"
    for flag, reason in (("past_force_flat", ExitReason.EOD), ("halted", ExitReason.HALT), ("daily_loss_hit", ExitReason.DAILY_LOSS)):
        d = ExitEngine(limits).evaluate(_pos(option_sl=0.60), _view(option_mid=0.55, option_ltp=0.55, **{flag: True}))
        assert d is not None and d.reason is reason and d.qty == 16500, flag


def test_a_stale_option_quote_still_enforces_the_equity_stop_and_the_backstops():
    e = ExitEngine(RT_X_LIMITS)
    stale = dict(quote_ok=False, option_mid=None)
    assert e.evaluate_stale(_pos(), _view(underlying_ltp=188.2, **stale)) is None, "under its stop: nothing to do"
    d = e.evaluate_stale(_pos(), _view(underlying_ltp=188.61, **stale))
    assert d is not None and d.reason is ExitReason.SL_EQ and "option quote stale" in d.note
    d = e.evaluate_stale(_pos(), _view(underlying_ltp=188.0, past_force_flat=True, **stale))
    assert d is not None and d.reason is ExitReason.EOD
    # nothing priced off the option fires on a stale quote: a "target" or option stop is not judged
    assert e.evaluate_stale(_pos(option_sl=0.9), _view(option_ltp=0.5, underlying_ltp=188.0, **stale)) is None


# -- the breaker --------------------------------------------------------------------------------------


def _gateway(reject: bool):
    from kotsin_nse.exec.gateway import Gateway, Mode
    from kotsin_nse.exec.paper import NoBook

    class Matcher:
        def fill(self, intent, book, *, fallback_ltp=None, now=None):
            if reject:
                raise NoBook("book for TATASTEEL is 24735 ms old (limit 6000)")
            return Fill(price=0.44, qty=intent.qty, ts=time.time(), charges=10.0)

    return Gateway(matcher=Matcher(), mode=lambda: Mode.PAPER, halted=lambda: (False, ""),
                   book_for=lambda c: None, ltp_for=lambda c: 0.5)


def _intent(purpose, coid):
    return OrderIntent(strategy="FUDKII_RT_X", instrument=OPT, side=OrderSide.SELL if purpose is Purpose.EXIT else OrderSide.BUY,
                       qty=16500, purpose=purpose, signal_id="s", client_order_id=coid, reason="r")


def test_exit_rejections_never_count_toward_the_engine_wide_breaker():
    g = _gateway(reject=True)
    for n in range(30):
        g.submit(_intent(Purpose.EXIT, f"x{n}"))
    assert g.consecutive_rejects == 0 and not g.breaker_tripped, "30 refused exits: a contract, not a broken gateway"
    for n in range(g.caps.breaker_consecutive_rejects):
        g.submit(_intent(Purpose.ENTRY, f"e{n}"))
    assert g.breaker_tripped, "entries still trip it at the configured count"


@pytest.mark.asyncio
async def test_a_tripped_breaker_blocks_entries_but_closes_nothing(settings, monkeypatch):
    from kotsin_nse.engine import Engine
    from kotsin_nse.exec.gateway import Mode
    from kotsin_nse.instrument.select import Quote

    e = Engine(settings)
    await e.start()
    try:
        await e.set_mode(Mode.PAPER)
        pos = _pos(strategy="FUDKII_RT_X")
        e.positions[pos.id] = pos
        now = time.time()
        e.ltps[OPT.scrip_code], e.ltps[UND.scrip_code] = 0.70, 187.9
        e.quotes[OPT.scrip_code] = Quote(ltp=0.70, bid=0.69, ask=0.71, ts=now)
        monkeypatch.setattr("kotsin_nse.engine.past_force_flat", lambda *_a: False)  # never the wall clock
        e.gateway.tripped_books.add("FUDKII_RT_X")
        assert not e.halted()[0] and e.gateway.book_tripped("FUDKII_RT_X"), "the book's own entries stop, nothing else"
        await e._manage_positions()
        assert pos.status == "OPEN", "the breaker no longer force-closes positions"
        e._halted, e._halt_reason = True, "operator"
        await e._manage_positions()
        assert pos.status != "OPEN" or pos.qty_remaining < pos.qty, "an OPERATOR halt still closes"
    finally:
        await e.stop()


# -- partial fills ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_partial_exit_books_what_filled_and_sends_the_rest_again(settings, monkeypatch):
    from kotsin_nse.domain import ExitDecision
    from kotsin_nse.engine import Engine
    from kotsin_nse.exec.gateway import Mode
    from kotsin_nse.exec.paper import BookSnapshot

    e = Engine(settings)
    await e.start()
    try:
        await e.set_mode(Mode.PAPER)
        pos = _pos(strategy="FUDKII", qty=16250, entry=3.75, option_sl=2.94)
        e.positions[pos.id] = pos
        now = time.time()
        # PNBHOUSING 09:46:19: the touch held 14,300, the order was for 16,250
        e.books[OPT.scrip_code] = BookSnapshot(scrip_code=OPT.scrip_code, bids=[(2.69, 14300)], asks=[(2.80, 50000)], ts=now)
        d = ExitDecision(pos.id, ExitReason.SL_EQ, 2.69, 16250, "underlying breached")
        await e._exit(pos, d, now)
        assert pos.status == "OPEN" and pos.qty_remaining == 1950, "1,950 contracts are still held"
        e.books[OPT.scrip_code] = BookSnapshot(scrip_code=OPT.scrip_code, bids=[(2.65, 5000)], asks=[(2.80, 50000)], ts=now)
        await e._exit(pos, ExitDecision(pos.id, ExitReason.SL_EQ, 2.65, 1950, "underlying breached"), now)
        assert pos.status == "CLOSED" and pos.qty_remaining == 0
        ids = [o["client_order_id"] for o in await e.ledger.rows_between("orders", now - 60, now + 60)]
        assert len(ids) == 2 and ids[0] != ids[1], "the rest went under a new id, not a 'duplicate'"
    finally:
        await e.stop()


# -- books and quotes -----------------------------------------------------------------------------------


def test_a_sell_with_no_bid_is_refused_and_no_book_at_all_still_falls_back(settings):
    from kotsin_nse.exec.paper import BookSnapshot, NoBook, PaperMatcher
    from kotsin_nse.risk.costs import CostModel

    m = PaperMatcher(CostModel(settings))
    now = time.time()
    asks_only = BookSnapshot(scrip_code="1", bids=[], asks=[(0.74, 5000)], ts=now)
    with pytest.raises(NoBook, match="no bid"):
        m.fill(_intent(Purpose.EXIT, "a"), asks_only, fallback_ltp=0.72, now=now)
    f = m.fill(_intent(Purpose.EXIT, "b"), None, fallback_ltp=0.72, now=now)
    assert f.qty == 16500 and f.price <= 0.72 and f.levels == 0, "no book at all: the degraded path, as before (force-flat relies on it)"


@pytest.mark.asyncio
async def test_quiet_held_contracts_are_requoted_in_the_background(settings, monkeypatch):
    import kotsin_nse.engine as engine_mod
    from kotsin_nse.engine import Engine
    from kotsin_nse.exec.paper import BookSnapshot
    from kotsin_nse.instrument.select import Quote

    e = Engine(settings)
    pos = _pos()
    e.positions[pos.id] = pos
    now = time.time()
    e.quotes[OPT.scrip_code] = Quote(ltp=0.55, bid=0.54, ask=0.56, ts=now - 2)      # quote fresh...
    e.books[OPT.scrip_code] = BookSnapshot(scrip_code=OPT.scrip_code, bids=[(0.54, 9000)], asks=[(0.56, 9000)], ts=now - 25)  # ...book 25 s old
    calls = []

    async def market_feed(insts):
        calls.append([i.scrip_code for i in insts])
        return {OPT.scrip_code: {"ltp": 0.55, "bid": 0.54, "ask": 0.0, "bid_qty": 60000, "ask_qty": 0, "ts": time.time()}}

    monkeypatch.setattr(e.rest, "market_feed", market_feed)
    monkeypatch.setattr(engine_mod, "is_open", lambda *_a: False)
    assert await e._refresh_held_quotes(now) == 0 and calls == [], "closed segment: no calls"
    monkeypatch.setattr(engine_mod, "is_open", lambda *_a: True)
    assert await e._refresh_held_quotes(now) == 1 and calls == [[OPT.scrip_code]]
    b = e.books[OPT.scrip_code]
    assert b.bids == [(0.54, 60000)] and b.asks == [] and b.age_ms(time.time()) < 1000, "a fresh one-sided book to sell into"
    assert await e._refresh_held_quotes(time.time()) == 0, "fresh now: nothing more is asked"


@pytest.mark.asyncio
async def test_the_selector_refreshes_a_stale_book_even_when_the_quote_is_fresh(settings, option):
    from kotsin_nse.engine import Engine
    from kotsin_nse.exec.paper import BookSnapshot
    from kotsin_nse.instrument.select import Quote

    e = Engine(settings)
    now = time.time()
    e.quotes[option.scrip_code] = Quote(ltp=7.0, bid=6.9, ask=7.0, ts=now)
    e.books[option.scrip_code] = BookSnapshot(scrip_code=option.scrip_code, bids=[(6.9, 500)], asks=[(7.0, 500)], ts=now - 30)
    asked = []

    async def market_feed(insts):
        asked.extend(i.scrip_code for i in insts)
        return {}

    async def nothing(*_a, **_k):
        return None

    e.rest.market_feed = market_feed
    e._follow_depth = nothing
    e.feed.subscribe = nothing
    await e._ensure_quotes([option], 1500.0)
    assert asked == [option.scrip_code], "the 09:45 stale-book entry rejections came from skipping this"


def test_the_base_limits_are_untouched():
    """Phase 2 changes how exits are carried out, not the trading rules."""
    assert RiskLimits().sustain_s is None and RT_X_LIMITS.sustain_s == 75.0 and RT_Y_LIMITS.peak_giveback_pct == 3.0
