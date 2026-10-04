"""Every strategy under all three stop rules (operator, 2026-10-04: "each startegy: fudkii-rt-y, fudkii-rt-x, etc
have all 3 ... and then we comapre which [stop rule] works best with whcih startegy"; "yes they should keep running
as per their rules").

Each book's fill is copied into two mirror books — the same contract, size, price, instant and stop levels — whose
only difference is the rule that judges the stop: ``_SE`` the stock's stop with a fixed 60 s confirmation, ``_SA``
confirmed by magnitude × time. Their own wallets; off the trading totals; a strip on every card."""

from __future__ import annotations

import math
import re
import time
from dataclasses import replace
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.domain import Direction, ExitReason
from kotsin_nse.engine import _ORDER_REF_RE, BOOK_LABELS, IN_TREND_BOOKS, ORDER_CODES
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.risk.limits import RT_Y_LIMITS, RT_Y_W1_LIMITS, RiskLimits, stop_rule_limits
from kotsin_nse.strategy.keys import SHADOW_BOOKS, STOP_MIRRORS, StrategyKey, stop_mirrors_of
from tests.test_limit_orders import UND, _book, _engine, _sig


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


SOURCES = ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y", "FUDKII_RT_MCX",
           "FUDKII_RT_Y_F", "FUDKII_RT_Y_W1", "FUDKII_CT_M")


def test_every_book_has_two_mirrors_with_their_own_codes_labels_and_purses():
    assert {src.value for src, _r in STOP_MIRRORS.values()} == set(SOURCES)
    for src in SOURCES:
        m = stop_mirrors_of(src)
        assert set(m) == {"E", "A"} and m["E"].value == f"{src}_SE" and m["A"].value == f"{src}_SA"
    assert all(k in SHADOW_BOOKS for k in STOP_MIRRORS), "never in the day's totals"
    codes = [ORDER_CODES[k.value] for k in StrategyKey]
    assert len(set(codes)) == len(codes), "every book's order code is its own"
    for k in STOP_MIRRORS:
        code = ORDER_CODES[k.value]
        assert _ORDER_REF_RE.match(f"{code}-261005-091500-001"), code
        assert len(code) == len(ORDER_CODES[STOP_MIRRORS[k][0].value]), "no longer than its source's: ids stay inside 38"
    assert BOOK_LABELS["FUDKII_RT_Y_SA"] == "RT-Y · stop adaptive (shadow)"
    assert StrategyKey.FUDKII_RT_Y_W1_SE.display_name == "RT-Y · wide stop (shadow) · stop E (shadow)"


def test_a_mirrors_limits_are_its_books_with_only_the_stop_rule_changed():
    e_lim, a_lim = stop_rule_limits(RT_Y_LIMITS, "E"), stop_rule_limits(RT_Y_LIMITS, "A")
    assert e_lim.stop_mode == a_lim.stop_mode == "equity"
    assert math.isinf(e_lim.eq_stop_area_pct_s) and a_lim.eq_stop_area_pct_s == 1.0, "E: 60 s fixed; A: magnitude × time"
    assert replace(e_lim, stop_mode="option", eq_stop_area_pct_s=1.0) == RT_Y_LIMITS
    assert replace(a_lim, stop_mode="option") == RT_Y_LIMITS
    with pytest.raises(ValueError):
        stop_rule_limits(RiskLimits(), "Z")


@pytest.mark.asyncio
async def test_every_fill_is_mirrored_exactly_and_each_mirror_judges_by_its_own_rule(settings, clock):
    e = await _engine(settings, clock)
    try:
        for m, (src, rule) in STOP_MIRRORS.items():
            lim = e.limits_for(m.value)
            assert lim == stop_rule_limits(e.limits_for(src.value), rule), m
        _book(e, 16.95, 17.25, clock[0])
        sig = _sig(clock)
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])  # the ask comes down to the limits: every in-trend book fills
        await e._manage_positions()
        by = {p.strategy: p for p in e.positions.values()}
        books = ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_RT_Y_W1")
        assert set(by) == {*books, *(m.value for b in books for m in stop_mirrors_of(b).values())}, "each book and its two mirrors"
        for b in books:
            src = by[b]
            for m in stop_mirrors_of(b).values():
                mp = by[m.value]
                assert (mp.entry, mp.qty, mp.instrument, mp.opened_ts, mp.signal_id) == (src.entry, src.qty, src.instrument, src.opened_ts, src.signal_id)
                assert (mp.equity_sl, mp.option_sl, mp.option_targets, mp.r_unit) == (src.equity_sl, src.option_sl, src.option_targets, src.r_unit)
                assert mp.exec_log["ref"].startswith(ORDER_CODES[m.value]) and mp.exec_log["exits"] == []
                assert e.wallets[m.value].balance < e.wallets[m.value].initial or e.wallets[m.value].deployed > 0, "its own purse paid"
        # the wide shadow's mirrors copy the WIDE stop, never widened twice
        assert by["FUDKII_RT_Y_W1_SE"].equity_sl == by["FUDKII_RT_Y_W1"].equity_sl != by["FUDKII_RT_Y"].equity_sl
        assert RT_Y_W1_LIMITS.equity_stop_buffer_pct == 1.0
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_option_stop_takes_the_book_out_while_its_mirrors_hold_on_the_stocks_stop(settings, clock):
    """RT-Y's option mid sits under its option stop for 75 s with the stock inside its own stop: RT-Y (the
    current rule) is stopped; its two mirrors, judging the stock, hold."""
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=(StrategyKey.FUDKII_RT_Y,))
        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])
        e.ltps[UND.scrip_code] = 187.0
        await e._manage_positions()
        y = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_Y")
        assert y.direction is Direction.BULLISH and y.equity_sl == 185.0
        low = round(y.option_sl - 0.30, 2)
        for _ in range(90):
            clock[0] += 1
            _book(e, low - 0.05, low + 0.05, clock[0])
            e.ltps[UND.scrip_code] = 186.0  # the stock well inside its 185.00 stop
            await e._manage_positions()
        assert y.status == "CLOSED" and y.exit_reason == ExitReason.SL_OP.value, "the option stop's 75 s sustain"
        mirrors = [p for p in e.positions.values() if p.strategy in ("FUDKII_RT_Y_SE", "FUDKII_RT_Y_SA")]
        assert len(mirrors) == 2 and all(p.status == "OPEN" for p in mirrors), "the stock never reached its stop"
        # now the stock goes decisively through: both mirrors sell at once, on the stock
        clock[0] += 1
        e.ltps[UND.scrip_code] = 184.70
        _book(e, low - 0.05, low + 0.05, clock[0])
        await e._manage_positions()
        assert all(p.status == "CLOSED" and p.exit_reason == ExitReason.SL_EQ.value for p in mirrors)
        x = mirrors[0].exec_log["exits"][-1]
        assert x["stop"]["level"] == 185.0 and x["stop"]["triggerOn"] == "underlying" and x["stop"]["triggerPrice"] == 184.70
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_operators_skip_closes_the_real_trade_and_its_mirrors_keep_their_rules(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        sig = _sig(clock)
        await e._handle_signal(sig, None, books=IN_TREND_BOOKS)
        clock[0] += 22
        _book(e, 16.95, 17.10, clock[0])
        e.ltps[UND.scrip_code] = 187.0
        await e._manage_positions()
        await e.operator_skip("FUDKII_RT_X", sig.signal_id)
        clock[0] += 16
        _book(e, 16.95, 17.10, clock[0])
        await e._manage_positions()
        real = [p for p in e.positions.values() if p.strategy == "FUDKII_RT_X"]
        assert not real or real[0].status == "CLOSED", "the real trade is closed"
        mirrors = [p for p in e.positions.values() if p.strategy in ("FUDKII_RT_X_SE", "FUDKII_RT_X_SA")]
        assert len(mirrors) == 2 and all(p.status == "OPEN" for p in mirrors), "the mirrors keep running"
        assert all("closed by the operator" in p.note for p in mirrors)
        card = (await e.book_cards("FUDKII_RT_X"))["cards"][0]
        rows = {r["rule"]: r for r in card["stopRules"]}
        assert set(rows) == {"current", "E", "A"}
        assert rows["current"]["status"] == "EXITED" and rows["current"]["exitReason"] == ExitReason.MANUAL.value
        assert rows["E"]["status"] == rows["A"]["status"] == "OPEN" and rows["E"]["operatorClosedReal"] is True
        assert rows["A"]["openGross"] is not None, "an open mirror shows its P&L so far"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_card_without_a_position_or_for_a_book_without_mirrors_has_no_strip(settings, clock):
    e = await _engine(settings, clock)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None, books=(StrategyKey.FUDKII,))
        card = (await e.book_cards("FUDKII"))["cards"][0]
        assert card["state"] == "PENDING" and card["stopRules"] is None, "nothing held yet: no strip"
        assert e._stop_rule_rows("FUKAA", {"id": "x", "signal_id": "s"}, {}, {}) is None
    finally:
        await e.stop()


def test_the_mirror_order_code_follows_the_ref_pattern_for_every_source():
    for k in STOP_MIRRORS:
        code = ORDER_CODES[k.value]
        assert re.fullmatch(r"FI[EA]-[A-Z]{1,3}", code), code


def test_the_shadow_page_tallies_each_book_against_its_mirrors_trade_by_trade():
    from kotsin_nse.api.shadow import TABS, stop_rules_summary

    def pos(pid, book, sid, status="CLOSED", reason="SL-OP"):
        return {"id": pid, "strategy": book, "signal_id": sid, "status": status, "opened_ts": 1.0, "symbol": "TECHM", "entry": 27.1,
                "instrument": {"name": "TECHM 27 OCT 2026 CE 1620.00"}, "exit_reason": reason}

    positions = [
        pos("a", "FUDKII_RT_Y", "s1"), pos("b", "FUDKII_RT_Y_SE", "s1", reason="SL-EQ"), pos("c", "FUDKII_RT_Y_SA", "s1", reason="TRAIL"),
        pos("d", "FUDKII_RT_Y", "s2"), pos("e", "FUDKII_RT_Y_SE", "s2", status="OPEN"), pos("f", "FUDKII_RT_Y_SA", "s2"),
        pos("g", "FUDKII_RT_Y", "s0"),  # before the mirrors existed: not a comparison
    ]
    trades = [{"position_id": "a", "net": -4080.0, "exit_reason": "SL-OP"}, {"position_id": "b", "net": -6600.0, "exit_reason": "SL-EQ"},
              {"position_id": "c", "net": 2100.0, "exit_reason": "TRAIL"}, {"position_id": "d", "net": -100.0, "exit_reason": "SL-OP"},
              {"position_id": "f", "net": -100.0, "exit_reason": "SL-OP"}, {"position_id": "g", "net": 5.0, "exit_reason": "EOD"}]
    sr = stop_rules_summary(positions=positions, trades=trades)
    y = sr["books"]["FUDKII_RT_Y"]
    assert y["current"]["closed"] == y["E"]["closed"] == y["A"]["closed"] == 1, "only the trigger closed under all three counts"
    assert y["E"]["diff"] == -2520.0 and y["E"]["worse"] == 1 and y["A"]["diff"] == 6180.0 and y["A"]["better"] == 1
    assert y["current"]["stops"] == 1 and y["A"]["stops"] == 0 and y["A"]["wins"] == 1
    assert [t["signal_id"] for t in sr["trades"]] == ["s1", "s2"] and sr["trades"][1]["closed"] is False
    assert sr["total"]["A"]["diff"] == 6180.0
    assert TABS[0].id == "gate-b" and any(t.id == "stop-rules" for t in TABS)
