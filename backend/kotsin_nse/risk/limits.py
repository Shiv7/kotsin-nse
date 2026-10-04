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
    #: Fixed size (operator, 2026-09-27: "take 4 lots min as long as it is less than 75,000/-"):
    #: when set, a book buys exactly ``max_lots`` lots if they cost less than this, and nothing else
    #: sizes it — no risk budget, no position budget. (A 1 % risk budget, never the operator's rule,
    #: cut 25 of the 57 RT-X/RT-Y trades of 17-25 Sep below 4 lots.) A strike whose lots cost this or
    #: more is passed over by the selector for its next choice (operator, 2026-09-22: "try
    #: identifying far otm who's 4 lots rest within our cap").
    fixed_lots_under_inr: float | None = None
    #: After T1, sell nothing at the higher rungs and step the SL no further than breakeven: every lot
    #: left rides the give-back line from the latest peak, armed at the T1 touch (operator,
    #: 2026-09-27: "exit 1 lot and trail all for T2, T3, T4 or beyond till the 3% drop from the
    #: current latest peak").
    trail_all_after_t1: bool = False
    #: The option stop is never more than this % below the premium paid — a cap on what one lot can
    #: lose before the stock's own stop is reached (operator, 2026-09-27: "whenever the system detects
    #: that the entire premium is at risk, we need to kick-in a rule to protect it"). Set at the fill
    #: and held through every re-projection; the stop keeps the book's own confirmation (sustain,
    #: hard floor). None = off.
    max_premium_loss_pct: float | None = None
    #: The option stop is the option PRICED with the underlying at its stop (Black–Scholes, the
    #: option's own implied volatility, the time left) instead of the equity distance × a stand-in
    #: delta drawn as a straight line, which overstates the loss of an OTM option and reads a whole
    #: premium lost where the model keeps a quarter of it (instrument/pricing.py).
    priced_option_stop: bool = False
    #: a book that is not own-ladder could take its TARGETS from the option's own clustered ladder
    #: instead of the equity levels projected through delta. No book does: the operator (2026-09-26)
    #: — "the parents' targets come from that parent's own logic and strategy"
    targets_from_own_ladder: bool = False
    #: POSITION COUNTS ARE PER BOOK. FUKAA is derived from FUDKII, so the two fire on the same
    #: underlying in the same batch; counting them together meant FUDKII always took the slot and
    #: FUKAA — funded, gate-counted and advertised — could never take a single trade. That is the
    #: shape of a strategy that has never fired in its life, and this codebase exists to not ship
    #: it. The old stack ran them as separate books deliberately (cross-strategy scrip dedup was
    #: excised 2026-06-24); what it lacked was a view of the aggregate, which is the next field.
    #: Thirty concurrent positions per book (operator, 2026-09-24) — the parent, FUKAA and every
    #: RT/CT twin alike. It was 3, which is where the parent stopped on a busy morning while its
    #: own twins ran to 30: the same trigger filled in four books and the parent was the one that
    #: ran out of slots. The real constraint is the book's own Rs 10,00,000 wallet and its lot cap.
    max_positions_per_strategy: int = 30
    max_positions_per_underlying: int = 1  # per book
    #: ...and since 2026-09-23 the MONEY IS PER BOOK TOO: every book — the parent, its RT twins, the
    #: CT fades — is checked against its own positions and its own wallet. The cross-book
    #: aggregate stayed visible on the risk page but stopped gating anything the evening the
    #: 21–23 Sep replay showed the parent's ceiling (which counted its own twins, four positions a
    #: fill) turning the 09:45 burst into a lottery over two names. This field is now a per-book
    #: ceiling that sits above ``max_positions_per_strategy``; it never binds first.
    max_positions_all_books: int = 90
    max_underlying_exposure_pct: float = 20.0
    #: per WALLET, reset each morning (operator, 2026-09-26: "we agree on the 10% daily-loss halt for
    #: each separate wallet, which resets every morning"; it was 3%)
    daily_loss_limit_pct: float = 10.0
    #: per wallet and persistent: stays on day after day until that wallet is reset
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
    #: Own-ladder books: the minimum gain on the premium paid before ANY arm — own T1, equity T1 or
    #: the percentage itself; a nearer rung waits for it (operator, 2026-09-25: a rung 1 % over entry
    #: arming the trade and stepping the SL to breakeven is how KEI and GRASIM were stopped).
    #: Replaces the expected-move threshold, which asked an intraday trade
    #: for half of a whole DAY's move: on 2026-09-24 the four RT-Y positions needed +43 %, +49 %,
    #: +54 % and +72 % to arm, reached +5.7 %, +3.8 %, 0 % and 0 %, and not one of them ever armed.
    arm_at_pct: float | None = None
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
    #: -- the strategic stop (operator, 2026-10-04) ------------------------------------------------------
    #: "option": the stop is judged on the OPTION — the delta-projected option stop, the 75 s sustain, the
    #: 9 % hard floor, the stock stop as its confirmation — the rules every book ran with to 4 Oct.
    #: "equity": the STOCK stop is the thesis trigger and the option only the instrument sold
    #: (risk/exits.py ``_equity_stop``): a decisive breach sells at once, a marginal one is confirmed by
    #: magnitude × time, and the premium cap is the catastrophe backstop. The option-side stop rules are
    #: inert; the rising line (rung ratchet, give-back), targets, trail and backstops are untouched. Tape
    #: study 29 Sep – 1 Oct (73 trades): option stops fired with the stock a median 13 % of the way to its
    #: own stop; 23 of 33 stock breaches were back inside within 5 min. Set per engine in data/engine.json.
    stop_mode: str = "option"
    #: through the stock stop by this % of price: decisive, sold at once
    eq_stop_margin_pct: float = 0.10
    #: the stock moved this % against the trade over ``eq_stop_fast_window_s``: decisive, sold at once
    eq_stop_fast_pct: float = 0.35
    eq_stop_fast_window_s: float = 60.0
    #: the longest a marginal breach is confirmed before it sells ...
    eq_stop_confirm_s: float = 60.0
    #: ... or sooner, once the integral of (% through) over the continuous breach reaches this (%·s): a
    #: 0.05 % breach confirms in 20 s, a 0.02 % one in 50 s, a 0.10 % one at once (the margin). Only
    #: persistence separated the breaches that reversed from the ones that did not (nothing knowable at
    #: the breach instant did), so the wait is magnitude × time, never a clock alone.
    eq_stop_area_pct_s: float = 1.0
    #: the option bid (the last trade without one) this % under the premium paid sells whatever the stock
    #: says — the catastrophe backstop (None = off)
    eq_stop_premium_cap_pct: float | None = 25.0
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
    #: the option-side stop is never nearer than this many ticks below the entry premium (0 = off).
    #: A δ-projected stop on a ₹1.60 contract is one tick; the 2026-09-23 replay of the sub-₹5
    #: rejections through these engines used 8 ticks, and that is the setting that was validated.
    min_stop_ticks: int = 0
    #: entry gate on the twin (None = off): the mirror is skipped when the underlying's — or its
    #: front future's — trigger 30m bar and the one before are both under this × the T-2…T-7
    #: volume baseline (the reference router's dried-volume SKIP; NSE 0.85). SBILIFE 2026-09-23:
    #: FUT 0.79 / 0.51 into its daily S1, −5.9k.
    dried_volume_v: float | None = None
    #: Market-breadth gate (the operator's paper A/B, 2026-09-25): enter only when MORE than this
    #: share of the NSE universe trades beyond its own day open in the trade's direction at the
    #: trigger. Sep 1–25 replay, RT-Y option trades: +0.72 % above 0.5 against −2.88 % at or below
    #: it on the held-out half. None = no gate; an unmeasurable breadth never blocks.
    breadth_min: float | None = None
    #: Gate B (operator, 2026-09-26), both from the Sep 1–25 replay and both held in each half:
    #: skip when a classic 1d/1wk/1mo key level sits within this many ATR30 AHEAD of the close
    #: (−1.5 % / −1.6 % a trade against +0.1 % / +1.0 % without) ...
    skip_pivot_ahead_atr: float | None = None
    #: ... and skip a 09:45 trigger (the session's first 30m bar) whose open gapped this many
    #: daily ATRs the trade's own way (−2.0 % / −5.7 % against −0.7 % / −1.3 %). None = off.
    skip_open_gap_datr: float | None = None
    #: The 09:45 gap fade (CT-Y only): fade a first-bar trigger that gapped this many daily ATRs
    #: its own way — the opposite OTM, stop 1 ATR30 past the close. The replay's fade made +3.72 %
    #: / +5.87 % in the two halves (40 trades, before costs). None = off.
    gap_fade_datr: float | None = None
    #: The gap fade's own RR is a label; set this to make it a floor. None = never blocks.
    gap_fade_min_rr: float | None = None
    #: A shadow twin's equity stop, moved this many percent further from entry than the book it
    #: shadows (the "1 % past" test, operator 2026-09-26). The option stop is re-projected for the
    #: wider level. None = the stop as planned.
    equity_stop_buffer_pct: float | None = None
    #: A planned underlying stop nearer than this many ATR30 to the trigger's close is moved out to it at
    #: the fill, and the option stop re-projected for it (``Engine._floor_equity_stop``). Stop study,
    #: 2026-10-01: HDFCLIFE's stop sat 0.18 ATR30 under the close, a print AT it stopped every book at
    #: 09:56:33 and the call then ran 14.60 -> 18.20; grace on the trigger (a 75 s sustain, a 1-minute
    #: close, a buffer) did not save it — the option stop is the same level through delta — and hurt
    #: real breakdowns. On RT-Y: Aug engine replay +2,830, Aug-Sep model +1.8k, live 28 Sep - 1 Oct
    #: HDFCLIFE +7,535 against -2,200; never a worse worst trade (the 25 % premium cap still binds).
    #: None = the stop as planned.
    min_equity_stop_atr: float | None = None

    def position_budget(self, balance: float) -> float:
        return min(balance * self.max_position_pct / 100, self.max_position_inr)


#: The NSE books' fixed size (operator, 2026-09-27): exactly 4 lots when they cost less than this;
#: the selector steps further OTM when they do not. Never MCX: "₹75,000 cap does not apply to any
#: MCX trade" (RT-MCX has its own limits, and sizing skips the rule for an MCX contract).
FIXED_LOTS_UNDER_INR = 75_000.0


#: FUDKII-RT-X's exit policy. Everything else is the base book's; only the exit differs, which is
#: the whole point of running the two side by side.
RT_X_LIMITS = RiskLimits(
    max_lots=4,
    fixed_lots_under_inr=FIXED_LOTS_UNDER_INR,  # 4 lots under ₹75,000 (operator, 2026-09-27)                     # Rs 1,00,000 or 4 lots, whichever binds lower
    max_positions_per_strategy=30,  # its own pool of slots
    #: a per-book ceiling above the pool (every book counts only its own positions since 2026-09-23)
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
    min_stop_ticks=8,
)

#: The policy that ran on 2026-09-23 and took +10.8k on GRASIM: the contract's own daily R1–R4,
#: armed the moment the underlying touches its T1 or the option closes a minute over its R1, one
#: lot out at breakeven, then a 2 % give-back that needs three consecutive reads.
RT_N_LIMITS = RiskLimits(
    max_lots=4,
    fixed_lots_under_inr=FIXED_LOTS_UNDER_INR,  # 4 lots under ₹75,000 (operator, 2026-09-27)
    # the option stop priced with the stock at its stop, never more than 35 % under the premium paid
    # (operator, 2026-09-27: "fudkii-rt-n use only priced+35% logic" — RT-X, RT-Y, CT-X, CT-Y and the
    # parent as they are). 1-25 Sep, fresh purses: RT-N −2,05,273 → −1,80,291 (option-stop exits 44 → 27)
    priced_option_stop=True,
    max_premium_loss_pct=35.0,
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
    min_stop_ticks=8,
)

#: The third vertical, replayed to +19.7k on the same day: arm only once the option has made half
#: a day's expected move, the SL one rung behind, a wide band in the option's own volatility
#: units, and every post-arm stop needing the 75 s sustain.
RT_Y_LIMITS = RiskLimits(
    max_lots=4,
    fixed_lots_under_inr=FIXED_LOTS_UNDER_INR,  # 4 lots under ₹75,000 (operator, 2026-09-27)
    max_positions_per_strategy=30,
    max_positions_all_books=90,
    time_stop_bars=None,
    sustain_s=75.0,
    hard_floor_below_stop_pct=9.0,
    peak_giveback_pct=3.0,
    trail_dwell_samples=3,
    peak_arm_after_s=90.0,
    own_ladder=True,
    reproject_stop_s=10.0,
    ladder_mode="mtf",
    arm_mode="touch",
    arm_min_move=0.0,          # superseded by arm_at_pct (2026-09-24)
    arm_at_pct=5.0,            # never arm below entry +5 %; nearer own rungs wait for it
    sl_lag=True,
    giveback_move_frac=0.0,    # the expected-move band went with the expected-move arm
    band_exit="dwell",         # 3 s of confirmation, not 75: a 3 % give-back is meant to be prompt
    post_arm_sustain=True,
    dried_volume_v=0.85,
    min_stop_ticks=8,
    breadth_min=0.5,           # paper A/B: RT-Y gated, RT-X / RT-N not (2026-09-25)
    skip_pivot_ahead_atr=0.5,  # gate B (2026-09-26): a key pivot within 0.5 ATR30 ahead
    skip_open_gap_datr=0.3,    # gate B: a 09:45 trigger that gapped >= 0.3 daily ATR its own way
    min_equity_stop_atr=None,  # built and validated at RT_Y_STOP_FLOOR_ATR; OFF until the operator says (2026-10-01)
    # the option stop never more than 25 % under the premium paid (operator, 2026-09-28: "execute and
    # make live: RT-Y 25% premium cap on normal trades"). Replay 1–28 Sep, fresh purses: 28 trades,
    # ₹16,973 → ₹19,732 (SRF +1,844, IRFC +1,300, COLPAL −385). RT-Y's alone: the wide-stop shadow
    # and CT-Y inherit these limits and switch it off below.
    max_premium_loss_pct=25.0,
)

#: RT-Y's underlying-stop floor, validated by the 1 Oct stop study (``min_equity_stop_atr``) — not
#: switched on: the operator has not decided (2026-10-01). Turning it on is RT_Y_LIMITS'
#: ``min_equity_stop_atr=RT_Y_STOP_FLOOR_ATR``; RT-Y-F, CT-Y and the wide shadow stay off either way.
RT_Y_STOP_FLOOR_ATR = 0.5

#: The commodity book: RT-X's policy with the sizing it always had — up to 4 lots within the risk
#: and position budgets. The NSE books' fixed 4 lots under ₹75,000 is not for MCX (operator,
#: 2026-09-27: "₹75,000 cap does not apply to any MCX trade").
RT_MCX_LIMITS = replace(RT_X_LIMITS, fixed_lots_under_inr=None)

#: The counter-trend books: RT-X's and RT-Y's exits on the fade. No dried-volume gate — the wall
#: rule (strategy/counter.py) is the fade's own filter, as in the reference stack.
CT_X_LIMITS = replace(RT_X_LIMITS, dried_volume_v=None)
CT_Y_LIMITS = replace(
    RT_Y_LIMITS, dried_volume_v=None, breadth_min=None, skip_pivot_ahead_atr=None, skip_open_gap_datr=None,
    gap_fade_datr=0.3,  # CT-Y fades the 09:45 gap-with triggers RT-Y stands aside from (2026-09-26)
    max_premium_loss_pct=None,  # RT-Y's 25 % cap is RT-Y's (2026-09-28), not the fade's
    min_equity_stop_atr=None,   # so is its stop floor (2026-10-01): a fade's stop is its own plan's
)

#: The wide-stop shadow (operator, 2026-09-26: ""1% past" looks good this week — can we shadow
#: this"): RT-Y's exits exactly, on RT-Y's own entries, with the equity stop 1 % further away.
#: Sep 1–25 replay on gate-B trades: 1–18 Sep +0.43 % against +1.14 % on the touch, 19–25 Sep
#: +6.30 % against +3.90 % — unproven (+0.44 ± 1.86 points over the month), worst trade −41 %
#: against −35 %. It only mirrors RT-Y, so RT-Y's entry gates are its gates.
RT_Y_W1_LIMITS = replace(RT_Y_LIMITS, equity_stop_buffer_pct=1.0,
                         max_premium_loss_pct=None,  # a capped stop is not a wider one (2026-09-28)
                         min_equity_stop_atr=None)   # the PLAN's stop 1 % further, as always (1 Oct check)

#: The graded-F shadow (operator, 2026-09-28: "keep 'Graded-F triggers for RT-Y (with the raw-pivot
#: ladder)' as shadow book"): RT-Y's rules exactly — its gates, its exits, its 25 % premium cap — on
#: the triggers FUDKII grades F and does not publish, which RT-Y itself never sees. Its own purse,
#: kept off the trading tabs and shown on the Shadow page. Replay 1–28 Sep (inside RT-Y's purse):
#: 16 trades, 11 won, −₹2,364 — DIXON −13,251, BLUESTARCO −12,782, SBICARD −7,340.
RT_Y_F_LIMITS = replace(RT_Y_LIMITS, min_equity_stop_atr=None)  # RT-Y's floor is not the shadow's (mixed in the study)

#: FUDKII-CT-M (operator, 2026-10-03), a shadow: CT-Y's fade plan and exits on every published NSE trigger the
#: market is clearly against — at most this share of the NSE names past today's open the trigger's way (RT-Y
#: trades only above 50 %). The 25 Sep - 1 Oct actual replay: 3 fades, all won (+₹19,127). On the option model over
#: 24 Aug - 1 Oct the rule lost: 73 fades, −₹80,934 (−₹76,238 to 11 Sep, −₹4,696 after) — though following the same
#: triggers lost −₹2,17,547. Unproven, hence a shadow.
CT_M_MARKET_AGAINST_MAX = 0.45
CT_M_LIMITS = replace(CT_Y_LIMITS, gap_fade_datr=None)  # its own fade route, not CT-Y's 09:45 gap rule
