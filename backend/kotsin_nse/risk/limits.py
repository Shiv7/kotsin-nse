"""Risk limits — one frozen record, one home.

Everything that can stop a trade lives here. In the old stack the same rule lived in two services
with different values: the dashboard closed a HotStocks position after 3 trading days while the
executor carried ``maxhold.days=5``, so the real holding period was whichever job fired first and
neither code nor document named an authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


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
    #: The option carries its OWN classic ladder (R1–R4 from its previous session); nothing is
    #: delta-projected onto it. The ratchet arms when the underlying touches its own T1 or the
    #: option's 1-minute close reaches its own R1; one lot leaves on arming, the rest trail the
    #: peak and step the option's own R2/R3/R4. Operator's design, 2026-09-23.
    own_ladder: bool = False
    #: Re-derive the option-side stop from live delta this often. None keeps the entry projection
    #: (the base book) — a level computed off a delta that stopped being true at entry.
    reproject_stop_s: float | None = None
    #: Lots that leave when the ratchet arms.
    arm_tranche_lots: int = 1
    #: -- the knobs that tell the three RT books apart (docs/PIVOTS.md §6) --
    #: "mtf": the contract's own daily+weekly rungs above entry | "daily_r": its daily R1–R4 above entry
    ladder_mode: str = "mtf"
    #: "touch": T1 touch pays a lot, its sustain steps the SL and arms the band |
    #: "immediate": arming (equity T1 touch or own-R1 1m close) pays a lot and starts the band at once
    arm_mode: str = "touch"
    #: a rung must sit this many expected daily option moves above entry to be a target (0 = every rung)
    arm_min_move: float = 0.0
    #: the stepped SL trails one rung BEHIND the last touch (breakeven until T2 is touched)
    sl_lag: bool = False
    #: band = max(peak_giveback_pct, giveback_move_frac × expected daily option move), spread-floored
    giveback_move_frac: float = 0.0
    #: how the band exits: "through" (one read), "dwell" (trail_dwell_samples reads), "sustain" (sustain_s)
    band_exit: str = "through"
    #: the stepped rung SL also needs sustain_s continuous breach rather than one read
    post_arm_sustain: bool = False
    #: entry gate on the twin (None = off): the mirror is skipped when the underlying's — or its
    #: front future's — trigger 30m bar and the one before are both under this × the T-2…T-7
    #: volume baseline (the reference router's dried-volume SKIP; NSE 0.85). SBILIFE 2026-09-23:
    #: FUT 0.79 / 0.51 into its daily S1, −5.9k.
    dried_volume_v: float | None = None

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
    max_positions_all_books=90,
    time_stop_bars=None,            # exits on targets, ratchet, stop or the close — never a clock
    sustain_s=75.0,                 # continuous option-side breach before it counts
    hard_floor_below_stop_pct=9.0,  # path-independent escape hatch
    peak_giveback_pct=3.0,          # the rising stop: max(stepped rung SL, peak − 3 %), spread-floored
    trail_dwell_samples=3,
    peak_arm_after_s=90.0,
    own_ladder=True,
    reproject_stop_s=10.0,
    dried_volume_v=0.85,
)

#: The policy that ran on 2026-09-23 and took +10.8k on GRASIM: the contract's own daily R1–R4,
#: armed the moment the underlying touches its T1 or the option closes a minute over its R1, one
#: lot out at breakeven, then a 2 % give-back that needs three consecutive reads.
RT_N_LIMITS = RiskLimits(
    max_lots=4,
    max_positions_per_strategy=30,
    max_positions_all_books=90,
    time_stop_bars=None,
    sustain_s=75.0,
    hard_floor_below_stop_pct=9.0,
    peak_giveback_pct=2.0,
    trail_dwell_samples=3,
    peak_arm_after_s=90.0,
    own_ladder=True,
    reproject_stop_s=10.0,
    ladder_mode="daily_r",
    arm_mode="immediate",
    band_exit="dwell",
)

#: The third vertical, replayed to +19.7k on the same day: arm only once the option has made half
#: a day's expected move, the SL one rung behind, a wide band in the option's own volatility
#: units, and every post-arm stop needing the 75 s sustain.
RT_Y_LIMITS = RiskLimits(
    max_lots=4,
    max_positions_per_strategy=30,
    max_positions_all_books=90,
    time_stop_bars=None,
    sustain_s=75.0,
    hard_floor_below_stop_pct=9.0,
    peak_giveback_pct=10.0,
    trail_dwell_samples=3,
    peak_arm_after_s=90.0,
    own_ladder=True,
    reproject_stop_s=10.0,
    ladder_mode="mtf",
    arm_mode="touch",
    arm_min_move=0.5,
    sl_lag=True,
    giveback_move_frac=0.25,
    band_exit="sustain",
    post_arm_sustain=True,
    dried_volume_v=0.85,
)

#: The counter-trend books: RT-X's and RT-Y's exits on the fade. No dried-volume gate — the wall
#: rule (strategy/counter.py) is the fade's own filter, as in the reference stack.
CT_X_LIMITS = replace(RT_X_LIMITS, dried_volume_v=None)
CT_Y_LIMITS = replace(RT_Y_LIMITS, dried_volume_v=None)
