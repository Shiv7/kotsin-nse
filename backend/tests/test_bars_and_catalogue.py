"""Bar construction and the scrip master."""

from __future__ import annotations

import time

from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.periods import monthly, previous_complete, weekly
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource
from kotsin_nse.config import Segment
from kotsin_nse.domain import InstrumentKind, OptionType
from kotsin_nse.instrument.catalogue import Catalogue, parse_master
from kotsin_nse.market.session import ist_day, ist_hm

from .conftest import bar, ist_ts

MASTER_EQ = """Exch,ExchType,Scripcode,Name,Expiry,ScripType,StrikeRate,FullName,TickSize,LotSize,QtyLimit,Multiplier,SymbolRoot,ISIN,Series
N,C,2885,RELIANCE,,,0,RELIANCE INDUSTRIES,0.05,1,100000,1,RELIANCE,INE002A01018,EQ
N,C,11536,TCS,,,0,TATA CONSULTANCY,0.05,1,100000,1,TCS,INE467B01029,EQ
N,C,9999,SOMEBOND,,,0,A BOND,0.01,1,100,1,SOMEBOND,INE000X01011,N2
"""

MASTER_FO = """Exch,ExchType,Scripcode,Name,Expiry,ScripType,StrikeRate,FullName,TickSize,LotSize,QtyLimit,Multiplier,SymbolRoot,ISIN,Series
N,D,54321,RELIANCE,2026-09-25 00:00:00,XX,0,RELIANCE 25 SEP 2026 FUT,0.05,250,50000,1,RELIANCE,,
N,D,54322,RELIANCE,2026-10-30 00:00:00,XX,0,RELIANCE 30 OCT 2026 FUT,0.05,250,50000,1,RELIANCE,,
N,D,45678,RELIANCE,2026-09-25 00:00:00,CE,1500,RELIANCE 25 SEP 2026 CE 1500,0.05,250,50000,1,RELIANCE,,
N,D,45679,RELIANCE,2026-09-25 00:00:00,PE,1500,RELIANCE 25 SEP 2026 PE 1500,0.05,250,50000,1,RELIANCE,,
N,D,45680,RELIANCE,2026-09-25 00:00:00,CE,1550,RELIANCE 25 SEP 2026 CE 1550,0.05,250,50000,1,RELIANCE,,
"""

MASTER_MCX = """Exch,ExchType,Scripcode,Name,Expiry,ScripType,StrikeRate,FullName,TickSize,LotSize,QtyLimit,Multiplier,SymbolRoot,ISIN,Series
M,D,255555,ALUMINI,2026-09-30 00:00:00,XX,0,ALUMINI 30 SEP 2026,0.05,1,10000,1000,ALUMINI,,
"""


# -- catalogue ---------------------------------------------------------------------------------------


def test_master_parses_equities_and_skips_non_eq_series():
    rows = parse_master(MASTER_EQ, Segment.NSE_EQ)
    ok = [i for i, good in rows if good]
    skipped = [i for i, good in rows if not good]
    assert {i.symbol for i in ok} == {"RELIANCE", "TCS"}
    assert skipped and skipped[0].symbol == "SOMEBOND"


def test_master_parses_options_with_strike_and_lot():
    rows = [i for i, ok in parse_master(MASTER_FO, Segment.NSE_FO) if ok]
    ce = [i for i in rows if i.option_type is OptionType.CE and i.strike == 1500][0]
    assert ce.kind is InstrumentKind.OPTION
    assert ce.lot_size == 250
    assert ce.underlying == "RELIANCE"
    assert ce.expiry == "2026-09-25"
    fut = [i for i in rows if i.option_type is OptionType.FUT]
    assert len(fut) == 2 and all(f.kind is InstrumentKind.FUTURE for f in fut)


def test_mcx_multiplier_is_carried_from_the_master():
    """The field that turns ₹99,943 into ₹99.9 million."""
    rows = [i for i, ok in parse_master(MASTER_MCX, Segment.MCX_FO) if ok]
    assert rows[0].multiplier == 1000
    assert rows[0].notional(349.45, 286) == 349.45 * 286 * 1000


def _catalogue() -> Catalogue:
    cat = Catalogue()
    for text, seg in ((MASTER_EQ, Segment.NSE_EQ), (MASTER_FO, Segment.NSE_FO)):
        for inst, ok in parse_master(text, seg):
            if not ok:
                continue
            cat.by_code[inst.scrip_code] = inst
            if inst.kind is InstrumentKind.EQUITY:
                cat.equity_by_symbol[inst.symbol] = inst
            elif inst.kind is InstrumentKind.FUTURE:
                cat.futures_by_symbol.setdefault(inst.underlying, []).append(inst)
            else:
                cat.options_by_symbol.setdefault(inst.underlying, []).append(inst)
    return cat


def test_front_future_is_resolved_live_and_skips_expired():
    """Stored future scrip codes expire monthly; that is what returned NO_DATA for 39 of 40
    scrips in the old OI gate."""
    cat = _catalogue()
    from datetime import date

    assert cat.front_future("RELIANCE", on=date(2026, 9, 1)).scrip_code == "54321"
    assert cat.front_future("RELIANCE", on=date(2026, 9, 26)).scrip_code == "54322"
    assert cat.front_future("RELIANCE", on=date(2026, 11, 1)) is None


def test_chain_is_filtered_by_type_and_sorted_by_strike():
    cat = _catalogue()
    ce = cat.chain("RELIANCE", "2026-09-25", OptionType.CE)
    assert [i.strike for i in ce] == [1500.0, 1550.0]
    assert cat.chain("RELIANCE", "2026-09-25", OptionType.PE)[0].strike == 1500.0


def test_symbol_lookup_never_returns_a_scrip_code_keyed_row():
    """P18: the class of bug where a symbol is passed into a numeric-code lookup."""
    cat = _catalogue()
    assert cat.equity("RELIANCE") is not None
    assert cat.get("RELIANCE") is None
    assert cat.get("2885") is not None


# -- aggregator ---------------------------------------------------------------------------------------


async def test_volume_is_the_delta_of_the_cumulative_total(equity):
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    t = ist_ts("2026-09-18", "09:15")
    agg.state[equity.scrip_code].connected_since = t - 60
    for total in (100, 250, 400):
        await agg.on_tick(
            {"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": total, "ts": t + 1}
        )
    forming = store.forming("RELIANCE", "1m")
    assert forming is not None
    assert forming.volume == 400.0  # 100 + 150 + 150, i.e. the deltas from a zero base


async def test_a_backwards_total_does_not_emit_negative_volume(equity):
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    t = ist_ts("2026-09-18", "09:15")
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 500, "ts": t})
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 100, "ts": t})
    assert store.forming("RELIANCE", "1m").volume >= 0


async def test_a_bucket_that_began_before_connect_is_tagged_partial(equity):
    """Excluded from determinism checks rather than looking like a volume collapse."""
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    t = ist_ts("2026-09-18", "11:00")
    agg.state[equity.scrip_code].connected_since = t + 30  # we connected mid-bucket
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 10, "ts": t + 40})
    assert store.forming("RELIANCE", "1m").source is BarSource.PARTIAL
    assert agg.partial_bars == 1


async def test_bar_closes_when_the_next_bucket_opens(equity):
    closed = []
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",), on_bar_close=lambda b: _collect(closed, b))
    agg.track(equity)
    t = ist_ts("2026-09-18", "09:15")
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 10, "ts": t})
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 101.0, "total_qty": 20, "ts": t + 61})
    assert len(closed) == 1
    assert ist_hm(closed[0].ts) == "09:15"
    assert closed[0].complete is True


async def _collect(sink, b):
    sink.append(b)


async def test_flush_stale_closes_a_bar_no_tick_would(equity):
    """An illiquid scrip that stops trading at 14:32 must not leave its 14:30 bar forming until
    the close."""
    closed = []
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",), on_bar_close=lambda b: _collect(closed, b))
    agg.track(equity)
    t = ist_ts("2026-09-18", "09:15")
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 10, "ts": t})
    assert await agg.flush_stale(now=t + 5) == 0
    assert await agg.flush_stale(now=t + 120) == 1
    assert closed and closed[0].complete


def test_seed_stamps_vwap_on_every_bar_not_just_the_last(equity):
    """CAN2 stamped VWAP on ``candles[-1]`` alone while reading it on three bars, so two of the
    three used a value left over from the previous cycle."""
    store = BarStore()
    agg = Aggregator(store, timeframes=("30m",))
    rows = [
        {"dt": "2026-09-18 09:15:00", "o": 100, "h": 101, "l": 99, "c": 100, "v": 100},
        {"dt": "2026-09-18 09:45:00", "o": 100, "h": 105, "l": 100, "c": 104, "v": 300},
        {"dt": "2026-09-18 10:15:00", "o": 104, "h": 106, "l": 103, "c": 105, "v": 200},
    ]
    from kotsin_nse.market.session import ist_naive_to_ts

    n = agg.seed(equity, "30m", rows, ts_of=ist_naive_to_ts)
    assert n == 3
    bars = store.bars("RELIANCE", "30m")
    assert all(b.vwap is not None for b in bars)
    assert bars[0].vwap != bars[-1].vwap


def test_store_seed_merges_rather_than_discarding_live_bars(equity):
    store = BarStore()
    t = ist_ts("2026-09-18", "09:15")
    store.close(bar(t + 1800, 10, 11, 9, 10))
    store.seed("RELIANCE", "30m", [bar(t, 9, 10, 8, 9)])
    assert store.count("RELIANCE", "30m") == 2


# -- periods ---------------------------------------------------------------------------------------------


def test_weekly_and_monthly_aggregate_from_dailies():
    days = [
        bar(ist_ts(f"2026-09-{d:02d}", "09:15"), 100 + d, 110 + d, 90 + d, 105 + d, tf="1d")
        for d in (14, 15, 16, 17, 18, 21, 22)
    ]
    wks = weekly(days)
    assert len(wks) == 2
    assert wks[0].high == max(b.high for b in days[:5])
    assert wks[0].close == days[4].close
    mons = monthly(days)
    assert len(mons) == 1


def test_previous_complete_excludes_the_running_period():
    """This week's pivot comes from last week's OHLC and must be fixed for the whole week."""
    days = [
        bar(ist_ts(f"2026-09-{d:02d}", "09:15"), 100, 110, 90, 105, tf="1d")
        for d in (14, 15, 16, 17, 18, 21, 22)
    ]
    from datetime import date

    prev = previous_complete(weekly(days), date(2026, 9, 22))
    assert prev is not None
    assert prev.end == date(2026, 9, 18)


def test_ist_day_is_used_for_grouping():
    t = ist_ts("2026-09-18", "23:00")  # still the 18th in IST, the 18th in UTC too
    assert ist_day(t).isoformat() == "2026-09-18"
    late = ist_ts("2026-09-18", "05:00")  # 23:30 UTC on the 17th
    assert ist_day(late).isoformat() == "2026-09-18"
    assert time.time() > 0


def test_tracking_a_future_alongside_its_cash_symbol_is_refused(equity):
    """Regression, found 2026-09-21. A future's `symbol` is its root — identical to the cash
    symbol — and BarStore is keyed by (symbol, tf). Tracking both wrote futures ticks into the
    equity's bars, so every SuperTrend and Bollinger value was computed on a mix of two
    instruments separated by the basis. Silent, and it corrupted the decision frame."""
    import pytest

    from kotsin_nse.domain import Instrument, OptionType

    fut = Instrument(
        scrip_code="54321",
        symbol="RELIANCE",
        segment=Segment.NSE_FO,
        kind=InstrumentKind.FUTURE,
        expiry="2026-09-25",
        option_type=OptionType.FUT,
        underlying="RELIANCE",
    )
    agg = Aggregator(BarStore(), timeframes=("1m",))
    agg.track(equity)
    with pytest.raises(ValueError, match="bar-series collision"):
        agg.track(fut)


def test_tracking_the_same_instrument_twice_is_a_no_op(equity):
    agg = Aggregator(BarStore(), timeframes=("1m",))
    agg.track(equity)
    agg.track(equity)
    assert len(agg.state) == 1


async def test_a_mid_session_connect_does_not_book_the_whole_day_into_one_bar(equity):
    """P-volume: connect at 14:10 and the first TotalQty is the day so far, not a bar's worth.

    Observed live on 2026-09-21: the engine started at 14:10 IST and RELIANCE's 14:15 bucket came
    out at 8,132,120 against the 714,679 the broker's own candle reported for the same window —
    the whole session's volume, booked into whichever bucket happened to be open at connect.
    """
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    t = ist_ts("2026-09-18", "14:15")
    agg.state[equity.scrip_code].connected_since = t - 5  # joined hours after the 09:15 open
    for total in (8_000_000, 8_000_150, 8_000_400):
        await agg.on_tick(
            {"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": total, "ts": t + 1}
        )
    forming = store.forming("RELIANCE", "1m")
    assert forming is not None
    # The first tick primes the baseline and books nothing; only the 150 + 250 traded while we
    # were watching is ours to claim.
    assert forming.volume == 400.0


async def test_a_partial_bar_never_overwrites_the_brokers_own_candle(equity):
    """The REST backfill lands first on a mid-session start; the partial bucket closes after it."""
    t = ist_ts("2026-09-18", "14:15")

    store = BarStore()
    rest = bar(t, 100.0, 101.0, 99.0, 100.5, 714_679.0)
    rest.source = BarSource.REST
    store.close(rest)

    partial = bar(t, 100.0, 101.0, 99.0, 100.5, 8_132_120.0)
    partial.source = BarSource.PARTIAL
    store.close(partial)

    held = store.bars("RELIANCE", "30m")
    assert len(held) == 1
    assert held[0].volume == 714_679.0
    assert held[0].source is BarSource.REST

    # ...but REST overwriting a partial, the direction the code was written for, still works.
    store2 = BarStore()
    p = bar(t, 100.0, 101.0, 99.0, 100.5, 1.0)
    p.source = BarSource.PARTIAL
    store2.close(p)
    r = bar(t, 100.0, 101.0, 99.0, 100.5, 714_679.0)
    r.source = BarSource.REST
    store2.close(r)
    assert store2.bars("RELIANCE", "30m")[0].volume == 714_679.0
