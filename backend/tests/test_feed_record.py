"""The feed's record set — RECORD-ONLY (operator, 2026-10-19: record the 5-read and the strike walls "for research only").

The record band's codes (KN_RECORD_BAND) stream at the broker beside the engine's own, on the one socket, and every
row the socket carries is handed to the recorder (``on_record``). A row of a code only the recorder asked for never
reaches the engine's handlers — its quotes, OI, books, bars, tape or strike chooser.

When the engine itself subscribes a code only a record set streams (this engine's band or the twin's), it is answered
as the broker answers a fresh subscribe — the code's current state, a plain frame arriving now, live from now — not
primed with the pool's aged frame as a code the twin's ENGINE holds is (5 Oct). So a book reads what it would without
the band, up to one broker round trip sooner (review, 20 Oct). The twin's band is pinned at the hub apart from its
engine's codes (the hub's ``Record`` marker): it never makes a code read as the twin's engine holding it.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.venue.fivepaisa.hub import HubServer
from kotsin_nse.venue.fivepaisa.ws import build_frame

from .test_feed_pool import RELIANCE, Broker, _feed, _oi, _tick, _until

BAND = Instrument("88001", "PNB", Segment.NSE_FO, InstrumentKind.OPTION)
BAND2 = Instrument("88002", "PNB", Segment.NSE_FO, InstrumentKind.OPTION)
#: what differs between two runs of the same frames: the clock
VOLATILE = ("recv_ts", "dispatch_ts")


def _tap(feed) -> list[tuple[str, str, bool]]:
    got: list[tuple[str, str, bool]] = []
    feed.on_record = lambda ch, row, arrived, pooled: got.append((ch, str(row.get("Token") or row.get("ScripCode")), pooled))
    return got


def _up(feed) -> None:
    """The broker socket up for a minute already (what ``_connect_and_read`` sets)."""
    feed.health.connected, feed.health.connected_since = True, time.time() - 60


def _same(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in VOLATILE}


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
    assert all("Record" not in f for f in broker.frames), "the hub's marker never reaches the broker"
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
async def test_the_recorder_gets_the_row_after_the_engine():
    """The tap runs once the engine has the whole message (review, 20 Oct: off the engine's path)."""
    order: list[str] = []
    a = _feed([])

    async def on_tick(t: dict) -> None:
        order.append("engine")

    a.on_tick = on_tick
    a.on_record = lambda *_a: order.append("recorder")
    a._ws = Broker()
    await a.subscribe("mf", [RELIANCE])
    await a._handle(_tick(RELIANCE.scrip_code, 1400.0))
    assert order == ["engine", "recorder"]


@pytest.mark.asyncio
async def test_an_engine_subscribe_of_a_band_code_is_a_fresh_snapshot_live_from_now():
    seen: list[dict] = []
    a = _feed(seen)
    a._ws = broker = Broker()
    _up(a)
    await a.subscribe_record("mf", [BAND])
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    assert seen == []
    await asyncio.sleep(0.01)
    t0 = time.time()
    await a.subscribe("mf", [BAND])  # a book prices a trigger on this strike
    assert broker.subs(BAND.scrip_code) == 1, "streaming at the broker already: not sent again"
    assert len(seen) == 1 and "pooled" not in seen[0] and seen[0]["bid"] == 4.1
    assert seen[0]["recv_ts"] >= t0, "a frame arriving now, as the broker's snapshot would"
    assert a.subscribed_since("mf", BAND.scrip_code) >= t0, "live from the engine's own subscribe, never the band's"
    assert a.pool_snapshots == 1 and a.pool_primed == 0
    await a._handle(_tick(BAND.scrip_code, 4.25, 4.2, 4.3))
    assert len(seen) == 2 and "pooled" not in seen[1], "from now on the engine's own"


@pytest.mark.asyncio
async def test_a_book_reads_the_same_with_the_band_on_and_off():
    """The record-only proof of the band (review, 20 Oct): the strike a book subscribes reaches it with the same price,
    book and OI print, as a fresh frame, live from its own subscribe — whether the band streamed it before or the
    broker answers the engine's own subscribe with its snapshot."""
    state = _tick(BAND.scrip_code, 4.2, 4.1, 4.3)  # the strike's current state at the broker
    oi_state = _oi(BAND.scrip_code)

    async def run(band: bool) -> tuple[Any, ...]:
        seen: list[dict] = []
        ois: list[dict] = []
        a = _feed(seen, oi=ois)
        a._ws = broker = Broker()
        _up(a)
        if band:
            await a.subscribe_record("mf", [BAND])
            await a.subscribe_record("oi", [BAND])
            await a._handle(_tick(BAND.scrip_code, 4.0, 3.9, 4.1))  # what the band streamed before
            await a._handle(state)
            await a._handle(oi_state)
        t0 = time.time()
        await a.subscribe("mf", [BAND])
        await a.subscribe("oi", [BAND])
        if not band:
            await a._handle(state)  # the broker's snapshots of the engine's fresh subscribes
            await a._handle(oi_state)
        fresh = all(x["recv_ts"] >= t0 for x in (*seen, *ois))
        live = all(a.subscribed_since(ch, BAND.scrip_code) >= t0 for ch in ("mf", "oi"))
        return [_same(t) for t in seen], [_same(o) for o in ois], fresh, live, broker.subs(BAND.scrip_code)

    off = await run(False)
    on = await run(True)
    assert on[:4] == off[:4] and on[2] and on[3]
    assert len(on[0]) == 1 and len(on[1]) == 1 and "pooled" not in on[0][0]
    assert off[4] == on[4] == 2, "two subscribes at the broker either way: the engine's, or the band's"


@pytest.mark.asyncio
async def test_no_stand_in_snapshot_while_the_socket_is_down_the_reconnects_own_reaches_the_engine():
    seen: list[dict] = []
    a = _feed(seen)
    a._ws = Broker()
    _up(a)
    await a.subscribe_record("mf", [BAND])
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    a._ws, a.health.connected = None, False  # the socket drops
    await a.subscribe("mf", [BAND])
    assert seen == [], "the pool's frame is from before the drop: not a current state"
    a._ws = broker = Broker()
    _up(a)
    await a._subscribe_everything()
    assert broker.subs(BAND.scrip_code) == 1
    await a._handle(_tick(BAND.scrip_code, 4.3, 4.2, 4.4))  # the reconnect's snapshot
    assert len(seen) == 1 and seen[0]["bid"] == 4.2


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
async def test_a_reconnect_subscribes_the_engines_codes_first_then_the_record_sets():
    """Engines first across every channel (re-check, 20 Oct): a book's depth and OI never wait behind the bands' prices."""
    a = _feed([])
    a._ws = broker = Broker()
    await a.subscribe("mf", [RELIANCE])
    await a.subscribe("md", [RELIANCE])
    await a.subscribe("oi", [BAND])  # the engine's OI of a strike its band also streams: sent once, with the engine's
    await a.subscribe_record("mf", [BAND])
    await a.subscribe_record("oi", [BAND, BAND2])
    await a.pin("twin-1", "mf", "s", [BAND2])
    await a.pin("twin-1", "oi", "s", [Instrument("88004", "PNB", Segment.NSE_FO, InstrumentKind.OPTION)])
    await a.pin("twin-1", "mf", "s", [Instrument("88003", "PNB", Segment.NSE_FO, InstrumentKind.OPTION)], record=True)
    broker.frames.clear()
    await a._subscribe_everything()
    sent = [(f["Method"], e["ScripCode"]) for f in broker.frames for e in f["MarketFeedData"]]
    assert sent == [("MarketFeedV3", RELIANCE.scrip_code), ("MarketFeedV3", BAND2.scrip_code),
                    ("MarketDepthService", RELIANCE.scrip_code),
                    ("GetScripInfoForFuture", BAND.scrip_code), ("GetScripInfoForFuture", "88004"),
                    ("MarketFeedV3", BAND.scrip_code), ("MarketFeedV3", "88003"), ("GetScripInfoForFuture", BAND2.scrip_code)], \
        "this engine's and the twin engine's codes on every channel, then this engine's band and the twin's band"


@pytest.mark.asyncio
async def test_a_stream_frame_landing_during_the_stand_in_snapshot_is_not_sent_again_with_an_earlier_time():
    """Re-check, 20 Oct: the stand-in snapshot dispatches one code after another, each awaiting the engine. A stream
    frame of a later code landing meanwhile reaches the engine off the stream (the code is wanted already); the
    snapshot does not send it again as a frame arriving before it — quote times never go back, no tick counts twice."""
    seen: list[dict] = []
    a = _feed(seen)
    a._ws = Broker()
    _up(a)
    await a.subscribe_record("mf", [BAND, BAND2])
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    await a._handle(_tick(BAND2.scrip_code, 7.0, 6.9, 7.1))
    landed = False

    async def on_tick(t: dict) -> None:
        nonlocal landed
        seen.append(t)
        if t["scrip_code"] == BAND.scrip_code and not landed:
            landed = True
            await asyncio.sleep(0.002)
            await a._handle(_tick(BAND2.scrip_code, 7.05, 7.0, 7.1))  # BAND2's stream, during BAND's snapshot

    a.on_tick = on_tick
    ticks0 = a.health.ticks
    await a.subscribe("mf", [BAND, BAND2])
    b2 = [t for t in seen if t["scrip_code"] == BAND2.scrip_code]
    assert [t["bid"] for t in b2] == [7.0], "BAND2 once: the stream's frame, not the pool's copy of it again"
    assert a.health.ticks - ticks0 == 2 and a.pool_snapshots == 1
    assert [t["recv_ts"] for t in seen] == sorted(t["recv_ts"] for t in seen)


@pytest.mark.asyncio
async def test_a_subscribe_builds_no_band_wide_set():
    """On the decision path (strike selection, re-check 20 Oct: 69 → 230 µs a call with the band on): membership per
    fresh code, and a call whose codes are all held returns at once."""
    a = _feed([])
    a._ws = broker = Broker()
    _up(a)
    await a.subscribe_record("mf", [BAND])
    await a.pin("twin-1", "mf", "s", [BAND2], record=True)

    def boom(*_a: Any, **_k: Any) -> set[str]:
        raise AssertionError("a set of every band's codes built per subscribe")

    a._twin_record_codes = boom  # type: ignore[method-assign]
    a._twin_codes = boom  # type: ignore[method-assign]
    await a.subscribe("mf", [RELIANCE, BAND, BAND2])
    assert broker.subs(RELIANCE.scrip_code) == 1 and broker.subs(BAND.scrip_code) == 1 and broker.subs(BAND2.scrip_code) == 1
    frames = len(broker.frames)
    await a.subscribe("mf", [RELIANCE, BAND, BAND2])
    assert len(broker.frames) == frames


def test_the_record_marker_is_the_hubs_alone():
    plain = json.loads(build_frame("C1", "mf", "s", [BAND]))
    marked = json.loads(build_frame("C1", "mf", "s", [BAND], record=True))
    assert "Record" not in plain and marked["Record"] == 1
    assert list(plain) == ["ClientCode", "MarketFeedData", "Method", "Operation"], "the broker's key order is untouched"


@pytest.fixture
async def hub():
    a_seen: list[dict] = []
    a = _feed(a_seen)
    broker = Broker()
    a._ws = broker
    _up(a)
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
async def test_the_twins_record_band_is_pinned_at_the_hub_apart_and_reaches_neither_engines_handlers(hub):
    a, a_seen, broker, server, twin = hub
    a_tap = _tap(a)
    b, b_seen = await twin()
    b_tap = _tap(b)
    await b.subscribe_record("mf", [BAND])
    await _until(lambda: broker.subs(BAND.scrip_code) == 1)
    assert server.stats()["pinned_record"]["mf"] == 1 and server.stats()["pinned"]["mf"] == 0
    assert all("Record" not in f for f in broker.frames)
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    await _until(lambda: any(c == BAND.scrip_code for _, c, _ in b_tap))
    await asyncio.sleep(0.05)
    assert a_seen == [] and b_seen == [], "the twin's band code reaches the serving engine's handlers no more than its own"
    assert ("mf", BAND.scrip_code, False) in a_tap, "the serving engine's recorder sees it (one socket, every row)"


@pytest.mark.asyncio
async def test_the_serving_engines_subscribe_of_a_code_only_the_twins_band_holds_is_a_fresh_snapshot(hub):
    """B's band alone never changes A (review, 20 Oct): A reads the code as without it — live from now, not since B's pin."""
    a, a_seen, broker, _server, twin = hub
    b, _b_seen = await twin()
    await b.subscribe_record("mf", [BAND])
    await _until(lambda: broker.subs(BAND.scrip_code) == 1)
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    t0 = time.time()
    await a.subscribe("mf", [BAND])
    assert broker.subs(BAND.scrip_code) == 1
    assert len(a_seen) == 1 and "pooled" not in a_seen[0] and a_seen[0]["recv_ts"] >= t0
    assert a.subscribed_since("mf", BAND.scrip_code) >= t0


@pytest.mark.asyncio
async def test_a_twin_asking_for_a_code_only_a_band_streams_is_answered_as_a_fresh_snapshot(hub):
    a, _a_seen, broker, server, twin = hub
    await a.subscribe_record("mf", [BAND])
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    b, b_seen = await twin()
    t0 = time.time()
    await b.subscribe("mf", [BAND])
    await _until(lambda: any(t["scrip_code"] == BAND.scrip_code for t in b_seen))
    got = next(t for t in b_seen if t["scrip_code"] == BAND.scrip_code)
    assert "pooled" not in got and got["bid"] == 4.1 and got["recv_ts"] >= t0
    assert b.subscribed_since("mf", BAND.scrip_code) >= t0, "live from the twin's own subscribe"
    assert broker.subs(BAND.scrip_code) == 1
    assert server.stats()["answered_as_snapshot"] == 1 and server.stats()["answered_from_pool"] == 0


@pytest.mark.asyncio
async def test_the_twins_own_band_code_goes_to_the_hub_and_its_engine_unsubscribe_keeps_the_band(hub):
    a, _a_seen, broker, server, twin = hub
    b, b_seen = await twin()
    await b.subscribe_record("mf", [BAND])
    await _until(lambda: broker.subs(BAND.scrip_code) == 1)
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    await _until(lambda: ("mf", BAND.scrip_code) in b._pool)
    await b.subscribe("mf", [BAND])
    await _until(lambda: any(t["scrip_code"] == BAND.scrip_code for t in b_seen))
    assert "pooled" not in b_seen[0], "the hub's snapshot, not the twin's own pool"
    await _until(lambda: server.stats()["pinned"]["mf"] == 1)
    await b.unsubscribe("mf", [BAND])
    await _until(lambda: server.stats()["pinned"]["mf"] == 0)
    unsubs = sum(1 for f in broker.frames if f["Operation"] == "Unsubscribe")
    assert unsubs == 0 and server.stats()["pinned_record"]["mf"] == 1, "the twin's band still holds it at the broker"
    await b.unsubscribe_record("mf", [BAND])
    await _until(lambda: sum(1 for f in broker.frames if f["Operation"] == "Unsubscribe") == 1)


async def _twin_reads_a_code_the_serving_engine_streams(hub, *, band: bool) -> tuple[list[tuple[Any, ...]], float | None, float]:
    a, _a_seen, _broker, _server, twin = hub
    await a.subscribe("mf", [BAND])  # the serving ENGINE holds the strike (a book of A priced it)
    t_a = a.subscribed_since("mf", BAND.scrip_code)
    await a._handle(_tick(BAND.scrip_code, 4.2, 4.1, 4.3))
    b, b_seen = await twin()
    if band:
        await b.subscribe_record("mf", [BAND])  # the twin's band holds it too
        await _until(lambda: _server.stats()["pinned_record"]["mf"] == 1)
    await a._handle(_tick(BAND.scrip_code, 4.25, 4.2, 4.3))  # the stream's last frame before the twin's book wants it
    await _until(lambda: ("mf", BAND.scrip_code) in b._pool and b._pool[("mf", BAND.scrip_code)][0]["LastRate"] == 4.25)
    arrived = b._pool[("mf", BAND.scrip_code)][1]
    await b.subscribe("mf", [BAND])  # the twin's book prices the same strike; no further frame comes (illiquid)
    await _until(lambda: _server.stats()["answered_from_pool"] >= 1)
    await asyncio.sleep(0.1)
    got = [(t.get("pooled"), t["ltp"], t["bid"], t["ask"], t["recv_ts"] == arrived) for t in b_seen
           if t["scrip_code"] == BAND.scrip_code]
    assert t_a is not None
    return got, b.subscribed_since("mf", BAND.scrip_code), t_a


@pytest.mark.asyncio
async def test_the_twins_band_code_the_serving_engine_streams_is_primed_from_its_own_pool_as_without_the_band(hub):
    """Re-check, 20 Oct (BAJFINANCE 5 Oct shape): with its band on, the twin's book got NO first frame of a strike its
    band held and the serving engine's books streamed — its subscribe left the own-pool prime to the hub's answer, and
    the hub's pool frame was older than its own copy. Now: the twin's own pool frame, at its true age, live since the
    serving engine's subscribe — as with the band off."""
    got, since, t_a = await _twin_reads_a_code_the_serving_engine_streams(hub, band=True)
    assert got == [(True, 4.25, 4.2, 4.3, True)], got
    assert since == pytest.approx(t_a, abs=1e-5)


@pytest.mark.asyncio
async def test_the_same_twin_read_with_the_band_off(hub):
    got, since, t_a = await _twin_reads_a_code_the_serving_engine_streams(hub, band=False)
    assert got == [(True, 4.25, 4.2, 4.3, True)], got
    assert since == pytest.approx(t_a, abs=1e-5)


@pytest.mark.asyncio
async def test_the_hubs_broker_socket_down_and_up_is_a_gap_for_the_twins_recorder(hub):
    _a, _a_seen, _broker, server, twin = hub
    b, _b_seen = await twin()
    gaps: list[tuple[float, float]] = []
    b.on_broker_gap = lambda down, up: gaps.append((down, up))
    server.state(False)
    await asyncio.sleep(0.05)
    server.state(True)
    await _until(lambda: len(gaps) == 1)
    assert gaps[0][1] - gaps[0][0] >= 0.04


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
