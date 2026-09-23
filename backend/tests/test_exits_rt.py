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


def test_rt_y_does_not_arm_until_the_option_has_made_half_a_days_expected_move():
    """KEI and GRASIM armed at breakeven and were retested through it: arming waits for the option
    to have moved 0.5 × its expected daily move, on the equity trigger and on its own rungs alike."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_edm=1.0)  # a day's move = 100 % of premium → arm at 30.00
    assert e.evaluate(pos, _view(option_ltp=22.0, option_mid=22.0, underlying_ltp=1020.0, now=2000.0)) is None
    assert e.evaluate(pos, _view(option_ltp=24.5, option_mid=24.5, now=2001.0)) is None, "its own T1 at 24 is under the threshold"
    assert pos.armed_by == ""
    d = e.evaluate(pos, _view(option_ltp=30.0, option_mid=30.0, underlying_ltp=1020.0, now=2010.0))
    assert d is not None and pos.option_targets == (30.0, 32.0, 36.0) and pos.ratchet_sl == 20.0


def test_rt_y_keeps_the_sl_one_rung_behind_and_every_post_arm_stop_needs_the_sustain():
    """T1 sustained arms the band but leaves the SL at breakeven (one rung behind), and a breach
    of the line must hold 75 s: KEI's breakeven fired on a one-minute wick at 10:31 with the
    underlying up, two hours before a +143 % move."""
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_edm=0.4)  # threshold 24.00 = T1
    _sustain_t1(e, pos)
    assert pos.sustained_idx == 0 and pos.armed_ts == 2076.0 and pos.ratchet_sl == 20.0, "armed, SL still at breakeven"
    # band = max(10 %, 0.25 × 40 %) = 10 % off the 24.50 peak → 22.05, above the 20.00 rung SL
    assert e._band_level(pos, _view(now=2076.0)) == 22.05
    assert e.evaluate(pos, _view(option_ltp=21.5, option_mid=21.5, now=2100.0)) is None, "one read through the band is not an exit"
    assert pos.line_breach_since == 2100.0
    assert e.evaluate(pos, _view(option_ltp=22.5, option_mid=22.5, now=2130.0)) is None
    assert pos.line_breach_since is None, "back over the line: the clock resets"
    assert e.evaluate(pos, _view(option_ltp=21.5, option_mid=21.5, now=2140.0)) is None
    assert e.evaluate(pos, _view(option_ltp=21.5, option_mid=21.5, now=2214.0)) is None, "74 s is not 75"
    d = e.evaluate(pos, _view(option_ltp=21.5, option_mid=21.5, now=2216.0))
    assert d is not None and d.reason.value == "TRAIL" and "[sustain]" in d.note and d.qty == pos.qty_remaining


def test_rt_y_t2_touch_steps_the_sl_to_t1_and_t2_sustained_leaves_it_there():
    from kotsin_nse.risk.exits import apply_exit
    from kotsin_nse.risk.limits import RT_Y_LIMITS

    e = ExitEngine(RT_Y_LIMITS)
    pos = _own(strategy="FUDKII_RT_Y", option_edm=0.4)
    _sustain_t1(e, pos)
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
