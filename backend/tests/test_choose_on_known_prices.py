"""Operator, 2026-10-01: SONACOMS (09:45) and PAYTM (13:15) were refused "no tradeable strike", the
cheaper puts read "one-sided" — while the feed showed them two-sided the same second (770 PE 10.15 /
10.70). The broker's snapshot never carries a bid or ask; a strike subscribed at the trigger held only
that when the walk reached it, so the walk refused what it could not price. 14 triggers, 28 Sep -
1 Oct. The choice now waits — briefly, and only for strikes ahead of what it would take — for the
feed to price them; at the session's first minute it waits for books still filling (the 09:15 carry:
TATASTEEL, SIEMENS, GLENMARK)."""

from __future__ import annotations

import asyncio
import time

import pytest

import kotsin_nse.engine as eng
from kotsin_nse.config import Segment
from kotsin_nse.domain import Direction, Instrument, InstrumentKind, OptionType
from kotsin_nse.engine import Engine
from kotsin_nse.instrument.select import Quote, SelectionPolicy, select_option
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey


def _put(sym: str, strike: float, code: str, lot: int) -> Instrument:
    return Instrument(code, f"{sym} 27 OCT 2026 PE {strike:.2f}", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=lot,
                      tick_size=0.05, expiry="2026-10-27", strike=strike, option_type=OptionType.PE, underlying=sym)


def _sig(sym: str, entry: float, target: float) -> Signal:
    return Signal(strategy=StrategyKey.FUDKII, symbol=sym, direction=Direction.BEARISH, ts=int(time.time() // 1800 * 1800),
                  entry=entry, stop=entry + 5.45, targets=(target,), grade="A", rr=15.1, reason="ST flip DOWN + close below lower band")


def _snap(code: str, ltp: float) -> dict:
    return {code: {"ltp": ltp, "bid": 0.0, "ask": 0.0, "bid_qty": 0, "ask_qty": 0, "ts": time.time(), "volume": 0}}


async def _engine(settings) -> Engine:
    e = Engine(settings)
    fh = e.feed.health
    fh.connected, fh.connected_since, fh.last_message_ts = True, time.time() - 3600, time.time() - 0.2
    return e


@pytest.fixture(autouse=True)
def _intraday(monkeypatch):
    monkeypatch.setattr(eng, "OPEN_SETTLE_S", 0.0)


def test_a_snapshot_is_unpriced_and_a_feed_one_side_is_one_sided():
    now = time.time()
    a, b = _put("X", 100.0, "1", 100), _put("X", 99.0, "2", 100)
    quotes = {"1": Quote(ltp=6.0, bid=0.0, ask=0.0, ts=now, src="snapshot"), "2": Quote(ltp=5.5, bid=0.0, ask=5.6, ts=now)}
    sel = select_option(chain=[a, b], quotes=quotes, spot=101.0, target1=96.0, direction=Direction.BEARISH, now=now,
                        policy=SelectionPolicy(min_premium=0.0))
    assert not sel.ok and "100:unpriced" in sel.reason and "99:one-sided" in sel.reason
    assert sel.unpriced == ("1",) and sel.one_sided == ("2",)
    chain = [_put("X", 100.0 - k, str(10 + k), 100) for k in range(9)]
    sel = select_option(chain=chain, quotes={}, spot=101.0, target1=92.0, direction=Direction.BEARISH, now=now,
                        policy=SelectionPolicy(min_delta=0.0))
    assert sel.reason.endswith("(+3 more)") and len(sel.unpriced) == 9, "every refused strike is counted, not the first six"


@pytest.mark.asyncio
async def test_sonacoms_0945_the_fallback_strike_is_priced_by_its_first_frame(settings):
    """810 / 800 PE on the feed but 4 lots of 1,225 cost ₹1.25 lakh / ₹1.03 lakh (≥ ₹75,000); 790-760
    subscribed at the trigger, the snapshot only. Their frames, the same second: 790 16.50 / 17.20
    (4 lots ₹82.6k, too dear), 780 one-sided, 770 10.15 / 10.70, 760 8.20 / 8.65."""
    e = await _engine(settings)
    k = {810: "128039", 800: "109866", 790: "128037", 780: "109860", 770: "128035", 760: "109857"}
    chain = [_put("SONACOMS", float(s), c, 1225) for s, c in k.items()]
    await e.feed.subscribe("mf", chain)
    now = time.time()
    e.quotes[k[810]] = Quote(ltp=25.0, bid=25.0, ask=25.95, ts=now)
    e.quotes[k[800]] = Quote(ltp=21.2, bid=20.9, ask=21.35, ts=now)
    snap: dict = {}
    for s in (790, 780, 770, 760):
        snap |= _snap(k[s], {790: 17.25, 780: 13.0, 770: 10.3, 760: 8.45}[s])
    e._apply_snapshot(snap, now)
    pol = SelectionPolicy(outlay_lots=4, outlay_under_inr=75_000.0, min_delta=0.0)  # the pricing alone; the floor below
    sig = _sig("SONACOMS", 813.2, 730.65)
    before = select_option(chain=chain, quotes=e.quotes, spot=813.2, target1=730.65, direction=Direction.BEARISH, now=now, policy=pol)
    assert not before.ok and "one-sided" not in before.reason and "unpriced" in before.reason, before.reason

    async def frames():
        await asyncio.sleep(0.3)
        t = time.time()
        e.quotes[k[790]] = Quote(ltp=17.25, bid=16.5, ask=17.2, ts=t)
        e.quotes[k[780]] = Quote(ltp=13.0, bid=0.0, ask=13.25, ts=t)
        e.quotes[k[770]] = Quote(ltp=10.3, bid=10.15, ask=10.7, ts=t)
        e.quotes[k[760]] = Quote(ltp=8.45, bid=8.2, ask=8.65, ts=t)

    t0 = time.perf_counter()
    task = asyncio.create_task(frames())
    sel = await e._choose_option(chain=chain, sig=sig, pol=pol, atr30=0.0, watch=[], wait=True)
    await task
    assert sel.ok and sel.instrument.strike in (770.0, 760.0) and 0.25 < time.perf_counter() - t0 < 1.0, (sel, time.perf_counter() - t0)
    assert sel.premium * 1225 * 4 < 75_000


@pytest.mark.asyncio
async def test_a_feed_one_sided_strike_is_passed_at_once_intraday(settings):
    """Intraday a one-sided FEED quote is a fact, not a delay: the walk moves on without waiting."""
    e = await _engine(settings)
    a, b = _put("X", 100.0, "1", 100), _put("X", 99.0, "2", 100)
    await e.feed.subscribe("mf", [a, b])
    now = time.time()
    e.quotes["1"] = Quote(ltp=6.0, bid=0.0, ask=6.1, ts=now)
    e.quotes["2"] = Quote(ltp=5.5, bid=5.45, ask=5.55, ts=now)
    t0 = time.perf_counter()
    sel = await e._choose_option(chain=[a, b], sig=_sig("X", 101.0, 96.0), pol=SelectionPolicy(min_premium=0.0), atr30=0.0, watch=[], wait=True)
    assert sel.ok and sel.instrument.strike == 99.0 and time.perf_counter() - t0 < 0.1


@pytest.mark.asyncio
async def test_a_silent_feed_is_not_waited_for(settings):
    e = await _engine(settings)
    e.feed.health.last_message_ts = time.time() - 30  # nothing for 30 s: no frame is coming
    a = _put("X", 100.0, "1", 100)
    await e.feed.subscribe("mf", [a])
    e._apply_snapshot(_snap("1", 6.0), time.time())
    t0 = time.perf_counter()
    sel = await e._choose_option(chain=[a], sig=_sig("X", 101.0, 96.0), pol=SelectionPolicy(min_premium=0.0), atr30=0.0, watch=[], wait=True)
    assert not sel.ok and "unpriced" in sel.reason and time.perf_counter() - t0 < 0.1


@pytest.mark.asyncio
async def test_a_strike_not_on_the_feed_is_not_waited_for(settings):
    """No subscription, no frame: an unpriced strike the feed was never asked for costs nothing."""
    e = await _engine(settings)
    a, b = _put("X", 100.0, "1", 100), _put("X", 99.0, "2", 100)
    e.quotes["2"] = Quote(ltp=5.5, bid=5.45, ask=5.55, ts=time.time())
    t0 = time.perf_counter()
    sel = await e._choose_option(chain=[a, b], sig=_sig("X", 101.0, 96.0), pol=SelectionPolicy(min_premium=0.0), atr30=0.0, watch=[], wait=True)
    assert sel.ok and sel.instrument.strike == 99.0 and time.perf_counter() - t0 < 0.1


@pytest.mark.asyncio
async def test_a_strike_ahead_that_never_quotes_costs_only_its_grace(settings, monkeypatch):
    monkeypatch.setattr(eng, "STRIKE_GRACE_S", 0.5)
    e = await _engine(settings)
    e.quote_wait_s = 5.0
    a, b = _put("X", 100.0, "1", 100), _put("X", 99.0, "2", 100)
    await e.feed.subscribe("mf", [a, b])
    now = time.time()
    pol = SelectionPolicy(min_premium=0.0)
    for c in ("1", "2"):
        e.quotes[c] = Quote(ltp=5.5, bid=5.45, ask=5.55, ts=now)
    first = select_option(chain=[a, b], quotes=e.quotes, spot=101.0, target1=96.0, direction=Direction.BEARISH, now=now, policy=pol).instrument
    other = b if first is a else a
    e._apply_snapshot(_snap(first.scrip_code, 6.0), now)
    e.quotes[first.scrip_code] = Quote(ltp=6.0, bid=0.0, ask=0.0, ts=now, src="snapshot")
    t0 = time.perf_counter()
    sel = await e._choose_option(chain=[a, b], sig=_sig("X", 101.0, 96.0), pol=pol, atr30=0.0, watch=[], wait=True)
    assert sel.ok and sel.instrument is other and 0.4 < time.perf_counter() - t0 < 1.0, time.perf_counter() - t0


@pytest.mark.asyncio
async def test_at_the_open_a_book_still_filling_is_waited_for(settings, monkeypatch):
    """TATASTEEL 180 PE, 2026-10-01: 0 / 0 at 09:15:03, 2.42 / 2.53 at 09:15:27 — the carried trigger
    chose at 09:15:07 and was refused. In the session's first minute a one-sided book is waited for;
    after it, it is not."""
    monkeypatch.setattr(eng, "OPEN_SETTLE_S", 1.5)
    monkeypatch.setattr(eng, "session_open_ts", lambda seg, day: time.time() - 0.1)
    e = await _engine(settings)
    a = _put("TATASTEEL", 180.0, "1", 5500)
    await e.feed.subscribe("mf", [a])
    e.quotes["1"] = Quote(ltp=2.78, bid=0.0, ask=0.0, ts=time.time())  # the feed's own empty book
    pol = SelectionPolicy(min_premium=0.0)

    async def fill():
        await asyncio.sleep(0.4)
        e.quotes["1"] = Quote(ltp=2.45, bid=2.42, ask=2.53, ts=time.time())

    t0 = time.perf_counter()
    task = asyncio.create_task(fill())
    sel = await e._choose_option(chain=[a], sig=_sig("TATASTEEL", 184.7, 177.0), pol=pol, atr30=0.0, watch=[], wait=True)
    await task
    assert sel.ok and 0.35 < time.perf_counter() - t0 < 1.2
    # the window closed: one-sided is a fact again
    monkeypatch.setattr(eng, "OPEN_SETTLE_S", 0.0)
    e.quotes["1"] = Quote(ltp=2.78, bid=0.0, ask=0.0, ts=time.time())
    t0 = time.perf_counter()
    sel = await e._choose_option(chain=[a], sig=_sig("TATASTEEL", 184.7, 177.0), pol=pol, atr30=0.0, watch=[], wait=True)
    assert not sel.ok and "one-sided" in sel.reason and time.perf_counter() - t0 < 0.1


def test_the_delta_floor_holds_for_the_fallback_strikes_too():
    """Operator, 2026-10-01: the fallback walk had no delta floor; its trades under 0.20 lost ₹1,14,096
    (43 trades, 24 Aug - 1 Oct). SONACOMS 09:45: once priced, 780 / 770 / 760 PE sit 4-7 % out
    (estimated delta 0.17 / 0.15 / 0.15) and 790 PE costs ₹82.6k for 4 lots — no strike, by rule. The
    OTM strike nearest spot is exempt (a coarse grid), and a refused strike is never waited for."""
    now = time.time()
    k = {810: "128039", 800: "109866", 790: "128037", 780: "109860", 770: "128035", 760: "109857"}
    chain = [_put("SONACOMS", float(s), c, 1225) for s, c in k.items()]
    q = {k[810]: (25.0, 25.95), k[800]: (20.9, 21.35), k[790]: (16.5, 17.2), k[780]: (12.6, 13.25), k[770]: (10.15, 10.7), k[760]: (8.2, 8.65)}
    quotes = {c: Quote(ltp=(b + a) / 2, bid=b, ask=a, ts=now) for c, (b, a) in q.items()}
    enforced = SelectionPolicy(outlay_lots=4, outlay_under_inr=75_000.0, enforce_fallback_delta=True)
    sel = select_option(chain=chain, quotes=quotes, spot=813.2, target1=730.65, direction=Direction.BEARISH, now=now, policy=enforced)
    assert not sel.ok and "780:delta-0.17<0.2" in sel.reason and "770:delta-0.15<0.2" in sel.reason, sel.reason
    assert not sel.unpriced, "a strike under the floor is refused before its price is asked for"
    # the live setting (operator, 1 Oct): SHADOW — the strike is taken and marked with its delta
    sel = select_option(chain=chain, quotes=quotes, spot=813.2, target1=730.65, direction=Direction.BEARISH, now=now,
                        policy=SelectionPolicy(outlay_lots=4, outlay_under_inr=75_000.0))
    assert sel.ok and sel.instrument.strike in (780.0, 770.0, 760.0) and sel.delta_shadow is not None and sel.delta_shadow < 0.2
    # a coarse grid: IDEA at 13.27, its nearest call 14 (5.5 % out) is as near as the chain goes
    idea = [Instrument(str(c), f"IDEA 27 OCT 2026 CE {s:.2f}", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=71475, tick_size=0.01,
                       expiry="2026-10-27", strike=s, option_type=OptionType.CE, underlying="IDEA") for c, s in ((1, 14.0), (2, 15.0))]
    iq = {"1": Quote(ltp=0.21, bid=0.2, ask=0.21, ts=now), "2": Quote(ltp=0.08, bid=0.07, ask=0.08, ts=now)}
    sel = select_option(chain=idea, quotes=iq, spot=13.27, target1=14.2, direction=Direction.BULLISH, now=now,
                        policy=SelectionPolicy(min_premium=0.0, enforce_fallback_delta=True))
    assert sel.ok and sel.instrument.strike == 14.0 and sel.delta_shadow is None, sel.reason


@pytest.mark.asyncio
async def test_in_shadow_mode_the_decision_logs_what_the_floor_would_have_bought(settings):
    """The far strike is bought; the event says the enforced floor would have taken the nearer one."""
    e = await _engine(settings)
    near, far = _put("X", 100.0, "1", 100), _put("X", 95.0, "2", 100)
    await e.feed.subscribe("mf", [near, far])
    now = time.time()
    e.quotes["1"] = Quote(ltp=8.0, bid=0.0, ask=8.2, ts=now)   # one-sided: the walk passes it
    e.quotes["2"] = Quote(ltp=5.5, bid=5.45, ask=5.55, ts=now)
    events: list[tuple[str, dict]] = []

    async def spy(kind, payload):
        events.append((kind, payload))

    e.ledger.event = spy  # type: ignore[method-assign]
    sel = await e._choose_option(chain=[near, far], sig=_sig("X", 101.0, 96.0), pol=SelectionPolicy(min_premium=0.0),
                                 atr30=0.0, watch=[], wait=True)
    assert sel.ok and sel.instrument.strike == 95.0 and sel.delta_shadow == pytest.approx(0.15)
    kind, p = events[-1]
    assert kind == "strike.delta_shadow" and p["took"] == 95.0 and p["enforcedTook"] is None and "95:delta-0.15<0.2" in p["enforcedReason"]
    events.clear()
    await e._choose_option(chain=[near, far], sig=_sig("X", 101.0, 96.0), pol=SelectionPolicy(min_premium=0.0),
                           atr30=0.0, watch=[], wait=False)
    assert events == [], "a card preview logs nothing"
