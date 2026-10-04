"""The RT books on a FUDKII trigger: each takes it on its OWN order (operator, 2026-09-26).

Before 2026-09-26 an RT book was a copy of the parent's fill. That had never had a test, and on
2026-09-23 it crashed on every parent fill all morning — ``'ExposureVerdict' object has no attribute
'ok'`` — so the RT books traded nothing while the parent took two trades. Since the operator's
"each variant and twin assess it at the same time in parallel", every in-trend book decides and
enters for itself; these are the tests of that.
"""

import time

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import (
    Direction,
    Instrument,
    InstrumentKind,
    OptionType,
    OrderSide,
    Position,
    PosSide,
)
from kotsin_nse.engine import IN_TREND_BOOKS, Engine
from kotsin_nse.exec.gateway import Mode
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote, Selection, estimate_delta, map_levels_to_option
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey
from tests._books import held

RELIANCE_OPT = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION,
                          lot_size=250, strike=1500.0, option_type=OptionType.CE, underlying="RELIANCE")
RELIANCE = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="RELIANCE")


async def _paper(settings) -> Engine:
    e = Engine(settings.model_copy(update={"paper_limit_orders": False}))
    await e.start()
    await e.set_mode(Mode.PAPER)
    return e


async def _trigger(e: Engine, opt: Instrument, und: Instrument, *, premium: float = 50.0, entry: float = 1500.0,
                   stop: float = 1490.0, targets: tuple[float, ...] = (1550.0,), shift: int = 0,
                   strategy: StrategyKey = StrategyKey.FUDKII, books=IN_TREND_BOOKS) -> Signal:
    """A trigger on ``und`` into ``books``, its contract ``opt`` quoted at ``premium`` with depth."""
    e.underlyings[und.symbol] = und

    async def select(underlying, sig, *, tape=True):
        return Selection(opt, premium=premium, reason="ok", spread_pct=0.5)

    e._select_instrument = select  # type: ignore[method-assign]
    now = time.time()
    e.books[opt.scrip_code] = BookSnapshot(opt.scrip_code, bids=[(premium - 0.05, 500_000)], asks=[(premium, 500_000)], ts=now)
    e.quotes[opt.scrip_code] = Quote(ltp=premium, bid=premium - 0.05, ask=premium, ts=now)
    e.ltps[opt.scrip_code] = premium
    sig = Signal(strategy=strategy, symbol=und.symbol, direction=Direction.BULLISH, ts=int(now // 1800 * 1800) - 1800 + shift,
                 entry=entry, stop=stop, targets=targets, grade="A", rr=2.0, reason="ST flip UP + close above upper band")
    await e._handle_signal(sig, None, books=books)
    return sig


@pytest.mark.asyncio
async def test_every_in_trend_book_enters_a_fudkii_trigger_on_its_own_order(settings):
    e = await _paper(settings)
    try:
        before = {k: e.wallets[k].available for k in ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_RT_Y_W1")}
        sig = await _trigger(e, RELIANCE_OPT, RELIANCE)
        by = {p.strategy: p for p in held(e)}
        assert set(by) == {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_RT_Y_W1"}, "every in-trend book, and RT-Y's wide-stop shadow"
        for book, p in by.items():
            assert (p.instrument, p.entry, p.signal_id) == (RELIANCE_OPT, 50.0, sig.signal_id)
            # the size is each book's own (operator: "the 4 lots cap is for each strategy individually")
            assert p.qty == 4 * 250
            own = e.costs.leg(RELIANCE_OPT, OrderSide.BUY, 50.0, 1000).total
            assert e.wallets[book].available == pytest.approx(before[book] - 50.0 * 1000 - own), "each book pays its own purse"
        orders = {o["strategy"] for o in await e.ledger.rows_between("orders", 0, time.time() + 60) if o["purpose"] == "ENTRY"}
        assert orders == {"FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y"}, "an order per book (the shadow rides RT-Y's)"
        assert e.wallets["FUDKII_RT_MCX"].available == e.wallets["FUDKII_RT_MCX"].balance, "NSE never touches the MCX purse"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_commodity_trigger_goes_to_the_mcx_book_alone_and_fukaa_trades_only_for_itself(settings):
    e = await _paper(settings)
    try:
        fut = Instrument("482", "CRUDEOIL", Segment.MCX_FO, InstrumentKind.FUTURE, lot_size=100,
                         multiplier=100, expiry="2099-12-31", underlying="CRUDEOIL")
        await _trigger(e, fut, fut, premium=5.0, entry=5.0, stop=4.9, targets=(5.3,))
        assert [p.strategy for p in held(e)] == ["FUDKII_RT_MCX"]

        await _trigger(e, RELIANCE_OPT, RELIANCE, strategy=StrategyKey.FUKAA, books=None)
        assert sorted(p.strategy for p in held(e)) == ["FUDKII_RT_MCX", "FUKAA"], "a FUKAA signal is FUKAA's alone"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_each_rt_book_carries_the_options_own_classic_ladder_and_the_parent_its_projection(settings):
    from kotsin_nse.bars.pivots import classic_pivots
    from kotsin_nse.instrument.legs import LegPivots

    e = await _paper(settings)
    try:
        lv = classic_pivots(60.0, 40.0, 50.0)  # own R1 60, R2 70, R3 90, R4 110
        e.leg_pivots.by_code[RELIANCE_OPT.scrip_code] = LegPivots(
            scrip_code=RELIANCE_OPT.scrip_code, symbol="RELIANCE 29 SEP 2026 CE 1500", root="RELIANCE", kind="CE",
            strike=1500.0, levels=lv, session="2026-09-22", close=50.0, volume=10_000,
        )
        await _trigger(e, RELIANCE_OPT, RELIANCE)
        by = {p.strategy: p for p in e.positions.values()}
        assert by["FUDKII_RT_X"].option_t1 == 60.0 and by["FUDKII_RT_X"].option_targets == (60.0, 70.0, 90.0, 110.0)
        assert by["FUDKII_RT_X"].targets_hit == 0 and "own mtf ladder" in by["FUDKII_RT_X"].note
        assert by["FUDKII_RT_N"].option_targets == (60.0, 70.0, 90.0, 110.0) and "own daily_r ladder" in by["FUDKII_RT_N"].note
        assert by["FUDKII_RT_Y"].option_targets == (60.0, 70.0, 90.0, 110.0), "no IV history → no expected move → every rung above entry"
        d = estimate_delta(spot=1500.0, strike=1500.0, option_type=OptionType.CE)
        _, projected = map_levels_to_option(equity_entry=1500.0, equity_stop=1490.0, equity_targets=(1550.0,), option_premium=50.0, delta=d)
        assert by["FUDKII"].option_targets == projected, "the parent keeps its own delta-projected ladder"

        # a contract with no ladder: equity trigger only
        e.leg_pivots.by_code.clear()
        opt2 = Instrument("55555", "TCS", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=175,
                          strike=3000.0, option_type=OptionType.CE, underlying="TCS")
        und2 = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TCS")
        sig2 = await _trigger(e, opt2, und2, entry=3000.0, stop=2950.0, targets=(3100.0,))
        twin2 = next(p for p in e.positions.values() if p.strategy == "FUDKII_RT_X" and p.signal_id == sig2.signal_id)
        assert twin2.option_t1 == 0.0 and twin2.option_targets == () and "equity trigger only" in twin2.note
    finally:
        await e.stop()


def test_a_twin_and_its_parent_never_share_an_exit_order_id():
    """2026-09-23 14:35: the DIXON parent's SL-EQ exit and its twin's carried the same
    client_order_id (both keyed on the shared signal id); the twin's was refused as a duplicate
    every second while the underlying sat through the stop."""
    from dataclasses import replace

    from kotsin_nse.domain import ExitDecision, ExitReason
    from kotsin_nse.engine import exit_client_order_id

    opt = Instrument("45678", "DIXON", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=50, strike=14000.0,
                     option_type=OptionType.CE, underlying="DIXON")
    parent = Position(id="pos-parent", strategy="FUDKII", instrument=opt, underlying=opt, side=PosSide.LONG,
                      qty=350, entry=19.58, opened_ts=1.0, signal_id="FUDKII-DIXON-1-A", direction=Direction.BULLISH)
    twin = replace(parent, id="pos-twin", strategy="FUDKII_RT_X")
    d = ExitDecision(position_id="x", reason=ExitReason.SL_EQ, ref_price=17.5, qty=350)
    assert exit_client_order_id(parent, d) != exit_client_order_id(twin, d)
    assert exit_client_order_id(twin, d) == exit_client_order_id(twin, d), "a retry is the same order"


@pytest.mark.asyncio
async def test_a_restored_open_position_is_subscribed_even_when_off_the_shortlist(settings):
    """2026-09-23: the DIXON 14000 CE twin came back from a restart six strikes off the shortlist,
    with no quote, no evaluation and no exit — silently."""
    from types import SimpleNamespace

    e = Engine(settings)
    calls: list[tuple[str, list[str]]] = []

    async def recorder(channel, instruments):
        calls.append((channel, [i.scrip_code for i in instruments]))

    e.feed = SimpleNamespace(subscribe=recorder)
    far = Instrument("100512", "DIXON", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=50, strike=14000.0,
                     option_type=OptionType.CE, underlying="DIXON")
    und = Instrument("1", "DIXON", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="DIXON")
    e.positions["open"] = Position(id="open", strategy="FUDKII_RT_X", instrument=far, underlying=und, side=PosSide.LONG,
                                   qty=350, entry=19.58, opened_ts=1.0, signal_id="s", direction=Direction.BULLISH)
    e.positions["closed"] = Position(id="closed", strategy="FUDKII", instrument=far, underlying=und, side=PosSide.LONG,
                                     qty=350, entry=19.58, opened_ts=1.0, signal_id="s", direction=Direction.BULLISH,
                                     status="CLOSED", qty_remaining=0)
    got = await e._subscribe_open_positions()
    assert [i.scrip_code for i in got] == ["100512"]
    assert calls == [("mf", ["100512"]), ("md", ["100512"]), ("oi", ["100512"])]
    e.positions.clear()
    assert await e._subscribe_open_positions() == []


# -- the dried-volume entry gate (RT-X and RT-Y only) -------------------------------------------


def _slots30(n: int, prev_last: str = "14:45") -> list[float]:
    """``n`` 30m bucket starts ending at 09:15 IST today, the ones before it on the previous
    session's grid — a stock's continuous session ends 14:45, a future's 15:15. A volume reading
    reads its eight bars by slot, so the bars must sit where a real session puts them."""
    from datetime import datetime, timedelta
    from datetime import time as dtime

    from kotsin_nse.market.session import IST, ist_today

    t = ist_today()
    prev = t - timedelta(days=1)
    while prev.weekday() >= 5:  # the test settings hold no holiday list: weekdays trade
        prev -= timedelta(days=1)
    end = datetime(t.year, t.month, t.day, 9, 15, tzinfo=IST).timestamp()
    last = datetime.combine(prev, dtime.fromisoformat(prev_last), tzinfo=IST).timestamp()
    return [end if back == 0 else last - (back - 1) * 1800 for back in range(n - 1, -1, -1)]


def _bars30(symbol: str, code: str, vols: list[float]) -> list:
    """30m bars ending at 09:15 IST today, in the store's own bucket-start convention."""
    from kotsin_nse.bars.unified import BarSource, UnifiedBar

    return [UnifiedBar(symbol=symbol, scrip_code=code, tf="30m", ts=ts, open=100.0, high=101.0,
                       low=99.0, close=100.0, volume=v, source=BarSource.REST, complete=True)
            for ts, v in zip(_slots30(len(vols)), vols, strict=True)]


async def _skips(e: Engine) -> dict[str, dict]:
    return {x["book"]: x for x in await e.ledger.rows_between("events", 0, time.time() + 60) if x.get("kind") == "rt_twin.skipped"}


@pytest.mark.asyncio
async def test_dried_equity_volume_skips_rt_x_and_rt_y_but_not_the_control_book(settings, midday):
    e = await _paper(settings)
    try:
        # baseline T-2..T-7 = 10,000; the trigger bar 4,000 (0.40) and the one before 5,000 (0.50)
        e.store.seed("RELIANCE", "30m", _bars30("RELIANCE", "2885", [10_000.0] * 6 + [5_000.0, 4_000.0]))
        await _trigger(e, RELIANCE_OPT, RELIANCE)
        assert sorted(p.strategy for p in held(e)) == ["FUDKII", "FUDKII_RT_N"], "the parent and the control book take it"
        sk = await _skips(e)
        assert set(sk) == {"FUDKII_RT_X", "FUDKII_RT_Y"} and all(x["gate"] == "dried_volume" and "equity 0.40/0.50" in x["reason"] for x in sk.values())
        assert e.wallets["FUDKII_RT_X"].available == e.wallets["FUDKII_RT_X"].balance
        card = (await e.book_cards("FUDKII_RT_X"))["cards"][0]
        assert card["state"] == "SKIPPED" and "dried volume" in card["skip"]["reason"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_dry_front_future_skips_even_when_the_equity_bar_is_live(settings):
    """SBILIFE 2026-09-23 09:45: equity surge 1.48 / 2.51 but the SEP future 0.79 / 0.51 closing on
    its daily S1 — the future is read too, from the broker's candles, up to the trigger bar only."""
    from kotsin_nse.market.session import TradingCalendar, session_buckets_back, to_ist

    e = await _paper(settings)
    try:
        eq = _bars30("RELIANCE", "2885", [10_000.0] * 6 + [25_000.0, 15_000.0])
        e.store.seed("RELIANCE", "30m", eq)
        fut = Instrument("68781", "RELIANCE", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=250,
                         expiry="2099-12-31", underlying="RELIANCE")
        e.catalogue_loader.catalogue.futures_by_symbol["RELIANCE"] = [fut]
        asked: list[tuple] = []

        async def candles(inst, tf, start, end):
            asked.append((inst.scrip_code, tf))
            # the future's own grid: it trades to 15:30, so the session before ends on a 15:15 bar
            # the future's own grid, every slot a reading or its ATR reads (it trades to 15:30, so the
            # session before ends on a 15:15 bar) — no gap for the 1m fill to mend
            slots = session_buckets_back(Segment.NSE_FO, eq[-1].ts, 15, "30m", TradingCalendar())
            rows = [{"dt": to_ist(ts).strftime("%Y-%m-%dT%H:%M:00"), "o": 1, "h": 1, "l": 1, "c": 1, "v": v}
                    for ts, v in zip(slots, [10_000.0] * 13 + [5_100.0, 7_900.0], strict=True)]
            # the partial bar after the trigger, which must not be read as T
            rows.append({"dt": to_ist(eq[-1].ts + 1800).strftime("%Y-%m-%dT%H:%M:00"), "o": 1, "h": 1, "l": 1, "c": 1, "v": 90_000.0})
            return rows

        e.rest.candles = candles  # type: ignore[method-assign]
        await _trigger(e, RELIANCE_OPT, RELIANCE)
        assert asked == [("68781", "30m"), ("68781", "1d")], "one context fetch per trigger for both gated books"
        assert sorted(p.strategy for p in held(e)) == ["FUDKII", "FUDKII_RT_N"]

        # the broker not answering is "unknown", never "dried": every book enters
        async def broken(inst, tf, start, end):
            raise RuntimeError("historical endpoint down")

        e.rest.candles = broken  # type: ignore[method-assign]
        e._fut_cache.clear()  # the context is fetched once per trigger bar; a new bar asks again
        e.positions.clear()
        await _trigger(e, RELIANCE_OPT, RELIANCE, shift=1800)
        assert sorted(p.strategy for p in held(e)) == ["FUDKII", "FUDKII_RT_N", "FUDKII_RT_X", "FUDKII_RT_Y", "FUDKII_RT_Y_W1"]
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_each_book_is_checked_against_its_own_wallet_and_positions(settings):
    """Every book is independent (2026-09-23): a book's exposure check sees its own purse, not the
    sum of every wallet, and another book's positions do not count against it."""
    e = await _paper(settings)
    try:
        seen = []
        book = e._exposure_by_strategy["FUDKII_RT_X"]
        real = book.check

        def spy(**kw):
            seen.append(kw)
            return real(**kw)

        book.check = spy  # type: ignore[method-assign]
        for i in range(7):  # seven parent positions already open
            p2 = Position(id=f"o{i}", strategy="FUDKII", instrument=RELIANCE_OPT, underlying=RELIANCE, side=PosSide.LONG, qty=250,
                          entry=50.0, opened_ts=1.0, signal_id=f"s{i}", direction=Direction.BULLISH)
            e.positions[p2.id] = p2
        await _trigger(e, RELIANCE_OPT, RELIANCE)
        assert seen and seen[0]["total_capital"] == 1_000_000.0 != sum(w.balance for w in e.wallets.values()), "its own purse, at check time"
        assert any(p.strategy == "FUDKII_RT_X" for p in e.positions.values()), "seven parent positions do not block RT-X"
        assert not [p for p in e.positions.values() if p.strategy == "FUDKII" and p.id not in {f"o{i}" for i in range(7)}], \
            "the parent already holds RELIANCE: its own cap, its own refusal"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_rejected_exit_is_retried_under_a_new_id_on_a_fresh_book_and_fills(settings, monkeypatch):
    """2026-09-25 14:03: RT-X and RT-N tried to close TATASTEEL 185 PE (₹0.73, a put nobody was
    trading). The depth book had not changed for 24.7 s, so the paper matcher refused the exit as
    stale; the id stayed 'seen', and every retry after that — once a second — was refused as a
    duplicate. The positions could not close at all, and the 15:20 force-flat would have failed
    the same way."""
    from kotsin_nse.domain import ExitDecision, ExitReason
    from kotsin_nse.exec.gateway import Mode
    from kotsin_nse.exec.paper import BookSnapshot

    e = Engine(settings)
    await e.start()
    try:
        e._mode = Mode.PAPER
        opt = Instrument("153805", "TATASTEEL", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=2750,
                         tick_size=0.01, strike=185.0, option_type=OptionType.PE, underlying="TATASTEEL")
        und = Instrument("3499", "TATASTEEL", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="TATASTEEL")
        now = time.time()
        pos = Position(id="pos-tata", strategy="FUDKII_RT_X", instrument=opt, underlying=und, side=PosSide.LONG,
                       qty=16500, entry=0.73, opened_ts=now - 1100, signal_id="FUDKII-TATASTEEL-1-B",
                       direction=Direction.BEARISH, equity_entry=187.25, equity_sl=188.6, option_sl=0.26)
        e.positions[pos.id] = pos
        # a real book, but one that stopped changing 25 s ago
        e.books["153805"] = BookSnapshot(scrip_code="153805", bids=[(0.60, 50000)], asks=[(0.62, 50000)], ts=now - 25)
        quotes = {"n": 0}

        async def market_feed(instruments):
            quotes["n"] += 1
            return {"153805": {"ltp": 0.61, "bid": 0.60, "ask": 0.62, "bid_qty": 60000, "ask_qty": 60000, "ts": time.time()}}

        d = ExitDecision(position_id=pos.id, reason=ExitReason.SL_EQ, ref_price=0.60, qty=16500)
        # first: the REST snapshot is down, so the stale book is all there is -> refused, backed off
        async def down(instruments):
            raise RuntimeError("REST down")

        monkeypatch.setattr(e.rest, "market_feed", down)
        await e._exit(pos, d, now)
        assert pos.status == "OPEN" and e._exit_attempts[pos.id] == 1
        assert e._exit_retry_at[pos.id] == pytest.approx(now + 2.0), "a pause before the next try"
        await e._exit(pos, d, now + 1.0)
        assert e._exit_attempts[pos.id] == 1, "inside the pause: nothing sent, nothing counted"

        # second: after the pause, a fresh quote stands a book and the SAME exit fills under a new id
        monkeypatch.setattr(e.rest, "market_feed", market_feed)
        await e._exit(pos, d, now + 3.0)
        assert quotes["n"] == 1, "the stale book was refreshed from a snapshot quote"
        assert pos.status == "CLOSED" and pos.exit_price == pytest.approx(0.60)
        assert pos.id not in e._exit_attempts, "a fill clears the retry state"
        ids = [o["client_order_id"] for o in await e.ledger.rows_between("orders", now - 60, now + 60)]
        assert ids[-1].endswith("|r1") and ids[0] == ids[-1][:-3], "the retry is a new order, not a duplicate"
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_a_restart_does_not_block_the_retry_of_a_rejected_order(settings):
    from kotsin_nse.ledger.db import Ledger

    led = Ledger(settings.db_url)
    await led.init()
    try:
        base = {"id": "o1", "strategy": "FUDKII_RT_X", "scrip_code": "1", "symbol": "X", "side": "SELL",
                "purpose": "EXIT", "qty": 1, "mode": "PAPER", "signal_id": "s", "position_id": "p", "ts": time.time()}
        await led.insert_order({**base, "client_order_id": "p|EXIT|0|SL-EQ", "status": "REJECTED"}, "REJECTED_BOOK")
        await led.insert_order({**base, "id": "o2", "client_order_id": "q|EXIT|0|SL-EQ", "status": "FILLED"}, "PAPER_FILLED")
        known = await led.known_client_order_ids()
        assert "q|EXIT|0|SL-EQ" in known, "a filled order is never re-sent"
        assert "p|EXIT|0|SL-EQ" not in known, "a rejected one went nowhere and may be retried"
    finally:
        await led.close()
