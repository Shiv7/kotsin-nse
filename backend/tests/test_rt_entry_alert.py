"""The FUDKII_RT ENTRY row: published in the same call that places the parent's order, and
stamped with the twin's actual fill once it happens."""

import time
from datetime import timedelta

import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import ist_today


def _bars(sym: str, code: str, n: int = 40) -> list[UnifiedBar]:
    base = time.time() - n * 1800
    return [UnifiedBar(symbol=sym, scrip_code=code, tf="30m", ts=base + i * 1800, open=100, high=101,
                       low=99, close=100 + (i % 3), volume=1000, source=BarSource.REST, complete=True)
            for i in range(n)]


@pytest.mark.asyncio
async def test_entry_row_is_published_at_adoption_and_carries_the_fill_afterwards(settings, equity):
    e = Engine(settings)
    await e.start()
    try:
        e.underlyings[equity.symbol] = equity
        bars = _bars(equity.symbol, equity.scrip_code)
        e.store.seed(equity.symbol, "30m", bars)
        sig = {"signal_id": "FUDKII-RELIANCE-1-A", "symbol": equity.symbol, "scrip_code": equity.scrip_code,
               "direction": "BULLISH", "entry": 100.0, "stop": 98.0, "targets": [110.0], "grade": "A"}
        before = time.time()
        e.alerts.adopt_signal(sig, bars[-1])
        rows = e.alerts.feed("FUDKII_RT", 10)
        assert [r["kind"] for r in rows] == ["ENTRY"]
        row = rows[0]
        assert before <= row["firedAt"] <= time.time(), "stamped at the adoption instant, not a later bar"
        assert row["cta"]["action"] == "ENTER" and row["plan"]["entry"] == 100.0

        fill_ts = time.time() + 0.8
        e.alerts.mark_entered(sig["signal_id"], ts=fill_ts, price=7.48, qty=2250)
        row = e.alerts.feed("FUDKII_RT", 10)[0]
        entered = row["card"]["entered"]
        assert entered["price"] == 7.48 and entered["qty"] == 2250 and entered["ts"] == fill_ts
        assert 0 < entered["lagFromFiredS"] < 5 and len(entered["ist"]) == 12
        assert ist_today() - timedelta(days=1) < ist_today()  # sanity: helper importable
    finally:
        await e.stop()
