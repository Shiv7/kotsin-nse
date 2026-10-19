"""Engine A's broker reconnect is a reconnect for engine B too (2026-10-19). B reads A's socket through the feed hub,
and the hub's ``#down`` / ``#up`` lines only set B's ``connected`` flag: A marked its forming bars PARTIAL, moved
``connected_since`` and re-primed its volume baselines at every reconnect, B did none of it, and the snapshot frames
of A's resubscribe built one-print LIVE bars in B with the gap's volume lumped into one minute (624 of the 721 bars
B had and A had not, 13-16 Oct; CROMPTON 14 Oct 10:25 at 10.5x its median minute). ``#up`` after a ``#down`` now
runs B's own reconnect repair (``Settings.hub_reconnect_repair``, default on). Real sockets, a fake broker."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.store import BarStore
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.venue.fivepaisa.hub import HubServer
from kotsin_nse.venue.fivepaisa.ws import FivePaisaFeed

IST = timezone(timedelta(hours=5, minutes=30))
CROMPTON = Instrument("17094", "CROMPTON", Segment.NSE_EQ, InstrumentKind.EQUITY)


class Broker:
    async def send(self, text: str) -> None:
        pass

    async def close(self) -> None:
        pass


async def _until(cond, within: float = 3.0) -> None:
    end = time.monotonic() + within
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def _frame(ltp: float, traded: float, total: int) -> str:
    return json.dumps([{"Token": int(CROMPTON.scrip_code), "Exch": "N", "ExchType": "C", "LastRate": ltp,
                        "TotalQty": total, "LastQty": 10, "TickDt": f"/Date({int(traded * 1000)})/"}])


async def _rig(repair: bool):
    """A (serving, broker socket faked) and B (reading the hub) with B's real aggregator behind its feed."""
    store = BarStore()
    agg = Aggregator(store)
    agg.track(CROMPTON)
    calls: list[bool] = []

    async def on_connect(again: bool) -> None:
        calls.append(again)
        if again:
            agg.on_reconnect()

    a = FivePaisaFeed(Settings(_env_file=None), None, on_tick=lambda t: asyncio.sleep(0))  # type: ignore[arg-type]
    b = FivePaisaFeed(Settings(_env_file=None), None, on_tick=agg.on_tick, on_connect=on_connect)  # type: ignore[arg-type]
    b.hub_up_is_reconnect = repair
    a._ws = Broker()
    server = HubServer(a, "127.0.0.1", 0)
    await server.start()
    server.state(True)
    b.hub = ("127.0.0.1", server.port)
    await b.subscribe("mf", [CROMPTON])
    task = asyncio.create_task(b._connect_and_read())
    await _until(lambda: b.health.connected and calls == [False])
    return a, b, server, task, agg, store, calls


async def _close(server, task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()


@pytest.mark.asyncio
async def test_a_link_s_first_up_is_not_a_reconnect_a_down_then_up_is():
    _a, b, server, task, *_, calls = await _rig(True)
    try:
        assert calls == [False], "the link's own connect"
        server.state(True)  # an up the link already knew: nothing
        await asyncio.sleep(0.05)
        assert calls == [False] and b.hub_broker_reconnects == 0
        server.state(False)
        await _until(lambda: not b.health.connected)
        server.state(True)
        await _until(lambda: calls == [False, True])
        assert b.hub_broker_reconnects == 1 and b.hub_stats()["broker_reconnects"] == 1  # type: ignore[index]
    finally:
        await _close(server, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [True, False])
async def test_the_resubscribe_snapshot_after_a_reconnect_builds_no_bar(repair, monkeypatch):
    """14 Oct: A's socket dropped at 10:20 and was read again at 10:26; CROMPTON's snapshot carried its last
    trade (10:25:40) and the whole gap's TotalQty. A drops it as stale; B built a LIVE 10:25 bar from it."""
    clock = {"now": datetime(2026, 10, 14, 10, 18, 10, tzinfo=IST).timestamp()}
    monkeypatch.setattr(time, "time", lambda: clock["now"])  # the wall clock of that morning (monotonic untouched)
    a, b, server, task, agg, store, calls = await _rig(repair)
    try:
        st = agg.state[CROMPTON.scrip_code]
        st.connected_since = clock["now"] - 3600  # B has been up all morning
        await a._handle(_frame(101.0, clock["now"], 50_000))  # the last frame before the drop
        await _until(lambda: st.ticks == 1)
        assert store.forming("CROMPTON", "1m") is not None
        server.state(False)
        await _until(lambda: not b.health.connected)
        clock["now"] = datetime(2026, 10, 14, 10, 26, 2, tzinfo=IST).timestamp()  # A is back
        server.state(True)
        await _until(lambda: b.health.connected)
        await asyncio.sleep(0.05)
        stale_before = agg.stale_frames
        last_trade = datetime(2026, 10, 14, 10, 25, 40, tzinfo=IST).timestamp()
        await a._handle(_frame(103.0, last_trade, 215_318))
        await _until(lambda: st.ticks == 2)
        bucket = int(datetime(2026, 10, 14, 10, 25, tzinfo=IST).timestamp())
        forming = store.forming("CROMPTON", "1m")
        at_1025 = [x for x in store.bars("CROMPTON", "1m") if x.ts == bucket] + ([forming] if forming and forming.ts == bucket else [])
        if repair:
            assert calls == [False, True] and agg.stale_frames > stale_before, "dropped as stale, as engine A drops it"
            assert not at_1025, "no one-print 10:25 bar"
            assert st.last_total_qty in (None, 215_318), "the baseline re-primed: no lumped volume"
        else:
            assert calls == [False], "the old flag-only handling"
            assert at_1025 and at_1025[0].volume == 215_318 - 50_000, "the old one-print bar with the gap's volume"
    finally:
        await _close(server, task)


def test_the_switch_defaults_on_and_is_wired_only_for_a_hub_reader():
    assert Settings(_env_file=None).hub_reconnect_repair is True
    f = FivePaisaFeed(Settings(_env_file=None), None, on_tick=lambda t: asyncio.sleep(0))  # type: ignore[arg-type]
    assert f.hub_up_is_reconnect is False, "the serving engine (its own socket) never reads #up lines"
