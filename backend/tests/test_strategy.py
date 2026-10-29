"""The CPR retest strategy: the sequence on bars so far, the exit rules one bar at a time against the
research simulator, and a shadow round trip through the runner and the gateway."""

from __future__ import annotations

import asyncio
import random
from datetime import date

from kotsin_nse.bars.store import BarStore
from kotsin_nse.bars.unified import BarSource, UnifiedBar
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.exec.gateway import Gateway, Mode
from kotsin_nse.exec.paper import PaperMatcher
from kotsin_nse.market.session import session_open_ts
from kotsin_nse.risk.costs import CostModel
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.strategy.runner import ShadowRunner
from kotsin_nse.structure.engine import Bar5

from .conftest import ANCHOR_DAY, ist_ts

DAY = ANCHOR_DAY  # Monday 2026-01-05
OPEN = int(session_open_ts(Segment.NSE_EQ, __import__("datetime").date.fromisoformat(DAY)))
PREV = (110.0, 100.0, 104.0)  # previous session H, L, C → P 104.67, BC 104.33, TC 105, S1 99.33, R1 109.33
ADR = 10.0  # tol 1.0, brk 0.5

#: a bullish day as the owner reads it: S1 tested by 09:20 under the CPR, a swing high at 09:25 broken at
#: 09:40 (BOS), through TC at 09:50, back to TC at 09:55 and held, the first close above that bar at 10:05
DAY_BARS = [
    (102.0, 102.4, 101.5, 101.8),  # 0 09:15 under the CPR
    (101.8, 102.6, 100.2, 101.0),  # 1 09:20 S1 held as support (low 100.2 ≤ S1 + tol), close < BC
    (101.0, 102.8, 100.8, 102.3),  # 2 09:25 swing high 102.8
    (102.3, 102.5, 101.5, 101.8),  # 3
    (101.8, 102.2, 101.2, 101.6),  # 4 the swing high is known
    (101.6, 103.4, 101.5, 103.2),  # 5 09:40 BOS up (close above 102.8)
    (103.2, 104.5, 103.0, 104.2),  # 6
    (104.2, 106.0, 104.0, 105.8),  # 7 09:50 through TC + brk (105.5)
    (105.8, 106.5, 105.2, 105.9),  # 8 09:55 retest: low ≤ TC + tol (106), close ≥ BC → held
    (105.9, 106.3, 105.5, 106.1),  # 9
    (106.1, 107.0, 106.0, 106.8),  # 10 10:05 first close above the retest bar's high (106.5) → entry 106.8
    (106.8, 109.5, 106.7, 109.2),  # 11 R1 109.33 reached → stop 108.33, target PDH 110
    (109.2, 109.8, 108.9, 109.4),  # 12
    (109.4, 109.6, 108.2, 108.4),  # 13 trail hit at 108.33
]


def bars5(rows, start=0) -> list[Bar5]:
    return [Bar5(OPEN + 300 * (start + i), o, h, lo, c, 1000.0) for i, (o, h, lo, c) in enumerate(rows)]


def test_the_sequence_advances_stage_by_stage_on_the_bars_so_far():
    levels = cr.day_levels(*PREV)
    assert round(levels["BC"], 2) == 104.33 and levels["TC"] == 105.0
    tol = cr.tolerance(ADR, 102.0)
    b = bars5(DAY_BARS)
    names = [cr.sequence(b[:k], levels, tol)[1].name for k in range(1, len(b) + 1)]
    assert names[0] == "none"
    assert names[1] == "open_test" and names[4] == "open_test"
    assert names[5] == "bos"
    assert names[7] == "through"
    assert names[8] == "retest_held"
    assert names[9] == "retest_held"
    assert names[10] == "confirmed"
    st = cr.sequence(b, levels, tol)[1]
    assert (st.i_os, st.lvl_os, st.i_bos, st.i_brk, st.i_rt, st.result, st.j) == (1, "S1", 5, 7, 8, "held", 10)
    assert cr.sequence(b, levels, tol)[-1].name == "none"
    # the confirmation is known on its own bar and never earlier: the stage at k = 10 has no j
    assert cr.sequence(b[:10], levels, tol)[1].j is None


def test_a_retest_that_closes_back_under_bc_fails_and_a_late_confirmation_is_missed():
    levels = cr.day_levels(*PREV)
    tol = cr.tolerance(ADR, 102.0)
    failed = [*DAY_BARS[:8], (105.8, 106.2, 103.5, 104.0)]  # back under BC on the retest bar
    assert cr.sequence(bars5(failed), levels, tol)[1].name == "retest_failed"
    late = [*DAY_BARS[:9], *([(105.9, 106.4, 105.6, 106.0)] * 6), (106.0, 107.0, 106.0, 106.8)]
    st = cr.sequence(bars5(late), levels, tol)[1]
    assert st.name == "retest_held" and st.j is None  # the seventh bar after the retest is too late


def test_walls_are_beyond_the_entry_by_a_tolerance_nearest_first():
    levels = cr.day_levels(*PREV)
    ws = cr.walls(1, 106.8, levels, {"WP": 108.0, "WR1": 112.0, "WR2": 115.0, "WS1": 100.0, "WS2": 97.0}, 1.0)
    assert [w[0] for w in ws] == ["WP", "R1", "PDH", "WR1", "R2", "WR2", "R3"]
    assert ws[0][1] == 108.0
    # the round number 107.0 is only 0.2 away: not a wall. Short side mirrors.
    assert all(w[0] != "ROUND" for w in ws)
    ws_short = cr.walls(-1, 103.0, levels, None, 1.0)
    assert [w[0] for w in ws_short] == ["PDL", "S1", "S2", "S3"]  # the round number 102.5 is within a tolerance
    assert cr.walls(-1, 103.0, levels, None, 0.3)[0] == ("ROUND", 102.5)
    assert cr.round_step(1500.0) == 10 and cr.round_step(250.0) == 1 and cr.round_step(40.0) == 0.5


def test_walls_without_round_numbers_and_the_nearest_level_at_any_distance():
    levels = cr.day_levels(*PREV)
    assert cr.walls(-1, 103.0, levels, None, 0.3, use_round=False)[0] == ("PDL", 100.0)
    assert all(w[0] != "ROUND" for w in cr.walls(1, 106.8, levels, None, 0.1, use_round=False))
    # S1 99.33 and PDL 100 sit under a short at 100.2 inside a tolerance of 1.0: no wall, but the nearest level is PDL
    assert cr.walls(-1, 100.2, levels, None, 1.0, use_round=False)[0][0] == "S2"
    assert cr.nearest_level(-1, 100.2, levels, None) == ("PDL", 100.0)
    assert cr.nearest_level(1, 106.8, levels, None)[0] == "R1"


def test_wall_then_trail_takes_the_wall_then_trails_to_the_next():
    levels = cr.day_levels(*PREV)
    ws = [v for _k, v in cr.walls(1, 106.8, levels, None, 1.0)]
    p = cr.ExitPlan.open("wall_then_trail", 1, 106.8, 104.2, ws, 1.0)
    assert p.target_price() == ws[0]  # R1 109.33
    rows = DAY_BARS[11:]
    out = [p.step(h, lo, c) for _o, h, lo, c in rows]
    assert out[0] is None and round(p.stop_price(), 2) == 108.33 and p.target_price() == 110.0
    assert out[1] is None
    assert out[2] is not None and out[2][1] == "trail" and round(out[2][0], 2) == 108.33
    assert round(p.pnl_bps(out[2][0]), 1) == round((108.33 - 106.8) / 106.8 * 1e4, 1)
    fixed = cr.ExitPlan.open("fixed_wall", 1, 106.8, 104.2, ws, 1.0)
    assert fixed.step(109.5, 106.7, 109.2) == (ws[0], "target")
    one_two = cr.ExitPlan.open("fixed_1to2", 1, 106.8, 104.2, ws, 1.0)
    assert round(one_two.target_price() or 0, 2) == 112.0
    stopped = cr.ExitPlan.open("fixed_wall", 1, 106.8, 104.2, ws, 1.0)
    assert stopped.step(109.5, 104.0, 109.2) == (104.2, "stop")  # touches both: the stop
    short = cr.ExitPlan.open("fixed_wall", -1, 100.0, 102.0, [97.0], 1.0)
    assert short.step(101.0, 96.5, 97.5) == (97.0, "target")
    assert cr.ExitPlan.open("fixed_wall", 1, 100.0, 98.0, [105.0], 1.0).step(101.0, 99.0, 100.5, last=True) == (100.5, "close")


def _reference(rule, side, j, E, stop0, walls_sorted, h, lo, c, tol):
    """``retest_mgmt.simulate`` as the research ran it (the three rules the shadow book scores)."""
    n = len(c)
    sg = side
    H = [sg * (h[k] if sg > 0 else lo[k]) for k in range(n)]
    L = [sg * (lo[k] if sg > 0 else h[k]) for k in range(n)]
    C = [sg * x for x in c]
    e, stop = sg * E, sg * stop0
    risk = e - stop
    walls = [sg * w for w in walls_sorted]
    target = walls[0] if walls else None
    wall_i = 0
    if rule == "fixed_1to2":
        target = e + 2 * risk
    for k in range(j + 1, n):
        if L[k] <= stop:
            return (stop - e) / abs(E) * 1e4, "stop" if stop < e else "trail"
        if target is not None and H[k] >= target:
            if rule == "wall_then_trail":
                wall_i += 1
                if wall_i < len(walls):
                    stop = max(stop, target - tol)
                    target = walls[wall_i]
                    continue
                return (target - e) / abs(E) * 1e4, "target"
            return (target - e) / abs(E) * 1e4, "target"
    return (C[-1] - e) / abs(E) * 1e4, "close"


def test_exit_plans_reproduce_the_research_simulator_on_random_paths():
    rng = random.Random(7)
    for trial in range(400):
        side = rng.choice((1, -1))
        n = rng.randint(3, 40)
        px = 100.0
        h, lo, c = [], [], []
        for _ in range(n):
            o = px
            cl = o * (1 + rng.gauss(0, 0.004))
            hi = max(o, cl) * (1 + abs(rng.gauss(0, 0.002)))
            lw = min(o, cl) * (1 - abs(rng.gauss(0, 0.002)))
            h.append(hi), lo.append(lw), c.append(cl)
            px = cl
        E = 100.0
        stop0 = E - side * rng.uniform(0.2, 1.5)
        k = rng.randint(1, 4)
        ws = sorted((E + side * rng.uniform(0.3, 3.0) for _ in range(k)), key=lambda w: side * w)
        tol = rng.uniform(0.05, 0.5)
        for rule in cr.RULES:
            ref = _reference(rule, side, -1, E, stop0, ws, h, lo, c, tol)
            p = cr.ExitPlan.open(rule, side, E, stop0, ws, tol)
            got = None
            for i in range(n):
                got = p.step(h[i], lo[i], c[i], last=i == n - 1)
                if got is not None:
                    break
            assert got is not None, (trial, rule)
            assert got[1] == ref[1], (trial, rule, got, ref)
            assert abs(p.pnl_bps(got[0]) - ref[0]) < 1e-6, (trial, rule, got, ref)


def test_filter_spec_and_tags():
    assert cr.passes("none", {}) and cr.passes("", {"x": False})
    assert cr.passes("st60_al+dma20_al", {"st60_al": True, "dma20_al": True})
    assert not cr.passes("st60_al+dma20_al", {"st60_al": True})
    closes = [100 + i * 0.5 for i in range(80)]
    t = cr.daily_tags(1, closes, [x + 1 for x in closes], [x - 1 for x in closes], [1000 + i for i in range(80)])
    assert t["dma20_al"] and t["dma50_al"] and t["ema_stack_al"] and t["ret5_al"] and t["prev_ret_al"]
    assert cr.daily_tags(-1, closes, closes, closes, None)["dma20_al"] is False
    assert cr.daily_tags(1, closes[:10], closes[:10], closes[:10], None) == {}
    up = [100 + i for i in range(30)]
    assert cr.supertrend_dir([x + 1 for x in up], [x - 1 for x in up], up) == 1
    dn = list(reversed(up))
    assert cr.supertrend_dir([x + 1 for x in dn], [x - 1 for x in dn], dn) == -1
    b = bars5(DAY_BARS)
    it = cr.intraday_tags(1, b, 10, ADR, None, [0.1, 0.2, 0.3, 0.9])
    assert it["move_adr"] == round((106.8 / 102.0 - 1) * 100 / (ADR / 106.8 * 100), 2)
    assert it["xs_pct"] == 0.75 and it["pos_08"] and not it["ib_al"]


# -- the runner --------------------------------------------------------------------------------------------


def _inst(symbol="ABC", code="1") -> Instrument:
    return Instrument(scrip_code=code, symbol=symbol, segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY, name=symbol, underlying=symbol)


def _ubar(symbol, code, tf, ts, o, h, lo, c) -> UnifiedBar:
    return UnifiedBar(symbol=symbol, scrip_code=code, tf=tf, ts=int(ts), open=o, high=h, low=lo, close=c, volume=1000.0, complete=True)


def _seed_daily(store: BarStore, symbol: str, code: str) -> None:
    """Sixty flat sessions before the day (range 10 → ADR 10), the last one the PREV session."""
    rows = []
    for i in range(60, 0, -1):
        ts = ist_ts(DAY, "09:15") - i * 86400
        if i == 1:
            rows.append(_ubar(symbol, code, "1d", ts, 104.0, *PREV))
        else:
            rows.append(_ubar(symbol, code, "1d", ts, 104.0, 109.0, 99.0, 104.0))
    store.seed(symbol, "1d", rows)


def _runner(tmp_path, store, rule="wall_then_trail", filt="none", symbols=("ABC",), **kw) -> ShadowRunner:
    s = Settings(_env_file=None, data_dir=tmp_path, engine_enabled=False, strategy_rule=rule, strategy_filter=filt, **kw)
    costs = CostModel(s)
    gw = Gateway(matcher=PaperMatcher(costs), mode=lambda: Mode.SHADOW, halted=lambda: (False, ""), book_for=lambda _c: None, ltp_for=lambda _c: None)
    tasks: list[asyncio.Task] = []

    def spawn(coro, name):
        t = asyncio.create_task(coro, name=name)
        tasks.append(t)
        return t

    r = ShadowRunner(s, store=store, gateway=gw, ledger=None, costs=costs, underlyings={sym: _inst(sym, str(i + 1)) for i, sym in enumerate(symbols)}, spawn=spawn)
    r._tasks = tasks  # type: ignore[attr-defined]
    return r


async def _feed(r: ShadowRunner, store: BarStore, rows, symbol="ABC", code="1", pad_to: int | None = None) -> None:
    all_rows = list(rows)
    if pad_to is not None:
        last = all_rows[-1][3]
        all_rows += [(last, last + 0.1, last - 0.1, last)] * (pad_to - len(all_rows))
    for i, (o, h, lo, c) in enumerate(all_rows):
        b = _ubar(symbol, code, "5m", OPEN + 300 * i, o, h, lo, c)
        store.close(b)
        await r.on_bar(b)
    await asyncio.gather(*r._tasks)  # type: ignore[attr-defined]


async def test_the_runner_takes_the_retest_and_trails_out_in_shadow(tmp_path):
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store)
    await _feed(r, store, DAY_BARS, pad_to=75)
    snap = r.snapshot()
    assert snap["day"] == DAY and snap["with_levels"] == 1
    assert snap["summary"]["signals"] == 1 and snap["summary"]["taken"] == 1 and snap["summary"]["closed"] == 1 and snap["summary"]["open"] == 0
    sig = snap["signals"][0]
    assert sig["side"] == "LONG" and sig["entry"] == 106.8 and sig["stop"] == 104.2 and sig["retest_time"] == "09:55" and sig["time"] == "10:10"
    assert sig["bar"] == "10:05", "the bar is labelled by its open, as the owner's chart labels it; `time` is when the close fires"
    assert sig["walls"][0]["name"] == "R1"
    t = snap["trades"][0]
    assert t["entry_bar"] == "10:05" and t["entry_time"] == "10:10"
    assert [w["name"] for w in sig["walls"][:3]] == ["R1", "WR1", "PDH"]  # the weekly R1 109.67 sits between R1 and PDH
    assert t["status"] == "CLOSED" and t["exit_reason"] == "trail" and round(t["exit"], 2) == 108.67 and t["mode"] == "SHADOW"
    assert t["qty"] == 234  # ₹25,000 at 106.8
    assert t["net_inr"] is not None and t["net_inr"] < t["gross_inr"] and t["gross_inr"] > 0
    assert t["virtual"]["fixed_wall"]["how"] == "target" and t["virtual"]["fixed_1to2"]["how"] == "close"  # scored to the day's end
    assert t["virtual"]["fixed_1to2"]["bars"] == 64 and r.plans == {}
    assert "st60_al" in t["tags"] and "dma20_al" in t["tags"] and t["tags"]["move_adr"] > 0
    assert r.orders == 2  # the ENTRY and the EXIT, both SHADOW
    assert (tmp_path / "strategy" / f"{DAY}.json").exists()
    assert r.read_day(DAY)["summary"]["closed"] == 1
    # the same bar again (the reconciler re-installing it) changes nothing
    await _feed(r, store, DAY_BARS[-1:])
    assert r.snapshot()["summary"]["signals"] == 1 and r.orders == 2


async def test_a_position_still_open_at_the_last_bar_is_closed_there(tmp_path):
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store, rule="fixed_1to2")
    await _feed(r, store, DAY_BARS, pad_to=75)
    t = r.snapshot()["trades"][0]
    assert t["exit_reason"] == "close" and t["bars_held"] == 64 and t["closed_ts"] == OPEN + 75 * 300


async def test_the_filter_skips_the_signal_but_records_it(tmp_path):
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store, filt="xs_top20")  # no peers → no rank → the tag is absent → not met
    await _feed(r, store, DAY_BARS)
    s = r.snapshot()["summary"]
    assert s["signals"] == 1 and s["taken"] == 0 and s["skipped"] == 1 and r.orders == 0
    assert "filter" in r.snapshot()["signals"][0]["why"]


async def test_an_index_root_is_recorded_but_not_booked(tmp_path):
    """Day 1 (2026-10-27) shadow-traded NIFTY and BANKNIFTY as ₹25,000 of stock: the index roots carry an
    NSE_EQ leg (kind EQUITY) in the catalogue. The sequence is still recorded; nothing is booked."""
    store = BarStore()
    _seed_daily(store, "NIFTY", "999920000")
    r = _runner(tmp_path, store, symbols=("NIFTY",))
    await _feed(r, store, DAY_BARS, symbol="NIFTY", code="999920000")
    snap = r.snapshot()
    assert snap["universe"] == 0 and snap["with_levels"] == 1
    assert snap["summary"]["signals"] == 1 and snap["summary"]["taken"] == 0 and r.orders == 0
    assert snap["signals"][0]["symbol"] == "NIFTY" and snap["signals"][0]["why"].startswith("index")
    assert [s["symbol"] for s in snap["stages"]] == ["NIFTY"]


async def test_a_halt_rejects_the_entry_and_the_signal_says_so(tmp_path):
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store)
    r.gateway._halted = lambda: (True, "manual")
    await _feed(r, store, DAY_BARS)
    sig = r.snapshot()["signals"][0]
    assert not sig["taken"] and "REJECTED_HALT" in sig["why"] and r.snapshot()["summary"]["open"] == 0


async def test_the_runner_follows_the_engine_when_the_universe_dict_is_replaced(settings):
    """The engine assigns a new ``underlyings`` dict once the universe is built (and on a rebuild). The
    first live boot showed the strategy with 0 stocks: it held the empty dict from construction."""
    from kotsin_nse.engine import Engine

    e = Engine(settings)
    await e.start()
    try:
        assert e.strategy is not None and e.strategy.underlyings == {}
        e.underlyings = {"ABC": _inst()}
        assert e.strategy.underlyings["ABC"].scrip_code == "1"
        assert e.strategy.snapshot()["universe"] == 1
    finally:
        await e.stop()


async def test_a_running_days_candle_stamped_before_the_open_is_not_the_previous_day(tmp_path):
    """2026-10-27: the 09:25 restart seeded NIFTY's running daily candle at 09:14 and 'the last 1d bar before
    the open' was that — PDH/PDL/PDC were today's first 5m bar and every level was fiction. Dates, not ts."""
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    store.seed("ABC", "1d", [_ubar("ABC", "1", "1d", ist_ts(DAY, "09:14"), 120.0, 125.0, 115.0, 120.0)])
    r = _runner(tmp_path, store)
    await _feed(r, store, DAY_BARS, pad_to=75)
    snap = r.snapshot()
    lv = snap["stages"][0]["levels"]
    assert (lv["PDH"], lv["PDL"], lv["PDC"]) == PREV and snap["stages"][0]["closes"]["day"] < DAY
    assert snap["summary"]["signals"] == 1 and snap["signals"][0]["entry"] == 106.8


async def test_the_brokers_candle_wins_over_the_live_day_bar_for_the_same_date(tmp_path):
    """A long-running engine holds two bars for yesterday: the live 1d bar it closed at 15:30 (ts 09:15) and,
    after a re-backfill, the broker's candle at 00:00. One date, one bar: the exchange's."""
    store = BarStore()
    _seed_daily(store, "ABC", "1")  # the previous session as a LIVE bar at 09:15 with PREV = (110, 100, 104)
    rest = _ubar("ABC", "1", "1d", ist_ts(DAY, "00:00") - 86400, 104.0, 112.0, 100.0, 104.0)
    rest.source = BarSource.REST
    store.seed("ABC", "1d", [rest])
    r = _runner(tmp_path, store)
    await _feed(r, store, DAY_BARS, pad_to=75)
    lv = r.snapshot()["stages"][0]["levels"]
    assert lv["PDH"] == 112.0 and lv["PDL"] == 100.0


async def test_pdc_last_builds_the_cpr_on_the_previous_sessions_last_continuous_trade(tmp_path):
    """AXISBANK 2026-10-26: the closing auction's price 1220.0 (the broker's daily candle, the official close
    since 2026-08-03), the last continuous trade 1225.0 (the close of the 14:45 30m bar); the owner's Kite CPR
    implied 1224.97. ``KN_STRATEGY_PDC=last`` draws his levels; the auction print (in the 15:15 bucket on some
    days) is not a trade of the continuous session; an index has no auction and keeps its official close."""
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    prev = ist_ts(DAY, "09:15") - 86400
    store.seed("ABC", "30m", [_ubar("ABC", "1", "30m", prev + 5.5 * 3600, 104.5, 105.2, 104.4, 105.0),  # 14:45–15:15
                              _ubar("ABC", "1", "30m", prev + 6 * 3600, 104.0, 104.0, 104.0, 104.0)])  # 15:15: the auction print
    r = _runner(tmp_path, store, strategy_pdc="last")
    await _feed(r, store, DAY_BARS, pad_to=75)
    snap = r.snapshot()
    st = snap["stages"][0]
    assert snap["pdc"] == "last" and {k: st["closes"][k] for k in ("official", "last", "used")} == {"official": 104.0, "last": 105.0, "used": "last"}
    assert st["closes"]["hl"] == "daily"  # two 30m bars are not a session: the daily candle's high and low stand
    assert st["levels"] == cr.day_levels(110.0, 100.0, 105.0) and st["levels"]["P"] != cr.day_levels(*PREV)["P"]
    # an index: no continuous-trade close, the official one builds the levels
    store2 = BarStore()
    _seed_daily(store2, "NIFTY", "999920000")
    store2.seed("NIFTY", "30m", [_ubar("NIFTY", "999920000", "30m", prev + 5.5 * 3600, 104.5, 105.2, 104.4, 105.0)])
    r2 = _runner(tmp_path, store2, symbols=("NIFTY",), strategy_pdc="last")
    await _feed(r2, store2, DAY_BARS, symbol="NIFTY", code="999920000", pad_to=75)
    st2 = r2.snapshot()["stages"][0]
    assert st2["closes"]["last"] is None and st2["levels"] == cr.day_levels(*PREV)
    # the official close is the default
    assert _runner(tmp_path, store).pdc == "official"



async def test_an_entry_past_the_move_cap_is_recorded_not_taken(tmp_path):
    """28 Oct 2026: RECLTD, PREMIERENE and HINDUNILVR were entered after a full day's range had been travelled;
    on the dev window the 604 entries past 0.6 ADR lose. DAY_BARS moves 0.50 ADR from the open by the entry."""
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store, strategy_max_move_adr=0.4)
    await _feed(r, store, DAY_BARS, pad_to=75)
    s = r.snapshot()
    assert s["summary"]["signals"] == 1 and s["summary"]["taken"] == 0 and "ADR" in s["signals"][0]["why"] and r.orders == 0
    assert s["signals"][0]["nearest"]["name"] == "R1" and s["max_move_adr"] == 0.4 and s["walls_round"] is False
    assert all(w["name"] != "ROUND" for w in s["signals"][0]["walls"])


async def test_an_entry_at_or_after_the_cutoff_is_recorded_not_taken(tmp_path):
    """29 Oct 2026, LT: entered at 14:50 for a wall 98 bps away with seven bars left; on the dev window the entries
    from 14:30 on lose −17.4 net with 26 % winners. A cutoff at the entry bar's label; "" switches it off."""
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store, strategy_entry_cutoff="09:15")  # every entry bar of the day is at or after it
    await _feed(r, store, DAY_BARS, pad_to=75)
    s = r.snapshot()
    assert s["summary"]["signals"] == 1 and s["summary"]["taken"] == 0 and "cutoff" in s["signals"][0]["why"] and r.orders == 0
    assert s["entry_cutoff"] == "09:15"
    store2 = BarStore()
    _seed_daily(store2, "ABC", "1")
    r2 = _runner(tmp_path / "off", store2, strategy_entry_cutoff="")
    await _feed(r2, store2, DAY_BARS, pad_to=75)
    assert r2.snapshot()["summary"]["taken"] == 1


async def test_pdc_last_takes_the_previous_days_high_and_low_from_continuous_trades(tmp_path):
    """27 Oct 2026, PREMIERENE: the 15:28 auction printed 862.00 against a last trade of 842; the daily candle's
    high and close became 862 and the 28th's PDH and R1 were that one print. With twelve 30m bars of the
    session the high and low come from them; the auction's 15:15 bucket is not continuous trading."""
    store = BarStore()
    _seed_daily(store, "ABC", "1")  # the daily candle: H 110, L 100, C 104
    prev = ist_ts(DAY, "09:15") - 86400
    rows = [_ubar("ABC", "1", "30m", prev + i * 1800, 104.0, 108.0, 101.0, 104.0) for i in range(12)]  # 09:15 … 14:45
    rows[-1] = _ubar("ABC", "1", "30m", prev + 11 * 1800, 104.5, 108.5, 104.4, 105.0)  # 14:45–15:15: the last continuous trade 105
    rows.append(_ubar("ABC", "1", "30m", prev + 12 * 1800, 110.0, 110.0, 110.0, 110.0))  # 15:15: the auction print at the daily high
    store.seed("ABC", "30m", rows)
    r = _runner(tmp_path, store, strategy_pdc="last")
    await _feed(r, store, DAY_BARS, pad_to=75)
    st = r.snapshot()["stages"][0]
    assert st["closes"]["hl"] == "continuous" and (st["closes"]["high"], st["closes"]["low"], st["closes"]["high_official"]) == (108.5, 101.0, 110.0)
    assert st["levels"] == cr.day_levels(108.5, 101.0, 105.0)
    # asked for the daily candle instead: the print stays in
    r2 = _runner(tmp_path, BarStore(), strategy_pdc="last", strategy_levels_hl="daily")
    assert r2.s.strategy_levels_hl == "daily"


async def test_every_signal_carries_the_engines_readings_and_the_previous_sessions_official_oi(tmp_path):
    """30 Oct 2026: the positional book carried the F&O board, the order flow and NSE's closing OI on each entry;
    the intraday signals carried none of it (owner: "OI, OFI, micro structure and short long buildup ... factor
    in this enrich data"). Every signal, taken or not, now records the engine's readings at the signal and the
    previous session's official OI; a reader that fails is logged and never blocks the entry."""
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store)
    asked: list[tuple[str, date]] = []

    def bhav(sym: str, day: date) -> dict:
        asked.append((sym, day))
        return {"day": day.isoformat(), "build_up": "short build-up"}

    r.enrich = lambda sym: {"fno": {"futures_state": "long build-up", "x_pcr_oi": 0.8}, "flow": {"ofi": 12.5, "delta": -300}, "micro": {"queue_imbalance": 0.2}}
    r.bhav = bhav
    await _feed(r, store, DAY_BARS, pad_to=75)
    s = r.snapshot()
    sig = s["signals"][0]
    assert sig["context"]["fno"]["futures_state"] == "long build-up" and sig["context"]["flow"]["ofi"] == 12.5 and sig["context"]["micro"]["queue_imbalance"] == 0.2
    assert sig["bhav"] == {"day": s["stages"][0]["closes"]["day"], "build_up": "short build-up"}
    assert asked == [("ABC", date.fromisoformat(s["stages"][0]["closes"]["day"]))]  # the previous session, not today
    assert s["trades"][0]["context"] == sig["context"] and s["trades"][0]["bhav"] == sig["bhav"]

    store2 = BarStore()
    _seed_daily(store2, "ABC", "1")
    r2 = _runner(tmp_path / "fail", store2)

    def boom(sym: str) -> dict:
        raise RuntimeError("no board yet")

    r2.enrich = boom
    r2.bhav = lambda sym, day: None
    await _feed(r2, store2, DAY_BARS, pad_to=75)
    s2 = r2.snapshot()
    assert s2["summary"]["taken"] == 1 and s2["signals"][0]["context"] == {} and s2["signals"][0]["bhav"] is None
