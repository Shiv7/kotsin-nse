"""The reconciler, the scripFinder-style universe, book microstructure, and the session-bound bar
builder — every one of them exists because of something measured on the first live session."""

from __future__ import annotations

import asyncio
import time
from datetime import date

import pytest

from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.micro import (
    BookState,
    MicroAggregator,
    depth_imbalance,
    l1_ofi,
    microprice,
    spread_bps,
)
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource
from kotsin_nse.bars.verify import BarReconciler
from kotsin_nse.config import Segment
from kotsin_nse.domain import InstrumentKind, OptionType
from kotsin_nse.instrument.catalogue import Catalogue, parse_master
from kotsin_nse.instrument.universe import UniverseBuilder, UniversePolicy
from kotsin_nse.market.session import ist_hm, session_close_ts
from kotsin_nse.venue.fivepaisa.auth import Session

from .conftest import bar, ist_ts

# ---------------------------------------------------------------------------------------------------
# store.replace_closed
# ---------------------------------------------------------------------------------------------------


def test_replace_closed_swaps_by_bucket_and_leaves_the_forming_bar_alone(equity):
    store = BarStore()
    t = ist_ts("2026-09-18", "09:15")
    store.close(bar(t, 10, 11, 9, 10))
    store.close(bar(t + 1800, 10, 12, 9, 11))
    forming = bar(t + 3600, 11, 11, 11, 11)
    forming.complete = False
    store.set_forming(forming)

    fixed = bar(t, 10, 11.5, 9, 10.2)
    fixed.source = BarSource.REST
    assert store.replace_closed(fixed) is True
    assert store.bars("RELIANCE", "30m")[0].high == 11.5
    assert store.forming("RELIANCE", "30m") is forming, "close() would have dropped it; replace must not"

    older = bar(t - 1800, 9, 10, 8, 9)
    assert store.replace_closed(older) is False  # inserted, not replaced
    assert [b.ts for b in store.bars("RELIANCE", "30m")] == [t - 1800, t, t + 1800]


# ---------------------------------------------------------------------------------------------------
# reconciler
# ---------------------------------------------------------------------------------------------------


class _Rest:
    def __init__(self, rows_by_tf, *, fail=False, delay=0.0):
        self.rows_by_tf = rows_by_tf
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def candles(self, inst, tf, start, end):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("broker down")
        return self.rows_by_tf.get(tf, [])


def _row(day: str, hm: str, o, h, low, c, v):
    return {"dt": f"{day} {hm}:00", "o": o, "h": h, "l": low, "c": c, "v": v}


def _reconciler(rest, store, equity):
    return BarReconciler(rest, store, lambda _s: equity, settle_delay_s=0.0, retry_delay_s=0.0)


async def test_exact_bar_is_confirmed_not_replaced(equity):
    store = BarStore()
    t = ist_ts("2026-09-18", "10:15")
    live = bar(t, 100, 101, 99, 100.5, 500)
    store.close(live)
    rest = _Rest({"30m": [_row("2026-09-18", "10:15", 100, 101, 99, 100.5, 500)]})
    c = await _reconciler(rest, store, equity).reconcile_bar(live)
    assert c.found and c.exact and not c.replaced
    assert store.last("RELIANCE", "30m") is live
    assert live.extra["confirmed"] is True


async def test_differing_bar_is_replaced_and_the_live_values_kept(equity):
    """COPPER 23:14: live close 1412.75, exchange 1412.45. The exchange wins, the disagreement stays
    inspectable."""
    store = BarStore()
    t = ist_ts("2026-09-18", "10:15")
    live = bar(t, 100, 101, 99, 100.75, 7)
    store.close(live)
    rest = _Rest({"30m": [_row("2026-09-18", "10:15", 100, 100.5, 99, 100.45, 5)]})
    r = _reconciler(rest, store, equity)
    c = await r.reconcile_bar(live)
    assert c.found and not c.exact and c.replaced
    assert c.diffs == {"h": 0.5, "c": 0.3, "v": 2.0}
    now = store.last("RELIANCE", "30m")
    assert now.source is BarSource.REST and now.close == 100.45 and now.volume == 5
    assert now.extra["live"]["c"] == 100.75 and now.extra["confirmed"] is True
    s = r.stats["30m"]
    assert (s.compared, s.exact, s.replaced, s.h_diff, s.c_diff, s.v_diff) == (1, 0, 1, 1, 1, 1)


async def test_missing_bucket_is_counted_and_the_bar_left_alone(equity):
    store = BarStore()
    t = ist_ts("2026-09-18", "10:15")
    live = bar(t, 100, 101, 99, 100.5, 500)
    store.close(live)
    rest = _Rest({"30m": []})
    c = await _reconciler(rest, store, equity).reconcile_bar(live)
    assert not c.found and not c.error
    assert rest.calls == 2, "one retry for a bucket the exchange has not published yet"
    assert store.last("RELIANCE", "30m") is live


async def test_rest_failure_and_timeout_are_recorded_not_raised(equity):
    store = BarStore()
    t = ist_ts("2026-09-18", "10:15")
    live = bar(t, 100, 101, 99, 100.5, 500)
    store.close(live)
    c = await _reconciler(_Rest({}, fail=True), store, equity).reconcile_bar(live)
    assert c.error and not c.found
    r = _reconciler(_Rest({"30m": []}, delay=0.5), store, equity)
    c2 = await r.reconcile_bar(live, timeout_s=0.05)
    assert "timeout" in c2.error
    assert r.stats["30m"].rest_failures == 1


async def test_sweep_reconciles_only_unconfirmed_live_bars_of_today(equity):
    store = BarStore()
    today = date.today().isoformat()
    t = ist_ts(today, "10:15")
    b1 = bar(t, 100, 101, 99, 100.5, 500)
    b2 = bar(t + 1800, 100.5, 102, 100, 101, 400)
    store.close(b1)
    store.close(b2)
    rest = _Rest({"30m": [_row(today, "10:15", 100, 101, 99, 100.5, 500), _row(today, "10:45", 100.5, 102.5, 100, 101, 400)]})
    r = BarReconciler(rest, store, lambda _s: equity, sweep_tfs=("30m",), settle_delay_s=0.0)
    n = await r.sweep(["RELIANCE"], today=date.today())
    assert n == 2 and rest.calls == 1
    assert store.bars("RELIANCE", "30m")[1].high == 102.5
    # a second sweep finds nothing left to check
    assert await r.sweep(["RELIANCE"], today=date.today()) == 0
    assert rest.calls == 1


# ---------------------------------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------------------------------

EQ = """Exch,ExchType,Scripcode,Name,Expiry,ScripType,StrikeRate,FullName,TickSize,LotSize,QtyLimit,Multiplier,SymbolRoot,ISIN,Series
N,C,2885,RELIANCE,,,0,RELIANCE,0.05,1,1,1,RELIANCE,,EQ
N,C,11536,TCS,,,0,TCS,0.05,1,1,1,TCS,,EQ
N,C,5,NOFNO,,,0,NO DERIVATIVES,0.05,1,1,1,NOFNO,,EQ
"""
FO = """Exch,ExchType,Scripcode,Name,Expiry,ScripType,StrikeRate,FullName,TickSize,LotSize,QtyLimit,Multiplier,SymbolRoot,ISIN,Series
N,D,1,RELIANCE,2026-09-29 00:00:00,XX,0,RELIANCE SEP FUT,0.05,250,1,1,RELIANCE,,
N,D,2,RELIANCE,2026-10-27 00:00:00,XX,0,RELIANCE OCT FUT,0.05,250,1,1,RELIANCE,,
N,D,3,RELIANCE,2026-11-24 00:00:00,XX,0,RELIANCE NOV FUT,0.05,250,1,1,RELIANCE,,
N,D,10,RELIANCE,2026-09-22 00:00:00,CE,1500,RELIANCE 22 SEP CE 1500,0.05,250,1,1,RELIANCE,,
N,D,11,RELIANCE,2026-09-29 00:00:00,CE,1400,RELIANCE CE 1400,0.05,250,1,1,RELIANCE,,
N,D,12,RELIANCE,2026-09-29 00:00:00,CE,1450,RELIANCE CE 1450,0.05,250,1,1,RELIANCE,,
N,D,13,RELIANCE,2026-09-29 00:00:00,CE,1500,RELIANCE CE 1500,0.05,250,1,1,RELIANCE,,
N,D,14,RELIANCE,2026-09-29 00:00:00,CE,1550,RELIANCE CE 1550,0.05,250,1,1,RELIANCE,,
N,D,15,RELIANCE,2026-09-29 00:00:00,CE,1600,RELIANCE CE 1600,0.05,250,1,1,RELIANCE,,
N,D,16,RELIANCE,2026-09-29 00:00:00,CE,1650,RELIANCE CE 1650,0.05,250,1,1,RELIANCE,,
N,D,17,RELIANCE,2026-09-29 00:00:00,CE,2000,RELIANCE CE 2000,0.05,250,1,1,RELIANCE,,
N,D,20,RELIANCE,2026-09-29 00:00:00,PE,1400,RELIANCE PE 1400,0.05,250,1,1,RELIANCE,,
N,D,21,RELIANCE,2026-09-29 00:00:00,PE,1450,RELIANCE PE 1450,0.05,250,1,1,RELIANCE,,
N,D,22,RELIANCE,2026-09-29 00:00:00,PE,1500,RELIANCE PE 1500,0.05,250,1,1,RELIANCE,,
N,D,23,RELIANCE,2026-09-29 00:00:00,PE,1000,RELIANCE PE 1000,0.05,250,1,1,RELIANCE,,
N,D,30,NIFTY,2026-09-29 00:00:00,XX,0,NIFTY SEP FUT,0.05,75,1,1,NIFTY,,
N,D,31,NIFTY,2026-09-29 00:00:00,CE,25000,NIFTY CE 25000,0.05,75,1,1,NIFTY,,
N,D,40,TCS,2026-09-29 00:00:00,XX,0,TCS SEP FUT,0.05,175,1,1,TCS,,
"""


def _catalogue() -> Catalogue:
    cat = Catalogue()
    for text, seg in ((EQ, Segment.NSE_EQ), (FO, Segment.NSE_FO)):
        for inst, ok in parse_master(text, seg):
            if not ok:
                continue
            cat.by_code[inst.scrip_code] = inst
            if inst.kind is InstrumentKind.EQUITY:
                cat.equity_by_symbol[inst.symbol] = inst
            elif inst.kind is InstrumentKind.FUTURE:
                cat.futures_by_symbol.setdefault(inst.underlying, []).append(inst)
            else:
                cat.options_by_symbol.setdefault(inst.underlying, []).append(inst)
    return cat


TODAY = date(2026, 9, 21)


def test_universe_is_every_root_with_a_derivative_joined_to_its_equity():
    """``FNOUniverseService.extractFNOUniverse`` + ``getFNOEquities``: NOFNO has no derivative
    and is out; NIFTY has no cash leg and rides its front future."""
    b = UniverseBuilder(_catalogue())
    assert b.fno_roots(Segment.NSE_FO) == {"RELIANCE", "NIFTY", "TCS"}
    groups = b.build_underlyings([Segment.NSE_EQ, Segment.NSE_FO], TODAY)
    assert set(groups) == {"RELIANCE", "NIFTY", "TCS"}
    assert groups["RELIANCE"].underlying.kind is InstrumentKind.EQUITY
    assert groups["NIFTY"].underlying.kind is InstrumentKind.FUTURE and groups["NIFTY"].note == "index"
    assert [f.scrip_code for f in groups["RELIANCE"].futures] == ["1", "2"], "front + next, not all three"


def test_indices_can_be_excluded():
    b = UniverseBuilder(_catalogue(), UniversePolicy(include_indices=False))
    assert "NIFTY" not in b.build_underlyings([Segment.NSE_FO], TODAY)


def test_strikes_are_the_band_around_the_close_n_per_side_nearest_tradeable_expiry():
    """``ScripGroupPopulator.selectStrikes``: ±12% of close, 5 per side by ATM proximity. The
    22 Sep expiry (tomorrow) is skipped by min_days_to_expiry; 1000 and 2000 are outside the band."""
    b = UniverseBuilder(_catalogue(), UniversePolicy(band_pct=12.0, strikes_per_side=3, min_days_to_expiry=2))
    g = b.build_underlyings([Segment.NSE_FO], TODAY)["RELIANCE"]
    b.select_strikes(g, 1500.0, TODAY)
    assert g.option_expiry == "2026-09-29"
    ce = [o.strike for o in g.options if o.option_type is OptionType.CE]
    pe = [o.strike for o in g.options if o.option_type is OptionType.PE]
    assert ce == [1450.0, 1500.0, 1550.0]
    assert pe == [1400.0, 1450.0, 1500.0]
    assert all(o.expiry == "2026-09-29" for o in g.options)


def test_no_close_means_no_strikes_and_says_so():
    b = UniverseBuilder(_catalogue())
    g = b.build_underlyings([Segment.NSE_FO], TODAY)["RELIANCE"]
    b.select_strikes(g, None, TODAY)
    assert g.options == [] and "no close" in g.note


def test_subscriptions_never_put_cash_equity_on_the_oi_channel():
    """Cash has no open interest — the MicroAlpha defect in one line."""
    b = UniverseBuilder(_catalogue(), UniversePolicy(strikes_per_side=2))
    groups = b.build_underlyings([Segment.NSE_FO], TODAY)
    b.select_all(groups, lambda s: 1500.0, TODAY)
    subs = UniverseBuilder.subscriptions(groups.values(), depth_symbols=["RELIANCE"])
    codes = lambda ch: {i.scrip_code for i in subs[ch]}  # noqa: E731
    assert "2885" in codes("mf") and "2885" in codes("md") and "2885" not in codes("oi")
    assert {"1", "2"} <= codes("oi") and {"1", "2"} <= codes("mf")
    assert any(i.kind is InstrumentKind.OPTION for i in subs["oi"])
    got = UniverseBuilder.summary(groups, depth_symbols=["RELIANCE"])["subscriptions"]
    assert got == {k: len(v) for k, v in subs.items()}, "the summary reports what was subscribed"


def test_depth_is_subscribed_for_the_archive_sample_only_never_every_strike():
    """~1,000 depth frames a second went through the socket reader for microstructure metrics no
    strategy reads, and at a 30m boundary the reader fell 15 s behind — every book stale at once.
    Depth now rides the rolling set (Engine._sync_depth); the boot subscription is the sample."""
    b = UniverseBuilder(_catalogue(), UniversePolicy(strikes_per_side=2))
    groups = b.build_underlyings([Segment.NSE_FO], TODAY)
    b.select_all(groups, lambda s: 1500.0, TODAY)

    none = UniverseBuilder.subscriptions(groups.values())
    assert none["md"] == [], "no sample named, no standing depth at all"
    assert len(none["mf"]) > 1 and none["oi"], "prices and OI are unaffected"

    sample = UniverseBuilder.subscriptions(groups.values(), depth_symbols=["reliance"])
    assert {i.scrip_code for i in sample["md"]} == {"2885"}, "the underlying, case-insensitively"
    assert not any(i.kind is InstrumentKind.OPTION for i in sample["md"]), "never a strike"
    assert {i.scrip_code for i in sample["mf"]} == {i.scrip_code for i in none["mf"]}

    other = UniverseBuilder.subscriptions(groups.values(), depth_symbols=["NOTLISTED"])
    assert other["md"] == []


# ---------------------------------------------------------------------------------------------------
# microstructure
# ---------------------------------------------------------------------------------------------------


def _book(bp, bq, ap, aq, ts=0.0):
    return BookState([(bp, bq), (bp - 1, 50)], [(ap, aq), (ap + 1, 50)], ts)


def test_l1_ofi_signs_and_magnitudes():
    base = _book(100, 10, 101, 10)
    assert l1_ofi(base, _book(100, 15, 101, 10)) == 5  # bid size up at same price → buying
    assert l1_ofi(base, _book(100, 10, 101, 15)) == -5  # ask size up → selling
    assert l1_ofi(base, _book(101, 5, 102, 5)) == 5 + 10  # bid lifts a level: +Qb −(nothing), ask lifts: +Qa'
    assert l1_ofi(base, base) == 0
    assert l1_ofi(BookState([], [], 0), base) is None


def test_book_derived_metrics():
    b = _book(100, 30, 101, 10)
    assert depth_imbalance(b, 1) == (30 - 10) / 40
    assert microprice(b) == (101 * 30 + 100 * 10) / 40
    assert spread_bps(b) == pytest.approx(1 / 100.5 * 1e4)
    assert depth_imbalance(BookState([], [], 0)) is None


def test_micro_aggregator_buckets_and_stamps():
    m = MicroAggregator(tf_seconds=1800, bucket_of=lambda _c, ts: int(ts // 1800) * 1800)
    m.on_depth("X", [(100, 10)], [(101, 10)], ts=1000.0)
    m.on_depth("X", [(100, 20)], [(101, 10)], ts=1100.0)
    live = m.live("X")
    assert live["updates"] == 2 and live["ofi"] == 10 and live["ofi_buy"] == 10
    m.on_depth("X", [(100, 20)], [(101, 10)], ts=1900.0)  # new bucket
    prev = m.for_bar("X", 0)
    assert prev is not None and prev["updates"] == 2
    assert m.for_bar("X", 1800)["updates"] == 1
    assert "kyle_lambda" in m.stats()["not_computable_without_tape"] and m.stats()["tape_available"] is False


# ---------------------------------------------------------------------------------------------------
# aggregator: session bounds, day extremes, live 1d
# ---------------------------------------------------------------------------------------------------


async def test_ticks_outside_the_session_never_become_bars(equity):
    """MCX: 12 phantom forming bars 150 s after the 23:30 close, built from post-close snapshot
    frames. NSE: pre-open prints would be clamped INTO the 09:15 bar."""
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    pre = ist_ts("2026-09-18", "09:05")
    post = ist_ts("2026-09-18", "15:31")
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 10, "ts": pre})
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 20, "ts": post})
    assert store.forming("RELIANCE", "1m") is None
    assert agg.out_of_session_ticks == 2
    inside = ist_ts("2026-09-18", "09:15")
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 30, "ts": inside})
    assert store.forming("RELIANCE", "1m") is not None and ist_hm(store.forming("RELIANCE", "1m").ts) == "09:15"


async def test_a_day_extreme_that_moved_between_frames_is_credited_to_the_open_bucket(equity):
    store = BarStore()
    agg = Aggregator(store, timeframes=("1m",))
    agg.track(equity)
    agg.state[equity.scrip_code].connected_since = ist_ts("2026-09-18", "09:00")
    t = ist_ts("2026-09-18", "10:00")
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 10, "ts": t, "high": 102.0, "low": 98.0})
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.5, "total_qty": 20, "ts": t + 10, "high": 104.0, "low": 98.0})
    f = store.forming("RELIANCE", "1m")
    assert f.high == 104.0, "the day high rose between frames: a print happened that LastRate never showed"
    assert f.low == 100.0, "the day low did not move, so the bucket low stays what we saw"


async def test_live_daily_bar_closes_at_the_session_close(equity):
    store = BarStore()
    agg = Aggregator(store, timeframes=("1d",))
    agg.track(equity)
    agg.state[equity.scrip_code].connected_since = ist_ts("2026-09-18", "09:00")
    t = ist_ts("2026-09-18", "09:15")
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 100.0, "total_qty": 10, "ts": t})
    await agg.on_tick({"scrip_code": equity.scrip_code, "ltp": 103.0, "total_qty": 40, "ts": ist_ts("2026-09-18", "14:00")})
    f = store.forming("RELIANCE", "1d")
    assert f is not None and f.ts == t and f.high == 103.0 and f.volume == 40
    assert await agg.flush_stale(now=ist_ts("2026-09-18", "15:00")) == 0
    assert await agg.flush_stale(now=session_close_ts(Segment.NSE_EQ, date(2026, 9, 18)) + 1) == 1
    assert store.count("RELIANCE", "1d") == 1 and store.last("RELIANCE", "1d").complete


# ---------------------------------------------------------------------------------------------------
# auth: calendar-bound token
# ---------------------------------------------------------------------------------------------------


def test_session_is_usable_until_it_actually_expires():
    """JWT exp is 23:59:59 IST, fixed. A 30-minute early margin made the last half hour of every
    day a false alarm; nothing may refuse a token the broker still accepts."""
    s = Session(access_token="x", client_code="c", expires_at=time.time() + 600)
    assert s.usable and s.valid and 590 < s.seconds_left <= 600
    dead = Session(access_token="x", client_code="c", expires_at=time.time() - 1)
    assert not dead.usable and dead.seconds_left == 0.0


async def test_partial_bars_are_corrections_not_fidelity_measurements(equity):
    """After a 23:46 restart the reconciler reported 30m exact=0/16: every one a PARTIAL bar made
    of a single closing snapshot. Those are corrections, and must not dilute the metric."""
    store = BarStore()
    t = ist_ts("2026-09-18", "10:15")
    frag = bar(t, 100.5, 100.5, 100.5, 100.5, 7)
    frag.source = BarSource.PARTIAL
    store.close(frag)
    rest = _Rest({"30m": [_row("2026-09-18", "10:15", 100, 101, 99, 100.5, 500)]})
    r = _reconciler(rest, store, equity)
    c = await r.reconcile_bar(frag)
    assert c.partial and c.replaced
    s = r.stats["30m"]
    assert (s.compared, s.exact, s.partial_replaced) == (0, 0, 1)
    assert s.to_json()["exact_pct"] is None
    assert store.last("RELIANCE", "30m").volume == 500
