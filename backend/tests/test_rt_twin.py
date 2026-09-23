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

        twins = {p.strategy: p for p in e.positions.values() if p.strategy.startswith("FUDKII_RT")}
        assert set(twins) == {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}, "one twin per NSE exit policy"
        for t in twins.values():
            assert (t.instrument, t.qty, t.entry, t.opened_ts) == (opt, 250, 50.0, now), "copied, not re-matched"
            assert t.id != pos.id and "twin of p1" in t.note
            assert e.wallets[t.strategy].available == pytest.approx(before - 50.0 * 250 - 12.5), "each book pays its own purse"
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


@pytest.mark.asyncio
async def test_the_twin_carries_the_options_own_classic_ladder_when_it_has_one(settings):
    from kotsin_nse.bars.pivots import classic_pivots
    from kotsin_nse.instrument.legs import LegPivots

    e = Engine(settings)
    await e.start()
    try:
        opt = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
                         lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE")
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        lv = classic_pivots(60.0, 40.0, 50.0)  # own R1 60, R2 70, R3 90, R4 110
        e.leg_pivots.by_code[opt.scrip_code] = LegPivots(
            scrip_code=opt.scrip_code, symbol="RELIANCE 29 SEP 2026 CE 1500", root="RELIANCE", kind="CE",
            strike=1500.0, levels=lv, session="2026-09-22", close=50.0, volume=10_000,
        )
        now = time.time()
        pos = Position(id="p1", strategy="FUDKII", instrument=opt, underlying=und, side=PosSide.LONG,
                       qty=250, entry=50.0, opened_ts=now, signal_id="s1", direction=Direction.BULLISH,
                       equity_entry=1500.0, equity_sl=1450.0, equity_targets=(1550.0,),
                       option_sl=40.0, option_targets=(70.0,))
        e.positions[pos.id] = pos
        await e._open_rt_twin(pos, opt, _fill(now))
        twin = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X")
        assert twin.option_t1 == 60.0 and twin.option_targets == (60.0, 70.0, 90.0, 110.0)
        assert twin.targets_hit == 0 and "own mtf ladder" in twin.note
        n = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_N")
        assert n.option_targets == (60.0, 70.0, 90.0, 110.0) and "own daily_r ladder" in n.note
        y = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        assert y.option_targets == (60.0, 70.0, 90.0, 110.0), "no IV history → no expected move → every rung above entry"
        assert pos.option_targets == (70.0,), "the parent keeps its delta-projected ladder"

        # a contract with no ladder: equity trigger only. A different underlying, because the RT
        # book allows one position per underlying and the first twin already holds RELIANCE.
        e.leg_pivots.by_code.clear()
        opt2 = Instrument("55555", "TCS", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=175,
                          strike=3000.0, option_type=OptionType.CE, underlying="TCS")
        und2 = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        pos2 = Position(id="p2", strategy="FUDKII", instrument=opt2, underlying=und2, side=PosSide.LONG,
                        qty=175, entry=50.0, opened_ts=now, signal_id="s2", direction=Direction.BULLISH,
                        equity_entry=3000.0, equity_sl=2950.0, equity_targets=(3100.0,), option_sl=40.0)
        e.positions[pos2.id] = pos2
        await e._open_rt_twin(pos2, opt2, _fill(now))
        twin2 = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X" and p.signal_id == "s2")
        assert twin2.option_t1 == 0.0 and twin2.option_targets == () and "equity trigger only" in twin2.note
    finally:
        await e.stop()


def test_a_twin_and_its_parent_never_share_an_exit_order_id():
    """2026-09-23 14:35: the DIXON parent's SL-EQ exit and its twin's carried the same
    client_order_id (both keyed on the shared signal id); the twin's was refused as a duplicate
    every second while the underlying sat through the stop."""
    from dataclasses import replace

    from kotsin_nse.domain import ExitDecision, ExitReason
    from kotsin_nse.engine import exit_client_order_id

    opt = Instrument("45678", "DIXON", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=50, strike=14000.0,
                     option_type=OptionType.CE, underlying="DIXON")
    parent = Position(id="pos-parent", strategy="FUDKII", instrument=opt, underlying=opt, side=PosSide.LONG,
                      qty=350, entry=19.58, opened_ts=1.0, signal_id="FUDKII-DIXON-1-A", direction=Direction.BULLISH)
    twin = replace(parent, id="pos-twin", strategy="FUDKII_RT_X")
    d = ExitDecision(position_id="x", reason=ExitReason.SL_EQ, ref_price=17.5, qty=350)
    assert exit_client_order_id(parent, d) != exit_client_order_id(twin, d)
    assert exit_client_order_id(twin, d) == exit_client_order_id(twin, d), "a retry is the same order"


@pytest.mark.asyncio
async def test_a_restored_open_position_is_subscribed_even_when_off_the_shortlist(settings):
    """2026-09-23: the DIXON 14000 CE twin came back from a restart six strikes off the shortlist,
    with no quote, no evaluation and no exit — silently."""
    from types import SimpleNamespace

    e = Engine(settings)
    calls: list[tuple[str, list[str]]] = []

    async def recorder(channel, instruments):
        calls.append((channel, [i.scrip_code for i in instruments]))

    e.feed = SimpleNamespace(subscribe=recorder)
    far = Instrument("100512", "DIXON", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=50, strike=14000.0,
                     option_type=OptionType.CE, underlying="DIXON")
    und = Instrument("1", "DIXON", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="DIXON")
    e.positions["open"] = Position(id="open", strategy="FUDKII_RT_X", instrument=far, underlying=und, side=PosSide.LONG,
                                   qty=350, entry=19.58, opened_ts=1.0, signal_id="s", direction=Direction.BULLISH)
    e.positions["closed"] = Position(id="closed", strategy="FUDKII", instrument=far, underlying=und, side=PosSide.LONG,
                                     qty=350, entry=19.58, opened_ts=1.0, signal_id="s", direction=Direction.BULLISH,
                                     status="CLOSED", qty_remaining=0)
    got = await e._subscribe_open_positions()
    assert [i.scrip_code for i in got] == ["100512"]
    assert calls == [("mf", ["100512"]), ("md", ["100512"]), ("oi", ["100512"])]
    e.positions.clear()
    assert await e._subscribe_open_positions() == []


# -- the dried-volume entry gate (RT-X and RT-Y only) -------------------------------------------


def _bars30(symbol: str, code: str, vols: list[float]) -> list:
    """30m bars ending at 09:15 IST today, in the store's own bucket-start convention."""
    from datetime import datetime

    from kotsin_nse.bars.unified import BarSource, UnifiedBar
    from kotsin_nse.market.session import IST, ist_today

    t = ist_today()
    end = datetime(t.year, t.month, t.day, 9, 15, tzinfo=IST).timestamp()
    out = []
    for i, v in enumerate(vols):
        ts = end - (len(vols) - 1 - i) * 1800
        out.append(UnifiedBar(symbol=symbol, scrip_code=code, tf="30m", ts=ts, open=100.0, high=101.0,
                              low=99.0, close=100.0, volume=v, source=BarSource.REST, complete=True))
    return out


def _reliance_fill(e, now):
    opt = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
                     lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE")
    und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
    pos = Position(id="p1", strategy="FUDKII", instrument=opt, underlying=und, side=PosSide.LONG,
                   qty=250, entry=50.0, opened_ts=now, signal_id="s1", direction=Direction.BULLISH,
                   equity_entry=1500.0, equity_sl=1450.0, option_sl=40.0, option_targets=(70.0,))
    e.positions[pos.id] = pos
    return pos, opt


@pytest.mark.asyncio
async def test_dried_equity_volume_skips_rt_x_and_rt_y_but_not_the_control_book(settings):
    e = Engine(settings)
    await e.start()
    try:
        # baseline T-2..T-7 = 10,000; the trigger bar 4,000 (0.40) and the one before 5,000 (0.50)
        e.store.seed("RELIANCE", "30m", _bars30("RELIANCE", "2885", [10_000.0] * 6 + [5_000.0, 4_000.0]))
        pos, opt = _reliance_fill(e, time.time())
        await e._open_rt_twin(pos, opt, _fill(time.time()))
        books = sorted(p.strategy for p in e.positions.values() if p.strategy.startswith("FUDKII_RT"))
        assert books == ["FUDKII_RT_N"], "the control book still mirrors every fill"
        assert e.wallets["FUDKII_RT_X"].available == e.wallets["FUDKII_RT_X"].balance
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_dry_front_future_skips_even_when_the_equity_bar_is_live(settings):
    """SBILIFE 2026-09-23 09:45: equity surge 1.48 / 2.51 but the SEP future 0.79 / 0.51 closing on
    its daily S1 — the future is read too, from the broker's candles, up to the trigger bar only."""
    from kotsin_nse.market.session import to_ist

    e = Engine(settings)
    await e.start()
    try:
        eq = _bars30("RELIANCE", "2885", [10_000.0] * 6 + [25_000.0, 15_000.0])
        e.store.seed("RELIANCE", "30m", eq)
        fut = Instrument("68781", "RELIANCE", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=250,
                         expiry="2026-09-29", underlying="RELIANCE")
        e.catalogue_loader.catalogue.futures_by_symbol["RELIANCE"] = [fut]
        asked: list[tuple] = []

        async def candles(inst, tf, start, end):
            asked.append((inst.scrip_code, tf))
            rows = [{"dt": to_ist(b.ts).strftime("%Y-%m-%dT%H:%M:00"), "o": 1, "h": 1, "l": 1, "c": 1, "v": v}
                    for b, v in zip(eq, [10_000.0] * 6 + [5_100.0, 7_900.0], strict=True)]
            # the partial bar after the trigger, which must not be read as T
            rows.append({"dt": to_ist(eq[-1].ts + 1800).strftime("%Y-%m-%dT%H:%M:00"), "o": 1, "h": 1, "l": 1, "c": 1, "v": 90_000.0})
            return rows

        e.rest.candles = candles  # type: ignore[method-assign]
        pos, opt = _reliance_fill(e, time.time())
        await e._open_rt_twin(pos, opt, _fill(time.time()))
        assert asked == [("68781", "30m")]
        books = sorted(p.strategy for p in e.positions.values() if p.strategy.startswith("FUDKII_RT"))
        assert books == ["FUDKII_RT_N"]

        # the broker not answering is "unknown", never "dried": all three books mirror
        async def broken(inst, tf, start, end):
            raise RuntimeError("historical endpoint down")

        e.rest.candles = broken  # type: ignore[method-assign]
        e.positions.clear()
        pos, opt = _reliance_fill(e, time.time())
        await e._open_rt_twin(pos, opt, _fill(time.time()))
        books = sorted(p.strategy for p in e.positions.values() if p.strategy.startswith("FUDKII_RT"))
        assert books == ["FUDKII_RT_N", "FUDKII_RT_X", "FUDKII_RT_Y"]
    finally:
        await e.stop()
