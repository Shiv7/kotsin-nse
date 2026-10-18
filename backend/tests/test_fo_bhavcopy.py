"""NSE's F&O bhavcopy: the official closing OI of every contract — the previous close an OI change
is measured from, because 5paisa's own change field is 0.0 on every frame."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date

import pytest

from kotsin_nse.market.fo_bhavcopy import OiDailyStore, backfill, fetch_day, parse, url_for

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
          "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
          "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def _row(tp: str, token: int, sym: str, expiry: str, oi: int, chg: int, close: float, strike: str = "", opt: str = "") -> str:
    return (f"2026-10-01,2026-10-01,FO,NSE,{tp},{token},,{sym},,{expiry},{expiry},{strike},{opt},{sym}X,0,0,0,{close},"
            f"{close},{close},0,{close},{oi},{chg},10,0,0,F1,675,,,,,")


def _bhavcopy(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_0_0_0_20261001_F_0000.csv", "\n".join([HEADER, *rows]) + "\n")
    return buf.getvalue()


JSW = [
    _row("STF", 48900, "JSWSTEEL", "2026-10-27", 39_903_300, -448_200, 1236.6),
    _row("STF", 61619, "JSWSTEEL", "2026-11-23", 570_375, 6_075, 1245.0),
    _row("STO", 36059, "JSWSTEEL", "2026-10-27", 0, 0, 39.3, "890.00", "CE"),
]


# -- the exchange's file ------------------------------------------------------------------------------


def test_the_bhavcopy_parses_to_one_row_per_contract_keyed_by_the_token_5paisa_uses():
    df = parse(_bhavcopy(JSW))
    assert list(df.scrip_code) == ["48900", "61619", "36059"]
    fut = df[df.scrip_code == "48900"].iloc[0]
    assert (fut.kind, fut.expiry, fut.oi, fut.oi_change) == ("FUTSTK", "2026-10-27", 39_903_300.0, -448_200.0)
    assert df[df.scrip_code == "36059"].iloc[0].option_type == "CE"
    with pytest.raises(ValueError):
        parse(b"<html>challenge</html>")


def test_the_store_answers_the_official_close_and_never_a_contract_nobody_holds(tmp_path):
    store = OiDailyStore(tmp_path / "oi_daily")
    d = date(2026, 10, 1)
    store.write(d, parse(_bhavcopy(JSW)))
    assert store.has(d) and store.days() == [d]
    assert store.closes(d, {"48900", "61619", "36059", "99999"}) == {"48900": 39_903_300.0, "61619": 570_375.0}
    assert store.closes(date(2026, 9, 30), {"48900"}) == {}


class _Resp:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code, self.content = status, content


class _Http:
    """Answers the bhavcopy of the days it holds; 404 for the rest."""

    def __init__(self, files: dict[date, bytes]) -> None:
        self.files = {url_for(d): b for d, b in files.items()}
        self.asked: list[str] = []

    async def get(self, url: str, **_: object) -> _Resp:
        self.asked.append(url)
        return _Resp(200, self.files[url]) if url in self.files else _Resp(404)


def test_a_day_nse_has_no_file_for_is_none_and_the_backfill_skips_weekends_and_held_days(tmp_path):
    store = OiDailyStore(tmp_path / "oi_daily")
    http = _Http({date(2026, 10, 1): _bhavcopy(JSW)})
    assert asyncio.run(fetch_day(http, date(2026, 10, 2))) is None, "a holiday has no file"
    out = asyncio.run(backfill(http, store, date(2026, 9, 30), date(2026, 10, 4), pace_s=0))
    assert out == {"fetched": 1, "held": 0, "missing": 2}, "30 Sep missing, 1 Oct fetched, 2 Oct none, 3-4 Oct a weekend"
    asked = len(http.asked)
    again = asyncio.run(backfill(http, store, date(2026, 10, 1), date(2026, 10, 1), pace_s=0))
    assert again == {"fetched": 0, "held": 1, "missing": 0} and len(http.asked) == asked, "a held day is not asked again"


# -- our own OI candles ---------------------------------------------------------------------------

