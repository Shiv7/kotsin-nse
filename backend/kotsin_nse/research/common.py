"""What every research module shares: where the holdout begins, and how a trade's orders are charged."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from ..domain import Instrument, OrderSide
from ..risk.costs import CostModel

#: The holdout, 2025-10-01 → 2026-10-16, is untouched (docs/HANDOFF.md §4): every exploration run
#: ends on or before 2025-09-30, and nothing lifts the guard but an explicit ``allow_holdout``.
HOLDOUT_START = date(2025, 10, 1)
EXPLORATION_END = date(2025, 9, 30)


def assert_exploration_window(start: date, end: date, *, allow_holdout: bool = False) -> None:
    """Refuse a run whose range reaches into the holdout."""
    if end >= HOLDOUT_START and not allow_holdout:
        raise ValueError(f"{start} … {end} is inside the holdout (from {HOLDOUT_START}); the exploration window ends {EXPLORATION_END}")


def trade_costs(costs: CostModel, inst: Instrument, sign: int, entry: float, qty: float, exits: Iterable[Mapping[str, Any]]) -> float:
    """Charges on every order the trade placed: the entry and one order per exit — a half at T1 and
    the rest at T2 are two closing orders, each with its own brokerage, not one at the average
    price. ``sign`` +1 long / −1 short: a short sells to open and buys to close. Each exit is
    ``{"price", "frac"}``, the fraction of ``qty`` it closed."""
    open_side, close_side = (OrderSide.BUY, OrderSide.SELL) if sign > 0 else (OrderSide.SELL, OrderSide.BUY)
    total = costs.leg(inst, open_side, entry, qty).total  # type: ignore[arg-type]
    return total + sum(costs.leg(inst, close_side, float(e["price"]), qty * float(e["frac"])).total for e in exits)  # type: ignore[arg-type]


__all__ = ["EXPLORATION_END", "HOLDOUT_START", "assert_exploration_window", "trade_costs"]
