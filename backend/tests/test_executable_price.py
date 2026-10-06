"""A button's price is one that can trade (operator, 2026-10-06: KOTAKBANK 440 CE's SKIP read "≈ ₹5.97" — the mid —
"we need to write the price that we can buy at"): the ask to buy and the bid to sell, walked through a fresh depth book
for the whole quantity with the paper matcher's own ceiling."""

from __future__ import annotations

import time

import pytest

from kotsin_nse.engine import Engine
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote

CODE = "88643"


@pytest.fixture
def engine(settings):
    e = Engine(settings)
    e.quotes[CODE] = Quote(5.43, 5.40, 5.45, ts=time.time())
    return e


def test_without_a_depth_book_the_touch_is_the_price(engine):
    assert engine._executable(CODE, 8000, buy=False) == {"side": "sell", "touch": 5.40, "walk": None, "filled": None}
    assert engine._executable(CODE, 8000, buy=True) == {"side": "buy", "touch": 5.45, "walk": None, "filled": None}


def test_a_fresh_book_is_walked_for_the_whole_quantity(engine):
    now = time.time()
    engine.books[CODE] = BookSnapshot(scrip_code=CODE, bids=[(5.40, 4000), (5.35, 2000), (5.30, 4000)],
                                      asks=[(5.45, 3000), (5.50, 6000)], ts=now)
    # 4,000 at 5.40, 2,000 at 5.35, 2,000 at 5.30 — the 11:24 stop on this contract filled 5.36 with the bid at 5.40
    assert engine._executable(CODE, 8000, buy=False, now=now) == {"side": "sell", "touch": 5.40, "walk": 5.36, "filled": 8000}
    assert engine._executable(CODE, 8000, buy=True, now=now) == {"side": "buy", "touch": 5.45, "walk": 5.48, "filled": 8000}
    # a thin book says how much of the order it holds
    engine.books[CODE] = BookSnapshot(scrip_code=CODE, bids=[(5.40, 4000), (5.35, 2000)], asks=[(5.45, 3000)], ts=now)
    assert engine._executable(CODE, 8000, buy=False, now=now)["filled"] == 6000


def test_a_stale_book_is_not_walked_and_no_price_is_none(engine):
    now = time.time()
    engine.books[CODE] = BookSnapshot(scrip_code=CODE, bids=[(9.0, 99999)], asks=[(9.1, 99999)], ts=now - 3600)
    assert engine._executable(CODE, 8000, buy=False, now=now) == {"side": "sell", "touch": 5.40, "walk": None, "filled": None}
    engine.quotes[CODE] = Quote(5.43, 0.0, 5.45, ts=now)
    assert engine._executable(CODE, 8000, buy=False, now=now) is None, "no bid: nothing to sell into"
