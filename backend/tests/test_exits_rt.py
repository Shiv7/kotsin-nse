"""The FUDKII-RT exit policy: sustain, hard floor, peak ratchet — and a written precedence."""

from __future__ import annotations

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
        expiry="2026-09-29", strike=100.0, option_type=OptionType.CE, underlying="X",
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


def test_the_peak_ratchet_arms_only_after_t1_and_needs_consecutive_reads():
    e = ExitEngine(RT_X_LIMITS)
    pos = _pos(targets_hit=1, peak_mid=30.0)
    # give-back floor is max(2%, 1.5 x spread). spread 0.5% -> 2% of 30.00 = 0.60 -> level 29.40
    v = dict(option_ltp=29.0, option_mid=29.0, now=2000.0)
    assert e.evaluate(pos, _view(**v)) is None and pos.trail_dwell == 1
    assert e.evaluate(pos, _view(**v)) is None and pos.trail_dwell == 2
    d = e.evaluate(pos, _view(**v))
    assert d is not None and "gave back" in d.note


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
