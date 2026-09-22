"""How many lots.

Sized from the **stop**, not from a flat rupee figure, so size and risk are consistent by
construction — the only book in the old stack where that was true was QUANT, and it was the only
one whose stop and sizing could not disagree. CAN2 sized at a flat ₹33,000 and never read its own
wallet, so a position did not shrink in a drawdown.

Then clamped by:

* the per-position budget (``min(pct of wallet, absolute ceiling)``);
* lot granularity, via the instrument's own ``lot_size`` and ``multiplier`` — never guessed;
* **the cost floor**: if the round-trip charge on the sized position exceeds a fraction of the
  expected move to T1, the trade is declined. This is the guard the old book did not have and it is
  the one its own economics asked for.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import Instrument, InstrumentKind
from .costs import CostModel
from .limits import RiskLimits


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: int
    lots: int
    outlay: float  # premium actually paid
    risk_inr: float  # loss if the option stop is hit
    cost_pct_of_target: float | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.qty > 0


def size_position(
    *,
    instrument: Instrument,
    premium: float,
    option_stop: float,
    option_target1: float | None,
    balance: float,
    available: float,
    limits: RiskLimits,
    costs: CostModel,
    max_cost_share_of_target: float = 0.35,
) -> SizingResult:
    per_unit_risk = max(0.0, premium - option_stop)
    if premium <= 0 or balance <= 0 or per_unit_risk <= 0:
        return SizingResult(0, 0, 0.0, 0.0, None, "invalid inputs (premium/stop)")

    step = instrument.qty_step
    unit_cost = premium * step * instrument.multiplier
    if unit_cost <= 0:
        return SizingResult(0, 0, 0.0, 0.0, None, "unknown contract size — declined, not guessed")

    risk_budget = balance * limits.risk_per_trade_pct / 100
    by_risk = int(risk_budget // (per_unit_risk * step * instrument.multiplier))
    budget = min(limits.position_budget(balance), available)
    by_budget = int(budget // unit_cost)
    lots = max(0, min(by_risk, by_budget))
    # The lot ceiling binds alongside the rupee one — whichever is lower wins, and which one
    # bound is worth knowing: cut to one lot by a rich premium is a liquidity fact, cut to the
    # ceiling is a policy one.
    capped_by_lots = limits.max_lots is not None and lots > limits.max_lots
    if capped_by_lots:
        lots = limits.max_lots
    if lots < 1:
        why = (
            "risk budget below one lot"
            if by_risk < 1
            else f"budget ₹{budget:,.0f} below one lot at ₹{unit_cost:,.0f}"
        )
        return SizingResult(0, 0, 0.0, 0.0, None, why)

    qty = lots * step
    outlay = premium * qty * instrument.multiplier
    risk_inr = per_unit_risk * qty * instrument.multiplier

    cost_share: float | None = None
    if option_target1 and option_target1 > premium:
        charges = costs.round_trip(instrument, premium, option_target1, qty).total
        gain_to_t1 = (option_target1 - premium) * qty * instrument.multiplier
        cost_share = charges / gain_to_t1 if gain_to_t1 > 0 else None
        if cost_share is not None and cost_share > max_cost_share_of_target:
            return SizingResult(
                0,
                0,
                outlay,
                risk_inr,
                cost_share,
                f"costs are {cost_share:.0%} of the move to T1 (cap {max_cost_share_of_target:.0%})",
            )

    return SizingResult(
        qty,
        lots,
        outlay,
        risk_inr,
        cost_share,
        f"ok (lot cap {limits.max_lots})" if capped_by_lots else "ok",
    )


def lots_of(instrument: Instrument, qty: int) -> int:
    if instrument.kind is InstrumentKind.EQUITY:
        return qty
    return qty // max(1, instrument.lot_size)
