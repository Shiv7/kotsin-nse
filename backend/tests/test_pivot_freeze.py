"""The pivot contract, frozen against numbers verified outside the codebase.

Source bars are RELIANCE's official NSE sessions (bhavcopy, 2026-09-23 audit); the expected levels
are what the live engine published that morning and what the CLI report reproduced from the
broker's daily candles. Changing ``classic_pivots``, the period selection, or the previous-session
rule must fail here first — silently moving a level the exits sit on is the failure this guards.
"""

import math
from datetime import date, datetime

from kotsin_nse.bars.daily import previous_session
from kotsin_nse.bars.periods import monthly, previous_complete, weekly
from kotsin_nse.bars.pivots import classic_pivots
from kotsin_nse.bars.unified import BarSource, UnifiedBar

TODAY = date(2026, 9, 23)


def _bar(day: date, o: float, h: float, lo: float, c: float, v: float = 1e7) -> UnifiedBar:
    ts = datetime(day.year, day.month, day.day, 9, 15).timestamp()
    return UnifiedBar(symbol="RELIANCE", scrip_code="2885", tf="1d", ts=ts, open=o, high=h, low=lo,
                      close=c, volume=v, source=BarSource.REST, complete=True)


# NSE bhavcopy, RELIANCE EQ. 2026-09-14 (Monday) was an NSE holiday: there is no bar, on purpose.
SEPTEMBER = [
    _bar(date(2026, 9, 15), 1252.50, 1259.40, 1235.30, 1235.30),
    _bar(date(2026, 9, 16), 1243.00, 1255.00, 1240.00, 1240.00),
    _bar(date(2026, 9, 17), 1244.80, 1253.40, 1238.50, 1243.90),
    _bar(date(2026, 9, 18), 1245.00, 1247.30, 1226.40, 1226.40),
    _bar(date(2026, 9, 21), 1234.10, 1249.10, 1232.50, 1247.40),
    _bar(date(2026, 9, 22), 1247.60, 1251.90, 1237.40, 1240.40),
]
# Today's own bar, as the aggregator builds it from ticks during the session. Never a source.
TODAY_LIVE = UnifiedBar(symbol="RELIANCE", scrip_code="2885", tf="1d",
                        ts=datetime(2026, 9, 23, 9, 15).timestamp(), open=1242.0, high=1246.0,
                        low=1241.0, close=1243.6, volume=6e5, source=BarSource.LIVE, complete=False)
# August 2026 aggregates to H 1337.00 (05-Aug) L 1270.10 (20-Aug) C 1277.00 (31-Aug). Daily
# detail inside the month is synthetic; only the aggregate is asserted, which is all monthly uses.
AUGUST = [
    _bar(date(2026, 8, 3), 1300.0, 1310.0, 1295.0, 1305.0),
    _bar(date(2026, 8, 5), 1320.0, 1337.00, 1315.0, 1330.0),
    _bar(date(2026, 8, 12), 1325.0, 1330.0, 1300.0, 1302.0),
    _bar(date(2026, 8, 20), 1290.0, 1295.0, 1270.10, 1280.0),
    _bar(date(2026, 8, 31), 1282.0, 1290.0, 1275.0, 1277.00),
]


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, abs_tol=0.005)


def test_daily_ladder_for_23_sep_comes_from_22_sep_and_matches_the_published_levels():
    bars = AUGUST + SEPTEMBER + [TODAY_LIVE]
    src = previous_session(bars, TODAY)
    assert src is not None and datetime.fromtimestamp(src.ts).date() == date(2026, 9, 22)
    lv = classic_pivots(src.high, src.low, src.close)
    assert lv is not None
    expect = dict(pivot=1243.23, r1=1249.07, r2=1257.73, r3=1272.23, r4=1286.73,
                  s1=1234.57, s2=1228.73, s3=1214.23, s4=1199.73, tc=1244.65, bc=1241.82)
    for name, want in expect.items():
        assert _close(round(getattr(lv, name), 2), want), (name, getattr(lv, name), want)


def test_weekly_ladder_is_the_four_session_week_of_15_to_18_sep():
    bars = AUGUST + SEPTEMBER + [TODAY_LIVE]
    wk = previous_complete(weekly(bars), TODAY)
    assert wk is not None and (wk.start, wk.end) == (date(2026, 9, 15), date(2026, 9, 18))
    assert (wk.high, wk.low, wk.close) == (1259.40, 1226.40, 1226.40)
    lv = classic_pivots(wk.high, wk.low, wk.close)
    assert lv is not None
    assert _close(round(lv.pivot, 2), 1237.40) and _close(round(lv.r1, 2), 1248.40)
    assert _close(round(lv.s1, 2), 1215.40) and _close(round(lv.r2, 2), 1270.40)
    assert _close(round(lv.s2, 2), 1204.40) and _close(round(lv.tc, 2), 1242.90)
    assert _close(round(lv.bc, 2), 1231.90)


def test_monthly_ladder_is_august_not_the_running_september():
    bars = AUGUST + SEPTEMBER + [TODAY_LIVE]
    mo = previous_complete(monthly(bars), TODAY)
    assert mo is not None and (mo.start, mo.end) == (date(2026, 8, 3), date(2026, 8, 31))
    lv = classic_pivots(mo.high, mo.low, mo.close)
    assert lv is not None
    assert _close(round(lv.pivot, 2), 1294.70) and _close(round(lv.r1, 2), 1319.30)
    assert _close(round(lv.s1, 2), 1252.40) and _close(round(lv.r2, 2), 1361.60)
    assert _close(round(lv.s2, 2), 1227.80)


def test_the_stale_pipelines_inputs_would_move_every_level():
    """What the old stack computed for 22-Sep — a daily rolled up from intraday candles that stop at
    15:15 — differs only in the close, and that alone moves P by 1.37 and R1 by 2.73."""
    ours = classic_pivots(1251.90, 1237.40, 1240.40)
    theirs = classic_pivots(1251.90, 1237.40, 1244.50)
    assert ours is not None and theirs is not None
    assert _close(round(theirs.pivot - ours.pivot, 2), 1.37)
    assert _close(round(theirs.r1 - ours.r1, 2), 2.73)
