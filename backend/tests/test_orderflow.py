"""Delta estimated from snapshots: the quote rule on each snapshot's new volume."""

from __future__ import annotations

import pytest

from kotsin_nse.orderflow.meter import FlowMeter, classify, imbalance

T0 = 1_790_000_060.0  # a minute boundary (1_790_000_040) + 20 s


def test_quote_rule_then_tick_rule():
    assert classify(101.0, 100.0, 101.0, 100.5, 0) == 1  # at the ask
    assert classify(100.0, 100.0, 101.0, 100.5, 0) == -1  # at the bid
    assert classify(100.6, 100.0, 101.0, 100.5, 0) == 1  # inside, uptick
    assert classify(100.5, 100.0, 101.0, 100.5, -1) == -1  # inside, unchanged: as the last one


def test_new_volume_is_classified_and_cumulated_and_the_first_frame_only_primes():
    m = FlowMeter()
    m.on_tick("X", T0, 100.5, 1_000, 100.0, 101.0)  # primes the running total, no volume counted
    m.on_tick("X", T0 + 5, 101.0, 1_300, 100.5, 101.0)  # +300 at the previous ask → buy
    m.on_tick("X", T0 + 10, 100.5, 1_400, 100.5, 101.0)  # +100 at the previous bid → sell
    fm = m.minutes("X")[-1]
    assert (fm.buy, fm.sell, fm.delta, fm.cum_delta, fm.frames) == (300, 100, 200, 200, 3)
    m.on_tick("X", T0 + 45, 101.5, 1_500, 101.0, 101.5)  # next minute: +100 above the old ask → buy
    assert m.minutes("X")[-1].cum_delta == 300 and [x.ts for x in m.drain()] == [int(T0 // 60) * 60]


def test_depth_imbalance_is_averaged_over_the_minute():
    assert imbalance([(100, 30)], [(101, 10)]) == pytest.approx(0.5)
    m = FlowMeter()
    m.on_depth("X", T0, [(100, 30)], [(101, 10)])
    m.on_depth("X", T0 + 1, [(100, 10)], [(101, 10)])
    assert m.minutes("X")[-1].imbalance == pytest.approx(0.25)


def test_ofi_at_the_best_level_follows_cont_kukanov_stoikov():
    from kotsin_nse.orderflow.meter import ofi

    b, a = [(100.0, 50)], [(100.5, 40)]
    assert ofi(b, a, [(100.0, 70)], [(100.5, 40)]) == 20  # bid size grew at the same price
    assert ofi(b, a, [(100.1, 30)], [(100.5, 40)]) == 30  # bid stepped up: its new size
    assert ofi(b, a, [(99.95, 80)], [(100.5, 40)]) == -50  # bid fell: its old size taken away
    assert ofi(b, a, [(100.0, 50)], [(100.5, 10)]) == 30  # ask size shrank at the same price
    assert ofi(b, a, [(100.0, 50)], [(100.4, 25)]) == -25  # ask stepped down: sellers leaning in
    assert ofi(b, a, [(100.0, 50)], [(100.55, 60)]) == 40  # ask lifted: its old size gone


def test_ofi_deep_adds_the_levels_below_one_by_one():
    from kotsin_nse.orderflow.meter import ofi

    prev_b, prev_a = [(100.0, 50), (99.95, 10)], [(100.5, 40), (100.55, 10)]
    new_b, new_a = [(100.0, 50), (99.95, 60)], [(100.5, 40), (100.55, 10)]
    assert ofi(prev_b, prev_a, new_b, new_a, 1) == 0 and ofi(prev_b, prev_a, new_b, new_a, 10) == 50


def test_the_book_reading_microprice_queue_and_distance_weighted_imbalance():
    from kotsin_nse.orderflow.meter import read_book

    r = read_book([(100.0, 300), (99.0, 10_000)], [(100.2, 100)])
    assert r is not None
    assert r.mid == pytest.approx(100.1) and r.spread_bps == pytest.approx(0.2 / 100.1 * 1e4)
    assert r.qi == pytest.approx(0.5)
    assert r.micro_bps == pytest.approx(((100.2 * 300 + 100.0 * 100) / 400 - 100.1) / 100.1 * 1e4)
    assert 0 < r.wimb < 0.6, "the 10,000 resting 110 bps away barely counts"
    assert (r.bid_depth, r.ask_depth, r.levels) == (10_300, 100, 1)
    assert read_book([(100.0, 10)], [(100.0, 10)]) is None and read_book([], [(100.0, 10)]) is None


def test_the_minute_keeps_the_book_readings_and_ofi_only_between_two_frames():
    m = FlowMeter()
    m.on_depth("X", T0, [(100.0, 50)], [(100.5, 40)])
    m.on_depth("X", T0 + 1, [(100.0, 70)], [(100.5, 40)])
    j = m.minutes("X")[-1].to_json()
    assert j["ofi"] == 20 and j["ofi_deep"] == 20 and j["book_n"] == 2 and j["levels"] == 1
    assert j["qi"] == pytest.approx(((50 - 40) / 90 + (70 - 40) / 110) / 2, abs=1e-4) and j["mid"] == 100.25
    assert "qi_sum" not in j and j["bid_depth"] == 60


def test_the_auction_and_a_crossed_book_give_volume_no_side():
    m = FlowMeter()
    # NSE pre-open, RELIANCE 2026-10-23: market orders shown at the bands, the quantity growing
    m.on_tick("X", T0, 1184.5, 1_000, 1302.9, 1066.1, auction=True)
    m.on_tick("X", T0 + 5, 1184.5, 10_000, 1302.9, 1066.1, auction=True)
    m.on_tick("X", T0 + 10, 1184.5, 12_000, 1302.9, 1066.1)  # a crossed book even after the flag
    fm = m.minutes("X")[-1]
    assert (fm.buy, fm.sell, fm.unknown, fm.cum_delta) == (0, 0, 11_000, 0)
    m.on_depth("X", T0 + 11, [(1302.9, 1479)], [(1066.1, 2824)])
    assert m.minutes("X")[-1].imbalance is None and m.minutes("X")[-1].book_n == 0
