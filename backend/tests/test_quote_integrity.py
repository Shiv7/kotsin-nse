"""Operator, 2026-09-28: the broker's snapshot wiped good quotes the instant before a strike was
chosen — HDFCBANK's 720 PE (15.85 / 15.95 on the feed) read "one-sided" at the 09:45 trigger and all
three of that day's triggers were refused. A snapshot without a bid/ask never replaces a two-sided
quote; it confirms it only on a live subscription whose last trade the broker agrees with."""

from __future__ import annotations

import asyncio
import time

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType
from kotsin_nse.engine import Engine
from kotsin_nse.exec.paper import BookSnapshot
from kotsin_nse.instrument.select import Quote, SelectionPolicy, select_option


def _put(strike: float, code: str) -> Instrument:
    return Instrument(code, f"HDFCBANK 27 OCT 2026 PE {strike:.2f}", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=650,
                      tick_size=0.05, expiry="2026-10-27", strike=strike, option_type=OptionType.PE, underlying="HDFCBANK")


P720, P710 = _put(720.0, "78427"), _put(710.0, "97328")


def _sig(symbol="HDFCBANK", entry=722.15, targets=(716.1,)):
    from kotsin_nse.strategy.base import Signal
    from kotsin_nse.strategy.keys import StrategyKey

    return Signal(strategy=StrategyKey.FUDKII, symbol=symbol, direction=Direction.BEARISH, ts=int(time.time() // 1800 * 1800),
                  entry=entry, stop=entry + 0.65, targets=targets, grade="A", rr=9.0, reason="ST flip DOWN + close below lower band")


@pytest.fixture(autouse=True)
def _intraday(monkeypatch):
    """Off the session's first minute, whenever the suite runs (the open has its own tests)."""
    import kotsin_nse.engine as eng

    monkeypatch.setattr(eng, "OPEN_SETTLE_S", 0.0)


def _row(code, ltp, bid=0.0, ask=0.0):
    qty = 650 if bid > 0 and ask > 0 else 0
    return {code: {"ltp": ltp, "bid": bid, "ask": ask, "bid_qty": qty, "ask_qty": qty, "ts": time.time(), "volume": 0}}


async def _engine(settings, *, live=True):
    e = Engine(settings)
    fh = e.feed.health
    fh.connected, fh.connected_since = live, time.time() - 3600
    fh.last_message_ts = time.time() - (0.5 if live else 30.0)
    await e.feed.subscribe("mf", [P720])
    await e.feed.subscribe("md", [P720])
    for ch in ("mf", "md"):  # on the feed since 09:14, long before any quote a test holds
        e.feed._since[ch]["78427"] = time.time() - 3600
    return e


@pytest.mark.asyncio
async def test_a_snapshot_without_bid_ask_never_wipes_a_two_sided_quote(settings):
    e = await _engine(settings, live=False)  # the feed silent: nothing can be confirmed
    old = time.time() - 34
    e.quotes["78427"] = Quote(ltp=15.85, bid=15.85, ask=15.95, ts=old)
    e._apply_snapshot(_row("78427", 15.85), time.time())
    q = e.quotes["78427"]
    assert (q.bid, q.ask, q.ts) == (15.85, 15.95, old), "kept as it was: its own age says how old it is"
    assert e.snapshot_kept == 1 and e.snapshot_confirmed == 0


@pytest.mark.asyncio
async def test_it_confirms_the_held_quote_only_when_it_can(settings):
    e = await _engine(settings)
    now = time.time()
    e.quotes["78427"] = Quote(ltp=15.85, bid=15.85, ask=15.95, ts=now - 34)
    e._apply_snapshot(_row("78427", 15.85), now)
    q = e.quotes["78427"]
    assert (q.bid, q.ask) == (15.85, 15.95) and q.ts == now and e.snapshot_confirmed == 1, "the 720 PE at 09:45:05"
    # the broker's last trade is not ours: the feed missed a change — not confirmed
    e.quotes["78427"] = Quote(ltp=15.85, bid=15.85, ask=15.95, ts=now - 34)
    e._apply_snapshot(_row("78427", 16.05), now)
    assert e.quotes["78427"].ts == now - 34
    # a quote from before the last reconnect: a gap could hide a change
    e.feed.health.connected_since = now - 10
    e._apply_snapshot(_row("78427", 15.85), now)
    assert e.quotes["78427"].ts == now - 34
    # not on the subscription at all
    e.feed.health.connected_since = now - 3600
    e.quotes["97328"] = Quote(ltp=11.9, bid=11.9, ask=12.0, ts=now - 40)
    e._apply_snapshot(_row("97328", 11.9), now)
    assert e.quotes["97328"].ts == now - 40


@pytest.mark.asyncio
async def test_a_two_sided_snapshot_still_installs_and_nothing_held_stays_ltp_only(settings):
    e = await _engine(settings)
    e._apply_snapshot(_row("97328", 11.9, 11.9, 12.0), time.time())
    assert (e.quotes["97328"].bid, e.quotes["97328"].ask) == (11.9, 12.0) and "97328" in e.books
    e._apply_snapshot(_row("78425", 8.7), time.time())
    q = e.quotes["78425"]
    assert (q.ltp, q.bid, q.ask) == (8.7, 0.0, 0.0) and q.spread_pct is None, "as before: the last price, no book"


@pytest.mark.asyncio
async def test_a_held_book_is_confirmed_on_a_live_depth_subscription(settings):
    e = await _engine(settings)
    now = time.time()
    e.quotes["78427"] = Quote(ltp=15.85, bid=15.85, ask=15.95, ts=now - 34)
    e.books["78427"] = BookSnapshot("78427", bids=[(15.85, 650)], asks=[(15.95, 1300)], ts=now - 34)
    e._apply_snapshot(_row("78427", 15.85), now)
    b = e.books["78427"]
    assert b.ts == now and b.bids == [(15.85, 650)] and e.snapshot_book_confirmed == 1


@pytest.mark.asyncio
async def test_the_0945_hdfcbank_trigger_gets_its_first_choice(settings):
    """The 720 PE on the feed since 09:14, unchanged for 34 s; 710–670 never subscribed. Before, the
    snapshot wiped every one to 0/0 and the selector refused all six "one-sided"."""
    e = await _engine(settings)
    now = time.time()
    e.quotes["78427"] = Quote(ltp=15.85, bid=15.85, ask=15.95, ts=now - 34)
    e._apply_snapshot({**_row("78427", 15.85), **_row("97328", 11.85)}, now)
    pol = SelectionPolicy(min_premium=0.0, outlay_lots=4, outlay_under_inr=75_000.0)
    sel = select_option(chain=[P720, P710], quotes=e.quotes, spot=722.15, target1=716.1, direction=Direction.BEARISH, now=now, policy=pol)
    assert sel.ok and sel.instrument.strike == 720.0 and sel.premium == pytest.approx(15.90)


@pytest.mark.asyncio
async def test_the_choice_waits_for_a_strikes_first_frame_and_never_past_its_cap(settings):
    """HDFCBANK 710 PE, 2026-09-28: subscribed at the trigger, its first feed frame 1.1 s later. Until
    then it holds only the broker's snapshot (no bid, no ask): UNPRICED — the choice waits for the
    frame, returns on it, and never waits past its cap."""
    e = await _engine(settings)
    e.quote_wait_s = 1.0
    await e.feed.subscribe("mf", [P710])
    e._apply_snapshot(_row("97328", 11.85), time.time())
    pol = SelectionPolicy(min_premium=0.0)
    assert "710:unpriced" in select_option(chain=[P710], quotes=e.quotes, spot=722.15, target1=716.1,
                                           direction=Direction.BEARISH, now=time.time(), policy=pol).reason

    async def feed_frame():
        await asyncio.sleep(0.3)  # the feed's first frame after the subscription
        e.quotes["97328"] = Quote(ltp=11.9, bid=11.9, ask=12.0, ts=time.time())

    t0 = time.perf_counter()
    task = asyncio.create_task(feed_frame())
    sel = await e._choose_option(chain=[P710], sig=_sig(), pol=pol, atr30=0.0, watch=[], wait=True)
    await task
    assert sel.ok and sel.instrument.strike == 710.0 and 0.25 < time.perf_counter() - t0 < 0.8, "returned on the frame"
    p680 = _put(680.0, "78423")
    await e.feed.subscribe("mf", [p680])
    e._apply_snapshot(_row("78423", 3.0), time.time())  # never quoted by the feed
    t0 = time.perf_counter()
    sel = await e._choose_option(chain=[p680], sig=_sig(), pol=pol, atr30=0.0, watch=[], wait=True)
    assert not sel.ok and "680:unpriced" in sel.reason and 0.9 < time.perf_counter() - t0 < 1.6, "gave up at the cap"
    e.quote_wait_s = 0.0
    t0 = time.perf_counter()
    await e._choose_option(chain=[p680], sig=_sig(), pol=pol, atr30=0.0, watch=[], wait=True)
    assert time.perf_counter() - t0 < 0.05, "no wait at all when switched off (the replay)"


@pytest.mark.asyncio
async def test_a_held_quote_the_broker_contradicts_waits_for_the_next_frame(settings):
    """HCLTECH 1240 PE, 11:15:04: held 45.15 / 45.55 (last 44.85, 23 s old); the broker's last trade
    45.65; the feed's frame 45.25 / 45.80 came 0.1 s later. The choice waits for it — and, should it
    not come, still has the held quote rather than none."""
    p1240 = Instrument("78016", "HCLTECH 27 OCT 2026 PE 1240.00", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=400,
                       tick_size=0.05, expiry="2026-10-27", strike=1240.0, option_type=OptionType.PE, underlying="HCLTECH")
    e = await _engine(settings)
    await e.feed.subscribe("mf", [p1240])
    e.quote_wait_s = 1.0
    now = time.time()
    e.quotes["78016"] = Quote(ltp=44.85, bid=45.15, ask=45.55, ts=now - 23)
    e._apply_snapshot(_row("78016", 45.65), now)
    assert e.quotes["78016"].ts == now - 23 and "78016" in e._quote_outdated, "kept, and known to be old"

    async def frame():
        await asyncio.sleep(0.1)
        e.quotes["78016"] = Quote(ltp=45.65, bid=45.25, ask=45.80, ts=time.time())

    pol = SelectionPolicy(min_premium=0.0)
    sig = _sig("HCLTECH", entry=1250.0, targets=(1220.0,))
    t0 = time.perf_counter()
    task = asyncio.create_task(frame())
    sel = await e._choose_option(chain=[p1240], sig=sig, pol=pol, atr30=0.0, watch=[], wait=True)
    await task
    assert time.perf_counter() - t0 < 0.5 and sel.premium == pytest.approx((45.25 + 45.80) / 2), "chosen on the new frame"
    # no frame at all: the wait ends (cap 1 s) and the held quote (23 s, inside 30) still stands
    e.quotes["78016"] = Quote(ltp=44.85, bid=45.15, ask=45.55, ts=time.time() - 23)
    e._apply_snapshot(_row("78016", 45.65), time.time())
    sel = await e._choose_option(chain=[p1240], sig=sig, pol=pol, atr30=0.0, watch=[], wait=True)
    assert sel.ok and sel.premium == pytest.approx(45.35) and "78016" not in e._quote_outdated


@pytest.mark.asyncio
async def test_a_book_from_an_earlier_depth_subscription_is_never_confirmed(settings):
    """Review, 2026-09-28: depth drops a strike 30 minutes after the selector last looked at it and
    ``self.books`` keeps its last book; ``_follow_depth`` re-subscribes it just before the snapshot.
    A 9,000 s old book at 12.00 / 12.20 beside a live 15.85 / 15.95 was re-stamped fresh — and a paper
    buy would have filled at 12.20. It must stay aged, so the matcher refuses it."""
    e = await _engine(settings)
    now = time.time()
    e.quotes["78427"] = Quote(ltp=15.9, bid=15.85, ask=15.95, ts=now - 2)
    e.books["78427"] = BookSnapshot("78427", bids=[(12.0, 650)], asks=[(12.2, 650)], ts=now - 9000)
    await e.feed.unsubscribe("md", [P720])
    await e.feed.subscribe("md", [P720])  # re-followed a moment ago
    e._apply_snapshot(_row("78427", 15.9), now)
    b = e.books["78427"]
    assert b.ts == now - 9000 and e.snapshot_book_confirmed == 0
    assert b.age_ms(now) > e.matcher.age_limit_ms(now), "stays too old for the matcher to fill against"
    # on a subscription that ran throughout, a book that contradicts the live quote is still refused
    e.feed._since["md"]["78427"] = now - 3600
    e.books["78427"] = BookSnapshot("78427", bids=[(12.0, 650)], asks=[(12.2, 650)], ts=now - 40)
    e._apply_snapshot(_row("78427", 15.9), now)
    assert e.books["78427"].ts == now - 40 and e.snapshot_book_confirmed == 0


@pytest.mark.asyncio
async def test_a_held_position_is_marked_from_the_broker_when_the_feed_cannot_vouch(settings):
    """Review, 2026-09-28: kept at its old age, a feed outage over a minute (25 Sep 12:23–12:31)
    turned every open position stale and its mid was a price the broker had contradicted. An open
    position's contract is marked from the broker's last price, fresh — as before the change."""
    from kotsin_nse.domain import Position, PosSide

    e = await _engine(settings, live=False)  # the feed silent for 30 s
    now = time.time()
    e.positions["p1"] = Position(id="p1", strategy="FUDKII_RT_Y", instrument=P720, underlying=P720, side=PosSide.LONG,
                                 qty=2600, entry=16.0, opened_ts=now - 600, signal_id="s", direction=Direction.BEARISH,
                                 equity_entry=722.15, equity_sl=722.8, equity_targets=(716.1,), option_sl=15.6,
                                 option_targets=(16.86,), grade="A")
    e.quotes["78427"] = Quote(ltp=15.9, bid=15.85, ask=15.95, ts=now - 90)
    e._apply_snapshot(_row("78427", 11.0), now)  # the broker's last trade: through the stop
    q = e.quotes["78427"]
    assert q.mid == 11.0 and now - q.ts < 1 and e.snapshot_held_marked == 1, "marked from the broker, not left 90 s old"
    # with the feed alive and the broker agreeing, the held contract keeps its two-sided quote
    e.feed.health.connected, e.feed.health.last_message_ts = True, time.time()
    e.quotes["78427"] = Quote(ltp=15.9, bid=15.85, ask=15.95, ts=now - 30)
    e._apply_snapshot(_row("78427", 15.9), now)
    assert (e.quotes["78427"].bid, e.quotes["78427"].ask, e.quotes["78427"].ts) == (15.85, 15.95, now)


@pytest.mark.asyncio
async def test_a_strike_that_never_quotes_behind_the_choice_costs_nothing(settings):
    """Review, 2026-09-28: one strike in the span that never quotes (HDFCBANK OCT 650/660 PE, no quote
    all day) cost every trigger the full wait. The choice waits only for strikes AHEAD of the one it
    would take: a priced first choice is taken at once, whatever sits further out unpriced — and a
    card preview never waits at all."""
    from types import SimpleNamespace

    from kotsin_nse.bars.unified import BarSource, UnifiedBar

    e = await _engine(settings)
    und = Instrument("1333", "HDFCBANK", Segment.NSE_EQ, InstrumentKind.EQUITY, underlying="HDFCBANK")
    chain = [_put(float(k), f"9{k}") for k in range(650, 730, 10)]
    e.catalogue_loader.catalogue = SimpleNamespace(expiries=lambda sym: ["2026-10-27"], chain=lambda sym, expiry, ot: chain)
    t0 = int(time.time() // 1800 * 1800) - 1800 * 30
    e.store.seed("HDFCBANK", "30m", [UnifiedBar(symbol="HDFCBANK", scrip_code="1333", tf="30m", ts=t0 + 1800 * k, open=723.0,
                                                high=724.75, low=721.25, close=723.0, volume=1e6, source=BarSource.REST, complete=True)
                                     for k in range(30)])
    await e.feed.subscribe("mf", chain)
    now = time.time()
    for i in chain:
        if i.strike >= 670:
            e.quotes[i.scrip_code] = Quote(ltp=5.0, bid=4.95, ask=5.05, ts=now)
    e._apply_snapshot({**_row("9650", 1.2), **_row("9660", 1.6)}, now)  # 650 / 660: the snapshot, never a frame

    async def nothing(*_a, **_k):
        return None

    e._ensure_quotes = nothing  # type: ignore[method-assign]
    e._ensure_leg_ladder = nothing  # type: ignore[method-assign]
    e.quote_wait_s = 5.0
    sig = _sig(targets=(680.0,))
    span = e._strike_watch(chain, 722.15, Direction.BEARISH, 3.5, (680.0,))
    assert len(span) >= 4, "the span runs from 720 out to the target's 680"
    t0p = time.perf_counter()
    sel = await e._select_instrument(und, sig)
    assert sel.ok and sel.instrument.strike >= 670 and time.perf_counter() - t0p < 0.3, (sel, time.perf_counter() - t0p)
    # the preview, with its first choice unpriced: no wait, it shows what it has
    e._apply_snapshot(_row(sel.instrument.scrip_code, 5.0), now)
    e.quotes[sel.instrument.scrip_code] = Quote(ltp=5.0, bid=0.0, ask=0.0, ts=now, src="snapshot")
    t0p = time.perf_counter()
    await e._select_instrument(und, sig, tape=False)
    assert time.perf_counter() - t0p < 0.3, "a card preview never waits"
