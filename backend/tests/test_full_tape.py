"""The full tape (operator, 2026-09-27): every NSE code the feed quotes, once a second on change,
written as new part files, MCX left out, day directories older than 60 days deleted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kotsin_nse.instrument.select import Quote
from kotsin_nse.ops.fulltape import FullTape, read_day

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 9, 28, 9, 45, tzinfo=IST).timestamp()


def test_a_row_only_when_the_price_bid_or_ask_changed(tmp_path):
    t = FullTape(tmp_path)
    q = {"1": Quote(ltp=10.0, bid=9.9, ask=10.1, ts=T0), "2": Quote(ltp=5.0, bid=4.95, ask=5.05, ts=T0)}
    assert t.sample(T0, q) == 2
    q["1"] = Quote(ltp=10.0, bid=9.9, ask=10.1, ts=T0 + 1)  # a new tick at the same prices: not a change
    assert t.sample(T0 + 1, q) == 0
    q["2"] = Quote(ltp=5.0, bid=4.95, ask=5.10, ts=T0 + 2)
    assert t.sample(T0 + 2, q) == 1
    assert t.write(t.take(), now=T0) == 3
    df = read_day(tmp_path, "2026-09-28")
    assert list(df.columns) == ["scrip_code", "ts", "ltp", "bid", "ask", "quote_ts"] and len(df) == 3
    assert df[df.scrip_code == "2"].ask.tolist() == [5.05, 5.10]


def test_outside_the_session_and_mcx_are_not_recorded(tmp_path):
    t = FullTape(tmp_path, skip=lambda code: code == "mcx")
    q = {"1": Quote(ltp=10.0, bid=9.9, ask=10.1, ts=T0), "mcx": Quote(ltp=5000.0, bid=4999.0, ask=5001.0, ts=T0)}
    assert t.sample(datetime(2026, 9, 28, 8, 59, tzinfo=IST).timestamp(), q) == 0, "before the session"
    assert t.sample(datetime(2026, 9, 28, 15, 45, tzinfo=IST).timestamp(), q) == 0, "after the close"
    assert t.sample(T0, q) == 1, "the NSE code, not the MCX one"


def test_fo_ticks_are_recorded_to_1540_after_the_1530_close(tmp_path):
    """Operator, 2026-10-17: "we will record tick till 15:40IST" — F&O trade to 15:40; the old 15:31 end lost
    every F&O tick after it. Only the recorder runs on: the session close (bars, signals) stays 15:30."""
    from kotsin_nse.config import Segment
    from kotsin_nse.market.session import spec
    from kotsin_nse.ops.fulltape import WINDOW

    assert WINDOW == ("09:14", "15:41") and spec(Segment.NSE_FO).close.strftime("%H:%M") == "15:30"
    t = FullTape(tmp_path)
    at = lambda h, m, s=0: datetime(2026, 9, 28, h, m, s, tzinfo=IST).timestamp()  # noqa: E731
    for k, (h, m, sec) in enumerate([(15, 29, 59), (15, 31, 30), (15, 35, 0), (15, 40, 59), (15, 41, 59)]):
        assert t.sample(at(h, m, sec), {"1": Quote(ltp=10.0 + k, bid=9.9 + k, ask=10.1 + k, ts=at(h, m, sec))}) == 1, f"{h}:{m}:{sec}"
    assert t.sample(at(15, 42), {"1": Quote(ltp=20.0, bid=19.9, ask=20.1, ts=at(15, 42))}) == 0, "15:42: the recorder has stopped"


@pytest.mark.asyncio
async def test_engine_b_records_a_post_close_hub_frame_and_its_oi_and_builds_no_bar_from_it(settings, monkeypatch):
    """Engine B has no broker socket of its own: it reads engine A's feed hub, which forwards every raw frame
    with no clock (15 Oct: 32,650 OI rows on 3,818 codes reached B between 15:30 and 15:45) — only the full
    tape's window threw B's F&O frames after 15:31 away. Through B's own tick path (two clocks: when we saw a
    frame, when it traded), a 15:39 option frame is on the full tape and its OI in the archive (operator,
    2026-10-17: "we will record tick till 15:40IST"); a 15:39 future frame builds no bar and leaves no late
    volume for the next one — the session close (bars, signals) stays 15:30."""
    import time as _time
    from datetime import date

    from kotsin_nse.bars.aggregator import Aggregator
    from kotsin_nse.bars.store import BarStore
    from kotsin_nse.config import Segment
    from kotsin_nse.domain import Instrument, InstrumentKind
    from kotsin_nse.market.session import ist_hm
    from kotsin_nse.ops.archive import read_day_frame
    from tests.test_limit_orders import OPT, _engine

    day = date.today()
    at = lambda h, m, s=0: datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST).timestamp()  # noqa: E731
    clock = [at(15, 39, 30)]
    monkeypatch.setattr(_time, "time", lambda: clock[0])
    e = await _engine(settings, clock)
    try:
        e.fulltape.take()
        await e._on_tick({"scrip_code": OPT.scrip_code, "ltp": 12.5, "bid": 12.45, "ask": 12.55, "exch": "N", "exch_type": "D",
                          "recv_ts": clock[0], "ts": clock[0] - 3, "total_qty": 50_000})
        assert e.quotes[OPT.scrip_code].ts == clock[0] and e.quotes[OPT.scrip_code].traded_ts == clock[0] - 3
        e._record_tape()
        rows = [r for r in e.fulltape.take() if r[0] == OPT.scrip_code]
        assert rows == [(OPT.scrip_code, int(clock[0]), 12.5, 12.45, 12.55, clock[0])], "15:39:30: on the full tape"
        await e._on_oi({"scrip_code": OPT.scrip_code, "open_interest": 1_250_000.0, "oi_change_pct": 1.5, "recv_ts": clock[0],
                        "tick_ts": clock[0] - 3})
        e.archive.flush(final=True)
        oi = read_day_frame(settings.data_dir / "archive", "oi", day.isoformat())
        assert len(oi) == 1 and oi.iloc[0].oi == 1_250_000.0, "the OI archive has no session cut"
    finally:
        await e.stop()

    fut = Instrument("52000", "RELIANCE", Segment.NSE_FO, InstrumentKind.FUTURE, name="RELIANCE 27 OCT 2026", lot_size=500,
                     tick_size=0.1, underlying="RELIANCE")
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m", "30m"))
    agg.track(fut)
    agg.state[fut.scrip_code].connected_since = at(9, 0)
    await agg.on_tick({"scrip_code": fut.scrip_code, "ltp": 1500.0, "total_qty": 1_000, "ts": at(15, 29, 30)})
    for h, m, s in ((15, 30, 0), (15, 35, 0), (15, 39, 59)):
        await agg.on_tick({"scrip_code": fut.scrip_code, "ltp": 1501.0, "total_qty": 2_000, "ts": at(h, m, s)})
    assert agg.out_of_session_ticks == 3 and not agg.state[fut.scrip_code].late_qty
    assert ist_hm(store.forming("RELIANCE", "1m").ts) == "15:29", "no bar after the 15:30 close"


def test_each_flush_is_a_new_file_and_nothing_is_rewritten(tmp_path):
    t = FullTape(tmp_path)
    for k in range(3):
        t.sample(T0 + 60 * k, {"1": Quote(ltp=10.0 + k, bid=9.9 + k, ask=10.1 + k, ts=T0 + 60 * k)})
        t.write(t.take(), now=T0)
    files = sorted((tmp_path / "2026-09-28").glob("*.parquet"))
    assert len(files) == 3 and len(read_day(tmp_path, "2026-09-28")) == 3
    assert t.write([], now=T0) == 0


def test_day_directories_older_than_60_days_are_deleted(tmp_path):
    t = FullTape(tmp_path, keep_days=60)
    for day in ("2026-07-29", "2026-07-30", "2026-09-27"):
        (tmp_path / day).mkdir()
        (tmp_path / day / "093000.parquet").write_bytes(b"x")
    assert t.prune(T0) == 1, "29 Jul is 61 days before 28 Sep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-07-30", "2026-09-27"]
    assert t.stats()["disk_mb"] >= 0 and t.stats()["keep_days"] == 60


def test_the_engine_records_the_feed_and_leaves_mcx_off(settings):
    from kotsin_nse.config import Segment
    from kotsin_nse.domain import Instrument, InstrumentKind
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    assert e.fulltape.enabled and e.fulltape.keep_days == 60 and settings.tape_full_flush_s == 120
    crude = Instrument("482", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, expiry="2099-12-31", underlying="CRUDEOIL")
    e.catalogue_loader.catalogue.by_code["482"] = crude
    assert e._fulltape_skip("482") is True and e._fulltape_skip("2885") is False


def test_a_recorder_fault_never_stops_the_clock_tick(settings):
    from kotsin_nse.engine import Engine

    e = Engine(settings)

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    e.fulltape.sample = boom  # type: ignore[method-assign]
    e._record_tape()  # must not raise: the depth sync after it in the clock still runs
    assert e.fulltape.errors == 1 and "disk gone" in e.fulltape.last_error
