"""One 5paisa socket for two engines (operator, 2026-10-04: "can we not treat it like a single pipeline
feeding 2 engines/buckets simultaneously" — "yes go ahead. build"). Phase 34 keeps the broker socket and
passes every raw message on before any work of its own; phase 35 reads them over loopback, decodes them
with its own code, and has its subscriptions held at the broker for it. Real sockets, a fake broker."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.venue.fivepaisa import hub as hub_mod
from kotsin_nse.venue.fivepaisa.hub import HubServer, read_hub_config
from kotsin_nse.venue.fivepaisa.ws import FivePaisaFeed

RELIANCE = Instrument("2885", "RELIANCE", Segment.NSE_EQ, InstrumentKind.EQUITY)
TCS = Instrument("11536", "TCS", Segment.NSE_EQ, InstrumentKind.EQUITY)
OPT = Instrument("45678", "RELIANCE", Segment.NSE_FO, InstrumentKind.OPTION)


class Broker:
    """The broker socket as the serving feed sees it: records every frame sent."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, text: str) -> None:
        self.frames.append(json.loads(text))

    async def close(self) -> None:
        pass

    def subs(self, op: str = "Subscribe", method: str = "MarketFeedV3") -> list[str]:
        return [e["ScripCode"] for f in self.frames if f["Operation"] == op and f["Method"] == method for e in f["MarketFeedData"]]


def _tick(code: str, ltp: float) -> str:
    return json.dumps([{"Token": int(code), "Exch": "N", "ExchType": "C", "LastRate": ltp, "TickDt": "/Date(1791100000000)/"}])


def _feed(seen: list[dict]) -> FivePaisaFeed:
    async def on_tick(t: dict) -> None:
        seen.append(t)

    return FivePaisaFeed(Settings(_env_file=None), None, on_tick=on_tick)  # type: ignore[arg-type]


async def _until(cond, within: float = 3.0) -> None:
    end = time.monotonic() + within
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


@pytest.fixture
async def pair():
    a_seen: list[dict] = []
    b_seen: list[dict] = []
    a, b = _feed(a_seen), _feed(b_seen)
    broker = Broker()
    a._ws = broker
    await a.subscribe("mf", [RELIANCE])
    server = HubServer(a, "127.0.0.1", 0)
    await server.start()
    server.state(True)
    b.hub = ("127.0.0.1", server.port)
    await b.subscribe("mf", [RELIANCE, TCS])
    task = asyncio.create_task(b._connect_and_read())
    yield a, b, broker, server, a_seen, b_seen, task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()


@pytest.mark.asyncio
async def test_the_twin_gets_every_message_and_each_engine_reads_only_what_it_subscribed(pair):
    a, b, broker, server, a_seen, b_seen, _ = pair
    await _until(lambda: "11536" in broker.subs())
    assert broker.subs() == ["2885", "11536"], "TCS subscribed at the broker for the twin; RELIANCE not sent twice"
    await _until(lambda: b.health.connected)
    for code, px in (("2885", 1500.0), ("11536", 3100.0)):
        await a._handle(_tick(code, px))
    await _until(lambda: len(b_seen) == 2)
    assert [t["scrip_code"] for t in a_seen] == ["2885"], "phase 34 never reads the twin's TCS"
    assert [(t["scrip_code"], t["ltp"]) for t in b_seen] == [("2885", 1500.0), ("11536", 3100.0)]
    assert b_seen[0]["recv_ts"] >= a_seen[0]["recv_ts"], "the twin ages a price from when IT received it"
    st = b.hub_stats()
    assert st is not None and st["messages"] == 2 and st["lag_max_ms"] < 500, "the loopback hop"
    assert server.stats()["pinned"] == {"mf": 2, "md": 0, "oi": 0}


@pytest.mark.asyncio
async def test_neither_side_drops_a_code_the_other_still_wants(pair):
    a, b, broker, _server, *_ = pair
    await _until(lambda: "11536" in broker.subs())
    await a.unsubscribe("mf", [RELIANCE])
    assert broker.subs("Unsubscribe") == [], "the twin still reads RELIANCE: it stays at the broker"
    await b.unsubscribe("mf", [TCS])
    await _until(lambda: broker.subs("Unsubscribe") == ["11536"])
    await b.unsubscribe("mf", [RELIANCE])
    await _until(lambda: broker.subs("Unsubscribe") == ["11536", "2885"])  # now nobody wants it


@pytest.mark.asyncio
async def test_a_twin_that_leaves_is_unpinned_and_a_reconnect_subscribes_both_sides(pair):
    a, _b, broker, server, _, _, task = pair
    await _until(lambda: "11536" in broker.subs())
    broker.frames.clear()
    await a._subscribe_everything()  # the broker socket reconnects
    assert sorted(broker.subs()) == ["11536", "2885"], "its own codes AND the twin's"
    broker.frames.clear()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await _until(lambda: server.stats()["twins"] == 0)
    await _until(lambda: broker.subs("Unsubscribe") == ["11536"])  # the twin's own code leaves with it
    assert server.stats()["pinned"] == {"mf": 0, "md": 0, "oi": 0} and a.is_subscribed("mf", "2885")


@pytest.mark.asyncio
async def test_the_twin_follows_the_hubs_broker_socket_and_a_slow_twin_is_dropped_not_waited_for(pair, monkeypatch):
    a, b, _broker, server, *_ = pair
    await _until(lambda: b.health.connected)
    server.state(False)
    await _until(lambda: not b.health.connected)
    server.state(True)
    await _until(lambda: b.health.connected)
    monkeypatch.setattr(hub_mod, "MAX_CLIENT_BUFFER", -1)  # every twin is "too far behind"
    t0 = time.perf_counter()
    await a._handle(_tick("2885", 1501.0))
    assert time.perf_counter() - t0 < 0.05 and server.dropped == 1, "dropped, never awaited"


def test_the_hub_is_loopback_only_and_an_engine_is_one_side(tmp_path):
    def cfg(d):
        (tmp_path / "engine.json").write_text(json.dumps({"feed_hub": d}))
        return read_hub_config(tmp_path)

    assert read_hub_config(tmp_path).serve is None, "no file: the engine's own socket"
    assert cfg({"serve": "127.0.0.1:8510"}).serve == ("127.0.0.1", 8510)
    assert cfg({"connect": "127.0.0.1:8510"}).connect == ("127.0.0.1", 8510)
    assert cfg({"serve": "0.0.0.0:8510"}).serve is None, "never on the network"
    both = cfg({"serve": "127.0.0.1:8510", "connect": "127.0.0.1:8511"})
    assert both.serve is None and both.connect is None
