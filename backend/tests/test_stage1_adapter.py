"""Stage 1 of the data-pipeline plan (2026-10-03): 5paisa's quirks handled once, at the edge — the
candle grid, the snapshot's own time, option volume from the feed, a broker date that is no date."""

from __future__ import annotations

import time

import pandas as pd
import pytest

from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.verify import BarReconciler
from kotsin_nse.config import Segment
from kotsin_nse.market.candles import snap_candles
from kotsin_nse.market.session import ist_naive_to_ts
from kotsin_nse.research.history import HistoryStore, guess_segment
from kotsin_nse.venue.fivepaisa.ws import FivePaisaFeed, parse_broker_date

from .conftest import bar, ist_ts


def _row(day: str, hm: str, c: float, v: float = 100.0) -> dict:
    return {"dt": f"{day}T{hm}:00", "o": c, "h": c, "l": c, "c": c, "v": v}


# -- the grid -------------------------------------------------------------------------------------


def test_a_candle_stamped_at_its_first_trade_is_its_bucket_and_off_session_rows_are_no_bars():
    rows = [
        _row("2026-09-21", "09:08", 1230.0),           # pre-open print
        _row("2026-09-21", "09:15", 1234.1),
        _row("2026-09-21", "10:46", 1233.3),           # the 10:45 bar, stamped at its first trade
        _row("2026-09-21", "14:45", 1246.6),
        _row("2026-09-21", "15:28", 1247.4, 9_471_626),  # the 15:15 auction, stamped at its first trade
        _row("2026-09-21", "15:50", 1247.4, 4_135),      # post-close: no bar
    ]
    s = snap_candles(rows, Segment.NSE_EQ, "30m")
    assert [r["dt"][11:16] for r in s.rows] == ["09:15", "10:45", "14:45", "15:15"]
    assert s.rows[1]["stamped"] == "2026-09-21T10:46:00" and s.moved == 2
    assert s.dropped == ["2026-09-21T09:08:00", "2026-09-21T15:50:00"]
    assert snap_candles(s.rows, Segment.NSE_EQ, "30m").rows == s.rows, "idempotent"


def test_a_buckets_own_on_grid_row_wins_and_daily_rows_pass_untouched():
    s = snap_candles([_row("2026-09-21", "10:46", 1.0), _row("2026-09-21", "10:45", 2.0)], Segment.NSE_EQ, "30m")
    assert [r["c"] for r in s.rows] == [2.0]
    daily = [_row("2026-09-21", "00:00", 5.0), _row("2026-09-22", "09:15", 6.0)]
    assert snap_candles(daily, Segment.NSE_EQ, "1d").rows == daily


class _Rest:
    def __init__(self, rows):
        self.rows = rows

    async def candles(self, inst, tf, start, end):
        return self.rows


@pytest.mark.asyncio
async def test_the_decision_reconcile_finds_a_candle_stamped_at_its_first_trade(equity):
    """It matched by exact time: the 10:46-stamped 10:45 bar was never found, the decision waited
    out the retry and ran on the snapshot-built bar."""
    store = BarStore()
    t = ist_ts("2026-09-18", "10:45")
    live = bar(t, 100, 101, 99, 100.75, 7)
    store.close(live)
    rest = _Rest([{"dt": "2026-09-18 10:46:00", "o": 100, "h": 100.5, "l": 99, "c": 100.45, "v": 5}])
    c = await BarReconciler(rest, store, lambda _s: equity, settle_delay_s=0.0, retry_delay_s=0.0).reconcile_bar(live)
    assert c.found and c.replaced and store.last(equity.symbol, "30m").close == 100.45


# -- the backtest cache ---------------------------------------------------------------------------


def test_the_cache_stores_the_grid_and_repairs_one_written_before_it(tmp_path):
    store = HistoryStore(tmp_path)
    raw = [_row("2026-09-21", "14:45", 1246.6), _row("2026-09-21", "15:28", 1247.4), _row("2026-09-21", "15:50", 1247.4)]
    # a cache written the old way: raw stamps
    store.save("RELIANCE", "30m", pd.DataFrame([{"ts": int(ist_naive_to_ts(r["dt"])), "o": r["o"], "h": r["h"], "l": r["l"],
                                                  "c": r["c"], "v": r["v"]} for r in raw]))
    out = store.normalize("RELIANCE", "30m", guess_segment(store.load("RELIANCE", "30m")))
    assert out == {"before": 3, "after": 2, "moved": 1, "dropped": 1}
    assert store.segment_of("RELIANCE") is Segment.NSE_EQ
    # and a fresh merge with its segment lands on the grid directly
    store.merge("TCS", "30m", raw, segment=Segment.NSE_EQ)
    assert len(store.load("TCS", "30m")) == 2


def test_an_mcx_series_is_recognised_and_kept_out_of_an_nse_backtest(tmp_path, settings):
    from kotsin_nse.research.backtest import Backtester, BacktestParams

    store = HistoryStore(tmp_path / "history")
    crude = [_row("2026-09-21", "21:00", 5600.0)]
    store.save("CRUDEOIL", "30m", pd.DataFrame([{"ts": int(ist_naive_to_ts(r["dt"])), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
                                                 for r in crude]))
    assert guess_segment(store.load("CRUDEOIL", "30m")) is Segment.MCX_FO
    store.set_segment("CRUDEOIL", Segment.MCX_FO)
    res = Backtester(settings, BacktestParams(segment=Segment.NSE_EQ)).run(store, ["CRUDEOIL"])
    assert res.trades == [] and res.signals == 0, "skipped, not replayed on NSE's session"


# -- quotes and timestamps ------------------------------------------------------------------------


def test_a_broker_date_that_is_no_date_is_none():
    assert parse_broker_date("/Date(1758271500000)/") == 1758271500.0
    assert parse_broker_date("/Date(-62135596800000)/") is None, ".NET's minimum date"
    assert parse_broker_date("/Date()/") is None and parse_broker_date(None) is None


def test_a_frame_without_tickdt_takes_its_arrival_never_seconds_of_day():
    tick = FivePaisaFeed._tick({"Token": 2885, "LastRate": 1300.0, "Time": 35_847}, arrived=1_790_000_000.0)
    assert tick["ts"] == 1_790_000_000.0


@pytest.mark.asyncio
async def test_the_snapshot_carries_the_brokers_trade_time_not_the_time_of_the_call(settings):
    from kotsin_nse.domain import Instrument, InstrumentKind
    from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST

    rest = FivePaisaREST.__new__(FivePaisaREST)
    rest.s = settings
    traded = time.time() - 240

    async def post(path, body):
        return {"Data": [{"Token": 45678, "LastRate": 7.0, "TickDt": f"/Date({int(traded * 1000)})/"},
                         {"Token": 45679, "LastRate": 8.0}]}

    rest._post = post  # type: ignore[method-assign]
    inst = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION)
    got = await rest.market_feed([inst, Instrument("45679", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION)])
    # two clocks (review, 2026-10-03): when we asked, and when the broker last traded
    assert got["45678"]["traded_ts"] == pytest.approx(traded, abs=0.01), "four minutes since the trade, and it says so"
    assert time.time() - got["45678"]["ts"] < 5, "observed now: an age guard reads when we saw it"
    assert got["45679"]["traded_ts"] == 0.0 and time.time() - got["45679"]["ts"] < 5, "no TickDt: an unknown trade time, never a guess"


@pytest.mark.asyncio
async def test_an_option_reads_its_volume_from_the_feed_and_a_cached_snapshot_never_rewinds_the_ltp(settings, option):
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    e._option_codes.add(option.scrip_code)
    now = time.time()
    await e._on_tick({"scrip_code": option.scrip_code, "ltp": 7.2, "bid": 7.1, "ask": 7.3, "total_qty": 125_000,
                      "last_qty": 250, "recv_ts": now, "ts": now, "exch": "N", "exch_type": "D"})
    assert e.option_volume[option.scrip_code] == 125_000.0
    e._apply_snapshot({option.scrip_code: {"ltp": 6.9, "bid": 0.0, "ask": 0.0, "volume": 120_000, "ts": now,
                                           "traded_ts": now - 5}}, now)
    assert e.ltps[option.scrip_code] == 7.2, "an older REST price is not a new trade"
    assert e._ltp_traded_ts[option.scrip_code] == now, "nor does it rewind the trade clock"
    assert e.option_volume[option.scrip_code] == 125_000.0
