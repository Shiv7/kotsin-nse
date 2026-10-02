"""2026-09-28, after the 15:51 restart: two data faults the health page and the pivots surfaced.

* The daily series held the same session two and three times — 5paisa's provisional candle (stamped
  09:15, wrong volume), its end-of-day candle (stamped 00:00) and, for the indices, an 08:59
  pre-open row — so every index's weekly levels this week were built on a low the market never
  printed (NIFTY 22,282.65; the week's real low was 23,020.95).
* The first frame after a subscribe re-sends the last trade with its own time; for a name whose
  last trade sits in a bucket that ended before the boot, the aggregator opened that bucket and the
  clock closed it at once — 36 repeated closes, `bars_close_once` red all evening.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.daily import is_official, one_per_session, previous_session
from kotsin_nse.bars.periods import previous_complete, weekly
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import from_ist, ist_hm, session_open_ts

NIFTY = Instrument("999920000", "NIFTY", Segment.NSE_EQ, InstrumentKind.INDEX, name="NIFTY", underlying="NIFTY")
KOTAK = Instrument("1922", "KOTAKBANK", Segment.NSE_EQ, InstrumentKind.EQUITY, name="KOTAKBANK", underlying="KOTAKBANK")


def _ts(day: str, hm: str) -> float:
    return from_ist(datetime.fromisoformat(f"{day}T{hm}:00"))


def _d(inst: Instrument, day: str, hm: str, o: float, h: float, lo: float, c: float, v: float) -> UnifiedBar:
    return UnifiedBar(inst.symbol, inst.scrip_code, "1d", _ts(day, hm), o, h, lo, c, v, source=BarSource.REST, complete=True)


def _open(inst: Instrument):
    return lambda d: session_open_ts(inst.segment, d)


# -- the daily series: one bar per session -----------------------------------------------------------


def test_the_end_of_day_candle_wins_over_the_provisional_one():
    """KOTAKBANK as the store held it after the 15:51 boot: 23–25 Sep twice each."""
    cached = [
        _d(KOTAK, "2026-09-23", "00:00", 414.65, 417.25, 411.35, 413.25, 8_628_172),
        _d(KOTAK, "2026-09-23", "09:15", 414.95, 417.25, 411.35, 413.25, 19_218_260),
        _d(KOTAK, "2026-09-24", "00:00", 407.0, 410.0, 404.3, 405.0, 12_799_846),
        _d(KOTAK, "2026-09-24", "09:15", 407.0, 410.0, 404.3, 405.0, 31_485_347),
        _d(KOTAK, "2026-09-25", "09:15", 406.9, 407.95, 399.3, 404.0, 38_693_964),
    ]
    fresh = [
        _d(KOTAK, "2026-09-25", "00:00", 406.9, 407.95, 399.3, 404.0, 18_175_573),
        _d(KOTAK, "2026-09-28", "09:15", 399.95, 403.7, 395.75, 401.55, 46_399_297),
    ]
    out = one_per_session((cached, fresh), _open(KOTAK))
    assert [(b.ts, b.volume) for b in out] == [
        (_ts("2026-09-23", "00:00"), 8_628_172),
        (_ts("2026-09-24", "00:00"), 12_799_846),
        (_ts("2026-09-25", "00:00"), 18_175_573),
        (_ts("2026-09-28", "09:15"), 46_399_297),
    ]
    # and a provisional candle never displaces an end-of-day one, whichever supply carries it
    again = one_per_session((out, [_d(KOTAK, "2026-09-25", "09:15", 1, 1, 1, 1, 1)]), _open(KOTAK))
    assert next(b for b in again if b.ts >= _ts("2026-09-25", "00:00")).volume == 18_175_573


def test_the_pre_open_row_never_sets_an_index_weekly_level():
    """NIFTY week of 21–25 Sep: the 08:59 rows carried lows 700 points under the day's."""
    held = [
        _d(NIFTY, "2026-09-21", "00:00", 23300.0, 23489.0, 23250.0, 23420.0, 0),
        _d(NIFTY, "2026-09-22", "00:00", 23420.0, 23470.0, 23330.0, 23352.0, 0),
        _d(NIFTY, "2026-09-23", "00:00", 23352.15, 23466.9, 23349.55, 23446.8, 0),
        _d(NIFTY, "2026-09-23", "09:15", 23358.5, 23466.9, 23349.7, 23446.8, 0),
        _d(NIFTY, "2026-09-24", "00:00", 23221.8, 23281.95, 23046.15, 23063.1, 0),
        _d(NIFTY, "2026-09-24", "08:59", 23446.8, 23454.5, 22529.5, 23254.0, 0),
        _d(NIFTY, "2026-09-24", "09:15", 23220.25, 23280.6, 23046.2, 23063.1, 0),
        _d(NIFTY, "2026-09-25", "00:00", 23035.0, 23162.7, 23020.95, 23140.5, 0),
        _d(NIFTY, "2026-09-25", "08:59", 23063.1, 23063.1, 22282.65, 22981.65, 0),
        _d(NIFTY, "2026-09-25", "09:15", 23035.0, 23162.55, 23021.1, 23140.5, 0),
    ]
    week = previous_complete(weekly(held), date(2026, 9, 28))
    assert week.low == 22282.65, "the fault, as the live engine had it"
    clean = one_per_session((held,), _open(NIFTY))
    assert len(clean) == 5 and all(ist_hm(b.ts) == "00:00" for b in clean)
    week = previous_complete(weekly(clean), date(2026, 9, 28))
    assert (week.high, week.low, week.close) == (23489.0, 23020.95, 23140.5)
    assert previous_session(clean, date(2026, 9, 28)).low == 23020.95
    # a date whose only row is the pre-open one keeps it — nothing better exists
    lone = one_per_session(([_d(NIFTY, "2026-09-29", "08:59", 1, 2, 0.5, 1.5, 0)],), _open(NIFTY))
    assert len(lone) == 1


def test_a_fresh_provisional_candle_replaces_the_held_one():
    held = [_d(KOTAK, "2026-09-28", "09:15", 399.95, 403.7, 395.75, 401.0, 40_000_000)]
    fresh = [_d(KOTAK, "2026-09-28", "09:15", 399.95, 403.7, 395.75, 401.55, 46_399_297)]
    (only,) = one_per_session((held, fresh), _open(KOTAK))
    assert only is fresh[0]


def test_two_contracts_on_one_mcx_date_keep_the_traded_one(mcx_future):
    """NATURALGAS June: a far month's one-lot print and the front month's session on one date."""
    rows = [
        _d(mcx_future, "2026-06-18", "09:14", 306.9, 312.0, 305.2, 309.8, 39),
        _d(mcx_future, "2026-06-18", "22:13", 327.0, 327.0, 327.0, 327.0, 1),
    ]
    (only,) = one_per_session((rows,), _open(mcx_future))
    assert only.volume == 39


def test_the_engine_installs_one_bar_per_session_from_cache_and_rest(settings):
    e = Engine(settings)
    e.underlyings[KOTAK.symbol] = KOTAK
    cached = [
        _d(KOTAK, "2026-09-24", "00:00", 407.0, 410.0, 404.3, 405.0, 12_799_846),
        _d(KOTAK, "2026-09-24", "09:15", 407.0, 410.0, 404.3, 405.0, 31_485_347),
        _d(KOTAK, "2026-09-25", "09:15", 406.9, 407.95, 399.3, 404.0, 38_693_964),
    ]
    e.daily_cache.save(KOTAK.symbol, cached)
    e._seed_daily_from_cache([KOTAK])
    assert [b.volume for b in e.store.bars(KOTAK.symbol, "1d")] == [12_799_846, 38_693_964], "the cache's own doubles go at boot"
    e._seed_daily(KOTAK, [
        {"dt": "2026-09-24T00:00:00", "o": 407.0, "h": 410.0, "l": 404.3, "c": 405.0, "v": 12_799_846},
        {"dt": "2026-09-25T00:00:00", "o": 406.9, "h": 407.95, "l": 399.3, "c": 404.0, "v": 18_175_573},
        {"dt": "2026-09-28T09:15:00", "o": 399.95, "h": 403.7, "l": 395.75, "c": 401.55, "v": 46_399_297},
    ])
    held = e.store.bars(KOTAK.symbol, "1d")
    assert [b.volume for b in held] == [12_799_846, 18_175_573, 46_399_297] and all(is_official(b) for b in held)
    reloaded = e.daily_cache.load(KOTAK.symbol, KOTAK.scrip_code)
    assert [b.ts for b in reloaded] == [b.ts for b in held], "the disk cache heals with the store"


# -- the boot frame: a bucket that ended before we connected is never opened --------------------------


async def _agg(connected: float):
    closed: list[UnifiedBar] = []

    async def sink(b):
        closed.append(b)

    store = BarStore()
    agg = Aggregator(store, timeframes=("1m", "30m", "1d"), on_bar_close=sink)
    agg.track(KOTAK)
    agg.state[KOTAK.scrip_code].connected_since = connected
    return agg, store, closed


def _frame(ts: float, px: float = 401.55, total: int = 46_399_297) -> dict:
    return {"scrip_code": KOTAK.scrip_code, "ltp": px, "total_qty": total, "ts": ts}


@pytest.mark.asyncio
async def test_the_last_trade_resent_after_a_post_close_boot_is_not_a_bar():
    """15:51 boot; KOTAKBANK's first frame carries TickDt 15:29:58 — the 15:15 bucket, long decided."""
    agg, store, closed = await _agg(_ts("2026-09-28", "15:51"))
    store.seed(KOTAK.symbol, "30m", [UnifiedBar(KOTAK.symbol, KOTAK.scrip_code, "30m", int(_ts("2026-09-28", "15:15")),
                                                401.0, 402.0, 400.5, 401.55, 3e6, source=BarSource.REST, complete=True)])
    await agg.on_tick(_frame(_ts("2026-09-28", "15:29") + 58))
    await agg.flush_stale(_ts("2026-09-28", "15:51") + 1)
    assert closed == [] and agg.stale_frames == 3, "30m, 1m and the day: every one of them ended at 15:30"
    assert store.last(KOTAK.symbol, "30m").source is BarSource.REST
    assert agg.state[KOTAK.scrip_code].last_total_qty == 46_399_297, "the frame still primes the volume baseline"


@pytest.mark.asyncio
async def test_a_mid_session_boot_still_builds_the_bucket_it_joined():
    """10:05 boot: an illiquid name's 09:32 print is stale; the 09:45 bucket we joined is not."""
    agg, store, closed = await _agg(_ts("2026-09-28", "10:05"))
    await agg.on_tick(_frame(_ts("2026-09-28", "09:32"), total=1_000))
    assert store.forming(KOTAK.symbol, "30m") is None and agg.stale_frames == 2, "30m 09:15 and 1m 09:32 ended; the day did not"
    assert store.forming(KOTAK.symbol, "1d") is not None
    await agg.on_tick(_frame(_ts("2026-09-28", "10:06"), total=1_200))
    f = store.forming(KOTAK.symbol, "30m")
    assert f is not None and f.ts == int(_ts("2026-09-28", "09:45")) and f.source is BarSource.PARTIAL
    await agg.flush_stale(_ts("2026-09-28", "10:15") + 1)
    assert [b.ts for b in closed if b.tf == "30m"] == [int(_ts("2026-09-28", "09:45"))]


@pytest.mark.asyncio
async def test_the_engine_counts_no_repeated_close_after_a_post_close_boot(settings):
    e = Engine(settings)
    e.underlyings[KOTAK.symbol] = KOTAK
    e.aggregator.track(KOTAK)
    e.aggregator.state[KOTAK.scrip_code].connected_since = _ts("2026-09-28", "15:51")
    e._decided.add((KOTAK.symbol, int(_ts("2026-09-28", "15:15"))))  # the run before the restart decided it
    await e.aggregator.on_tick(_frame(_ts("2026-09-28", "15:29") + 58))
    await e.aggregator.flush_stale(_ts("2026-09-28", "15:51") + 1)
    assert e._duplicate_closes == 0
