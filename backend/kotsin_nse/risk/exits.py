"""The ONLY module that decides an exit.

Every exit rule in the system is here: the option stop, the underlying stop, the target ladder, the
T1 staircase, the peak trail, the hard floor, the time stop and the session force-flat. Nothing
else may move a stop or close a position.

That constraint is the whole point. In the old stack a HotStocks position had its stop written by
the dashboard at entry, re-written daily by a recompute job, and independently trailed by two rules
in the executor — one of which set a stop *above* the live price and stopped the position out
instantly. The fix was three flags turning the executor's trails off plus a hygiene guard, which
works but leaves four places that could still do it. Here there is one.

Ordering matters and is deliberate:

1. **Stops first.** A genuine stop always wins over a force-flat on the same bar.
2. **Targets next**, partial by the ladder.
3. **Trail** only after the arm threshold, and it may only tighten.
4. **Time stop and EOD** last, as backstops.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import Direction, ExitDecision, ExitReason, Position
from .limits import RiskLimits


@dataclass(frozen=True, slots=True)
class MarketView:
    """Everything the exit engine may look at, gathered by the caller."""

    option_ltp: float
    underlying_ltp: float | None
    now: float
    bars_held: int
    past_force_flat: bool
    halted: bool = False
    daily_loss_hit: bool = False


class ExitEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def evaluate(self, pos: Position, view: MarketView) -> ExitDecision | None:
        if pos.status != "OPEN" or pos.qty_remaining <= 0:
            return None
        lim = self.limits
        ltp = view.option_ltp
        if ltp > 0:
            self._track(pos, ltp)

        # 1. stops --------------------------------------------------------------------------------
        if ltp > 0 and pos.option_sl > 0 and ltp <= pos.option_sl:
            return ExitDecision(
                pos.id,
                ExitReason.SL_OP,
                ltp,
                pos.qty_remaining,
                f"option {ltp:.2f} ≤ stop {pos.option_sl:.2f} (peak {pos.peak_r:.2f}R)",
            )
        if view.underlying_ltp is not None and pos.equity_sl > 0:
            breached = (
                view.underlying_ltp <= pos.equity_sl
                if pos.direction is Direction.BULLISH
                else view.underlying_ltp >= pos.equity_sl
            )
            if breached:
                return ExitDecision(
                    pos.id,
                    ExitReason.SL_EQ,
                    ltp,
                    pos.qty_remaining,
                    f"underlying {view.underlying_ltp:.2f} breached {pos.equity_sl:.2f}",
                )

        # 2. hard floor ---------------------------------------------------------------------------
        if pos.peak_r > 0 and ltp > 0:
            peak_price = pos.entry + pos.peak_r * pos.r_unit
            floor_price = pos.entry + (peak_price - pos.entry) * (1 - lim.hard_floor_pct / 100)
            if peak_price > pos.entry and ltp <= floor_price <= peak_price and pos.peak_r >= 1.0:
                return ExitDecision(
                    pos.id,
                    ExitReason.TRAIL,
                    ltp,
                    pos.qty_remaining,
                    f"hard floor: gave back {lim.hard_floor_pct:.0f}% of a {pos.peak_r:.2f}R peak",
                )

        # 3. targets ------------------------------------------------------------------------------
        if ltp > 0 and pos.targets_hit < len(pos.option_targets):
            nxt = pos.option_targets[pos.targets_hit]
            if nxt > 0 and ltp >= nxt:
                share = (
                    lim.target_ladder[pos.targets_hit]
                    if pos.targets_hit < len(lim.target_ladder)
                    else 0.0
                )
                qty = self._ladder_qty(pos, share)
                if qty > 0:
                    return ExitDecision(
                        pos.id,
                        ExitReason.TARGET,
                        ltp,
                        qty,
                        f"T{pos.targets_hit + 1} {nxt:.2f} hit — taking {share:.0%}",
                    )

        # 4. trail ---------------------------------------------------------------------------------
        self._trail(pos, ltp)

        # 5. backstops ------------------------------------------------------------------------------
        if view.halted:
            return ExitDecision(pos.id, ExitReason.HALT, ltp, pos.qty_remaining, "halted")
        if view.daily_loss_hit:
            return ExitDecision(
                pos.id, ExitReason.DAILY_LOSS, ltp, pos.qty_remaining, "daily loss limit"
            )
        if view.past_force_flat:
            return ExitDecision(
                pos.id, ExitReason.EOD, ltp, pos.qty_remaining, "segment force-flat"
            )
        if view.bars_held >= lim.time_stop_bars:
            return ExitDecision(
                pos.id,
                ExitReason.TIME_STOP,
                ltp,
                pos.qty_remaining,
                f"held {view.bars_held} bars ≥ {lim.time_stop_bars}",
            )
        return None

    # -- state -----------------------------------------------------------------------------------

    def _track(self, pos: Position, ltp: float) -> None:
        r = pos.r_now(ltp)
        pos.mfe_r = max(pos.mfe_r, r)
        pos.mae_r = min(pos.mae_r, r)
        pos.peak_r = max(pos.peak_r, r)

    def _ladder_qty(self, pos: Position, share: float) -> int:
        if share <= 0:
            return 0
        step = pos.instrument.qty_step
        want = int(pos.qty * share)
        want = (want // step) * step if step > 1 else want
        # The last rung takes whatever is left, so rounding never strands an unsellable remainder.
        if pos.targets_hit == len(pos.option_targets) - 1 or want <= 0:
            return pos.qty_remaining
        return min(want, pos.qty_remaining)

    def _trail(self, pos: Position, ltp: float) -> None:
        """Move the stop up. Never down — enforced by the comparison, not by convention."""
        lim = self.limits
        if ltp <= 0 or pos.r_unit <= 0:
            return
        new_stop = pos.option_sl

        if lim.breakeven_after_t1 and pos.targets_hit >= 1:
            new_stop = max(new_stop, pos.entry)

        gain_pct = (ltp - pos.entry) / pos.entry * 100 if pos.entry > 0 else 0.0
        if gain_pct >= lim.trail_arm_pct:
            peak_price = pos.entry + pos.peak_r * pos.r_unit
            giveback = (peak_price - pos.entry) * lim.trail_giveback_pct / 100
            new_stop = max(new_stop, peak_price - giveback)

        if new_stop > pos.option_sl:
            pos.option_sl = round(new_stop, 2)


def apply_exit(
    pos: Position, decision: ExitDecision, *, fill_price: float, charges: float, now: float
) -> float:
    """Book a (possibly partial) exit and return the **gross** P&L of the slice.

    Charges are accumulated on the position but not netted here, so the ledger reports the two
    separately — which is the only way a finding like "81% of round-trip cost is flat brokerage"
    is ever visible in the numbers rather than in someone's memory.
    """
    qty = min(decision.qty, pos.qty_remaining)
    gross = (fill_price - pos.entry) * pos.dir_sign * qty * pos.instrument.multiplier
    pos.qty_remaining -= qty
    pos.charges += charges
    if decision.reason is ExitReason.TARGET:
        pos.targets_hit += 1
    if pos.qty_remaining <= 0:
        pos.status = "CLOSED"
        pos.closed_ts = now
        pos.exit_price = fill_price
        pos.exit_reason = decision.reason.value
    return gross
