"""The owner's four-layer gate (strategy/wise_gate.py): each layer judged alone, the reason names the layer that said no."""
from kotsin_nse.strategy import wise_gate as wg

BULL = {"futures_state": "LB", "bias": "Strong Bull", "x_call_wall": 120.0, "x_put_wall": 90.0}
BEAR = {"futures_state": "SB", "bias": "Strong Bear", "x_call_wall": 110.0, "x_put_wall": 80.0}


def _vp(poc=99.0, val=97.0, vah=101.0):
    return {"poc": poc, "val": val, "vah": vah, "minutes": 40}


def test_all_four_layers_agree_for_a_long():
    g = wg.layers(1, 100.0, 1.0, 5.0, with_move=True, tags={"st60_al": True}, fno=BULL, retest_ext=98.0, vp=_vp())
    assert g["passed"] and all(g[k] for k in wg.LAYERS) and g["room"] == 20.0 and wg.why(g) == "stack: all four"


def test_each_layer_says_no_on_its_own_and_the_reason_names_it():
    base = dict(with_move=True, tags={"st60_al": True}, fno=BULL, retest_ext=98.0, vp=_vp())
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "with_move": False})
    assert not g["passed"] and not g["equity"] and "equity" in wg.why(g) and g["future"] and g["option"] and g["profile"]
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "tags": {"st60_al": False}})
    assert not g["equity"]
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "fno": {**BULL, "futures_state": "SC"}})
    assert not g["future"] and "future (SC)" in wg.why(g)
    assert wg.layers(1, 100.0, 1.0, 5.0, **{**base, "fno": {**BULL, "futures_state": "SC"}}, future_loose=True)["future"]
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "fno": {**BULL, "bias": "Strong Bear"}})
    assert not g["option"] and "option (Strong Bear" in wg.why(g)
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "fno": {**BULL, "x_call_wall": 103.0}})
    assert not g["option"] and g["room"] == 3.0  # under one ADR of room
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "vp": _vp(poc=100.5)})
    assert not g["profile"] and not g["poc_with"] and g["va_held"] and "profile (poc 100.5" in wg.why(g)
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "retest_ext": 95.0})
    assert not g["profile"] and g["poc_with"] and not g["va_held"]  # the pullback went through the value area low by more than a tolerance
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "vp": None})
    assert not g["profile"] and "no profile yet" in wg.why(g)
    g = wg.layers(1, 100.0, 1.0, 5.0, **{**base, "fno": {}})
    assert not g["future"] and not g["option"] and "no state" in wg.why(g) and "no board" in wg.why(g)


def test_a_short_mirrors_every_layer():
    g = wg.layers(-1, 100.0, 1.0, 5.0, with_move=True, tags={"st60_al": True}, fno=BEAR, retest_ext=102.0, vp=_vp(poc=101.0, val=99.0, vah=101.0))
    assert g["passed"] and g["room"] == 20.0
    assert not wg.layers(-1, 100.0, 1.0, 5.0, with_move=True, tags={"st60_al": True}, fno={**BEAR, "futures_state": "LU"}, retest_ext=102.0, vp=_vp(poc=101.0))["future"]
    assert wg.layers(-1, 100.0, 1.0, 5.0, with_move=True, tags={"st60_al": True}, fno={**BEAR, "futures_state": "LU"}, retest_ext=102.0, vp=_vp(poc=101.0), future_loose=True)["future"]
    assert not wg.layers(-1, 100.0, 1.0, 5.0, with_move=True, tags={"st60_al": True}, fno=BEAR, retest_ext=102.5, vp=_vp(poc=101.0, val=99.0, vah=101.0))["profile"]


def _bar(o, h, lo, c):
    from kotsin_nse.structure.engine import Bar5
    return Bar5(0, o, h, lo, c, 0.0)


def test_the_extra_checks_are_recorded_with_their_facts():
    doji = _bar(100.0, 101.0, 99.2, 100.1)  # a tenth of a point of body in a 1.8 range; the lower wick is under half the range, so not a hammer
    hammer = _bar(100.0, 101.0, 97.0, 100.8)  # a 0.8 body in a 4-point range with a 3-point lower wick: a hammer, not a doji
    prev = _bar(100.0, 100.6, 99.4, 99.6)
    engulf = _bar(99.5, 101.2, 99.3, 101.0)
    ex = wg.extras(1, retest_hm="09:30", retest_bar=doji, confirm_bar=engulf, prev_bar=prev, fno={"futures_state": "LB", "calls_state": "LB", "puts_state": "SB", "x_pcr_oi": 0.6, "x_pcr_oi_chg": 0.1},
                   monthly={"MP": 99.2, "MR1": 105.0}, lo=99.0, hi=99.5, tol=0.5)
    assert ex["doji"] and not ex["pin"] and ex["engulf"] and ex["candle_any"] and ex["doji_or_pin"]
    assert ex["fno_full"] and ex["options_with"] and ex["pcr_with"] and ex["combo"] == "LB/LB/SB"
    assert ex["near_monthly"] and ex["monthly"] == ["MP"] and ex["rt_before_1230"] and not ex["rt_after_open"]
    ex2 = wg.extras(1, retest_hm="13:05", retest_bar=hammer, confirm_bar=prev, prev_bar=engulf, fno={}, monthly=None, lo=None, hi=None, tol=0.5)
    assert ex2["pin"] and not ex2["doji"] and not ex2["engulf"] and not ex2["fno_full"] and not ex2["pcr_with"] and not ex2["near_monthly"]
    assert not ex2["rt_before_1230"] and ex2["rt_after_open"]
    ok, missing = wg.extra_passed(ex, ("fno_full", "rt_after_open", "near_monthly"))
    assert not ok and missing == ["rt_after_open"]
    assert wg.extra_passed(ex, ()) == (True, [])
    short = wg.extras(-1, retest_hm="10:00", retest_bar=_bar(100.0, 103.0, 99.7, 99.8), confirm_bar=_bar(100.5, 100.6, 98.8, 99.0), prev_bar=_bar(99.6, 100.6, 99.4, 100.4),
                      fno={"futures_state": "SB", "calls_state": "SB", "puts_state": "LB", "x_pcr_oi_chg": -0.2}, monthly=None, lo=None, hi=None, tol=0.5)
    assert short["pin"] and short["engulf"] and short["fno_full"] and short["pcr_with"]


def test_market_profile_and_fibonacci_tags():
    mp = {"poc": 100.0, "val": 99.0, "vah": 101.0, "day_type": "normal", "ib_high": 101.5, "ib_low": 98.5, "single_prints": 2, "poor_high": False, "poor_low": True}
    x = wg.market_profile_tags(1, 100.5, mp, 0.5)
    assert x["tpo_with"] and x["tpo_in_value"] and x["tpo_day_type"] == "normal" and x["tpo_poor_behind"] and x["tpo"]["poc"] == 100.0
    assert not wg.market_profile_tags(-1, 100.5, mp, 0.5)["tpo_with"]
    assert wg.market_profile_tags(1, 100.5, None, 0.5)["tpo"] is None
    sw = {"high": 120.0, "low": 100.0, "close": 115.0}
    f = wg.fib_tags(1, 107.7, sw, 0.5)  # 12.3 of a 20-point swing below the high: a 61.5 % retracement
    assert f["fib_ratio"] == 0.615 and f["fib_level"] == 0.618 and f["fib_near"] and f["trend20"] == "up" and f["trend20_with"]
    f2 = wg.fib_tags(1, 111.0, sw, 0.5)  # 45 %: a point off the 50 and 1.4 off the 38.2 — not within half a point of either
    assert f2["fib_level"] is None and not f2["fib_near"] and f2["fib_ratio"] == 0.45
    f3 = wg.fib_tags(-1, 110.0, sw, 0.5)  # a short, measured from the swing low: 50 %
    assert f3["fib_level"] == 0.5 and not f3["trend20_with"]
    assert wg.fib_tags(1, 100.0, None, 0.5)["fib_ratio"] is None

