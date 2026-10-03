"""Every book the old stack ran, what it needs, and whether this engine can run it yet.

The ``/all-strategies`` page is a *port plan*, not a mirror. The dashboard's ``/strategy`` page
renders one tab per book by fetching ``/strategy-state/<book>/…`` from a Mongo collection that
**streamingcandle** fills over Kafka — the signal there *is* the computation, so unlike HotStocks
(whose inputs turned out to be public NSE publications this engine can fetch itself) there is no
external source that can stand in for a book this repo does not implement.

So each entry records four things, and never guesses at any of them:

* ``params`` — the values actually deployed in the old stack, transcribed from
  ``streamingcandle/src/main/resources/application.properties`` with the key kept verbatim so a
  reader can grep for it. Not defaults, not recommendations: what was running.
* ``have`` / ``need`` — which inputs this engine already produces and which it does not. This is
  the honest measure of what a port costs; several books need nothing new at all.
* ``status`` — ``live`` when this repo trades it, ``alerting`` when it computes and publishes the
  book but deliberately keeps it out of the gateway, ``not_ported`` when it does not compute it
  at all. The page states which, plainly, rather than rendering an empty tab that looks like a
  quiet market. Nothing moves to ``live`` on the strength of having been written.

Pure data, no imports: the ``strategy is pure`` contract forbids this package from reaching for
venue, exec, ledger, api, ops, feed or instrument, and a table of facts needs none of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SRC = "streamingcandle/src/main/resources/application.properties"


@dataclass(frozen=True, slots=True)
class Book:
    key: str
    label: str
    tf: str
    summary: str
    status: str  # "live" | "alerting" | "not_ported"
    params: dict[str, str] = field(default_factory=dict)
    have: tuple[str, ...] = ()
    need: tuple[str, ...] = ()
    source: str = ""
    note: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "tf": self.tf,
            "summary": self.summary,
            "status": self.status,
            "params": self.params,
            "have": list(self.have),
            "need": list(self.need),
            "source": self.source,
            "note": self.note,
            "paramsSource": SRC if self.params else "",
        }


#: Inputs this engine already produces, named once so the entries below can reference them.
BARS = "30m/1m bars on the session grid"
PIVOTS = "MTF pivot zones + confluence stop/targets"
OI = "live OI (socket, GetScripInfoForFuture)"
DEPTH = "20-level depth + L1 OFI / microprice (bars/micro.py)"
CHAIN = "option chain, lot size, expiry"
COST = "cost model + sizing from the stop"

BOOKS: tuple[Book, ...] = (
    Book(
        key="FUDKII",
        label="FUDKII",
        tf="30m",
        summary="SuperTrend flip coinciding with a Bollinger break, expressed as an OTM option.",
        status="live",
        params={
            "fudkii.trigger.st.period": "7",
            "fudkii.trigger.bb.period": "20",
            "fudkii.trigger.bb.mult": "2.0",
            "fudkii.trigger.require.both": "true",
        },
        have=(BARS, PIVOTS, CHAIN, COST),
        source="kotsin_nse/strategy/fudkii.py",
        note=(
            "Runs here. The inherited parameters backtest at −1.40R (t = −7.96) over 481 trades; "
            "see docs/strategies/FUDKII.md §8 before trusting a signal."
        ),
    ),
    Book(
        key="FUKAA",
        label="FUKAA",
        tf="30m",
        summary=(
            "FUDKII's trigger, admitted only when volume confirms participation. In SHADOW since 2 Oct 2026: every signal "
            "is recorded with the inputs it read and its alignment with the market and the OI, and never traded."
        ),
        status="live",
        params={
            "volume": "max(T, T-1) ≥ 4.0x its T-2…T-7 baseline (the engine's checked slot reading); else watched one bar, promoted at T+1",
            "composite.min": "60 — volume, OI, momentum and RR scores",
            "OI change": "against the previous session's close: current month; current + next in the last 3 sessions; z vs the NIFTY50",
            "momentum": "the trigger bar's body on a 60-bar ATR",
            "refoi.nse / refoi.mcx": "5.0 % / 8.0 %",
            "shadow": "True — a SHADOW row and a fukaa.shadow event per signal, never an order",
        },
        have=(BARS, PIVOTS, CHAIN, COST, OI),
        source="kotsin_nse/strategy/fukaa.py; kotsin_nse/bars/oi_read.py; kotsin_nse/engine.py (_fukaa_shadow, oi_reading)",
        note=(
            "Inputs fixed 2 Oct: the broker's OI change is 0.0 on every frame and momentum had never been computed, so it "
            "could never fire. 23 Sep – 1 Oct estimate with the fixed inputs: 7 signals, about −₹11,900. Thresholds to be "
            "re-fitted after the 27 Oct expiry; the Shadow page's FUKAA tab lists every signal."
        ),
    ),
    Book(
        key="FUDKII_RT_X",
        label="FUDKII-RT-X",
        tf="30m entry, 1s exit",
        summary=(
            "FUDKII's entries under the RT exit policy — sustained option stop, hard floor and a "
            "peak ratchet — on its own wallet, so only the exit differs."
        ),
        status="live",
        params={
            "sustain_s": "75 (continuous breach)",
            "hard_floor_below_stop_pct": "9.0",
            "own_ladder": "the option's own daily+weekly classic rungs above entry; T1 touch pays a lot, T1 sustained arms",
            "peak_giveback_pct": "3.0 (floored at 1.5x the live spread), one read through it exits",
            "trail_dwell_samples": "3",
            "peak_arm_after_s": "90",
            "time_stop_bars": "None (off)",
            "dried_volume_skip": "no mirror when surgeT and surgeT-1 are both < 0.85x the T-2..T-7 volume baseline, on the equity or its front future",
            "premium_floor": "none (the FUDKII family selects without the Rs 5 floor; FUKAA keeps it); option stop never nearer than 8 ticks",
        },
        have=("30m/1m bars on the session grid", "MTF pivot zones + confluence stop/targets"),
        source="kotsin_nse/risk/exits.py (RT policy branch)",
        note=(
            "Runs beside FUDKII on the same signal and the same contract at the same entry, so the "
            "only variable is the exit. Both curves are paper."
        ),
    ),
    Book(
        key="FUDKII_RT_MCX",
        label="FUDKII-RT-MCX",
        tf="30m entry, 1s exit",
        summary=(
            "The RT exit policy on MCX commodities, in its own Rs 30,00,000 wallet so its thirty "
            "slots are reachable."
        ),
        status="live",
        params={
            "initial_inr": "3,000,000",
            "max_lots": "4",
            "max_positions_per_strategy": "30",
            "sustain_s": "75 (continuous breach)",
            "peak_giveback_pct": "2.0 (floored at 1.5x the live spread)",
        },
        have=("30m/1m bars on the session grid", "MTF pivot zones + confluence stop/targets"),
        source="kotsin_nse/risk/exits.py (RT policy branch)",
        note=(
            "Separate from the NSE book on purpose: one CRUDEOIL lot is a different size of bet "
            "from one equity lot, and a shared wallet would let whichever fired first decide what "
            "the other could afford. Bands on the contract's own realised vol, not India VIX."
        ),
    ),
    Book(
        key="FUDKII_RT_N",
        label="FUDKII-RT-N",
        tf="30m entry, 1s exit",
        summary=(
            "FUDKII's entries under the immediate-arming RT policy: the option's own daily R1-R4, "
            "armed the moment the underlying touches its T1 or the option closes a minute over its "
            "R1, one lot out, then a 2% give-back that needs three consecutive reads."
        ),
        status="live",
        params={
            "ladder_mode": "daily_r",
            "arm_mode": "immediate",
            "peak_giveback_pct": "2.0 (floored at 1.5x the live spread)",
            "band_exit": "dwell (3 consecutive reads)",
            "sustain_s": "75 (the option stop before arming)",
        },
        have=("30m/1m bars on the session grid", "MTF pivot zones + confluence stop/targets"),
        source="kotsin_nse/risk/exits.py (own-ladder branch)",
        note=(
            "The policy that ran on 2026-09-23 (+10.8k on GRASIM). Same fills as RT-X and RT-Y, "
            "its own wallet, so the three curves differ only in the exit."
        ),
    ),
    Book(
        key="FUDKII_RT_Y",
        label="FUDKII-RT-Y",
        tf="30m entry, 1s exit",
        summary=(
            "FUDKII's entries under the third RT vertical: arm once the option is +5 % on the "
            "premium paid, never sooner, a nearer own T1 waiting for it; the SL one rung behind; "
            "once armed, every lot left exits on a 3 % fall from the highest price since arming."
        ),
        status="live",
        params={
            "ladder_mode": "mtf (every own daily+weekly rung above entry)",
            "arm_at_pct": "5.0 (the minimum before any arm; own T1s below it wait; set 2026-09-25)",
            "sl_lag": "true (breakeven until T2 touches, then T1, ...)",
            "peak_giveback_pct": "3.0 (all remaining lots out on a 3 % fall from the post-arm peak)",
            "band_exit": "dwell (3 consecutive one-second reads)",
            "post_arm_sustain": "true (the rung SL keeps its 75 s sustain)",
            "dried_volume_skip": "no mirror when surgeT and surgeT-1 are both < 0.85x the T-2..T-7 volume baseline, on the equity or its front future",
            "max_premium_loss_pct": "25 (the option stop never more than 25 % under the premium paid; set 2026-09-28)",
        },
        have=(
            "30m/1m bars on the session grid",
            "MTF pivot zones + confluence stop/targets",
            "per-name ATM implied vol (market/iv.py)",
        ),
        source="kotsin_nse/risk/exits.py (own-ladder branch); market/iv.py expected_move_frac",
        note=(
            "Rule changed 2026-09-24. The expected-move arm never fired: that day's four RT-Y "
            "positions needed +43 % to +72 % to arm and peaked at +5.7 %. BANKNIFTY's PE, filled at "
            "205.93 and peaking at 217.70, wanted 293.85. Replayed on the tick tape under the new "
            "rule it arms at the 216.23 touch, pays one lot at 216.45 and leaves the rest on the "
            "3 % line at 208.50: gross +547 where the old rule ended at breakeven. Paper."
        ),
    ),
    Book(
        key="FUDKII_CT_X",
        label="FUDKII-CT-X",
        tf="30m entry, 1s exit",
        summary=(
            "The counter-trend fade: when a FUDKII trigger runs into a multi-timeframe pivot wall "
            "(strength >= 5.2, nearness-clustered within 0.25 ATR, reached within 0.5 ATR of the "
            "candle's extreme) on a genuine ST flip, the opposite OTM is bought with the confluence "
            "plan recomputed for the flipped direction — under RT-X's exits."
        ),
        status="live",
        params={
            "counter_wall_min": "5.2 (daily 4.0 / weekly 3.2 / monthly 2.0 x nearness rank 1.0/0.8/0.6/0.4)",
            "cluster_atr": "0.25", "reach_atr": "0.5", "require_st_flip": "true",
            "lots": "one lot out per target (own ladder)",
        },
        have=("30m/1m bars on the session grid", "MTF pivot zones + confluence stop/targets"),
        source="kotsin_nse/strategy/counter.py; streamingcandle FudkiiSignalTrigger wall-counter (live 2026-09-11)",
        note=(
            "Its own books beside the in-trend mirrors, not instead of them: the same signals, the "
            "opposite direction, so the fade is measured against RT-X on identical triggers. The old "
            "geometry fade and the F14 flip were both retired by the reference stack as net-losing; "
            "this is the rule it kept."
        ),
    ),
    Book(
        key="FUDKII_CT_Y",
        label="FUDKII-CT-Y",
        tf="30m entry, 1s exit",
        summary=(
            "Two fades under RT-Y's exits: (a) its own 09:45 gap fade — a first-bar trigger that gapped 0.3 daily ATR or more "
            "its own way, faded with the stock stop 1 ATR30 past the close and the fade side's walls as targets; (b) CT-X's "
            "counter-trend fade, decided for itself, except on a trigger it already gap-faded."
        ),
        status="live",
        params={
            "gap_fade_datr": "0.3 daily ATR, the trigger's own way, 09:45 bar only",
            "fade plan": "Engine.fade_plan — the same plan FUDKII-CT-M uses",
            "counter_wall_min": "5.2 (route b, CT-X's)",
            "exits": "RT-Y policy incl. the +5 % T1 floor; no 25 % premium cap",
            "size": "4 lots under ₹75,000",
        },
        have=("30m/1m bars on the session grid", "MTF pivot zones + confluence stop/targets", "per-name ATM implied vol (market/iv.py)"),
        source="kotsin_nse/engine.py (gap_fade_plan, fade_plan, _gap_fade); kotsin_nse/strategy/counter.py; kotsin_nse/risk/exits.py",
        note=(
            "Its own entries since 26 Sep — no longer a mirror of CT-X's fill. Replay 24 Aug – 1 Oct: +₹32,306 on 18 fades, "
            "the profit from fading B/C-graded triggers. The Shadow page's Gap fade tab tracks route (a)."
        ),
    ),
    Book(
        key="FUDKII_RT_Y_W1",
        label="RT-Y · wide stop (shadow)",
        tf="30m entry, 1s exit",
        summary=(
            "A shadow of RT-Y: every RT-Y entry, the same contract, size, price and instant, under "
            "RT-Y's exits with one change — the equity stop 1 % further from entry, and the option "
            "stop re-projected for it. It answers whether the \"1 % past\" stop beats the touch."
        ),
        status="live",
        params={
            "equity_stop_buffer_pct": "1.0 (bullish: stop x 0.99; bearish: stop x 1.01)",
            "entries": "RT-Y's only — a trigger RT-Y's gates skip is never in this book",
            "exits": "RT-Y policy (arm +5 %, SL one rung behind, 3 % give-back, 75 s sustain, hard floor 9 % under the option stop)",
        },
        have=("30m/1m bars on the session grid", "MTF pivot zones + confluence stop/targets"),
        source="kotsin_nse/engine.py (_open_shadow_twins); kotsin_nse/risk/limits.py RT_Y_W1_LIMITS",
        note=(
            "Sep 1–25 replay on gate-B trades: 1–18 Sep +0.43 % against the touch's +1.14 %, "
            "19–25 Sep +6.30 % against +3.90 %; over the month +0.44 ± 1.86 points — unproven, "
            "and the worst trade was −41 % against −35 %. Paper, shown on the Shadow page."
        ),
    ),
    Book(
        key="FUDKII_RT_Y_F",
        label="RT-Y · graded F (shadow)",
        tf="30m entry, 1s exit",
        summary=(
            "A shadow with entries of its own: every NSE trigger FUDKII grades F and does not publish, judged by RT-Y's "
            "gates and traded under RT-Y's exits with its 25 % premium cap. Where no pivot cluster ahead makes a target, "
            "the raw pivots ahead, nearest first, are the ladder. It answers whether RT-Y should take graded-F triggers."
        ),
        status="live",
        params={
            "entries": "triggers FUDKII does not publish (NOT_PUBLISHED), NSE only — never a published one",
            "gates": "RT-Y's: breadth > 50 %, no key pivot within 0.5 ATR30 ahead, no 09:45 gap ≥ 0.3 daily ATR its way, dried volume",
            "exits": "RT-Y policy, option stop never more than 25 % under the premium paid",
        },
        have=("30m/1m bars on the session grid", "MTF pivot zones + confluence stop/targets"),
        source="kotsin_nse/engine.py (_handle_unpublished); kotsin_nse/risk/limits.py RT_Y_F_LIMITS; strategy/fudkii.py (raw-pivot ladder)",
        note=(
            "Replay 1–28 Sep (inside RT-Y's purse): 16 trades, 11 won, −₹2,364. Paper, shown on the Shadow page; "
            "its P&L is never added to the trading books'."
        ),
    ),
    Book(
        key="FUDKII_CT_M",
        label="FUDKII-CT-M · market-against fade (shadow)",
        tf="30m entry, 1s exit",
        summary=(
            "A shadow with entries of its own: every published NSE trigger the market is clearly against — at most 45 % "
            "of the NSE names past today's open the trigger's way — is faded with CT-Y's plan (the opposite OTM, the "
            "stock stop 1 ATR30 past the close, the walls on the fade's side) and traded under CT-Y's exits."
        ),
        status="live",
        params={
            "entries": "published NSE triggers with breadth ≤ 45 % (risk/limits.py CT_M_MARKET_AGAINST_MAX)",
            "plan": "CT-Y's fade plan (Engine.fade_plan): stop 1 ATR30 past the close, the fade side's walls, else 1 ATR30",
            "exits": "CT-Y's: RT-Y's policy incl. the +5 % T1 floor, no 25 % premium cap",
        },
        have=("30m/1m bars on the session grid", "market breadth at the trigger", "MTF pivot zones"),
        source="kotsin_nse/engine.py (_market_fade, fade_plan); kotsin_nse/risk/limits.py CT_M_LIMITS",
        note=(
            "Operator, 2026-10-03. 25 Sep - 1 Oct actual replay: 3 fades, all won, +₹19,127 (two more had no option "
            "prices). Option model 24 Aug - 1 Oct: 73 fades, −₹80,934 (−₹76,238 to 11 Sep, −₹4,696 after). Paper; the Shadow "
            "page's Market fade tab lists every fade and skip; its P&L is never added to the trading books'."
        ),
    ),
    Book(
        key="FUDKII_RT",
        label="FUDKII-RT",
        tf="1m on a 30m signal",
        summary=(
            "A fired FUDKII signal kept alive and re-evaluated on every tick until it expires, "
            "rather than being decided once at the boundary."
        ),
        status="alerting",
        params={
            "fudkii.rt.living.enabled": "true",
            "fudkii.rt.living.ttl.ms": "2100000 (35m)",
            "fudkii.rt.living.refresh.ms": "60000 (1m)",
            "fudkii.rt.living.reeval.ms": "300000 (5m)",
            "fudkii.rt.living.reeval.rr.gate": "1.0",
        },
        have=(BARS, PIVOTS, CHAIN, COST),
        source="alerts/detectors.py FudkiiRtDetector",
        note=(
            "Ported. Adopts each FUDKII signal as it fires, re-checks it on the 1m close every 5 minutes, and retires it when the reward left falls under the 1.0R gate or the 35m TTL runs out — emitting the retirement, because a signal that quietly vanished looks identical to one that was never taken."
        ),
    ),
    Book(
        key="FUDKOI",
        label="FUDKOI",
        tf="30m",
        summary="FUDKII gated on an open-interest move, so the option leg has flow behind it.",
        status="alerting",
        params={
            "fudkoi.trigger.enabled": "true",
            "fudkoi.trigger.oi.threshold.mcx": "100.0",
            "fudkoi.trigger.kafka.topic": "kotsin_FUDKOI",
        },
        have=(BARS, PIVOTS, CHAIN, OI),
        source="alerts/detectors.py FudkoiDetector",
        note=(
            "Ported. The FUDKII geometry plus the OI confirmation, thresholded at the deployed 100% on MCX and the 5% NSE reference. Cash equity carries no OI, so it reads the underlying's front future — and declines to fire when OI is absent rather than treating absent as zero."
        ),
    ),
    Book(
        key="PIVOTBOSS",
        label="PIVOTBOSS",
        tf="daily bias + intraday",
        summary="CPR width regime and pivot confluence as a directional day bias.",
        status="alerting",
        params={
            "pivotboss.bias.threshold.strong": "65",
            "pivotboss.bias.threshold.mild": "40",
            "pivotboss.cpr.width.narrowFactor": "0.5",
            "pivotboss.cpr.width.wideFactor": "1.5",
            "pivotboss.cadence.maxPerScripPerDay": "2",
            "pivotboss.cadence.cooldownMinutes": "60",
            "pivotboss.cadence.globalDailyCap": "30",
            "pivotboss.confluence.bpsIndex": "15",
        },
        have=(PIVOTS, BARS),
        source="alerts/detectors.py PivotBossDetector",
        note=(
            "Ported. CPR width against its own 11-session average gives the regime; only NARROW is actionable, since a wide CPR is a range day and not a signal. Bias is distance from the central pivot in ATR plus the room to the next confluence wall, under the deployed cadence caps of 2 per scrip and 30 a day."
        ),
    ),
    Book(
        key="MCX_BB_30",
        label="MCX_BB30",
        tf="30m (MCX)",
        summary="Bollinger break on MCX commodities with a volume-surge floor and a cooldown.",
        status="alerting",
        params={
            "mcxbb30.trigger.cooldown.ms": "see " + SRC,
            "mcxbb.trigger.min.score": "see " + SRC,
            "mcxbb30.trigger.kafka.topic": "kotsin_MCX_BB_30",
        },
        have=(BARS, PIVOTS, COST),
        source="alerts/detectors.py BbBreakDetector",
        note=(
            "Ported. Shares one detector with its NSE and 15m siblings; only the cooldown and surge floor differ, which is exactly how the three differed in the old stack."
        ),
    ),
    Book(
        key="MCX_BB_15",
        label="MCX_BB15",
        tf="15m (MCX)",
        summary="The same break on the 15m frame, with a shorter cooldown.",
        status="alerting",
        params={
            "mcxbb15.trigger.cooldown.ms": "1800000 (30m)",
            "mcxbb15.trigger.min.volume.surge": "1.0",
            "mcxbb15.trigger.kafka.topic": "kotsin_MCX_BB_15",
        },
        have=(BARS, PIVOTS, COST),
        source="alerts/detectors.py BbBreakDetector",
    ),
    Book(
        key="NSE_BB_30",
        label="NSE_BB30",
        tf="30m (NSE)",
        summary="The MCX break ported to NSE equities, with a higher surge floor.",
        status="alerting",
        params={
            "nsebb30.trigger.cooldown.ms": "5400000 (90m)",
            "nsebb30.trigger.min.volume.surge": "1.5",
            "nsebb30.trigger.kafka.topic": "kotsin_NSE_BB_30",
        },
        have=(BARS, PIVOTS, COST),
        source="alerts/detectors.py BbBreakDetector",
    ),
    Book(
        key="RETEST",
        label="RETEST",
        tf="5m",
        summary="A broken level retested and held, scored before entry.",
        status="not_ported",
        params={
            "retest.v2.enabled": "true",
            "retest.v2.fresh.turn.5m.max.bars": "2",
            "retest.v2.rtscore.min": "70",
        },
        have=(BARS, PIVOTS),
        need=("broken-level tracking", "the RT score"),
        source="streamingcandle BrokenLevel.java, CandidateLevel.java",
    ),
    Book(
        key="MICROALPHA",
        label="MICROALPHA",
        tf="5m",
        summary="Order-flow imbalance and book pressure as a short-horizon conviction score.",
        status="not_ported",
        params={
            "microalpha.enabled": "false  (already off in the old stack)",
            "microalpha.trigger.min.conviction": "15",
            "microalpha.trigger.high.conviction": "40",
            "microalpha.trigger.cooldown.minutes": "10",
            "microalpha.trigger.max.signals.per.day": "8",
            "microalpha.trigger.require.orderbook": "true",
            "microalpha.trigger.atr.stop.multiplier": "1.5",
            "microalpha.flow.ofi.weight": "0.40",
        },
        have=(DEPTH, BARS),
        need=("the conviction blend on top of OFI",),
        source="streamingcandle (microalpha)",
        note=(
            "Was already disabled in the old stack (enabled=false). This venue has no trade tape "
            "and no aggressor side, so Kyle's λ and VPIN are not computable — bars/micro.py says "
            "so rather than approximating them, and any port inherits that limit."
        ),
    ),
    Book(
        key="QUANT",
        label="QUANT",
        tf="multi",
        summary="A composite score across timeframes, cached for the other books to read.",
        status="not_ported",
        params={
            "quant.score.timeframes": "see " + SRC,
            "quant.score.options.analytics.enabled": "see " + SRC,
        },
        have=(BARS, PIVOTS, OI, CHAIN),
        need=("the score definition and its cache",),
        source="streamingcandle (quant.score)",
    ),
    Book(
        key="MERE",
        label="MERE",
        tf="30m MTF",
        summary="Multi-timeframe agreement book; the old repo carries its backtest, not its config.",
        status="not_ported",
        params={},
        have=(BARS, PIVOTS),
        need=("the definition itself — no deployed config exists to transcribe",),
        source="streamingcandle/backtest/mere_mtf_backtest.py (research only)",
        note=(
            "The only book here with no deployed parameters anywhere in the old stack: it lived as "
            "a backtest script. Porting it means choosing parameters, which is a research task, "
            "not a transcription."
        ),
    ),
)

BY_KEY: dict[str, Book] = {b.key: b for b in BOOKS}
LIVE_KEYS: tuple[str, ...] = tuple(b.key for b in BOOKS if b.status == "live")
#: Books this engine computes and publishes, but does not trade.
ALERTING_KEYS: tuple[str, ...] = tuple(b.key for b in BOOKS if b.status == "alerting")
