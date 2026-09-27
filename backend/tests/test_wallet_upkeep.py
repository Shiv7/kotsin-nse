"""The wallets made true from their own records (audit and operator, 2026-09-26).

* "1. The 15% drawdown halt can clear overnight" — the daily and drawdown breakers shared one flag:
  a daily halt hid a drawdown crossed under it, and the morning cleared both;
* "2. No morning reset after a restart past midnight" — the day rolled over only when the running
  process saw midnight ("we can easily record the date and time and immediately correct the status of
  the wallet depending upon opening and closing balance of that date");
* "3. ₹24,326.25 stuck as deployed in RT-X, RT-N and RT-Y since 24 Sep 09:45" — SBILIFE's twins were
  copied from a parent already closed; the copy was fixed on 2026-09-25, the money it stranded never
  was, because nothing ever compared ``deployed`` with what a book actually holds.
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType, Position, PosSide
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.risk.limits import RiskLimits
from kotsin_nse.risk.wallet import Wallet

LIM = RiskLimits()  # 10 % daily, 15 % drawdown


def _t(day_offset: int, hm: str = "11:00") -> float:
    h, m = map(int, hm.split(":"))
    return datetime.combine(ist_today(), dtime(h, m), tzinfo=IST).timestamp() + day_offset * 86_400


def test_a_drawdown_crossed_under_a_daily_halt_is_recorded_and_survives_the_morning():
    """The audit's probe: −10.5 % trips the daily halt; a further −7 % takes the book past 15 % below
    its peak while it is halted for the day. Before: halted=False the next morning."""
    w = Wallet.new("FUDKII_RT_X", 1_000_000, _t(0))
    w.apply_close(-105_000, _t(0))
    assert w.check_breakers(LIM, _t(0)) == "DAILY_LOSS -10.50%"
    w.apply_close(-70_000, _t(0, "11:30"))  # the flatten exits after the halt: 17.5 % below the peak
    assert w.check_breakers(LIM, _t(0, "11:30")) == "DRAWDOWN 17.50%", "read even though already halted"
    assert w.halted and w.halt_reason == "DRAWDOWN 17.50% · DAILY_LOSS -10.50%"
    closed = w.rollover(_t(1, "09:00"))
    assert closed == {"day": ist_today().isoformat(), "open": 1_000_000.0, "close": 825_000.0, "pnl": -175_000.0,
                      "dailyHalt": "DAILY_LOSS -10.50%", "drawdownHalt": "DRAWDOWN 17.50%"}
    assert w.halted and w.halt_reason == "DRAWDOWN 17.50%" and not w.daily_halt, "only the daily halt clears"
    assert w.day_start_balance == 825_000.0, "today opens on yesterday's close"


def test_one_close_through_both_limits_records_both():
    w = Wallet.new("FUDKII", 1_000_000, _t(0))
    w.apply_close(-160_000, _t(0))
    assert w.check_breakers(LIM, _t(0)) == "DAILY_LOSS -16.00% · DRAWDOWN 16.00%"
    w.rollover(_t(1))
    assert w.halted and w.drawdown_halt.startswith("DRAWDOWN")


def test_the_stored_row_is_the_previous_builds_shape_and_both_builds_read_each_other():
    """Audit, 2026-09-26: the previous build loads a wallet with ``cls(**row)``; two new keys in the
    row crashed its boot, so a rollback would not start. The slots live in ``halt_reason``."""
    import dataclasses

    w = Wallet.new("FUDKII_RT_X", 1_000_000, _t(0))
    w.apply_close(-160_000, _t(0))
    w.check_breakers(LIM, _t(0))
    row = w.to_json()
    previous_fields = {f.name for f in dataclasses.fields(Wallet)} - {"daily_halt", "drawdown_halt"}
    assert set(row) == previous_fields, "exactly the previous build's keys"
    assert row["halted"] and row["halt_reason"] == "DRAWDOWN 16.00% · DAILY_LOSS -16.00%"
    back = Wallet.from_json(row)
    assert (back.drawdown_halt, back.daily_halt) == ("DRAWDOWN 16.00%", "DAILY_LOSS -16.00%")
    # the previous build's own rollover clears only a reason that STARTS with DAILY_LOSS: this
    # combined reason starts with DRAWDOWN, so even rolled back the drawdown halt survives the night
    assert not row["halt_reason"].startswith("DAILY_LOSS")


def test_a_row_written_before_the_two_slots_keeps_its_halt():
    old = Wallet.new("FUDKII", 1_000_000, _t(0)).to_json()
    old.update(halted=True, halt_reason="DRAWDOWN 15.25%")
    w = Wallet.from_json(old)
    assert w.drawdown_halt == "DRAWDOWN 15.25%" and w.halted
    w.rollover(_t(1))
    assert w.halted, "a pre-fix drawdown halt is not lost to the first morning"
    old.update(halt_reason="DAILY_LOSS -3.21%")
    w2 = Wallet.from_json(old)
    assert w2.daily_halt == "DAILY_LOSS -3.21%"
    w2.rollover(_t(1))
    assert not w2.halted


@pytest.mark.asyncio
async def test_a_boot_after_midnight_rolls_every_wallet_over_and_logs_the_closed_day(settings):
    e = Engine(settings)
    await e.start_core()
    try:
        y = e.wallets["FUDKII_RT_Y"]
        y.day = "2026-09-25"  # the book's last recorded day
        y.day_start_balance, y.balance = 1_000_000.0, 890_000.0
        y.daily_halt = "DAILY_LOSS -11.00%"
        y._sync_halt()
        await e._wallet_upkeep(time.time(), boot=True)
        assert y.day == ist_today().isoformat() and not y.halted and y.day_start_balance == 890_000.0
        ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 60) if x.get("kind") == "wallet.rollover"]
        mine = next(x for x in ev if x["strategy"] == "FUDKII_RT_Y")
        assert (mine["day"], mine["open"], mine["close"], mine["dailyHalt"], mine["atBoot"]) == ("2026-09-25", 1_000_000.0, 890_000.0, "DAILY_LOSS -11.00%", True)
        await e._wallet_upkeep(time.time())
        assert len([x for x in await e.ledger.rows_between("events", 0, time.time() + 60) if x.get("kind") == "wallet.rollover"]) == len(ev), "idempotent"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_breaker_earned_while_the_process_was_down_is_on_record_before_the_first_entry(settings):
    e = Engine(settings)
    await e.start_core()
    try:
        x = e.wallets["FUDKII_RT_X"]
        x.peak, x.balance, x.day_start_balance = 1_000_000.0, 840_000.0, 840_000.0  # 16 % down, today flat
        await e._wallet_upkeep(time.time(), boot=True)
        assert x.halted and x.drawdown_halt == "DRAWDOWN 16.00%"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_money_stranded_as_deployed_is_released_at_boot_and_while_running_only_on_a_second_sighting(settings):
    e = Engine(settings)
    await e.start_core()
    try:
        for book in ("FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"):
            e.wallets[book].deployed = 24_326.25  # SBILIFE's three twins, born CLOSED on 2026-09-24
        await e._wallet_upkeep(time.time(), boot=True)
        assert all(e.wallets[b].deployed == 0.0 for b in ("FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"))
        ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 60) if x.get("kind") == "wallet.deployed_corrected"]
        assert {x["strategy"] for x in ev} == {"FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}
        assert all(x["was"] == 24_326.25 and x["drift"] == 24_326.25 and x["now"] == 0.0 for x in ev)

        # while running: an open position's cost is what deployed should be; a drift is corrected only
        # when the SAME drift is seen again at least 30 s later
        opt = Instrument("1", "X", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=100, strike=100.0, option_type=OptionType.CE, underlying="X")
        und = Instrument("2", "X", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="X")
        e.positions["p"] = Position(id="p", strategy="FUDKII_RT_X", instrument=opt, underlying=und, side=PosSide.LONG, qty=400,
                                    entry=10.0, opened_ts=time.time(), signal_id="s", direction=Direction.BULLISH)
        w = e.wallets["FUDKII_RT_X"]
        w.deployed = 4_000.0 + 500.0
        t0 = time.time()
        await e._wallet_upkeep(t0)
        await e._wallet_upkeep(t0 + 10)
        assert w.deployed == 4_500.0, "not on a first sighting, nor inside 30 s"
        await e._wallet_upkeep(t0 + 31)
        assert w.deployed == 4_000.0, "the position's cost, the stray 500 released"
    finally:
        await e.stop()
