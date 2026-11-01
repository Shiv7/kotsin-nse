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


def test_a_near_contract_without_a_settle_does_not_borrow_the_next_months(tmp_path):
    store = OiDailyStore(tmp_path)
    _day(store, date(2026, 10, 19), 1000, 100, 100.0)
    _day(store, date(2026, 10, 20), 1000, 100, 100.0)
    store.write(date(2026, 10, 21), pd.DataFrame([  # the near contract's settle is missing; the next month settled 2 % up
        {"scrip_code": "1", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-10-27", "strike": 0, "option_type": "", "close": float("nan"), "settle": float("nan"), "oi": 1000, "oi_change": 0, "volume": 1},
        {"scrip_code": "2", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-11-24", "strike": 0, "option_type": "", "close": 102.0, "settle": 102.0, "oi": 110, "oi_change": 0, "volume": 1},
    ]))
    _day(store, date(2026, 10, 22), 1000, 100, 101.0)
    _day(store, date(2026, 10, 23), 1000, 100, 102.0)
    t = futures_table(store).set_index("day")
    assert "2026-10-21" not in t.index, "no settle, no price change: not +2 % read off the next month's row"
    assert "2026-10-22" not in t.index, "its previous settle is the missing one"
    assert list(t.index) == ["2026-10-20", "2026-10-23"]
    assert round(t.loc["2026-10-23", "price_chg_pct"], 4) == round((102 / 101 - 1) * 100, 4), "the near contract against its own settle"


def test_a_change_is_read_only_from_the_previous_session_of_the_store(tmp_path):
    store = OiDailyStore(tmp_path)
    _day(store, date(2026, 10, 20), 1000, 0, 100.0)
    _day(store, date(2026, 10, 21), 1100, 0, 101.0)
    store.write(date(2026, 10, 22), pd.DataFrame([  # a session the store holds, with ABC absent from the bhavcopy
        {"scrip_code": "9", "symbol": "XYZ", "kind": "FUTSTK", "expiry": "2026-10-27", "strike": 0, "option_type": "", "close": 50.0, "settle": 50.0, "oi": 10, "oi_change": 0, "volume": 1},
    ]))
    _day(store, date(2026, 10, 23), 1300, 0, 103.0)
    _day(store, date(2026, 10, 26), 1200, 0, 102.0)  # Friday → Monday: consecutive sessions of the store
    t = futures_table(store)
    t = t[t["symbol"] == "ABC"].set_index("day")
    assert list(t.index) == ["2026-10-21", "2026-10-26"], "the 23rd's change would be measured from the 21st: two sessions"
    assert round(t.loc["2026-10-26", "oi_chg_pct"], 2) == round((1200 / 1300 - 1) * 100, 2)
    assert t.loc["2026-10-26", "label"] == "LONG UNWINDING"


def test_the_day_after_expiry_compares_the_new_near_contract_with_its_own_previous_settle(tmp_path):
    store = OiDailyStore(tmp_path)
    store.write(date(2026, 10, 27), pd.DataFrame([
        {"scrip_code": "1", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-10-27", "strike": 0, "option_type": "", "close": 100.0, "settle": 100.0, "oi": 500, "oi_change": 0, "volume": 1},
        {"scrip_code": "2", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-11-24", "strike": 0, "option_type": "", "close": 101.0, "settle": 101.0, "oi": 900, "oi_change": 0, "volume": 1},
    ]))
    store.write(date(2026, 10, 28), pd.DataFrame([  # October expired; November is near now, flat on its own
        {"scrip_code": "2", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-11-24", "strike": 0, "option_type": "", "close": 101.0, "settle": 101.0, "oi": 1500, "oi_change": 0, "volume": 1},
    ]))
    r = futures_table(store).iloc[-1]
    assert r["price_chg_pct"] == 0.0  # not +1 % from switching contracts


def test_the_session_after_an_expiry_is_not_read_as_the_market_unwinding(tmp_path):
    """28 Oct 2026: the October series had expired the day before and a plain total of yesterday's
    OI read 143 of 218 stocks as short covering (total OI −5.6 %). Yesterday's OI is summed over
    the contracts still alive today."""
    store = OiDailyStore(tmp_path)
    _day(store, date(2026, 10, 23), 900, 100, 100.0)
    _day(store, date(2026, 10, 27), 300, 710, 100.0)  # expiry day: 1,000 → 1,010, the roll
    store.write(date(2026, 10, 28), pd.DataFrame([  # the October contract is gone; November added 10
        {"scrip_code": "2", "symbol": "ABC", "kind": "FUTSTK", "expiry": "2026-11-24", "strike": 0, "option_type": "", "close": 100.0, "settle": 100.5, "oi": 720, "oi_change": 10, "volume": 1},
    ]))
    t = futures_table(store).set_index("day")
    assert round(t.loc["2026-10-27", "oi_chg_pct"], 2) == 1.0  # both contracts alive on expiry day: 1,000 → 1,010
    assert round(t.loc["2026-10-28", "oi_chg_pct"], 2) == 1.41  # against the 710 still alive, not the 1,010 total
    assert t.loc["2026-10-28", "label"] == "LONG BUILD-UP"
