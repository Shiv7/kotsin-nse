"""The trade ledger names each trade's book and the side of the trigger it trades (operator, 2026-10-04:
"add the strategy name and counter-trend/trend, in the columns below in the table/list"), shows MFE and MAE
in money as well as R ("add the actual values of MFE and MAE"), and prints a strike without the decimals it
does not need ("KEI 29 SEP 2026 CE 4700 unless it is ... 15.5")."""

from __future__ import annotations

import httpx
import pytest

from kotsin_nse.api.daybook import contract_label
from kotsin_nse.api.routes import _excursions, build_app
from kotsin_nse.config import Settings
from kotsin_nse.engine import Engine
from kotsin_nse.strategy.keys import ALL_KEYS, COUNTER_TREND, StrategyKey, describe_book


def test_every_book_has_a_name_and_a_side_and_only_the_fades_are_counter_trend():
    for k in ALL_KEYS:
        d = describe_book(k.value)
        assert d["strategy_label"] == k.display_name and d["trend"] in {"trend", "counter-trend"}
    assert {k for k in ALL_KEYS if describe_book(k.value)["trend"] == "counter-trend"} == COUNTER_TREND
    assert COUNTER_TREND == {StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y, StrategyKey.FUDKII_CT_M}
    assert describe_book("FUDKII_RT_Y_F")["trend"] == "trend", "the graded-F shadow trades the trigger's own way"
    assert describe_book("FUDKII_RT") == {"strategy_label": "FUDKII_RT", "trend": ""}, "a retired key: raw, side unknown"


@pytest.mark.asyncio
async def test_rows_stored_before_the_labels_existed_are_served_with_them(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path, db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", engine_enabled=False)
    e = Engine(s)
    await e.ledger.init()
    for i, book in enumerate(("FUDKII_RT_Y", "FUDKII_CT_Y")):
        await e.ledger.insert_trade({"id": f"trd-{i}", "position_id": f"pos-{i}", "strategy": book, "symbol": "X", "underlying": "X",
                                     "closed_ts": 1790000000.0 + i, "net": 0.0, "charges": 0.0, "r_multiple": 0.0, "exit_reason": "EOD"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(e)), base_url="http://t") as c:
        rows = (await c.get("/api/trades")).json()
    assert [(r["strategy"], r["strategy_label"], r["trend"]) for r in rows] == [
        ("FUDKII_CT_Y", "FUDKII-CT-Y", "counter-trend"), ("FUDKII_RT_Y", "FUDKII-RT-Y", "trend")]


def test_mfe_and_mae_in_money_are_the_option_price_and_the_rupees_open_then():
    # KEI 29 SEP CE 4700, FUDKII, 2026-09-23 09:45: entry 37.70, first stop 31.47, 875 units
    kei = {"entry": 37.7, "r_unit": 6.23, "side": "LONG", "qty": 875, "multiplier": 1, "mfe_r": 0.209, "mae_r": -0.064}
    assert _excursions(kei) == {"mark_basis": "last", "mfe_price": 39.0, "mae_price": 37.3, "mfe_inr": pytest.approx(1139.31),
                                "mae_inr": pytest.approx(-348.88)}
    short = {**kei, "side": "SHORT"}
    assert _excursions(short)["mfe_price"] == 36.4, "a short's best price is below its entry"
    none = _excursions({"entry": 10.0, "mfe_r": 0.5})
    assert none.pop("mark_basis") == "last" and set(none.values()) == {None}, "no R unit stored: no figure"
    assert _excursions({**kei, "mark_basis": "bid"})["mark_basis"] == "bid", "a trade marked on the bid says so"


def test_a_strike_keeps_only_the_decimals_it_needs():
    assert contract_label("KEI 29 SEP 2026 CE 4700.00") == "KEI 29 SEP 2026 CE 4700"
    assert contract_label("GAIL 27 OCT 2026 PE 167.50") == "GAIL 27 OCT 2026 PE 167.5"
    assert contract_label("IDEA 27 OCT 2026 CE 15.25") == "IDEA 27 OCT 2026 CE 15.25"
    assert contract_label("CRUDEOIL 19 OCT 2026") == "CRUDEOIL 19 OCT 2026", "a future has no strike"
    assert contract_label("—") == "—"
