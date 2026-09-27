"""The FUDKII-RT exit policy: sustain, hard floor, peak ratchet — and a written precedence."""

from __future__ import annotations

import pytest

from kotsin_nse.risk.exits import ExitEngine, MarketView
from kotsin_nse.risk.limits import RT_X_LIMITS, RiskLimits


def _pos(**kw):
    from kotsin_nse.config import Segment
    from kotsin_nse.domain import (
        Direction,
        Instrument,
        InstrumentKind,
        OptionType,
        Position,
        PosSide,
    )

    inst = Instrument(
        scrip_code="1", symbol="X", segment=Segment.NSE_FO,
        kind=InstrumentKind.OPTION, name="X CE", lot_size=100, tick_size=0.05, multiplier=1,
        # 1025 against a 1000 underlying: δ ≈ 0.30, so the live re-projection of the equity stop
        # (1000 → 990) lands on exactly the 17.00 these fixtures were written with.
        expiry="2026-09-29", strike=1025.0, option_type=OptionType.CE, underlying="X",
    )
    base = dict(
        id="p1", strategy="FUDKII_RT_X", instrument=inst, underlying=inst, side=PosSide.LONG,
        qty=100, entry=20.0, opened_ts=1000.0, signal_id="s1", direction=Direction.BULLISH,
        equity_entry=1000.0, equity_sl=990.0, equity_targets=(1020.0,),
        option_sl=17.0, option_targets=(24.0,), grade="A",
    )
    base.update(kw)
    return Position(**base)


def _view(**kw):
    base = dict(option_ltp=20.0, underlying_ltp=1000.0, now=2000.0, bars_held=0,
                past_force_flat=False, option_mid=20.0, spread_pct=0.005, quote_ok=True)
    base.update(kw)
    return MarketView(**base)


def test_the_precedence_order_is_written_down_and_asserted():
    """Precedence is a contract, not an accident of line numbers."""
    assert ExitEngine.RULE_ORDER == (
        "hard_floor_below_stop",
        "equity_confirmed_stop",
        "option_stop",
        "legacy_hard_floor",
        "targets",
        "peak_ratchet",
        "halt",
        "daily_loss",
        "force_flat",
        "time_stop",
    )


def test_an_option_breach_alone_does_not_exit_until_it_has_been_held():
    """A wick through a thin book is not a stop. The clock must run before it counts."""
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos()
    # first read below the stop: clock starts, no exit
    assert e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5, now=2000.0)) is None
    assert pos.breach_since == 2000.0
    # still inside the 75s window
    assert e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5, now=2060.0)) is None
    # past it
    d = e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5, now=2080.0))
    assert d is not None and "held below" in d.note


def test_recovery_clears_the_clock_so_two_touches_are_not_one_sustain():
    """The bug this exists to prevent: a touch now and another in five minutes is not a sustain."""
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos()
    e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5, now=2000.0))
    assert pos.breach_since == 2000.0
    e.evaluate(pos, _view(option_ltp=18.0, option_mid=18.0, now=2010.0))   # recovered
    assert pos.breach_since is None
    e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5, now=2300.0))   # breached again
    assert pos.breach_since == 2300.0, "the clock restarts, it does not resume"


def test_a_missing_quote_pauses_the_clock_rather_than_clearing_it():
    """Absent is a third state. Treating it as recovery would hand back a spent grace period —
    which is exactly what a 90-second feed gap would do."""
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos()
    e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5, now=2000.0))
    e.evaluate(pos, _view(option_ltp=0.0, option_mid=None, quote_ok=False, now=2040.0))
    assert pos.breach_since == 2000.0, "unknown must neither clear nor advance the breach"


def test_the_equity_confirming_the_breach_exits_at_once_with_no_grace():
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos()
    d = e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5, underlying_ltp=989.0, now=2000.0))
    assert d is not None and "no grace" in d.note


def test_the_hard_floor_beats_the_sustain_window():
    """A collapse is not a wick, and the floor is path-independent so a feed gap cannot hide it."""
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos()
    # 9% through a 17.00 stop is 15.47
    d = e.evaluate(pos, _view(option_ltp=15.0, option_mid=15.0, now=2000.0))
    assert d is not None and "hard floor" in d.note
    assert pos.breach_since is None, "the floor fires before the clock is even consulted"


def test_the_rising_stop_lifts_to_peak_minus_3pct_once_armed_and_is_hard():
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos(targets_hit=1, peak_mid=30.0, armed_by="option", armed_ts=1900.0, sustained_idx=0, ratchet_sl=24.0)
    # give-back is max(3%, 1.5 x spread) = 3% of 30.00 = 0.90 -> the line is 29.10; 29.0 is through it
    d = e.evaluate(pos, _view(option_ltp=29.0, option_mid=29.0, now=2000.0))
    assert d is not None and "hard SL 29.10" in d.note and d.qty == pos.qty_remaining


def test_a_single_print_cannot_trip_the_ratchet():
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos(targets_hit=1, peak_mid=30.0)
    assert e.evaluate(pos, _view(option_ltp=29.0, option_mid=29.0)) is None
    e.evaluate(pos, _view(option_ltp=29.8, option_mid=29.8))   # one good read resets the dwell
    assert pos.trail_dwell == 0


def test_a_wide_spread_widens_the_giveback_so_two_ticks_cannot_trip_it():
    """2% of a 20.00 premium is 0.40; on a contract quoting 0.60 wide that is one tick."""
    e = ExitEngine(RT_X_LIMITS)
    wide = _pos(targets_hit=1, peak_mid=30.0)
    # spread 3% -> floor becomes 4.5% -> level 28.65, so 29.00 is NOT a give-back
    for _ in range(4):
        assert e.evaluate(wide, _view(option_ltp=29.0, option_mid=29.0, spread_pct=0.03)) is None


def test_the_base_book_is_untouched_by_the_rt_policy():
    """FUDKII and FUKAA keep their own exits: no sustain, no ratchet, and their time stop intact."""
    base = RiskLimits()
    assert base.sustain_s is None
    assert base.peak_giveback_pct is None
    assert base.time_stop_bars == 8
    assert RT_X_LIMITS.time_stop_bars is None

    e = ExitEngine(base)
    pos = _pos(strategy="FUDKII")
    d = e.evaluate(pos, _view(option_ltp=16.5, option_mid=16.5))
    assert d is not None, "the base book exits on the touch, with no grace period"


def test_a_feed_gap_is_replayed_from_the_candles_rather_than_guessed():
    """On reconnect the engine knows where the premium is, not where it went. The broker serves 1m
    candles for a listed option, so the gap is walked."""
    from kotsin_nse.risk.exits import replay_gap

    pos = _pos()
    # three minutes below the 17.00 stop while the socket was down
    gap = [(3000.0, 16.8), (3060.0, 16.5), (3120.0, 16.4)]
    d = replay_gap(pos, gap, RT_X_LIMITS)
    assert d is not None and "replayed the feed gap" in d.note

    # a gap that recovered mid-way must not accumulate across the recovery
    pos2 = _pos()
    assert replay_gap(pos2, [(3000.0, 16.8), (3060.0, 18.0), (3120.0, 16.9)], RT_X_LIMITS) is None
    assert pos2.breach_since == 3120.0, "the clock restarted at the last breach, not the first"


def test_replay_is_inert_for_a_book_without_a_sustain_policy():
    from kotsin_nse.risk.exits import replay_gap

    pos = _pos(strategy="FUDKII")
    assert replay_gap(pos, [(3000.0, 1.0)], RiskLimits()) is None


def test_the_rt_twin_mirrors_the_entry_exactly_so_only_the_exit_differs():
    """Same contract, same qty, same fill price, same instant. If the entries differed by a tick
    the two curves would be comparing entries as well as exits."""
    from dataclasses import replace

    from kotsin_nse.strategy.keys import StrategyKey

    primary = _pos(strategy=StrategyKey.FUDKII.value, id="pos-a")
    twin = replace(
        primary,
        id="pos-b",
        strategy=StrategyKey.FUDKII_RT_X.value,
        note=f"{primary.note} · RT exit policy, twin of {primary.id}",
    )
    for field in ("instrument", "qty", "entry", "opened_ts", "option_sl", "option_targets",
                  "equity_sl", "equity_targets", "direction", "signal_id"):
        assert getattr(twin, field) == getattr(primary, field), field
    assert twin.id != primary.id
    assert twin.strategy == "FUDKII_RT_X"
    assert "twin of pos-a" in twin.note


def test_the_two_books_take_different_exits_from_identical_state():
    """The point of the pair: same position, same market, two answers."""
    base = ExitEngine(RiskLimits())
    rt = ExitEngine(RT_X_LIMITS)
    a, b = _pos(strategy="FUDKII"), _pos(strategy="FUDKII_RT_X")
    v = _view(option_ltp=16.5, option_mid=16.5, now=2000.0)

    assert base.evaluate(a, v) is not None, "the base book exits on the touch"
    assert rt.evaluate(b, v) is None, "the RT book waits for the breach to hold"


# -- the RT-X ladder (operator's design, 2026-09-23) ------------------------------------------


def _own(**kw):
    """An RT-X twin on a 4-lot position whose option carries its own ladder above entry."""
    from kotsin_nse.config import Segment
    from kotsin_nse.domain import Instrument, InstrumentKind, OptionType

    inst = Instrument(
        scrip_code="1", symbol="X", segment=Segment.NSE_FO, kind=InstrumentKind.OPTION, name="X CE",
        lot_size=100, tick_size=0.05, multiplier=1, expiry="2026-09-29", strike=1010.0,
        option_type=OptionType.CE, underlying="X",
    )
    base = dict(instrument=inst, qty=400, option_t1=24.0, option_targets=(24.0, 28.0, 32.0, 36.0))
    base.update(kw)
    return _pos(**base)


def _sustain_t1(e, pos, *, t0=2000.0, level=24.5):
    """Drive T1 through touch → 75 s above → a minute close → sustained. Returns the touch decision."""
    from kotsin_nse.risk.exits import apply_exit

    d = e.evaluate(pos, _view(option_ltp=level, option_mid=level, now=t0))
    assert d is not None and d.reason.value == "TARGET"
    apply_exit(pos, d, fill_price=level, charges=0, now=t0)
    for t in (t0 + 30, t0 + 65, t0 + 74):  # +65 crosses a minute boundary → the close is seen
        assert e.evaluate(pos, _view(option_ltp=level, option_mid=level, now=t)) is None
    assert pos.armed_ts is None, "74 s is not 75"
    assert e.evaluate(pos, _view(option_ltp=level, option_mid=level, now=t0 + 76)) is None
    return d


def test_t1_touch_takes_one_lot_and_floors_the_hard_sl_at_breakeven():
    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    assert e.evaluate(pos, _view(option_ltp=23.9, option_mid=23.9)) is None
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0))
    assert d is not None and d.qty == 100 and "T1 24.00 touched" in d.note
    assert pos.ratchet_sl == 20.0 and pos.armed_by == "option" and pos.armed_ts is None


def test_t1_sustained_75s_with_a_minute_close_arms_and_steps_the_sl_to_t1():
    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    _sustain_t1(e, pos)
    assert pos.sustained_idx == 0 and pos.armed_ts == 2076.0 and pos.ratchet_sl == 24.0


def test_a_dip_below_t1_resets_the_sustain_clock_and_the_close_flag():
    from kotsin_nse.risk.exits import apply_exit

    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    d = e.evaluate(pos, _view(option_ltp=24.5, option_mid=24.5, now=2000.0))
    apply_exit(pos, d, fill_price=24.5, charges=0, now=2000.0)
    assert e.evaluate(pos, _view(option_ltp=23.5, option_mid=23.5, now=2030.0)) is None
    assert pos.t_touch_ts is None and not pos.t_close_ok, "below the rung: the clock stops"
    assert e.evaluate(pos, _view(option_ltp=24.5, option_mid=24.5, now=2065.0)) is None
    assert pos.t_touch_ts == 2065.0
    assert e.evaluate(pos, _view(option_ltp=24.5, option_mid=24.5, now=2130.0)) is None
    assert pos.armed_ts is None, "65 s since it came back above"
    assert e.evaluate(pos, _view(option_ltp=24.5, option_mid=24.5, now=2141.0)) is None
    assert pos.armed_ts == 2141.0 and pos.ratchet_sl == 24.0


def test_the_equity_trigger_makes_the_options_price_at_that_instant_t1():
    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    d = e.evaluate(pos, _view(option_ltp=22.0, option_mid=22.0, underlying_ltp=1020.0))
    assert d is not None and d.qty == 100 and "equity" in d.note
    assert pos.option_t1 == 22.0 and pos.option_targets == (22.0, 24.0, 28.0, 32.0)
    assert pos.armed_by == "equity" and pos.ratchet_sl == 20.0


def test_t2_touch_takes_a_lot_keeps_the_sl_at_t1_and_t2_sustain_steps_it():
    from kotsin_nse.risk.exits import apply_exit

    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    _sustain_t1(e, pos)
    d = e.evaluate(pos, _view(option_ltp=28.0, option_mid=28.0, now=2100.0))
    assert d is not None and d.qty == 100 and "T2 28.00 touched" in d.note
    apply_exit(pos, d, fill_price=28.0, charges=0, now=2100.0)
    # the stepped component stays at T1 until T2 is sustained; the band (28 × 0.97 = 27.16) is read
    # live and is what the single rising line becomes — max(stepped SL, peak − 3 %) — never folded in
    assert pos.sustained_idx == 0 and pos.ratchet_sl == 24.0
    assert e._band_level(pos, _view(now=2100.0)) == 27.16
    for t in (2130.0, 2165.0):
        assert e.evaluate(pos, _view(option_ltp=28.5, option_mid=28.5, now=t)) is None
    assert e.evaluate(pos, _view(option_ltp=28.5, option_mid=28.5, now=2176.0)) is None
    assert pos.sustained_idx == 1 and pos.ratchet_sl == 28.0


def test_the_last_rung_takes_the_rest():
    from kotsin_nse.risk.exits import apply_exit

    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    _sustain_t1(e, pos)
    for ltp in (28.0, 32.0):
        d = e.evaluate(pos, _view(option_ltp=ltp, option_mid=ltp, now=2100.0 + ltp))
        apply_exit(pos, d, fill_price=ltp, charges=0, now=2100.0 + ltp)
    d = e.evaluate(pos, _view(option_ltp=36.0, option_mid=36.0, now=2200.0))
    assert d is not None and d.qty == 100 and "the rest" in d.note


def test_after_arming_the_stop_is_max_of_the_stepped_sl_and_peak_minus_3pct_and_is_hard():
    e = ExitEngine(RT_X_LIMITS)
    pos = _own(option_targets=(24.0, 100.0))  # a far T2, so 40 tests the line, not a rung
    _sustain_t1(e, pos)
    # the peak lifts the line: 40 × (1 − 3 %) = 38.80 (spread 0.5 % × 1.5 = 0.75 % < 3 %); the
    # stepped SL stays at T1 — the band is read live, never folded into it
    assert e.evaluate(pos, _view(option_ltp=40.0, option_mid=40.0, now=2100.0)) is None
    assert pos.ratchet_sl == 24.0 and e._band_level(pos, _view(now=2100.0)) == 38.8
    # trading through it ends the trade at once — no 75 s grace on the rising stop
    d = e.evaluate(pos, _view(option_ltp=38.5, option_mid=38.5, now=2101.0))
    assert d is not None and d.qty == pos.qty_remaining and "hard SL 38.80" in d.note


def test_breakeven_is_hard_too_after_the_first_lot():
    from kotsin_nse.risk.exits import apply_exit

    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, now=2000.0))
    apply_exit(pos, d, fill_price=24.0, charges=0, now=2000.0)
    d = e.evaluate(pos, _view(option_ltp=19.9, option_mid=19.9, now=2010.0))
    assert d is not None and d.qty == 300 and "hard SL 20.00" in d.note


def test_a_contract_without_its_own_ladder_arms_on_the_equity_trigger_only():
    e = ExitEngine(RT_X_LIMITS)
    pos = _own(option_t1=0.0, option_targets=())
    assert e.evaluate(pos, _view(option_ltp=30.0, option_mid=30.0, now=2000.0)) is None
    d = e.evaluate(pos, _view(option_ltp=30.0, option_mid=30.0, underlying_ltp=1020.0, now=2070.0))
    assert d is not None and pos.option_targets == (30.0,) and pos.armed_by == "equity"


def test_the_base_book_never_arms_on_the_underlying_and_keeps_its_share_ladder():
    e = ExitEngine(RiskLimits())
    pos = _own(strategy="FUDKII")
    assert e.evaluate(pos, _view(underlying_ltp=1020.0)) is None, "equity T1 means nothing to the base book"
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0))
    assert d is not None and d.qty == 100 and "40%" in d.note, "the legacy share ladder (40%, lot-rounded), untouched"
    assert pos.armed_by == "" and pos.ratchet_sl == 0.0


# -- RT-N and RT-Y: the same fills, a different arming and give-back ------------------------------


def test_rt_n_arms_the_instant_the_underlying_touches_its_t1_and_the_band_needs_three_reads():
    """The policy that ran on 2026-09-23: one lot out at the equity trigger and the 2 % band live at
    once — no 75 s sustain in front of it — but a breach of the band is three consecutive reads, so
    one crossed print cannot end the trade."""
    from kotsin_nse.risk.exits import apply_exit
    from kotsin_nse.risk.limits import RT_N_LIMITS

    e = ExitEngine(RT_N_LIMITS)
    pos = _own(strategy="FUDKII_RT_N")
    d = e.evaluate(pos, _view(option_ltp=22.0, option_mid=22.0, underlying_ltp=1020.0, now=2000.0))
    assert d is not None and d.qty == 100 and pos.armed_by == "equity"
    apply_exit(pos, d, fill_price=22.0, charges=0, now=2000.0)
    assert pos.armed_ts == 2000.0 and pos.ratchet_sl == 20.0 and pos.peak_mid == 22.0
    # 23.50 is under the next rung (its own R1 at 24.00 follows the 22.00 equity-made T1)
    assert e.evaluate(pos, _view(option_ltp=23.5, option_mid=23.5, now=2010.0)) is None
    assert e._band_level(pos, _view(now=2010.0)) == 23.03, "2 % off the 23.50 peak"
    for t in (2011.0, 2012.0):
        assert e.evaluate(pos, _view(option_ltp=23.0, option_mid=23.0, now=t)) is None
    d = e.evaluate(pos, _view(option_ltp=23.0, option_mid=23.0, now=2013.0))
    assert d is not None and d.reason.value == "TRAIL" and "[dwell]" in d.note and d.qty == pos.qty_remaining


def test_rt_n_arms_on_a_minute_close_over_its_own_r1_not_on_a_touch():
    from kotsin_nse.risk.limits import RT_N_LIMITS

    e = ExitEngine(RT_N_LIMITS)
    pos = _own(strategy="FUDKII_RT_N")
    assert e.evaluate(pos, _view(option_ltp=24.5, option_mid=24.5, now=2000.0)) is None, "a touch of R1 is not a close"
    assert pos.armed_by == ""
    d = e.evaluate(pos, _view(option_ltp=24.5, option_mid=24.5, now=2065.0))
    assert d is not None and d.qty == 100 and "1m close 24.50" in d.note
    assert pos.armed_ts == 2065.0 and pos.ratchet_sl == 20.0


def test_rt_y_arms_at_plus_5pct_when_it_has_no_own_rung_over_it_and_not_before():
    """The expected-move threshold asked an intraday trade for half of a whole DAY's move. On
    2026-09-24 the four RT-Y positions needed +43 %, +49 %, +54 % and +72 % to arm, reached +5.7 %
    at best, and not one of them ever armed. +5 % on the premium paid is the minimum T1."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_edm=1.0, option_t1=0.0, option_targets=())  # no own rung
    assert e.evaluate(pos, _view(option_ltp=20.9, option_mid=20.9, now=2000.0)) is None, "under +5 %"
    assert pos.armed_by == ""
    d = e.evaluate(pos, _view(option_ltp=21.0, option_mid=21.0, now=2001.0))
    assert d is not None and d.qty == 100, "the arm pays one lot, not the position"
    assert "entry +5% (21.00)" in d.note
    assert pos.option_targets == (21.0,) and pos.option_t1 == 21.0
    assert pos.armed_by == "option" and pos.ratchet_sl == 20.0, "the T1 staircase: SL to breakeven"


def test_rt_y_an_own_t1_over_plus_5pct_is_t1_itself():
    """Operator, 2026-09-26: "treat +5% as a floor, not as where lot 1 actually sells". Own T1 24.00
    on a 20.00 entry: +5 % (21.00) sells nothing; lot 1 goes at 24.00, where its sell also rests."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y")  # own ladder 24 / 28 / 32 / 36 on a 20.00 entry
    for k, px in enumerate((21.0, 22.5, 23.9)):
        assert e.evaluate(pos, _view(option_ltp=px, option_mid=px, now=2000.0 + k)) is None, f"{px}: over +5 %, under its own T1"
    assert pos.armed_by == "" and pos.targets_hit == 0 and pos.option_targets == (24.0, 28.0, 32.0, 36.0)
    assert e.resting_target(pos) == (0, 24.0, 100), "the resting sell is at the same T1"
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, now=2010.0))
    assert d is not None and d.qty == 100 and "its own rung" in d.note
    assert pos.armed_by == "option" and pos.ratchet_sl == 20.0


def test_rt_y_a_near_own_t1_waits_for_the_5pct_minimum():
    """Operator, 2026-09-25: +5 % is the minimum before arming. A rung 2.5 % over entry must not arm
    the trade and step the SL to breakeven — that is how KEI and GRASIM were stopped. It is dropped
    and the minimum becomes T1, the higher rungs following."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_t1=20.5, option_targets=(20.5, 28.0, 32.0, 36.0))
    assert e.evaluate(pos, _view(option_ltp=20.6, option_mid=20.6, now=2000.0)) is None, "its T1 is under +5 %"
    assert pos.armed_by == "" and pos.ratchet_sl == 0.0, "no arm, no breakeven stop"
    d = e.evaluate(pos, _view(option_ltp=21.0, option_mid=21.0, now=2001.0))
    assert d is not None and "entry +5% (21.00)" in d.note
    assert pos.option_targets == (21.0, 28.0, 32.0, 36.0), "the rung below the minimum is dropped"


def test_rt_y_the_equity_t1_also_waits_for_the_5pct_minimum():
    """With no own T1 at or over +5 %, the minimum is T1: the underlying at its T1 sells lot 1 once
    the option is at least +5 %."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_t1=20.5, option_targets=(20.5, 28.0, 32.0, 36.0))
    assert e.evaluate(pos, _view(option_ltp=20.4, option_mid=20.4, underlying_ltp=1020.0, now=2000.0)) is None
    assert pos.armed_by == "", "the underlying is at its T1 but the option is only +2 %"
    d = e.evaluate(pos, _view(option_ltp=21.2, option_mid=21.2, underlying_ltp=1020.0, now=2001.0))
    assert d is not None and pos.armed_by == "equity" and pos.option_targets[0] == 21.2


def test_rt_y_the_equity_t1_never_sells_below_an_own_t1_over_5pct():
    """T1 = max(own T1, +5 %) on every route (operator, 2026-09-26: "treat +5% as a floor, not as
    where lot 1 actually sells"; review: this route sold lot 1 at 21.50 with the own T1 at 24.00)."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y")  # own ladder 24/28/32/36 on a 20.00 entry
    assert e.evaluate(pos, _view(option_ltp=21.5, option_mid=21.5, underlying_ltp=1020.0, now=2000.0)) is None
    assert pos.armed_by == "" and pos.option_targets == (24.0, 28.0, 32.0, 36.0), "T1 is still its own 24.00"
    assert e.resting_target(pos) == (0, 24.0, 100), "and its sell rests there"
    d = e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, underlying_ltp=1020.0, now=2001.0))
    assert d is not None and d.qty == 100 and pos.armed_by == "option", "the option at its own T1: lot 1 goes there"


def test_rt_x_the_equity_t1_still_sells_where_the_option_stands():
    """A book without the +5 % floor is unchanged: the underlying at its T1 makes the option's price T1."""
    from kotsin_nse.risk.limits import RT_X_LIMITS

    e = ExitEngine(RT_X_LIMITS)
    pos = _own()
    d = e.evaluate(pos, _view(option_ltp=21.5, option_mid=21.5, underlying_ltp=1020.0, now=2000.0))
    assert d is not None and pos.armed_by == "equity" and pos.option_targets[0] == 21.5


def test_rt_y_with_no_own_rung_rests_its_t1_at_the_5pct_minimum():
    """Review, 2026-09-26: with no own ladder nothing rested and lot 1 went at market on the first
    print over +5 %. The minimum is T1, so its sell rests there — one lot, the band carries the rest."""
    from kotsin_nse.risk.limits import RT_X_LIMITS, RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_t1=0.0, option_targets=())
    assert e.resting_target(pos) == (0, 21.0, 100)
    d = e.resting_target_filled(pos, 2000.0, 21.0, 21.0)
    assert d.qty == 100 and pos.option_targets == (21.0,) and pos.armed_by == "option"
    assert ExitEngine(RT_X_LIMITS).resting_target(_own(option_t1=0.0, option_targets=())) is None, "RT-X has no floor: nothing to rest"


def test_rt_y_a_jump_through_both_arms_on_the_real_rung_and_keeps_the_ladder():
    """When one print clears the minimum AND an own rung above it, the rung is the arm."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_t1=22.0, option_targets=(22.0, 28.0, 32.0, 36.0))
    d = e.evaluate(pos, _view(option_ltp=22.5, option_mid=22.5, now=2000.0))
    assert d is not None and "T1 22.00 touched" in d.note and "its own rung" in d.note
    assert pos.option_targets == (22.0, 28.0, 32.0, 36.0)


def test_rt_y_with_no_own_rungs_still_arms_and_keeps_lots_back_for_the_band():
    """BANKNIFTY gapped 1.53 % on 2026-09-24. Its option ladder, anchored on the previous close,
    carried nothing above entry, so the book had no T1, could never arm and could never trail. The
    percentage arms it; because there is no rung above the synthetic T1, the arm must still pay only
    its tranche and leave the rest to the give-back band."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_t1=0.0, option_targets=())
    assert e.evaluate(pos, _view(option_ltp=20.9, option_mid=20.9, now=2000.0)) is None
    d = e.evaluate(pos, _view(option_ltp=21.5, option_mid=21.5, now=2001.0))
    assert d is not None and d.qty == 100, "one lot out, not all four"
    assert pos.option_targets == (21.5,) and pos.option_t1 == 21.5


#: BANKNIFTY 29 SEP 2026 PE 55300, 2026-09-24, from the tick tape: (IST second, ltp, bid, ask).
_BN_TAPE = [
    ("09:45:03", 206.05, 205.0, 205.9), ("09:45:04", 206.4, 206.4, 207.05), ("09:45:05", 208.65, 207.9, 208.75),
    ("09:45:06", 212.0, 211.2, 212.1), ("09:45:07", 210.5, 209.7, 210.55), ("09:45:09", 211.25, 210.7, 211.5),
    ("09:45:10", 210.65, 210.85, 211.5), ("09:45:11", 212.0, 210.95, 211.6), ("09:45:12", 213.0, 212.55, 213.15),
    ("09:45:13", 213.95, 213.55, 214.05), ("09:45:15", 214.5, 213.95, 214.45), ("09:45:16", 215.8, 216.25, 217.2),
    ("09:45:17", 216.4, 216.45, 217.35), ("09:45:18", 216.2, 215.75, 216.7), ("09:45:20", 216.9, 216.5, 217.0),
    ("09:45:22", 217.45, 217.2, 218.1), ("09:45:23", 216.6, 216.2, 217.0), ("09:45:24", 217.7, 216.25, 217.15),
    ("09:45:26", 215.25, 214.35, 215.0), ("09:45:27", 215.6, 215.3, 216.45), ("09:45:28", 216.6, 216.0, 216.9),
    ("09:45:29", 214.8, 212.3, 213.2), ("09:45:31", 209.7, 209.5, 210.3), ("09:45:32", 210.4, 209.3, 210.25),
    ("09:45:33", 209.7, 208.5, 209.45), ("09:45:34", 208.2, 208.0, 208.75), ("09:45:35", 208.3, 207.3, 208.15),
]


def test_rt_y_banknifty_2026_09_24_on_the_tape_arms_at_216_and_leaves_on_the_3pct_line():
    """The trade that set the rule. The PE filled at 205.93; the old threshold wanted 293.85
    (+42.7 %) and nothing armed. It crossed +5 % (216.23) at 09:45:17, peaked at 217.70 and was
    back under +5 % nine seconds later — so arming must switch the band on AT the touch. A 75 s
    sustain never completed and the rest went at breakeven (tape replay: 201.00, gross -128). With
    the band live at the touch the rest leave on the 3 % line off the 217.65 peak (208.50, +547)."""
    from datetime import datetime, timedelta, timezone

    from kotsin_nse.config import Segment
    from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType
    from kotsin_nse.risk.exits import apply_exit
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    ist = timezone(timedelta(hours=5, minutes=30))

    def ts(hms: str) -> float:
        return datetime.strptime(f"2026-09-24 {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=ist).timestamp()

    pe = Instrument(
        scrip_code="69776", symbol="BANKNIFTY", segment=Segment.NSE_FO, kind=InstrumentKind.OPTION,
        name="BANKNIFTY 29 SEP 2026 PE 55300.00", lot_size=30, tick_size=0.05, multiplier=1,
        expiry="2026-09-29", strike=55300.0, option_type=OptionType.PE, underlying="BANKNIFTY",
    )
    pos = _own(
        strategy="FUDKII_RT_Y", instrument=pe, qty=120, entry=205.93, opened_ts=ts("09:45:03"),
        direction=Direction.BEARISH, equity_entry=55480.0, equity_sl=55676.35, equity_targets=(55275.95,),
        option_sl=136.98, option_t1=0.0, option_targets=(), option_edm=0.853,
    )
    e = ExitEngine(RT_Y_LIMITS)
    tape = {ts(h): (ltp, bid, ask) for h, ltp, bid, ask in _BN_TAPE}
    out, last = [], None
    for sec in range(int(ts("09:45:03")), int(ts("09:45:35")) + 1):
        last = tape.get(float(sec), last)
        ltp, bid, ask = last
        mid = (bid + ask) / 2
        d = e.evaluate(pos, _view(option_ltp=ltp, option_mid=mid, underlying_ltp=55480.0,
                                  spread_pct=(ask - bid) / mid, now=float(sec)))
        if d is not None:
            fill = bid
            out.append((datetime.fromtimestamp(sec, ist).strftime("%H:%M:%S"), d.reason.value, d.qty, fill, d.note))
            apply_exit(pos, d, fill_price=fill, charges=0, now=float(sec))
        if pos.qty_remaining == 0:
            break
    assert [o[:4] for o in out] == [
        ("09:45:17", "TARGET", 30, 216.45),
        ("09:45:33", "TRAIL", 90, 208.5),
    ], out
    assert "entry +5% (216.23)" in out[0][4]
    assert "give-back line 211.12 off the 217.65 peak" in out[1][4] and "[dwell]" in out[1][4]
    assert sum((f - 205.93) * q for _, _, q, f, _ in out) == pytest.approx(547.2, abs=0.5)

def _touch_t1_y(e, pos, *, t0=2000.0, level=24.5):
    """RT-Y's T1 touch: one lot out, SL to breakeven, and the band armed at that instant."""
    from kotsin_nse.risk.exits import apply_exit

    d = e.evaluate(pos, _view(option_ltp=level, option_mid=level, now=t0))
    assert d is not None and d.reason.value == "TARGET"
    apply_exit(pos, d, fill_price=level, charges=0, now=t0)
    assert pos.armed_ts == t0 and pos.peak_mid == level, "armed at the touch, not after a sustain"
    return d


def test_rt_y_keeps_the_sl_one_rung_behind_and_exits_the_rest_on_a_3pct_giveback():
    """T1's touch arms the band and leaves the SL at breakeven (one rung behind). The band is 3 %
    off the peak and confirms over three one-second reads — prompt, but one print cannot end it."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_edm=0.4)
    _touch_t1_y(e, pos)
    assert pos.ratchet_sl == 20.0, "SL at breakeven"
    assert e._band_level(pos, _view(now=2000.0)) == 23.77, "3 % off the 24.50 peak, over the 20.00 SL"
    assert e.evaluate(pos, _view(option_ltp=23.7, option_mid=23.7, now=2100.0)) is None, "one read is not an exit"
    assert pos.trail_dwell == 1
    assert e.evaluate(pos, _view(option_ltp=24.0, option_mid=24.0, now=2101.0)) is None
    assert pos.trail_dwell == 0, "back over the line: the dwell resets"
    assert e.evaluate(pos, _view(option_ltp=23.7, option_mid=23.7, now=2102.0)) is None
    assert e.evaluate(pos, _view(option_ltp=23.7, option_mid=23.7, now=2103.0)) is None
    d = e.evaluate(pos, _view(option_ltp=23.7, option_mid=23.7, now=2104.0))
    assert d is not None and d.reason.value == "TRAIL" and "[dwell]" in d.note and d.qty == pos.qty_remaining


def test_rt_y_the_3pct_band_sits_above_the_lagged_rung_sl_at_every_rung():
    """Because the rung SL lags a whole rung behind, the 3 % band is always the higher line once
    armed: it is the band that ends an RT-Y trade, the rung SL the floor beneath it."""
    from kotsin_nse.risk.exits import apply_exit
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_edm=0.4)
    _touch_t1_y(e, pos)
    d = e.evaluate(pos, _view(option_ltp=28.0, option_mid=28.0, now=2100.0))
    apply_exit(pos, d, fill_price=28.0, charges=0, now=2100.0)
    assert pos.ratchet_sl == 24.0, "the SL lags one rung behind T2"
    assert e._band_level(pos, _view(now=2100.0)) == 27.16, "3 % off the 28.00 peak, well over the 24.00 SL"
    for t in (2200.0, 2201.0):
        assert e.evaluate(pos, _view(option_ltp=27.0, option_mid=27.0, now=t)) is None
    d = e.evaluate(pos, _view(option_ltp=27.0, option_mid=27.0, now=2202.0))
    assert d is not None and d.reason.value == "TRAIL" and "[dwell]" in d.note
    assert "give-back line 27.16 off the 28.00 peak" in d.note and d.qty == pos.qty_remaining


def test_ct_y_inherits_the_rt_y_arming_and_giveback_exactly():
    from kotsin_nse.risk.limits import CT_Y_LIMITS, RT_Y_LIMITS

    assert CT_Y_LIMITS.arm_at_pct == RT_Y_LIMITS.arm_at_pct == 5.0
    assert CT_Y_LIMITS.peak_giveback_pct == RT_Y_LIMITS.peak_giveback_pct == 3.0
    assert CT_Y_LIMITS.band_exit == RT_Y_LIMITS.band_exit == "dwell"
    assert CT_Y_LIMITS.giveback_move_frac == RT_Y_LIMITS.giveback_move_frac == 0.0
    assert CT_Y_LIMITS.dried_volume_v is None, "the wall rule is the fade's own filter"


def test_rt_y_t2_touch_steps_the_sl_to_t1_and_t2_sustained_leaves_it_there():
    from kotsin_nse.risk.exits import apply_exit
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_edm=0.4)
    _touch_t1_y(e, pos)
    d = e.evaluate(pos, _view(option_ltp=28.0, option_mid=28.0, now=2100.0))
    assert d is not None and "T2 28.00 touched" in d.note
    apply_exit(pos, d, fill_price=28.0, charges=0, now=2100.0)
    assert pos.ratchet_sl == 24.0
    for t in (2130.0, 2165.0, 2176.0):
        assert e.evaluate(pos, _view(option_ltp=28.5, option_mid=28.5, now=t)) is None
    assert pos.sustained_idx == 1 and pos.ratchet_sl == 24.0, "one rung behind: T2 sustained leaves the SL at T1"


def test_the_legacy_half_of_peak_floor_is_the_base_books_not_the_rt_books():
    """Entry 20, stop 17, peak 24.5 (1.5R), back to 21.5: the base book gives up half the peak
    and leaves; an own-ladder book has its own line and this rule must not pre-empt it."""
    base, rt = ExitEngine(RiskLimits()), ExitEngine(RT_X_LIMITS)
    a, b = _own(strategy="FUDKII", option_targets=(40.0,)), _own(option_targets=(40.0,), option_t1=40.0)
    for e, p in ((base, a), (rt, b)):
        assert e.evaluate(p, _view(option_ltp=24.5, option_mid=24.5, now=2000.0)) is None
    assert base.evaluate(a, _view(option_ltp=21.5, option_mid=21.5, now=2010.0)) is not None
    assert rt.evaluate(b, _view(option_ltp=21.5, option_mid=21.5, now=2010.0)) is None
