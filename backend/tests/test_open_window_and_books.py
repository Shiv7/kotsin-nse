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
    assert (wide, tight) == (25_000.0, 6_000.0)
    for hm in ("09:00", "09:15", "09:45", "09:54"):
        assert m.age_limit_ms(_at(hm)) == wide, f"{hm} is inside the opening window"
    for hm in ("08:59", "09:55", "10:30", "15:15", "23:00"):
        assert m.age_limit_ms(_at(hm)) == tight, f"{hm} is outside it"
    assert Settings(_env_file=None).paper_open_max_book_age_ms == 25_000.0
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
    fill = m.fill(intent, book(15_271, _at("09:45")), now=_at("09:45"))
    assert fill.qty == 250 and round(fill.book_age_ms) == 15_271 and m.rejected_stale == 0
    with pytest.raises(NoBook, match="15271 ms old"):
        m.fill(intent, book(15_271, _at("10:30")), now=_at("10:30"))
    assert m.rejected_stale == 1
    # the wide window is not unlimited
    with pytest.raises(NoBook, match=r"limit 25000"):
        m.fill(intent, book(25_100, _at("09:45")), now=_at("09:45"))


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
