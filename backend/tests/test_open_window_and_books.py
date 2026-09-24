"""The three rules the 2026-09-24 open forced (operator, same morning).

Three stale-depth rejections at the 09:45 decision tripped a breaker set to 3, the breaker halts
the ENGINE rather than the gateway, and every book — including the two that were fine — had its
live positions force-flattened at market. Hence: a far longer reject fuse, a depth window that is
wide through the opens and tight afterwards, and a commodity book that cannot see NSE at all.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OrderIntent, OrderSide, Purpose
from kotsin_nse.engine import Engine
from kotsin_nse.exec.gateway import LiveCaps
from kotsin_nse.exec.paper import BookSnapshot, NoBook, PaperMatcher
from kotsin_nse.instrument.select import Quote
from kotsin_nse.market.session import IST
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy.keys import StrategyKey


def _at(hm: str) -> float:
    h, m = (int(x) for x in hm.split(":"))
    return datetime.combine(datetime.now(IST).date(), dtime(h, m), tzinfo=IST).timestamp()


# -- the reject fuse ----------------------------------------------------------------------------


def test_eleven_rejects_are_tolerated_and_the_twelfth_trips_the_breaker(settings):
    assert LiveCaps().breaker_consecutive_rejects == 12
    assert Settings(_env_file=None).live_breaker_consecutive_rejects == 12
    e = Engine(settings)
    g = e.gateway
    assert g.caps.breaker_consecutive_rejects == 12, "the setting reaches the live gateway"

    for i in range(11):
        g.consecutive_rejects = i + 1
        g.breaker_tripped = g.consecutive_rejects >= g.caps.breaker_consecutive_rejects
        assert not g.breaker_tripped, f"reject {i + 1} must not trip it"
    g.consecutive_rejects = 12
    g.breaker_tripped = g.consecutive_rejects >= g.caps.breaker_consecutive_rejects
    assert g.breaker_tripped, "the twelfth does"
    # and a tripped breaker is what halts the whole engine — the reason the fuse was lengthened
    assert e.halted()[0] and "breaker" in e.halted()[1]
    g.reset_breaker()
    assert not e.halted()[0]


# -- the depth window ---------------------------------------------------------------------------


def test_the_depth_window_is_wide_through_the_opens_and_tight_after(settings):
    m = PaperMatcher(CostModel(settings))
    assert m.open_window_ist == ("09:00", "09:55")
    wide, tight = m.open_max_book_age_ms, m.max_book_age_ms
    assert (wide, tight) == (10_000.0, 6_000.0), "tightened once the backlog was measured out"
    for hm in ("09:00", "09:15", "09:45", "09:54"):
        assert m.age_limit_ms(_at(hm)) == wide, f"{hm} is inside the opening window"
    for hm in ("08:59", "09:55", "10:30", "15:15", "23:00"):
        assert m.age_limit_ms(_at(hm)) == tight, f"{hm} is outside it"
    assert Settings(_env_file=None).paper_open_max_book_age_ms == 10_000.0
    assert Settings(_env_file=None).paper_max_book_age_ms == 6_000.0
    assert Engine(settings).matcher.open_window_ist == ("09:00", "09:55")


def test_the_book_that_was_rejected_at_the_open_would_now_fill(settings, option):
    """ADANIENT's depth was 15,271 ms old at 09:45 and the order was refused. Same book, same
    instant, under the new window: it fills. At 10:30 it is still refused."""

    def book(age_ms: float, at: float) -> BookSnapshot:
        return BookSnapshot(scrip_code="45678", bids=[(6.9, 10_000)], asks=[(7.0, 10_000)],
                            ts=at - age_ms / 1000)

    m = PaperMatcher(CostModel(settings))
    inst = option
    intent = OrderIntent(strategy="FUDKII", instrument=inst, side=OrderSide.BUY, qty=250,
                         purpose=Purpose.ENTRY, signal_id="s", client_order_id="c", reason="r")
    fill = m.fill(intent, book(9_500, _at("09:45")), now=_at("09:45"))
    assert fill.qty == 250 and round(fill.book_age_ms) == 9_500 and m.rejected_stale == 0
    with pytest.raises(NoBook, match="9500 ms old"):
        m.fill(intent, book(9_500, _at("10:30")), now=_at("10:30"))
    assert m.rejected_stale == 1
    # the opening allowance is a margin for a thin strike, not a licence for a 15 s price: the
    # backlog that produced those was measured out, so the book that was refused at 09:45 today
    # would be refused again — correctly, this time.
    with pytest.raises(NoBook, match=r"limit 10000"):
        m.fill(intent, book(15_271, _at("09:45")), now=_at("09:45"))


# -- one exchange per book ------------------------------------------------------------------------


def test_the_commodity_book_takes_mcx_only_and_the_others_take_the_rest():
    mcx = StrategyKey.FUDKII_RT_MCX.value
    assert Engine.book_trades(mcx, Segment.MCX_FO)
    for seg in (Segment.NSE_EQ, Segment.NSE_FO, Segment.NSE_IDX, None):
        assert not Engine.book_trades(mcx, seg), f"{seg} must never reach the commodity book"
    for book in (StrategyKey.FUDKII.value, StrategyKey.FUDKII_RT_X.value,
                 StrategyKey.FUDKII_RT_N.value, StrategyKey.FUDKII_RT_Y.value,
                 StrategyKey.FUDKII_CT_X.value, StrategyKey.FUDKII_CT_Y.value):
        assert Engine.book_trades(book, Segment.NSE_EQ)
        assert not Engine.book_trades(book, Segment.MCX_FO), "commodities are the MCX book's"
    # there is no currency segment in this engine at all
    assert {s.value for s in Segment} == {"NSE_EQ", "NSE_FO", "NSE_IDX", "MCX_FO"}


@pytest.mark.asyncio
async def test_an_nse_trigger_never_reaches_the_commodity_books_page(settings, equity):
    """The card page reads every FUDKII trigger of the day; each book must see only its own."""
    from kotsin_nse.domain import Direction
    from kotsin_nse.strategy.base import Signal

    e = Engine(settings)
    await e.start()
    try:
        crude = Instrument("482", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE,
                           lot_size=100, expiry="2026-10-17", underlying="CRUDEOIL")
        e.underlyings["RELIANCE"], e.underlyings["CRUDEOIL"] = equity, crude
        ts = int(time.time()) - 600
        for sym in ("RELIANCE", "CRUDEOIL"):
            sig = Signal(strategy=StrategyKey.FUDKII, symbol=sym, direction=Direction.BULLISH,
                         ts=ts, entry=100.0, stop=95.0, targets=(110.0,), grade="A", rr=2.0,
                         reason="ST flip UP + close above upper band")
            await e.ledger.insert_signal(sig.to_json(), "PAPER_FILLED", sig.reason)

        mcx = await e.book_cards(StrategyKey.FUDKII_RT_MCX.value)
        assert [c["symbol"] for c in mcx["cards"]] == ["CRUDEOIL"]
        for book in (StrategyKey.FUDKII_RT_X.value, StrategyKey.FUDKII.value):
            rows = await e.book_cards(book)
            assert [c["symbol"] for c in rows["cards"]] == ["RELIANCE"], f"{book} sees no commodity"
    finally:
        await e.stop()


def test_a_fresh_book_is_used_at_its_own_age_the_window_is_a_ceiling_not_a_wait(settings, option):
    """The wider opening window must not make a fill wait for, or prefer, older depth. The matcher
    prices on whatever snapshot the feed last delivered; the window only decides whether to refuse
    it. A 2 s book fills on 2 s data at 09:45 exactly as it does at 11:00."""
    m = PaperMatcher(CostModel(settings))
    intent = OrderIntent(strategy="FUDKII", instrument=option, side=OrderSide.BUY, qty=250,
                         purpose=Purpose.ENTRY, signal_id="s", client_order_id="c", reason="r")

    def book(age_ms: float, at: float, ask: float) -> BookSnapshot:
        return BookSnapshot(scrip_code="45678", bids=[(ask - 0.1, 10_000)], asks=[(ask, 10_000)],
                            ts=at - age_ms / 1000)

    inside, outside = _at("09:45"), _at("11:00")
    fresh_in = m.fill(intent, book(2_000, inside, 7.0), now=inside)
    fresh_out = m.fill(intent, book(2_000, outside, 7.0), now=outside)
    assert fresh_in.price == fresh_out.price == 7.0, "same book, same price, whatever the window"
    assert round(fresh_in.book_age_ms) == round(fresh_out.book_age_ms) == 2_000
    assert m.rejected_stale == 0
    # the price comes from the book it was handed, not from the window
    assert m.fill(intent, book(200, inside, 7.5), now=inside).price == 7.5


# -- depth where it is used, not everywhere ------------------------------------------------------


@pytest.mark.asyncio
async def test_depth_follows_what_is_about_to_be_priced_and_lets_the_rest_go(settings, equity, option):
    """~1,000 frames a second went through the socket reader for metrics no strategy reads. Depth
    now tracks the contract being priced, live cards and open positions — and nothing else."""
    from kotsin_nse.domain import Direction, Position, PosSide

    e = Engine(settings)
    e.underlyings[equity.symbol] = equity
    cat = e.catalogue_loader.catalogue
    for inst in (equity, option):
        cat.by_code[inst.scrip_code] = inst

    calls: list[tuple[str, tuple[str, ...]]] = []

    class Feed:
        async def subscribe(self, ch, insts):
            calls.append(("s", tuple(i.scrip_code for i in insts)))

        async def unsubscribe(self, ch, insts):
            calls.append(("u", tuple(i.scrip_code for i in insts)))

    e.feed = Feed()
    e._depth_pinned = {"2885"}          # the archive sample
    e._depth_following = {"2885"}
    assert e.depth_wanted() == set(), "nothing held, nothing carded, nothing to follow"
    await e._sync_depth()
    assert calls == [], "an empty set is not a subscription storm"

    # a contract the selector is weighing goes on depth, with its underlying leg
    e.tape.follow(equity.symbol, [option.scrip_code], now=time.time())
    await e._sync_depth()
    assert calls and calls[0][0] == "s" and option.scrip_code in calls[0][1]
    assert option.scrip_code in e._depth_following

    # it lapses -> depth is handed back, but never the pinned archive sample
    e.tape._watch.clear()
    calls.clear()
    await e._sync_depth()
    assert calls and calls[0][0] == "u" and calls[0][1] == (option.scrip_code,)
    assert e._depth_following == {"2885"}, "the archive sample survives every reconcile"

    # an open position pins its own contract for as long as it is open
    e.positions["p"] = Position(
        id="p", strategy="FUDKII_RT_X", instrument=option, underlying=equity, side=PosSide.LONG,
        qty=250, entry=10.0, opened_ts=time.time(), signal_id="s", direction=Direction.BULLISH,
    )
    assert option.scrip_code in e.depth_wanted()
    # and the rolling set is capped so a pathological day cannot restore the old load
    assert e.s.depth_max_subscriptions == 250 and e.s.depth_follow_enabled


def test_a_snapshot_quote_becomes_a_one_level_book_rather_than_a_guessed_price(settings, option):
    """The selector already REST-quotes the strikes it weighs, and that response carries the touch
    and its size. A contract whose depth has not warmed up fills against that instead of the
    degraded last-price-plus-slippage path."""
    from kotsin_nse.exec.paper import book_from_quote

    now = time.time()
    b = book_from_quote("45678", bid=6.9, ask=7.0, bid_qty=5_000, ask_qty=5_000, ts=now)
    assert b is not None and b.best_bid == 6.9 and b.best_ask == 7.0 and b.age_ms(now) == 0

    m = PaperMatcher(CostModel(settings))
    intent = OrderIntent(strategy="FUDKII", instrument=option, side=OrderSide.BUY, qty=250,
                         purpose=Purpose.ENTRY, signal_id="s", client_order_id="c", reason="r")
    fill = m.fill(intent, b, now=now)
    assert fill.price == 7.0 and fill.qty == 250 and fill.levels == 1

    # one level only: a bigger order truncates at what the touch could absorb, and says so
    big = OrderIntent(strategy="FUDKII", instrument=option, side=OrderSide.BUY, qty=9_000,
                      purpose=Purpose.ENTRY, signal_id="s2", client_order_id="c2", reason="r")
    assert m.fill(big, b, now=now).qty == 5_000 and m.truncated == 1
    # a one-sided or sizeless quote is not a book, and is refused rather than invented
    assert book_from_quote("1", bid=0.0, ask=7.0, bid_qty=0, ask_qty=10, ts=now) is None
    assert book_from_quote("1", bid=6.9, ask=7.0, bid_qty=0, ask_qty=10, ts=now) is None


@pytest.mark.asyncio
async def test_the_selectors_rest_quote_stands_a_book_behind_a_cold_contract(settings, option):
    """The contract chosen at a trigger may not have had its depth subscription warm up yet. The
    quote the selector already fetched becomes its book — but never over a live one."""
    e = Engine(settings)
    e.matcher.max_book_age_ms = 6_000.0
    e.matcher.open_window_ist = ("09:00", "09:00")  # never inside the window, for determinism

    subscribed: list[tuple[str, int]] = []

    class Feed:
        async def subscribe(self, ch, insts):
            subscribed.append((ch, len(insts)))

        async def unsubscribe(self, ch, insts):
            pass

    class Rest:
        async def market_feed(self, insts):
            return {
                i.scrip_code: {"ltp": 7.0, "bid": 6.9, "ask": 7.0, "bid_qty": 4_000,
                               "ask_qty": 4_000, "ts": time.time()}
                for i in insts
            }

    e.feed, e.rest = Feed(), Rest()
    await e._ensure_quotes([option], spot=1500.0)

    book = e.books.get(option.scrip_code)
    assert book is not None
    assert book is not None and book.best_ask == 7.0 and book.asks[0][1] == 4_000
    assert dict(subscribed).keys() >= {"mf", "md"}, "price AND depth, ahead of the order"
    assert option.scrip_code in e._depth_following

    # a live, fresh 20-level book is never replaced by the one-level stand-in
    real = BookSnapshot(scrip_code=option.scrip_code, bids=[(6.95, 9_999)], asks=[(6.99, 9_999)],
                        ts=time.time())
    e.books[option.scrip_code] = real
    e.quotes.pop(option.scrip_code, None)
    await e._ensure_quotes([option], spot=1500.0)
    assert e.books[option.scrip_code] is real, "the real ladder wins while it is fresh"


@pytest.mark.asyncio
async def test_a_strike_with_a_fresh_price_still_gets_its_depth_before_the_order(settings, option):
    """The REST fetch is skipped when the price is already fresh. Depth must not be skipped with
    it — since depth only follows what is in use, that strike would reach the matcher with no book
    at all, which is exactly the hole narrowing the subscription could have opened."""
    e = Engine(settings)
    subs: list[tuple[str, tuple[str, ...]]] = []

    class Feed:
        async def subscribe(self, ch, insts):
            subs.append((ch, tuple(i.scrip_code for i in insts)))

    class Rest:
        async def market_feed(self, insts):
            raise AssertionError("must not be called: the quote is fresh")

    e.feed, e.rest = Feed(), Rest()
    e.quotes[option.scrip_code] = Quote(ltp=7.0, bid=6.9, ask=7.0, ts=time.time())

    await e._ensure_quotes([option], spot=1500.0)
    assert ("md", (option.scrip_code,)) in subs, "depth is subscribed regardless of price freshness"
    assert option.scrip_code in e._depth_following
    assert not any(ch == "mf" for ch, _ in subs), "and no REST round trip was needed"
