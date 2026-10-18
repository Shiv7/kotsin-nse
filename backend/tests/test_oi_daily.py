"""Daily futures OI per stock from NSE's bhavcopy: the roll is not a collapse, and a trade reads only
sessions before its own."""

from __future__ import annotations

from datetime import date

import pandas as pd

from kotsin_nse.market.fo_bhavcopy import OiDailyStore
from kotsin_nse.research.mtf import oi_alignment
from kotsin_nse.research.oi_daily import futures_table, label, previous_state


def _day(store: OiDailyStore, d: date, near_oi: float, next_oi: float, settle: float) -> None:
    store.write(d, pd.DataFrame([
        {"scrip_code": "1", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-10-27", "strike": 0, "option_type": "", "close": settle, "settle": settle, "oi": near_oi, "oi_change": 0, "volume": 1},
        {"scrip_code": "2", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-11-24", "strike": 0, "option_type": "", "close": settle, "settle": settle, "oi": next_oi, "oi_change": 0, "volume": 1},
    ]))


def test_labels_are_the_four_quadrants():
    assert label(1, 1) == "LONG BUILD-UP" and label(-1, 1) == "SHORT BUILD-UP"
    assert label(1, -1) == "SHORT COVERING" and label(-1, -1) == "LONG UNWINDING"


def test_the_roll_from_near_to_next_is_not_read_as_unwinding(tmp_path):
    store = OiDailyStore(tmp_path)
    _day(store, date(2026, 10, 20), 1000, 100, 100.0)
    _day(store, date(2026, 10, 21), 400, 750, 101.0)  # 1,100 → 1,150 in total: the OI moved, it did not leave
    t = futures_table(store)
    r = t.iloc[-1]
    assert r["label"] == "LONG BUILD-UP" and round(r["oi_chg_pct"], 2) == 4.55


def test_a_trade_reads_only_the_sessions_before_its_day(tmp_path):
    store = OiDailyStore(tmp_path)
    _day(store, date(2026, 10, 20), 1000, 0, 100.0)
    _day(store, date(2026, 10, 21), 1100, 0, 99.0)
    _day(store, date(2026, 10, 22), 900, 0, 101.0)
    t = futures_table(store)
    assert previous_state(t, "ABC", date(2026, 10, 22))[0] == "SHORT BUILD-UP"
    assert previous_state(t, "ABC", date(2026, 10, 23))[0] == "SHORT COVERING"


def test_alignment_with_the_trade():
    assert oi_alignment("BULLISH", "SHORT COVERING") == "WITH"
    assert oi_alignment("BEARISH", "SHORT COVERING") == "AGAINST"
    assert oi_alignment("BEARISH", "SHORT BUILD-UP") == "WITH"
    assert oi_alignment("BULLISH", "") == ""
