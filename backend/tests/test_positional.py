"""The daily order block's positional book: zones to pending limits, fills, exits, the short gate, persistence."""

from __future__ import annotations

from datetime import date, timedelta

from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.market.session import day_start_ts
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy import daily_ob as ob
from kotsin_nse.strategy.positional import PositionalRunner, daily_series
from kotsin_nse.structure.engine import Bar5

START = date(2026, 1, 5)  # a Monday


def sessions(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def demand_series() -> list[tuple[float, float, float, float]]:
    """Forty quiet sessions, a swing high at 103, the last red candle (the zone: o 100.5, l 99.8), a displacement
    close at 105.5 through the swing high, three sessions away, then the return that trades through Z."""
    rows = [(100.0, 101.0, 99.0, 100.0 + (0.2 if i % 2 else -0.2)) for i in range(40)]
    rows += [(100.0, 103.0, 99.5, 102.0), (102.0, 101.0, 99.5, 100.2), (100.2, 101.0, 99.3, 100.4)]
    rows += [(100.5, 100.8, 99.8, 99.9)]  # the order block: the last red candle before the move
    rows += [(100.0, 106.0, 99.9, 105.5), (105.5, 106.5, 104.8, 106.0), (106.0, 106.8, 105.0, 105.5), (105.5, 106.0, 104.9, 105.2)]
    return rows


def mirror(rows):
    return [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in rows]


def _bar(symbol, code, d, o, h, lo, c, src=BarSource.REST) -> UnifiedBar:
    return UnifiedBar(symbol=symbol, scrip_code=code, tf="1d", ts=int(day_start_ts(d)), open=o, high=h, low=lo, close=c, volume=1e5, source=src, complete=True)


def _seed(store: BarStore, symbol: str, code: str, rows, nifty_up: bool = False) -> list[date]:
    days = sessions(len(rows))
    store.seed(symbol, "1d", [_bar(symbol, code, d, *r) for d, r in zip(days, rows, strict=True)])
    nifty = [_bar("NIFTY", "999920000", d, 20000 + (i * 20 if nifty_up else -i * 20), 20010 + (i * 20 if nifty_up else -i * 20), 19990 + (i * 20 if nifty_up else -i * 20), 20000 + (i * 20 if nifty_up else -i * 20)) for i, d in enumerate(days)]
    store.seed("NIFTY", "1d", nifty)
    return days


def _runner(tmp_path, store, symbols=("ABC",), **kw) -> PositionalRunner:
    s = Settings(_env_file=None, data_dir=tmp_path, engine_enabled=False, **kw)
    insts = {sym: Instrument(scrip_code=str(i + 1), symbol=sym, segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY, name=sym, underlying=sym) for i, sym in enumerate(symbols)}
    insts["NIFTY"] = Instrument(scrip_code="999920000", symbol="NIFTY", segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY, name="NIFTY", underlying="NIFTY")
    return PositionalRunner(s, store=store, costs=CostModel(s), underlyings=insts)


def test_the_rule_reads_a_zone_a_fill_and_the_exits():
    bars = [Bar5(1_000 + i * 86400, *r, 1e5) for i, r in enumerate(demand_series())]
    zs = ob.pending_zones(bars, adr=2.0)
    assert len(zs) == 1 and zs[0].side == 1 and zs[0].z == 100.5 and zs[0].low == 99.8 and zs[0].high == 100.8 and zs[0].stop < 99.8
    z = zs[0]
    assert not ob.fills(1, z.z, Bar5(0, 102, 103, 100.45, 101, 0))  # a touch 5 bps above the 10 bps line is not a fill
    assert ob.fills(1, z.z, Bar5(0, 102, 103, 100.3, 101, 0)) and ob.returned(1, z.high, z.low, Bar5(0, 102, 103, 100.7, 101, 0))
    e, s = z.z, z.stop
    t = e + 2 * (e - s)
    assert ob.step(1, e, s, t, Bar5(0, 101, t + 0.1, e - 0.1, t, 0), first=True, held=0, max_sessions=20) == (t, "target")
    assert ob.step(1, e, s, t, Bar5(0, 101, t + 0.1, s - 0.1, t, 0), first=True, held=0, max_sessions=20) == (s, "stop")  # the stop before the target
    assert ob.step(1, e, s, t, Bar5(0, s - 0.5, s, s - 1, s - 0.8, 0), first=False, held=1, max_sessions=20) == (s - 0.5, "gap_stop")
    assert ob.step(1, e, s, t, Bar5(0, 101, 101.5, 100.6, 101.2, 0), first=False, held=20, max_sessions=20) == (101.2, "time")
    assert ob.step(1, e, s, t, Bar5(0, 101, 101.5, 100.6, 101.2, 0), first=False, held=3, max_sessions=20) is None
    assert ob.trailing_return([100.0] * 21) == 0.0 and ob.trailing_return([100.0] * 5) is None
    assert ob.short_allowed("nifty20", -1.0) and not ob.short_allowed("nifty20", 0.5) and ob.short_allowed("always", 5.0) and not ob.short_allowed("off", -5.0)


async def test_the_book_places_the_limit_fills_it_and_closes_at_the_target(tmp_path):
    store = BarStore()
    rows = demand_series()
    days = _seed(store, "ABC", "1", rows)
    r = _runner(tmp_path, store)
    assert await r.catch_up() == [days[-1].isoformat()]  # first boot: the pending limits, no trades back-filled
    assert r.pending["ABC"][0]["z"] == 100.5 and r.last_day == days[-1].isoformat() and not r.positions
    # the next session trades through Z and on to the target the same day
    nxt = sessions(len(rows) + 1)[-1]
    store.seed("ABC", "1d", [_bar("ABC", "1", nxt, 105.0, 105.2, 100.3, 103.0)])
    store.seed("NIFTY", "1d", [_bar("NIFTY", "999920000", nxt, 19000, 19010, 18990, 19000)])
    line = await r.on_session_close(nxt)
    assert line["opened"] == 1 and line["closed"] == 1 and not r.positions
    t = r.trades[0]
    assert t["symbol"] == "ABC" and t["side"] == 1 and t["entry"] == 100.5 and t["exit_reason"] == "target" and t["exit"] == t["target"]
    assert t["qty"] == int(500 / (100.5 - t["stop"])) and t["venue"] == "cash" and t["charges_inr"] > 0 and t["net_inr"] < t["gross_inr"] and t["r"] == 2.0
    assert t["regime"]["nifty_ret20"] is not None and t["regime"]["nifty_ret20"] < 0
    snap = r.snapshot()
    assert snap["summary"]["closed"] == 1 and snap["summary"]["by_side"]["long"]["closed"] == 1 and snap["summary"]["exits"]["target"] == 1
    # persistence: a new runner reads the same book, and the session is not processed twice
    r2 = _runner(tmp_path, store)
    assert r2.last_day == nxt.isoformat() and len(r2.trades) == 1
    assert (await r2.on_session_close(nxt)).get("skipped") == "already processed"
    # the same date arriving again as a live-built bar does not replace the broker's candle
    assert daily_series(store, "ABC")[-1].source is BarSource.REST


async def test_a_touch_that_does_not_trade_through_is_not_a_fill_and_is_not_retried(tmp_path):
    store = BarStore()
    rows = demand_series()
    _seed(store, "ABC", "1", rows)
    r = _runner(tmp_path, store)
    await r.catch_up()
    nxt = sessions(len(rows) + 1)[-1]
    store.seed("ABC", "1d", [_bar("ABC", "1", nxt, 105.0, 105.2, 100.6, 103.0)])  # into the zone (high 100.8), not through Z
    store.seed("NIFTY", "1d", [_bar("NIFTY", "999920000", nxt, 19000, 19010, 18990, 19000)])
    line = await r.on_session_close(nxt)
    assert line["touched"] == 1 and line["opened"] == 0 and r.skipped[-1]["why"] == "touched, not through" and not r.pending.get("ABC")


async def test_the_short_gate_follows_nifty(tmp_path):
    store = BarStore()
    rows = mirror(demand_series())  # a supply zone
    _seed(store, "ABC", "1", rows, nifty_up=True)
    r = _runner(tmp_path, store)
    await r.catch_up()
    assert r.pending["ABC"][0]["side"] == -1
    nxt = sessions(len(rows) + 1)[-1]
    store.seed("ABC", "1d", [_bar("ABC", "1", nxt, 95.0, 99.7, 94.8, 97.0)])
    store.seed("NIFTY", "1d", [_bar("NIFTY", "999920000", nxt, 21000, 21010, 20990, 21000)])
    line = await r.on_session_close(nxt)
    assert line["opened"] == 0 and line["skipped"] == 1 and "short gate" in r.skipped[-1]["why"]
    # with the gate off the same return is a short
    store2 = BarStore()
    _seed(store2, "ABC", "1", rows, nifty_up=True)
    r2 = _runner(tmp_path / "b", store2, positional_short_gate="always")
    await r2.catch_up()
    store2.seed("ABC", "1d", [_bar("ABC", "1", nxt, 95.0, 99.7, 94.8, 97.0)])
    store2.seed("NIFTY", "1d", [_bar("NIFTY", "999920000", nxt, 21000, 21010, 20990, 21000)])
    line2 = await r2.on_session_close(nxt)
    assert line2["opened"] == 1 and r2.trades[0]["venue"] == "future" and r2.trades[0]["side"] == -1 and r2.trades[0]["exit_reason"] == "target"  # the session ran to the target
