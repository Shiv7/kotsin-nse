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
    #: Mid, not last-traded. Every level the RT policy measures uses it, because a single print
    #: crossing a wide spread is not a move and LTP cannot tell the difference.
    option_mid: float | None = None
    #: Live spread as a fraction of mid. The give-back floor scales with it: the spread *is* the
    #: failure mode a fixed percentage walks into.
    spread_pct: float | None = None
    #: False when the quote is missing or stale. Distinct from "not breached": an absent quote is a
    #: third state, and treating it as recovery would reset a sustain clock that should pause.
    quote_ok: bool = True


class ExitEngine:
    """Ordered rules, evaluated in a written sequence.

    The order below is the contract, not an accident of line numbers. ``test_exits_precedence``
    asserts this tuple verbatim and asserts that when several rules are simultaneously true the
    earlier one wins — so a reordering is a failing test rather than a silent behaviour change.
    """

    #: Highest priority first. Each entry is a pure predicate returning a decision or ``None``.
    RULE_ORDER: tuple[str, ...] = (
        "hard_floor_below_stop",   # escape hatch: path-independent, beats every grace period
        "equity_confirmed_stop",   # the thesis is wrong; no grace
        "option_stop",             # option-side, subject to sustain when the policy asks for it
        "legacy_hard_floor",       # give-back of a peak, the pre-existing rule
        "targets",
        "peak_ratchet",
        "halt",
        "daily_loss",
        "force_flat",
        "time_stop",
    )

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def evaluate(self, pos: Position, view: MarketView) -> ExitDecision | None:
        if pos.status != "OPEN" or pos.qty_remaining <= 0:
            return None
        lim = self.limits
        ltp = view.option_ltp
        if ltp > 0:
            self._track(pos, ltp)

        mid = view.option_mid if view.option_mid and view.option_mid > 0 else ltp
        self._mark(pos, view, mid)

        # 0. hard floor below the stop — path-independent, so a feed gap cannot hide it -----------
        if lim.sustain_s is not None and view.quote_ok and mid > 0 and pos.option_sl > 0:
            floor = pos.option_sl * (1 - lim.hard_floor_below_stop_pct / 100)
            if mid <= floor:
                return ExitDecision(
                    pos.id,
                    ExitReason.SL_OP,
                    ltp,
                    pos.qty_remaining,
                    f"hard floor {floor:.2f} ({lim.hard_floor_below_stop_pct:.0f}% through the "
                    f"{pos.option_sl:.2f} stop) — a collapse is not a wick",
                )

        # 1. stops --------------------------------------------------------------------------------
        # An equity breach is checked FIRST when a sustain policy is active: if the underlying has
        # confirmed, the option-side breach is not noise and gets no grace.
        if lim.sustain_s is not None and self._equity_breached(pos, view):
            return ExitDecision(
                pos.id,
                ExitReason.SL_EQ,
                ltp,
                pos.qty_remaining,
                f"underlying confirmed the breach at {view.underlying_ltp:.2f} — no grace",
            )
        if lim.sustain_s is not None and mid > 0 and pos.option_sl > 0:
            # The sustained decision is the answer under this policy, so it returns here rather
            # than falling through to the plain-touch rule below — which would otherwise reach its
            # own return first and report the touch, not the sustain.
            if (held := self._sustained_option_stop(pos, view, mid)) is not None:
                return held
            if mid <= pos.option_sl:
                return None  # breached, not yet sustained, equity unconfirmed — hold
        if ltp > 0 and pos.option_sl > 0 and ltp <= pos.option_sl:
            if lim.sustain_s is not None:
                return None  # a sustain policy never exits on a bare touch
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
        if (rt := self._peak_ratchet(pos, view, mid)) is not None:
            return rt
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
        if lim.time_stop_bars is not None and view.bars_held >= lim.time_stop_bars:
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


    # -- FUDKII-RT policy helpers ------------------------------------------------------------------

    def _equity_breached(self, pos: Position, view: MarketView) -> bool:
        if view.underlying_ltp is None or pos.equity_sl <= 0:
            return False
        return (
            view.underlying_ltp <= pos.equity_sl
            if pos.direction is Direction.BULLISH
            else view.underlying_ltp >= pos.equity_sl
        )

    def _sustained_option_stop(
        self, pos: Position, view: MarketView, mid: float
    ) -> ExitDecision | None:
        """Exit only once the option has been through its stop *continuously* for ``sustain_s``.

        Three states, not two. Above the level clears the clock; below it starts or continues it;
        **no quote at all pauses it**. Treating an absent quote as recovery would reset a clock that
        should stand still, and a 90-second feed gap would hand back a grace period that was
        already spent — which is exactly what happened to this engine's own feed at 15:49 today.
        """
        lim = self.limits
        if not view.quote_ok:
            return None  # unknown: neither breach nor recovery, so the clock simply does not move
        if mid > pos.option_sl:
            pos.breach_since = None
            return None
        if pos.breach_since is None:
            pos.breach_since = view.now
            return None
        held_s = view.now - pos.breach_since
        if held_s < (lim.sustain_s or 0):
            return None
        return ExitDecision(
            pos.id,
            ExitReason.SL_OP,
            view.option_ltp,
            pos.qty_remaining,
            f"option mid held below {pos.option_sl:.2f} for {held_s:.0f}s "
            f"(≥ {lim.sustain_s:.0f}s) with the underlying unconfirmed",
        )

    def _mark(self, pos: Position, view: MarketView, mid: float) -> None:
        """Peak watermark on the mid, armed only after the entry noise has passed."""
        lim = self.limits
        if lim.peak_giveback_pct is None or not view.quote_ok or mid <= 0:
            return
        if view.now - pos.opened_ts < lim.peak_arm_after_s:
            return
        if mid > pos.peak_mid:
            pos.peak_mid = mid
            pos.trail_dwell = 0

    def _peak_ratchet(self, pos: Position, view: MarketView, mid: float) -> ExitDecision | None:
        """Give back at most ``peak_giveback_pct`` of the peak — floored by the live spread.

        Two percent of a 20.00 premium is 0.40. On a contract quoting 0.20 wide that is two ticks,
        and a single print would trip it, so the give-back is floored at a multiple of the spread:
        a wide contract must move further before it counts. The trigger then needs
        ``trail_dwell_samples`` consecutive reads below the level, because one bad print is exactly
        one read.
        """
        lim = self.limits
        if lim.peak_giveback_pct is None or not view.quote_ok or pos.peak_mid <= 0 or mid <= 0:
            return None
        if pos.targets_hit < 1:
            return None  # the ratchet arms only once T1 has paid
        give = lim.peak_giveback_pct / 100
        if view.spread_pct:
            give = max(give, view.spread_pct * lim.peak_giveback_spread_mult)
        level = pos.peak_mid * (1 - give)
        if mid > level:
            pos.trail_dwell = 0
            return None
        pos.trail_dwell += 1
        if pos.trail_dwell < lim.trail_dwell_samples:
            return None
        return ExitDecision(
            pos.id,
            ExitReason.TRAIL,
            view.option_ltp,
            pos.qty_remaining,
            f"gave back {give * 100:.1f}% of a {pos.peak_mid:.2f} peak "
            f"({pos.trail_dwell} consecutive reads below {level:.2f})",
        )



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
