"""The OI pipeline end to end (operator, 2026-10-03): raw OI prints kept whole → our own OI candles →
the change measured from a previous close we can name (the exchange's bhavcopy, else our archive,
else today's pre-open print) — never 5paisa's change field, which is 0.0 on every frame."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date, datetime
from datetime import time as dt_time

import pandas as pd
import pytest

from kotsin_nse.bars.oi_candles import OiCandleBuilder, candles_from_prints
from kotsin_nse.bars.store import BarStore
from kotsin_nse.config import Segment
from kotsin_nse.market.fo_bhavcopy import OiDailyStore, backfill, fetch_day, parse, url_for
from kotsin_nse.market.session import from_ist
from kotsin_nse.ops.archive import DailyArchive
from kotsin_nse.ops.feed_rate import FeedRate
from kotsin_nse.research.backtest import BacktestContext
from kotsin_nse.research.oi_daily import futures_oi, with_changes
from kotsin_nse.strategy.base import Context
from tests.test_fukaa_inputs import _engine

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
          "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
          "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def _row(tp: str, token: int, sym: str, expiry: str, oi: int, chg: int, close: float, strike: str = "", opt: str = "") -> str:
    return (f"2026-10-01,2026-10-01,FO,NSE,{tp},{token},,{sym},,{expiry},{expiry},{strike},{opt},{sym}X,0,0,0,{close},"
            f"{close},{close},0,{close},{oi},{chg},10,0,0,F1,675,,,,,")


def _bhavcopy(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_0_0_0_20261001_F_0000.csv", "\n".join([HEADER, *rows]) + "\n")
    return buf.getvalue()


JSW = [
    _row("STF", 48900, "JSWSTEEL", "2026-10-27", 39_903_300, -448_200, 1236.6),
    _row("STF", 61619, "JSWSTEEL", "2026-11-23", 570_375, 6_075, 1245.0),
    _row("STO", 36059, "JSWSTEEL", "2026-10-27", 0, 0, 39.3, "890.00", "CE"),
]


def _at(d: date, hm: str) -> float:
    h, m = map(int, hm.split(":"))
    return from_ist(datetime.combine(d, dt_time(h, m)))


# -- the exchange's file ------------------------------------------------------------------------------


def test_the_bhavcopy_parses_to_one_row_per_contract_keyed_by_the_token_5paisa_uses():
    df = parse(_bhavcopy(JSW))
    assert list(df.scrip_code) == ["48900", "61619", "36059"]
    fut = df[df.scrip_code == "48900"].iloc[0]
    assert (fut.kind, fut.expiry, fut.oi, fut.oi_change) == ("FUTSTK", "2026-10-27", 39_903_300.0, -448_200.0)
    assert df[df.scrip_code == "36059"].iloc[0].option_type == "CE"
    with pytest.raises(ValueError):
        parse(b"<html>challenge</html>")


def test_the_store_answers_the_official_close_and_never_a_contract_nobody_holds(tmp_path):
    store = OiDailyStore(tmp_path / "oi_daily")
    d = date(2026, 10, 1)
    store.write(d, parse(_bhavcopy(JSW)))
    assert store.has(d) and store.days() == [d]
    assert store.closes(d, {"48900", "61619", "36059", "99999"}) == {"48900": 39_903_300.0, "61619": 570_375.0}
    assert store.closes(date(2026, 9, 30), {"48900"}) == {}


class _Resp:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code, self.content = status, content


class _Http:
    """Answers the bhavcopy of the days it holds; 404 for the rest."""

    def __init__(self, files: dict[date, bytes]) -> None:
        self.files = {url_for(d): b for d, b in files.items()}
        self.asked: list[str] = []

    async def get(self, url: str, **_: object) -> _Resp:
        self.asked.append(url)
        return _Resp(200, self.files[url]) if url in self.files else _Resp(404)


def test_a_day_nse_has_no_file_for_is_none_and_the_backfill_skips_weekends_and_held_days(tmp_path):
    store = OiDailyStore(tmp_path / "oi_daily")
    http = _Http({date(2026, 10, 1): _bhavcopy(JSW)})
    assert asyncio.run(fetch_day(http, date(2026, 10, 2))) is None, "a holiday has no file"
    out = asyncio.run(backfill(http, store, date(2026, 9, 30), date(2026, 10, 4), pace_s=0))
    assert out == {"fetched": 1, "held": 0, "missing": 2}, "30 Sep missing, 1 Oct fetched, 2 Oct none, 3-4 Oct a weekend"
    asked = len(http.asked)
    again = asyncio.run(backfill(http, store, date(2026, 10, 1), date(2026, 10, 1), pace_s=0))
    assert again == {"fetched": 0, "held": 1, "missing": 0} and len(http.asked) == asked, "a held day is not asked again"


# -- our own OI candles ---------------------------------------------------------------------------


def test_oi_candles_are_built_from_the_prints_inside_the_session_only():
    d = date(2026, 10, 5)
    b = OiCandleBuilder()
    assert b.on_print("48900", 39_903_300, _at(d, "09:05"), Segment.NSE_FO) == [], "a pre-open print is not a candle"
    b.on_print("48900", 40_000_000, _at(d, "09:15"), Segment.NSE_FO)
    b.on_print("48900", 40_100_000, _at(d, "09:15") + 30, Segment.NSE_FO)
    b.on_print("48900", 39_950_000, _at(d, "09:15") + 50, Segment.NSE_FO)
    closed = b.on_print("48900", 40_200_000, _at(d, "09:16") + 5, Segment.NSE_FO)
    assert [(c.tf, c.o, c.h, c.l, c.c, c.prints) for c in closed] == [("1m", 40_000_000, 40_100_000, 39_950_000, 39_950_000, 3)]
    b.on_print("48900", 40_300_000, _at(d, "09:45") + 1, Segment.NSE_FO)
    thirty = b.closed("48900", "30m")
    assert len(thirty) == 1 and (thirty[0].o, thirty[0].c) == (40_000_000, 40_200_000), "09:15–09:45 rolled from the same prints"
    assert b.forming("48900", "1d").c == 40_300_000 and b.forming("48900", "1d").o == 40_000_000
    assert b.stats()["out_of_session"] == 1


def test_a_contract_that_stops_printing_has_its_candles_closed_by_the_clock():
    d = date(2026, 10, 5)
    b = OiCandleBuilder()
    b.on_print("48900", 40_000_000, _at(d, "14:31"), Segment.NSE_FO)
    assert b.flush(_at(d, "14:31") + 30) == []
    closed = b.flush(_at(d, "15:31"))
    assert sorted(c.tf for c in closed) == ["1d", "1m", "30m"], "the session is over: nothing stays forming"
    assert b.closed("48900", "1d")[-1].c == 40_000_000, "the day candle's close is the session's last OI"


def test_candles_rebuilt_from_the_archive_match_the_live_ones():
    d = date(2026, 10, 5)
    prints = [("48900", _at(d, "09:15") + 10 * k, 40_000_000 + 1_000 * k) for k in range(40)]
    live = OiCandleBuilder(timeframes=("1m",), keep={"1m": 1000})
    for code, ts, oi in prints:
        live.on_print(code, oi, ts, Segment.NSE_FO)
    live.flush(float("inf"))
    rebuilt = candles_from_prints(prints, "1m")
    assert [(c.ts, c.o, c.h, c.l, c.c) for c in rebuilt] == [(c.ts, c.o, c.h, c.l, c.c) for c in live.closed("48900", "1m")]


# -- the raw frame, kept whole ----------------------------------------------------------------------


def test_the_archive_keeps_every_field_of_the_oi_frame_including_5paisas_own_change(tmp_path):
    a = DailyArchive(tmp_path / "archive")
    ts = _at(date(2026, 10, 5), "10:00")
    a.oi("48900", ts, 40_000_000.0, 0.0, change=96_500.0, tick_ts=ts - 2, ltp=1240.5, volume=1_234_567.0)
    a.flush(final=True)
    df = pd.read_parquet(tmp_path / "archive" / "oi" / "2026-10-05.parquet")
    row = df.iloc[0]
    assert (row.oi, row.change_pct, row.change, row.ltp, row.volume) == (40_000_000.0, 0.0, 96_500.0, 1240.5, 1_234_567.0)
    assert row.tick_ts == ts - 2


# -- the previous close, and where it came from -------------------------------------------------------


def _archive_prints(settings, day: date, rows: list[tuple[str, str, float]]) -> None:
    oi_dir = settings.data_dir / "archive" / "oi"
    oi_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"scrip_code": c, "ts": _at(day, hm), "oi": v, "change_pct": 0.0} for c, hm, v in rows]).to_parquet(
        oi_dir / f"{day}.parquet")


def test_the_exchanges_close_comes_first_then_our_archive_then_the_pre_open_print(settings):
    e = _engine(settings)
    today = date(2026, 10, 5)
    prev = e.calendar.previous_trading_day(today)
    e.oi_daily.write(prev, parse(_bhavcopy([JSW[0]])))  # the exchange has the near month only
    _archive_prints(settings, prev, [("48900", "15:29", 40_321_800.0), ("61619", "15:29", 570_000.0)])
    assert e._seed_oi_reference(today=today) == 2
    assert e._oi_ref == {"48900": 39_903_300.0, "61619": 570_000.0}
    assert e._oi_ref_src == {"48900": "nse", "61619": "archive"}
    e._note_oi("48900", 39_903_300.0 * 1.01, _at(today, "10:00"))
    r = e.oi_reading("JSWSTEEL", now=_at(today, "10:00") + 5)
    assert r.ok and r.ref_source == "nse" and r.change_pct == pytest.approx(1.0, abs=1e-6)


def test_the_bhavcopy_arriving_later_replaces_the_archive_reference_with_the_official_one(settings, monkeypatch):
    e = _engine(settings)
    today = date(2026, 10, 5)
    prev = e.calendar.previous_trading_day(today)
    _archive_prints(settings, prev, [("48900", "15:29", 40_321_800.0)])
    e._seed_oi_reference(today=today)
    assert e._oi_ref_src["48900"] == "archive"

    async def fake_fetch(http, day):
        return parse(_bhavcopy(JSW)) if day == prev else None

    monkeypatch.setattr("kotsin_nse.engine.fetch_fo_bhavcopy", fake_fetch)
    assert asyncio.run(e._ensure_oi_bhavcopy(today=today)) is True
    assert e._oi_ref["48900"] == 39_903_300.0 and e._oi_ref_src["48900"] == "nse"
    assert e.oi_daily.has(prev) and e._oi_bhav_day == prev


def test_no_file_yet_keeps_the_references_it_has(settings, monkeypatch):
    e = _engine(settings)
    today = date(2026, 10, 5)
    _archive_prints(settings, e.calendar.previous_trading_day(today), [("48900", "15:29", 40_321_800.0)])
    e._seed_oi_reference(today=today)

    async def none(http, day):
        return None

    monkeypatch.setattr("kotsin_nse.engine.fetch_fo_bhavcopy", none)
    assert asyncio.run(e._ensure_oi_bhavcopy(today=today)) is False
    assert e._oi_ref == {"48900": 40_321_800.0} and e._oi_bhav_day is None


def test_a_futures_print_builds_its_oi_candles_and_the_view_shows_reference_and_source(settings):
    e = _engine(settings)
    today = date(2026, 10, 5)
    e.oi_daily.write(e.calendar.previous_trading_day(today), parse(_bhavcopy(JSW)))
    e._seed_oi_reference(today=today)
    e._note_oi("48900", 40_000_000.0, _at(today, "09:20"))
    assert e.oi_candles.forming("48900", "1m").c == 40_000_000.0
    leg = next(x for x in e.oi_view("JSWSTEEL")["legs"] if x["code"] == "48900")
    assert (leg["ref"], leg["refSource"], leg["oi"]) == (39_903_300.0, "nse", 40_000_000.0)


# -- the feed probe -------------------------------------------------------------------------------


def test_a_feed_that_sends_every_trade_shows_all_the_volume_and_a_conflated_one_does_not():
    every, conflated = FeedRate(), FeedRate()
    total = 1_000
    for k in range(60):
        total += 10
        every.on_tick("A", "NSE_EQ", 100.0 + k, 10, total)  # one frame per 10-share trade
    total = 1_000
    for k in range(60):
        total += 30  # three 10-share trades between frames; the frame shows the last one
        conflated.on_tick("A", "NSE_EQ", 100.0 + 9 * k, 10, total)
    e, c = every.snapshot()["NSE_EQ"], conflated.snapshot()["NSE_EQ"]
    assert e["volume_seen_pct"] == pytest.approx(100.0) and e["verdict"].startswith("tick-by-tick")
    assert c["volume_seen_pct"] == pytest.approx(33.3, abs=0.1) and c["verdict"].startswith("conflated")
    assert c["frames_per_min_median"] == pytest.approx(6.8, abs=0.1) and c["gap_s_median"] == 9.0


# -- the backtester ---------------------------------------------------------------------------------


def test_every_context_answers_everything_a_strategy_may_ask(settings):
    from kotsin_nse.engine import AsOfContext, Engine

    asked = {n for n, v in vars(Context).items() if not n.startswith("_") and (callable(v) or isinstance(v, property))}
    ctx = BacktestContext(BarStore(), {}, Segment.NSE_EQ)
    for impl in (ctx, AsOfContext(Engine(settings))):
        missing = sorted(n for n in asked if not hasattr(impl, n))
        assert not missing, f"{type(impl).__name__}: a Context method it cannot answer fails every symbol: {missing}"
    assert not ctx.oi_reading("RELIANCE").ok and ctx.oi_reading("RELIANCE").doubt
    assert not ctx.volume_reading("RELIANCE", 0).ok


def test_a_run_where_every_symbol_fails_is_an_error_not_an_empty_result(settings, monkeypatch, tmp_path):
    from kotsin_nse.research import backtest as bt
    from kotsin_nse.research.history import HistoryStore

    def boom(self, *a, **k):
        raise AttributeError("'BacktestContext' object has no attribute 'something_new'")

    monkeypatch.setattr(bt.Backtester, "_run_symbol", boom)
    runner = bt.Backtester(settings, bt.BacktestParams())
    with pytest.raises(RuntimeError, match="every one of 2 symbols failed"):
        runner.run(HistoryStore(tmp_path / "history"), ["RELIANCE", "TCS"])


# -- the daily OI history -------------------------------------------------------------------------


def test_the_daily_table_sums_every_expiry_and_names_the_quadrant(tmp_path):
    store = OiDailyStore(tmp_path / "oi_daily")
    store.write(date(2026, 9, 30), parse(_bhavcopy([
        _row("STF", 48900, "JSWSTEEL", "2026-10-27", 40_351_500, 0, 1250.0),
        _row("STF", 61619, "JSWSTEEL", "2026-11-23", 564_300, 0, 1258.0)])))
    store.write(date(2026, 10, 1), parse(_bhavcopy(JSW)))
    t = with_changes(futures_oi(store, ["JSWSTEEL"]))
    first, last = t.iloc[0], t.iloc[-1]
    assert first.quadrant is None, "no previous close, no quadrant — NaN must not read as a direction"
    assert last.total_oi == 39_903_300 + 570_375
    assert last.oi_chg_pct == pytest.approx((-448_200 + 6_075) / (40_473_675 - (-442_125)) * 100.0)
    assert last.px_chg_pct < 0 and last.quadrant == "long unwinding"


def test_a_level_resent_at_subscribe_is_as_old_as_its_trade_not_its_arrival(settings):
    """At a subscribe 5paisa re-sends each contract's last print: on a Saturday JSWSTEEL's 1 Oct level
    arrived 'now' and read +1.05 % against the official close. Aged by its own time it is doubtful."""
    import asyncio as _asyncio

    e = _engine(settings)
    today = date(2026, 10, 3)
    e.oi_daily.write(e.calendar.previous_trading_day(today), parse(_bhavcopy(JSW)))
    e._seed_oi_reference(today=today)
    arrived = _at(today, "16:40")
    printed = _at(date(2026, 10, 1), "15:29")
    _asyncio.run(e._on_oi({"scrip_code": "48900", "open_interest": 40_321_800, "oi_change": 0, "oi_change_pct": 0.0,
                           "ts": printed, "recv_ts": arrived, "ltp": 1236.6, "volume": 0}))
    r = e.oi_reading("JSWSTEEL", now=arrived + 1)
    assert not r.ok and "old" in r.doubt
