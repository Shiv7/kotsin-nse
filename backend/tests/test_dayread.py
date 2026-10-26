"""The day read on a hand-built session: support at S1, the break through the CPR, the retest that
holds, swings labelled as they become known, and who was positioning on each leg."""

from __future__ import annotations

from datetime import date

from kotsin_nse.analysis.dayread import (
    Leg,
    bands,
    cpr,
    despike,
    leg_oi,
    leg_options,
    level_events,
    read_day,
    structure_events,
)
from kotsin_nse.structure.engine import Bar5

T0 = 1_790_000_000
PREV = (110.0, 100.0, 104.0)  # h, l, c → P 104.67, BC 105, TC 104.33 (TC/BC swap: band 104.33–105), S1 99.33, R1 109.33


def bars(rows):
    return [Bar5(T0 + 300 * i, o, h, lo, c, 100) for i, (o, h, lo, c) in enumerate(rows)]


ROWS = [  # o, h, l, c — a morning: dip to S1, hold, rally through the CPR, pull back to it, hold, resume
    (102.0, 102.5, 99.4, 99.9),  # low 99.4 within a tolerance of S1 99.33, closes above: support held
    (99.9, 100.8, 99.6, 100.6),
    (100.6, 101.5, 100.3, 101.3),
    (101.3, 102.0, 101.0, 101.2),
    (101.2, 103.0, 101.1, 102.8),  # swing low at bar 1 (99.6) becomes known here
    (102.8, 106.0, 102.7, 105.8),  # closes above the CPR (105 + half a tolerance)
    (105.8, 107.0, 105.5, 106.5),
    (106.5, 106.7, 105.9, 106.0),
    (106.0, 106.2, 104.9, 105.1),  # pulls back into the band, low 104.9, closes inside: CPR held
    (105.1, 106.8, 105.0, 106.6),
    (106.6, 108.0, 106.4, 107.8),  # swing high 107 at bar 6 was taken at bar 10? no: 107.8 > 107 here → BOS up
    (107.8, 108.2, 107.5, 107.9),
]


def test_cpr_levels_from_the_previous_day():
    lv = cpr(*PREV)
    assert lv["P"] == 104.67 and lv["BC"] == 104.33 and lv["TC"] == 105.0
    assert lv["S1"] == 99.33 and lv["R1"] == 109.33 and (lv["PDH"], lv["PDL"], lv["PDC"]) == (110.0, 100.0, 104.0)


def test_level_events_tell_the_support_the_break_and_the_retest_once_each():
    lv = cpr(*PREV)
    ev = level_events(bars(ROWS), bands(lv), tol=0.5)
    texts = [(e.kind, e.level, e.text) for e in ev]
    assert texts[0][:2] == ("SUPPORT", "S1") and "held as support (low 99.40)" in texts[0][2]
    assert ("BREAK_UP", "CPR") in [t[:2] for t in texts]
    retest = [t for t in texts if t[0] == "SUPPORT" and t[1] == "CPR"]
    assert len(retest) == 1 and "held as support (low 104.90)" in retest[0][2]
    # the CPR fired twice (break, then retest) only because price closed two tolerances away in between
    assert [t[1] for t in texts].count("CPR") == 2


def test_swings_are_labelled_when_known_and_a_close_beyond_one_is_a_break_of_structure():
    ev, legs = structure_events(bars(ROWS))
    kinds = [(e.kind, e.price) for e in ev]
    assert ("HL", 104.9) in kinds  # the first low is "higher low" by convention (no previous one)
    assert any(k == "HH" for k, _p in kinds)
    assert any(k == "BOS_UP" for k, _p in kinds)
    assert legs and legs[-1].closed is False and (legs[0].p0, legs[0].p1) == (102.0, 107.0)  # open → first swing high


def test_leg_oi_reads_short_covering_then_long_build_up_and_ignores_a_one_frame_spike():
    oi = [(T0 + 300 * i + 300, v) for i, v in enumerate([1000, 995, 990, 985, 980, 975, 980, 986, 992, 998, 1005, 1010])]
    up1 = Leg(T0, T0 + 300 * 5, 99.6, 105.8, True)
    up2 = Leg(T0 + 300 * 8, T0 + 300 * 11, 104.9, 107.9, True)
    assert leg_oi(up1, oi)["build_up"] == "SC"
    assert leg_oi(up2, oi)["build_up"] == "LB"
    spiked = list(oi)
    spiked[6] = (spiked[6][0], 1300.0)
    assert despike(spiked)[6][1] == 986.0 and despike(oi) == oi


def test_leg_options_say_who_wrote_what_around_price():
    t = [T0 + 300 * i + 300 for i in range(12)]
    opt = {
        ("PE", 105.0): [(x, 100.0 + 10 * i) for i, x in enumerate(t)],  # puts added at the money
        ("PE", 100.0): [(x, 50.0) for x in t],
        ("CE", 110.0): [(x, 200.0 - 5 * i) for i, x in enumerate(t)],  # calls above shed
        ("CE", 150.0): [(x, 999.0) for x in t],  # far away: ignored
    }
    r = leg_options(Leg(T0, T0 + 300 * 11, 99.6, 107.9, True), 107.9, opt, step=5.0)
    assert r["puts_net"] == 110.0 and r["calls_net"] == -55.0 and "support being built" in r["read"]
    assert r["moves"][0].startswith("105PE +110")


def test_read_day_assembles_the_timeline_and_the_summary():
    oi = [(T0 + 300 * i + 300, 1000 - 2 * i) for i in range(12)]
    r = read_day("TEST", date(2026, 10, 26), bars(ROWS), PREV, adr=5.0, fut_oi=oi)
    assert r["gap_pct"] == round((102.0 / 104.0 - 1) * 100, 2) and r["tolerance"] == 0.5
    times = [e["time"] for e in r["events"]]
    assert times == sorted(times) and r["events"][0]["kind"] == "SUPPORT"
    assert r["legs"][0]["futures"]["read"] == "short covering"
    assert r["htf"]["close"]["vs_cpr"] == "above the CPR" and r["htf"]["15m"]["trend"] in ("up", "mixed")
    assert r["summary"][0].startswith("Opened 102.00") and any("Leg " in s for s in r["summary"])
