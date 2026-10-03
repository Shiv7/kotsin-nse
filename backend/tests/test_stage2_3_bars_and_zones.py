"""Stages 2-3 of the data-pipeline plan (2026-10-03): volume that is never lost or double-counted, a
floor in the segment's own units, SuperTrend every reader agrees on, and ONE zone builder for live and
the backtest — fixed before the session, refusing a provisional candle and a corporate-action basis."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.daily import basis_ok
from kotsin_nse.bars.indicators import SUPERTREND_CONVERGED_BARS
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.bars.volume_read import FLOOR, VolBar, floor_for, slot_reading
from kotsin_nse.bars.zones import build_zones, is_provisional, session_tolerance
from kotsin_nse.config import Segment
from kotsin_nse.market.session import from_ist

from .conftest import ist_ts


def _at(day: str, hms: str) -> float:
    y, m, d = map(int, day.split("-"))
    h, mi, s = map(int, hms.split(":"))
    return from_ist(datetime(y, m, d, h, mi, s))


def _tick(equity, ts: float, total: int, ltp: float = 100.0, last: int = 10) -> dict:
    return {"scrip_code": equity.scrip_code, "ltp": ltp, "ts": ts, "total_qty": total, "last_qty": last}


# -- volume in the aggregator ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_opening_auction_primes_the_baseline_and_stays_out_of_the_0915_bar(equity):
    """A 09:07 total of 50,000 then 50,400 at 09:15:02 booked a 50,400 bar; the continuous 400 is it."""
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    agg.state[equity.scrip_code].connected_since = _at("2026-10-05", "09:00:00")
    await agg.on_tick(_tick(equity, _at("2026-10-05", "09:07:00"), 50_000))
    await agg.on_tick(_tick(equity, _at("2026-10-05", "09:15:02"), 50_400))
    assert store.forming(equity.symbol, "1m").volume == 400


@pytest.mark.asyncio
async def test_a_stale_frame_keeps_the_higher_baseline_and_counts_nothing_twice(equity):
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    st = agg.state[equity.scrip_code]
    st.connected_since = _at("2026-10-05", "09:00:00")
    for t, total in (("09:20:01", 200), ("09:20:05", 100), ("09:20:09", 250)):
        await agg.on_tick(_tick(equity, _at("2026-10-05", t), total))
    assert store.forming(equity.symbol, "1m").volume == 250, "200 + 50 — the stale 100 booked nothing"


@pytest.mark.asyncio
async def test_a_frame_late_for_its_bar_moves_its_volume_to_the_next_never_loses_it(equity):
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    agg.state[equity.scrip_code].connected_since = _at("2026-10-05", "09:00:00")
    await agg.on_tick(_tick(equity, _at("2026-10-05", "10:44:10"), 1_000))
    await agg.flush_stale(_at("2026-10-05", "10:45:01"))  # the clock closes 10:44
    await agg.on_tick(_tick(equity, _at("2026-10-05", "10:44:59"), 31_000))  # late: +30,000
    await agg.on_tick(_tick(equity, _at("2026-10-05", "10:45:20"), 31_100))
    assert store.forming(equity.symbol, "1m").volume == 30_100, "the late 30,000 lands in the next bar"


def test_the_volume_floor_is_in_the_segments_own_units():
    assert floor_for(Segment.NSE_EQ) == FLOOR == 1000.0
    assert floor_for(Segment.MCX_FO) == 10.0, "MCX volume is in lots (GOLD's 30m median ~214)"


def test_the_same_slot_reading_reads_a_bar_against_its_own_time_of_day():
    days = [f"2026-09-{d:02d}" for d in (14, 15, 16, 17, 18, 21)]
    bars = [VolBar(int(ist_ts(d, "10:45")), 100.0) for d in days[:-1]] + [VolBar(int(ist_ts(d, "14:45")), 900.0) for d in days[:-1]]
    t = int(ist_ts(days[-1], "10:45"))
    r = slot_reading([*bars, VolBar(t, 60.0)], t)
    assert r.ok and r.ratio == pytest.approx(0.6) and r.sessions == 5 and r.slot == "10:45"
    assert not slot_reading([VolBar(t, 60.0)], t).ok, "too few sessions: doubtful, never a number"


# -- SuperTrend: one window, every reader --------------------------------------------------------


def test_fudkii_reads_supertrend_over_the_converged_window(settings):
    from kotsin_nse.strategy.fudkii import FudkiiConfig

    asked: list[int] = []

    class Ctx:
        def bars(self, symbol, tf, n):
            asked.append(n)
            return []

    from kotsin_nse.strategy.fudkii import Fudkii

    Fudkii(FudkiiConfig()).on_bar(Ctx(), UnifiedBar("X", "1", "30m", 0, 1, 1, 1, 1, 1, complete=True))
    assert asked and asked[0] >= SUPERTREND_CONVERGED_BARS == 120


# -- one zone builder ----------------------------------------------------------------------------


def _daily(day: date, close: float, *, hm: str = "00:00", hi: float | None = None, lo: float | None = None) -> UnifiedBar:
    ts = from_ist(datetime(day.year, day.month, day.day, *map(int, hm.split(":"))))
    return UnifiedBar("X", "1", "1d", int(ts), close, hi or close * 1.01, lo or close * 0.99, close, 1e6,
                      source=BarSource.REST, complete=True)


def _weekdays(n: int, end: date) -> list[date]:
    from datetime import timedelta

    out, d = [], end
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return list(reversed(out))


def _thirty(day: date, close: float) -> list[UnifiedBar]:
    out = []
    for hm in ("09:15", "09:45", "10:15", "10:45", "11:15", "11:45", "12:15", "12:45", "13:15", "13:45", "14:15", "14:45"):
        ts = int(from_ist(datetime(day.year, day.month, day.day, *map(int, hm.split(":")))))
        out.append(UnifiedBar("X", "1", "30m", ts, close, close + 2, close - 2, close, 1e4, source=BarSource.REST, complete=True))
    return out


def test_the_width_is_fixed_before_the_session_so_a_restart_cannot_change_it():
    today = date(2026, 10, 6)
    days = _weekdays(40, today)
    intraday = [b for d in days for b in _thirty(d, 1000.0)]
    before = session_tolerance(intraday, today, 0.30)
    # the session runs: its own bars (a violent one) change nothing
    wild = [replace(b, high=b.close + 40, low=b.close - 40) for b in _thirty(today, 1000.0)]
    assert session_tolerance([*intraday, *wild], today, 0.30) == before


def test_one_builder_refuses_a_provisional_candle_and_a_corporate_action_basis():
    today = date(2026, 10, 6)
    days = _weekdays(40, today)
    intraday = [b for d in days for b in _thirty(d, 1000.0)]
    dailies = [_daily(d, 1000.0) for d in days]
    ok = build_zones(dailies, intraday, today, k=0.30, segment=Segment.NSE_EQ)
    assert ok.zones and not ok.refused

    provisional = [*dailies[:-1], _daily(days[-1], 1000.0, hm="09:15")]
    assert is_provisional(provisional[-1], Segment.NSE_EQ) and not is_provisional(provisional[-1], Segment.MCX_FO)
    assert build_zones(provisional, intraday, today, k=0.30, segment=Segment.NSE_EQ).refused == "provisional"

    adjusted = [replace(b, open=b.open * 0.374, high=b.high * 0.374, low=b.low * 0.374, close=b.close * 0.374) for b in dailies]
    got = build_zones(adjusted, intraday, today, k=0.30, segment=Segment.NSE_EQ)
    assert got.refused == "basis" and not got.zones, "VEDL ×0.374: zones at a third of the price"
    assert basis_ok(1000.0, 1050.0) and not basis_ok(374.0, 1000.0)


def test_the_backtest_and_live_build_identical_zones_from_identical_data(settings):
    from kotsin_nse.engine import Engine
    from kotsin_nse.market.session import ist_today
    from kotsin_nse.research.backtest import BacktestContext

    today = ist_today()
    days = _weekdays(40, today)
    dailies = [_daily(d, 1000.0 + (i % 7) * 4) for i, d in enumerate(days)]
    intraday = [b for i, d in enumerate(days) for b in _thirty(d, 1000.0 + (i % 7) * 4)]
    e = Engine(settings)
    e.store.seed("X", "1d", dailies)
    e.store.seed("X", "30m", intraday)
    live = e.zones_for("X")
    store = BarStore()
    store.seed("X", "30m", intraday)
    ctx = BacktestContext(store, {"X": dailies}, Segment.NSE_EQ, zone_k=e.volatility_regime("X").k)
    ctx.today = today
    assert live and [(round(z.price, 4), z.strength) for z in ctx.zones("X")] == [(round(z.price, 4), z.strength) for z in live]
