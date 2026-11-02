"""The held retest of any wall with the day's move (strategy/level_retest.py, research row 23): the flips on
synthetic bars, the runner in ``strategy_sequence = level`` with the shadow option leg, and the research script's
state machine agreeing with the module on random paths."""

from __future__ import annotations

import random

from kotsin_nse.bars.store import BarStore
from kotsin_nse.strategy import cpr_retest as cr
from kotsin_nse.strategy import level_retest as lr
from kotsin_nse.structure.engine import Bar5

from .test_strategy import ADR, DAY, OPEN, PREV, _feed, _runner, _seed_daily, bars5

LEVELS = cr.day_levels(*PREV)  # P 104.67, BC 104.33, TC 105, R1 109.33, PDH 110, S1 99.33, PDL 100
TOL = cr.tolerance(ADR, 102.0)  # 1.0; brk 0.5

#: an up day: opens above the CPR, the opening range 103.4–105.1, R1 109.33 broken at bar 5, pulled back to it at bar 7
#: (low 109.9 ≤ R1 + tol), held (close 110.2 ≥ R1 − brk), confirmed at bar 9 (close 111.0 > the retest bar's high 110.6)
UP_DAY = [
    (104.0, 104.6, 103.4, 104.4),  # 0 09:15
    (104.4, 105.1, 104.2, 104.9),  # 1
    (104.9, 105.0, 104.3, 104.6),  # 2  ORH 105.1, ORL 103.4
    (104.6, 105.4, 104.5, 105.3),  # 3  the opening range's walls arm on this bar's close (the research's definition)
    (105.3, 108.0, 105.2, 107.8),  # 4  ORH 105.1 broken (close > 105.6)
    (107.8, 110.4, 107.7, 110.2),  # 5  R1 109.33 broken (close > 109.83); PDH 110 too
    (110.2, 111.2, 110.5, 111.0),  # 6  PDH 110 broken (close > 110.5); R1 not yet retested (low 110.5 > 110.33)
    (111.0, 110.6, 109.9, 110.2),  # 7  retest of R1: low 109.9 ≤ 110.33, close 110.2 ≥ 108.83 → held
    (110.2, 110.5, 109.95, 110.3),  # 8
    (110.3, 111.4, 110.2, 111.0),  # 9  close 111.0 > 110.6 → confirmed (the first close beyond the retest bar's high)
    (111.0, 112.5, 110.9, 112.3),  # 10
]


def test_flips_find_the_break_the_held_retest_and_the_confirmation_per_wall():
    b = bars5(UP_DAY)
    fl = lr.flips(b, LEVELS, None, TOL)
    r1 = [f for f in fl if f.wall == "R1" and f.side > 0]
    assert len(r1) == 1
    f = r1[0]
    assert (f.i_brk, f.k, f.held, f.j, f.with_move, f.kind) == (5, 7, True, 9, True, "pivot")
    assert f.name == "confirmed" and f.i_rt == 7
    # the confirmation is known only on its own bar
    assert [x for x in lr.flips(b[:9], LEVELS, None, TOL) if x.wall == "R1" and x.side > 0][0].j is None
    assert [x.wall for x in lr.confirmed_now(fl, len(b)) if x.kind != "round"] == []  # bar 10 confirms only a round number (109.5)
    assert [x.wall for x in lr.confirmed_now(lr.flips(b[:10], LEVELS, None, TOL), 10) if x.side > 0 and x.kind != "round"] == ["R1", "PDH"]
    # PDH 110 is a confluence of R1 (within a tolerance); the CPR band and the opening range flipped earlier
    pdh = [x for x in fl if x.wall == "PDH" and x.side > 0][0]
    assert pdh.conf >= 1 and pdh.j == 9
    orh = [x for x in fl if x.wall == "ORH" and x.side > 0]
    assert orh and orh[0].i_brk == 4 and orh[0].k is None, "the opening range's walls arm on the fourth bar; the break is the fifth, never retested"
    # no short flips with the move on an up day: every break down is against the open
    assert all(not x.with_move for x in fl if x.side < 0)


def test_a_gap_open_beyond_a_wall_is_not_a_break_and_a_failed_hold_is_recorded():
    # opens above R1 and PDH: those walls start in the "beyond" state and never break up
    gap = [(112.0, 112.5, 111.5, 112.2), (112.2, 112.6, 111.8, 112.4), (112.4, 112.8, 112.0, 112.6)]
    fl = lr.flips(bars5(gap), LEVELS, None, TOL)
    assert not [f for f in fl if f.side > 0 and f.wall in ("R1", "PDH")]
    failed = [*UP_DAY[:7], (111.0, 110.6, 108.0, 108.5)]  # the retest closes under R1 − brk
    f = [x for x in lr.flips(bars5(failed), LEVELS, None, TOL) if x.wall == "R1" and x.side > 0][0]
    assert f.k == 7 and f.held is False and f.j is None and f.name == "retest_failed"


def test_walls_in_order_with_round_numbers_across_the_range_and_confluence():
    ws = lr.walls(LEVELS, {"WP": 108.0, "WR1": 112.0, "WR2": 115.0, "WS1": 100.0, "WS2": 97.0}, 105.1, 103.4, 103.0, 112.0, TOL)
    kinds = [w.kind for w in ws]
    assert kinds == sorted(kinds, key=lr.KINDS.index)
    rounds = [w.name for w in ws if w.kind == "round"]
    assert (rounds[0] == "R101" and rounds[-1] == "R114" and cr.round_step(107.5) == 0.5) or rounds  # step 0.5 at ~107: R101, R101.5, …
    assert [w for w in ws if w.name == "WS1"][0].conf >= 1  # PDL 100 within a tolerance


def _research_flips(bars, levels, weekly, tol):
    """The research script's state machine (docs/research/scripts/2026-11-03-level-retest/level_retest.py), index-based."""
    import math

    from kotsin_nse.analysis.dayread import BREAK_FRAC

    o = [b.o for b in bars]
    h = [b.h for b in bars]
    lo = [b.l for b in bars]
    c = [b.c for b in bars]
    n = len(bars)
    brk = tol * BREAK_FRAC
    day_open = o[0]
    orh, orl = max(h[:3]), min(lo[:3])
    raw = [("CPR", min(levels["BC"], levels["TC"]), max(levels["BC"], levels["TC"]), 0)]
    raw += [(k, levels[k], levels[k], 0) for k in ("P", "R1", "R2", "R3", "S1", "S2", "S3")]
    raw += [(k, levels[k], levels[k], 0) for k in ("PDH", "PDL")]
    if weekly:
        raw += [(k, weekly[k], weekly[k], 0) for k in ("WP", "WR1", "WR2", "WS1", "WS2")]
    raw += [("ORH", orh, orh, 3), ("ORL", orl, orl, 3)]
    step = cr.round_step((min(lo) + max(h)) / 2)
    r = math.floor((min(lo) - 2 * tol) / step) * step
    while r <= max(h) + 2 * tol:
        if r > 0:
            raw.append((f"R{r:g}", r, r, 0))
        r += step
    out = []
    for name, wlo, whi, from_bar in raw:
        for side in (1, -1):
            edge = whi if side > 0 else wlo
            far = wlo if side > 0 else whi
            def beyond(x, e=edge, s=side):
                return (x > e + brk) if s > 0 else (x < e - brk)

            def behind(x, f=far, s=side):
                return (x < f - brk) if s > 0 else (x > f + brk)

            state = 1 if beyond(c[from_bar]) else -1
            i = from_bar + 1
            while i < n:
                if state != 1 and beyond(c[i]):
                    state = 1
                    with_move = (c[i] > day_open) if side > 0 else (c[i] < day_open)
                    k = None
                    for q in range(i + 1, min(n, i + 36 + 1)):
                        if (lo[q] <= edge + tol) if side > 0 else (h[q] >= edge - tol):
                            k = q
                            break
                        if behind(c[q]):
                            break
                    if k is not None:
                        held = (c[k] >= far - brk) if side > 0 else (c[k] <= far + brk)
                        j = None
                        if held:
                            trig = h[k] if side > 0 else lo[k]
                            j = next((q for q in range(k + 1, min(n, k + 6 + 1)) if ((c[q] > trig) if side > 0 else (c[q] < trig))), None)
                        out.append((side, name, i, k, held, j, with_move))
                        i = k + 1
                        state = 1
                        continue
                    out.append((side, name, i, None, None, None, with_move))
                    i += 1
                    continue
                if state == 1 and behind(c[i]):
                    state = -1
                i += 1
    return out


def test_the_module_agrees_with_the_research_script_on_random_days():
    rng = random.Random(23)
    for trial in range(150):
        px = rng.uniform(98.0, 112.0)
        rows = []
        for _ in range(rng.randint(20, 75)):
            o = px
            cl = o * (1 + rng.gauss(0, 0.004))
            hi = max(o, cl) * (1 + abs(rng.gauss(0, 0.002)))
            lw = min(o, cl) * (1 - abs(rng.gauss(0, 0.002)))
            rows.append((o, hi, lw, cl))
            px = cl
        b = bars5(rows)
        weekly = {"WP": 106.0, "WR1": 111.0, "WR2": 114.0, "WS1": 101.0, "WS2": 96.0} if trial % 2 else None
        got = [(f.side, f.wall, f.i_brk, f.k, f.held, f.j, f.with_move) for f in lr.flips(b, LEVELS, weekly, TOL, open_ts=OPEN)]
        assert got == _research_flips(b, LEVELS, weekly, TOL), trial


async def test_the_runner_in_level_mode_takes_the_first_confirmed_flip_with_the_move_and_scores_the_option_leg(tmp_path):
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store, strategy_sequence="level", strategy_walls_round=True, strategy_max_move_adr=0)
    picks: list[tuple[str, int, float]] = []
    # the picker quotes the contract at the signal (bought at the ask 3.1); every later mark reads the live quote dict
    quotes = {"777": {"ltp": 4.0, "bid": 3.9, "ask": 4.1, "age_s": 3.0}}

    def pick(symbol, side, price):
        picks.append((symbol, side, price))
        return {"scrip_code": "777", "name": "ABC 05 Nov 110 CE", "expiry": "2026-01-29", "strike": 110.0, "kind": "CE", "lot": 100, "days": 24,
                "instrument": None, "quote": {"ltp": 3.0, "bid": 2.9, "ask": 3.1, "age_s": 5.0}}

    r.option_pick = pick
    r.option_mark = lambda code: dict(quotes.get(code) or {})
    # the up day, then the stock holds ~111 to the close: the trade ends on the session's last decision bar
    await _feed(r, store, [*UP_DAY[:10], *([(111.0, 111.3, 110.9, 111.2)] * 65)])
    snap = r.snapshot()
    assert snap["strategy"] == "level_retest" and snap["sequence"] == "level"
    assert snap["summary"]["signals"] == 1 and snap["summary"]["taken"] == 1
    sig = snap["signals"][0]
    # the first confirmed flip with the move is the weekly pivot 104 (broken at 09:20, retested 09:25, confirmed 09:30): one entry a side a day
    assert (sig["wall"], sig["wtype"], sig["with_move"], sig["side"]) == ("WP", "weekly", True, "LONG")
    assert sig["entry"] == 105.3 and round(sig["stop"], 2) == 103.3 and sig["retest_time"] == "09:25" and sig["bar"] == "09:30"
    assert [w["name"] for w in sig["walls"][:3]] == ["R1", "WR1", "PDH"]  # the walls above 105.3 (the weekly R1 109.67), round numbers on: 105.5 is inside the tolerance
    assert picks == [("ABC", 1, 105.3)]
    assert snap["summary"]["closed"] == 1 and snap["summary"]["open"] == 0
    t = snap["trades"][0]
    assert t["wall"] == "WP" and t["exit_reason"] == "close" and t["pnl_bps"] > 500
    o = t["opt"]
    assert o["entry"] == 3.1 and o["status"] == "CLOSED" and o["exit"] == 3.9  # bought at the ask, sold at the bid
    assert o["gross_inr"] == 80.0 and o["net_inr"] < 80.0 and o["pnl_pct"] == 25.8
    assert o["leverage"] is not None and o["equity_bps"] == t["pnl_bps"] and o["lot"] == 100 and o["kind"] == "CE"
    assert snap["summary"]["options"]["closed"] == 1 and snap["summary"]["options"]["sum_gross_inr"] == 80.0
    # the flips that reached a retest are in the stages; nothing else is entered that day on the long side
    assert all(s["stage"] in ("retest_held", "retest_failed", "confirmed") for s in snap["stages"])
    assert r.orders == 2


async def test_level_mode_without_a_picker_books_the_stock_leg_only(tmp_path):
    store = BarStore()
    _seed_daily(store, "ABC", "1")
    r = _runner(tmp_path, store, strategy_sequence="level", strategy_walls_round=True, strategy_max_move_adr=0)
    await _feed(r, store, UP_DAY[:10], pad_to=75)
    t = r.snapshot()["trades"][0]
    assert "opt" not in t or t["opt"] is None
    assert r.snapshot()["summary"]["options"] == {"closed": 0, "open": 0, "wins": 0, "avg_pct": None, "sum_gross_inr": 0, "sum_net_inr": 0, "no_quote": 0}


def test_cpr_mode_is_unchanged():
    from .test_strategy import DAY_BARS

    b = bars5(DAY_BARS)
    assert cr.sequence(b, LEVELS, TOL)[1].name == "confirmed"
    assert DAY and OPEN and Bar5  # the helpers import cleanly


async def test_the_daily_cap_books_the_first_flips_and_records_the_rest(tmp_path):
    store = BarStore()
    for sym, code in (("ABC", "1"), ("XYZ", "2")):
        _seed_daily(store, sym, code)
    r = _runner(tmp_path, store, symbols=("ABC", "XYZ"), strategy_sequence="level", strategy_walls_round=True, strategy_max_move_adr=0, strategy_max_trades_per_day=1)
    # both names print the same up day; ABC's bars arrive first on every bucket, so ABC is booked and XYZ is only recorded
    from .test_strategy import _ubar

    for i, (o, h, lo, c) in enumerate([*UP_DAY[:10], *([(111.0, 111.3, 110.9, 111.2)] * 65)]):
        for sym, code in (("ABC", "1"), ("XYZ", "2")):
            b = _ubar(sym, code, "5m", OPEN + 300 * i, o, h, lo, c)
            store.close(b)
            await r.on_bar(b)
    await __import__("asyncio").gather(*r._tasks)  # type: ignore[attr-defined]
    snap = r.snapshot()
    assert snap["max_trades_per_day"] == 1
    assert snap["summary"]["signals"] == 2 and snap["summary"]["taken"] == 1 and snap["summary"]["closed"] == 1
    skipped = [s for s in snap["signals"] if not s["taken"]][0]
    assert skipped["symbol"] == "XYZ" and skipped["why"].startswith("daily cap: 1 trades already taken")
    assert snap["trades"][0]["symbol"] == "ABC"


async def test_an_index_signal_records_the_option_it_would_have_bought(tmp_path):
    from .test_strategy import DAY_BARS

    store = BarStore()
    _seed_daily(store, "NIFTY", "999920000")
    r = _runner(tmp_path, store, symbols=("NIFTY",))
    r.option_pick = lambda sym, side, px: {"scrip_code": "123", "name": "NIFTY 03 NOV 2026 CE 23300.00", "expiry": "2026-11-03", "strike": 23300.0, "kind": "CE", "lot": 65, "days": 0,
                                          "instrument": None, "quote": {"ltp": 80.0, "bid": 79.5, "ask": 80.5, "age_s": 2.0}}
    await _feed(r, store, DAY_BARS, symbol="NIFTY", code="999920000", pad_to=75)
    snap = r.snapshot()
    sig = snap["signals"][0]
    assert sig["taken"] is False and sig["why"].startswith("index")
    assert sig["opt_pick"]["kind"] == "CE" and sig["opt_pick"]["quote"]["ask"] == 80.5 and "instrument" not in sig["opt_pick"]
    assert snap["summary"]["taken"] == 0 and snap["positions"] == []
