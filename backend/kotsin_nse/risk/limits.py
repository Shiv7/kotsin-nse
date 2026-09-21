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
    #: a position older than this is closed regardless of price
    time_stop_bars: int = 8  # 8 × 30m = one session
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
