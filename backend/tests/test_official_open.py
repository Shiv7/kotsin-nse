"""The official open and the official gap (CTB-19Oct, operator 18-19 Oct 2026: "the 1 AM definition"): the official
open is the pre-open equilibrium print — the 1D candle's open — read off the feed's frames by their exchange time, the
first continuous trade where there was no pre-open match (NSE's rule); the official close is the previous session's
closing-auction print, the 1D close. Every trigger carries ``gapOfficial`` beside the first trade's ``gapDatr``, and
KN_GAP_SOURCE says which one the gap rules read: ``official`` by default (operator, 19 Oct: "did we not decide to use
official open ... at 1 AM?" — official for every book, confirmed 19 Oct), the first bar's ``gapDatr`` where the open was
not seen (flagged ``gapRuleRead`` first_bar); ``first_tick``, the switch-back, makes every decision the one made before.

Research (b1_7days, 9-19 Oct): the official numbers matched 126/126 (broker 1D = engine 1D = the tape's last pre-open
print); the first trade differed on 122 of 146; the real cases below are 19 Oct's and 29 Sep's."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any

import pytest
from pydantic import ValidationError

from kotsin_nse import engine as engine_mod
from kotsin_nse.bars.official_open import (
    FIRST_BAR,
    FIRST_TRADE,
    PREOPEN,
    OfficialOpens,
    official_gap,
)
from kotsin_nse.bars.pivots import Zone
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment, Settings, known_env_keys
from kotsin_nse.domain import Direction, Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import IST
from kotsin_nse.risk.limits import CT_Y_LIMITS, RT_Y_F_LIMITS, RT_Y_LIMITS
from kotsin_nse.strategy.base import Signal
from kotsin_nse.strategy.keys import StrategyKey
from kotsin_nse.strategy.regime_gates import gap_switch, rt_gate_reasons, rule_gap, rule_gap_read
from kotsin_nse.venue.fivepaisa.ws import FivePaisaFeed
from tests.test_gate_b_and_gap_fade import _seed, _trigger

DAY = date(2026, 10, 19)  # a Monday: the previous session is Friday 16 Oct


def at(hms: str, day: date = DAY) -> float:
    h, m, *s = (int(x) for x in hms.split(":"))
    return datetime.combine(day, dtime(h, m, s[0] if s else 0), tzinfo=IST).timestamp()


# -- the capture -------------------------------------------------------------------------------------------


def test_the_last_pre_open_print_today_is_the_open_and_the_first_continuous_frame_settles_it():
    oo = OfficialOpens(None)
    assert oo.note("PREMIERENE", 901.05, at("15:29:59", DAY - timedelta(days=3)), at("08:30")) is None, "Friday's last trade"
    assert oo.note("PREMIERENE", 905.0, at("09:07:40"), at("09:07:41")) is None
    assert oo.note("PREMIERENE", 908.0, at("09:09:19"), at("09:09:20")) is None
    assert oo.note("PREMIERENE", 906.0, at("09:08:00"), at("09:09:25")) is None, "late, with an older exchange time"
    got = oo.note("PREMIERENE", 911.5, at("09:15:00"), at("09:15:01"), feed_open=908.0, feed_prev_close=901.05)
    assert got is not None and (got.price, got.src, got.exch_ts) == (908.0, PREOPEN, at("09:09:19"))
    assert (got.feed_open, got.feed_prev_close) == (908.0, 901.05), "the frame's OpenRate / PClose, kept for the audit"
    assert oo.note("PREMIERENE", 915.0, at("09:20:00"), at("09:20:00")) is None and oo.get("PREMIERENE", DAY) == got, "once a day"
    assert oo.get("PREMIERENE", DAY - timedelta(days=1)) is None


def test_a_frame_without_a_trusted_exchange_time_never_counts():
    oo = OfficialOpens(None)
    oo.note("UNIONBANK", 171.68, None, at("08:30"))  # no TickDt
    oo.note("UNIONBANK", 171.68, -62135596800.0, at("08:30"))  # .NET's minimum date
    oo.note("UNIONBANK", 171.68, at("15:29:40", DAY - timedelta(days=3)), at("08:30"))  # the last trade's time: Friday
    oo.note("UNIONBANK", 171.0, at("08:59:59"), at("09:00"))  # before the pre-open
    assert oo.untimed == 2 and oo.other_day == 1
    got = oo.note("UNIONBANK", 173.1, at("09:15:00"), at("09:15:00"))
    assert got is not None and (got.price, got.src) == (173.1, FIRST_TRADE), "none of them was a pre-open print"


def test_no_pre_open_match_opens_at_the_first_trade_and_says_so():
    oo = OfficialOpens(None)
    oo.note("RELIANCE", 1400.0, at("15:29:30", DAY - timedelta(days=3)), at("08:31"))  # the feed was up before the open
    got = oo.note("RELIANCE", 1404.0, at("09:15:02"), at("09:15:02"))
    assert got is not None and (got.price, got.src, got.exch_ts) == (1404.0, FIRST_TRADE, at("09:15:02"))
    assert oo.stats()["bySource"] == {FIRST_TRADE: 1} and oo.stats()["watchFrom"] == "08:31:00"


def test_a_process_that_joined_after_the_open_takes_no_first_trade():
    oo = OfficialOpens(None)
    assert oo.note("SBIN", 812.0, at("09:59:58"), at("10:00:00")) is None, "the first trade it sees is not the first trade"
    assert oo.get("SBIN", DAY) is None
    # ... but a pre-open frame it does get (a snapshot carrying the match's time) is the open
    oo.note("PNB", 121.12, at("09:08:12"), at("10:00:01"))
    got = oo.note("PNB", 122.0, at("10:00:02"), at("10:00:02"))
    assert got is not None and (got.price, got.src) == (121.12, PREOPEN)


def test_an_index_is_captured_like_any_underlying():
    """NIFTY / BANKNIFTY ride the cash feed (999920xx) and print their pre-open like a stock (review 19 Oct): BANKNIFTY
    12 Oct read 55,350.15 at 09:13:59 on the tape — the 1D EOD open; its first continuous value was 55,328.40. An
    order-entry value before it is refused. With no pre-open frame at all, the first value of the session."""
    day = date(2026, 10, 12)
    oo = OfficialOpens(None)
    oo.note("BANKNIFTY", 55256.65, at("15:30:00", day - timedelta(days=3)), at("08:30", day))  # Friday's close
    oo.note("BANKNIFTY", 55120.0, at("09:03:00", day), at("09:03:00", day))  # order entry: an indicative value
    oo.note("BANKNIFTY", 55350.15, at("09:13:59", day), at("09:13:59", day))
    got = oo.note("BANKNIFTY", 55328.4, at("09:15:00", day), at("09:15:00", day))
    assert got is not None and (got.price, got.src, got.exch_ts) == (55350.15, PREOPEN, at("09:13:59", day))
    assert oo.order_entry == 1 and oo.stats()["refusedOrderEntry"] == 1
    g = official_gap(got.price, 55256.65, 683.3519, bullish=False)
    assert g == pytest.approx(-0.136826, abs=5e-7), "a bearish trigger: the gap up is against it"
    bare = OfficialOpens(None)
    bare.note("BANKNIFTY", 55844.0, at("15:30:00", DAY - timedelta(days=3)), at("08:30"))
    got = bare.note("BANKNIFTY", 55797.0, at("09:15:00"), at("09:15:00"))
    assert got is not None and (got.price, got.src) == (55797.0, FIRST_TRADE), "no pre-open frame: the first value"


def test_an_order_entry_frame_is_not_the_pre_open_match():
    """MAHABANK 19 Oct (review 19 Oct): a frame that arrived 09:05:48 at the previous close, 79.79, with a crossed book —
    order entry, before the match; research has no pre-open match and the first trade 79.86. Were it stamped today, it
    would have stood as the open at a gap of 0. Order entry closes at a random second in 09:07-09:08: from 09:07 a frame
    is the match's."""
    oo = OfficialOpens(None)
    oo.note("MAHABANK", 79.79, at("15:29:40", DAY - timedelta(days=3)), at("08:30"))
    assert oo.note("MAHABANK", 79.79, at("09:05:48"), at("09:05:48")) is None and oo.order_entry == 1
    assert oo.note("MAHABANK", 79.79, at("09:06:59"), at("09:06:59")) is None and oo.order_entry == 2
    got = oo.note("MAHABANK", 79.86, at("09:15:01"), at("09:15:01"))
    assert got is not None and (got.price, got.src) == (79.86, FIRST_TRADE)
    oo.note("PNB", 120.87, at("09:07:00"), at("09:07:01"))  # the earliest second the match can carry
    got = oo.note("PNB", 121.0, at("09:15:00"), at("09:15:00"))
    assert got is not None and (got.price, got.src) == (120.87, PREOPEN)


def test_a_reconnect_after_the_open_settles_no_first_trade_off_its_snapshot():
    """Review 19 Oct: the feed up from 00:45, PNB's match caught at 09:08, then down 09:10-10:30 (the Mac asleep) — the
    reconnect's first frame for a name with no pre-open price is the snapshot of its LAST trade (TickDt 10:29:58). It
    settled as the open at the 10:29 price; now nothing settles and the engine reads the first 30m bar (``first_bar``)."""
    oo = OfficialOpens(None)
    oo.note("Y", 200.0, at("15:29:00", DAY - timedelta(days=3)), at("00:45"))  # the subscribe snapshot: Friday's trade
    oo.note("PNB", 120.87, at("09:08:12"), at("09:08:13"))  # a pre-open price caught before the sleep
    oo.feed_connected(at("10:30:00"))
    assert oo.note("Y", 212.0, at("10:29:58"), at("10:30:05")) is None and oo.get("Y", DAY) is None
    got = oo.note("PNB", 123.4, at("10:29:59"), at("10:30:05"))
    assert got is not None and (got.price, got.src) == (120.87, PREOPEN), "a pre-open price held still settles"
    oo.note("UNIONBANK", 172.9, at("09:08:30"), at("10:30:06"))  # re-sent with the match's own time
    got = oo.note("UNIONBANK", 174.0, at("10:30:10"), at("10:30:10"))
    assert got is not None and (got.price, got.src) == (172.9, PREOPEN)
    assert oo.stats()["watchFrom"] == "10:30:00"


def test_a_reconnect_before_the_open_changes_nothing():
    oo = OfficialOpens(None)
    oo.note("RELIANCE", 1400.0, at("15:29:30", DAY - timedelta(days=3)), at("00:45"))
    oo.feed_connected(at("09:12:00"))  # the socket dropped 09:10-09:12: the open is still ahead
    got = oo.note("RELIANCE", 1404.0, at("09:15:02"), at("09:15:02"))
    assert got is not None and (got.price, got.src) == (1404.0, FIRST_TRADE) and oo.stats()["watchFrom"] == "00:45:00"
    fresh = OfficialOpens(None)
    fresh.feed_connected(at("08:30"))  # a first connect before the open: nothing to say yet
    assert fresh.day == DAY and fresh.watch_from is None


def test_a_pooled_frame_that_arrived_yesterday_never_rolls_today_away(tmp_path):
    """Review 19 Oct: the pool primes a code with the time its frame first ARRIVED — possibly yesterday. It rolled the
    day back, wiping today's pre-open prices and what ``load`` restored; nothing re-settled."""
    oo = OfficialOpens(tmp_path)
    assert oo.load(DAY) == 0 and oo.day == DAY, "the boot starts today with or without a file"
    oo.note("X", 100.0, at("08:30"), at("08:30"))
    oo.note("X", 101.0, at("09:08:10"), at("09:08:11"))
    assert oo.note("Q", 50.0, at("15:29:00", DAY - timedelta(days=1)), at("15:30:00", DAY - timedelta(days=1)), pooled=True) is None
    assert oo.day == DAY
    got = oo.note("X", 101.5, at("09:15:01"), at("09:15:01"))
    assert got is not None and (got.price, got.src) == (101.0, PREOPEN)


def test_a_pooled_frame_never_says_the_feed_was_up_nor_settles_a_first_trade():
    """Review 19 Oct: a restart at 09:20 whose first frame is a pooled one that arrived at the other engine 09:14:58 —
    it claimed the feed was up for the open, and later frames settled as first trades. A pooled pre-open price is still
    the name's pre-open price, by its exchange time."""
    oo = OfficialOpens(None)
    assert oo.note("W", 400.0, at("09:08:20"), at("09:14:58"), pooled=True) is None
    assert oo.watch_from is None
    assert oo.note("V", 512.0, at("09:20:01"), at("09:20:02")) is None, "V's 09:20 trade is not its first"
    got = oo.note("W", 401.0, at("09:20:03"), at("09:20:03"))
    assert got is not None and (got.price, got.src) == (400.0, PREOPEN)
    up = OfficialOpens(None)
    up.note("V", 500.0, at("15:29:00", DAY - timedelta(days=3)), at("08:30"))  # live: the feed was up before the open
    assert up.note("V", 512.0, at("09:15:02"), at("09:15:03"), pooled=True) is None, "a pooled frame settles no first trade"
    got = up.note("V", 512.5, at("09:15:04"), at("09:15:04"))
    assert got is not None and (got.price, got.src) == (512.5, FIRST_TRADE)


def test_the_days_opens_and_pending_pre_open_prices_survive_a_restart(tmp_path):
    oo = OfficialOpens(tmp_path)
    oo.note("PNB", 116.3, at("15:29:00", DAY - timedelta(days=3)), at("08:30"))
    oo.note("PNB", 121.12, at("09:08:12"), at("09:08:13"))
    oo.note("PNB", 121.5, at("09:15:00"), at("09:15:00"))
    oo.note("AXISBANK", 1180.0, at("09:08:20"), at("09:08:21"))  # pre-open, not settled yet
    assert oo.dirty
    oo.write(oo.snapshot())
    assert not oo.dirty and (tmp_path / "2026-10-19.json").exists()
    again = OfficialOpens(tmp_path)
    assert again.load(DAY) == 1 and again.get("PNB", DAY) == oo.get("PNB", DAY)
    assert again.watch_from is None, "a restart did not watch this open"
    got = again.note("AXISBANK", 1175.0, at("09:16:00"), at("09:16:00"))
    assert got is not None and (got.price, got.src) == (1180.0, PREOPEN), "the pre-open price saved before the restart"
    assert OfficialOpens(tmp_path).load(DAY + timedelta(days=1)) == 0


def test_the_feed_says_when_a_frame_has_no_exchange_time():
    stamped = FivePaisaFeed._tick({"Token": 1, "LastRate": 10.0, "TickDt": "/Date(1792381500000)/"}, 1792381501.0)
    assert stamped["exch_ts"] == 1792381500.0 and stamped["ts"] == 1792381500.0
    none = FivePaisaFeed._tick({"Token": 1, "LastRate": 10.0}, 1792381501.0)
    assert none["exch_ts"] is None
    dotnet = FivePaisaFeed._tick({"Token": 1, "LastRate": 10.0, "TickDt": "/Date(-62135596800000)/"}, 1792381501.0)
    assert dotnet["exch_ts"] is None and dotnet["ts"] == -62135596800.0, "``ts`` keeps its old value: the bars drop it"


# -- the gap's arithmetic on real cases ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "open_px", "close_px", "datr", "bullish", "gap", "b1"),
    [
        ("PREMIERENE 19 Oct", 908.00, 901.05, 27.5396, True, 0.2524, True),  # the operator's "non-gap but a strong fade"
        ("UNITDSPR 19 Oct", 1372.0, 1380.0, 31.8168, False, 0.2514, True),  # bearish: a gap down is its own way
        ("UNIONBANK 19 Oct", 172.9, 171.68, 4.8906, True, 0.2495, False),  # the near-miss — NOT B1
        ("KALYANKJIL 29 Sep", 566.00, 564.00, 17.92, True, 0.1116, False),
    ],
)
def test_the_official_gap_on_the_research_cases(name, open_px, close_px, datr, bullish, gap, b1):
    g = official_gap(open_px, close_px, datr, bullish=bullish)
    assert g == pytest.approx(gap, abs=5e-5), name
    assert (g >= 0.25) is b1, name
    assert official_gap(open_px, close_px, datr, bullish=not bullish) == pytest.approx(-g), "against the trigger: negative"
    assert official_gap(open_px, close_px, 0.0, bullish=bullish) is None


# -- every trigger carries both gaps ---------------------------------------------------------------------------


def _store(e: Engine, sym: str, code: str, *, first_open: float) -> int:
    """30 flat official sessions to Friday 16 Oct (true range 60: daily ATR 60, close 4,700), 20 prior 30m bars and
    today's 09:15 bar opening at ``first_open`` — the first trade."""
    dailies, d = [], DAY - timedelta(days=46)
    while len(dailies) < 30:
        d += timedelta(days=1)
        if d.weekday() < 5 and d < DAY:
            dailies.append(UnifiedBar(sym, code, "1d", int(at("00:00", d)), 4700, 4730, 4670, 4700, 1e6, source=BarSource.REST, complete=True))
    e.store.seed(sym, "1d", dailies)
    open_ts = int(at("09:15"))
    prior = [UnifiedBar(sym, code, "30m", open_ts - 86_400 * 3 + k * 1800, 4690, 4700, 4685, 4695, 1e4, source=BarSource.REST, complete=True)
             for k in range(20)]
    e.store.seed(sym, "30m", [*prior, UnifiedBar(sym, code, "30m", open_ts, first_open, first_open + 6, first_open - 10, first_open - 4, 3e4,
                                                 source=BarSource.REST, complete=True)])
    return open_ts


@pytest.mark.asyncio
async def test_every_trigger_carries_both_gaps_and_the_feed_fills_the_official_one(settings, monkeypatch):
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    e = Engine(settings)
    inst = Instrument("2303", "HAL", Segment.NSE_EQ, InstrumentKind.EQUITY, name="HAL", underlying="HAL")
    e.underlyings["HAL"] = inst
    e.aggregator.track(inst)
    open_ts = _store(e, "HAL", "2303", first_open=4730.0)
    for exch, recv, ltp in ((at("15:29:50", DAY - timedelta(days=3)), at("08:30"), 4700.0), (at("09:08:10"), at("09:08:11"), 4721.0),
                            (at("09:15:00"), at("09:15:00"), 4730.0)):
        await e._on_tick({"scrip_code": "2303", "ltp": ltp, "ts": exch, "exch_ts": exch, "recv_ts": recv, "open": 4721.0, "prev_close": 4700.0})
    c = e.trigger_context(Signal(strategy=StrategyKey.FUDKII, symbol="HAL", direction=Direction.BULLISH, ts=open_ts, entry=4726.0, stop=4718.0))
    assert c["gapDatr"] == pytest.approx(0.5), "the first trade's gap is kept, named as before"
    assert c["gapOfficial"] == pytest.approx(21 / 60) and c["gapOpenSrc"] == PREOPEN
    assert (c["officialOpen"], c["officialClose"], c["dAtr"], c["officialCloseEod"]) == (4721.0, 4700.0, 60.0, True)
    assert c["officialOpenTs"] == at("09:08:10") and c["gapRule"] == "official" and c["gapRuleRead"] == "official"
    assert rule_gap(c) == c["gapOfficial"], "the default: the gap rules read the official gap"
    bear = e.trigger_context(Signal(strategy=StrategyKey.FUDKII, symbol="HAL", direction=Direction.BEARISH, ts=open_ts, entry=4726.0, stop=4740.0))
    assert bear["gapOfficial"] == pytest.approx(-21 / 60), "signed the trigger's way"


@pytest.mark.asyncio
async def test_an_index_trigger_carries_the_official_gap_too(settings, monkeypatch):
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    e = Engine(settings)
    idx = Instrument("999920005", "BANKNIFTY", Segment.NSE_EQ, InstrumentKind.EQUITY, name="BANKNIFTY", underlying="BANKNIFTY")
    e.underlyings["BANKNIFTY"] = idx
    e.aggregator.track(idx)
    open_ts = _store(e, "BANKNIFTY", "999920005", first_open=4706.0)
    await e._on_tick({"scrip_code": "999920005", "ltp": 4700.0, "ts": at("15:30", DAY - timedelta(days=3)),
                      "exch_ts": at("15:30", DAY - timedelta(days=3)), "recv_ts": at("08:30")})
    await e._on_tick({"scrip_code": "999920005", "ltp": 4706.0, "ts": at("09:15:00"), "exch_ts": at("09:15:00"), "recv_ts": at("09:15:00")})
    c = e.trigger_context(Signal(strategy=StrategyKey.FUDKII, symbol="BANKNIFTY", direction=Direction.BULLISH, ts=open_ts, entry=4702.0, stop=4690.0))
    assert c["gapOpenSrc"] == FIRST_TRADE and c["gapOfficial"] == pytest.approx(0.1) and c["gapDatr"] == pytest.approx(0.1)


def test_a_process_that_never_saw_the_open_reads_the_first_bar(settings, monkeypatch):
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    e = Engine(settings)
    e.underlyings["HAL"] = Instrument("2303", "HAL", Segment.NSE_EQ, InstrumentKind.EQUITY, name="HAL", underlying="HAL")
    open_ts = _store(e, "HAL", "2303", first_open=4730.0)
    c = e.trigger_context(Signal(strategy=StrategyKey.FUDKII, symbol="HAL", direction=Direction.BULLISH, ts=open_ts, entry=4726.0, stop=4718.0))
    assert c["gapOpenSrc"] == FIRST_BAR and c["officialOpen"] == 4730.0 and c["gapOfficial"] == pytest.approx(c["gapDatr"])
    # official by default, but the open was not seen: the gap rules fall back to the first bar's gapDatr, and say so
    assert c["gapRule"] == "official" and c["gapRuleRead"] == FIRST_BAR and rule_gap(c) == c["gapDatr"] == 0.5


class _Clock:
    """The engine module's ``time`` with a fixed ``time()`` — the rest is the real module."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def time(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_the_engine_tells_the_capture_of_a_reconnect_after_the_open(settings, monkeypatch):
    """Review 19 Oct, end to end: the feed up at 00:45, asleep over the open, back at 10:30 — the reconnect's snapshot
    of HAL's 10:29:58 trade is not its open, and the trigger reads ``first_bar``."""
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    e = Engine(settings)
    await e.start()
    try:
        inst = Instrument("2303", "HAL", Segment.NSE_EQ, InstrumentKind.EQUITY, name="HAL", underlying="HAL")
        e.underlyings["HAL"] = inst
        e.aggregator.track(inst)
        open_ts = _store(e, "HAL", "2303", first_open=4730.0)
        await e._on_tick({"scrip_code": "2303", "ltp": 4700.0, "ts": at("15:29:50", DAY - timedelta(days=3)),
                          "exch_ts": at("15:29:50", DAY - timedelta(days=3)), "recv_ts": at("00:45")})
        monkeypatch.setattr(engine_mod, "time", _Clock(at("10:30:00")))
        await e._on_feed_connect(True)
        await e._on_tick({"scrip_code": "2303", "ltp": 4790.0, "ts": at("10:29:58"), "exch_ts": at("10:29:58"), "recv_ts": at("10:30:05")})
        assert e.official_opens.get("HAL", DAY) is None
        c = e.trigger_context(Signal(strategy=StrategyKey.FUDKII, symbol="HAL", direction=Direction.BULLISH, ts=open_ts, entry=4726.0, stop=4718.0))
        assert c["gapOpenSrc"] == FIRST_BAR and c["officialOpen"] == 4730.0
    finally:
        await e.stop()


@pytest.mark.asyncio
async def test_the_engine_passes_a_pooled_frame_as_pooled(settings, monkeypatch):
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    e = Engine(settings)
    inst = Instrument("2303", "HAL", Segment.NSE_EQ, InstrumentKind.EQUITY, name="HAL", underlying="HAL")
    e.underlyings["HAL"] = inst
    e.aggregator.track(inst)
    await e._on_tick({"scrip_code": "2303", "ltp": 4721.0, "ts": at("09:08:10"), "exch_ts": at("09:08:10"), "recv_ts": at("09:14:58"),
                      "pooled": True})
    assert e.official_opens.watch_from is None and e.official_opens.stats()["preopenPending"] == 1


def test_the_close_is_flagged_official_only_off_the_eod_row(settings, monkeypatch):
    """Review 19 Oct: ``officialCloseEod`` read ``is_official`` alone, true of an index's 08:59 pre-open row and of a
    provisional 09:15 one too (REST and complete) — neither is the closing-auction print. The EOD row is the 00:00 one."""
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    e = Engine(settings)
    e.underlyings["NIFTY"] = Instrument("999920000", "NIFTY", Segment.NSE_EQ, InstrumentKind.EQUITY, name="NIFTY", underlying="NIFTY")
    open_ts = _store(e, "NIFTY", "999920000", first_open=4730.0)
    sig = Signal(strategy=StrategyKey.FUDKII, symbol="NIFTY", direction=Direction.BULLISH, ts=open_ts, entry=4726.0, stop=4718.0)
    assert e.trigger_context(sig)["officialCloseEod"] is True
    fri = DAY - timedelta(days=3)
    rows = [b for b in e.store.bars("NIFTY", "1d", 40) if b.ts < int(at("00:00", fri))]
    e.store.seed("NIFTY", "1d", [*rows, UnifiedBar("NIFTY", "999920000", "1d", int(at("08:59", fri)), 4700, 4730, 4282, 4700, 0.0,
                                                   source=BarSource.REST, complete=True)])
    c = e.trigger_context(sig)
    assert c["officialCloseEod"] is False and c["officialClose"] == 4700, "recorded, not blocking"


def test_the_health_page_says_which_gap_the_rules_read(settings):
    e = Engine(settings)
    h = e.health_snapshot()
    assert h["gap"]["source"] == "official" and "settled" in h["gap"]["official_open"]
    assert Engine(settings.model_copy(update={"gap_source": "first_tick"})).health_snapshot()["gap"]["source"] == "first_tick"


# -- KN_GAP_SOURCE: official for every book (operator, 19 Oct), first_tick the switch-back ----------------------


def test_the_switch_defaults_to_the_official_open_and_takes_only_its_two_values(tmp_path):
    """Operator, 19 Oct: "did we not decide to use official open ... at 1 AM?" — the 1 AM definition for every book,
    confirmed 19 Oct (built defaulting to first_tick, awaiting that choice). ``first_tick`` stays as the switch-back."""
    assert Settings(_env_file=None, data_dir=tmp_path).gap_source == "official"
    assert Settings(_env_file=None, data_dir=tmp_path, gap_source="first_tick").gap_source == "first_tick"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, gap_source="pre_open")
    assert "KN_GAP_SOURCE" in known_env_keys(), "a known key: the boot's unknown-key check lets it through"


#: contexts as RT-Y / CT-Y read them, each with an official gap that would decide otherwise
CTXS = [
    {"share": 0.62, "names": 218, "openBar": True, "gapDatr": 0.29, "gapOfficial": 0.45, "relVol": 3.4, "pivotsAheadAtr": [], "atr30": 2.0},
    {"share": 0.62, "names": 218, "openBar": True, "gapDatr": 0.41, "gapOfficial": 0.12, "relVol": 3.4, "pivotsAheadAtr": [], "atr30": 2.0},
    {"share": 0.62, "names": 218, "openBar": True, "gapDatr": 0.76, "gapOfficial": 0.2, "relVol": 1.1,
     "pivotsAheadAtr": [["1mo.PIVOT", 0.4]], "atr30": 2.0},
    {"share": 0.44, "names": 218, "openBar": True, "gapDatr": 0.31, "gapOfficial": 0.8, "pivotsAheadAtr": [], "atr30": 2.0},
]


@pytest.mark.parametrize("ctx", CTXS)
def test_first_tick_reproduces_the_old_decisions_exactly(ctx):
    """Pinned: a context logged under the switch-back (``first_tick``) reads gapDatr — its decisions are those of the
    same context with no official gap in it at all (the engine before the official open), byte for byte."""
    before = {k: v for k, v in ctx.items() if k != "gapOfficial"}
    now = {**ctx, "gapRule": "first_tick", "gapOpenSrc": PREOPEN}
    assert rule_gap(now) == rule_gap(before) == ctx["gapDatr"] and rule_gap_read(now) == "first_tick"
    for lim in (RT_Y_LIMITS, RT_Y_F_LIMITS, CT_Y_LIMITS):
        assert rt_gate_reasons(now, lim) == rt_gate_reasons(before, lim)
        assert gap_switch(now, lim) == gap_switch(before, lim)


@pytest.mark.parametrize("ctx", CTXS)
def test_the_default_official_rule_falls_back_to_the_first_bar_only_where_the_open_was_not_seen(ctx):
    """Under the default the rules read gapOfficial; where the official open was not seen (``gapOpenSrc`` first_bar)
    they read the first bar's gapDatr — the decisions first_tick makes — and the reason says so."""
    seen = {**ctx, "gapRule": "official", "gapOpenSrc": PREOPEN}
    assert rule_gap(seen) == ctx["gapOfficial"] and rule_gap_read(seen) == "official"
    unseen = {**ctx, "gapRule": "official", "gapOpenSrc": FIRST_BAR}
    first = {**ctx, "gapRule": "first_tick", "gapOpenSrc": FIRST_BAR}
    assert rule_gap(unseen) == rule_gap(first) == ctx["gapDatr"] and rule_gap_read(unseen) == FIRST_BAR
    for lim in (RT_Y_LIMITS, RT_Y_F_LIMITS, CT_Y_LIMITS):
        got, old = rt_gate_reasons(unseen, lim), rt_gate_reasons(first, lim)
        assert [g for g, _ in got] == [g for g, _ in old], "the same gates as first_tick"
        assert all("first bar: official open not seen" in w for g, w in got if g == "open_gap")
        assert (gap_switch(unseen, lim) is None) == (gap_switch(first, lim) is None)
    assert "first bar: official open not seen" in rt_gate_reasons({**CTXS[1], "gapRule": "official", "gapOpenSrc": FIRST_BAR,
                                                                   "relVol": None}, RT_Y_LIMITS)[0][1]


def test_official_makes_the_same_rules_read_the_official_gap():
    ctx = {**CTXS[0], "gapRule": "official"}
    assert rule_gap(ctx) == 0.45
    assert gap_switch(ctx, RT_Y_LIMITS) is not None and gap_switch({**ctx, "gapRule": "first_tick"}, RT_Y_LIMITS) is None
    late = {**CTXS[1], "relVol": None}
    assert [g for g, _ in rt_gate_reasons(late, RT_Y_LIMITS)] == ["open_gap"]
    assert rt_gate_reasons({**late, "gapRule": "official"}, RT_Y_LIMITS) == [], "officially 0.12: no first-bar trap"


def test_ct_ys_gap_fade_reads_the_gap_the_switch_names(settings):
    e = Engine(settings)
    _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
    ctx = {"openBar": True, "gapDatr": 0.29, "gapOfficial": 0.45, "atr30": 2.0}
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "gapRule": "first_tick"}) is None
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), ctx) is None, "a context without the switch: the first tick"
    plan = e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "gapRule": "official"})
    assert plan is not None and plan["gapDatr"] == 0.45 and plan["gapRule"] == "official" and plan["gapRuleRead"] == "official"
    assert e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "gapRule": "official", "gapOpenSrc": FIRST_BAR}) is None, (
        "the open not seen: the first bar's 0.29, as first_tick")
    fb = e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "gapDatr": 0.45, "gapOfficial": 0.45, "gapRule": "official",
                                                      "gapOpenSrc": FIRST_BAR})
    assert fb is not None and fb["gapDatr"] == 0.45 and fb["gapRuleRead"] == FIRST_BAR, "flagged on the plan"
    first = e.gap_fade_plan(_trigger(Direction.BULLISH), {**ctx, "gapDatr": 0.45, "gapOfficial": 0.1, "gapRule": "first_tick"})
    assert first is not None and "gapRule" not in first and first["gapDatr"] == 0.45, "the default plan, as before"


@pytest.mark.asyncio
async def test_the_switch_is_stamped_on_every_trigger_as_it_is_logged(settings, monkeypatch):
    monkeypatch.setattr(engine_mod, "ist_today", lambda: DAY)
    for rule in ("official", "first_tick"):
        e = Engine(settings.model_copy(update={"gap_source": rule}))
        e.underlyings["HAL"] = Instrument("2303", "HAL", Segment.NSE_EQ, InstrumentKind.EQUITY, name="HAL", underlying="HAL")
        open_ts = _store(e, "HAL", "2303", first_open=4730.0)
        for px, exch in ((4721.0, at("09:08:10")), (4730.0, at("09:15:00"))):  # the pre-open print, settled at 09:15
            e.official_opens.note("HAL", px, exch, exch + 1)
        c = e.trigger_context(Signal(strategy=StrategyKey.FUDKII, symbol="HAL", direction=Direction.BULLISH, ts=open_ts, entry=4726.0,
                                     stop=4718.0))
        assert c["gapRule"] == rule == c["gapRuleRead"] and c["gapOpenSrc"] == PREOPEN, rule
        assert rule_gap(c) == (c["gapOfficial"] if rule == "official" else c["gapDatr"]), rule
        assert (c["gapOfficial"], c["gapDatr"]) == (pytest.approx(21 / 60), 0.5), "both logged either way"


@pytest.mark.asyncio
async def test_ct_y_decides_on_the_official_gap_by_default_and_on_the_first_tick_on_the_switch_back(settings):
    """The whole counter path on a trigger whose first trade gapped 0.29 and whose official open gapped 0.45: under
    ``official`` (the default from 21 Oct) CT-Y gap-fades it; under ``first_tick`` it does not — as before 21 Oct."""
    assert settings.gap_source == "official"
    for rule, faded in (("first_tick", False), ("official", True)):
        e = Engine(settings)
        await e.start()
        try:
            _seed(e, zones=[Zone(184.0, 6.0, ["1d.PIVOT"])])
            trig = _trigger(Direction.BULLISH)
            e._breadth_at[trig.signal_id] = {"share": 0.7, "names": 220, "openBar": True, "gapDatr": 0.29, "gapOfficial": 0.45,
                                             "gapRule": rule, "atr30": 2.0}
            handled: list[Signal] = []

            async def capture(sig, bar, *, adopt=True, _h=handled):
                _h.append(sig)

            async def no_legs(underlying, bar, **_kw):
                return []

            e._handle_signal = capture  # type: ignore[method-assign]
            e._counter_legs = no_legs  # type: ignore[method-assign]
            await e._handle_counter(trig, None)  # type: ignore[arg-type]
            assert any(s.strategy is StrategyKey.FUDKII_CT_Y for s in handled) is faded, rule
        finally:
            await e.stop()
