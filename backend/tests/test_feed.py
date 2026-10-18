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
