"""Bookkeeping: what the ledger and the wallets say must be what happened.

* SBILIFE 2026-09-24: the parent exited 66 ms after entry and its three twins were born CLOSED —
  no exit order, no trade row, committed capital never released (since 2026-09-26 no book is a copy
  of another; the stranded money is released by tests/test_wallet_upkeep.py's upkeep);
* RT-X HEROMOTOCO 2026-09-25: two exit slices (150 @ 50.65, 450 @ 40.20) booked as −₹552 because
  the trade row priced the whole quantity at the last slice; the real gross is +₹1,015.50;
* a restart dropped the trail's peak, its dwell count and the stop-wait clock (the option's own T1 was already kept);
* the RT twins copied the parent's size (TATASTEEL 6 lots) past their own 4-lot cap.
"""

from __future__ import annotations

import time

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    ExitDecision,
    ExitReason,
    Instrument,
    InstrumentKind,
    OptionType,
    Position,
    PosSide,
)
from kotsin_nse.engine import _position_from_json, _position_json, _trade_from
from kotsin_nse.risk.exits import apply_exit

HERO = Instrument("109773", "HEROMOTOCO", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=150, tick_size=0.05,
                  strike=5400.0, option_type=OptionType.CE, underlying="HEROMOTOCO")
HERO_EQ = Instrument("1348", "HEROMOTOCO", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="HEROMOTOCO")


def _pos(**kw) -> Position:
    base = dict(id="p1", strategy="FUDKII_RT_X", instrument=HERO, underlying=HERO_EQ, side=PosSide.LONG, qty=600,
                entry=41.12, opened_ts=time.time() - 400, signal_id="FUDKII-HEROMOTOCO-1-B", direction=Direction.BULLISH,
                equity_entry=5373.0, equity_sl=5344.65, option_sl=27.71, option_targets=(50.26, 67.72), entry_charges=48.0)
    base.update(kw)
    return Position(**base)


def test_a_tranche_exit_is_booked_slice_by_slice_with_both_legs_charges():
    p = _pos()
    apply_exit(p, ExitDecision(p.id, ExitReason.TARGET, 50.65, 150, "T1"), fill_price=50.65, charges=48.64, now=time.time())
    apply_exit(p, ExitDecision(p.id, ExitReason.SL_OP, 40.2, 450, "breakeven"), fill_price=40.2, charges=91.0, now=time.time())
    t = _trade_from(p, time.time())
    assert t.gross == pytest.approx(150 * (50.65 - 41.12) + 450 * (40.2 - 41.12)) == pytest.approx(1015.5)
    assert t.charges == pytest.approx(48.64 + 91.0 + 48.0), "entry charges are part of the round trip"
    assert t.net == pytest.approx(1015.5 - 187.64)


def test_a_restart_keeps_everything_the_exit_engine_was_holding():
    p = _pos(peak_mid=51.4, trail_dwell=2, breach_since=1234.5, option_t1=50.26, realised_gross=1429.5)
    q = _position_from_json(_position_json(p))
    assert (q.peak_mid, q.trail_dwell, q.breach_since, q.option_t1) == (51.4, 2, 1234.5, 50.26)
    assert q.realised_gross == 1429.5 and q.entry_charges == 48.0
