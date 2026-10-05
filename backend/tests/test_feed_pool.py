"""The feed pool (operator, 2026-10-05: "whichever subscribes, must put all data in a pool that everyone can have
access to without waiting ... no engine will have to duplicate subscription or fetch data again and again").

The broker sends a code's frame once, to the one socket. Before the pool, the engine that subscribed second
discarded it as not yet wanted, never got it again (the hub does not re-subscribe a held code) and waited for
the code's next change: BAJFINANCE 1010 CE on 5 Oct — phase 35 asked at 09:45:04.3, the broker's first frame
(18.00 / 17.90 / 18.05) came at 04.432, phase 34 asked at 04.56, held only its REST price (no bid or ask) and
gave the strike up at 06.6; the next frame came at 09:45:17. Now each side keeps the last frame of every code
on the socket and reads it the moment it subscribes, with the time it arrived."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.instrument.select import Quote
from kotsin_nse.venue.fivepaisa.hub import HubServer
from kotsin_nse.venue.fivepaisa.ws import FivePaisaFeed

RELIANCE = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY)
CALL = Instrument("76201", "BAJFINANCE", Segment.NSE_FO, InstrumentKind.OPTION)
FUT = Instrument("35001", "BAJFINANCE", Segment.NSE_FO, InstrumentKind.FUTURE)


class Broker:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, text: str) -> None:
        self.frames.append(json.loads(text))

    async def close(self) -> None:
        pass

    def subs(self, code: str) -> int:
        return sum(1 for f in self.frames if f["Operation"] == "Subscribe" for e in f["MarketFeedData"] if e["ScripCode"] == code)


def _tick(code: str, ltp: float, bid: float = 0.0, ask: float = 0.0) -> str:
    return json.dumps([{"Token": int(code), "Exch": "N", "ExchType": "D", "LastRate": ltp, "BidRate": bid, "OffRate": ask,
                        "TickDt": "/Date(1791173704000)/"}])


def _depth(code: str) -> str:
    return json.dumps({"Token": int(code), "Details": [{"Price": 17.9, "Quantity": 750, "BbBuySellFlag": 66},
                                                       {"Price": 18.05, "Quantity": 1500, "BbBuySellFlag": 83}]})


def _oi(code: str) -> str:
    return json.dumps([{"Token": int(code), "OpenInterest": 62178000, "TickDt": "/Date(1791173704000)/"}])


def _feed(seen: list[dict], depth: list[dict] | None = None, oi: list[dict] | None = None) -> FivePaisaFeed:
    async def on_tick(t: dict) -> None:
        seen.append(t)

    async def on_depth(d: dict) -> None:
        (depth if depth is not None else seen).append(d)

    async def on_oi(o: dict) -> None:
        (oi if oi is not None else seen).append(o)

    return FivePaisaFeed(Settings(_env_file=None), None, on_tick=on_tick, on_depth=on_depth, on_oi=on_oi)  # type: ignore[arg-type]


async def _until(cond, within: float = 3.0) -> None:
    end = time.monotonic() + within
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_frame_only_the_twin_asked_for_is_pooled_and_read_at_once_when_this_engine_subscribes():
    seen: list[dict] = []
    a = _feed(seen)
    broker = Broker()
    a._ws = broker
    await a.pin("twin-1", "mf", "s", [CALL])  # phase 35 asked first: held at the broker for it
    assert broker.subs(CALL.scrip_code) == 1
    await a._handle(_tick(CALL.scrip_code, 18.0, 17.9, 18.05))  # the broker's first frame
    arrived = a._pool[("mf", CALL.scrip_code)][1]
    assert seen == [], "not wanted yet: kept in the pool, not dispatched"

    await a.subscribe("mf", [CALL])  # phase 34 asks
    assert len(seen) == 1 and seen[0]["pooled"] is True
    assert (seen[0]["bid"], seen[0]["ask"]) == (17.9, 18.05)
    assert seen[0]["recv_ts"] == arrived, "the frame keeps the time it arrived: its age is its own"
    assert broker.subs(CALL.scrip_code) == 1, "nothing is subscribed twice at the broker"
    assert a.subscribed_since("mf", CALL.scrip_code) <= arrived, "the broker has streamed it since the twin's pin"
    assert a.health.ticks == 0 and a.pool_stats() == {"codes": 1, "primed": 1}

    await a._handle(_tick(CALL.scrip_code, 18.2, 18.3, 18.45))
    assert len(seen) == 2 and "pooled" not in seen[1], "and from then on it is read live"


@pytest.mark.asyncio
async def test_a_code_nobody_streams_goes_to_the_broker_and_primes_nothing_and_oi_is_never_primed():
    seen: list[dict] = []
    oi: list[dict] = []
    a = _feed(seen, oi=oi)
    broker = Broker()
    a._ws = broker
    await a.subscribe("mf", [CALL])
    assert seen == [] and broker.subs(CALL.scrip_code) == 1
    t = time.time()
    assert t - 1 <= a.subscribed_since("mf", CALL.scrip_code) <= t

    await a.pin("twin-1", "oi", "s", [FUT])
    await a._handle(_oi(FUT.scrip_code))
    await a.subscribe("oi", [FUT])
    assert oi == [], "an OI level is read as it prints, never replayed"


@pytest.mark.asyncio
async def test_a_book_is_primed_too():
    seen: list[dict] = []
    books: list[dict] = []
    a = _feed(seen, depth=books)
    a._ws = Broker()
    await a.pin("twin-1", "md", "s", [CALL])
    await a._handle(_depth(CALL.scrip_code))
    await a.subscribe("md", [CALL])
    assert len(books) == 1 and books[0]["pooled"] is True and books[0]["bids"] == [(17.9, 750)]


@pytest.fixture
async def hub():
    a_seen: list[dict] = []
    a = _feed(a_seen)
    broker = Broker()
    a._ws = broker
    await a.subscribe("mf", [RELIANCE])
    server = HubServer(a, "127.0.0.1", 0)
    await server.start()
    server.state(True)
    tasks: list[asyncio.Task] = []

    async def twin(*subs: Instrument) -> tuple[FivePaisaFeed, list[dict]]:
        seen: list[dict] = []
        b = _feed(seen)
        b.hub = ("127.0.0.1", server.port)
        if subs:
            await b.subscribe("mf", list(subs))
        tasks.append(asyncio.create_task(b._connect_and_read()))
        await _until(lambda: b._ws is not None)
        return b, seen

    yield a, broker, server, twin
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await server.stop()


@pytest.mark.asyncio
async def test_the_reading_side_pools_what_the_hub_engine_asked_for_and_reads_it_when_it_subscribes(hub):
    a, broker, _server, twin = hub
    b, b_seen = await twin(RELIANCE)
    await a.subscribe("mf", [CALL])  # phase 34 asked first (NAUKRI, 5 Oct 09:45:07)
    await a._handle(_tick(CALL.scrip_code, 18.0, 17.9, 18.05))
    await _until(lambda: ("mf", CALL.scrip_code) in b._pool)
    assert b_seen == [], "not wanted by the twin yet"
    await b.subscribe("mf", [CALL])
    await _until(lambda: any(t["scrip_code"] == CALL.scrip_code for t in b_seen))
    got = [t for t in b_seen if t["scrip_code"] == CALL.scrip_code]
    assert got[0]["pooled"] is True and got[0]["bid"] == 17.9
    await asyncio.sleep(0.1)
    assert broker.subs(CALL.scrip_code) == 1, "the hub held it already: not subscribed at the broker again"


@pytest.mark.asyncio
async def test_a_twin_that_joins_late_is_answered_from_the_hubs_pool_with_since_when_it_streamed(hub):
    a, _broker, server, twin = hub
    await a.subscribe("mf", [CALL])
    since = a.subscribed_since("mf", CALL.scrip_code)
    await a._handle(_tick(CALL.scrip_code, 18.0, 17.9, 18.05))
    arrived = a._pool[("mf", CALL.scrip_code)][1]
    c, c_seen = await twin()  # (re)connected after the frame passed: its own pool never saw it
    assert ("mf", CALL.scrip_code) not in c._pool
    await c.subscribe("mf", [CALL])
    await _until(lambda: any(t["scrip_code"] == CALL.scrip_code for t in c_seen))
    got = next(t for t in c_seen if t["scrip_code"] == CALL.scrip_code)
    assert got["pooled"] is True and abs(got["recv_ts"] - arrived) < 1e-5, "the hub's arrival time, not the twin's"
    assert abs(c.subscribed_since("mf", CALL.scrip_code) - since) < 1e-5, "live since the hub's own subscription"
    assert server.stats()["answered_from_pool"] == 1


@pytest.mark.asyncio
async def test_the_engine_takes_a_pooled_price_at_its_own_age_over_a_rest_price_with_no_book(settings):
    e = Engine(settings)
    trades: list[dict] = []

    async def on_tick(t: dict) -> None:
        trades.append(t)

    e.aggregator.on_tick = on_tick  # type: ignore[method-assign]
    code = CALL.scrip_code
    arrived = time.time() - 0.13
    # the REST price the engine fetched itself, 0.13 s after the frame: a last price, no bid or ask
    e.quotes[code] = Quote(ltp=18.0, bid=0.0, ask=0.0, ts=arrived + 0.13, src="snapshot")
    await e._on_tick({"scrip_code": code, "ltp": 18.0, "bid": 17.9, "ask": 18.05, "recv_ts": arrived, "ts": arrived, "pooled": True})
    q = e.quotes[code]
    assert (q.bid, q.ask, q.ts, q.src) == (17.9, 18.05, arrived, "feed") and q.priced
    assert trades == [] and e.pool_quotes == 1, "a price, never a trade: no bar sees it again"

    # a newer feed quote is never replaced by an older pooled one
    e.quotes[code] = Quote(ltp=18.2, bid=18.3, ask=18.45, ts=time.time())
    await e._on_tick({"scrip_code": code, "ltp": 18.0, "bid": 17.9, "ask": 18.05, "recv_ts": arrived, "ts": arrived, "pooled": True})
    assert e.quotes[code].ltp == 18.2 and e.pool_quotes == 1

    # a REST price that is a NEWER trade than the pooled frame: the book is taken, its price marked known-old
    e.quotes[code] = Quote(ltp=18.4, bid=0.0, ask=0.0, ts=time.time(), src="snapshot")
    await e._on_tick({"scrip_code": code, "ltp": 18.0, "bid": 17.9, "ask": 18.05, "recv_ts": arrived, "ts": arrived, "pooled": True})
    assert e.quotes[code].priced and code in e._quote_outdated
