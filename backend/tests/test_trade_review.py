"""The trade review's root causes (operator, 2026-10-05: "be accurate in identifying the root cause of the loss for
each trade"): each rule is pinned to the record that makes it, and a trade the record cannot explain says so."""

from __future__ import annotations

import pytest

from kotsin_nse.api.review import (
    ROOT_CAUSE_LABEL,
    ROOT_CAUSE_ORDER,
    parent_signal_id,
    review_day,
    review_trade,
)

T0 = 1_791_173_700.0  # 09:45 IST, 5 Oct 2026
ATR = 1.0


def _bars(path: list[tuple[float, float]], start: float = T0 - 1800) -> list[dict]:
    """One-minute bars from (low, high) pairs, one a minute."""
    return [{"ts": start + 60 * i, "o": (lo + hi) / 2, "h": hi, "l": lo, "c": (lo + hi) / 2, "v": 1000.0} for i, (lo, hi) in enumerate(path)]


def _pos(**kw) -> dict:
    p = {
        "id": "p1", "strategy": "FUDKII_RT_X", "symbol": "ABC", "direction": "BULLISH", "status": "CLOSED", "qty": 1000,
        "entry": 10.0, "r_unit": 1.0, "initial_option_sl": 9.0, "option_sl": 9.0, "equity_entry": 100.0, "equity_sl": 99.0,
        "equity_targets": [102.0, 104.0], "equity_atr": ATR, "opened_ts": T0 + 30, "closed_ts": T0 + 30 + 1200,
        "exit_reason": "SL-EQ", "exit_price": 9.0, "mfe_r": 0.1, "mae_r": -1.0, "targets_hit": 0, "note": "delta≈0.50 (estimated)",
        "signal_id": "FUDKII-ABC-1791171900-B", "instrument": {"name": "ABC CE 100", "tick_size": 0.05},
        "exec_log": {"entry": {"signalTs": T0 + 5, "filledTs": T0 + 30, "waitS": 25.0, "ref": 10.0, "fillPrice": 10.0,
                               "bookAtPlace": {"bid": 9.95, "ask": 10.05}},
                     "exits": [{"fillPrice": 9.0, "stop": {"level": 99.0, "triggerOn": "underlying", "triggerPrice": 98.95, "bidAtTrigger": 9.0}}]},
    }
    p.update(kw)
    return p


def _review(pos, *, path, trade=None, breadth=None, route=None, fade=None, signal=None, mirrors=()):
    trade = trade or {"net": -1200.0, "gross": -1000.0, "charges": 200.0, "r_multiple": -1.2}
    breadth = breadth or {"share": 0.6, "direction": "BULLISH", "volSurgeT": 2.0, "volSurgeT1": 2.0, "gapDatr": 0.1, "openBar": True}
    signal = signal or {"evidence": {"close": 100.0, "atr": ATR, "oi_change_pct": 1.0}}
    return review_trade(pos, trade=trade, signal=signal, breadth=breadth, route=route, fade=fade, bars=_bars(path),
                        mirrors=list(mirrors), session_end=T0 + 6 * 3600)


FLAT = [(99.9, 100.1)] * 30  # the half hour before the trigger


def test_every_cause_has_a_label_and_a_place_in_the_order():
    assert set(ROOT_CAUSE_ORDER) <= set(ROOT_CAUSE_LABEL)
    assert parent_signal_id("FUDKII_CT_Y-BAJFINANCE-1791171900-B") == "FUDKII-BAJFINANCE-1791171900-B"
    assert parent_signal_id("FUDKII-MAHABANK-1791171900-B") == "FUDKII-MAHABANK-1791171900-B"


def test_the_option_stop_firing_while_the_stock_never_reached_its_own_is_named():
    pos = _pos(exit_reason="SL-OP", exit_price=8.9, exec_log={**_pos()["exec_log"], "exits": [
        {"fillPrice": 8.9, "stop": {"level": 9.0, "triggerOn": "option mid", "triggerPrice": 8.95, "bidAtTrigger": 8.9}}]})
    r = _review(pos, path=FLAT + [(99.6, 100.2)] * 20 + [(99.7, 100.4)] * 300)  # the stock never under 99.0, before or after
    assert r["cause"]["key"] == "option_stop" and "never reached its stop 99" in r["cause"]["why"]
    assert r["stock"]["hitStop"] is False and r["stock"]["laterHitStopTs"] is None


def test_an_option_stop_before_a_stock_that_then_breaks_its_stop_is_the_thesis_failing_not_the_option():
    pos = _pos(exit_reason="SL-OP", exit_price=8.9, exec_log={**_pos()["exec_log"], "exits": [
        {"fillPrice": 8.9, "stop": {"level": 9.0, "triggerOn": "option mid", "triggerPrice": 8.95, "bidAtTrigger": 8.9}}]})
    r = _review(pos, path=FLAT + [(99.3, 100.2)] * 22 + [(98.5, 99.5)] * 10, mirrors=[{"rule": "E", "status": "CLOSED", "net": -1500.0}])
    assert r["cause"]["key"] != "option_stop" and r["stock"]["laterHitStopTs"] is not None
    assert "the option stop got out before the stock reached its stop" in r["cause"]["why"] and "E -1,500" in r["cause"]["why"]


def test_a_stock_stop_inside_one_bars_noise_is_the_cause():
    pos = _pos(equity_sl=99.9, mfe_r=1.2)  # 0.1 ATR from the entry
    r = _review(pos, path=FLAT + [(99.8, 100.6)] * 20)
    assert r["cause"]["key"] == "tight_stop" and "0.10 ATR" in r["cause"]["why"] and "+1.20 R first" in r["cause"]["why"]


def test_a_trailed_stop_above_the_entry_that_filled_below_it_is_the_exit_cost():
    pos = _pos(exit_reason="SL-OP", option_sl=10.4, exit_price=9.9, mfe_r=0.8, exec_log={**_pos()["exec_log"], "exits": [
        {"fillPrice": 9.9, "stop": {"level": 10.4, "triggerOn": "option last", "triggerPrice": 10.1, "bidAtTrigger": 9.95}}]})
    r = _review(pos, path=FLAT + [(99.8, 101.0)] * 20, trade={"net": -300.0, "gross": -100.0, "charges": 200.0, "r_multiple": -0.3})
    assert r["cause"]["key"] == "exit_cost" and "₹500 below the stop level" in r["cause"]["why"] and "above the 10 paid" in r["cause"]["why"]
    exit_check = next(c for c in r["checks"] if c["key"] == "exit")
    assert exit_check["status"] == "bad" and exit_check["exitSlipInr"] == 500.0 and not any("under the level" in x for x in r["cause"]["contributing"])


def test_a_trail_at_breakeven_that_closed_just_under_is_named_and_a_winner_given_back_too():
    ex = {**_pos()["exec_log"], "exits": [{"fillPrice": 9.9, "stop": {"level": 9.95, "triggerOn": "option mid", "triggerPrice": 9.92, "bidAtTrigger": 9.9}}]}
    r = _review(_pos(exit_reason="SL-OP", option_sl=9.95, exit_price=9.9, mfe_r=0.2, exec_log=ex), path=FLAT + [(99.8, 100.2)] * 20)
    assert r["cause"]["key"] == "breakeven_stop"
    r = _review(_pos(exit_reason="SL-OP", option_sl=9.95, exit_price=9.9, mfe_r=0.9, exec_log=ex), path=FLAT + [(99.8, 100.9)] * 20)
    assert r["cause"]["key"] == "gave_back" and "+0.90 R" in r["cause"]["why"]


def test_a_stall_at_the_level_just_ahead_is_a_pivot_rejection():
    route = {"legs": [{"name": "equity", "levels": {"1d.R1": 100.3, "1d.S1": 98.0}}]}
    r = _review(_pos(), path=FLAT + [(99.9, 100.3)] * 10 + [(98.8, 99.5)] * 10, route=route)
    assert r["cause"]["key"] == "pivot_rejection" and "1d.R1 100.3" in r["cause"]["why"]
    pivot = next(c for c in r["checks"] if c["key"] == "pivot")
    assert pivot["status"] == "bad" and pivot["stalled"] is True


def test_a_chased_entry_and_a_market_against_the_trade():
    pos = _pos(exec_log={**_pos()["exec_log"], "entry": {**_pos()["exec_log"]["entry"], "filledTs": T0 + 30}})
    path = FLAT + [(100.4, 100.6)] + [(99.0, 100.5)] * 15  # the fill minute already 0.5 ATR up
    r = _review(pos, path=path, signal={"evidence": {"close": 100.0, "atr": ATR, "oi_change_pct": 1.0}})
    assert r["cause"]["key"] == "chased"
    r = _review(_pos(), path=FLAT + [(98.9, 100.1)] * 15, breadth={"share": 0.30, "direction": "BULLISH", "volSurgeT": 2.0, "volSurgeT1": 2.0})
    assert r["cause"]["key"] == "market_against" and "30 %" in r["cause"]["why"]


def test_stopped_then_right_is_too_early_and_costs_and_rule_closes_are_their_own():
    r = _review(_pos(), path=FLAT + [(98.9, 100.1)] * 21 + [(100.5, 102.5)] * 30)  # stopped, then +2.5 ATR and T1
    assert r["cause"]["key"] == "too_early" and "reaching the first target 102" in r["cause"]["why"]
    r = _review(_pos(), path=FLAT + [(98.9, 100.1)] * 15, trade={"net": -50.0, "gross": 150.0, "charges": 200.0, "r_multiple": -0.05})
    assert r["cause"]["key"] == "costs"
    r = _review(_pos(exit_reason="DAILY_LOSS"), path=FLAT + [(99.5, 100.1)] * 15)
    assert r["cause"]["key"] == "closed_by_rule"


def test_a_fade_through_its_stop_that_then_turned_was_right_but_early():
    """BAJFINANCE CT-Y, 5 Oct: the gap fade's stop 993.45 (1 ATR) went at 09:53, the stock ran to 995.8 (0.31 ATR past
    it), was back through the 985.95 entry at 10:22 and 1.88 ATR the fade's way by 11:50 — not a failed fade."""
    fade = {"stop": 101.0, "gapDatr": 0.76}
    pos = _pos(strategy="FUDKII_CT_Y", direction="BEARISH", equity_sl=101.0, signal_id="FUDKII_CT_Y-ABC-1791171900-B", equity_targets=[97.0])
    path = FLAT + [(99.9, 100.9)] * 8 + [(100.5, 101.3)] * 3 + [(99.5, 100.6)] * 20 + [(98.0, 99.0)] * 30
    r = _review(pos, path=path, fade=fade, breadth={"share": 0.56, "direction": "BULLISH", "volSurgeT": 8.6, "volSurgeT1": 10.8})
    why = r["cause"]["why"]
    assert r["cause"]["key"] == "too_early" and why.startswith("the fade was right, but early")
    assert "101.3" in why and "0.30 ATR past the stop" in why and "back through the entry 100" in why and "2.00 ATR the trade's way" in why
    r = _review(pos, path=FLAT + [(99.9, 101.2)] * 15 + [(100.6, 101.5)] * 30, fade=fade,
                breadth={"share": 0.56, "direction": "BULLISH", "volSurgeT": 8.6, "volSurgeT1": 10.8})
    assert r["cause"]["key"] == "fade_failed" and "did not come back" in r["cause"]["why"]


def test_a_stock_that_never_got_clear_of_a_level_just_ahead_is_the_cause_not_its_entry_cost():
    """MAHABANK, 5 Oct: the 1mo pivot 82.01 sat 0.08 ATR over the 81.96 entry; the best was 82.21 (0.31 ATR past it, short of
    1d.R2 82.43); the option stop went at 10:02 and the stock broke its own stop at 10:55. The ₹1,430 paid over the mid was
    not why it lost ₹4,837."""
    route = {"legs": [{"name": "equity", "levels": {"1mo.PIVOT": 100.08, "1d.R2": 100.72, "1d.S1": 98.0}}]}
    ex = {"entry": {**_pos()["exec_log"]["entry"], "fillPrice": 10.3, "bookAtPlace": {"bid": 9.95, "ask": 10.05}},
          "exits": [{"fillPrice": 9.0, "stop": {"level": 9.1, "triggerOn": "option mid", "triggerPrice": 9.05, "bidAtTrigger": 9.0}}]}
    pos = _pos(exit_reason="SL-OP", entry=10.3, exit_price=9.0, closed_ts=T0 + 30 + 900, exec_log=ex)
    path = FLAT + [(99.9, 100.39)] * 15 + [(99.6, 100.2)] * 10 + [(98.8, 99.4)] * 10
    r = _review(pos, path=path, route=route, trade={"net": -1300.0, "gross": -1300.0, "charges": 0.0, "r_multiple": -1.3})
    assert r["cause"]["key"] == "pivot_rejection", r["cause"]
    why = r["cause"]["why"]
    assert "1mo.PIVOT 100.08" in why and "0.31 ATR past it" in why and "short of 1d.R2 100.72" in why and "broke its stop 99" in why
    pivot = next(c for c in r["checks"] if c["key"] == "pivot")
    assert pivot["status"] == "bad" and pivot["stalled"] is True
    assert any("over" in x or "paid" in x for x in r["cause"]["contributing"]), "the entry cost is a contributing factor"


def test_a_failed_fade_names_its_wall_and_a_clean_loss_says_the_record_has_no_cause():
    fade = {"stop": 101.0, "gapDatr": 0.8}
    pos = _pos(strategy="FUDKII_CT_Y", direction="BEARISH", equity_sl=101.0, signal_id="FUDKII_CT_Y-ABC-1791171900-B")
    r = _review(pos, path=FLAT + [(99.9, 101.2)] * 15, fade=fade,
                breadth={"share": 0.6, "direction": "BULLISH", "volSurgeT": 2.0, "volSurgeT1": 2.0})
    assert r["cause"]["key"] == "fade_failed" and r["counterTrend"] is True
    r = _review(_pos(), path=FLAT + [(98.9, 100.1)] * 15)
    assert r["cause"]["key"] == "unexplained"


def test_open_and_winning_trades_and_the_days_cause_tally():
    assert _review(_pos(status="OPEN", closed_ts=None), path=FLAT + [(99.8, 100.4)] * 10)["cause"]["key"] == "open"
    assert _review(_pos(exit_reason="TRAIL"), path=FLAT + [(99.8, 101.4)] * 10, trade={"net": 900.0, "gross": 1100.0, "charges": 200.0})["cause"]["key"] == "win"
    day = review_day(positions=[_pos(), _pos(id="m1", strategy="FUDKII_RT_X_SE")], trades=[{"position_id": "p1", "net": -1200.0, "gross": -1000.0, "charges": 200.0}],
                     signals=[], events=[], bars_by_symbol={"ABC": _bars(FLAT + [(98.9, 100.1)] * 15)}, session_end=T0 + 3600,
                     labels={}, mirror_books={"FUDKII_RT_X_SE": ("FUDKII_RT_X", "E")})
    assert [t["id"] for t in day["trades"]] == ["p1"], "a mirror rides on its source's row"
    assert day["trades"][0]["mirrors"][0]["rule"] == "E" and day["summary"]["losses"] == 1
    assert day["summary"]["causes"][0]["trades"] == 1


@pytest.mark.parametrize("missing", ["breadth", "route", "bars"])
def test_a_trade_with_a_gap_in_its_record_still_reviews_and_says_what_it_cannot_answer(missing):
    kw = {"breadth": None, "route": None}
    r = review_trade(_pos(), trade={"net": -1200.0, "gross": -1000.0, "charges": 200.0}, signal=None, fade=None, mirrors=[], session_end=T0 + 3600,
                     bars=[] if missing == "bars" else _bars(FLAT + [(98.9, 100.1)] * 15), **kw)
    assert r["cause"]["key"] in ROOT_CAUSE_ORDER
    assert any(c["status"] == "unknown" for c in r["checks"])
