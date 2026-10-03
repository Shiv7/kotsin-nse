"""Stage 4 of the data-pipeline plan (2026-10-03): the same answers, sooner — each speed-up pinned to
the result it must not change."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import date, timedelta

import pandas as pd
import pytest

from kotsin_nse.bars.daily import previous_session
from kotsin_nse.bars.unified import UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.market.session import bucket_start, ist_today, session_close_ts, session_open_ts
from kotsin_nse.ops.archive import DailyArchive, day_paths, read_day_frame

from .conftest import ist_ts

# -- the archive: parts today, one file once the day is over ---------------------------------------


def test_todays_flushes_write_parts_and_every_reader_sees_one_day(tmp_path):
    a = DailyArchive(tmp_path / "archive")
    now = time.time()
    a.oi("48900", now - 10, 40_000_000.0, 0.0)
    a.flush()
    a.oi("48900", now - 5, 40_100_000.0, 0.0)
    a.oi("61619", now - 5, 570_000.0, 0.0)
    a.flush()
    today = ist_today().isoformat()
    files = day_paths(tmp_path / "archive", "oi", today)
    assert len(files) == 2 and all(".part-" in p.name for p in files), "nothing re-read or rewritten"
    df = a.read_day("oi", today)
    assert len(df) == 3 and a.days("oi") == [today]
    a.flush(final=True)
    assert [p.name for p in day_paths(tmp_path / "archive", "oi", today)] == [f"{today}.parquet"], "compacted at the end"
    assert len(read_day_frame(tmp_path / "archive", "oi", today)) == 3


def test_a_finished_days_parts_are_folded_in_at_the_next_flush(tmp_path):
    root = tmp_path / "archive"
    a = DailyArchive(root)
    past = (ist_today() - timedelta(days=3)).isoformat()
    (root / "oi").mkdir(parents=True)
    # parts a crash left behind on a past day
    pd.DataFrame([{"scrip_code": "1", "ts": 1.0, "oi": 10.0, "change_pct": 0.0}]).to_parquet(root / "oi" / f"{past}.part-1.parquet")
    pd.DataFrame([{"scrip_code": "1", "ts": 2.0, "oi": 11.0, "change_pct": 0.0}]).to_parquet(root / "oi" / f"{past}.part-2.parquet")
    assert len(a.read_day("oi", past)) == 2, "read as they are before compaction"
    a.flush()
    assert [p.name for p in day_paths(root, "oi", past)] == [f"{past}.parquet"]
    assert list(a.read_day("oi", past).oi) == [10.0, 11.0]


def test_a_part_still_being_written_is_never_read(tmp_path):
    root = tmp_path / "archive"
    (root / "oi").mkdir(parents=True)
    today = ist_today().isoformat()
    (root / "oi" / f"{today}.part-9.tmp.parquet").write_bytes(b"half written")
    assert day_paths(root, "oi", today) == [] and DailyArchive(root).days("oi") == []


# -- the same bar, without converting every timestamp -----------------------------------------------


def test_previous_session_from_the_end_is_the_same_bar_in_any_order():
    days = [date(2026, 9, d) for d in (21, 22, 23, 24, 25, 28, 29, 30)]
    bars = [UnifiedBar("X", "1", "1d", int(ist_ts(d.isoformat(), "00:00")), 1, 1, 1, float(i), 1) for i, d in enumerate(days)]
    rng = random.Random(7)
    for _ in range(20):
        shuffled = bars[:]
        rng.shuffle(shuffled)
        today = rng.choice(days)
        want = [b for b in shuffled if b.ts < ist_ts(today.isoformat(), "00:00")]
        assert previous_session(shuffled, today) is (want[-1] if want else None)
        assert previous_session(iter(shuffled), today) is (want[-1] if want else None)


# -- the aggregator's bucket arithmetic is bucket_start's -------------------------------------------


@pytest.mark.asyncio
async def test_the_aggregators_buckets_are_bucket_starts_own(equity):
    from kotsin_nse.bars.aggregator import TIMEFRAMES, Aggregator
    from kotsin_nse.bars.store import BarStore

    store = BarStore()
    agg = Aggregator(store)
    agg.track(equity)
    day = date(2026, 10, 6)
    open_ts, close_ts = session_open_ts(Segment.NSE_EQ, day), session_close_ts(Segment.NSE_EQ, day)
    agg.state[equity.scrip_code].connected_since = open_ts - 60
    rng = random.Random(3)
    for ts in sorted(rng.uniform(open_ts, close_ts - 1) for _ in range(400)):
        await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "ts": ts, "total_qty": 0})
        for tf in TIMEFRAMES:
            cur = store.forming(equity.symbol, tf)
            assert cur is not None and cur.ts == int(bucket_start(Segment.NSE_EQ, ts, tf)), (tf, ts)


# -- REST at the trigger: the future's daily rows once a day -------------------------------------


@pytest.mark.asyncio
async def test_the_futures_daily_rows_are_fetched_once_a_day_not_once_a_trigger(settings, equity):
    from kotsin_nse.domain import Instrument, InstrumentKind
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    fut = Instrument("48900", equity.symbol, Segment.NSE_FO, InstrumentKind.FUTURE, expiry="2026-10-27", underlying=equity.symbol)
    calls: list[str] = []

    async def candles(inst, tf, start, end):
        calls.append(tf)
        return [{"dt": "2026-10-01T00:00:00", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1e5}] if tf == "1d" else []

    e.rest.candles = candles  # type: ignore[method-assign]
    sem = asyncio.Semaphore(4)
    for bucket in (1_000, 2_800, 4_600):
        await e._fut_context_fetch(equity, fut, bucket, sem)
    assert calls.count("1d") == 1 and calls.count("30m") == 3


@pytest.mark.asyncio
async def test_an_empty_daily_answer_is_asked_again_by_the_next_bar(settings, equity):
    """Review, 2026-10-03: an empty 200 was held for the whole day, and the future had no daily or
    weekly levels until midnight."""
    from kotsin_nse.domain import Instrument, InstrumentKind
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    fut = Instrument("48900", equity.symbol, Segment.NSE_FO, InstrumentKind.FUTURE, expiry="2026-10-27", underlying=equity.symbol)
    calls: list[str] = []

    async def candles(inst, tf, start, end):
        calls.append(tf)
        return []

    e.rest.candles = candles  # type: ignore[method-assign]
    sem = asyncio.Semaphore(4)
    for bucket in (1_000, 2_800, 4_600):
        await e._fut_context_fetch(equity, fut, bucket, sem)
    assert calls.count("1d") == 3


# -- the boot backfill: bounded, and the cached dailies are not asked for again --------------------


@pytest.mark.asyncio
async def test_the_backfill_runs_bounded_in_parallel_and_skips_current_dailies(settings):
    from dataclasses import replace

    from kotsin_nse.bars.unified import BarSource
    from kotsin_nse.domain import Instrument, InstrumentKind
    from kotsin_nse.engine import Engine

    e = Engine(settings.model_copy(update={"backfill_concurrency": 3}))
    names = [Instrument(str(1000 + i), f"S{i}", Segment.NSE_EQ, InstrumentKind.EQUITY) for i in range(8)]
    prev = e.calendar.previous_trading_day(ist_today())
    current = names[0]
    e.store.seed(current.symbol, "1d", [replace(UnifiedBar(current.symbol, current.scrip_code, "1d",
                                                           int(ist_ts(prev.isoformat(), "00:00")), 1, 1, 1, 1, 1),
                                                source=BarSource.REST, complete=True)])
    live = peak = 0
    asked: list[tuple[str, str]] = []

    async def candles(inst, tf, start, end):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        asked.append((inst.symbol, tf))
        await asyncio.sleep(0.01)
        live -= 1
        return []

    e.rest.candles = candles  # type: ignore[method-assign]
    await e._backfill(names)
    assert peak == 3, "never more than backfill_concurrency in flight"
    assert (current.symbol, "1d") not in asked and (current.symbol, "30m") in asked
    assert sum(1 for _, tf in asked if tf == "1d") == 7


@pytest.mark.asyncio
async def test_the_backfill_holds_twelve_sessions_whatever_the_holidays(settings, monkeypatch):
    """SuperTrend reads 120 bars (10 NSE sessions); 15 calendar days before Tue 6 Oct with 29 Sep and
    2 Oct shut held 9 sessions — 117 bars (review, 2026-10-03)."""
    from datetime import date

    import kotsin_nse.engine as engine_mod
    from kotsin_nse.domain import Instrument, InstrumentKind
    from kotsin_nse.engine import BACKFILL_SESSIONS, Engine
    from kotsin_nse.market.session import TradingCalendar

    e = Engine(settings)
    e.calendar = TradingCalendar(frozenset({date(2026, 9, 29), date(2026, 10, 2)}))
    tue = date(2026, 10, 6)
    monkeypatch.setattr(engine_mod, "ist_today", lambda: tue)
    starts: list[str] = []

    async def candles(inst, tf, start, end):
        if tf == "30m":
            starts.append(start)
        return []

    e.rest.candles = candles  # type: ignore[method-assign]
    await e._backfill([Instrument("1001", "S", Segment.NSE_EQ, InstrumentKind.EQUITY)])
    start = date.fromisoformat(starts[0][:10])
    sessions = sum(1 for k in range((tue - start).days) if e.calendar.is_trading_day(start + timedelta(days=k)))
    assert sessions >= BACKFILL_SESSIONS == 12


# -- Stage 5: a duty's failure is counted, and a programming error fails the tests ---------------


def test_a_guarded_duty_counts_its_failures_and_strict_mode_reraises_programming_errors(monkeypatch):
    from kotsin_nse.ops.guard import Guards

    g = Guards()
    with g.duty("broker"):
        raise TimeoutError("5paisa slow")
    assert g.snapshot()["broker"]["errors"] == 1, "a market or I/O failure is swallowed and counted"
    with pytest.raises(TypeError):
        with g.duty("held_quotes.refresh"):
            raise TypeError("is_open() missing 1 required positional argument: 'calendar'")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with g.duty("held_quotes.refresh"):
        raise TypeError("is_open() missing 1 required positional argument: 'calendar'")
    snap = g.snapshot()
    assert snap["held_quotes.refresh"]["programmingErrors"] == 2 and list(snap)[0] == "held_quotes.refresh"


def test_the_health_page_fails_on_a_recent_programming_error_and_a_basis_mismatch(settings, monkeypatch):
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with e.guards.duty("decide"):
        raise AttributeError("'ExposureVerdict' object has no attribute 'ok'")
    e.zone_refusals["VEDL"] = "basis: 2026-04-15 official close 286.6 vs its 30m close 766"
    checks = {c["name"]: c for c in e.health_snapshot()["checks"]}
    assert checks["duty_errors"]["ok"] is False and "ExposureVerdict" in checks["duty_errors"]["detail"]
    assert checks["zones"]["ok"] is False and "VEDL" in checks["zones"]["detail"]


def test_the_day_roll_clears_per_day_state_and_a_withheld_auction_bar_is_shown(settings):
    """Review, 2026-10-03: option_volume (a running max on the snapshot path) and the withheld 15:15
    bars (_basis_mismatch: written, never read) were never cleared or surfaced."""
    from datetime import date

    from kotsin_nse.engine import Engine

    e = Engine(settings)
    e._basis_mismatch["TMPV"] = ("2026-10-01", 286.6, 766.0)
    zones = e._zones_check()
    assert not zones.ok and "TMPV" in zones.detail and "auction bar withheld" in zones.detail
    e.option_volume["123"] = 125_000.0
    e._daily_provisional_asked["X"] = 1.0
    e._roll_day_state(date(2026, 10, 6))
    assert not e.option_volume and not e._basis_mismatch and not e._daily_provisional_asked
    assert e._zones_check().ok
