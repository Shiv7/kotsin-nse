"""Graded-F triggers reach every RT and CT book (operator, 2026-10-06: "lets have all fudkii triggers (including F grade
ones) to simultaneously be published to fudkii-rt and fudki-ct variants and twins, then if they want to discard it, it
can ... let fudkii-rt-n be as is fudkii" · "dont retire the graded-F shadow").

Each book decides for itself and says so on its card: the graded-F shadow on RT-Y's rules as before; RT-Y only through
the gap switch; RT-X discards; the CT-Y-F shadow gap-fades on light volume; CT-Y, CT-X and CT-M discard."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.api import shadow
from kotsin_nse.bars.pivots import Zone
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import GRADED_F_RULES, UNPUBLISHED_BOOKS, UNPUBLISHED_FADE_BOOKS, Engine
from kotsin_nse.market.session import from_ist, ist_today
from kotsin_nse.risk.limits import CT_Y_F_LIMITS, CT_Y_LIMITS, RT_Y_LIMITS
from kotsin_nse.strategy.keys import COUNTER_TREND, SHADOW_BOOKS, StrategyKey
from kotsin_nse.strategy.regime_gates import graded_f_verdicts
from tests._books import held
from tests.test_gate_b_and_gap_fade import _seed, _trigger

from .test_rt_y_cap_and_graded_f import _unpublished
from .test_wide_stop_shadow import _rt_y_trigger

K = StrategyKey


def _skips(rows) -> list[tuple[str, str]]:
    return sorted((r["book"], r["gate"]) for r in rows if r.get("kind") == "rt_twin.skipped")


def test_every_rt_and_ct_book_is_offered_a_graded_f_trigger_and_rt_n_is_as_fudkii():
    assert UNPUBLISHED_BOOKS == (K.FUDKII_RT_Y_F, K.FUDKII_RT_Y, K.FUDKII_RT_X)
    assert UNPUBLISHED_FADE_BOOKS == (K.FUDKII_CT_Y_F, K.FUDKII_CT_Y, K.FUDKII_CT_X, K.FUDKII_CT_M)
    assert K.FUDKII not in UNPUBLISHED_BOOKS and K.FUDKII_RT_N not in UNPUBLISHED_BOOKS
    assert set(GRADED_F_RULES) == {K.FUDKII_RT_X, K.FUDKII_RT_Y, K.FUDKII_CT_Y, K.FUDKII_CT_X, K.FUDKII_CT_M}
    assert K.FUDKII_CT_Y_F in SHADOW_BOOKS and K.FUDKII_CT_Y_F in COUNTER_TREND
    assert CT_Y_F_LIMITS.gap_fade_max_rel_vol == 1.5 and replace(CT_Y_F_LIMITS, gap_fade_max_rel_vol=3.0) == CT_Y_LIMITS, \
        "CT-Y's fade, plan and exits with one number changed"


@pytest.mark.asyncio
async def test_rt_y_takes_a_graded_f_trigger_only_through_the_gap_switch_and_rt_x_discards_it(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        e._past_last_entry = lambda *a, **k: None  # type: ignore[method-assign]
        trig = _unpublished(sig)
        # NYKAA 5 Oct 09:45 as measured: a 1.26 daily-ATR gap on 5.7x the NIFTY50's volume
        e._breadth_at[trig.signal_id] = {"share": 0.7, "names": 200, "openBar": True, "gapDatr": 1.26, "relVol": 5.68}
        await e._handle_unpublished(trig, None)
        assert sorted(p.strategy for p in held(e)) == ["FUDKII_RT_Y", "FUDKII_RT_Y_W1"], \
            "RT-Y follows it (its wide-stop shadow copies the fill); the graded-F shadow keeps RT-Y's old gap gate"
        ev = await e.ledger.rows_between("events", 0, time.time() + 60)
        assert _skips(ev) == [("FUDKII_RT_X", "graded_f"), ("FUDKII_RT_Y_F", "open_gap")]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_without_the_switch_rt_y_says_why_it_stands_aside(settings):
    e, _opt, sig = await _rt_y_trigger(settings, Direction.BULLISH, 0.7)
    try:
        trig = _unpublished(sig)
        e._breadth_at[trig.signal_id] = {"share": 0.7, "names": 200, "openBar": True, "gapDatr": 0.38, "relVol": 2.83}
        await e._handle_unpublished(trig, None)
        assert not e.positions
        ev = [r for r in await e.ledger.rows_between("events", 0, time.time() + 60) if r.get("kind") == "rt_twin.skipped"]
        why = {r["book"]: r["reason"] for r in ev}
        assert why["FUDKII_RT_Y"] == GRADED_F_RULES[K.FUDKII_RT_Y] and why["FUDKII_RT_X"] == GRADED_F_RULES[K.FUDKII_RT_X]
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("ctx", "faded", "gate"), [
    ({"openBar": True, "gapDatr": 0.45, "relVol": 1.2}, True, None),               # DMART 29 Sep: 0.33 gap, 1.16x
    ({"openBar": True, "gapDatr": 0.45, "relVol": 3.01}, False, "gap_fade_blocked"),  # PETRONET 5 Oct: 3.01x
    ({"openBar": False, "gapDatr": 0.05, "relVol": 0.9}, False, "no_gap"),
])
async def test_the_ct_y_f_shadow_fades_a_graded_f_gap_on_light_volume_and_the_ct_books_discard_it(settings, ctx, faded, gate):
    e = Engine(settings)
    await e.start()
    try:
        _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
        trig = _unpublished(_trigger(Direction.BULLISH))
        e._breadth_at[trig.signal_id] = {"share": 0.6, "names": 220, "atr30": 2.0, **ctx}
        handled = []

        async def capture(sig, bar, *, adopt=True):
            handled.append(sig)

        e._handle_signal = capture  # type: ignore[method-assign]
        await e._handle_counter_unpublished(trig, None)
        ev = await e.ledger.rows_between("events", 0, time.time() + 5)
        skips = _skips(ev)
        assert {("FUDKII_CT_Y", "graded_f"), ("FUDKII_CT_X", "graded_f"), ("FUDKII_CT_M", "graded_f")} <= set(skips)
        if faded:
            fade = handled[0]
            assert fade.strategy is K.FUDKII_CT_Y_F and fade.direction is Direction.BEARISH and fade.source_signal_id == trig.signal_id
            assert fade.reason.startswith("GAP FADE (graded F) of") and trig.signal_id in e._gap_faded_f and trig.signal_id not in e._gap_faded
            assert [x["kind"] for x in ev if x["kind"].startswith("counter.gap_fade")] == ["counter.gap_fade_f"], "never read as CT-Y's own"
        else:
            assert handled == [] and ("FUDKII_CT_Y_F", gate) in skips
        await e._handle_counter_unpublished(trig, None)
        assert len(handled) == (1 if faded else 0), "one fade per trigger"
    finally:
        await e.stop()


def test_the_graded_f_verdicts_say_what_each_book_did():
    sgn = {"signal_id": "S", "direction": "BULLISH"}
    ctx = {"kind": "regime.breadth", "share": 0.62, "names": 218, "openBar": True, "gapDatr": 1.26, "relVol": 5.68}
    v = graded_f_verdicts(sgn, [ctx], rt_y_held=True, lim_y=RT_Y_LIMITS)
    assert v["rtY"]["action"] == "TAKE" and v["rtY"]["why"][0].startswith("gap switch: 09:45 gap 1.26")
    assert v["ctY"]["action"] == "NONE" and v["ctY"]["why"] == "graded F — CT-Y fades only published triggers"
    assert v["ctM"]["action"] == "NONE" and v["ctM"]["why"].startswith("graded F")
    skip = {"kind": "rt_twin.skipped", "book": "FUDKII_RT_Y", "gate": "graded_f", "reason": GRADED_F_RULES[K.FUDKII_RT_Y]}
    f_ev = {"kind": "counter.gap_fade_f", "side": "PE", "stop": 188.3, "targets": [184.0], "rr": 1.62, "grade": "C", "gapDatr": 0.45,
            "stopNote": "0.1 ATR30 past 1wk.R1 188.10 — nearer than 1 ATR30"}
    v = graded_f_verdicts(sgn, [ctx, skip, f_ev], rt_y_held=False, lim_y=RT_Y_LIMITS)
    assert (v["rtY"]["action"], v["rtY"]["state"]) == ("SKIP", "graded F")
    assert v["ctY"]["action"] == "GAP FADE via CT-Y-F" and v["ctY"]["stop"] == 188.3 and "1wk.R1" in v["ctY"]["why"]
    blocked = {**f_ev, "blocked": "volume 3.0x the NIFTY50's (≥ 1.5x) — the gap is followed in trend, not faded"}
    assert graded_f_verdicts(sgn, [ctx, blocked], rt_y_held=False, lim_y=RT_Y_LIMITS)["ctY"]["why"].startswith("graded F — CT-Y-F: volume")


@pytest.mark.asyncio
async def test_every_nse_book_cards_a_graded_f_trigger_with_its_own_decision(settings):
    e = Engine(settings)
    await e.start()
    try:
        e.underlyings["TCS"] = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        bar_ts = int(from_ist(datetime.combine(ist_today(), dtime(9, 15))))
        trig = _unpublished(replace(_trigger(Direction.BULLISH, bar_ts), symbol="TCS"))
        await e.ledger.insert_signal(trig.to_json(), "NOT_PUBLISHED", "FUDKII's own confluence_grade: grade F")
        await e.ledger.event("regime.breadth", {"signal_id": trig.signal_id, "symbol": "TCS", "share": 0.3, "names": 200,
                                                "openBar": False, "gapDatr": 0.1})
        for k in GRADED_F_RULES:
            await e._book_skip(k.value, trig, GRADED_F_RULES[k], gate="graded_f")
        await e._book_skip("FUDKII_CT_Y_F", trig, "not a 09:45 gap of ≥ 0.3 daily ATR its own way", gate="no_gap")
        cards = {b: (await e.book_cards(b, ist_today()))["cards"] for b in
                 ("FUDKII", "FUDKII_RT_N", "FUDKII_RT_X", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y", "FUDKII_CT_M", "FUDKII_CT_Y_F",
                  "FUDKII_RT_Y_F", "FUDKII_RT_MCX")}
        assert cards["FUDKII_RT_MCX"] == [] and all(len(v) == 1 for b, v in cards.items() if b != "FUDKII_RT_MCX")
        for b in ("FUDKII", "FUDKII_RT_N"):
            assert cards[b][0]["state"] == "NOT_PUBLISHED" and cards[b][0]["cta"]["reason"].startswith("not published"), b
        for k, why in GRADED_F_RULES.items():
            c = cards[k.value][0]
            assert c["state"] == "SKIPPED" and c["cta"]["enabled"] is False and c["cta"]["reason"] == why, k
        cf = cards["FUDKII_CT_Y_F"][0]
        assert cf["state"] == "SKIPPED" and cf["side"] == "PE" and cf["cta"]["reason"].startswith("paper shadow")
        v = cards["FUDKII_RT_X"][0]["verdicts"]
        assert (v["rtY"]["action"], v["rtY"]["state"]) == ("SKIP", "graded F") and v["ctY"]["why"].startswith("graded F — CT-Y-F: not a 09:45 gap")
        assert {r["book"] for r in cards["FUDKII"][0]["books"]} >= {"FUDKII_RT_Y_F", "FUDKII_CT_Y_F"}
    finally:
        await e.stop()


def test_the_shadow_pages_graded_f_tab_shows_rt_y_and_the_ct_y_f_shadow_beside_rt_y_f():
    def row(sid, decision, sym="TCS", strategy="FUDKII", source=None):
        return {"signal_id": sid, "strategy": strategy, "symbol": sym, "direction": "BULLISH", "ts": 1790568900, "decision": decision,
                "decision_reason": "graded F", "targets": [], "source_signal_id": source}

    sigs = [row("B", "NOT_PUBLISHED"), row("C", "NOT_PUBLISHED", "INFY"), row("FC", "PAPER_FILLED", "INFY", "FUDKII_CT_Y_F", "C")]
    ev = [{"kind": "regime.breadth", "signal_id": s} for s in ("B", "C")]
    pos = [{"id": "y1", "strategy": "FUDKII_RT_Y", "signal_id": "B", "status": "CLOSED"},
           {"id": "f1", "strategy": "FUDKII_CT_Y_F", "signal_id": "FC", "status": "CLOSED"}]
    got = shadow.graded_f_summary(signals=sigs, positions=pos, trades=[{"position_id": "y1", "net": 800.0}, {"position_id": "f1", "net": -300.0}],
                                  events=ev)
    by = {r["symbol"]: r for r in got["rows"]}
    assert by["TCS"]["rt_y"] == {"status": "EXITED", "net": 800.0} and by["INFY"]["ct_y_f"] == {"status": "EXITED", "net": -300.0}
    t = got["total"]
    assert (t["rt_y_traded"], t["rt_y_net"], t["ct_y_f_traded"], t["ct_y_f_net"]) == (1, 800.0, 1, -300.0)


@pytest.mark.asyncio
async def test_the_level_reader_reads_a_graded_f_trigger_and_nothing_trades_on_it(settings, monkeypatch):
    # operator, 2026-10-06: "yes make graded-F triggers run the level reader" — KOTAKBANK 09:45 crossed its monthly R1
    # and, graded F, was never read. The route is stamped and recorded; no fade book enters on it, even on COUNTER.
    import kotsin_nse.engine as engine_mod
    from kotsin_nse.bars.unified import BarSource, UnifiedBar
    from kotsin_nse.strategy.counter import NO_WALL, CounterDecision

    e = Engine(settings)
    await e.start()
    try:
        _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
        trig = _unpublished(_trigger(Direction.BULLISH))
        e._breadth_at[trig.signal_id] = {"share": 0.6, "names": 220, "atr30": 2.0, "openBar": False, "gapDatr": 0.05}
        handled = []

        async def capture(sig, bar, *, adopt=True):
            handled.append(sig)

        async def no_legs(und, bar, *, low_priority=False):
            assert low_priority, "an unpublished trigger reads on the low-priority futures queue"
            return []

        e._handle_signal = capture  # type: ignore[method-assign]
        monkeypatch.setattr(e, "_counter_legs", no_legs)
        monkeypatch.setattr(engine_mod, "counter_route", lambda legs, **kw: CounterDecision("COUNTER", "at pivot", NO_WALL))
        bar = UnifiedBar(symbol=trig.symbol, scrip_code=trig.symbol, tf="30m", ts=int(trig.ts), open=1, high=1, low=1, close=1,
                         volume=1, source=BarSource.REST, complete=True)
        await e._handle_counter_unpublished(trig, bar)
        ev = await e.ledger.rows_between("events", 0, time.time() + 5)
        routes = [x for x in ev if x["kind"] == "counter.route"]
        assert len(routes) == 1 and routes[0]["route"] == "COUNTER" and routes[0]["published"] is False
        assert handled == [], "read, recorded — never traded"
        # no candle, no reading
        await e._handle_counter_unpublished(_unpublished(_trigger(Direction.BEARISH)), None)
        ev = await e.ledger.rows_between("events", 0, time.time() + 5)
        assert len([x for x in ev if x["kind"] == "counter.route"]) == 1
    finally:
        await e.stop()
