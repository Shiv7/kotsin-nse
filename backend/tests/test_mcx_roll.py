"""The MCX roll (operator, 2026-09-30): a future 5 calendar days or fewer from expiry is not the one
read or traded — the next month is. On 29 Sep ALUMINIUM's expiring contract traded 53 lots against
1,438 in the next month, and its "breakout" came while the traded month fell. The chart, the entry
and the levels all move together; NSE is untouched."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.instrument.catalogue import Catalogue
from kotsin_nse.instrument.select import Quote
from kotsin_nse.instrument.universe import UniverseBuilder
from kotsin_nse.market.session import ist_today
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey

TODAY = ist_today()


def _fut(code: str, root: str, days: int, seg: Segment = Segment.MCX_FO) -> Instrument:
    return Instrument(code, root, seg, InstrumentKind.FUTURE, name=f"{root} +{days}d", underlying=root, lot_size=5,
                      expiry=(TODAY + timedelta(days=days)).isoformat())


def _catalogue() -> Catalogue:
    cat = Catalogue()
    for f in (_fut("A0", "ALUMINIUM", 0), _fut("A30", "ALUMINIUM", 30), _fut("A61", "ALUMINIUM", 61),
              _fut("G5", "GOLD", 5), _fut("G65", "GOLD", 65),
              _fut("C19", "CRUDEOIL", 19), _fut("C50", "CRUDEOIL", 50),
              _fut("S2", "SOLO", 2),
              _fut("N0", "RELIANCE", 0, Segment.NSE_FO), _fut("N28", "RELIANCE", 28, Segment.NSE_FO)):
        cat.by_code[f.scrip_code] = f
        cat.futures_by_symbol[f.underlying].append(f)
    return cat


def test_a_commodity_in_its_last_five_days_is_read_on_the_next_month_and_nse_is_untouched():
    b = UniverseBuilder(_catalogue())
    mcx = b.build_underlyings([Segment.MCX_FO], TODAY)
    assert [f.scrip_code for f in mcx["ALUMINIUM"].futures] == ["A30", "A61"] and mcx["ALUMINIUM"].underlying.scrip_code == "A30"
    assert [f.scrip_code for f in mcx["GOLD"].futures] == ["G65"], "5 days left is inside the roll"
    assert [f.scrip_code for f in mcx["CRUDEOIL"].futures] == ["C19", "C50"], "19 days left: no roll"
    assert [f.scrip_code for f in mcx["SOLO"].futures] == ["S2"], "never left without a contract"
    nse = b.build_underlyings([Segment.NSE_FO], TODAY)
    assert [f.scrip_code for f in nse["RELIANCE"].futures] == ["N0", "N28"], "NSE is not rolled here"


@pytest.mark.asyncio
async def test_an_mcx_entry_buys_the_contract_the_chart_reads_not_the_nearest(settings):
    e = Engine(settings)
    cat = _catalogue()
    e.catalogue_loader.catalogue = cat
    rolled = cat.by_code["A30"]
    now = time.time()
    e.quotes["A0"] = Quote(ltp=353.0, bid=352.0, ask=354.0, ts=now)
    e.quotes["A30"] = Quote(ltp=343.4, bid=343.35, ask=343.45, ts=now)
    sig = Signal(strategy=StrategyKey.FUDKII, symbol="ALUMINIUM", direction=Direction.BULLISH, ts=int(now) // 1800 * 1800,
                 entry=343.4, stop=342.0)
    sel = await e._select_instrument(rolled, sig, tape=False)
    assert sel.instrument is rolled and sel.premium == pytest.approx(343.4), "the October contract, at its own price"


def test_a_rolled_commodity_never_takes_the_expiring_months_cached_candles(settings):
    e = Engine(settings)
    cat = _catalogue()
    e.catalogue_loader.catalogue = cat
    day = UnifiedBar("X", "X", "1d", 1_790_000_000, 350.0, 355.0, 348.0, 353.0, 100.0, source=BarSource.REST, complete=True)
    for sym in ("ALUMINIUM", "CRUDEOIL"):
        e.daily_cache.save(sym, [day])
    e._seed_daily_from_cache([cat.by_code["A30"], cat.by_code["C19"]])
    assert e.store.bars("ALUMINIUM", "1d") == [], "rolled: the cache is the expiring month's — no levels rather than wrong ones"
    assert len(e.store.bars("CRUDEOIL", "1d")) == 1, "not rolled: the cache stands until REST answers"
