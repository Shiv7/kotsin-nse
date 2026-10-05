"""Quiet held options are re-quoted (operator, 2026-10-05: "fix the held-quote fix").

``_refresh_held_quotes`` asks the broker for a snapshot of every held contract whose quote or book has aged,
every HELD_QUOTE_POLL_S while its market is open. Since 2026-09-28 it called ``is_open`` without the trading
calendar and raised on every pass with a position open (29 Sep – 5 Oct: thousands of ``held_quotes.failed``),
so a quiet option was never re-quoted and, after 60 s, was managed on its stale-quote path alone."""

from __future__ import annotations

from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.engine import Engine
from kotsin_nse.market.session import IST
from tests.test_equity_stop import _pos
from tests.test_limit_orders import OPT

TUESDAY_11 = datetime(2026, 10, 6, 11, 0, tzinfo=IST).timestamp()  # an NSE session
SUNDAY_11 = datetime.combine(datetime(2026, 10, 4).date(), dtime(11, 0), tzinfo=IST).timestamp()


def _engine(settings, monkeypatch):
    e = Engine(settings)
    asked: list[list[str]] = []

    async def market_feed(insts):
        asked.append([i.scrip_code for i in insts])
        # as the broker's V1/MarketFeed comes back (venue/fivepaisa/rest.py): one row per code, stamped now
        return {i.scrip_code: {"ltp": 12.0, "bid": 0.0, "ask": 0.0, "ts": TUESDAY_11} for i in insts}

    monkeypatch.setattr(e.rest, "market_feed", market_feed)
    applied: list[int] = []
    monkeypatch.setattr(e, "_apply_snapshot", lambda rows, now: applied.append(len(rows)))
    return e, asked, applied


@pytest.mark.asyncio
async def test_a_quiet_held_option_is_requoted_while_its_market_is_open(settings, monkeypatch):
    e, asked, applied = _engine(settings, monkeypatch)
    pos = _pos()
    e.positions[pos.id] = pos  # held, and never quoted: stale by definition
    assert await e._refresh_held_quotes(TUESDAY_11) == 1, "the pass runs instead of raising"
    assert asked == [[OPT.scrip_code]] and applied == [1]


@pytest.mark.asyncio
async def test_nothing_is_asked_while_the_market_is_shut_or_nothing_is_held(settings, monkeypatch):
    e, asked, _ = _engine(settings, monkeypatch)
    assert await e._refresh_held_quotes(TUESDAY_11) == 0, "nothing held"
    pos = _pos()
    e.positions[pos.id] = pos
    assert await e._refresh_held_quotes(SUNDAY_11) == 0, "a Sunday is not a session"
    pos.status = "CLOSED"
    assert await e._refresh_held_quotes(TUESDAY_11) == 0, "a closed position is not held"
    assert asked == []
