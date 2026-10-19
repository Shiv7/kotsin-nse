"""FUDKII-CT-B1, the CTB-19Oct B1 gap fade (operator, 2026-10-19: "Both together Wednesday" — the official-open gap and
the paper book "trading every B1 at 0.25, recording at_030 + conviction"): every FUDKII trigger on the 09:15 candle,
NSE stocks and indices, published or graded F, is faded when its OFFICIAL gap is at least 0.25 daily ATR its own way and
no key pivot sits within 0.5 ATR30 ahead of the close — PE for a bullish trigger, CE for a bearish one, CT-Y's gap-fade
stop, one target 1 ATR30 from the close on the underlying, out by 11:15 — on its own path, wallet and exits, beside every
other book and never instead of one.

The named cases are 19 Oct's (resume file CTB-19Oct_README.md): PNB gapped 1.58 official with nothing ahead (group 1, a
fade); CANBK's gap was pinned by its monthly pivot 0.05 ATR30 ahead (in trend, B1x); UNIONBANK's 0.2495 is the near
miss — not B1."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime
from datetime import time as dtime

import pytest

from kotsin_nse.bars.pivots import PivotPoint, Zone
from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    ExitReason,
    Instrument,
    InstrumentKind,
    OptionType,
    Position,
    PosSide,
)
from kotsin_nse.engine import BOOK_LABELS, NO_PREMIUM_FLOOR, ORDER_CODES, Engine
from kotsin_nse.market.session import IST
from kotsin_nse.risk.exits import ExitEngine, MarketView
from kotsin_nse.risk.limits import (
    B1_GAP_DATR,
    B1_GAP_LINE,
    B1_PIN_ATR,
    B1_TARGET_ATR,
    B1_TIME_STOP_HM,
    CT_B1_LIMITS,
    CT_Y_LIMITS,
)
from kotsin_nse.strategy.b1 import NO_READ5, b1_read, conviction
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.catalog import BOOKS
from kotsin_nse.strategy.keys import (
    COUNTER_TREND,
    SHADOW_BOOKS,
    StrategyKey,
    describe_book,
    stop_mirrors_of,
)
from tests._books import held
from tests.test_gate_b_and_gap_fade import UND, _open_ts, _paper, _seed, _trigger

B1 = StrategyKey.FUDKII_CT_B1
PE = Instrument("153806", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, name="TATASTEEL 27 OCT 2026 PE 185.00", lot_size=2750,
                tick_size=0.01, strike=185.0, option_type=OptionType.PE, underlying="TATASTEEL")
#: 19 Oct, as logged (PNB): official gap 1.58, the first trade's 1.664, nothing ahead within 0.5 ATR30
PNB_LIKE = {"share": 0.44, "names": 220, "openBar": True, "gapDatr": 1.664, "gapOfficial": 1.58, "gapOpenSrc": "preopen",
            "officialOpen": 190.4, "officialClose": 184.0, "dAtr": 4.0, "atr30": 2.0, "relVol": 15.6,
            "pivotsAheadAtr": [["1d.R2", 0.9], ["1wk.R1", 1.4]], "gapRule": "first_tick"}
#: CANBK 19 Oct: a real gap, pinned — its monthly pivot 0.05 ATR30 ahead of the close
CANBK_LIKE = {**PNB_LIKE, "gapOfficial": 0.62, "pivotsAheadAtr": [["1mo.PIVOT", 0.05], ["1d.R1", 0.7]]}
#: UNIONBANK 19 Oct: (172.9 - 171.68) / 4.8906 = 0.2495 — under the line
UNIONBANK_LIKE = {**PNB_LIKE, "gapOfficial": 0.249457, "gapDatr": 0.452}
#: the engine's classic levels for the name: one, far away — the pin test can be read, and no stop moves to it
FAR = [PivotPoint(100_000.0, "1mo.R4", "1mo", 2.0)]


# -- the read ------------------------------------------------------------------------------------------------


def test_the_rule_and_its_numbers_are_the_operators():
    assert (B1_GAP_DATR, B1_GAP_LINE, B1_PIN_ATR, B1_TARGET_ATR, B1_TIME_STOP_HM) == (0.25, 0.30, 0.5, 1.0, "11:15")


def test_a_pnb_like_gap_with_nothing_ahead_is_b1_and_reads_its_conviction():
    zones = [Zone(181.0, 6.0, ["1d.S1", "1wk.PIVOT"]), Zone(188.6, 5.6, ["1d.R1", "1mo.R1"]), Zone(186.0, 3.0, ["1d.BC"])]
    r = b1_read(PNB_LIKE, close=187.25, bullish=True, zones=zones)
    assert r["take"] is True and r["gate"] is None and r["at_025"] and r["at_030"] and not r["keyPivotAhead"]
    c = r["conviction"]
    # gap 1.58 -> 2; stretch (187.25 - 184) / 4 = 0.81 -> 0.5; the fade side's wall 181 is 3.1 ATR30 away -> 1.5;
    # a trigger-side wall at 188.6, 0.68 ATR30 ahead -> 0.5; the weak 186 is no wall
    assert c["points"] == {"gap": 2.0, "stretch": 0.5, "room": 1.5, "trigWall": 0.5} and c["total"] == 4.5 and c["of"] == 5.0
    assert c["read5"] is None and c["note"] == NO_READ5 and "not computed in the engine" in c["note"]
    assert c["roomWall"]["price"] == 181.0 and c["trigWallAtr"] == pytest.approx(0.675)


def test_a_canbk_like_pinned_gap_is_in_trend_not_b1():
    r = b1_read(CANBK_LIKE, close=187.25, bullish=True, zones=[])
    assert r["take"] is False and r["gate"] == "pivot_pin" and r["keyPivotAhead"] and r["at_025"] and r["at_030"]
    assert r["pins"] == [["1mo.PIVOT", 0.05]] and "1mo.PIVOT +0.05" in r["why"] and "B1x" in r["why"]


def test_unionbank_like_0_2495_is_not_b1_and_records_both_lines():
    r = b1_read(UNIONBANK_LIKE, close=187.25, bullish=True, zones=[])
    assert r["take"] is False and r["gate"] == "no_gap" and r["at_025"] is False and r["at_030"] is False
    assert "0.249" in r["why"] and r["conviction"]["points"]["gap"] == 0.0


def test_premierene_like_0_252_is_b1_at_the_025_line_only():
    r = b1_read({**PNB_LIKE, "gapOfficial": 0.252364}, close=187.25, bullish=True, zones=[])
    assert r["take"] is True and r["at_025"] is True and r["at_030"] is False


def test_what_b1_does_not_read():
    assert b1_read({**PNB_LIKE, "openBar": False}, close=187.25, bullish=True, zones=[])["gate"] == "not_open_bar"
    assert b1_read({**PNB_LIKE, "gapOfficial": None}, close=187.25, bullish=True, zones=[])["gate"] == "no_gap_value"
    assert b1_read({**PNB_LIKE, "gapOfficial": -0.6}, close=187.25, bullish=True, zones=[])["gate"] == "no_gap", "a gap against"
    far = b1_read({**PNB_LIKE, "pivotsAheadAtr": [["1d.R3", 0.1], ["1wk.R1", 0.51]]}, close=187.25, bullish=True, zones=[])
    assert far["take"] is True, "R3 is not a key level, and 0.51 is past the reach"
    # no volume, breadth or grade gate: a light, against-the-market gap is still B1
    assert b1_read({**PNB_LIKE, "relVol": 0.8, "share": 0.1}, close=187.25, bullish=True, zones=[])["take"] is True


@pytest.mark.parametrize(("gap", "pts"), [(0.25, 0.5), (0.49, 0.5), (0.5, 1.0), (1.0, 1.5), (1.49, 1.5), (1.5, 2.0), (3.0, 2.0)])
def test_the_gap_tiers(gap, pts):
    assert conviction({**PNB_LIKE, "gapOfficial": gap}, close=187.25, bullish=True, zones=[])["points"]["gap"] == pts


def test_stretch_room_and_the_trigger_wall_tiers():
    def c(close, zones, **kw):
        return conviction({**PNB_LIKE, **kw}, close=close, bullish=False, zones=zones)["points"]

    # bearish: stretch = (official close - close) / dATR; the fade side is ABOVE the close
    assert c(184.0 - 0.79 * 4, [])["stretch"] == 0.0 and c(184.0 - 0.8 * 4, [])["stretch"] == 0.5
    assert c(184.0 - 1.5 * 4, [])["stretch"] == 1.0 and c(184.0 - 2.5 * 4, [])["stretch"] == 1.5
    assert c(180.0, [])["room"] == 1.5, "no wall on the fade's side: all the room there is"
    assert c(180.0, [Zone(180.9, 6.0)])["room"] == 0.0 and c(180.0, [Zone(181.0, 6.0)])["room"] == 0.5
    assert c(180.0, [Zone(182.0, 6.0)])["room"] == 1.0 and c(180.0, [Zone(184.0, 6.0)])["room"] == 1.5
    assert c(180.0, [Zone(178.0, 6.0)])["trigWall"] == 0.5 and c(180.0, [Zone(177.9, 6.0)])["trigWall"] == 0.0
    assert c(180.0, [Zone(181.0, 6.0)], atr30=0)["room"] == 0.0, "no ATR30: the room cannot be read"
    assert conviction({**PNB_LIKE, "gapOfficial": 3.0, "officialClose": 150.0}, close=187.25, bullish=True,
                      zones=[Zone(188.0, 6.0)])["total"] == 5.0, "capped at 5"


# -- the engine: judged on the 09:15 candle, on its own path -------------------------------------------------------


async def _judge(settings, ctx: dict, *, direction: Direction = Direction.BULLISH, published: bool = True, ts: int | None = None,
                 zones: list[Zone] | None = None, pivots: bool = True) -> tuple[Engine, Signal, list, list[dict]]:
    """``_b1_fade`` on a TATASTEEL trigger logged with ``ctx``: what reached ``_handle_signal`` and the events."""
    e = Engine(settings)
    await e.start()
    _seed(e, zones=zones or [Zone(184.0, 6.0, ["1d.PIVOT", "1wk.BC"])])
    if pivots:
        e._pivot_points = lambda symbol: FAR  # type: ignore[method-assign]
    trig = _trigger(direction, ts)
    if not published:
        trig = replace(trig, grade="F", context={"parent": {"published": False, "gate": "grade", "reason": "grade F rr=0.0 no wall ahead"}})
    e._breadth_at[trig.signal_id] = ctx
    handled: list = []

    async def capture(sig, bar, *, adopt=True):
        handled.append((sig, adopt))

    e._handle_signal = capture  # type: ignore[method-assign]
    await e._b1_fade(trig, None)
    events = await e.ledger.rows_between("events", 0, time.time() + 5)
    return e, trig, handled, events


@pytest.mark.asyncio
async def test_ct_b1_fades_a_pnb_like_gap_with_the_put_ct_ys_stop_and_one_target(settings):
    e, trig, handled, events = await _judge(settings, PNB_LIKE)
    try:
        assert len(handled) == 1
        fade, adopt = handled[0]
        assert fade.strategy is B1 and fade.direction is Direction.BEARISH and adopt is False
        assert fade.source_signal_id == trig.signal_id and "B1 GAP FADE" in fade.reason
        cty = e.gap_fade_plan(trig, {**PNB_LIKE, "gapDatr": 0.45}, StrategyKey.FUDKII_CT_Y)
        assert fade.stop == cty["stop"] == pytest.approx(189.25), "CT-Y's gap-fade stop"
        assert fade.targets == (pytest.approx(185.25),), "one target, 1 ATR30 from the close on the underlying"
        assert fade.rr == 1.0 and fade.evidence["at_030"] is True and fade.evidence["gap_official"] == 1.58
        rec = [x for x in events if x.get("kind") == "counter.b1"]
        assert len(rec) == 1 and rec[0]["take"] is True and rec[0]["side"] == "PE" and rec[0]["fade_signal_id"] == fade.signal_id
        assert rec[0]["at_025"] and rec[0]["at_030"] and rec[0]["keyPivotAhead"] is False and rec[0]["published"] is True
        assert rec[0]["conviction"]["note"] == NO_READ5 and rec[0]["gapDatr"] == 1.664 and rec[0]["gapOpenSrc"] == "preopen"
        await e._b1_fade(trig, None)
        assert len(handled) == 1, "one read per trigger"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_bearish_trigger_is_faded_with_the_call(settings):
    e, _trig, handled, events = await _judge(settings, PNB_LIKE, direction=Direction.BEARISH)
    try:
        fade, _ = handled[0]
        assert fade.direction is Direction.BULLISH and fade.targets == (pytest.approx(189.25),)
        assert next(x for x in events if x.get("kind") == "counter.b1")["side"] == "CE"
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("ctx", "gate"), [(CANBK_LIKE, "pivot_pin"), (UNIONBANK_LIKE, "no_gap")])
async def test_canbk_and_unionbank_like_are_skips_on_its_card_with_the_lines(settings, ctx, gate):
    e, _trig, handled, events = await _judge(settings, ctx)
    try:
        assert handled == []
        skip = [x for x in events if x.get("kind") == "rt_twin.skipped" and x.get("book") == B1.value]
        assert len(skip) == 1 and skip[0]["gate"] == gate and skip[0]["at030"] is (ctx["gapOfficial"] >= 0.3)
        rec = next(x for x in events if x.get("kind") == "counter.b1")
        assert rec["take"] is False and rec["gate"] == gate and "conviction" in rec
    finally:
        await e.stop()


def test_no_classic_level_for_the_name_is_no_fade():
    r = b1_read(PNB_LIKE, close=187.25, bullish=True, zones=[], pivots_known=False)
    assert r["take"] is False and r["gate"] == "pivots_unknown" and r["pivotsKnown"] is False and r["at_030"] is True
    assert b1_read(UNIONBANK_LIKE, close=187.25, bullish=True, zones=[], pivots_known=False)["gate"] == "no_gap", "no gap first"


@pytest.mark.asyncio
async def test_the_engine_does_not_fade_a_name_whose_pivots_it_could_not_read(settings):
    e, _trig, handled, events = await _judge(settings, PNB_LIKE, pivots=False)
    try:
        assert handled == []
        assert next(x for x in events if x.get("kind") == "rt_twin.skipped" and x.get("book") == B1.value)["gate"] == "pivots_unknown"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_graded_f_trigger_is_judged_too(settings):
    e, _trig, handled, events = await _judge(settings, PNB_LIKE, published=False)
    try:
        assert [s.strategy for s, _ in handled] == [B1], "any grade: no grade gate"
        assert next(x for x in events if x.get("kind") == "counter.b1")["published"] is False
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_later_candle_is_not_one_b1_reads(settings):
    e, _trig, handled, events = await _judge(settings, {**PNB_LIKE, "openBar": False}, ts=_open_ts() + 1800)
    try:
        assert handled == [] and not [x for x in events if x.get("kind") in ("counter.b1", "rt_twin.skipped")]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_an_index_trigger_is_judged(settings):
    e = Engine(settings)
    await e.start()
    try:
        idx = Instrument("999920005", "BANKNIFTY", Segment.NSE_EQ, InstrumentKind.EQUITY, name="BANKNIFTY", tick_size=0.05, underlying="BANKNIFTY")
        e.underlyings["BANKNIFTY"] = idx
        trig = Signal(strategy=StrategyKey.FUDKII, symbol="BANKNIFTY", direction=Direction.BEARISH, ts=_open_ts(), entry=55464.85,
                      stop=55600.0, targets=(55300.0,), reason="ST flip DOWN + close below lower band")
        e._breadth_at[trig.signal_id] = {**PNB_LIKE, "gapOfficial": 0.31, "atr30": 120.0}
        e.zones_for = lambda symbol: []  # type: ignore[method-assign]
        e._pivot_points = lambda symbol: FAR  # type: ignore[method-assign]
        handled: list = []

        async def capture(sig, bar, *, adopt=True):
            handled.append(sig)

        e._handle_signal = capture  # type: ignore[method-assign]
        await e._b1_fade(trig, None)
        assert [s.strategy for s in handled] == [B1] and handled[0].direction is Direction.BULLISH
        assert handled[0].targets == (pytest.approx(55584.85),)
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_b1_changes_no_other_books_decision(settings):
    """CT-B1 is its own job in ``_decide``: the counter route on the same trigger hands the books what it always did."""
    e, trig, handled, _ = await _judge(settings, {**PNB_LIKE, "gapDatr": 0.45, "relVol": 1.2, "share": 0.3})
    try:
        route: list = []

        async def capture(sig, bar, *, adopt=True):
            route.append(sig.strategy)

        async def no_legs(underlying, bar, **_kw):
            return []

        e._handle_signal = capture  # type: ignore[method-assign]
        e._counter_legs = no_legs  # type: ignore[method-assign]
        await e._handle_counter(trig, None)  # type: ignore[arg-type]
        assert sorted(k.value for k in route) == ["FUDKII_CT_M", "FUDKII_CT_Y"], "CT-Y's gap fade and CT-M, as before; no CT-B1"
        assert [s.strategy for s, _ in handled] == [B1]
    finally:
        await e.stop()


# -- the trade: its own book, wallet and exits ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fade_is_entered_in_its_own_book_with_the_put_and_no_option_ladder(settings):
    from kotsin_nse.exec.paper import BookSnapshot
    from kotsin_nse.instrument.select import Quote, Selection

    e = await _paper(settings)
    try:
        _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
        trig = _trigger(Direction.BULLISH)
        e._breadth_at[trig.signal_id] = PNB_LIKE

        async def select(underlying, s, *, tape=True):
            assert s.direction is Direction.BEARISH, "the put: the fade's side"
            return Selection(PE, premium=1.2, reason="ok", spread_pct=0.5)

        e._select_instrument = select  # type: ignore[method-assign]
        e._pivot_points = lambda symbol: FAR  # type: ignore[method-assign]
        now = time.time()
        e.books[PE.scrip_code] = BookSnapshot(PE.scrip_code, bids=[(1.19, 900_000)], asks=[(1.2, 900_000)], ts=now)
        e.quotes[PE.scrip_code] = Quote(ltp=1.2, bid=1.19, ask=1.2, ts=now)
        before = e.wallets[B1.value].available
        await e._b1_fade(trig, None)
        pos = [p for p in held(e) if p.strategy == B1.value]
        assert len(pos) == 1 and pos[0].instrument.option_type is OptionType.PE and pos[0].direction is Direction.BEARISH
        p = pos[0]
        assert p.equity_targets == (pytest.approx(185.25),) and p.equity_sl == pytest.approx(189.25)
        assert p.option_targets == () and p.option_t1 == 0.0, "no option rung: the underlying's T1 takes every lot"
        assert p.qty == 4 * PE.lot_size and e.wallets[B1.value].available < before, "4 lots, its own purse"
        mirrors = {m.value for m in stop_mirrors_of(B1.value).values()}
        assert {q.strategy for q in e.positions.values() if q.strategy in mirrors} == mirrors, "its two stop-rule mirrors"
        assert not [q for q in held(e) if q.strategy != B1.value], "no other book entered on it"
    finally:
        await e.stop()


def _b1_pos(opened: float) -> Position:
    return Position(id="b1", strategy=B1.value, instrument=PE, underlying=UND, side=PosSide.LONG, qty=11000, entry=1.2,
                    opened_ts=opened, signal_id="FUDKII_CT_B1-TATASTEEL-x-B", direction=Direction.BEARISH, equity_entry=187.25,
                    equity_sl=189.25, equity_targets=(185.25,), option_sl=0.9, qty_remaining=11000)


def _view(now: float, und: float, mid: float = 1.3, *, flat: bool = False) -> MarketView:
    return MarketView(option_ltp=mid, underlying_ltp=und, now=now, bars_held=1, past_force_flat=flat, option_mid=mid,
                      spread_pct=0.01, quote_ok=True, option_bid=mid - 0.01)


def _at(hm: str, day: date = date(2026, 10, 21)) -> float:
    h, m = (int(x) for x in hm.split(":"))
    return datetime.combine(day, dtime(h, m), tzinfo=IST).timestamp()


def test_every_lot_leaves_when_the_underlying_reaches_its_target():
    ex = ExitEngine(CT_B1_LIMITS)
    pos = _b1_pos(_at("09:46"))
    assert ex.evaluate(pos, _view(_at("10:00"), 186.0)) is None
    d = ex.evaluate(pos, _view(_at("10:05"), 185.2, mid=1.9))
    assert d is not None and d.reason is ExitReason.TARGET and d.qty == pos.qty_remaining, "all of it, not a lot"
    assert ex.resting_ladder(_b1_pos(_at("09:46"))) == [], "nothing rests: no option rung"


def test_the_11_15_time_stop_and_the_flatten():
    ex = ExitEngine(CT_B1_LIMITS)
    assert ex.evaluate(_b1_pos(_at("09:46")), _view(_at("11:14"), 186.5)) is None
    d = ex.evaluate(_b1_pos(_at("09:46")), _view(_at("11:15"), 186.5))
    assert d is not None and d.reason is ExitReason.TIME_STOP and d.qty == 11000 and "11:15" in d.note
    late = ex.evaluate(_b1_pos(_at("15:00", date(2026, 10, 20))), _view(_at("09:20"), 186.5))
    assert late is not None and late.reason is ExitReason.TIME_STOP, "anything opened on an earlier day: at once"
    flat = ex.evaluate(_b1_pos(_at("09:46")), _view(_at("10:30"), 186.5, flat=True))
    assert flat is not None and flat.reason is ExitReason.EOD
    assert ExitEngine(CT_Y_LIMITS).evaluate(_b1_pos(_at("09:46")), _view(_at("11:30"), 186.5, mid=1.22)) is None, "CT-Y has no clock"


def test_the_stop_is_ct_ys():
    ex = ExitEngine(CT_B1_LIMITS)
    d = ex.evaluate(_b1_pos(_at("09:46")), _view(_at("10:00"), 189.3, mid=1.0))
    assert d is not None and d.reason is ExitReason.SL_EQ, "the underlying through the plan's stop"
    lim = CT_B1_LIMITS
    assert (lim.sustain_s, lim.hard_floor_below_stop_pct, lim.fade_stop_nearer_level, lim.max_premium_loss_pct) == (
        CT_Y_LIMITS.sustain_s, CT_Y_LIMITS.hard_floor_below_stop_pct, True, None)
    assert (lim.max_lots, lim.fixed_lots_under_inr) == (CT_Y_LIMITS.max_lots, CT_Y_LIMITS.fixed_lots_under_inr)
    assert (lim.ladder_mode, lim.arm_at_pct, lim.peak_giveback_pct, lim.time_stop_hm) == ("none", None, None, "11:15")
    assert lim.gap_fade_datr is None and lim.gap_fade_max_rel_vol is None, "its rule is B1's, not CT-Y's gap fade"


# -- where books are listed ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_is_a_paper_counter_trend_shadow_with_its_own_wallet_codes_and_mirrors(settings):
    e = Engine(settings)
    await e.start()
    await e.stop()
    assert B1 in SHADOW_BOOKS and B1 in COUNTER_TREND and B1 in NO_PREMIUM_FLOOR
    assert describe_book(B1.value) == {"strategy_label": "CT-B1 · B1 gap fade (shadow)", "trend": "counter-trend"}
    assert ORDER_CODES[B1.value] == "FII-CTB" and ORDER_CODES["FUDKII_CT_B1_SE"] == "FIE-CTB"
    assert BOOK_LABELS[B1.value] == "CT-B1 B1 gap fade (shadow)" and e.wallets[B1.value].initial == settings.paper_initial_inr
    assert e.limits_for(B1.value) == CT_B1_LIMITS and e.book_trades(B1.value, Segment.NSE_EQ)
    assert not e.book_trades(B1.value, Segment.MCX_FO)
    book = next(b for b in BOOKS if b.key == B1.value)
    assert book.status == "live" and "0.25" in book.params["entries"] and "11:15" in book.params["exits"]


@pytest.mark.asyncio
async def test_its_tab_cards_the_09_15_triggers_alone_and_every_card_shows_it(settings):
    from kotsin_nse.engine import _position_json
    from kotsin_nse.market.session import ist_today

    e = Engine(settings)
    await e.start()
    try:
        e.underlyings["TATASTEEL"] = UND
        ts = _open_ts()
        trig = _trigger(Direction.BULLISH, ts)
        later = _trigger(Direction.BULLISH, ts + 3600)
        quiet = Signal(strategy=StrategyKey.FUDKII, symbol="PNB", direction=Direction.BEARISH, ts=ts, entry=121.0, stop=122.0)
        await e.ledger.insert_signal(trig.to_json(), "PAPER_FILLED", "")
        await e.ledger.insert_signal(later.to_json(), "PAPER_FILLED", "")
        await e.ledger.insert_signal(quiet.to_json(), "NOT_PUBLISHED", "FUDKII's own grade: grade F")
        fade = Signal(strategy=B1, symbol="TATASTEEL", direction=Direction.BEARISH, ts=ts, entry=187.25, stop=189.25, targets=(185.25,),
                      rr=1.0, grade="F", source_signal_id=trig.signal_id, reason=f"B1 GAP FADE of {trig.signal_id}")
        await e.ledger.insert_signal(fade.to_json(), "PAPER_FILLED", "")
        pos = _b1_pos(time.time())
        pos.signal_id = fade.signal_id
        await e.ledger.upsert_position(_position_json(pos))
        cards = (await e.book_cards(B1.value, ist_today()))["cards"]
        assert sorted(c["signalId"] for c in cards) == sorted([trig.signal_id, quiet.signal_id]), "the 09:15 candle, published or not"
        c = next(x for x in cards if x["signalId"] == trig.signal_id)
        assert c["state"] == "OPEN" and c["side"] == "PE" and c["describes"] == "fade" and c["routeLabel"] == "B1 GAP FADE"
        assert c["cta"]["enabled"] is False and "paper shadow" in c["cta"]["reason"], "no operator TAKE, no SKIP on its card"
        with pytest.raises(RuntimeError, match="paper shadow"):
            await e.operator_take(B1.value, trig.signal_id)
        assert next(x for x in cards if x["signalId"] == quiet.signal_id)["state"] == "NOT_JUDGED"
        parent = {x["signalId"]: x for x in (await e.book_cards("FUDKII", ist_today()))["cards"]}
        assert next(b for b in parent[trig.signal_id]["books"] if b["book"] == B1.value)["status"] == "OPEN"
        assert B1.value not in [b["book"] for b in parent[later.signal_id]["books"]], "only on a 09:15 trigger"
        assert c["exitPlan"] is not None and any("11:15" in r["at"] for r in c["exitPlan"]["rows"])
    finally:
        await e.stop()


def test_the_shadow_page_tab_totals_every_b1_by_both_lines_and_conviction():
    from kotsin_nse.api import shadow

    sig = [{"signal_id": "T1", "strategy": "FUDKII", "symbol": "PNB", "direction": "BULLISH", "ts": 1792381500},
           {"signal_id": "F1", "strategy": B1.value, "source_signal_id": "T1", "decision": "PAPER_FILLED"},
           {"signal_id": "T2", "strategy": "FUDKII", "symbol": "PREMIERENE", "direction": "BULLISH", "ts": 1792381500},
           {"signal_id": "F2", "strategy": B1.value, "source_signal_id": "T2", "decision": "PAPER_FILLED"},
           {"signal_id": "T3", "strategy": "FUDKII", "symbol": "CANBK", "direction": "BULLISH", "ts": 1792381500}]
    pos = [{"id": "p1", "strategy": B1.value, "signal_id": "F1", "status": "CLOSED", "entry": 2.1, "exit_reason": "TARGET",
            "instrument": {"name": "PNB 27 OCT 2026 PE 120.00"}},
           {"id": "p2", "strategy": B1.value, "signal_id": "F2", "status": "CLOSED", "entry": 9.0, "exit_reason": "TIME_STOP",
            "instrument": {"name": "PREMIERENE 27 OCT 2026 PE 900.00"}}]
    trades = [{"position_id": "p1", "net": 5200.0}, {"position_id": "p2", "net": -1300.0}]
    ev = [{"kind": "counter.b1", "signal_id": "T1", "symbol": "PNB", "take": True, "gapOfficial": 1.58, "at_030": True,
           "conviction": {"total": 4.5, "points": {"gap": 2.0}}, "side": "PE", "stop": 125.0, "targets": [120.0]},
          {"kind": "counter.b1", "signal_id": "T2", "symbol": "PREMIERENE", "take": True, "gapOfficial": 0.252, "at_030": False,
           "conviction": {"total": 1.0, "points": {"gap": 0.5}}, "side": "PE", "stop": 915.0, "targets": [890.0]},
          {"kind": "counter.b1", "signal_id": "T3", "symbol": "CANBK", "take": False, "gate": "pivot_pin", "gapOfficial": 0.62,
           "at_030": True, "pins": [["1mo.PIVOT", 0.05]], "why": "pinned", "conviction": {"total": 1.0, "points": {}}}]
    got = shadow.b1_summary(signals=sig, positions=pos, trades=trades, events=ev)
    t = got["total"]
    assert (t["read"], t["fades"], t["traded"]) == (3, 2, 2) and t["skipped"] == {"pivot_pin": 1}
    assert t["all"]["net"] == 3900.0 and t["all"]["win"] == 1 and t["at_030"]["net"] == 5200.0 and t["conv3"]["closed"] == 1
    assert [r["symbol"] for r in got["rows"]] == ["PNB", "PREMIERENE", "CANBK"] and got["rows"][2]["pin"] == "1mo.PIVOT +0.05"
    assert any(tab.id == "b1" for tab in shadow.TABS)
