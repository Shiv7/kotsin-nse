"""RT-Y's market-breadth gate — the operator's paper A/B (2026-09-25): RT-Y enters only when more
than half of the NSE universe trades beyond its own day open in the trade's direction; RT-X and
RT-N keep taking everything, so the two sides of the gate can be compared on paper.

Replay evidence (Sep 1–25, 358 triggers, RT-Y rule on real option candles): breadth > 0.5 made
+0.72 % a trade against −2.88 % at or below it on the held-out second half."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dtime

import pytest

from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import IN_TREND_BOOKS, Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote, Selection
from kotsin_nse.market.session import IST, ist_today
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey


def _universe(e: Engine, up: int, down: int) -> None:
    """``up`` names trading above their day open, ``down`` below it."""
    open_ts = int(datetime.combine(ist_today(), dtime(9, 15), tzinfo=IST).timestamp())
    for k in range(up + down):
        sym = f"N{k:03d}"
        inst = Instrument(f"9{k:03d}", sym, Segment.NSE_EQ, InstrumentKind.EQUITY, name=sym, underlying=sym)
        e.underlyings[sym] = inst
        last = 105.0 if k < up else 95.0
        e.store.seed(sym, "30m", [
            UnifiedBar(sym, inst.scrip_code, "30m", open_ts, 100.0, 106.0, 94.0, 101.0, 1000.0, source=BarSource.REST, complete=True),
            UnifiedBar(sym, inst.scrip_code, "30m", open_ts + 1800, 101.0, 106.0, 94.0, last, 1000.0, source=BarSource.REST, complete=True),
        ])


OPT = Instrument("153805", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=2750, tick_size=0.01,
                 strike=190.0, option_type=Direction.BULLISH.option_type, underlying="TATASTEEL")
UND = Instrument("3499", "TATASTEEL", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TATASTEEL")


async def _paper(settings) -> Engine:
    e = Engine(settings.model_copy(update={"paper_limit_orders": False}))
    await e.start()
    await e.set_mode(Mode.PAPER)
    e.underlyings["TATASTEEL"] = UND
    return e


def _sig() -> Signal:
    return Signal(strategy=StrategyKey.FUDKII, symbol="TATASTEEL", direction=Direction.BULLISH, ts=int(time.time()) // 1800 * 1800,
                  entry=187.25, stop=186.0, targets=(190.0,))


async def _drive(e: Engine, sig: Signal) -> None:
    async def select(underlying, s, *, tape=True):
        return Selection(OPT, premium=1.5, reason="ok", spread_pct=0.5)

    e._select_instrument = select  # type: ignore[method-assign]
    now = time.time()
    e.books[OPT.scrip_code] = BookSnapshot(OPT.scrip_code, bids=[(1.49, 900_000)], asks=[(1.5, 900_000)], ts=now)
    e.quotes[OPT.scrip_code] = Quote(ltp=1.5, bid=1.49, ask=1.5, ts=now)
    await e._handle_signal(sig, None, books=IN_TREND_BOOKS)


def _books(e: Engine) -> set[str]:
    return {p.strategy for p in e.positions.values() if p.strategy.startswith("FUDKII_RT")}


def test_breadth_is_the_share_beyond_the_day_open_the_trades_way(settings):
    e = Engine(settings)
    _universe(e, up=30, down=10)
    assert e.market_breadth(Direction.BULLISH)["share"] == pytest.approx(0.75)
    assert e.market_breadth(Direction.BEARISH)["share"] == pytest.approx(0.25)
    # a live tick overrides the last 30m close
    e.ltps["9000"] = 90.0
    assert e.market_breadth(Direction.BULLISH)["agree"] == 29


def test_too_few_names_is_unmeasured_not_zero(settings):
    e = Engine(settings)
    _universe(e, up=3, down=10)
    assert e.market_breadth(Direction.BULLISH)["share"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("up", "down", "rt_y"), [(12, 28, False), (20, 20, False), (28, 12, True), (3, 10, True)])
async def test_rt_y_alone_is_gated_on_breadth(settings, up, down, rt_y):
    """30 % agree → RT-Y skips with the reason on the card; exactly 50 % → skips (the replay's rule
    is "more than half"); 70 % → RT-Y enters; unmeasurable → never blocks. RT-X and RT-N always enter."""
    e = await _paper(settings)
    try:
        _universe(e, up, down)
        await _drive(e, _sig())
        books = _books(e)
        assert {"FUDKII_RT_X", "FUDKII_RT_N"} <= books
        assert ("FUDKII_RT_Y" in books) is rt_y
        if not rt_y:
            ev = [x for x in await e.ledger.rows_between("events", 0, time.time() + 5) if x.get("kind") == "rt_twin.skipped"]
            assert ev and ev[-1]["book"] == "FUDKII_RT_Y" and ev[-1]["gate"] == "breadth" and "not with the breakout" in ev[-1]["reason"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_gate_reads_the_breadth_logged_at_the_trigger(settings):
    """The number the trigger was logged with decides, not a later one (the parent fills a few
    seconds after the bar; the market may have moved)."""
    e = await _paper(settings)
    try:
        _universe(e, up=12, down=28)  # 30 % bullish at the trigger
        sig = _sig()
        await e._log_breadth(sig)
        logged = [x for x in await e.ledger.rows_between("events", 0, time.time() + 5) if x.get("kind") == "regime.breadth"]
        assert logged and logged[0]["share"] == pytest.approx(0.3) and logged[0]["names"] == 40, "a name with no bar today is not counted"
        for k in range(12, 40):  # the market turns up before the fill
            e.ltps[f"9{k:03d}"] = 110.0
        await _drive(e, sig)
        assert "FUDKII_RT_Y" not in _books(e) and {"FUDKII_RT_X", "FUDKII_RT_N"} <= _books(e)
    finally:
        await e.stop()


def test_ct_y_does_not_inherit_the_gate():
    from kotsin_nse.risk.limits import CT_Y_LIMITS, RT_N_LIMITS, RT_X_LIMITS, RT_Y_LIMITS

    assert RT_Y_LIMITS.breadth_min == 0.5
    assert CT_Y_LIMITS.breadth_min is None and RT_X_LIMITS.breadth_min is None and RT_N_LIMITS.breadth_min is None


def test_every_trigger_is_labelled_with_the_replays_measures(settings, monkeypatch):
    """Gap, pivots ahead, the 09:45 flag, trend efficiency and the own-volatility band — logged on
    every trigger for the A/B, measured as the Sep replay measured them (parity checked on all 332
    Sep triggers: gap 327/332 same call at 0.3, pivots ahead 331/332, 09:45 flag 332/332)."""
    from datetime import date, timedelta

    from kotsin_nse import engine as engine_mod
    from kotsin_nse.strategy.base import Signal
    from kotsin_nse.strategy.keys import StrategyKey

    day = date(2026, 9, 24)
    monkeypatch.setattr(engine_mod, "ist_today", lambda: day)
    e = Engine(settings)
    sym, code = "HAL", "2303"
    e.underlyings[sym] = Instrument(code, sym, Segment.NSE_EQ, InstrumentKind.EQUITY, name=sym, underlying=sym)
    # 30 flat official sessions around 4,700 (range 60), the last closing at 4,700: daily R1 = 2P - L = 4,730
    dailies = []
    d = day - timedelta(days=45)
    while len(dailies) < 30:
        d += timedelta(days=1)
        if d.weekday() < 5 and d < day:
            ts = int(datetime.combine(d, dtime(9, 15), tzinfo=IST).timestamp())
            dailies.append(UnifiedBar(sym, code, "1d", ts, 4700, 4730, 4670, 4700, 1e6, source=BarSource.REST, complete=True))
    e.store.seed(sym, "1d", dailies)
    open_ts = int(datetime.combine(day, dtime(9, 15), tzinfo=IST).timestamp())
    # today gaps up to 4,730 (+0.5 daily ATR of 60) and the 09:15 bar closes at 4,726, 4 points under R1
    prior = [UnifiedBar(sym, code, "30m", open_ts - 86_400 * 2 + k * 1800, 4690, 4700, 4685, 4695, 1e4,
                        source=BarSource.REST, complete=True) for k in range(20)]  # enough for a 14-bar ATR30
    first = UnifiedBar(sym, code, "30m", open_ts, 4730, 4736, 4720, 4726, 3e4, source=BarSource.REST, complete=True)
    e.store.seed(sym, "30m", [*prior, first])
    c = e.trigger_context(Signal(strategy=StrategyKey.FUDKII, symbol=sym, direction=Direction.BULLISH, ts=open_ts, entry=4726.0, stop=4718.0))
    assert c["openBar"] is True
    assert c["gapDatr"] == pytest.approx(0.5, abs=0.01)
    assert any(x.startswith("1d.R1") for x in c["pivotsAhead"]), c["pivotsAhead"]
    assert 0 < c["efficiency"] <= 1 and "volBand" in c
