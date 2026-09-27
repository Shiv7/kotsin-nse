"""The full tape (operator, 2026-09-27): every NSE code the feed quotes, once a second on change,
written as new part files, MCX left out, day directories older than 60 days deleted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    crude = Instrument("482", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100, expiry="2026-10-17", underlying="CRUDEOIL")
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
