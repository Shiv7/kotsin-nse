"""Costs.

The cost tests reproduce the measured NSE cash economics: at ₹33,000 a position the round trip was
~0.30% and flat brokerage was ~81% of it. If those numbers stop holding, either the model or the
finding is wrong, and we want to know which.
"""

from __future__ import annotations

from kotsin_nse.config import Settings
from kotsin_nse.domain import (
    OrderSide,
)
from kotsin_nse.risk.costs import CostModel


def _settings() -> Settings:
    return Settings(_env_file=None)


# -- costs ------------------------------------------------------------------------------------------


def test_round_trip_on_nse_cash_at_33k_matches_the_measured_030_percent(equity):
    """The number that decided this book's fate. 0.299% measured; the model must land on it."""
    costs = CostModel(_settings())
    price, qty = 330.0, 100  # ₹33,000
    pct = costs.round_trip_pct(equity, price, qty)
    assert 0.28 <= pct <= 0.35, pct


def test_a_percentage_slab_does_not_silently_replace_the_flat_charge(equity):
    """With the slab off (the default) the flat ₹40 is what is charged. Turning it on must only
    ever reduce the charge, never be the reason a small position looks cheap."""
    flat = CostModel(Settings(_env_file=None))
    slab = CostModel(Settings(_env_file=None, cost_brokerage_pct=0.03))
    assert flat.leg(equity, OrderSide.BUY, 330.0, 100).brokerage == 40.0
    assert slab.leg(equity, OrderSide.BUY, 330.0, 100).brokerage < 40.0


def test_flat_brokerage_dominates_the_round_trip_at_small_size(equity):
    """81% of the round trip was ₹40/order × 2. That is why entries, not exits, were the binding
    problem: a fixed cost does not scale down."""
    costs = CostModel(_settings())
    ch = costs.round_trip(equity, 330.0, 330.0, 100)
    assert ch.brokerage == 80.0  # ₹40 × 2 legs
    assert ch.brokerage / ch.total > 0.70


def test_cost_share_collapses_at_larger_size(equity):
    """Break-even needed ~₹1.3 lakh per position. The model must show that."""
    costs = CostModel(_settings())
    small = costs.round_trip_pct(equity, 330.0, 100)  # ₹33k
    large = costs.round_trip_pct(equity, 330.0, 400)  # ₹132k
    assert large < small / 2


def test_option_charges_are_levied_on_premium_turnover_not_notional(option):
    costs = CostModel(_settings())
    assert costs.turnover(option, 50.0, 250) == 50.0 * 250
    ch = costs.leg(option, OrderSide.SELL, 50.0, 250)
    assert ch.stt > 0  # STT on the sell leg only
    assert costs.leg(option, OrderSide.BUY, 50.0, 250).stt == 0


def test_mcx_multiplier_is_applied_to_turnover(mcx_future):
    """ALUMINI is quoted per kg on a 1,000 kg contract. A 286-qty entry logged ₹99,943 when the
    real notional was ₹99.9 million."""
    costs = CostModel(_settings())
    assert costs.turnover(mcx_future, 349.45, 286) == 349.45 * 286 * 1000
