"""The market-data seam: one interface whoever streams the data, and a meter that measures how
often that source really updates each contract instead of trusting its brochure."""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from kotsin_nse.config import Settings
from kotsin_nse.engine import Engine
from kotsin_nse.feed import build_source
from kotsin_nse.feed.base import Channel
from kotsin_nse.feed.fivepaisa import FivePaisaSource
from kotsin_nse.feed.rate import FeedRateMeter


def _tick(code: str, recv: float, total: int, ltp: float = 100.0, source: str = "fivepaisa") -> dict:
    return {
        "source": source,
        "scrip_code": code,
        "ltp": ltp,
        "total_qty": total,
        "last_qty": 0,
        "bid": ltp - 0.05,
        "ask": ltp + 0.05,
        "bid_qty": 10,
        "ask_qty": 10,
        "open": ltp,
        "high": ltp,
        "low": ltp,
        "prev_close": ltp,
        "ts": recv,
        "recv_ts": recv,
    }


# -- rate meter -------------------------------------------------------------------------------------


def test_rate_meter_counts_frames_and_the_frames_that_carried_volume():
    m = FeedRateMeter()
    # one frame every 10 s for 5 minutes; volume moves on every other frame
    for i in range(31):
        m.on_quote("fivepaisa", "2885", "NSE_EQ equity", 1000.0 + 10 * i, 100 * (i // 2))
    row = m.snapshot()["fivepaisa"]["NSE_EQ equity"]
    assert row["instruments"] == 1 and row["measured"] == 1
    assert row["frames_per_min"] == pytest.approx(31 / 5, rel=1e-3)
    assert row["trades_per_min"] == pytest.approx(15 / 5, rel=1e-3)
    assert row["gap_median_s"] == 10.0 and row["gap_p90_s"] == 10.0


def test_rate_meter_groups_by_source_and_never_mixes_them():
    m = FeedRateMeter()
    for i in range(121):
        m.on_quote("fivepaisa", "1", "NSE_FO future", 1000.0 + 9 * i, i)
        m.on_quote("vendor", "1", "NSE_FO future", 1000.0 + i, i)
    snap = m.snapshot()
    assert snap["fivepaisa"]["NSE_FO future"]["gap_median_s"] == 9.0
    assert snap["vendor"]["NSE_FO future"]["gap_median_s"] == 1.0


def test_rate_meter_waits_for_a_minute_before_quoting_a_rate():
    m = FeedRateMeter()
    for i in range(3):
        m.on_quote("fivepaisa", "1", "g", 1000.0 + i, 0)
    row = m.snapshot()["fivepaisa"]["g"]
    assert row["frames_per_min"] is None and row["gap_median_s"] is None
    m.reset()
    assert m.snapshot() == {}


# -- source selection and the 5paisa adapter ----------------------------------------------------------


def test_only_sources_with_an_adapter_are_accepted():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_source="accelpix")
    assert Settings(_env_file=None).data_source == "fivepaisa"


def test_the_5paisa_source_maps_channels_and_stamps_its_name(settings, equity):
    seen: list[dict] = []

    async def on_tick(t):
        seen.append(t)

    src = build_source(settings, auth=None, on_tick=on_tick)  # type: ignore[arg-type]
    assert isinstance(src, FivePaisaSource)
    assert src.capabilities.trade_side is False and src.capabilities.snapshots is True

    import asyncio

    async def go():
        await src.subscribe(Channel.QUOTES, [equity])
        await src.subscribe(Channel.DEPTH, [equity])
        await src.feed._dispatch({"Token": 2885, "LastRate": 101.5, "TotalQty": 500})

    asyncio.run(go())
    assert src.health.subscriptions == {"mf": 1, "md": 1, "oi": 0}
    assert seen and seen[0]["source"] == "fivepaisa" and seen[0]["scrip_code"] == "2885"


# -- the engine's tick path -------------------------------------------------------------------------------


async def test_engine_meters_every_quote_and_archives_underlyings_but_not_options(settings, equity, option):
    e = Engine(settings)
    await e.start()
    try:
        e._inst_by_code = {equity.scrip_code: equity, option.scrip_code: option}
        e._tick_archive_codes = {equity.scrip_code}
        for i in range(13):
            await e._on_tick(_tick(equity.scrip_code, 1_790_000_000.0 + 10 * i, 100 * i))
            await e._on_tick(_tick(option.scrip_code, 1_790_000_000.0 + 10 * i, 5 * i, ltp=12.0))
        rate = e.feed_rate.snapshot()["fivepaisa"]
        assert set(rate) == {"NSE_EQ equity", "NSE_FO option"}
        assert rate["NSE_EQ equity"]["gap_median_s"] == 10.0
        assert e.ltps[equity.scrip_code] == 100.0
        e.archive.flush(final=True)
        ticks = pd.read_parquet(next((settings.data_dir / "archive" / "ticks").glob("*.parquet")))
        assert set(ticks["scrip_code"]) == {equity.scrip_code}
        assert len(ticks) == 13 and set(ticks["source"]) == {"fivepaisa"}
    finally:
        await e.stop()


async def test_engine_measures_order_flow_on_watched_contracts_and_archives_closed_minutes(settings, equity):
    e = Engine(settings)
    await e.start()
    try:
        e._inst_by_code = {equity.scrip_code: equity}
        e._tick_archive_codes = {equity.scrip_code}
        t0 = 1_790_000_040.0
        await e._on_tick(_tick(equity.scrip_code, t0, 1_000, ltp=100.0))
        await e._on_tick(_tick(equity.scrip_code, t0 + 10, 1_300, ltp=100.05))  # at the previous ask
        await e._on_tick(_tick(equity.scrip_code, t0 + 70, 1_400, ltp=99.95))  # next minute, at the bid
        first = e.flow.minutes(equity.scrip_code)[0]
        assert first.buy == 300 and first.sell == 0
        closed = e.flow.drain()
        assert [m.ts for m in closed] == [int(t0)]
    finally:
        await e.stop()


async def test_bar_oi_comes_from_the_front_future_only(settings, equity):
    """RELIANCE's 1m bars flipped between the near (139.8M) and next-month (27.4M) OI on
    2026-10-19: every future of the root stamped the bars. Only the front month may."""
    e = Engine(settings)
    await e.start()
    try:
        e.underlyings = {equity.symbol: equity}
        e.aggregator.track(equity)
        e._future_to_underlying = {"F1": equity.symbol, "F2": equity.symbol}
        e._front_future = {"F1": equity.symbol}
        e.oi_ref = {"F1": 100_000_000.0}
        await e._on_oi({"scrip_code": "F1", "open_interest": 139_800_000, "oi_change_pct": 0.0, "recv_ts": 1.0})
        await e._on_oi({"scrip_code": "F2", "open_interest": 27_400_000, "oi_change_pct": 0.0, "recv_ts": 2.0})
        st = e.aggregator.state[equity.scrip_code]
        assert st.oi == 139_800_000 and st.fut_scrip_code == "F1"
        assert round(e.fut_oi_change[equity.symbol], 1) == 39.8
    finally:
        await e.stop()


def test_closing_oi_is_only_a_value_recorded_near_the_close(tmp_path):
    """2026-10-19 the engine dropped MCX at midday: that OI is not the close and must not be used."""
    from kotsin_nse.ops.archive import DailyArchive

    from .conftest import ist_ts

    arc = DailyArchive(tmp_path)
    close = ist_ts("2026-10-19", "23:30")
    arc.oi("A", close - 600, 1000.0, 0.0)  # recorded at 23:20
    arc.oi("A", close - 60, 1010.0, 0.0)
    arc.oi("B", close - 6 * 3600, 500.0, 0.0)  # last seen at 17:30
    arc.flush(final=True)
    assert arc.closing_oi("2026-10-19", ["A", "B", "C"], close - 900) == {"A": 1010.0}
    assert arc.closing_oi("2026-10-18", ["A"], close - 900) == {}


def test_first_oi_of_the_day_is_the_restart_reference(tmp_path):
    from kotsin_nse.ops.archive import DailyArchive

    from .conftest import ist_ts

    arc = DailyArchive(tmp_path)
    t = ist_ts("2026-10-20", "18:50")
    arc.oi("A", t + 60, 1005.0, 0.0)
    arc.oi("A", t, 1000.0, 0.0)
    arc.flush(final=True)
    assert arc.first_oi("2026-10-20", ["A", "B"]) == {"A": (t, 1000.0)}


def test_depth_keeps_twenty_levels_a_side(tmp_path):
    import pandas as pd

    from kotsin_nse.ops.archive import DailyArchive

    from .conftest import ist_ts

    arc = DailyArchive(tmp_path, depth_levels=20)
    t = ist_ts("2026-10-26", "10:00")
    arc.depth("A", t, [(100 - i * 0.05, 10 + i) for i in range(20)], [(100.05 + i * 0.05, 20 + i) for i in range(3)])
    arc.flush(final=True)
    row = pd.read_parquet(next((tmp_path / "depth").glob("*.parquet"))).iloc[0]
    assert row["b20q"] == 29 and row["a3q"] == 22 and pd.isna(row["a4p"])


def test_rows_added_while_a_flush_runs_are_kept_for_the_next(tmp_path, monkeypatch):
    """The flush swaps the buffer and owns what it took; a row the loop adds meanwhile waits."""
    from kotsin_nse.ops.archive import DailyArchive

    from .conftest import ist_ts

    arc = DailyArchive(tmp_path)
    t = ist_ts("2026-10-23", "10:00")
    arc.oi("A", t, 1000.0, 0.0)
    real_write = arc._write

    def slow_write(kind, day, rows):
        arc.oi("B", t + 1, 2000.0, 0.0)  # the loop adds a row while this flush is writing
        return real_write(kind, day, rows)

    monkeypatch.setattr(arc, "_write", slow_write)
    assert arc.flush() == 1 and arc.rows_buffered == 1
    monkeypatch.setattr(arc, "_write", real_write)
    assert arc.flush() == 1
    assert set(arc.first_oi("2026-10-23", ["A", "B"])) == {"A", "B"}
