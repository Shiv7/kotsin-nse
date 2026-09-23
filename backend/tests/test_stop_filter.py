"""The stop-inside-noise filter: available on GradePolicy, off by default (see the docstring for
the evidence), and a filter — the signal is graded F — never a widening."""

from kotsin_nse.bars.pivots import GradePolicy, Zone, compute_confluence

ZONES = [Zone(99.7, 6.0, ["1d.R1", "1wk.R1"]), Zone(103.0, 7.2, ["1d.R2", "1wk.R2"]), Zone(106.0, 6.0, ["1d.R3", "1wk.R3"])]


def test_off_by_default_and_a_filter_when_on():
    off = compute_confluence(close=100.0, bullish=True, zones=ZONES, atr_value=2.0)
    assert off.grade != "F" and off.stop == 99.7, "a 0.15-ATR stop trades with the filter off"
    on = compute_confluence(close=100.0, bullish=True, zones=ZONES, atr_value=2.0, policy=GradePolicy(min_stop_atr_filter=0.3))
    assert on.grade == "F" and on.stop == 99.7 and "inside one bar" in on.note, "filtered, not widened"
    far = compute_confluence(close=100.0, bullish=True, zones=[Zone(98.0, 6.0, ["1d.S1", "1wk.S1"]), *ZONES[1:]], atr_value=2.0, policy=GradePolicy(min_stop_atr_filter=0.3))
    assert far.grade != "F", "a 1-ATR stop passes the filter"


def test_the_fade_honours_the_same_policy_and_an_f_grade_is_no_plan():
    from kotsin_nse.domain import Direction
    from kotsin_nse.strategy.base import Signal
    from kotsin_nse.strategy.counter import NO_WALL, CounterDecision, flipped_signal
    from kotsin_nse.strategy.keys import StrategyKey

    sig = Signal(strategy=StrategyKey.FUDKII, symbol="X", direction=Direction.BULLISH, ts=1, entry=100.0, stop=98.0, targets=(104.0,))
    zones = [Zone(100.3, 6.0, ["1d.TC", "1wk.TC"]), Zone(96.0, 7.2, ["1d.S1", "1wk.S1"]), Zone(93.0, 6.0, ["1d.S2", "1wk.S2"])]
    dec = CounterDecision("COUNTER", "test", NO_WALL)
    assert flipped_signal(sig, key=StrategyKey.FUDKII_CT_X, zones=zones, atr=2.0, tick_size=0.05, decision=dec) is not None, "a 0.15-ATR stop fades with the filter off"
    assert flipped_signal(sig, key=StrategyKey.FUDKII_CT_X, zones=zones, atr=2.0, tick_size=0.05, decision=dec, policy=GradePolicy(min_stop_atr_filter=0.3)) is None, "the fade is filtered like the parent"
