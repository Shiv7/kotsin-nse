"""The F&O universe as it was: a stock is tradable as a future only on days NSE listed its stock
futures, a renamed stock keeps its history, and the swing backtest cannot pick a stock before it
qualified."""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta

import pandas as pd
import pytest

from kotsin_nse.config import Settings
from kotsin_nse.market.fo_bhavcopy import OiDailyStore, legacy_url_for, parse_legacy
from kotsin_nse.market.fo_universe import (
    SYMBOL_CHANGES_FILE,
    FoMembership,
    canonical,
    load,
    parse_symbol_changes,
)
from kotsin_nse.research.swing import FUNNEL, SwingParams, trades_for
from kotsin_nse.risk.costs import CostModel
from tests.test_swing import frame, series

D = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 8), date(2024, 3, 11)]  # 7 Mar a holiday


def test_a_stock_is_a_member_only_from_the_session_its_futures_appear_until_they_go():
    fm = FoMembership.from_sessions({D[0]: {"ABB"}, D[1]: {"ABB"}, D[2]: {"ABB", "HINDZINC"}, D[3]: {"HINDZINC"}, D[4]: {"HINDZINC"}})
    assert not fm.member("HINDZINC", D[1]) and fm.member("HINDZINC", D[2])
    assert fm.member("HINDZINC", date(2024, 3, 7)), "a holiday reads the session before it"
    assert fm.member("ABB", D[2]) and not fm.member("ABB", D[3])
    assert fm.intervals("ABB") == [(D[0], D[2])] and fm.intervals("HINDZINC") == [(D[2], D[4])]


def test_a_day_the_files_do_not_cover_is_an_error_not_a_no():
    fm = FoMembership.from_sessions({D[0]: {"ABB"}})
    with pytest.raises(ValueError):
        fm.member("ABB", D[0] - timedelta(days=1))
    with pytest.raises(ValueError):
        fm.member("ABB", D[0] + timedelta(days=30))


def test_a_renamed_stock_is_the_same_stock_under_todays_ticker():
    text = ("NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
            "United Spirits,MCDOWELL-N,UNITDSPR,17-JUL-2024\n Old Co,A1,B1,01-JAN-2020\n Old Co,B1,C1,01-JAN-2022\n")
    ch = parse_symbol_changes(text)
    assert canonical("MCDOWELL-N", ch) == "UNITDSPR" and canonical("A1", ch) == "C1" and canonical("ABB", ch) == "ABB"
    fm = FoMembership.from_sessions({D[0]: {"MCDOWELL-N"}, D[1]: {"UNITDSPR"}}, ch)
    assert fm.member("UNITDSPR", D[0]) and fm.member("UNITDSPR", D[1])
    assert fm.member("MCDOWELL-N", D[1]), "an old ticker asked about is mapped forward too"


def _legacy_zip(rows: list[str]) -> bytes:
    head = "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fo04JAN2021bhav.csv", "\n".join([head, *rows]) + "\n")
    return buf.getvalue()


def test_the_older_file_reads_into_the_same_columns_without_a_token():
    assert legacy_url_for(date(2021, 1, 4)).endswith("/2021/JAN/fo04JAN2021bhav.csv.zip")
    df = parse_legacy(_legacy_zip([
        "FUTSTK,ABB,28-Jan-2021,0,XX,1,1,1,1384.5,1384.5,120,10,1504000,-8000,04-JAN-2021,",
        "OPTSTK,ABB,28-Jan-2021,1400,CE,1,1,1,22.1,22.1,10,1,32000,4000,04-JAN-2021,",
    ]))
    fut, opt = df.iloc[0], df.iloc[1]
    assert fut.kind == "FUTSTK" and fut.expiry == "2021-01-28" and fut.option_type == "" and fut.scrip_code == ""
    assert fut.oi == 1_504_000 and fut.oi_change == -8_000 and fut.volume == 120
    assert opt.kind == "OPTSTK" and opt.option_type == "CE" and opt.strike == 1400


def test_load_reads_both_stores_and_the_rename_list(tmp_path):
    def day(kind_sym: list[tuple[str, str]]) -> pd.DataFrame:
        return pd.DataFrame([{"scrip_code": "", "symbol": s, "kind": k, "expiry": "", "strike": 0.0, "option_type": "", "close": 1.0,
                              "settle": 1.0, "oi": 1.0, "oi_change": 0.0, "volume": 1.0} for k, s in kind_sym])
    OiDailyStore(tmp_path / "oi_daily_legacy").write(date(2023, 12, 29), day([("FUTSTK", "MCDOWELL-N"), ("FUTIDX", "NIFTY")]))
    OiDailyStore(tmp_path / "oi_daily").write(date(2024, 1, 1), day([("FUTSTK", "UNITDSPR"), ("OPTSTK", "IRFC")]))
    (tmp_path / SYMBOL_CHANGES_FILE).write_text("NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\nX,MCDOWELL-N,UNITDSPR,17-JUL-2024\n")
    fm, renames = load(tmp_path)
    assert fm.first == date(2023, 12, 29) and fm.last == date(2024, 1, 1) and renames == 1
    assert fm.member("UNITDSPR", date(2023, 12, 29)) and not fm.member("NIFTY", date(2023, 12, 29))
    assert not fm.member("IRFC", date(2024, 1, 1)), "options alone are not stock futures"


def test_the_swing_backtest_cannot_pick_a_stock_before_it_qualifies():
    d1 = frame(series())
    p = SwingParams(use_bias=False, sides="long", room_r=0.0, max_stop_adr=50.0, fo_members=True)
    costs = CostModel(Settings(_env_file=None))
    start, end = date(2025, 1, 1), date(2026, 12, 31)
    assert trades_for("X", d1, None, p, start, end, costs, None, member=lambda d: True), "the hand-built setup trades"
    FUNNEL.clear()
    assert trades_for("X", d1, None, p, start, end, costs, None, member=lambda d: d >= date(2026, 1, 1)) == []
    assert FUNNEL.get("3cx no stock futures on the decision day", 0) >= 1
