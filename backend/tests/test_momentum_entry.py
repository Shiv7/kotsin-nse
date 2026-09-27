"""The entry rule on the OPTION's own move (operator, 2026-09-26).

"if it is racing ahead, take a call in 30th second and get in quickly" · "the option's move not the
stock/equity's move" · "Cap the 30-second buy price, for example no more than 3% above the signal
price of the option" · the limit moves to the mid "after 30s wait". At 60 s an unfilled entry is
missed: the Sep 1-25 replay lost ₹0.9-2.4 L a month buying the ask at 60 s."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.exec.resting import LimitPolicy, entry_cap, entry_limit, option_run_pct, race_call
from kotsin_nse.market.session import IST, ist_today

from .test_limit_orders import UND, _book, _engine, _sig


@pytest.fixture
def clock(monkeypatch):
    now = [datetime.combine(ist_today(), dtime(11, 0), tzinfo=IST).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def test_the_defaults_are_the_3_percent_cap_after_a_30_s_hold():
    """Operator, 2026-09-26: "if the results are poor, we will revert to only 3% cap" — the race at 30 s
    and the cross after 30 s both lost money in the Sep 1-25 replay, so both are off."""
    from kotsin_nse.config import Settings

    pol = LimitPolicy()
    assert (pol.entry_hold_s, pol.entry_cap_pct, pol.entry_wait_s) == (30.0, 3.0, 60.0)
    assert pol.entry_race_pct is None and pol.entry_cross_after_hold is False
    s = Settings(_env_file=None)
    assert (s.paper_limit_entry_hold_s, s.paper_limit_entry_cap_pct, s.paper_limit_entry_race_pct, s.paper_limit_entry_cross_after_hold) == (30.0, 3.0, None, False)


def test_the_cap_and_the_run_are_in_the_options_own_price():
    assert entry_cap(17.10, 3.0) == 17.60, "17.613 → the tick at or below it"
    assert entry_cap(17.10, None) is None and entry_cap(None, 3.0) is None
    assert entry_limit(17.10, 18.00, 18.40, 0.05, cap=17.60) == (17.60, "at the mid — the signal price has left the book, capped at 17.6 (signal price + the cap)")
    assert entry_limit(17.10, 16.95, 17.25, 0.05, cap=17.60)[0] == 17.10, "inside the book: the signal price, the cap is not in play"
    assert option_run_pct(17.10, 17.30, 17.50, None) == pytest.approx(1.754, abs=0.001)
    assert option_run_pct(17.10, None, None, 17.44) == pytest.approx(1.988, abs=0.001), "no two-sided book: the last price"
    assert option_run_pct(None, 17.3, 17.5, None) is None


def test_the_race_needs_the_run_and_an_ask_within_the_cap():
    pol = LimitPolicy(entry_race_pct=1.0)  # the switch, on
    assert race_call(1.8, 17.50, 17.60, pol)[0] is True
    go, why = race_call(0.6, 17.25, 17.60, pol)
    assert not go and "not racing" in why
    go, why = race_call(2.3, 17.70, 17.60, pol)
    assert not go and "over the cap" in why
    assert race_call(2.3, None, 17.60, pol) == (False, "option +2.3% at 30 s — racing, but no ask to take")
    assert race_call(5.0, 17.50, 17.60, replace(pol, entry_race_pct=None))[0] is False, "None = no early call"


@pytest.mark.asyncio
async def test_the_race_reads_the_option_not_the_stock_and_looks_once(settings, clock):
    e = await _engine(settings, clock)
    e.limit_policy = replace(e.limit_policy, entry_race_pct=1.0)  # the switch, on
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        r = next(iter(e._resting.values()))
        t0 = clock[0]
        e.ltps[UND.scrip_code] = 195.0  # the STOCK has run 4 % the trade's way …
        clock[0] = t0 + 31
        _book(e, 17.05, 17.25, clock[0])  # … the option is flat (+0.3 %): not racing
        await e._manage_positions()
        assert r.race_checked and "not racing" in r.momentum[0]["note"] and not e.positions
        clock[0] = t0 + 40
        _book(e, 17.30, 17.50, clock[0])  # the option races now — but the one look has been taken
        await e._manage_positions()
        assert len(r.momentum) == 1 and not [p for p in e.positions.values() if "racing" in (p.exec_log["entry"].get("outcome") or "")]
    finally:
        await e.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("ask_at_35", "ask_at_50", "fill"), [
    (17.50, 17.50, (35, 17.50)),   # within the cap at the first look after 30 s: bought at once, at the ask
    (17.80, 17.55, (50, 17.55)),   # over the cap at 35 s, within it at 50 s: bought then
    (17.80, 17.90, None),          # never within the cap: missed at 60 s
])
async def test_after_the_hold_the_ask_is_taken_as_soon_as_it_is_within_the_cap(settings, clock, ask_at_35, ask_at_50, fill):
    """Operator, 2026-09-26 (under test): "wait for 30s for the limit order to fill, if not, then fill
    order at the least price within 3% cap as soon as possible". Ref 17.10 → cap 17.60."""
    e = await _engine(settings, clock)
    e.limit_policy = replace(e.limit_policy, entry_cross_after_hold=True, entry_race_pct=None)
    try:
        _book(e, 16.95, 17.25, clock[0])
        await e._handle_signal(_sig(clock), None)
        t0 = clock[0]
        for dt, ask in ((20, 17.30), (35, ask_at_35), (50, ask_at_50), (61, 17.90)):
            clock[0] = t0 + dt
            _book(e, ask - 0.20, ask, clock[0])
            await e._manage_positions()
            if e.positions:
                break
        p = next((p for p in e.positions.values() if p.strategy == "FUDKII"), None)
        if fill is None:
            assert p is None and not e._resting, "never within the cap: missed"
        else:
            assert p is not None and p.entry == fill[1] and clock[0] == t0 + fill[0]
            assert p.entry <= 17.60 and "within the cap" in p.exec_log["entry"]["outcome"]
    finally:
        await e.stop()
