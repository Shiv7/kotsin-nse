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
