"""The FUDKII_RT twin: a FUDKII fill is mirrored into the RT book at the same price and instant.

This never had a test, and on 2026-09-23 it crashed on every parent fill all morning —
``'ExposureVerdict' object has no attribute 'ok'`` — so the RT books traded nothing while the
parent took two trades. A twin that silently fails is worse than no twin: the comparison it exists
for looks like "RT made no trades" rather than "RT is broken".
"""

import time
from types import SimpleNamespace

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType, Position, PosSide
from kotsin_nse.engine import Engine


def _fill(ts: float, charges: float = 12.5):
    return SimpleNamespace(fill=SimpleNamespace(ts=ts, charges=charges))


@pytest.mark.asyncio
async def test_a_fudkii_fill_is_mirrored_into_the_rt_book_at_the_same_price(settings):
    e = Engine(settings)
    await e.start()
    try:
        opt = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
                         lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE")
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        now = time.time()
        pos = Position(id="p1", strategy="FUDKII", instrument=opt, underlying=und, side=PosSide.LONG,
                       qty=250, entry=50.0, opened_ts=now, signal_id="s1", direction=Direction.BULLISH,
                       equity_entry=1500.0, equity_sl=1450.0, option_sl=40.0, option_targets=(70.0,))
        e.positions[pos.id] = pos
        before = e.wallets["FUDKII_RT_X"].available

        await e._open_rt_twin(pos, opt, _fill(now))

        twins = [p for p in e.positions.values() if p.strategy == "FUDKII_RT_X"]
        assert len(twins) == 1
        t = twins[0]
        assert (t.instrument, t.qty, t.entry, t.opened_ts) == (opt, 250, 50.0, now), "copied, not re-matched"
        assert t.id != pos.id and "twin of p1" in t.note
        assert e.wallets["FUDKII_RT_X"].available == pytest.approx(before - 50.0 * 250 - 12.5)
        assert e.wallets["FUDKII_RT_MCX"].available == e.wallets["FUDKII_RT_MCX"].balance, "NSE never touches the MCX purse"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_commodity_fill_goes_to_the_mcx_purse_and_other_books_are_ignored(settings):
    e = Engine(settings)
    await e.start()
    try:
        fut = Instrument("482", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100,
                         multiplier=100, expiry="2026-10-19", underlying="CRUDEOIL")
        now = time.time()
        pos = Position(id="m1", strategy="FUDKII", instrument=fut, underlying=fut, side=PosSide.LONG,
                       qty=100, entry=50.0, opened_ts=now, signal_id="s2", direction=Direction.BULLISH)  # 50 x 100 x mult 100 = 5 lakh
        e.positions[pos.id] = pos
        await e._open_rt_twin(pos, fut, _fill(now))
        assert [p.strategy for p in e.positions.values() if p.id != "m1"] == ["FUDKII_RT_MCX"]

        other = Position(id="f1", strategy="FUKAA", instrument=fut, underlying=fut, side=PosSide.LONG,
                         qty=100, entry=50.0, opened_ts=now, signal_id="s3", direction=Direction.BULLISH)
        e.positions[other.id] = other
        await e._open_rt_twin(other, fut, _fill(now))
        assert sum(1 for p in e.positions.values() if p.strategy.startswith("FUDKII_RT")) == 1, "only FUDKII is twinned"
    finally:
        await e.stop()
