"""Operator, 2026-09-28: "all strategies 'see' all these triggers always as soon as they are generated
and push them in parallel not in series". Every SuperTrend flip with the close through the band is a
trigger, decided the moment it is generated, every path side by side. The in-trend books and the fade
route take FUDKII's published signals; a trigger it grades F and does not publish reaches the graded-F
shadow alone (2026-09-28 night) until each book's own grade rule is proven (phase19: fades taking graded-F
triggers added +₹3,262 over 24 Aug–28 Sep, 8 of 16 extra fades won — not yet proven)."""

from __future__ import annotations

import asyncio
import time

import pytest

from kotsin_nse.bars.pivots import Zone
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import IN_TREND_BOOKS
from kotsin_nse.strategy.base import Outcome, Signal, published
from kotsin_nse.strategy.fudkii import Fudkii
from kotsin_nse.strategy.keys import StrategyKey

from .test_rt_twin import RELIANCE, RELIANCE_OPT, _paper
from .test_strategies import FakeCtx, _breakout_series

# -- the strategy: every trigger, published or not -------------------------------------------------


def test_a_trigger_fudkii_grades_f_is_still_a_trigger():
    bars = _breakout_series()
    close = bars[-1].close
    zones = [Zone(price=close * 0.90, strength=8.0, members=["1d.S1", "1wk.S1"]),
             Zone(price=close * 1.001, strength=8.0, members=["1d.R1", "1wk.R1"])]
    out = Fudkii().on_bar(FakeCtx(bars, zones), bars[-1])
    assert not out.signals and out.rejections[0].binding_gate == "confluence_grade", "the parent's rule is unchanged"
    assert len(out.triggers) == 1
    trig = out.triggers[0]
    assert not published(trig) and trig.grade == "F" and trig.stop < trig.entry
    parent = trig.context["parent"]
    assert parent["gate"] == "confluence_grade" and "grade F" in parent["reason"]


def test_a_published_signal_says_so_and_a_non_trigger_is_nothing():
    bars = _breakout_series()
    out = Fudkii().on_bar(FakeCtx(bars), bars[-1])
    assert out.signals and not out.triggers and published(out.signals[0])
    quiet = FakeCtx(__import__("tests.conftest", fromlist=["series"]).series([100.0 + i * 0.01 for i in range(60)]))
    assert not Fudkii().on_bar(quiet, quiet._bars[-1]).triggers, "no flip + break: no trigger"


# -- the engine: every book at once, side by side ----------------------------------------------------


def _sig(**kw) -> Signal:
    base = dict(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BULLISH, ts=int(time.time() // 1800 * 1800) - 1800,
                entry=1500.0, stop=1490.0, targets=(1550.0,), grade="A", rr=5.0, reason="ST flip UP + close above upper band")
    base.update(kw)
    return Signal(**base)


@pytest.mark.asyncio
async def test_the_bars_paths_run_side_by_side_and_an_unpublished_trigger_goes_to_the_graded_f_shadow_alone(settings):
    e = await _paper(settings)
    try:
        pub = _sig()
        trig = _sig(symbol="TCS", ts=pub.ts, grade="F", targets=(), context={"parent": {"published": False, "gate": "confluence_grade",
                                                                                          "reason": "grade F rr=0.0 no wall ahead — no target"}})
        e.fudkii.on_bar = lambda ctx, bar: Outcome(signals=[pub], triggers=[trig])  # type: ignore[method-assign]
        e.fukaa.on_signal = lambda ctx, bar, sig: Outcome()  # type: ignore[method-assign]

        async def no_breadth(sig):
            return None

        e._log_breadth = no_breadth  # type: ignore[method-assign]
        calls: list[tuple[str, float, float, dict]] = []

        async def handle(sig, bar, **kw):
            t0 = time.perf_counter()
            await asyncio.sleep(0.2)
            calls.append(("signal", t0, time.perf_counter(), {"sig": sig.signal_id, **kw}))

        async def counter(sig, bar):
            t0 = time.perf_counter()
            await asyncio.sleep(0.2)
            calls.append(("counter", t0, time.perf_counter(), {"sig": sig.signal_id}))

        async def unpublished(sig, bar):
            t0 = time.perf_counter()
            await asyncio.sleep(0.2)
            calls.append(("unpublished", t0, time.perf_counter(), {"sig": sig.signal_id}))

        e._handle_signal = handle  # type: ignore[method-assign]
        e._handle_counter = counter  # type: ignore[method-assign]
        e._handle_unpublished = unpublished  # type: ignore[method-assign]
        from .test_rt_twin import _bars30

        bar = _bars30("RELIANCE", "2885", [10_000.0] * 8)[-1]
        t0 = time.perf_counter()
        await e._decide(bar)
        took = time.perf_counter() - t0
        assert len(calls) == 3 and took < 0.35, "three paths of 0.2 s each, run together — not 0.6 s in series"
        assert max(c[1] for c in calls) < min(c[2] for c in calls), "every path started before any finished"
        sent = {c[3]["sig"]: c[3] for c in calls if c[0] == "signal"}
        assert set(sent) == {pub.signal_id}, "the in-trend books keep the parent's grade: only the published signal"
        assert sent[pub.signal_id]["books"] == IN_TREND_BOOKS and sent[pub.signal_id]["adopt"] is True
        assert {c[3]["sig"] for c in calls if c[0] == "counter"} == {pub.signal_id}, "the fade route: published triggers only (phase19 pending)"
        assert {c[3]["sig"] for c in calls if c[0] == "unpublished"} == {trig.signal_id}, "the unpublished one: the graded-F shadow's path"
        assert trig.signal_id in e._signals_today, "known to today's book, so a card's TAKE never 404s"
        rows = {r["signal_id"]: r for r in await e.ledger.rows_between("signals", 0, time.time() + 60)}
        assert rows[trig.signal_id]["decision"] == "NOT_PUBLISHED" and "no wall ahead" in rows[trig.signal_id]["decision_reason"], \
            "the unpublished trigger's own row, written before its paths ran"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_unpublished_trigger_that_reaches_the_books_is_refused_by_the_parent(settings):
    """A safety net: the bar never sends an unpublished trigger to the in-trend books, but should one
    arrive, FUDKII refuses it as its own rule (HAVELLS 2026-09-28 09:45: graded F, no wall ahead) and
    records that on the trigger's row; the twins decide for themselves."""
    from kotsin_nse.exec.paper import BookSnapshot
    from kotsin_nse.instrument.select import Quote, Selection

    e = await _paper(settings)
    try:
        e.underlyings[RELIANCE.symbol] = RELIANCE

        async def select(underlying, sig, *, tape=True):
            return Selection(RELIANCE_OPT, premium=50.0, reason="ok", spread_pct=0.5)

        e._select_instrument = select  # type: ignore[method-assign]
        now = time.time()
        e.books[RELIANCE_OPT.scrip_code] = BookSnapshot(RELIANCE_OPT.scrip_code, bids=[(49.95, 500_000)], asks=[(50.0, 500_000)], ts=now)
        e.quotes[RELIANCE_OPT.scrip_code] = Quote(ltp=50.0, bid=49.95, ask=50.0, ts=now)
        e.ltps[RELIANCE_OPT.scrip_code] = 50.0
        trig = _sig(grade="F", targets=(), context={"parent": {"published": False, "gate": "confluence_grade",
                                                              "reason": "grade F rr=0.0 no wall ahead — no target"}})
        out = await e._handle_signal(trig, None, books=IN_TREND_BOOKS, adopt=False)
        assert out["FUDKII"]["decision"] == "NOT_PUBLISHED" and "no wall ahead" in out["FUDKII"]["reason"]
        assert sorted(p.strategy for p in e.positions.values()) == ["FUDKII_RT_N", "FUDKII_RT_X", "FUDKII_RT_Y", "FUDKII_RT_Y_W1"]
        rows = [r for r in await e.ledger.rows_between("signals", 0, time.time() + 60) if r["signal_id"] == trig.signal_id]
        assert rows and rows[-1]["decision"] == "NOT_PUBLISHED", "the trigger's row carries the parent's own decision"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_concurrent_readers_share_one_futures_fetch(settings):
    e = await _paper(settings)
    try:
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        fut = Instrument("68781", "RELIANCE", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=250, expiry="2099-12-31", underlying="RELIANCE")
        e.catalogue_loader.catalogue.futures_by_symbol["RELIANCE"] = [fut]
        from .test_rt_twin import _bars30

        e.store.seed("RELIANCE", "30m", _bars30("RELIANCE", "2885", [10_000.0] * 8))
        asked = []

        async def candles(inst, tf, start, end):
            asked.append(tf)
            await asyncio.sleep(0.05)
            return []

        e.rest.candles = candles  # type: ignore[method-assign]
        e.quote_wait_s = 0.0
        a, b = await asyncio.gather(e._fut_context(und), e._fut_context(und))
        assert a is b and asked.count("30m") == 1 and asked.count("1d") == 1, "one fetch for both readers"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_unpublished_trigger_is_carded_only_for_the_books_that_see_it(settings):
    e = await _paper(settings)
    try:
        e.underlyings["TCS"] = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        from datetime import datetime
        from datetime import time as dtime

        from kotsin_nse.market.session import from_ist, ist_today

        # today's 09:15 bar, whatever the clock: "now − 30 min" was yesterday between 00:00 and 00:30
        bar_ts = int(from_ist(datetime.combine(ist_today(), dtime(9, 15))))
        trig = _sig(symbol="TCS", ts=bar_ts, grade="F", targets=(), context={"parent": {"published": False, "gate": "confluence_grade",
                                                                                          "reason": "grade F rr=0.0 no wall ahead — no target"}})
        await e.ledger.insert_signal(trig.to_json(), "NOT_PUBLISHED", "FUDKII's own confluence_grade: grade F")

        seen = {b: [c["symbol"] for c in (await e.book_cards(b, ist_today()))["cards"]] for b in
                ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y")}
        assert seen["FUDKII"] == ["TCS"], "the parent's own card, with its own reason"
        assert seen["FUDKII_RT_X"] == seen["FUDKII_RT_Y"] == seen["FUDKII_CT_X"] == seen["FUDKII_CT_Y"] == [], "no trading book is offered it"
        card = (await e.book_cards("FUDKII", ist_today()))["cards"][0]
        assert card["state"] == "NOT_PUBLISHED"
        assert card["cta"]["enabled"] is False and card["cta"]["reason"].startswith("not published — FUDKII's own confluence_grade"), \
            "no TAKE the parent would refuse (review, 2026-09-28)"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_graded_f_shadows_tab_cards_the_unpublished_triggers_alone_with_its_own_wallet(settings):
    """FUDKII-RT-Y-F's Alerts tab (operator, 2026-09-29): the triggers FUDKII did not publish — the
    only ones the shadow is offered — each with what the SHADOW did, never a published trigger, and
    the shadow's own wallet; no operator TAKE on a paper shadow."""
    e = await _paper(settings)
    try:
        from datetime import datetime
        from datetime import time as dtime

        from kotsin_nse.market.session import from_ist, ist_today

        e.underlyings["TCS"] = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        e.underlyings["INFY"] = Instrument("1594", "INFY", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="INFY")
        bar_ts = int(from_ist(datetime.combine(ist_today(), dtime(9, 15))))
        f = _sig(symbol="TCS", ts=bar_ts, grade="F", targets=(), context={"parent": {"published": False, "gate": "confluence_grade",
                                                                                      "reason": "grade F rr=0.0 no wall ahead — no target"}})
        await e.ledger.insert_signal(f.to_json(), "NOT_PUBLISHED", "FUDKII's own confluence_grade: grade F")
        pub = _sig(symbol="INFY", ts=bar_ts)
        await e.ledger.insert_signal(pub.to_json(), "LIMIT_UNFILLED", "limit not filled in 60 s")
        await e._book_skip("FUDKII_RT_Y_F", f, "breadth 30% of 200 names agree ≤ 50% — the market is not with the breakout", gate="breadth")

        out = await e.book_cards("FUDKII_RT_Y_F", ist_today())
        assert [c["symbol"] for c in out["cards"]] == ["TCS"], "the unpublished trigger alone — never a published one"
        card = out["cards"][0]
        assert card["state"] == "SKIPPED" and card["skip"]["gate"] == "breadth", "what the SHADOW did, with its own reason"
        assert card["cta"]["enabled"] is False and card["cta"]["reason"].startswith("paper shadow"), "no operator TAKE"
        assert out["wallet"]["strategy"] == "FUDKII_RT_Y_F" and out["wallet"]["initial"] == e.wallets["FUDKII_RT_Y_F"].initial
        assert [c["symbol"] for c in (await e.book_cards("FUDKII_RT_Y", ist_today()))["cards"]] == ["INFY"], "RT-Y's tab is unchanged"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_unpublished_triggers_futures_read_never_queues_behind_the_gates(settings):
    """Its own, smaller queue: with every permit of the gates' queue held, it still reads."""
    e = await _paper(settings)
    try:
        und = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        fut = Instrument("68781", "RELIANCE", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=250, expiry="2099-12-31", underlying="RELIANCE")
        e.catalogue_loader.catalogue.futures_by_symbol["RELIANCE"] = [fut]
        from .test_rt_twin import _bars30

        e.store.seed("RELIANCE", "30m", _bars30("RELIANCE", "2885", [10_000.0] * 8))

        async def candles(inst, tf, start, end):
            return []

        e.rest.candles = candles  # type: ignore[method-assign]
        for _ in range(4):
            await e._fut_sem.acquire()  # the gates' queue fully busy
        try:
            got = await asyncio.wait_for(e._fut_context(und, low_priority=True), timeout=1.0)
            assert got is not None
        finally:
            for _ in range(4):
                e._fut_sem.release()
    finally:
        await e.stop()
