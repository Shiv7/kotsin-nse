"""The Analysis page's pure parts: Fibonacci of the last swing, build-up across timeframes, and the
two hedged option-selling setups."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kotsin_nse.analysis.bigpicture import big_picture, fibonacci
from kotsin_nse.analysis.buildup import label, mtf, recent
from kotsin_nse.analysis.hedge import setups
from kotsin_nse.market.session import ist_at


def _daily(closes: list[float]) -> list[tuple[int, float, float, float, float, float]]:
    rows, d = [], date(2026, 6, 1)
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append((ist_at(d, "00:00"), c, c + 5, c - 5, c, 1e6))
        d += timedelta(days=1)
    return rows


def test_a_down_move_retraced_by_a_third_reads_as_such():
    rows = _daily([1300 - 2 * i for i in range(50)] + [1202 + 3 * i for i in range(10)])  # high first, then low, then a bounce
    f = fibonacci(rows, last=1235.0)
    assert f is not None and f["move"] == "DOWN"
    hi, lo = f["swing_high"], f["swing_low"]
    assert f["retraced_pct"] == pytest.approx((1235 - lo) / (hi - lo) * 100, abs=0.1)
    assert f["levels"][1]["price"] == pytest.approx(lo + 0.382 * (hi - lo), abs=0.01)


def test_the_big_picture_uses_only_sessions_before_today():
    rows = _daily([1000 + i for i in range(260)])
    today = date.fromordinal(max(date.fromtimestamp(r[0]).toordinal() for r in rows))
    bp = big_picture(rows, today)
    assert bp is not None and bp["prev_close"] == 1258.0, "today's own bar is not a previous session"
    assert bp["trend"].startswith("UP") and bp["1y"]["position_pct"] > 90


def test_build_up_labels_and_windows():
    assert (label(1.0, 2.0), label(-1.0, 2.0), label(1.0, -2.0), label(-1.0, -2.0), label(1.0, 0.1)) == ("LB", "SB", "SC", "LU", "FLAT")
    now = 1_792_731_600.0
    price = [(now - 3600, 100.0), (now - 900, 101.0), (now, 100.0)]
    oi = [(now - 3600, 1000.0), (now - 900, 1010.0), (now, 1030.0)]
    rows = {r["window"]: r for r in mtf(price, oi, now, prev_close=102.0, oi_ref=950.0)}
    assert rows["15 min"]["build_up"] == "SB" and rows["1 hour"]["build_up"] == "LB" and rows["today"]["build_up"] == "SB"


def _chain(future: float) -> list[dict]:
    """Premiums = intrinsic + a time value that decays away from the money (a smooth, sane chain)."""
    import math

    out = []
    for k in range(1150, 1240, 10):
        x = (k - future) / 20
        tv = 14 * math.exp(-abs(k - future) / 30)
        cd = max(0.02, min(0.98, 0.5 - x * 0.2))
        out.append({"strike": float(k),
                    "CE": {"ltp": round(max(future - k, 0) + tv, 2), "delta": cd, "theta": -1.0, "vega": 0.4},
                    "PE": {"ltp": round(max(k - future, 0) + tv, 2), "delta": cd - 1, "theta": -1.0, "vega": 0.4}})
    return out


def test_a_bearish_bias_gets_a_bear_call_spread_above_resistance_and_both_get_a_condor():
    out = setups(_chain(1190.0), 1190.0, "Strong Bear", 500, call_wall=1200.0, put_wall=1160.0, resistance=1196.0, support=1177.0)
    names = [s["name"] for s in out["setups"]]
    assert names == ["Bear call spread", "Iron condor"]
    bcs = out["setups"][0]
    sell, buy = bcs["legs"]
    assert sell["action"] == "SELL" and sell["strike"] >= 1200 and buy["strike"] == sell["strike"] + 20
    assert bcs["max_loss"] == pytest.approx(bcs["width"] - bcs["credit"]) and bcs["credit_lot"] == round(bcs["credit"] * 500)
    ic = out["setups"][1]
    assert len(ic["legs"]) == 4 and ic["breakeven"][0] > 1190 > ic["breakeven"][1]


def test_no_chain_no_setups():
    assert setups([], 1190.0, "Bull", 500, None, None, None, None)["setups"] == []


def test_an_intraday_window_across_a_recording_gap_is_not_read():
    now = 1_792_731_600.0
    price = [(now - 7200, 100.0), (now - 60, 99.0), (now, 99.0)]  # nothing recorded for two hours before the last minute
    oi = [(now - 7200, 1000.0), (now - 60, 980.0), (now, 975.0)]
    assert [r["window"] for r in mtf(price, oi, now, prev_close=None, oi_ref=None)] == []


def test_the_last_15_minutes_read_the_turn_on_the_same_strikes():
    """A sell-off since the open, then the last 15 minutes: price up, futures OI up (fresh longs),
    puts written; a strike subscribed mid-window (re-centring) must not count as new OI."""
    now = 1_000_000.0
    price = [(now - 3600, 100.0), (now - 900, 98.0), (now, 99.0)]
    fut = [(now - 3600, 1000.0), (now - 900, 1000.0), (now, 1010.0)]
    opts = [
        ("PE", 95.0, [(now - 3600, 500.0), (now - 900, 500.0), (now - 300, 560.0)]),
        ("PE", 100.0, [(now - 400, 9999.0)]),  # appeared 400 s ago: no point at the window's start
        ("CE", 105.0, [(now - 3600, 400.0), (now - 900, 400.0), (now, 380.0)]),
    ]
    r15, r60 = recent(price, fut, opts, now)
    assert r15["window"] == "15 min" and r15["futures"]["build_up"] == "LB"
    assert r15["puts"] == {"oi_chg": 60.0, "oi_chg_pct": 12.0, "strikes": 1}
    assert r15["calls"]["oi_chg"] == -20.0
    assert (r15["pcr_then"], r15["pcr_now"]) == (1.25, round(560 / 380, 3))
    assert r60["futures"]["build_up"] == "SB" and r60["futures"]["price_chg_pct"] == -1.0, "the hour still reads shorts adding"


def test_no_futures_point_near_the_start_no_window():
    now = 1_000_000.0
    assert recent([(now - 5000, 100.0), (now, 101.0)], [(now - 5000, 1.0), (now, 2.0)], [], now) == []


def test_the_summary_is_never_a_call_on_its_own_and_the_daily_cap_stops_paid_calls():
    import asyncio
    import time as _t

    from kotsin_nse.analysis.llm import DAILY_CAP, BudgetExceeded, Summarizer

    sm = Summarizer("sk-test-not-a-real-key")
    sm._cache["BEL"] = {"symbol": "BEL", "text": "old", "created_ts": _t.time() - 86_400}
    assert sm.cached("BEL")["text"] == "old", "a day-old summary is still served, not remade"
    got = asyncio.run(sm.summarize("BEL", {}, refresh=False))
    assert got["cached"] is True
    sm.calls_left()
    sm._calls = DAILY_CAP
    with pytest.raises(BudgetExceeded):
        asyncio.run(sm.summarize("BEL", {}, refresh=True))  # refused before any network call
