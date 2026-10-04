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

import math
from dataclasses import dataclass, replace

from ..domain import Direction, ExitDecision, ExitReason, OptionType, Position
from ..instrument.pricing import value_at
from ..instrument.select import estimate_delta
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
    #: the best bid — what the position could be SOLD for now; MFE / MAE are marked on it. None: no bid.
    option_bid: float | None = None


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
        #: per position: (minute bucket, last option print in it) — the option's own 1m close
        self._minute: dict[str, tuple[int, float]] = {}

    def evaluate(self, pos: Position, view: MarketView) -> ExitDecision | None:
        if pos.status != "OPEN" or pos.qty_remaining <= 0:
            return None
        lim = self.limits
        ltp = view.option_ltp
        if ltp > 0:
            self._track(pos, ltp, view.option_bid if view.quote_ok else None)

        mid = view.option_mid if view.option_mid and view.option_mid > 0 else ltp
        self._reproject_stop(pos, view)
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
                    level=round(floor, 2), trigger_price=mid, trigger_on="option mid",
                )

        # 1. stops --------------------------------------------------------------------------------
        # An equity breach is checked FIRST when a sustain policy is active: if the underlying has
        # confirmed, the option-side breach is not noise and gets no grace.
        if lim.own_ladder and (hard := self._own_hard_stop(pos, view, mid)) is not None:
            return hard
        if lim.sustain_s is not None and self._equity_breached(pos, view):
            return ExitDecision(
                pos.id,
                ExitReason.SL_EQ,
                ltp,
                pos.qty_remaining,
                f"underlying confirmed the breach at {view.underlying_ltp:.2f} — no grace",
                level=pos.equity_sl, trigger_price=view.underlying_ltp or 0.0, trigger_on="underlying",
            )
        if lim.sustain_s is not None and mid > 0 and pos.option_sl > 0:
            # The sustained decision is the answer under this policy, so it returns here rather
            # than falling through to the plain-touch rule below — which would otherwise reach its
            # own return first and report the touch, not the sustain.
            if (held := self._sustained_option_stop(pos, view, mid)) is not None:
                return held
            if mid <= pos.option_sl:
                # breached, not yet sustained, equity unconfirmed — hold the STOP, but never the
                # backstops: returning None here used to skip the halt, the daily-loss exit and
                # the 15:20 force-flat for as long as the option sat under its stop.
                return self._backstops(pos, view, ltp)
        if ltp > 0 and pos.option_sl > 0 and ltp <= pos.option_sl:
            if lim.sustain_s is not None:
                return self._backstops(pos, view, ltp)  # no exit on a bare touch; backstops still apply
            return ExitDecision(
                pos.id,
                ExitReason.SL_OP,
                ltp,
                pos.qty_remaining,
                f"option {ltp:.2f} ≤ stop {pos.option_sl:.2f} (peak {pos.peak_r:.2f}R)",
                level=pos.option_sl, trigger_price=ltp, trigger_on="option last",
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
                    level=pos.equity_sl, trigger_price=view.underlying_ltp, trigger_on="underlying",
                )

        # 2. hard floor ---------------------------------------------------------------------------
        # The base book's 50 %-of-peak give-back. Not for the own-ladder books: their exits are the
        # rung SL, the band and the force-flat, by design (docs/PIVOTS.md §6) — this rule would
        # have overridden RT-Y's wide band on the first pullback.
        if not lim.own_ladder and pos.peak_r > 0 and ltp > 0:
            peak_price = pos.entry + pos.peak_r * pos.r_unit
            floor_price = pos.entry + (peak_price - pos.entry) * (1 - lim.hard_floor_pct / 100)
            if peak_price > pos.entry and ltp <= floor_price <= peak_price and pos.peak_r >= 1.0:
                return ExitDecision(
                    pos.id,
                    ExitReason.TRAIL,
                    ltp,
                    pos.qty_remaining,
                    f"hard floor: gave back {lim.hard_floor_pct:.0f}% of a {pos.peak_r:.2f}R peak",
                    level=round(floor_price, 2), trigger_price=ltp, trigger_on="option last",
                )

        # 3. targets ------------------------------------------------------------------------------
        if lim.own_ladder:
            if (own := self._own_ladder(pos, view, ltp, mid)) is not None:
                return own
        elif ltp > 0 and pos.targets_hit < len(pos.option_targets):
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
        if not lim.own_ladder:
            self._trail(pos, ltp)  # the RT policy's ratchet supersedes the legacy 3%/40% trail

        # 5. backstops ------------------------------------------------------------------------------
        return self._backstops(pos, view, ltp)

    def _backstops(self, pos: Position, view: MarketView, ltp: float) -> ExitDecision | None:
        """Halt, daily loss, force-flat, time stop — the exits no stop policy may postpone."""
        lim = self.limits
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

    def evaluate_stale(self, pos: Position, view: MarketView) -> ExitDecision | None:
        """The option's quote is too old to judge anything priced off the option. The underlying's
        stop and the backstops do not need it, so they are still enforced — skipping the position
        outright left TATASTEEL's confirmed equity stop unevaluated (2026-09-25 14:30 and 14:39)."""
        if pos.status != "OPEN" or pos.qty_remaining <= 0:
            return None
        ltp = view.option_ltp
        if self._equity_breached(pos, view):
            return ExitDecision(
                pos.id, ExitReason.SL_EQ, ltp, pos.qty_remaining,
                f"underlying {view.underlying_ltp:.2f} breached {pos.equity_sl:.2f} (option quote stale)",
                level=pos.equity_sl, trigger_price=view.underlying_ltp or 0.0, trigger_on="underlying",
            )
        return self._backstops(pos, view, ltp)

    # -- state -----------------------------------------------------------------------------------

    def _track(self, pos: Position, ltp: float, bid: float | None = None) -> None:
        """MFE / MAE on what the position could be SOLD for: the bid when there is one, else the last trade.
        A stop sells into the bid, so marking the last trade left 47 of 120 exits below their own MAE
        (operator, 2026-10-04; median 2.75 % of the premium). ``peak_r`` stays on the last trade: the
        legacy hard floor and the trail read it, and an exit rule may not move with a reporting fix."""
        mark = bid if bid and bid > 0 else ltp
        r = pos.r_now(mark)
        pos.mfe_r = max(pos.mfe_r, r)
        pos.mae_r = min(pos.mae_r, r)
        pos.mark_basis = "bid"
        pos.peak_r = max(pos.peak_r, pos.r_now(ltp))

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
            level=pos.option_sl, trigger_price=mid, trigger_on="option mid",
        )

    def _reproject_stop(self, pos: Position, view: MarketView) -> None:
        """The option-side stop is the equity stop expressed through delta — and delta moves.

        Re-derived every ``reproject_stop_s`` from the underlying's live price, so the level is
        where the equity stop *is* on the premium now, not where it was at entry. Never below
        ``ratchet_sl``: once the ratchet has claimed ground, a falling delta may not give it back.
        """
        lim = self.limits
        if lim.reproject_stop_s is None or view.underlying_ltp is None or pos.equity_sl <= 0:
            return
        if view.now - pos.last_reproject_ts < lim.reproject_stop_s:
            return
        pos.last_reproject_ts = view.now
        delta = abs(
            estimate_delta(
                spot=view.underlying_ltp,
                strike=pos.instrument.strike,
                option_type=pos.instrument.option_type,
            )
        )
        projected = max(0.05, pos.entry - abs(pos.equity_entry - pos.equity_sl) * delta)
        if lim.priced_option_stop and view.option_mid and view.option_mid > 0 and pos.instrument.expiry:
            priced = value_at(
                option_price=view.option_mid, spot=view.underlying_ltp, target_spot=pos.equity_sl, strike=pos.instrument.strike,
                expiry=pos.instrument.expiry, now=view.now, call=pos.instrument.option_type is OptionType.CE,
            )
            if priced is not None:
                projected = max(0.05, priced)
        if lim.max_premium_loss_pct is not None:
            projected = max(projected, pos.entry * (1 - lim.max_premium_loss_pct / 100))
        if lim.min_stop_ticks:
            # never nearer than the floor: on a cheap contract the projection is a tick or two
            tick = pos.instrument.tick_size or 0.05
            projected = min(projected, max(tick, pos.entry - lim.min_stop_ticks * tick))
        pos.option_sl = round(max(projected, pos.ratchet_sl), 2)

    def _tranche(self, pos: Position) -> int:
        lot = max(1, pos.instrument.lot_size) * max(1, self.limits.arm_tranche_lots)
        return pos.qty_remaining if pos.qty_remaining <= lot else min(lot, pos.qty_remaining)

    def stop_line(self, pos: Position, view: MarketView) -> float:
        """The highest level a stop or the give-back line takes this position out at now (0 = none) —
        what the engine's 15:15 plan measures the distance to."""
        lim = self.limits
        line = max(pos.option_sl, pos.ratchet_sl)
        if lim.own_ladder:
            return round(max(line, self._band_level(pos, view)), 2)
        if lim.peak_giveback_pct is not None and pos.peak_mid > 0 and pos.targets_hit >= 1:
            give = lim.peak_giveback_pct / 100
            if view.spread_pct:
                give = max(give, view.spread_pct * lim.peak_giveback_spread_mult)
            line = max(line, pos.peak_mid * (1 - give))
        return round(line, 2)

    def _band_level(self, pos: Position, view: MarketView) -> float:
        """The peak give-back line, once armed: max(peak_giveback_pct, giveback_move_frac × the
        option's expected daily move), floored at a multiple of the live spread."""
        lim = self.limits
        if pos.armed_ts is None or pos.peak_mid <= 0 or lim.peak_giveback_pct is None:
            return 0.0
        give = lim.peak_giveback_pct / 100
        if lim.giveback_move_frac and pos.option_edm > 0:
            give = max(give, lim.giveback_move_frac * pos.option_edm)
        if view.spread_pct:
            give = max(give, view.spread_pct * lim.peak_giveback_spread_mult)
        return round(pos.peak_mid * (1 - give), 2)

    def _own_hard_stop(self, pos: Position, view: MarketView, mid: float) -> ExitDecision | None:
        """The rising line: the stepped rung SL or the peak give-back, whichever is higher. How a
        breach ends the trade is the book's choice — RT-X: one read through it; RT-N: the band
        needs ``trail_dwell_samples`` consecutive reads; RT-Y: rung SL and band alike need
        ``sustain_s`` of continuous breach, because KEI's breakeven line fired on a one-minute
        wick at 10:31 with the underlying up, two hours before a +143 % move."""
        lim = self.limits
        if not view.quote_ok or mid <= 0:
            return None
        band = self._band_level(pos, view)
        line = max(pos.ratchet_sl, band)
        if line <= 0 or mid > line:
            pos.line_breach_since = None
            pos.trail_dwell = 0
            return None
        by_band = band > pos.ratchet_sl
        mode = lim.band_exit if by_band else ("sustain" if lim.post_arm_sustain else "through")
        if mode == "dwell":
            pos.trail_dwell += 1
            if pos.trail_dwell < lim.trail_dwell_samples:
                return None
        elif mode == "sustain":
            if pos.line_breach_since is None:
                pos.line_breach_since = view.now
                return None
            if view.now - pos.line_breach_since < (lim.sustain_s or 0):
                return None
        what = (
            f"give-back line {band:.2f} off the {pos.peak_mid:.2f} peak"
            if by_band
            else (f"rung SL {pos.ratchet_sl:.2f}" if pos.ratchet_sl > pos.entry else f"breakeven {pos.ratchet_sl:.2f}")
        )
        return ExitDecision(
            pos.id, ExitReason.TRAIL if by_band else ExitReason.SL_OP, view.option_ltp, pos.qty_remaining,
            f"hard SL {line:.2f}: {what} traded through at {mid:.2f} [{mode}]; {pos.targets_hit} lot(s) already out",
            level=round(line, 2), trigger_price=mid, trigger_on="option mid",
        )

    def _last_rung(self, pos: Position, targets: tuple[float, ...]) -> bool:
        """Does a touch of the next rung take the rest of the position (not one lot)?"""
        i = pos.targets_hit
        lim = self.limits
        if lim.trail_all_after_t1:
            return False  # T1 sells one lot; every lot after it leaves on the give-back line or a stop
        last = i >= len(targets) - 1
        if last and i == 0 and len(targets) == 1 and lim.arm_at_pct is not None and lim.peak_giveback_pct is not None:
            # arming synthesised T1 from the live price and there is no rung above it: pay the arm
            # tranche and let the give-back band carry the remainder, rather than flattening here.
            last = False
        return last

    def _touch(self, pos: Position, view: MarketView, mid: float, by: str, why: str) -> ExitDecision:
        return self._touch_at(pos, view.now, view.option_ltp, mid, by, why)

    def _touch_at(self, pos: Position, now: float, ltp: float, mid: float, by: str, why: str) -> ExitDecision:
        """A rung touched: one lot out (the last rung takes the rest), the hard SL steps to the
        rung below (breakeven for T1), the sustain clock for this rung starts. Under the
        immediate policy the first touch is the arming itself."""
        i = pos.targets_hit
        last = self._last_rung(pos, pos.option_targets)
        floor = pos.entry if i == 0 else pos.option_targets[i - 1]
        pos.ratchet_sl = round(max(pos.ratchet_sl, floor), 2)
        pos.option_sl = round(max(pos.option_sl, pos.ratchet_sl), 2)
        if i == 0:
            pos.armed_by = by
            # arm_at_pct books arm the give-back band at the touch itself: "after 5 % is achieved,
            # we arm it and trail". Waiting 75 s for a sustain meant BANKNIFTY's PE on 2026-09-24,
            # nine seconds over +5 % before it broke, never armed and gave back to breakeven
            # (tape replay: 3 lots at 201.00, -128) instead of leaving on the 3 % line (208.50, +547).
            if self.limits.arm_mode == "immediate" or self.limits.arm_at_pct is not None or self.limits.trail_all_after_t1:
                pos.armed_ts = now
                pos.peak_mid = max(pos.peak_mid, mid)
                pos.trail_dwell = 0
        pos.t_touch_ts = now if mid >= pos.option_targets[i] else None
        pos.t_close_ok = False
        qty = pos.qty_remaining if last else self._tranche(pos)
        return ExitDecision(
            pos.id, ExitReason.TARGET, ltp, qty,
            f"T{i + 1} {pos.option_targets[i]:.2f} touched by {by} ({why}) — {'the rest' if last else 'one lot'} out; "
            f"hard SL {pos.ratchet_sl:.2f}",
        )

    def _sustain(self, pos: Position, view: MarketView, mid: float, minute_close: float | None) -> None:
        """Sustained = continuously at or above the rung for ``sustain_s`` AND a 1-minute close at
        or above it since the touch. Completing it arms T1's band; it steps the hard SL to the rung
        unless the book trails one rung behind (``sl_lag``)."""
        lim = self.limits
        k = pos.targets_hit - 1
        if k < 0 or k <= pos.sustained_idx or k >= len(pos.option_targets) or not view.quote_ok:
            return
        rung = pos.option_targets[k]
        if mid < rung:
            pos.t_touch_ts = None
            pos.t_close_ok = False
            return
        if pos.t_touch_ts is None:
            pos.t_touch_ts = view.now
        if minute_close is not None and minute_close >= rung:
            pos.t_close_ok = True
        held = view.now - pos.t_touch_ts
        if held >= (lim.sustain_s or 0) and pos.t_close_ok:
            pos.sustained_idx = k
            if not lim.sl_lag:
                pos.ratchet_sl = round(max(pos.ratchet_sl, rung), 2)
                pos.option_sl = round(max(pos.option_sl, pos.ratchet_sl), 2)
            if k == 0 and pos.armed_ts is None:
                pos.armed_ts = view.now
                pos.peak_mid = max(pos.peak_mid, mid)
                pos.trail_dwell = 0

    def _arm_threshold(self, pos: Position) -> tuple[float, float]:
        """``(threshold, pct_arm)``: the least the option must reach before T1 may arm, and the
        ``arm_at_pct`` minimum itself (0 when the book has none)."""
        lim = self.limits
        if lim.arm_at_pct is not None:
            # Operator's rule, 2026-09-25: +arm_at_pct on the premium paid is the MINIMUM before any
            # arm. An own T1 or the equity T1 nearer than that waits for it — a rung 1 % over entry
            # arming the trade and stepping the SL to breakeven is how KEI and GRASIM were stopped.
            pct_arm = round(pos.entry * (1 + lim.arm_at_pct / 100), 2)
            return pct_arm, pct_arm
        threshold = pos.entry * (1 + lim.arm_min_move * pos.option_edm) if (lim.arm_min_move and pos.option_edm) else 0.0
        return threshold, 0.0

    # -- target sells placed in advance (operator, 2026-09-26) ---------------------------------------
    #
    # "upon approaching the target … why not place order in advance? … first come first serve has
    # our name too and in case it is a touch-and-fall case, we at least make profit on lot 1". The
    # books whose target is a TOUCH rest every rung's sell in advance (since 2026-10-03; before, the
    # next rung only); the engine fills each on a touch (exec/resting.py) and books it with the same
    # state changes as the touch would have made.

    def resting_target(self, pos: Position) -> tuple[int, float, int] | None:
        """The next rung's sell: ``(rung index, limit, qty)``, or None — the first of ``resting_ladder``."""
        ladder = self.resting_ladder(pos)
        return ladder[0] if ladder else None

    def resting_ladder(self, pos: Position) -> list[tuple[int, float, int]]:
        """Every target sell to rest now, lowest first: ``[(rung index, limit, qty), ...]`` (operator,
        2026-10-03: "adding all targets immediately as we know ... to make the most of first come first
        serve"; before, only the next rung rested). The base books' share ladder (T{n}, ``target_ladder``
        share of the position); the own-ladder books a lot a rung, the last the rest; an ``arm_at_pct``
        book's first rung no lower than entry + that %. RT-N (``arm_mode="immediate"``) rests its own R1
        too (operator, 2026-09-26: "yes RT-N to get advance target sells too"): its T1 otherwise arms on
        a 1-minute CLOSE over R1, which a resting order cannot wait for. An ``arm_at_pct`` book with no
        own rung at all rests its T1 at the minimum itself (review, 2026-09-26); any other position with
        no targets rests nothing. Rungs strictly rise; the lots go to the lowest rungs and the last rung
        placed takes the rest, so the quantity on sale is never more than is held."""
        if pos.status != "OPEN" or pos.qty_remaining <= 0:
            return []
        lim = self.limits
        i0 = pos.targets_hit
        if lim.trail_all_after_t1 and i0 >= 1:
            return []  # after T1 nothing more is sold at a rung: the rest rides the give-back line
        targets = tuple(pos.option_targets)
        if i0 >= len(targets) or targets[i0] <= 0:
            if not (lim.own_ladder and lim.arm_at_pct is not None and i0 == 0 and not targets):
                return []
            first = 0.0  # the minimum below is T1
        else:
            first = targets[i0]
        tick = pos.instrument.tick_size or 0.05
        if lim.own_ladder and i0 == 0:
            threshold, pct_arm = self._arm_threshold(pos)
            first = max(first, threshold)
            if pct_arm and (not targets or targets[0] < pct_arm):  # own T1 below the minimum (or none): the minimum is T1
                targets = (first, *[r for r in targets if r > first])[:4]
            else:
                targets = (first, *targets[1:])
        rungs: list[tuple[int, float]] = []
        for i in range(i0, len(targets) if targets else 1):
            px = _tick_up(targets[i] if targets else first, tick)
            if px <= 0 or (rungs and px <= rungs[-1][1]):
                continue  # a rung not above the one below is not a rung
            rungs.append((i, px))
        if lim.trail_all_after_t1:
            rungs = rungs[:1]
        out: list[tuple[int, float, int]] = []
        left = pos.qty_remaining
        if not lim.own_ladder:
            step = pos.instrument.qty_step
            for k, (i, px) in enumerate(rungs):
                share = lim.target_ladder[i] if i < len(lim.target_ladder) else 0.0
                if share <= 0 or left <= 0:
                    break
                want = int(pos.qty * share)
                want = (want // step) * step if step > 1 else want
                qty = left if (k == len(rungs) - 1 or want <= 0) else min(want, left)
                out.append((i, px, qty))
                left -= qty
            return out
        lot = max(1, pos.instrument.lot_size) * max(1, lim.arm_tranche_lots)
        for k, (i, px) in enumerate(rungs):
            if left <= 0:
                break
            last = k == len(rungs) - 1 and not lim.trail_all_after_t1
            if (last and i == 0 and len(targets) == 1 and lim.arm_at_pct is not None
                    and lim.peak_giveback_pct is not None):
                # arming synthesised T1 and there is no rung above it: the arm tranche, and the
                # give-back band carries the remainder (``_last_rung``)
                last = False
            qty = left if (last or left <= lot) else lot
            out.append((i, px, qty))
            left -= qty
        return out

    def resting_target_filled(self, pos: Position, now: float, price: float, mid: float | None) -> ExitDecision:
        """A resting target sell filled at ``price``: the same state changes as the touch (the SL
        steps, T1 arms the band), and the TARGET decision to book it with."""
        lim = self.limits
        i = pos.targets_hit
        if not lim.own_ladder:
            share = lim.target_ladder[i] if i < len(lim.target_ladder) else 0.0
            return ExitDecision(pos.id, ExitReason.TARGET, price, self._ladder_qty(pos, share),
                                f"T{i + 1} resting limit filled at {price:.2f} — taking {share:.0%}")
        if i == 0:
            _, pct_arm = self._arm_threshold(pos)
            if pct_arm and (not pos.option_targets or pos.option_targets[0] < pct_arm):
                # the minimum is T1 (as _own_ladder does when the price reaches it first)
                pos.option_targets = (price, *[r for r in pos.option_targets if r > price])[:4]
                pos.option_t1 = price
        d = self._touch_at(pos, now, price, mid if mid and mid > 0 else price, "option", f"resting sell {price:.2f} filled")
        return replace(d, note=f"T{i + 1} resting limit filled · {d.note}")

    def _own_ladder(self, pos: Position, view: MarketView, ltp: float, mid: float) -> ExitDecision | None:
        """The RT books' ladder. T1–T4 are the contract's own levels (which ones is the book's
        ``ladder_mode``). Touch → a lot out and the hard SL steps to the rung below; sustained → the
        SL steps to the rung (or stays behind, ``sl_lag``) and T1's sustain arms the band. Under
        ``arm_mode="immediate"`` the underlying touching its T1, or the option's 1-minute close over
        its own R1, arms at once. If the underlying reaches its own T1 first, the option's price at
        that instant is T1 and the higher own rungs follow it. Books carrying ``arm_at_pct`` never arm
        below the premium paid plus that percentage; nearer rungs wait for it."""
        lim = self.limits
        bucket = int(view.now // 60)
        prev = self._minute.get(pos.id)
        minute_close = prev[1] if prev is not None and prev[0] != bucket else None
        if ltp > 0:
            self._minute[pos.id] = (bucket, ltp)
        if lim.trail_all_after_t1 and pos.targets_hit >= 1:
            # after T1: no rung is sold and the SL steps no further than breakeven — the give-back
            # line from the latest peak (``_own_hard_stop``) and the stops are the only exits
            return None
        self._sustain(pos, view, mid, minute_close)
        i = pos.targets_hit
        threshold, pct_arm = self._arm_threshold(pos)
        own_t1_below = not pos.option_targets or pos.option_targets[0] < pct_arm
        if i == 0 and not pos.armed_by and view.underlying_ltp is not None and pos.equity_targets and ltp > 0:
            t1 = pos.equity_targets[0]
            hit = view.underlying_ltp >= t1 if pos.direction is Direction.BULLISH else view.underlying_ltp <= t1
            # An arm_at_pct book's T1 is max(own T1, the minimum): with an own T1 at or over the
            # minimum, the underlying reaching its T1 sells nothing — lot 1 waits for the option's own
            # T1, where its sell rests (review, 2026-09-26: this path sold lot 1 at 21.50 with the
            # own T1 at 24.00, cancelling the resting sell there).
            if hit and ltp >= threshold and (not pct_arm or own_t1_below):
                pos.option_targets = (ltp, *[r for r in pos.option_targets if r > ltp])[:4]
                pos.option_t1 = ltp
                return self._touch(
                    pos, view, mid, "equity",
                    f"underlying {view.underlying_ltp:.2f} reached its T1 {t1:.2f}, option at {ltp:.2f}",
                )
        if lim.arm_mode == "immediate" and i == 0 and not pos.armed_by:
            # a touch of R1 is not a close: the option must close a minute at or over it
            if pos.option_t1 > 0 and minute_close is not None and minute_close >= pos.option_t1:
                return self._touch(pos, view, mid, "option", f"1m close {minute_close:.2f} ≥ its own R1 {pos.option_t1:.2f}")
            return None
        if (
            ltp > 0 and i < len(pos.option_targets) and ltp >= pos.option_targets[i]
            and (i > 0 or (ltp >= threshold and pos.option_targets[0] >= pct_arm))
        ):
            # at i == 0 an own rung arms only if it sits at or over the minimum itself — the case
            # where the price jumps straight through both, and the real ladder is kept
            return self._touch(pos, view, mid, "option", f"option {ltp:.2f} ≥ its own rung")
        if pct_arm and i == 0 and not pos.armed_by and ltp >= pct_arm and own_t1_below:
            # The minimum is reached and there is no own rung at or over it: the minimum becomes T1.
            # Own rungs below it are dropped and higher ones follow; with none at all (BANKNIFTY
            # after its 1.53 % gap on 2026-09-24) the give-back band carries the rest. An own T1 AT
            # OR OVER the minimum is T1 itself (operator, 2026-09-26: "treat +5% as a floor, not as
            # where lot 1 actually sells") — this branch sold lot 1 at the first price over +5 %
            # even then (HINDUNILVR 2026-09-25: own T1 4.72, lot 1 out at 3.90 while the resting
            # T1 sell sat at 4.75, then walked to the bid over 45 s and filled at 3.62).
            pos.option_targets = (ltp, *[r for r in pos.option_targets if r > ltp])[:4]
            pos.option_t1 = ltp
            return self._touch(
                pos, view, mid, "option",
                f"option {ltp:.2f} ≥ entry +{lim.arm_at_pct:g}% ({pct_arm:.2f})",
            )
        return None

    def _mark(self, pos: Position, view: MarketView, mid: float) -> None:
        """Peak watermark on the mid, armed only after the entry noise has passed — or, under the
        own-ladder policy, only once a trigger has armed the ratchet."""
        lim = self.limits
        if lim.peak_giveback_pct is None or not view.quote_ok or mid <= 0:
            return
        if lim.own_ladder:
            # The band is not folded into ratchet_sl: it is computed live (_band_level), so the
            # rung SL keeps its own exit rule and the band keeps its own.
            if pos.armed_ts is not None and mid > pos.peak_mid:
                pos.peak_mid = mid
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
        if lim.own_ladder:
            return None  # folded into the rising stop (_mark / _own_hard_stop)
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
            level=round(level, 2), trigger_price=mid, trigger_on="option mid",
        )



def _tick_up(price: float, tick: float) -> float:
    """A sell's limit on the exchange's tick, never below the level it is for."""
    return round(math.ceil(price / tick - 1e-9) * tick, 4) if tick > 0 else round(price, 2)


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
    pos.realised_gross += gross
    pos.qty_remaining -= qty
    pos.charges += charges
    # the fill is a price the position WAS sold at: the excursions include it, so a stop's fill is never
    # below the MAE nor a target's above the MFE (a stale-quote exit is often the only read of that moment)
    if fill_price > 0 and pos.r_unit > 0:
        r = pos.r_now(fill_price)
        pos.mae_r = min(pos.mae_r, r)
        pos.mfe_r = max(pos.mfe_r, r)
    if decision.reason is ExitReason.TARGET:
        pos.targets_hit += 1
    if pos.qty_remaining <= 0:
        pos.status = "CLOSED"
        pos.closed_ts = now
        pos.exit_price = fill_price
        pos.exit_reason = decision.reason.value
    return gross


def replay_gap(
    pos: Position, candles: list[tuple[float, float]], limits: RiskLimits
) -> ExitDecision | None:
    """Re-run the breach logic over minutes the feed missed, oldest first.

    A paused clock is correct while the quote is absent, but on reconnect the engine knows only
    where the premium is *now* — not where it went. The broker does serve 1m candles for a listed
    option, so the gap can be walked rather than guessed at, and the sustain decided on what
    actually happened instead of on the first tick after recovery.

    ``candles`` is ``[(ts, close)]``. Two limits are honest to state: the resolution is one minute,
    so a breach that began and ended inside a single candle is invisible; and an expired contract
    leaves the scrip master, so this only works while the option is still listed. The hard floor
    covers both, being path-independent.
    """
    if limits.sustain_s is None or not candles:
        return None
    for ts, close in sorted(candles):
        if close <= 0 or pos.option_sl <= 0:
            continue
        if close > pos.option_sl:
            pos.breach_since = None
            continue
        if pos.breach_since is None:
            pos.breach_since = ts
        elif ts - pos.breach_since >= limits.sustain_s:
            return ExitDecision(
                pos.id,
                ExitReason.SL_OP,
                close,
                pos.qty_remaining,
                f"replayed the feed gap: below {pos.option_sl:.2f} continuously for "
                f"{ts - pos.breach_since:.0f}s (1m resolution)",
            )
    return None
