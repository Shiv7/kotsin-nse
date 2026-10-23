"""Microstructure from snapshots: what the book and the flow say, each on a hand-built case."""

from __future__ import annotations

import pytest

from kotsin_nse.orderflow.micro import ABSORB_S, MicroMeter

T0 = 1_792_729_200.0  # 10:00 IST


def _book(bid: float, ask: float, bq: list[int], aq: list[int], step: float = 0.1):
    return [(round(bid - i * step, 2), q) for i, q in enumerate(bq)], [(round(ask + i * step, 2), q) for i, q in enumerate(aq)]


def test_microprice_queue_imbalance_spread_and_skew():
    m = MicroMeter()
    b, a = _book(100.0, 100.2, [300, 100], [100, 100])
    m.on_depth("X", T0, b, a)
    s = m.snapshot("X")
    assert s is not None
    assert s["microprice"] == pytest.approx((100.2 * 300 + 100.0 * 100) / 400)
    assert s["queue_imbalance"] == pytest.approx(0.5) and s["book_skew"] == pytest.approx(200 / 600, abs=1e-4)
    assert s["spread_bps"] == pytest.approx(0.2 / 100.1 * 1e4, rel=1e-3)


def test_kyle_lambda_is_the_price_impact_of_signed_flow():
    """Each frame buys or sells 10,000 at the touch and the mid moves 1 bp the same way."""
    m = MicroMeter()
    mid, tot, last_side = 100.0, 1000, 1
    for i in range(80):
        side = 1 if (i % 3) else -1
        tot += 10_000
        ltp = mid + 0.05 if side > 0 else mid - 0.05  # at the ask / at the bid of the previous quote
        mid *= 1 + side * 1e-4
        m.on_tick("X", T0 + i, ltp, tot, mid - 0.05, mid + 0.05)
        last_side = side
    lam = m.snapshot("X")["kyle_lambda_bps_per_10k"]
    assert lam == pytest.approx(1.0, rel=0.25) and last_side in (1, -1)


def test_vpin_is_high_for_one_sided_flow_and_low_for_balanced():
    one, two = MicroMeter(), MicroMeter()
    tot = 1000
    for i in range(240):
        tot += 500
        one.on_tick("X", T0 + i * 2, 100.2, tot, 100.0, 100.2)  # every print at the ask
        two.on_tick("X", T0 + i * 2, 100.2 if i % 2 else 100.0, tot, 100.0, 100.2)
    assert one.snapshot("X")["vpin"] > 0.9 and two.snapshot("X")["vpin"] < 0.2


def test_a_wall_that_vanishes_unreached_is_pulled_and_one_traded_through_is_filled():
    m = MicroMeter()
    big = [100, 100, 100, 2000, 100]
    b, a = _book(100.0, 100.2, [100] * 5, big)  # ask wall at 100.5
    m.on_tick("X", T0, 100.1, 1000, 100.0, 100.2)
    for i in range(10):
        m.on_depth("X", T0 + i, b, a)
    b2, a2 = _book(100.0, 100.2, [100] * 5, [100] * 5)
    m.on_depth("X", T0 + 11, b2, a2)  # gone, price never reached 100.5
    s = m.snapshot("X")
    assert s["walls_pulled"] == 1 and s["walls_filled"] == 0 and s["events"][-1]["kind"] == "WALL_PULLED"
    for i in range(10):
        m.on_depth("X", T0 + 20 + i, b, a)
    m.on_tick("X", T0 + 31, 100.5, 3000, 100.4, 100.5)  # trades up to the wall
    m.on_depth("X", T0 + 32, b2, a2)
    assert m.snapshot("X")["walls_filled"] == 1


def test_absorption_selling_into_an_unmoving_bid_well_beyond_its_size():
    m = MicroMeter()
    b, a = _book(100.0, 100.1, [500, 100], [100, 100])
    m.on_depth("X", T0, b, a)
    tot = 1000
    m.on_tick("X", T0, 100.05, tot, 100.0, 100.1)
    for i in range(1, int(ABSORB_S) + 3):
        tot += 120
        m.on_tick("X", T0 + i, 100.0, tot, 100.0, 100.1)  # every print at the bid
        m.on_depth("X", T0 + i, b, a)  # the bid stays at 100.0 with 500 shown
    kinds = [e["kind"] for e in m.snapshot("X")["events"]]
    assert "ABSORPTION" in kinds


def test_a_liquidity_vacuum_when_one_side_empties_in_seconds():
    m = MicroMeter()
    b, a = _book(100.0, 100.1, [1000] * 5, [1000] * 5)
    m.on_depth("X", T0, b, a)
    thin, _ = _book(100.0, 100.1, [100] * 5, [1000] * 5)
    m.on_depth("X", T0 + 2, thin, a)
    assert [e["kind"] for e in m.snapshot("X")["events"]] == ["VACUUM"]


def test_efficiency_ratio_trend_vs_chop_and_roll_spread_from_the_bounce():
    trend, chop = MicroMeter(), MicroMeter()
    tot = 1000
    for i in range(600):
        tot += 10
        p = 100 + i * 0.01
        trend.on_tick("X", T0 + i, p, tot, p - 0.05, p + 0.05)
        m0 = 100.0 + (0.2 if (i // 60) % 2 else 0)  # the mid steps up and back each minute: chop
        q = m0 + (0.05 if i % 2 else -0.05)  # prints bounce between bid and ask
        chop.on_tick("X", T0 + i, q, tot, m0 - 0.05, m0 + 0.05)
    assert trend.snapshot("X")["efficiency_ratio"] > 0.9
    assert chop.snapshot("X")["efficiency_ratio"] < 0.5
    roll = chop.snapshot("X")["roll_spread"]
    assert roll is not None and 0.05 <= roll <= 0.25, "the bid-ask bounce shows as a spread of the order of 0.1"
