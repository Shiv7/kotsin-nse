"""Risk limits — one frozen record, one home.

Everything that can stop a trade lives here. In the old stack the same rule lived in two services
with different values: the dashboard closed a HotStocks position after 3 trading days while the
executor carried ``maxhold.days=5``, so the real holding period was whichever job fired first and
neither code nor document named an authority.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskLimits:
    #: fraction of the wallet risked if the initial stop is hit
    risk_per_trade_pct: float = 1.0
    #: hard ceiling on one position's premium outlay, as a fraction of the wallet
    max_position_pct: float = 10.0
    #: absolute ceiling, whichever binds first
    max_position_inr: float = 100_000.0
    #: hard ceiling on lots, whichever of this and the rupee cap binds *lower*. ``None`` = no
    #: lot ceiling, which is what the existing books have always had: BLUESTARCO sized to 20
    #: lots on 2026-09-22 because Rs 1,00,000 of a 15.26 premium is 20 lots of 325, and nothing
    #: said otherwise. FUDKII-RT's exit ladder is written in lots (one at T1, the rest on the
    #: trail), so for that book the count has to be the specified one.
    max_lots: int | None = None
    #: POSITION COUNTS ARE PER BOOK. FUKAA is derived from FUDKII, so the two fire on the same
    #: underlying in the same batch; counting them together meant FUDKII always took the slot and
    #: FUKAA — funded, gate-counted and advertised — could never take a single trade. That is the
    #: shape of a strategy that has never fired in its life, and this codebase exists to not ship
    #: it. The old stack ran them as separate books deliberately (cross-strategy scrip dedup was
    #: excised 2026-06-24); what it lacked was a view of the aggregate, which is the next field.
    max_positions_per_strategy: int = 3
    max_positions_per_underlying: int = 1  # per book
    #: ...and MONEY IS AGGREGATE. This is the P15 guard: one trigger fanning out into several
    #: funded positions is fine, as long as the total premium at risk in that underlying is capped
    #: across every book. Counting positions per book without this would just move the problem.
    max_positions_all_books: int = 6
    max_underlying_exposure_pct: float = 20.0
    daily_loss_limit_pct: float = 3.0
    max_drawdown_pct: float = 15.0
    #: new entries stop this many minutes before the segment's force-flat
    entry_cutoff_buffer_min: int = 45
    #: a position older than this is closed regardless of price. ``None`` switches it off, which
    #: is what the RT policy does — it exits on its targets, its ratchet, its stop or the close,
    #: and a bar count cutting a live trade at four hours is an exit nobody asked for. The
    #: default stays 8 so the books that already rely on it are unchanged.
    time_stop_bars: int | None = 8  # 8 × 30m
    # ── FUDKII-RT exit policy (off by default; only the RT_X book turns these on) ───────────
    #: an option-side breach must hold for this long before it exits. Measured as *continuous*
    #: breach: any recovery above the level resets the clock, because a touch now and another
    #: in five minutes is not a five-minute sustain.
    sustain_s: float | None = None
    #: below this much through the option stop, exit at once whatever the sustain says. The
    #: escape hatch: a collapse is not a wick, and it is path-independent so a feed gap cannot
    #: hide it.
    hard_floor_below_stop_pct: float = 9.0
    #: give-back from the peak, as a fraction. Floored by the live spread — on a 20.00 premium
    #: with a 0.20 spread, 2% is two ticks and one print would trip it.
    peak_giveback_pct: float | None = None
    peak_giveback_spread_mult: float = 1.5
    #: consecutive 1s samples required below a trail level. A single bad print is exactly one
    #: sample, so requiring three is the cheapest guard there is.
    trail_dwell_samples: int = 3
    #: the peak watermark only starts after the position has been open this long, so the first
    #: seconds of entry noise cannot set a peak the trade then has to live under.
    peak_arm_after_s: float = 90.0

    #: fraction of the position taken at each target
    target_ladder: tuple[float, ...] = (0.4, 0.3, 0.2, 0.1)
    #: once T1 prints, the stop moves to entry — the "T1 staircase"
    breakeven_after_t1: bool = True
    #: after the ladder, trail the peak by this fraction of the peak gain
    trail_giveback_pct: float = 40.0
    #: the trail only arms once the position is this far in profit (user-locked at 3% in the old
    #: stack across every regime; kept as one number for the same reason)
    trail_arm_pct: float = 3.0
    #: a hard floor under the option premium: never let a win become a loss beyond this
    hard_floor_pct: float = 50.0

    def position_budget(self, balance: float) -> float:
        return min(balance * self.max_position_pct / 100, self.max_position_inr)


#: FUDKII-RT-X's exit policy. Everything else is the base book's; only the exit differs, which is
#: the whole point of running the two side by side.
RT_X_LIMITS = RiskLimits(
    max_lots=4,                     # Rs 1,00,000 or 4 lots, whichever binds lower
    max_positions_per_strategy=30,  # its own pool of slots
    #: The all-books ceiling counts every live position, FUDKII's included, so the base book's
    #: 6 would stop the twins at three pairs. Raised only for the guard the twin is checked
    #: against; FUDKII keeps its own conservative ceiling.
    max_positions_all_books=60,
    time_stop_bars=None,            # exits on targets, ratchet, stop or the close — never a clock
    sustain_s=75.0,                 # continuous option-side breach before it counts
    hard_floor_below_stop_pct=9.0,  # path-independent escape hatch
    peak_giveback_pct=2.0,          # floored at 1.5x the live spread
    trail_dwell_samples=3,
    peak_arm_after_s=90.0,
)
