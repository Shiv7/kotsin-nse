"""Frames with no broker time build no bar (2026-10-19). This engine stamped a frame whose TickDt was missing,
unparseable or .NET's minimum date with its ARRIVAL time; the snapshot burst of every broker resubscribe then
built flat bars at Friday's prices whenever it arrived inside a session's hours — Saturdays and Sundays from
3 Oct, the 2 Oct holiday, and Monday 12 Oct's levels from them (121 of 122 triggers graded F). Engine A keeps
the negative date and drops the frame. ``Settings.bars_refuse_untimed`` (default on) refuses them before they
can roll the day, prime the volume baseline or open a bucket; quotes still take them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from structlog.testing import capture_logs

from kotsin_nse.bars.aggregator import Aggregator, RefusedSampler
from kotsin_nse.bars.store import BarStore
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.market.session import bucket_start
from kotsin_nse.venue.fivepaisa.ws import FivePaisaFeed

IST = timezone(timedelta(hours=5, minutes=30))


def _ts(m: int, d: int, hh: int, mm: int, ss: int = 0) -> float:
    return datetime(2026, m, d, hh, mm, ss, tzinfo=IST).timestamp()


SAT_NSE = _ts(10, 17, 11, 7, 33)  # engine A's broker reconnect, Saturday 17 Oct
SAT_MCX = _ts(10, 17, 19, 51, 59)
BOOT = _ts(10, 16, 1, 0)
EQ = Instrument("13061", "X360ONE", Segment.NSE_EQ, InstrumentKind.EQUITY, name="360ONE", lot_size=1)
MCX = Instrument("574844", "CARDAMOM", Segment.MCX_FO, InstrumentKind.FUTURE, name="CARDAMOM", lot_size=1)

#: the frame shapes a resubscribe can send (scratchpad weekend_rc/sim_frames.py): the ones with no usable time
UNTIMED = {
    "no TickDt": {},
    "no TickDt, Time=55799": {"Time": 55799},
    ".NET minimum date": {"TickDt": "/Date(-62135596800000)/"},
    ".NET minimum date, Time=55799": {"TickDt": "/Date(-62135596800000)/", "Time": 55799},
    "Date(0)": {"TickDt": "/Date(0)/"},
    "unparseable": {"TickDt": "garbage"},
}


def _row(inst: Instrument, extra: dict, *, ltp: float = 1075.0, total: int = 0) -> dict:
    row = {"Token": int(inst.scrip_code), "Exch": "M" if inst.segment is Segment.MCX_FO else "N",
           "ExchType": "D" if inst.segment is Segment.MCX_FO else "C", "LastRate": ltp, "LastQty": 0,
           "TotalQty": total, "PClose": ltp, "High": 0, "Low": 0, "BidRate": 0, "OffRate": 0}
    row.update(extra)
    return row


def _friday_closed(agg: Aggregator, inst: Instrument) -> None:
    """The live state on Saturday: connected since before Friday's open, Friday's buckets closed."""
    st = agg.state[inst.scrip_code]
    st.connected_since = BOOT
    last = _ts(10, 16, 15, 29, 59) if inst.segment is Segment.NSE_EQ else _ts(10, 16, 23, 29, 59)
    for tf in agg.timeframes:
        st.closed_upto[tf] = int(bucket_start(inst.segment, last, tf))
    st.day, st.last_total_qty = "2026-10-16", 5_000_000


def _agg(refuse: bool = True) -> tuple[Aggregator, BarStore]:
    store = BarStore()
    agg = Aggregator(store, refuse_untimed=refuse)
    for inst in (EQ, MCX):
        agg.track(inst)
        _friday_closed(agg, inst)
    return agg, store


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(UNTIMED))
async def test_a_weekend_snapshot_with_no_trade_time_builds_no_bar(shape):
    for refuse in (True, False):
        agg, store = _agg(refuse)
        for inst, arrived in ((EQ, SAT_NSE), (MCX, SAT_MCX)):
            tick = FivePaisaFeed._tick(_row(inst, UNTIMED[shape]), arrived)
            assert tick["exch_ts"] is None and tick["ts"] == arrived, "the quote keeps its arrival time"
            await agg.on_tick(tick)
            built = store.forming(inst.symbol, "1m") is not None or store.forming(inst.symbol, "1d") is not None
            assert built is (not refuse), f"{shape} / {inst.segment.value}: refused only with the switch on"
        if refuse:
            assert agg.untimed_ticks == 2 and agg.untimed_by == {"NSE_EQ/weekend": 1, "MCX_FO/weekend": 1}
            assert agg.state[EQ.scrip_code].last_total_qty == 5_000_000 and agg.state[EQ.scrip_code].day == "2026-10-16"


@pytest.mark.asyncio
async def test_a_timed_frame_is_judged_exactly_as_before():
    """Weekday ticks with a TickDt: the same bars, volumes and counters whatever the switch."""
    mon = [(_ts(10, 19, 9, 15) + s, 1000.0 + s / 10, 100 * (s + 1)) for s in range(0, 600, 7)]
    mon += [(_ts(10, 19, 9, 8, 0), 999.0, 50)]  # a pre-open print: primes the baseline, no bar
    out = []
    for refuse in (True, False):
        store = BarStore()
        agg = Aggregator(store, refuse_untimed=refuse)
        agg.track(EQ)
        agg.state[EQ.scrip_code].connected_since = BOOT
        for t, px, total in sorted(mon, key=lambda x: x[0]):
            row = _row(EQ, {"TickDt": f"/Date({int(t * 1000)})/"}, ltp=px, total=total)
            await agg.on_tick(FivePaisaFeed._tick(row, t + 0.2))
        out.append(([(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in store.bars("X360ONE", "1m")],
                    agg.out_of_session_ticks, agg.untimed_ticks))
    assert out[0] == out[1] and out[0][0] and out[0][2] == 0


@pytest.mark.asyncio
async def test_an_untimed_pre_open_frame_primes_no_baseline():
    store = BarStore()
    agg = Aggregator(store)
    agg.track(EQ)
    agg.state[EQ.scrip_code].connected_since = BOOT
    stale = FivePaisaFeed._tick(_row(EQ, {}, total=9_000_000), _ts(10, 19, 9, 9))  # yesterday's total, untimed
    await agg.on_tick(stale)
    assert agg.state[EQ.scrip_code].last_total_qty is None and agg.state[EQ.scrip_code].day == ""
    timed = FivePaisaFeed._tick(_row(EQ, {"TickDt": f"/Date({int(_ts(10, 19, 9, 8) * 1000)})/"}, total=40_000),
                                _ts(10, 19, 9, 9))
    await agg.on_tick(timed)
    assert agg.state[EQ.scrip_code].last_total_qty == 40_000, "a timed pre-open print still primes it"


@pytest.mark.asyncio
async def test_an_illiquid_name_s_untimed_snapshot_is_refused_then_its_first_trade_builds_the_bar():
    agg, store = _agg()
    agg.state[MCX.scrip_code].day = "2026-10-19"
    agg.state[MCX.scrip_code].connected_since = _ts(10, 19, 9, 0)
    await agg.on_tick(FivePaisaFeed._tick(_row(MCX, {}, ltp=3290.0, total=1), _ts(10, 19, 9, 3)))
    assert store.forming("CARDAMOM", "1m") is None and agg.untimed_by == {"MCX_FO/session": 1}
    first = _ts(10, 19, 10, 2, 5)
    await agg.on_tick(FivePaisaFeed._tick(_row(MCX, {"TickDt": f"/Date({int(first * 1000)})/"}, ltp=3301.0, total=3),
                                          first + 0.1))
    bar = store.forming("CARDAMOM", "1m")
    assert bar is not None and bar.close == 3301.0


@pytest.mark.asyncio
async def test_a_tick_without_the_field_is_a_replay_and_is_judged_as_before():
    agg, store = _agg()
    await agg.on_tick({"scrip_code": EQ.scrip_code, "ltp": 1075.0, "ts": SAT_NSE})
    assert store.forming("X360ONE", "1m") is not None and agg.untimed_ticks == 0


@pytest.mark.asyncio
async def test_refused_frames_are_sampled_in_the_log_not_written_per_frame():
    agg, _ = _agg()
    with capture_logs() as logs:
        for k in range(40):  # one code, a burst: one line
            await agg.on_tick(FivePaisaFeed._tick(_row(EQ, {}), SAT_NSE + k))
    lines = [x for x in logs if x["event"] == "bars.untimed_refused"]
    assert len(lines) == 1 and agg.untimed_ticks == 40
    line = lines[0]
    assert line["token"] == "13061" and line["tick_dt"] is None and line["phase"] == "weekend" and line["exch"] == "N"
    assert "client" not in " ".join(line), "market fields only"


def test_the_sampler_caps_a_day_then_each_segment_phase():
    s = RefusedSampler(per_day=3, per_key=2, per_code_s=600.0)
    took = [s.take("d", "NSE_EQ/session", f"c{k}", 0.0) for k in range(10)]
    assert took.count(True) == 3
    assert s.take("d", "MCX_FO/session", "m1", 0.0) and s.take("d", "MCX_FO/session", "m2", 0.0)
    assert not s.take("d", "MCX_FO/session", "m3", 0.0), "the day's 3 and that key's 2 are used"
    assert s.take("e", "NSE_EQ/session", "c0", 1.0), "a new day starts again"
    assert not s.take("e", "NSE_EQ/session", "c0", 500.0) and s.take("e", "NSE_EQ/session", "c0", 601.0)


def test_the_switch_defaults_on_and_reaches_the_engine(settings):
    from kotsin_nse.engine import Engine

    assert Settings(_env_file=None).bars_refuse_untimed is True
    assert Engine(settings).aggregator.refuse_untimed is True
    off = settings.model_copy(update={"bars_refuse_untimed": False})
    assert Engine(off).aggregator.refuse_untimed is False
