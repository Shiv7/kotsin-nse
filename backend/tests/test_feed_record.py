"""The feed's record set — RECORD-ONLY (operator, 2026-10-19: record the 5-read and the strike walls "for research only").

The record band's codes (KN_RECORD_BAND) stream at the broker beside the engine's own, on the one socket, and every
row the socket carries is handed to the recorder (``on_record``). A row of a code only the recorder asked for never
reaches the engine's handlers — its quotes, OI, books, bars, tape or strike chooser. The one effect, behind the
switch: a code the band holds is held at the broker the way a code the twin holds is (5 Oct pool) — an engine
subscribe is not sent again and reads the pool's last frame at once, live since the band's subscribe.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.venue.fivepaisa.hub import HubServer

from .test_feed_pool import RELIANCE, Broker, _feed, _oi, _tick, _until

BAND = Instrument("88001", "PNB", Segment.NSE_FO, InstrumentKind.OPTION)
BAND2 = Instrument("88002", "PNB", Segment.NSE_FO, InstrumentKind.OPTION)


def _tap(feed) -> list[tuple[str, str, bool]]:
    got: list[tuple[str, str, bool]] = []
    feed.on_record = lambda ch, row, arrived, pooled: got.append((ch, str(row.get("Token") or row.get("ScripCode")), pooled))
    return got


@pytest.mark.asyncio
async def test_a_record_only_frame_reaches_the_recorder_and_never_the_engine():
    seen: list[dict] = []
    ois: list[dict] = []
    a = _feed(seen, oi=ois)
    a._ws = broker = Broker()
    tapped = _tap(a)
    await a.subscribe("mf", [RELIANCE])
    await a.subscribe_record("mf", [BAND])
    await a.subscribe_record("oi", [BAND])
    assert broker.subs(BAND.scrip_code) == 2, "streamed at the broker: prices and OI"
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    await a._handle(_oi(BAND.scrip_code))
    await a._handle(_tick(RELIANCE.scrip_code, 1400.0, 1399.9, 1400.1))
    assert [t["scrip_code"] for t in seen] == [RELIANCE.scrip_code] and ois == [], "the engine read only its own"
    assert tapped == [("mf", BAND.scrip_code, False), ("oi", BAND.scrip_code, False), ("mf", RELIANCE.scrip_code, False)]
    assert a.health.subscriptions == {"mf": 1, "md": 0, "oi": 0}, "the engine's own set keeps its meaning"
    assert a.record_counts()["mf"] == {"record": 1, "extra": 1, "socket": 2}


@pytest.mark.asyncio
async def test_a_frame_nobody_wants_still_reaches_the_engine_as_before_and_md_never_reaches_the_recorder():
    """The serving side's old rule stands: a code neither the engine nor a twin holds (a late frame after an
    unsubscribe) is dispatched — the record set only takes ITS codes away."""
    seen: list[dict] = []
    books: list[dict] = []
    a = _feed(seen, depth=books)
    a._ws = Broker()
    tapped = _tap(a)
    await a._handle(_tick("99999", 1.0))
    await a._handle(json.dumps({"Token": 99999, "Details": [{"Price": 1.0, "Quantity": 5, "BbBuySellFlag": 66}]}))
    assert len(seen) == 1 and len(books) == 1 and tapped == [("mf", "99999", False)]


@pytest.mark.asyncio
async def test_an_engine_subscribe_of_a_band_code_is_not_sent_again_is_primed_and_live_since_the_band():
    seen: list[dict] = []
    a = _feed(seen)
    a._ws = broker = Broker()
    await a.subscribe_record("mf", [BAND])
    band_since = a._record_since["mf"][BAND.scrip_code]
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    assert seen == []
    await asyncio.sleep(0.01)
    await a.subscribe("mf", [BAND])  # a book prices a trigger on this strike
    assert broker.subs(BAND.scrip_code) == 1, "held at the broker already: not sent again"
    assert len(seen) == 1 and seen[0]["pooled"] is True and seen[0]["bid"] == 4.1
    assert a.subscribed_since("mf", BAND.scrip_code) == band_since
    await a._handle(_tick(BAND.scrip_code, 4.25, 4.2, 4.3))
    assert len(seen) == 2 and "pooled" not in seen[1], "from now on the engine's own"


@pytest.mark.asyncio
async def test_unsubscribes_never_drop_a_code_the_other_side_holds():
    a = _feed([])
    a._ws = broker = Broker()

    def unsubs(code: str) -> int:
        return sum(1 for f in broker.frames if f["Operation"] == "Unsubscribe" for e in f["MarketFeedData"] if e["ScripCode"] == code)

    await a.subscribe_record("mf", [BAND])
    await a.subscribe("mf", [BAND])
    await a.unsubscribe("mf", [BAND])
    assert unsubs(BAND.scrip_code) == 0, "the band still wants it"
    await a.subscribe("mf", [BAND2])
    await a.subscribe_record("mf", [BAND2])
    assert broker.subs(BAND2.scrip_code) == 1
    await a.unsubscribe_record("mf", [BAND2])
    assert unsubs(BAND2.scrip_code) == 0 and a.is_subscribed("mf", BAND2.scrip_code), "the engine still wants it"
    await a.unsubscribe_record("mf", [BAND])
    assert unsubs(BAND.scrip_code) == 1, "neither wants it now"


@pytest.mark.asyncio
async def test_a_reconnect_subscribes_the_engines_set_the_record_set_and_the_twins():
    a = _feed([])
    a._ws = broker = Broker()
    await a.subscribe("mf", [RELIANCE])
    await a.subscribe_record("mf", [BAND])
    await a.subscribe_record("oi", [BAND2])
    await a.pin("twin-1", "mf", "s", [BAND2])
    broker.frames.clear()
    await a._subscribe_everything()
    sent = {(f["Method"], e["ScripCode"]) for f in broker.frames for e in f["MarketFeedData"]}
    assert sent == {("MarketFeedV3", RELIANCE.scrip_code), ("MarketFeedV3", BAND.scrip_code), ("MarketFeedV3", BAND2.scrip_code),
                    ("GetScripInfoForFuture", BAND2.scrip_code)}


@pytest.fixture
async def hub():
    a_seen: list[dict] = []
    a = _feed(a_seen)
    broker = Broker()
    a._ws = broker
    server = HubServer(a, "127.0.0.1", 0)
    await server.start()
    server.state(True)
    tasks: list[asyncio.Task] = []

    async def twin() -> tuple[object, list[dict]]:
        seen: list[dict] = []
        b = _feed(seen)
        b.hub = ("127.0.0.1", server.port)
        tasks.append(asyncio.create_task(b._connect_and_read()))
        await _until(lambda: b._ws is not None)
        return b, seen

    yield a, a_seen, broker, server, twin
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await server.stop()


@pytest.mark.asyncio
async def test_the_twins_record_band_is_pinned_at_the_hub_and_reaches_neither_engines_handlers(hub):
    a, a_seen, broker, server, twin = hub
    a_tap = _tap(a)
    b, b_seen = await twin()
    b_tap = _tap(b)
    await b.subscribe_record("mf", [BAND])
    await _until(lambda: broker.subs(BAND.scrip_code) == 1)
    assert server.stats()["pinned"]["mf"] == 1
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    await _until(lambda: any(c == BAND.scrip_code for _, c, _ in b_tap))
    await asyncio.sleep(0.05)
    assert a_seen == [] and b_seen == [], "the twin's band code reaches the serving engine's handlers no more than its own"
    assert ("mf", BAND.scrip_code, False) in a_tap, "the serving engine's recorder sees it (one socket, every row)"


@pytest.mark.asyncio
async def test_a_twin_asking_for_the_hub_engines_band_code_is_answered_from_the_pool(hub):
    a, _a_seen, broker, server, twin = hub
    await a.subscribe_record("mf", [BAND])
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    b, b_seen = await twin()
    await b.subscribe("mf", [BAND])
    await _until(lambda: any(t["scrip_code"] == BAND.scrip_code for t in b_seen))
    assert b_seen[0]["pooled"] is True and broker.subs(BAND.scrip_code) == 1
    assert server.stats()["answered_from_pool"] == 1


@pytest.mark.asyncio
async def test_a_recorder_fault_never_costs_the_engine_its_frame():
    seen: list[dict] = []
    a = _feed(seen)
    a._ws = Broker()
    await a.subscribe("mf", [RELIANCE])

    def boom(*_a) -> None:
        raise RuntimeError("recorder bug")

    a.on_record = boom
    await a._handle(_tick(RELIANCE.scrip_code, 1400.0))
    assert len(seen) == 1 and a.record_tap_errors == 1
