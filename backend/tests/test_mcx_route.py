"""Commodity triggers reach a book. Until 2026-09-25 none ever did: FUDKII's purse is NSE-only, so
every MCX trigger was booked WRONG_SEGMENT, and RT-MCX only mirrored FUDKII *fills* — which never
came. The trigger now goes to RT-MCX directly, with the trigger as its source."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.instrument.select import Quote, Selection
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey

#: GOLDPETAL — one of the four commodity triggers of 2026-09-25 (13:30 SILVER100, 14:00 GOLDTEN,
#: GOLDPETAL, GOLDGUINEA), all booked WRONG_SEGMENT and entered by nobody
CRUDE = Instrument("454818", "GOLDPETAL", Segment.MCX_FO, InstrumentKind.FUTURE, name="GOLDPETAL 30 OCT 2026", lot_size=1,
                   tick_size=1.0, multiplier=1, expiry="2099-12-31", underlying="GOLDPETAL")


def _trigger() -> Signal:
    ts = int(datetime.combine(ist_today(), dtime(16, 30), tzinfo=IST).timestamp())
    return Signal(strategy=StrategyKey.FUDKII, symbol="GOLDPETAL", direction=Direction.BULLISH, ts=ts, entry=11000.0,
                  stop=10950.0, targets=(11080.0, 11150.0), grade="A", rr=2.0, reason="ST flip UP + close above upper band")


async def _engine(settings, sel: Selection) -> tuple[Engine, list]:
    e = Engine(settings)
    await e.start()
    await e.set_mode(Mode.PAPER)  # SHADOW places nothing
    e.underlyings["GOLDPETAL"] = CRUDE
    adopted: list = []
    e.alerts.adopt_signal = lambda sig, bar=None: adopted.append(sig["signal_id"])  # type: ignore[method-assign]

    async def select(underlying, sig, *, tape=True):
        return sel

    e._select_instrument = select  # type: ignore[method-assign]
    return e, adopted


@pytest.mark.asyncio
async def test_a_commodity_trigger_is_entered_by_rt_mcx_with_the_trigger_as_its_source(settings):
    now = time.time()
    e, adopted = await _engine(settings, Selection(CRUDE, premium=11000.0, reason="ok", spread_pct=0.0002))
    try:
        e.quotes[CRUDE.scrip_code] = Quote(ltp=11000.0, bid=10999.0, ask=11001.0, ts=now)
        e.ltps[CRUDE.scrip_code] = 11000.0
        trig = _trigger()
        await e._handle_signal(trig, None)
        rows = {r["strategy"]: r for r in await e.ledger.rows_between("signals", 0, trig.ts + 86_400)}  # the trigger is at 16:30 today, which is ahead of the clock before 16:30
        assert rows["FUDKII"]["decision"] == "ROUTED", "no longer WRONG_SEGMENT"
        child = rows["FUDKII_RT_MCX"]
        assert child["source_signal_id"] == trig.signal_id
        assert adopted == [trig.signal_id], "one ENTRY card — the trigger's — not two"
        held = [p for p in e.positions.values() if p.strategy == "FUDKII_RT_MCX"]
        assert child["decision"].endswith("FILLED"), child
        assert len(held) == 1 and held[0].underlying.symbol == "GOLDPETAL"
        # the card for the RT-MCX book finds the position through the trigger
        c = next(x for x in (await e.book_cards("FUDKII_RT_MCX", ist_today()))["cards"] if x["symbol"] == "GOLDPETAL")
        assert c["state"] == "OPEN" and c["position"]["id"] == held[0].id
        assert [(b["book"], b["status"], b["side"]) for b in c["books"]] == [("FUDKII_RT_MCX", "OPEN", "LONG")], "only the commodity book, glowing"
        assert not [p for p in e.positions.values() if p.strategy.startswith("FUDKII_RT_") and p.strategy != "FUDKII_RT_MCX"], "no NSE twin"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_commodity_trigger_with_no_contract_says_so_on_the_rt_mcx_card(settings):
    e, _ = await _engine(settings, Selection(None, reason="no fresh quote for the front future"))
    try:
        trig = _trigger()
        await e._handle_signal(trig, None)
        c = next(x for x in (await e.book_cards("FUDKII_RT_MCX", ist_today()))["cards"] if x["symbol"] == "GOLDPETAL")
        assert c["state"] == "NO_INSTRUMENT", "the routed entry's own decision, not NO FILL TO MIRROR"
        assert c["cta"]["enabled"] is False and "front future" in c["cta"]["reason"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_nse_triggers_are_not_routed(settings):
    e, _ = await _engine(settings, Selection(None, reason="no tradeable strike"))
    try:
        e.underlyings["RELIANCE"] = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")
        sig = Signal(strategy=StrategyKey.FUDKII, symbol="RELIANCE", direction=Direction.BULLISH, ts=int(time.time()), entry=1500.0, stop=1490.0)
        await e._handle_signal(sig, None)
        rows = await e.ledger.rows_between("signals", 0, time.time() + 60)
        assert [(r["strategy"], r["decision"]) for r in rows] == [("FUDKII", "NO_INSTRUMENT")]
    finally:
        await e.stop()
