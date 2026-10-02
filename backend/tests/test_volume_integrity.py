"""Operator, 2026-09-28: "we need accurate volume values else our calculation gets affected … is there
a way in which we dont miss any volume bar and also gate against random glitches".

The case: HDFCBANK's 30m bars as the engine held them at the 09:45 trigger of 2026-09-28. 25 Sep
carried the full closing auction at 15:15 AND a 15:45 post-close bar of 3,203 shares, which sat in
the T-1 slot and read 0.00x — across the market, a median T-1 of 0.002."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.indicators import dried_volume, volume_surges
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.bars.verify import BarReconciler, volume_doubt
from kotsin_nse.bars.volume_read import VolBar, market_volume, read_volume
from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import (
    IST,
    NSE_EQ_CONTINUOUS_UNTIL,
    TradingCalendar,
    ist_hm,
    ist_naive_to_ts,
)

FRI, MON = date(2026, 9, 25), date(2026, 9, 28)
CAL = TradingCalendar(frozenset({date(2026, 10, 2)}))
#: (day, IST bar start, volume) — the live store's rows, 2026-09-28 09:45
HDFCBANK = [
    (FRI, "09:15", 2618808), (FRI, "09:45", 1682505), (FRI, "10:15", 1052183), (FRI, "10:45", 904842),
    (FRI, "11:15", 1093953), (FRI, "11:45", 1737308), (FRI, "12:15", 1261816), (FRI, "12:45", 1362884),
    (FRI, "13:15", 1467094), (FRI, "13:45", 1944605), (FRI, "14:15", 2931798), (FRI, "14:45", 7484625),
    (FRI, "15:15", 19606468), (FRI, "15:45", 3203),
    (MON, "09:15", 4797816),
]
EQ = Instrument("1333", "HDFCBANK", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="HDFCBANK")


def _ts(d: date, hm: str) -> int:
    return int(datetime.combine(d, time.fromisoformat(hm), tzinfo=IST).timestamp())


def _bars(rows, symbol="HDFCBANK", code="1333"):
    return [UnifiedBar(symbol=symbol, scrip_code=code, tf="30m", ts=_ts(d, hm), open=1.0, high=1.0, low=1.0, close=1.0,
                       volume=float(v), source=BarSource.REST, complete=True) for d, hm, v in rows]


def _read(rows, t=(MON, "09:15"), **kw):
    kw.setdefault("until", NSE_EQ_CONTINUOUS_UNTIL)
    return read_volume([VolBar(_ts(d, hm), float(v)) for d, hm, v in rows], segment=Segment.NSE_EQ,
                       t_ts=_ts(*t), calendar=CAL, **kw)


# -- the reading: only session bars, by slot ---------------------------------------------------------


def test_the_bar_before_0945_is_fridays_1445_not_the_auction_or_the_post_close():
    r = _read(HDFCBANK)
    assert r.ok
    # T-1 is Friday's 14:45 bar; the baseline Friday 11:45 … 14:15
    assert r.baseline == pytest.approx((1737308 + 1261816 + 1362884 + 1467094 + 1944605 + 2931798) / 6)
    assert r.surge_t == pytest.approx(2.69, abs=0.01) and r.surge_t1 == pytest.approx(4.19, abs=0.01)
    # as read live: 1.75 / 0.00 — "average" on the stock, and a 0.00 T-1 that let T decide "dried" alone
    old = volume_surges([float(v) for d, hm, v in HDFCBANK if hm != "15:15"], window=6, floor=1000.0)
    assert old[0] == pytest.approx(1.75, abs=0.01) and old[1] < 0.01


def test_a_quiet_0945_bar_is_no_longer_dried_by_the_post_close_bar():
    quiet = [*HDFCBANK[:-1], (MON, "09:15", 1_500_000)]
    old = volume_surges([float(v) for d, hm, v in quiet if hm != "15:15"], window=6, floor=1000.0)
    assert dried_volume(old[0], old[1], v=0.85), "the 15:15-only filter: 0.55 / 0.00 read as dried"
    new = _read(quiet)
    assert new.ok and not dried_volume(new.surge_t, new.surge_t1, v=0.85), "Friday's 14:45 bar was 4.2x — not dried"


def test_a_bar_off_the_grid_is_ignored_never_read_as_a_neighbours_slot():
    """The store holds only grid bars (the backfill snaps, the tick path buckets); a reading still
    reads by slot, so an 08:45 or 09:16 bar handed to it can never stand in for 09:15."""
    rows = [*HDFCBANK[:12], (MON, "08:45", 900), (MON, "09:16", 50), (MON, "09:15", 4797816)]
    assert _read(rows).surge_t1 == pytest.approx(4.19, abs=0.01)


def test_a_missing_bar_makes_the_reading_doubtful_never_shifts_it():
    """Without the slot check the bar before a gap silently became "T-1"."""
    gap = [row for row in HDFCBANK if row[1] != "14:45" or row[0] != FRI]
    r = _read(gap)
    assert not r.ok and r.kind == "missing" and r.doubt == "missing bar 25 Sep 14:45"


def test_a_zero_bar_or_a_flagged_bar_makes_the_reading_doubtful():
    zero = [(d, hm, 0 if (d, hm) == (FRI, "13:15") else v) for d, hm, v in HDFCBANK]
    assert _read(zero).kind == "zero"
    bars = [VolBar(_ts(d, hm), float(v), "broker 10 vs live build 900" if (d, hm) == (FRI, "12:45") else "") for d, hm, v in HDFCBANK]
    r = read_volume(bars, segment=Segment.NSE_EQ, t_ts=_ts(MON, "09:15"), calendar=CAL, until=NSE_EQ_CONTINUOUS_UNTIL)
    assert r.kind == "flagged" and "25 Sep 12:45" in r.doubt


def test_the_slots_walk_back_across_a_holiday():
    """Monday 5 Oct 09:45 after Gandhi Jayanti (Friday 2 Oct): T-1 is Thursday's 14:45."""
    thu, mon = date(2026, 10, 1), date(2026, 10, 5)
    rows = [(thu, hm, 1000 * (i + 1)) for i, hm in enumerate(("12:15", "12:45", "13:15", "13:45", "14:15", "14:45"))]
    rows += [(mon, "09:15", 9000), (mon, "09:45", 7000)]
    r = _read(rows, t=(mon, "09:45"))
    assert r.ok and r.baseline == pytest.approx(3500.0) and r.surge_t1 == pytest.approx(9000 / 3500)


def test_a_future_counts_its_1515_bar_but_not_a_post_close_row():
    rows = [(FRI, hm, 1000) for hm in ("12:15", "12:45", "13:15", "13:45", "14:15", "14:45")]
    rows += [(FRI, "15:15", 500), (FRI, "15:45", 3), (MON, "09:15", 2000)]
    r = read_volume([VolBar(_ts(d, hm), float(v)) for d, hm, v in rows], segment=Segment.NSE_FO, t_ts=_ts(MON, "09:15"), calendar=CAL)
    assert r.ok and r.surge_t1 == pytest.approx(0.5), "T-1 is the future's own 15:15 bar"


# -- the market-wide check ------------------------------------------------------------------------------


def test_a_market_median_t1_of_0002_is_a_broken_bar_not_a_quiet_market():
    from kotsin_nse.bars.volume_read import VolumeReading

    live_shaped = [VolumeReading(1.4, 0.002, 1.0)] * 208
    mv = market_volume(1, live_shaped)
    assert mv.alarm.startswith("market median T-1 0.002x")
    fine = market_volume(1, [VolumeReading(1.2, 1.9, 1.0)] * 208)
    assert fine.alarm == "" and fine.median_t1 == pytest.approx(1.9)
    many_doubts = market_volume(1, [VolumeReading(doubt="missing bar", kind="missing")] * 80 + [VolumeReading(1.0, 1.0, 1.0)] * 120)
    assert "80 of 200 readings doubtful" in many_doubts.alarm and many_doubts.reasons == {"missing": 80}
    assert market_volume(1, live_shaped[:20]).alarm == "", "too few names to judge the market"


def test_every_reading_at_an_alarmed_bar_is_doubtful_and_the_health_line_says_so(settings, monkeypatch):
    import kotsin_nse.engine as engine_mod

    monkeypatch.setattr(engine_mod, "ist_today", lambda: MON)  # the market check judges today's bars: today is the data's day
    e = Engine(settings)
    e.calendar = CAL
    # 60 names shaped like 2026-09-28 09:45: the post-close bar in T-1 (the old ingestion let it in)
    for i in range(60):
        sym = f"S{i}"
        e.underlyings[sym] = Instrument(str(1000 + i), sym, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=sym)
        e.store.seed(sym, "30m", _bars(HDFCBANK, symbol=sym, code=str(1000 + i)))
    # every name reads fine on its own (the reading drops 15:15 and 15:45) — so the market is fine
    r = e._volume_reading("S0", _ts(MON, "09:15"))
    assert r.ok and e._market_volume(_ts(MON, "09:15")).alarm == ""
    assert e._volume_check().ok and "60 names" in e._volume_check().detail
    # now break Friday 14:45 for 50 of them: the slot is missing, 83 % doubtful — an alarm
    e._vol_market.clear()
    for i in range(50):
        sym = f"S{i}"
        e.store._closed[(sym, "30m")] = [b for b in e.store.bars(sym, "30m") if ist_hm(b.ts) != "14:45"]
    mv = e._market_volume(_ts(MON, "09:15"))
    assert "50 of 60 readings doubtful" in mv.alarm
    r = e._volume_reading("S55", _ts(MON, "09:15"))
    assert not r.ok and r.kind == "market", "a name whose own bars look fine still does not decide at a broken bar"
    import time as _time

    real = _time.time
    try:
        _time.time = lambda: _ts(MON, "10:00")  # type: ignore[assignment]
        assert not e._volume_check().ok and "ALARM" in e._volume_check().detail, "red while NSE is open"
        _time.time = lambda: _ts(MON, "16:00")  # type: ignore[assignment]
        assert e._volume_check().ok and e._volume_check().detail.startswith("NSE closed · 28 Sep 09:15 bar: ALARM")
    finally:
        _time.time = real  # type: ignore[assignment]


def test_the_market_check_judges_only_todays_continuous_bars(settings):
    """Review, 2026-09-28: a strong FUDKII signal on the 15:15 bar asked the market check about the
    auction bar — every name "off-grid" → a false alarm; so did an older session's card."""
    e = Engine(settings)
    for i in range(60):
        sym = f"S{i}"
        e.underlyings[sym] = Instrument(str(1000 + i), sym, Segment.NSE_EQ, InstrumentKind.EQUITY, underlying=sym)
    import time as _time

    real = _time.time
    try:
        _time.time = lambda: _ts(MON, "15:31")  # type: ignore[assignment]
        mv = e._market_volume(_ts(MON, "15:15"))
        assert mv.alarm == "" and mv.names == 0 and _ts(MON, "15:15") not in e._vol_market
        assert e._market_volume(_ts(FRI, "10:15")).alarm == "", "an older session is not judged"
    finally:
        _time.time = real  # type: ignore[assignment]


def test_an_mcx_reading_walks_back_through_the_days_its_own_bars_traded(settings):
    """The holiday list is NSE's. 2026-10-02 (an NSE holiday) with an MCX evening session: the 3 Oct
    reading's T-1 is 2 Oct 23:00, not 1 Oct's — the NSE calendar would skip the evening silently."""
    e = Engine(settings)
    e.calendar = CAL
    e.underlyings["CRUDEOIL"] = Instrument("451", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, underlying="CRUDEOIL")
    thu, hol, fri = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
    evening = [f"{h:02d}:{m:02d}" for h in range(17, 24) for m in (0, 30) if f"{h:02d}:{m:02d}" <= "23:00"]
    rows = [(thu, hm, 999) for hm in evening]
    rows += [(hol, hm, 2000 if hm == "23:00" else 1000) for hm in evening]
    rows += [(fri, "09:00", 3000)]
    mk = lambda rs: [UnifiedBar(symbol="CRUDEOIL", scrip_code="451", tf="30m", ts=_ts(d, hm), open=1, high=1, low=1, close=1,  # noqa: E731
                                volume=float(v), source=BarSource.REST, complete=True) for d, hm, v in rs]
    r = e._volume_reading("CRUDEOIL", _ts(fri, "09:00"), mk(rows))
    assert r.ok and r.baseline == pytest.approx(1000.0) and r.surge_t1 == pytest.approx(2.0), "T-1 is the holiday evening's 23:00"
    # an evening too short to hold the eight slots is doubtful — never shifted onto Thursday
    short = [row for row in rows if row[0] != hol or row[1] >= "21:00"]
    assert e._volume_reading("CRUDEOIL", _ts(fri, "09:00"), mk(short)).kind == "missing"
    # a whole session missing from the store on a day that trades is missing — not skipped over
    mon, tue = date(2026, 10, 5), date(2026, 10, 6)
    gap = [(fri, hm, 1000) for hm in evening] + [(tue, "09:00", 3000)]  # Monday's session absent
    r = e._volume_reading("CRUDEOIL", _ts(tue, "09:00"), mk(gap))
    assert r.kind == "missing" and r.doubt.startswith("missing bar 05 Oct 20:00")
    assert mon.weekday() == 0


# -- ingestion: a broker row is snapped onto its bucket; outside the session it is no bar -----------------


def test_the_backfill_snaps_first_trade_stamps_and_drops_rows_outside_the_session():
    """Review, 2026-09-28: 5paisa stamps a 30m candle with its first trade's minute — HEROMOTOCO
    2026-09-21's 09:15 bucket is the row ``09:16`` (87,758 shares), and a same-evening fetch stamps
    the 15:15 auction ``15:28``. Those rows ARE their buckets; only pre-open and post-close are not."""
    store = BarStore()
    agg = Aggregator(store)
    rows = [
        {"dt": "2026-09-25 09:07:00", "o": 3, "h": 3, "l": 3, "c": 3, "v": 900},  # pre-open: was clamped INTO 09:15
        {"dt": "2026-09-25 09:16:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 87758},  # the 09:15 bucket
        {"dt": "2026-09-25 10:15:00", "o": 5, "h": 5, "l": 5, "c": 5, "v": 1000},
        {"dt": "2026-09-25 10:16:00", "o": 9, "h": 9, "l": 9, "c": 9, "v": 7},  # should both come: on-grid wins
        {"dt": "2026-09-25 15:28:00", "o": 2, "h": 2, "l": 2, "c": 2, "v": 19606468},  # the auction: the 15:15 bucket
        {"dt": "2026-09-25 15:50:00", "o": 2, "h": 2, "l": 2, "c": 2, "v": 3203},  # post-close: opened a phantom 15:45
        {"dt": "2026-09-28 09:15:00", "o": 4, "h": 4, "l": 4, "c": 4, "v": 4797816},
    ]
    agg.seed(EQ, "30m", rows, ts_of=ist_naive_to_ts)
    held = [(ist_hm(b.ts), b.volume) for b in store.bars("HDFCBANK", "30m")]
    assert held == [("09:15", 87758), ("10:15", 1000), ("15:15", 19606468), ("09:15", 4797816)]
    assert agg.rows_dropped == 2


@pytest.mark.asyncio
async def test_the_future_context_snaps_rows_onto_the_grid_and_rewrites_their_time(settings):
    """TATAPOWER SEP 2026-09-07 ``10:46`` (146,450 shares) is the 10:45 bucket; the readers compare
    ``dt`` with the trigger bucket as a string, so the stamp must become the bucket."""
    e = Engine(settings)
    fut = Instrument("68534", "HDFCBANK", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=650, expiry="2099-12-31", underlying="HDFCBANK")
    rows = [{"dt": "2026-09-25T15:16:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 500},
            {"dt": "2026-09-25T15:45:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 3},  # after the close: no bar
            {"dt": "2026-09-28T09:16:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 7},
            {"dt": "2026-09-28T09:15:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 900}]
    out = e._snap_fut_rows(fut, rows)
    assert [(r["dt"], r["v"]) for r in out] == [("2026-09-25T15:15:00", 500), ("2026-09-28T09:15:00", 900)]
    assert out[0]["stamped"] == "2026-09-25T15:16:00"


@pytest.mark.asyncio
async def test_a_futures_bucket_with_no_row_at_all_is_rebuilt_from_its_1m_candles(settings):
    e = Engine(settings)
    e.calendar = CAL
    fut = Instrument("68795", "TATAPOWER", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=1450, expiry="2099-12-31", underlying="TATAPOWER")
    day = date(2026, 9, 7)
    grid = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (15, 45) if "09:15" <= f"{h:02d}:{m:02d}" <= "15:15"]
    prev = date(2026, 9, 4)
    rows30 = [{"dt": f"{prev}T{hm}:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 100_000.0} for hm in grid]
    rows30 += [{"dt": f"{day}T{hm}:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 100_000.0} for hm in grid if hm <= "12:45" and hm != "10:45"]
    asked = []

    async def candles(inst, tf, start, end):
        asked.append((tf, start))
        assert tf == "1m" and start == "2026-09-07"
        return [{"dt": f"2026-09-07T10:{45 + i}:00", "o": 380 + i, "h": 381 + i, "l": 379, "c": 380.5 + i, "v": 6_656.8} for i in range(15)] + \
               [{"dt": "2026-09-07T11:02:00", "o": 395, "h": 399, "l": 394, "c": 398, "v": 46_598.0},
                {"dt": "2026-09-07T11:15:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 9e9}]  # the next bucket: not this one

    e.rest.candles = candles  # type: ignore[method-assign]
    t = _ts(day, "12:45")
    out, failed = await e._fill_fut_gaps(fut, rows30, t)
    assert not failed and asked == [("1m", "2026-09-07")], "one 1m call, for the one day with a gap"
    row = next(r for r in out if r["dt"].startswith("2026-09-07T10:45"))
    assert row["src"] == "1m" and row["v"] == pytest.approx(15 * 6_656.8 + 46_598.0) and (row["o"], row["h"], row["l"], row["c"]) == (380, 399, 379, 398)
    assert e._fut_volume_reading(out, t).ok, "the reading reads eight real slots, not a shifted seven"
    # the broker's 1m candles down too: the slot stays missing, the reading doubtful, the context not cached
    async def down(inst, tf, start, end):
        raise RuntimeError("historical endpoint down")

    e.rest.candles = down  # type: ignore[method-assign]
    out2, failed2 = await e._fill_fut_gaps(fut, rows30, t)
    assert failed2 and e._fut_volume_reading(out2, t).kind == "missing"


# -- the bar-close check: broker and live build past belief ------------------------------------------------


def _live(hm="11:15", v=100_000.0, source=BarSource.LIVE):
    return UnifiedBar(symbol="HDFCBANK", scrip_code="1333", tf="30m", ts=_ts(MON, hm), open=1, high=1, low=1, close=1, volume=v, source=source)


def test_volume_doubt_only_where_it_can_be_judged():
    assert volume_doubt(_live(), 0.0, EQ) == "broker candle has no volume, live build 100,000"
    assert volume_doubt(_live(), 40_000.0, EQ) == "broker 40,000 vs live build 100,000 (150% apart)"
    assert volume_doubt(_live(), 70_000.0, EQ) == "", "43 % apart: inside what a normal day shows (max 37 %)"
    assert volume_doubt(_live(source=BarSource.PARTIAL), 0.0, EQ) == "", "a build joined mid-bucket is expected to differ"
    assert volume_doubt(_live("15:15"), 0.0, EQ) == "", "the auction bar differs by design"
    assert volume_doubt(_live(v=0.0), 50_000.0, EQ) == "", "no live count to judge against"
    fut = Instrument("68534", "HDFCBANK", Segment.NSE_FO, InstrumentKind.FUTURE, underlying="HDFCBANK")
    assert volume_doubt(_live(), 0.0, fut) == ""


class _Rest:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def candles(self, inst, tf, start, end):
        self.calls += 1
        return self.rows


@pytest.mark.asyncio
async def test_a_flagged_bar_carries_its_doubt_into_the_reading():
    store = BarStore()
    live = _live(v=100_000.0)
    store.close(live)
    rest = _Rest([{"dt": "2026-09-28 11:15:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
    rec = BarReconciler(rest, store, lambda _s: EQ, settle_delay_s=0.0, retry_delay_s=0.0)
    c = await rec.reconcile_bar(live)
    assert c.replaced and c.volume_doubt.startswith("broker candle has no volume")
    assert store.last("HDFCBANK", "30m").extra["volume_doubt"] == c.volume_doubt
    assert rec.stats["30m"].volume_doubts == 1


# -- the after-close audit ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_audit_repairs_a_bar_that_differs_and_adds_one_never_built():
    store = BarStore()
    for hm, v in (("09:15", 4797816.0), ("09:45", 2609661.0), ("10:45", 1.0)):
        store.close(UnifiedBar(symbol="HDFCBANK", scrip_code="1333", tf="30m", ts=_ts(MON, hm), open=1, high=1, low=1,
                               close=1, volume=v, source=BarSource.REST, complete=True, prev_close=735.6))
    rows = [{"dt": f"2026-09-28 {hm}:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": v} for hm, v in
            (("09:15", 4797816), ("09:45", 2609661), ("10:15", 1500000), ("10:45", 1200000), ("15:15", 9e6), ("15:45", 3203))]
    rec = BarReconciler(_Rest(rows), store, lambda _s: EQ, settle_delay_s=0.0, retry_delay_s=0.0)
    keep = lambda inst, ts: ist_hm(ts) < "15:15" and ist_hm(ts)[3:] in ("15", "45")  # noqa: E731
    res = await rec.audit_day(["HDFCBANK"], MON, keep=keep, pace_s=0.0)
    assert (res["bars"], res["exact"], res["repaired"], res["added"], res["failed"]) == (4, 2, 1, 1, 0)
    held = {ist_hm(b.ts): b for b in store.bars("HDFCBANK", "30m")}
    assert held["10:15"].volume == 1500000 and held["10:15"].extra["audit"] == "added" and held["10:15"].prev_close == 735.6
    assert held["10:45"].volume == 1200000 and held["10:45"].extra["audit"] == "repaired"
    assert "15:45" not in held and "15:15" not in held, "only the judged buckets are touched"


@pytest.mark.asyncio
async def test_the_audit_reads_a_first_trade_stamp_as_its_bucket():
    store = BarStore()
    store.close(UnifiedBar(symbol="HDFCBANK", scrip_code="1333", tf="30m", ts=_ts(MON, "10:45"), open=1, high=1, low=1,
                           close=1, volume=1200000.0, source=BarSource.REST, complete=True))
    rows = [{"dt": "2026-09-28 10:46:00", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1200000}]
    rec = BarReconciler(_Rest(rows), store, lambda _s: EQ, settle_delay_s=0.0, retry_delay_s=0.0)
    res = await rec.audit_day(["HDFCBANK"], MON, keep=lambda inst, ts: True, pace_s=0.0)
    assert (res["bars"], res["exact"], res["repaired"], res["added"]) == (1, 1, 0, 0), "10:46 is the 10:45 bar, not a new one"


@pytest.mark.asyncio
async def test_the_audit_result_is_a_health_line(settings):
    e = Engine(settings)
    await e.ledger.init()
    e.underlyings["HDFCBANK"] = EQ

    async def audit_day(symbols, day, *, keep, pace_s=0.15):
        return {"day": day.isoformat(), "names": 1, "bars": 12, "exact": 5, "repaired": 6, "added": 1, "failed": 0, "examples": ["HDFCBANK 10:45 v 1→2"]}

    e.reconciler.audit_day = audit_day  # type: ignore[method-assign]
    sent = []
    e.telegram.fire_and_forget = lambda text, key=None: sent.append(text)  # type: ignore[method-assign]
    await e._audit_bars(MON)
    chk = e._bar_audit_check()
    assert chk.ok and chk.value == 7 and "6 repaired, 1 added" in chk.detail, "repaired is fixed: information, and an alert"
    assert sent and "Bar audit 28 Sep: 6 repaired, 1 added" in sent[0]
    e._bar_audit = {**e._bar_audit, "failed": 1, "of": 1}
    assert not e._bar_audit_check().ok, "an audit that could not look leaves the day unverified"
