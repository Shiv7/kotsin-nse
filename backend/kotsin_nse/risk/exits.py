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
            self._track(pos, ltp)

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
        pos.option_sl = round(max(projected, pos.ratchet_sl), 2)

    def _tranche(self, pos: Position) -> int:
        lot = max(1, pos.instrument.lot_size) * max(1, self.limits.arm_tranche_lots)
        return pos.qty_remaining if pos.qty_remaining <= lot else min(lot, pos.qty_remaining)

    def _own_hard_stop(self, pos: Position, view: MarketView, mid: float) -> ExitDecision | None:
        """The rising stop — breakeven at the T1 touch, the last sustained rung, or the peak less
        the give-back, whichever is highest — is *hard*: trading through it ends the trade."""
        if pos.ratchet_sl <= 0 or not view.quote_ok or mid <= 0 or mid > pos.ratchet_sl:
            return None
        return ExitDecision(
            pos.id, ExitReason.SL_OP, view.option_ltp, pos.qty_remaining,
            f"hard SL {pos.ratchet_sl:.2f} traded through at {mid:.2f} "
            f"({'peak − give-back' if pos.armed_ts else 'breakeven'}; {pos.targets_hit} lot(s) already out)",
        )

    def _touch(self, pos: Position, view: MarketView, mid: float, by: str, why: str) -> ExitDecision:
        """A rung touched: one lot out (the last rung takes the rest), the hard SL steps to the
        rung below (breakeven for T1), and the sustain clock for this rung starts."""
        i = pos.targets_hit
        last = i >= len(pos.option_targets) - 1
        floor = pos.entry if i == 0 else pos.option_targets[i - 1]
        pos.ratchet_sl = round(max(pos.ratchet_sl, floor), 2)
        pos.option_sl = round(max(pos.option_sl, pos.ratchet_sl), 2)
        if i == 0:
            pos.armed_by = by
        pos.t_touch_ts = view.now if mid >= pos.option_targets[i] else None
        pos.t_close_ok = False
        qty = pos.qty_remaining if last else self._tranche(pos)
        return ExitDecision(
            pos.id, ExitReason.TARGET, view.option_ltp, qty,
            f"T{i + 1} {pos.option_targets[i]:.2f} touched by {by} ({why}) — {'the rest' if last else 'one lot'} out; "
            f"hard SL {pos.ratchet_sl:.2f}",
        )

    def _sustain(self, pos: Position, view: MarketView, mid: float, minute_close: float | None) -> None:
        """Sustained = continuously at or above the rung for ``sustain_s`` AND a 1-minute close at
        or above it since the touch. Completing it steps the hard SL to the rung; for T1 it arms."""
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
        if held >= (self.limits.sustain_s or 0) and pos.t_close_ok:
            pos.sustained_idx = k
            pos.ratchet_sl = round(max(pos.ratchet_sl, rung), 2)
            pos.option_sl = round(max(pos.option_sl, pos.ratchet_sl), 2)
            if k == 0:
                pos.armed_ts = view.now
                pos.peak_mid = max(pos.peak_mid, mid)
                pos.trail_dwell = 0

    def _own_ladder(self, pos: Position, view: MarketView, ltp: float, mid: float) -> ExitDecision | None:
        """The RT book's ladder: T1–T4 are the premium's own daily+weekly classic levels above
        the entry. Touch → a lot out and the hard SL steps to the rung below; sustained (75 s and
        a 1-minute close) → the hard SL steps to the rung itself, and for T1 that arms the
        peak give-back. If the underlying reaches its own T1 first, the option's price at that
        instant *is* T1 and the higher own rungs follow it."""
        bucket = int(view.now // 60)
        prev = self._minute.get(pos.id)
        minute_close = prev[1] if prev is not None and prev[0] != bucket else None
        if ltp > 0:
            self._minute[pos.id] = (bucket, ltp)
        self._sustain(pos, view, mid, minute_close)
        i = pos.targets_hit
        if i == 0 and not pos.armed_by and view.underlying_ltp is not None and pos.equity_targets and ltp > 0:
            t1 = pos.equity_targets[0]
            hit = view.underlying_ltp >= t1 if pos.direction is Direction.BULLISH else view.underlying_ltp <= t1
            if hit:
                pos.option_targets = (ltp, *[r for r in pos.option_targets if r > ltp])[:4]
                pos.option_t1 = ltp
                return self._touch(pos, view, mid, "equity", f"underlying {view.underlying_ltp:.2f} reached its T1 {t1:.2f}, option at {ltp:.2f}")
        if ltp > 0 and i < len(pos.option_targets) and ltp >= pos.option_targets[i]:
            return self._touch(pos, view, mid, "option", f"option {ltp:.2f} ≥ its own rung")
        return None

    def _mark(self, pos: Position, view: MarketView, mid: float) -> None:
        """Peak watermark on the mid, armed only after the entry noise has passed — or, under the
        own-ladder policy, only once a trigger has armed the ratchet."""
        lim = self.limits
        if lim.peak_giveback_pct is None or not view.quote_ok or mid <= 0:
            return
        if lim.own_ladder:
            if pos.armed_ts is None:
                return
            if mid > pos.peak_mid:
                pos.peak_mid = mid
            give = lim.peak_giveback_pct / 100
            if view.spread_pct:
                give = max(give, view.spread_pct * lim.peak_giveback_spread_mult)
            lifted = round(pos.peak_mid * (1 - give), 2)
            if lifted > pos.ratchet_sl:
                pos.ratchet_sl = lifted
                pos.option_sl = round(max(pos.option_sl, pos.ratchet_sl), 2)
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
