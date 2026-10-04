"""The trade ledger names each trade's book and the side of the trigger it trades (operator, 2026-10-04:
"add the strategy name and counter-trend/trend, in the columns below in the table/list")."""

from __future__ import annotations

import httpx
import pytest

from kotsin_nse.api.routes import build_app
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
