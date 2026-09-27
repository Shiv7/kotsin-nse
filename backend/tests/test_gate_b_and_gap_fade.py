"""Gate B, the 09:45 gap fade, the verdict labels, the double-close lock and the repeated-close alarm
(operator, 2026-09-26: "RT-Y gate for Monday: B." / "yes build: 09:45 gap fade on CT-Y" / "The
double-close lock, before any real money." / "how do we ensure that [TORNTPHARM] does not happen
ever").

Replay evidence (Sep 1–25, RT-Y rule on real 1-min option candles, before costs): a key pivot within
0.5 ATR ahead −1.5 % / −1.6 % a trade in the two halves against +0.1 % / +1.0 %; a 09:45 trigger that
gapped its own way −2.0 % / −5.7 % against −0.7 % / −1.3 %; fading that gap (stop 1 ATR past the
close) +3.72 % / +5.87 % (40 trades)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.bars.pivots import Zone
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    ExitDecision,
    ExitReason,
    Instrument,
    InstrumentKind,
    OptionType,
    Position,
    PosSide,
)
from kotsin_nse.engine import FADE_BOOKS, IN_TREND_BOOKS, Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote, Selection
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.risk.limits import CT_X_LIMITS, CT_Y_LIMITS, RT_N_LIMITS, RT_X_LIMITS, RT_Y_LIMITS
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey
from kotsin_nse.strategy.regime_gates import rt_gate_reasons, trigger_verdicts

UND = Instrument("3499", "TATASTEEL", Segment.NSE_EQ, InstrumentKind.EQUITY, name="TATASTEEL", tick_size=0.01, underlying="TATASTEEL")
OPT = Instrument("153805", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=2750, tick_size=0.01,
                 strike=190.0, option_type=OptionType.CE, underlying="TATASTEEL")


def _open_ts() -> int:
    return int(datetime.combine(ist_today(), dtime(9, 15), tzinfo=IST).timestamp())


def _trigger(direction: Direction = Direction.BULLISH, ts: int | None = None) -> Signal:
    return Signal(strategy=StrategyKey.FUDKII, symbol="TATASTEEL", direction=direction, ts=ts or _open_ts(),
                  entry=187.25, stop=186.0 if direction is Direction.BULLISH else 188.5, targets=(190.0,), reason="ST flip UP + close above upper band")


def _parent(sid: str) -> Position:
    return Position(id="parent", strategy="FUDKII", instrument=OPT, underlying=UND, side=PosSide.LONG, qty=11000,
                    entry=1.5, opened_ts=time.time(), signal_id=sid, direction=Direction.BULLISH,
                    equity_entry=187.25, equity_sl=186.0, option_sl=1.2)


def _books(e: Engine) -> set[str]:
    return {p.strategy for p in e.positions.values() if p.strategy.startswith("FUDKII_RT")}


async def _paper(settings) -> Engine:
    e = Engine(settings.model_copy(update={"paper_limit_orders": False}))
    await e.start()
    await e.set_mode(Mode.PAPER)
    e.underlyings["TATASTEEL"] = UND
    return e


async def _drive(e: Engine, sig: Signal, inst: Instrument, books, premium: float = 1.5) -> None:
    """``sig`` into ``books``, its contract quoted at ``premium`` with depth."""

    async def select(underlying, s, *, tape=True):
        return Selection(inst, premium=premium, reason="ok", spread_pct=0.5)

    e._select_instrument = select  # type: ignore[method-assign]
    now = time.time()
    e.books[inst.scrip_code] = BookSnapshot(inst.scrip_code, bids=[(premium - 0.01, 900_000)], asks=[(premium, 900_000)], ts=now)
    e.quotes[inst.scrip_code] = Quote(ltp=premium, bid=premium - 0.01, ask=premium, ts=now)
    await e._handle_signal(sig, None, books=books)


# -- gate B ------------------------------------------------------------------------------------------


def test_only_rt_y_carries_gate_b_and_only_ct_y_fades_the_gap():
    assert (RT_Y_LIMITS.skip_pivot_ahead_atr, RT_Y_LIMITS.skip_open_gap_datr, RT_Y_LIMITS.breadth_min) == (0.5, 0.3, 0.5)
    for lim in (RT_X_LIMITS, RT_N_LIMITS, CT_X_LIMITS, CT_Y_LIMITS):
        assert lim.skip_pivot_ahead_atr is None and lim.skip_open_gap_datr is None
    assert CT_Y_LIMITS.gap_fade_datr == 0.3 and CT_Y_LIMITS.gap_fade_min_rr is None
    assert RT_Y_LIMITS.gap_fade_datr is None and CT_X_LIMITS.gap_fade_datr is None


def test_the_gate_reads_its_own_reach_and_older_contexts_still_work():
    ctx = {"share": 0.62, "names": 220, "pivotsAheadAtr": [["1d.R2", 0.27], ["1wk.R1", 0.8]], "openBar": False, "gapDatr": 0.1}
    assert [g for g, _ in rt_gate_reasons(ctx, RT_Y_LIMITS)] == ["pivot_ahead"]
    assert "1d.R2 +0.27 ATR" in rt_gate_reasons(ctx, RT_Y_LIMITS)[0][1] and "1wk.R1" not in rt_gate_reasons(ctx, RT_Y_LIMITS)[0][1]
    old = {"share": 0.3, "names": 220, "pivotsAhead": ["1d.S1 +0.2 ATR"], "openBar": True, "gapDatr": 0.49}
    assert [g for g, _ in rt_gate_reasons(old, RT_Y_LIMITS)] == ["breadth", "pivot_ahead", "open_gap"]
    assert rt_gate_reasons({}, RT_Y_LIMITS) == [] and rt_gate_reasons(None, RT_Y_LIMITS) == [], "nothing measured never blocks"
    assert rt_gate_reasons(old, RT_X_LIMITS) == [], "RT-X is the ungated control"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ctx", "gate"),
    [
        ({"share": 0.7, "names": 220, "pivotsAheadAtr": [["1d.R2", 0.27]], "pivotsAhead": ["1d.R2 +0.27 ATR"], "openBar": False, "gapDatr": 0.0}, "pivot_ahead"),
        ({"share": 0.7, "names": 220, "pivotsAheadAtr": [], "pivotsAhead": [], "openBar": True, "gapDatr": 0.41}, "open_gap"),
        ({"share": 0.7, "names": 220, "pivotsAheadAtr": [["1d.R2", 0.8]], "pivotsAhead": [], "openBar": True, "gapDatr": 0.29}, None),
    ],
)
async def test_rt_y_stands_aside_on_gate_b_while_rt_x_and_rt_n_take_it(settings, ctx, gate):
    e = await _paper(settings)
    try:
        sig = _trigger()
        e._breadth_at[sig.signal_id] = ctx
        await _drive(e, sig, OPT, IN_TREND_BOOKS)
        assert {"FUDKII_RT_X", "FUDKII_RT_N"} <= _books(e)
        assert ("FUDKII_RT_Y" in _books(e)) is (gate is None)
        if gate:
            ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 5) if x.get("kind") == "rt_twin.skipped"]
            assert ev[-1]["book"] == "FUDKII_RT_Y" and ev[-1]["gate"] == gate
    finally:
        await e.stop()


# -- the 09:45 gap fade ---------------------------------------------------------------------------------


def _seed(e: Engine, *, zones: list[Zone]) -> None:
    e.underlyings["TATASTEEL"] = UND
    t0 = _open_ts()
    e.store.seed("TATASTEEL", "30m", [UnifiedBar("TATASTEEL", "3499", "30m", t0 - 86_400 + k * 1800, 186.0, 187.0, 185.0, 186.0, 1e5,
                                                 source=BarSource.REST, complete=True) for k in range(20)])  # ATR30 = 2.0
    e.zones_for = lambda symbol: zones  # type: ignore[method-assign]


def test_the_gap_fade_plan_is_the_replays_rule(settings):
    e = Engine(settings)
    _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT", "1wk.BC"]), Zone(182.1, 7.0, ["1d.S1", "1mo.TC"]), Zone(186.5, 2.0, ["1d.BC"])])
    ctx = {"openBar": True, "gapDatr": 0.45, "atr30": 2.0}
    plan = e.gap_fade_plan(_trigger(Direction.BULLISH), ctx)
    assert plan["direction"] == "BEARISH" and plan["side"] == "PE"
    assert plan["stop"] == pytest.approx(189.25), "1 ATR30 past the close, against the fade"
    assert plan["targets"][:2] == [184.0, 182.1], "the walls on the fade's side, nearest first; the weak 186.5 is not a target"
    assert plan["rr"] == pytest.approx(1.62) and plan["grade"] == "C", "(187.25 − 184) / 2 = 1.62: over rr_c 1.2, under rr_b 1.8"
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "gapDatr": 0.29}) is None
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "openBar": False}) is None
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "gapDatr": -0.6}) is None, "a gap AGAINST the trigger is not this rule"
    bare = Engine(settings)
    _seed(bare, zones=[])
    p = bare.gap_fade_plan(_trigger(Direction.BEARISH), ctx)
    assert p["side"] == "CE" and p["stop"] == pytest.approx(185.25) and p["targets"] == [pytest.approx(189.25)]
    assert p["rr"] == 1.0 and p["grade"] == "F" and "1 ATR30" in p["targetNote"], "no wall: one target 1 ATR away, graded as a label"


@pytest.mark.asyncio
async def test_ct_y_alone_enters_the_gap_fade_and_the_counter_route_still_runs(settings):
    e = Engine(settings)
    await e.start()
    try:
        _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
        trig = _trigger(Direction.BULLISH)
        e._breadth_at[trig.signal_id] = {"share": 0.7, "names": 220, "openBar": True, "gapDatr": 0.45, "atr30": 2.0}
        handled: list[tuple[Signal, bool]] = []

        async def capture(sig, bar, *, adopt=True):
            handled.append((sig, adopt))

        async def no_legs(underlying, bar):
            return []

        e._handle_signal = capture  # type: ignore[method-assign]
        e._counter_legs = no_legs  # type: ignore[method-assign]
        await e._handle_counter(trig, None)  # type: ignore[arg-type]
        assert len(handled) == 1
        fade, adopt = handled[0]
        assert fade.strategy is StrategyKey.FUDKII_CT_Y and fade.direction is Direction.BEARISH and adopt is False
        assert fade.source_signal_id == trig.signal_id and fade.stop == pytest.approx(189.25) and "GAP FADE" in fade.reason
        kinds = [x["kind"] for x in await e.ledger.rows_between("events", 0, time.time() + 5)]
        assert "counter.gap_fade" in kinds and "counter.route" in kinds, "the gap fade is additive: the route still runs"
        assert trig.signal_id in e._gap_faded
        await e._handle_counter(trig, None)  # type: ignore[arg-type]
        assert len(handled) == 1, "one gap fade per trigger"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_floor_on_the_fades_rr_blocks_only_when_set(settings, monkeypatch):
    from dataclasses import replace

    e = Engine(settings)
    await e.start()
    try:
        _seed(e, zones=[])  # rr 1.0
        book = e._exits_by_strategy[StrategyKey.FUDKII_CT_Y.value]
        monkeypatch.setattr(book, "limits", replace(book.limits, gap_fade_min_rr=1.5))
        trig = _trigger(Direction.BULLISH)
        e._breadth_at[trig.signal_id] = {"openBar": True, "gapDatr": 0.45, "atr30": 2.0}
        handled = []

        async def capture(sig, bar, *, adopt=True):
            handled.append(sig)

        e._handle_signal = capture  # type: ignore[method-assign]
        await e._gap_fade(trig, None, UND)
        assert handled == []
        ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 5) if x.get("kind") == "counter.gap_fade"]
        assert ev and ev[-1]["blocked"].startswith("RR 1.00 < 1.5")
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_ct_y_does_not_also_take_ct_xs_fade_of_a_trigger_it_gap_faded(settings):
    e = await _paper(settings)
    try:
        trig = _trigger(Direction.BULLISH)
        e._gap_faded.add(trig.signal_id)
        ct_x_fade = Signal(strategy=StrategyKey.FUDKII_CT_X, symbol="TATASTEEL", direction=Direction.BEARISH, ts=trig.ts, entry=187.25,
                           stop=188.0, targets=(184.0,), source_signal_id=trig.signal_id)
        pe = Instrument("153806", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=2750, strike=185.0, option_type=OptionType.PE, underlying="TATASTEEL")
        await _drive(e, ct_x_fade, pe, FADE_BOOKS, premium=1.2)
        assert [p.strategy for p in e.positions.values()] == ["FUDKII_CT_X"], "CT-X takes its fade; CT-Y already holds the gap fade's view"
        ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 5) if x.get("kind") == "rt_twin.skipped"]
        assert ev[-1]["gate"] == "gap_fade"
    finally:
        await e.stop()


# -- the labels on every card -----------------------------------------------------------------------------


def test_the_verdicts_say_what_happened_and_what_would():
    sgn = {"signal_id": "S", "direction": "BULLISH"}
    ctx = {"kind": "regime.breadth", "share": 0.66, "names": 220, "pivotsAheadAtr": [["1wk.R1", 0.05]], "pivotsAhead": ["1wk.R1 +0.05 ATR"],
           "openBar": True, "gapDatr": 0.14}
    v = trigger_verdicts(sgn, [ctx], fade_x=None, gap_fade=None, rt_y_held=False, lim_y=RT_Y_LIMITS)
    assert v["rtY"]["action"] == "SKIP" and v["rtY"]["state"] == "would skip" and v["rtY"]["gate"] == "pivot_ahead"
    assert v["ctY"]["action"] == "NONE" and v["ctY"]["why"] == "no counter-trend route recorded"
    gap = {"kind": "counter.gap_fade", "side": "PE", "stop": 189.25, "targets": [184.0], "rr": 1.62, "grade": "C", "gapDatr": 0.45}
    skip = {"kind": "rt_twin.skipped", "book": "FUDKII_RT_Y", "gate": "open_gap", "reason": "09:45 trigger gapped 0.45 daily ATR its own way"}
    v = trigger_verdicts(sgn, [ctx, gap, skip], fade_x=None, gap_fade=None, rt_y_held=False, lim_y=RT_Y_LIMITS)
    assert v["rtY"] == {"action": "SKIP", "state": "gate B", "gate": "open_gap", "why": ["09:45 trigger gapped 0.45 daily ATR its own way"]}
    # what RT-Y's card says is what happened: a miss is not the gate at work (audit, 2026-09-26)
    for gate, action, state in (("missed", "MISSED", "missed"), ("stop_breached", "MISSED", "missed"),
                                ("dried_volume", "SKIP", "dried volume"), ("wallet_halted", "NOT TAKEN", "wallet halted"),
                                ("not_sized", "NOT TAKEN", "not sized")):
        ev = {"kind": "rt_twin.skipped", "book": "FUDKII_RT_Y", "gate": gate, "reason": "r"}
        got = trigger_verdicts(sgn, [ctx, ev], fade_x=None, gap_fade=None, rt_y_held=False, lim_y=RT_Y_LIMITS)["rtY"]
        assert (got["action"], got["state"]) == (action, state), gate
    assert v["ctY"]["action"] == "GAP FADE" and v["ctY"]["stop"] == 189.25 and v["ctY"]["rr"] == 1.62
    clean = {**ctx, "pivotsAheadAtr": [], "pivotsAhead": [], "openBar": False}
    v = trigger_verdicts(sgn, [clean], fade_x=None, gap_fade=None, rt_y_held=True, lim_y=RT_Y_LIMITS)
    assert v["rtY"]["action"] == "TAKE" and v["rtY"]["state"] == "taken" and "breadth 66% agree" in v["rtY"]["why"]


@pytest.mark.asyncio
async def test_ct_ys_card_describes_its_gap_fade_and_every_card_carries_the_verdicts(settings):
    e = Engine(settings)
    await e.start()
    try:
        ts = _open_ts()
        trig = _trigger(Direction.BULLISH, ts)
        await e.ledger.insert_signal(trig.to_json(), "PAPER_FILLED", "")
        fade = Signal(strategy=StrategyKey.FUDKII_CT_Y, symbol="TATASTEEL", direction=Direction.BEARISH, ts=ts, entry=187.25, stop=189.25,
                      targets=(184.0,), rr=1.62, grade="C", source_signal_id=trig.signal_id, reason=f"GAP FADE of {trig.signal_id}")
        await e.ledger.insert_signal(fade.to_json(), "PAPER_FILLED", "")
        await e.ledger.event("regime.breadth", {"signal_id": trig.signal_id, "symbol": "TATASTEEL", "share": 0.7, "names": 220,
                                                "openBar": True, "gapDatr": 0.45, "pivotsAhead": [], "pivotsAheadAtr": []})
        await e.ledger.event("counter.gap_fade", {"signal_id": trig.signal_id, "symbol": "TATASTEEL", "side": "PE", "stop": 189.25,
                                                  "targets": [184.0], "rr": 1.62, "grade": "C", "gapDatr": 0.45, "fade_signal_id": fade.signal_id})
        await e.ledger.event("rt_twin.skipped", {"book": "FUDKII_RT_Y", "signal_id": trig.signal_id, "symbol": "TATASTEEL",
                                                 "reason": "09:45 trigger gapped 0.45 daily ATR its own way", "gate": "open_gap"})
        pe = Instrument("153806", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=2750, strike=185.0, option_type=OptionType.PE, underlying="TATASTEEL")
        pos = Position(id="cy", strategy="FUDKII_CT_Y", instrument=pe, underlying=UND, side=PosSide.LONG, qty=2750, entry=1.2,
                       opened_ts=time.time(), signal_id=fade.signal_id, direction=Direction.BEARISH, equity_entry=187.25, equity_sl=189.25, option_sl=0.9)
        from kotsin_nse.engine import _position_json

        await e.ledger.upsert_position(_position_json(pos))
        cy = next(c for c in (await e.book_cards("FUDKII_CT_Y", ist_today()))["cards"] if c["symbol"] == "TATASTEEL")
        assert cy["describes"] == "fade" and cy["direction"] == "BEARISH" and cy["stop"] == 189.25 and cy["side"] == "PE"
        assert cy["state"] == "OPEN" and cy["routeLabel"] == "GAP FADE"
        assert next(b for b in cy["books"] if b["book"] == "FUDKII_CT_Y")["status"] == "OPEN"
        cx = next(c for c in (await e.book_cards("FUDKII_CT_X", ist_today()))["cards"] if c["symbol"] == "TATASTEEL")
        assert cx["describes"] == "trigger" and cx["state"] == "NO_ROUTE", "CT-X is untouched by CT-Y's gap fade"
        for book in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y"):
            c = next(x for x in (await e.book_cards(book, ist_today()))["cards"] if x["symbol"] == "TATASTEEL")
            assert c["verdicts"]["rtY"]["action"] == "SKIP" and c["verdicts"]["rtY"]["gate"] == "open_gap", book
            assert c["verdicts"]["ctY"]["action"] == "GAP FADE" and c["verdicts"]["ctY"]["side"] == "PE", book
    finally:
        await e.stop()


# -- the double-close lock ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_exits_racing_for_one_position_send_one_order(settings):
    e = Engine(settings)
    pos = Position(id="p", strategy="FUDKII_RT_X", instrument=OPT, underlying=UND, side=PosSide.LONG, qty=2750, entry=1.5,
                   opened_ts=time.time(), signal_id="s", direction=Direction.BULLISH, equity_entry=187.25, equity_sl=186.0, option_sl=1.2)
    sent: list[int] = []

    async def slow_exit(p, decision, now):
        sent.append(decision.qty)
        await asyncio.sleep(0.05)  # the order is at the venue
        p.qty_remaining -= decision.qty
        if p.qty_remaining <= 0:
            p.status = "CLOSED"

    e._exit_now = slow_exit  # type: ignore[method-assign]
    manual = ExitDecision(pos.id, ExitReason.MANUAL, 1.4, 2750, "operator skip")
    loop = ExitDecision(pos.id, ExitReason.SL_EQ, 1.4, 2750, "SL-EQ")
    await asyncio.gather(e._exit(pos, manual, time.time()), e._exit(pos, loop, time.time()))
    assert sent == [2750], "the second caller stood down while the first was at the venue"
    await e._exit(pos, loop, time.time())
    assert sent == [2750], "and a position already closed is never sold again"
    assert not e._exits_in_flight


@pytest.mark.asyncio
async def test_an_exit_decided_before_a_slice_filled_is_trimmed_to_what_is_left(settings):
    e = Engine(settings)
    pos = Position(id="p", strategy="FUDKII_RT_X", instrument=OPT, underlying=UND, side=PosSide.LONG, qty=11000, entry=1.5,
                   opened_ts=time.time(), signal_id="s", direction=Direction.BULLISH)
    pos.qty_remaining = 8250
    sent = []

    async def rec(p, decision, now):
        sent.append(decision.qty)

    e._exit_now = rec  # type: ignore[method-assign]
    await e._exit(pos, ExitDecision(pos.id, ExitReason.MANUAL, 1.4, 11000, "stale"), time.time())
    assert sent == [8250]


# -- never again a repeated close --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repeated_close_shows_on_health(settings):
    e = Engine(settings)

    async def decide(bar):
        e._decided.add((bar.symbol, bar.ts))

    e._reconcile_then_decide = decide  # type: ignore[method-assign]
    check = lambda: next(c for c in e.health_snapshot()["checks"] if c["name"] == "bars_close_once")  # noqa: E731
    assert check()["ok"] is True and check()["value"] == 0
    bar = UnifiedBar("TATASTEEL", "3499", "30m", _open_ts(), 186, 187, 185, 186.5, 1e5, source=BarSource.LIVE, complete=True)
    for _ in range(3):
        await e._on_bar_close(bar)
    await asyncio.sleep(0)
    c = check()
    assert c["ok"] is False and c["value"] == 2 and "repeated 30m closes" in c["detail"]
    assert e.health_snapshot()["bars"]["duplicate_closes"] == 2 and "late_ticks" in e.health_snapshot()["bars"]


def test_a_pivot_just_past_the_reach_does_not_gate():
    """GAIL / ADANIENT (19-25 Sep parity check): a level 0.503 ATR ahead, once rounded to 0.50,
    was read as "within 0.5". The gate compares the unrounded distance."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS
    from kotsin_nse.strategy.regime_gates import rt_gate_reasons

    assert rt_gate_reasons({"share": 0.7, "pivotsAheadAtr": [["1d.R1", 0.5034]]}, RT_Y_LIMITS) == []
    assert [g for g, _ in rt_gate_reasons({"share": 0.7, "pivotsAheadAtr": [["1d.R1", 0.4996]]}, RT_Y_LIMITS)] == ["pivot_ahead"]
