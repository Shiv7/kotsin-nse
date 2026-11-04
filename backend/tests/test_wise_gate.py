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
