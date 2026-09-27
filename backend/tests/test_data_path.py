"""The data path, 2026-09-25: one TORNTPHARM 30m bucket (14:45) was closed — and decided — 22 times,
because a tick stamped inside a bucket the clock had already closed re-opened it."""

from __future__ import annotations

import asyncio
import time

import pytest

from kotsin_nse.bars.aggregator import Aggregator
from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.engine import Engine
from kotsin_nse.market.session import from_ist

TORNT = Instrument("3518", "TORNTPHARM", Segment.NSE_EQ, InstrumentKind.EQUITY, name="TORNTPHARM", underlying="TORNTPHARM")


def _ts(hm: str) -> float:
    from datetime import datetime

    return from_ist(datetime.fromisoformat(f"2026-09-25T{hm}:00"))


async def _agg():
    closed: list[UnifiedBar] = []

    async def sink(b):
        closed.append(b)

    store = BarStore()
    agg = Aggregator(store, timeframes=("1m", "30m"), on_bar_close=sink)
    agg.track(TORNT)
    agg.state[TORNT.scrip_code].connected_since = _ts("09:00")
    return agg, store, closed


def _tick(ts: float, px: float, total: int) -> dict:
    return {"scrip_code": TORNT.scrip_code, "ltp": px, "total_qty": total, "ts": ts}


@pytest.mark.asyncio
async def test_a_late_tick_never_reopens_a_closed_bucket():
    agg, store, closed = await _agg()
    for k, (hm, px) in enumerate((("14:45", 3500.0), ("14:59", 3520.0), ("15:14", 3490.0))):
        await agg.on_tick(_tick(_ts(hm) + 10, px, 100 * (k + 1)))
    await agg.flush_stale(_ts("15:15") + 1)  # the clock closes the 14:45 bucket
    full = store.last("TORNTPHARM", "30m")
    assert (full.open, full.high, full.low, full.close) == (3500.0, 3520.0, 3490.0, 3490.0)
    for k in range(30):  # ticks stamped 15:14:59 arriving after the close — the exchange lagging
        await agg.on_tick(_tick(_ts("15:14") + 59, 3480.0 + k, 400 + k))
        await agg.flush_stale(_ts("15:15") + 2 + k)
    thirty = [b for b in closed if b.tf == "30m"]
    assert len(thirty) == 1, "closed once, not 31 times"
    assert store.last("TORNTPHARM", "30m") is full, "the full bar is never replaced by a stub of late ticks"
    assert agg.late_ticks >= 30


@pytest.mark.asyncio
async def test_a_mid_session_boot_still_closes_the_bucket_rest_seeded_in_progress():
    """5paisa serves the in-progress candle; a boot at 14:50 seeds the 14:45 bucket as REST. The live
    build must still close that bucket at 15:15, or the first decision after a boot is lost."""
    agg, store, closed = await _agg()
    store.seed("TORNTPHARM", "30m", [UnifiedBar("TORNTPHARM", TORNT.scrip_code, "30m", int(_ts("14:45")), 3500, 3505, 3498, 3502, 1e4,
                                                source=BarSource.REST, complete=True)])
    agg.state[TORNT.scrip_code].connected_since = _ts("14:50")
    await agg.on_tick(_tick(_ts("14:52"), 3510.0, 1000))
    await agg.flush_stale(_ts("15:15") + 1)
    assert [b.ts for b in closed if b.tf == "30m"] == [int(_ts("14:45"))]


@pytest.mark.asyncio
async def test_the_engine_decides_a_bucket_once_and_counts_it_once(settings):
    e = Engine(settings)
    decided: list = []

    async def fake_reconcile_then_decide(bar):
        decided.append((bar.symbol, bar.ts))
        e._decided.add((bar.symbol, bar.ts))

    e._reconcile_then_decide = fake_reconcile_then_decide  # type: ignore[method-assign]
    from kotsin_nse.domain import Direction, OptionType, Position, PosSide

    opt = Instrument("1", "TORNTPHARM", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=250, strike=3550.0, option_type=OptionType.CE, underlying="TORNTPHARM")
    e.positions["p"] = Position(id="p", strategy="FUDKII", instrument=opt, underlying=TORNT, side=PosSide.LONG, qty=250, entry=20.0,
                                opened_ts=time.time(), signal_id="s", direction=Direction.BULLISH)
    bar = UnifiedBar("TORNTPHARM", TORNT.scrip_code, "30m", int(_ts("14:45")), 3500, 3520, 3490, 3490, 1e4, source=BarSource.LIVE, complete=True)
    for _ in range(22):
        await e._on_bar_close(bar)
    import asyncio

    await asyncio.sleep(0)
    assert decided == [("TORNTPHARM", bar.ts)] and e.positions["p"].bars_held == 1 and e._duplicate_closes == 21


@pytest.mark.asyncio
async def test_a_reconnect_marks_the_gap_partial_and_never_books_the_gaps_volume_as_a_surge():
    agg, store, closed = await _agg()
    await agg.on_tick(_tick(_ts("12:00") + 5, 3500.0, 10_000))
    await agg.on_tick(_tick(_ts("12:00") + 30, 3501.0, 10_400))
    # the socket drops at 12:00:40 and comes back at 12:12:10; 60,000 shares traded in the gap
    assert agg.on_reconnect(_ts("12:12") + 10) == 2  # the 1m and 30m bars forming across the gap
    await agg.on_tick(_tick(_ts("12:12") + 15, 3490.0, 70_400))
    await agg.on_tick(_tick(_ts("12:12") + 40, 3492.0, 70_700))
    await agg.flush_stale(_ts("12:13") + 1)
    m1 = [b for b in closed if b.tf == "1m"]
    assert m1[0].source is BarSource.PARTIAL, "the 12:00 minute is missing the gap's ticks"
    first_after = m1[-1]
    assert first_after.ts == int(_ts("12:12")) and first_after.source is BarSource.PARTIAL
    assert first_after.volume == 300.0, "the gap's 60,000 shares are not booked into 12:12"
    assert store.forming("TORNTPHARM", "30m").source is BarSource.PARTIAL


@pytest.mark.asyncio
async def test_the_backoff_resets_after_a_healthy_connection(monkeypatch):
    """Only a clean close used to reset it: after one bad patch, every later drop waited 60 s."""
    from kotsin_nse.config import Settings
    from kotsin_nse.venue.fivepaisa import ws as ws_mod

    feed = ws_mod.FivePaisaFeed(Settings(_env_file=None), auth=None, on_tick=None)  # type: ignore[arg-type]
    clock = {"t": 1000.0}
    monkeypatch.setattr(ws_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(ws_mod.random, "uniform", lambda a, b: 0.0)
    sleeps: list[float] = []
    plan = ["fail"] * 4 + ["healthy"] + ["fail"]

    async def connect():
        step = plan.pop(0)
        if step == "healthy":
            feed.health.connected_since = clock["t"]
            clock["t"] += 3600  # an hour of good data, then the peer resets
        if not plan:
            feed._stop.set()
        raise ConnectionError(step)

    async def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s

    feed._connect_and_read = connect  # type: ignore[method-assign]
    monkeypatch.setattr(ws_mod.asyncio, "sleep", fake_sleep)
    await feed.run()
    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
    assert sleeps[4] == 1.0, "an hour-long healthy connection starts the backoff again"


@pytest.mark.asyncio
async def test_a_silent_socket_in_session_is_dropped(settings, monkeypatch):
    from kotsin_nse import engine as engine_mod

    e = Engine(settings)
    fh = e.feed.health
    now = time.time()
    fh.connected, fh.connected_since, fh.last_message_ts = True, now - 600, now - 20
    asked: list[str] = []

    async def reconnect(reason=""):
        asked.append(reason)

    e.feed.reconnect = reconnect  # type: ignore[method-assign]
    monkeypatch.setattr(engine_mod, "is_open", lambda seg, t, cal=None: True)
    e.groups = {"X": type("G", (), {"segment": Segment.NSE_EQ})()}
    await e._feed_watchdog(now)
    assert asked and asked[0].startswith("silent 20s")
    await e._feed_watchdog(now + 5)
    assert len(asked) == 1, "at most one silence reconnect a minute"


@pytest.mark.asyncio
async def test_the_decision_frames_alert_books_read_the_exchanges_candle(settings):
    """They used to run on the live build at close; the reconciler then replaced the bar under them."""
    fake = {k: "x" for k in ("fp_client_code", "fp_app_key", "fp_encrypt_key", "fp_user_id", "fp_pin", "fp_totp_secret")}
    e = Engine(settings.model_copy(update=fake))  # credentials, so the reconcile path runs (nothing connects)
    seen: list[tuple[str, float]] = []
    e.alerts.on_bar = lambda b: seen.append((b.tf, b.close))  # type: ignore[method-assign]
    live = UnifiedBar("TORNTPHARM", TORNT.scrip_code, "30m", int(_ts("14:45")), 3500, 3520, 3490, 3490, 1e4, source=BarSource.LIVE, complete=True)
    exch = UnifiedBar("TORNTPHARM", TORNT.scrip_code, "30m", int(_ts("14:45")), 3500, 3521, 3488, 3495, 1.1e4, source=BarSource.REST, complete=True)

    class Rec:
        async def reconcile_bar(self, bar, timeout_s):
            e.store.close(exch)
            return type("C", (), {"found": True, "error": None})()

    e.reconciler = Rec()  # type: ignore[assignment]
    e.reconciler_ready = True

    async def no_decide(bar):
        return None

    e._decide = no_decide  # type: ignore[method-assign]
    assert e.s.has_credentials
    await e._on_bar_close(live)
    assert seen == [], "not on the live build"
    await asyncio.gather(*list(e._decision_tasks))
    assert seen == [("30m", 3495)], "on the exchange's candle"
    one_min = UnifiedBar("TORNTPHARM", TORNT.scrip_code, "1m", int(_ts("15:14")), 3490, 3491, 3489, 3490, 100, source=BarSource.LIVE, complete=True)
    await e._on_bar_close(one_min)
    assert seen[-1] == ("1m", 3490), "the finer frames are unchanged"


@pytest.mark.asyncio
async def test_the_closing_auction_bar_is_not_the_bar_before_a_0945_trigger(settings):
    """PNBHOUSING / RELIANCE: RT-X and RT-Y skipped 09:45 triggers as "dried volume 0.53/0.00" — the
    0.00 was yesterday's 15:15 bar, now a closing-auction print with ~no volume in the broker's
    candle. The bar before 09:45 is yesterday's last continuously traded bar (14:45)."""
    from kotsin_nse.bars.indicators import dried_volume

    e = Engine(settings)
    e.underlyings["TORNTPHARM"] = TORNT
    y = int(_ts("09:15")) - 86_400  # the previous session
    prior = [UnifiedBar("TORNTPHARM", TORNT.scrip_code, "30m", y + k * 1800, 3500, 3510, 3490, 3500, 10_000.0, source=BarSource.REST, complete=True)
             for k in range(12)]  # 09:15 … 14:45, 10k each
    auction = UnifiedBar("TORNTPHARM", TORNT.scrip_code, "30m", y + 12 * 1800, 3500, 3500, 3500, 3500, 55.0, source=BarSource.REST, complete=True)
    trigger = UnifiedBar("TORNTPHARM", TORNT.scrip_code, "30m", int(_ts("09:15")), 3490, 3495, 3470, 3472, 5_300.0, source=BarSource.REST, complete=True)
    e.store.seed("TORNTPHARM", "30m", [*prior, auction, trigger])
    surges = await e._volume_surges(TORNT)
    s_t, s_t1 = surges["equity"]
    assert s_t == pytest.approx(0.53) and s_t1 == pytest.approx(1.0), "T-1 is the 14:45 bar, not the auction print"
    assert not dried_volume(s_t, s_t1, v=0.85), "one quiet bar is not dried volume"
