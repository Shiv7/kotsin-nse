"""The engine: one asyncio process wiring feed → bars → strategies → risk → gateway → ledger → API.

Everything the old stack spread across five JVMs, Kafka, Redis and Mongo happens here, in order,
in one place. That is not minimalism for its own sake — the failure modes that cost the most time
in the old system were all *between* the hops.

Task layout:

* ``_feed`` — the broker socket, reconnecting on its own budget;
* ``_clock`` — 1 Hz: closes bars whose bucket ended, runs exits, enforces the force-flat;
* ``_decide`` — consumes closed 30m bars, runs FUDKII then FUKAA, sizes, submits;
* ``_housekeeping`` — reconciliation, wallet snapshots, health, day rollover, scrip-master refresh.

Mode lives in the control table and is read on every submit. LIVE requires an ``armed_until``: an
engine that restarts after expiry comes up in PAPER and says so.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from bisect import bisect_right
from collections import deque
from collections.abc import Awaitable, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar

import httpx
import structlog

from .alerts.engine import LOOKBACK as ALERT_LOOKBACK
from .alerts.engine import AlertEngine
from .bars.aggregator import Aggregator
from .bars.daily import (
    REPAIR_BATCH,
    DailyCache,
    basis_ok,
    is_official,
    one_per_session,
    previous_session,
)
from .bars.daily import audit as audit_daily
from .bars.indicators import atr, dried_volume
from .bars.micro import MicroAggregator
from .bars.oi_candles import OiCandleBuilder
from .bars.oi_read import OiReading, oi_quadrant, read_oi, relative_z
from .bars.pivots import (
    PivotPoint,
    Zone,
    compute_confluence,
    pivot_points,
)
from .bars.store import BarStore
from .bars.unified import BarSource, UnifiedBar
from .bars.verify import BarReconciler
from .bars.volume_read import (
    MarketVolume,
    VolBar,
    VolumeReading,
    market_volume,
    read_volume,
    slot_reading,
)
from .bars.zones import ZoneBuild, build_zones, is_provisional
from .bus import Bus, Topic
from .committee.service import CommitteeService
from .config import Segment, Settings
from .domain import (
    Direction,
    ExitDecision,
    ExitReason,
    Instrument,
    InstrumentKind,
    OptionType,
    OrderIntent,
    OrderSide,
    Position,
    PosSide,
    Purpose,
    Trade,
    new_id,
)
from .exec.gateway import LIVE_MODES, Decision, Gateway, LiveCaps, LiveContext, Mode
from .exec.live import LiveExecutor
from .exec.paper import BookSnapshot, PaperMatcher, book_from_quote
from .exec.reconcile import Reconciler
from .exec.resting import (
    LimitPolicy,
    Resting,
    entry_cap,
    entry_limit,
    exit_limit,
    fills,
    option_run_pct,
    race_call,
    touch_fills,
    urgent_stop,
)
from .instrument.catalogue import CatalogueLoader
from .instrument.legs import OPTION_CLUSTER_TOL_PCT, LegPivotLoader, otm_legs
from .instrument.pricing import value_at
from .instrument.select import (
    Quote,
    Selection,
    SelectionPolicy,
    choose_expiry,
    estimate_delta,
    map_levels_to_option,
    select_future,
    select_option,
    strike_candidates,
)
from .instrument.universe import ScripGroup, UniverseBuilder, UniversePolicy
from .ledger.db import Ledger, events
from .market.candles import snap_candles
from .market.fo_bhavcopy import OiDailyStore
from .market.fo_bhavcopy import fetch_day as fetch_fo_bhavcopy
from .market.indices import NIFTY50
from .market.iv import (
    MIN_HISTORY,
    IvHistory,
    atm_iv,
    expected_move_frac,
    ladder_tolerance_pct,
    merge_points,
    regime_for_name,
    seed_points,
    years_to_expiry,
)
from .market.session import (
    IST,
    NSE_EQ_CONTINUOUS_UNTIL,
    TF_SECONDS,
    TradingCalendar,
    bucket_start,
    from_ist,
    in_session,
    is_open,
    ist_day,
    ist_hm,
    ist_naive_to_ts,
    ist_today,
    on_session_grid,
    past_force_flat,
    session_buckets_back,
    session_close_ts,
    session_open_ts,
    spec,
    to_ist,
)
from .market.session import (
    session_phase as session_phase_of,
)
from .market.volatility import (
    INDIA_VIX_SCRIP,
    Regime,
    regime_for_commodity,
    regime_for_equity,
)
from .ops.archive import DailyArchive
from .ops.feed_rate import FeedRate
from .ops.fulltape import FullTape
from .ops.guard import Guards
from .ops.health import Check, HealthMonitor
from .ops.tape import ROLE_EQUITY, ROLE_FUTURE, ROLE_INDEX, ROLE_OPTION, Tape
from .ops.telegram import Telegram
from .risk.costs import CostModel
from .risk.exits import ExitEngine, MarketView, apply_exit
from .risk.exposure import ExposureBook
from .risk.limits import (
    CT_M_LIMITS,
    CT_M_MARKET_AGAINST_MAX,
    CT_X_LIMITS,
    CT_Y_LIMITS,
    FIXED_LOTS_UNDER_INR,
    RT_MCX_LIMITS,
    RT_N_LIMITS,
    RT_X_LIMITS,
    RT_Y_F_LIMITS,
    RT_Y_LIMITS,
    RT_Y_W1_LIMITS,
    RiskLimits,
)
from .risk.sizing import size_position
from .risk.wallet import Wallet
from .strategy.base import Outcome, Signal, published
from .strategy.counter import (
    KEY_LEVELS,
    NO_WALL,
    CounterDecision,
    Leg,
    counter_route,
    fade_refusal,
    flipped_signal,
)
from .strategy.fudkii import Fudkii, FudkiiConfig
from .strategy.fukaa import Fukaa, FukaaConfig, select
from .strategy.keys import ALL_KEYS, INITIAL_INR, SHADOW_BOOKS, SHADOW_OF, StrategyKey
from .strategy.regime_gates import rt_gate_reasons, trigger_verdicts, volume_labels
from .venue.fivepaisa.auth import Authenticator
from .venue.fivepaisa.hub import HubServer, read_hub_config
from .venue.fivepaisa.rest import FivePaisaREST
from .venue.fivepaisa.ws import FivePaisaFeed

log = structlog.get_logger(__name__)

DECISION_TF = "30m"
SELECTION_POLICY = SelectionPolicy()
#: The FUDKII family selects without the ₹5 premium floor (operator, 2026-09-23 evening): the
#: parent picks the contract, the RT twins copy its fill, the CT books fade it — one floor for all
#: of them or none. FUKAA keeps the shared policy. The cheap contracts this admits carry an
#: option stop floored at MIN_STOP_TICKS below entry instead of the one-tick δ projection.
NO_PREMIUM_FLOOR = frozenset({
    StrategyKey.FUDKII, StrategyKey.FUDKII_RT_X, StrategyKey.FUDKII_RT_N, StrategyKey.FUDKII_RT_Y,
    StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y, StrategyKey.FUDKII_RT_Y_W1, StrategyKey.FUDKII_RT_Y_F,
    StrategyKey.FUDKII_CT_M,
})
MIN_STOP_TICKS = 8
#: How long an entry may wait for the chosen strike's own previous-session ladder. Bounded
#: because it sits in front of the order: a slow broker must cost a fraction of a second,
#: not an entry. On a miss the book opens on the percentage arm instead.
LEG_ENSURE_TIMEOUT_S = 2.0
#: A held contract whose quote is older than this, or whose book is past the matcher's age limit,
#: is re-quoted from the broker in the background — the depth socket only speaks when something
#: changes, and a quiet put's perfectly good book ages into "stale" (TATASTEEL, 2026-09-25).
HELD_QUOTE_MAX_AGE_S = 20.0
HELD_QUOTE_POLL_S = 5.0
#: A broker snapshot (V1/MarketFeed) carries no bid or ask; it may CONFIRM a held two-sided quote as
#: current only while the feed has spoken within this many seconds (``_still_stands``) and the
#: broker has no newer trade (``Quote.superseded_by``)
FEED_LIVE_S = 5.0
#: at a trigger, how long the choice may wait for the strikes it could not price yet — a strike
#: subscribed a moment ago has only the broker's snapshot (no bid, no ask) until the feed's first frame
#: (``_choose_option``); each such strike gets STRIKE_GRACE_S of it (the feed answers in ~1 s), and in
#: the session's first OPEN_SETTLE_S the books are still filling, so the choice may wait until then
QUOTE_WAIT_S = 5.0
QUOTE_POLL_S = 0.1
STRIKE_GRACE_S = 2.0
OPEN_SETTLE_S = 60.0
#: the 30m futures slots ending at a trigger that must be real bars: the volume reading's eight and the
#: futures leg's 15-bar ATR (``_fill_fut_gaps``)
FUT_GAP_SLOTS = 15
#: the after-close audit of the day's 30m bars (``_audit_bars``), IST — NSE closes 15:30
BAR_AUDIT_HM = "15:40"
#: a name whose previous session is still 5paisa's provisional 09:15 daily candle is re-asked this
#: often (not once a session: the broker's answer changes during the morning), and the ``zones``
#: health line fails from this IST minute while any is left — it has no levels, so every trigger on
#: it grades F (review, 2026-10-03)
PROVISIONAL_RETRY_S = 600.0
PROVISIONAL_ALARM_HM = "09:20"
#: the 30m backfill reaches back this many trading SESSIONS, not calendar days: FUDKII reads
#: SuperTrend over 120 bars (bars/indicators.py SUPERTREND_CONVERGED_BARS) = 10 NSE sessions of 13,
#: plus two for buckets the broker returns nothing for. Fifteen calendar days held 117 bars on a
#: Monday after a holiday week (review, 2026-10-03); MCX's 29 bars a session are covered too.
BACKFILL_SESSIONS = 12
#: the audit alerts when more bars than this share (or this many) had to be repaired or added
BAR_AUDIT_ALERT_SHARE, BAR_AUDIT_ALERT_MIN = 0.01, 5
#: a feed with no frame for this long in an open session is dead, whatever its socket says
FEED_SILENCE_S = 15.0
#: how long a counter-trend button's plan preview is reused before it is quoted again
COUNTER_PREVIEW_S = 15.0

BOOK_LABELS = {
    "FUDKII": "FUDKII", "FUDKII_RT_X": "RT-X", "FUDKII_RT_N": "RT-N", "FUDKII_RT_Y": "RT-Y",
    "FUDKII_CT_X": "CT-X", "FUDKII_CT_Y": "CT-Y", "FUDKII_RT_MCX": "RT-MCX",
    "FUDKII_RT_Y_W1": "RT-Y wide (shadow)", "FUDKII_RT_Y_F": "RT-Y graded F (shadow)",
    "FUDKII_CT_M": "CT-M market-against fade (shadow)",
}


def _humanise_reason(reason: str) -> str:
    """A selector or sizing reason in words: ``1000:one-sided, 1020:spread-7.2%`` reads as
    ``1000 one-sided, 1020 spread 7.2%``."""
    out = re.sub(r"(\d+(?:\.\d+)?):(one-sided|no-quote|stale-\d+s|spread-|premium-)", r"\1 \2", reason)
    return out.replace("spread-", "spread ").replace("premium-", "premium ").replace("stale-", "stale ").replace("no-quote", "no quote")


#: the trigger label "pivot just ahead" (and gate B's default reach), and how far ahead the context
#: records key levels so a gate can read its own threshold
PIVOT_AHEAD_ATR = 0.5
PIVOT_SCAN_ATR = 2.0


#: a signal written when its limit entry was placed, settled when it fills or is missed
_RESTING_DECISIONS = frozenset({"RESTING", "PARENT_HALTED_TWINS_RESTING", "PARENT_SKIPPED_TWINS_RESTING"})
#: The book part of an order id (operator, 2026-09-26: "cant all strategy names have its code in the
#: order ID so that they all remain unique"). ``ORDER_CODES[book]-YYMMDD-HHMMSS-NNN`` stays inside the
#: 38 characters of an id the broker keeps; the descriptive rest may be cut there without a collision.
ORDER_CODES = {
    "FUDKII": "FII-P", "FUDKII_RT_X": "FII-RTX", "FUDKII_RT_N": "FII-RTN", "FUDKII_RT_Y": "FII-RTY",
    "FUDKII_CT_X": "FII-CTX", "FUDKII_CT_Y": "FII-CTY", "FUDKII_RT_MCX": "FII-RTM", "FUDKII_RT_Y_W1": "FII-RYW", "FUDKII_RT_Y_F": "FII-RYF", "FUDKII_CT_M": "FII-CTM",
    "FUKAA": "FKA",
}
_ORDER_REF_RE = re.compile(r"^([A-Z]{3}(?:-[A-Z]{1,3})?)-(\d{6})-\d{6}-(\d{3,})(?:-|$)")
#: what an exit order is, in its id
EXIT_CODES = {
    ExitReason.SL_EQ: "SLE", ExitReason.SL_OP: "SLO", ExitReason.TARGET: "TG", ExitReason.TRAIL: "TR",
    ExitReason.TIME_STOP: "TS", ExitReason.EOD: "EOD", ExitReason.HALT: "HLT", ExitReason.DAILY_LOSS: "DL",
    ExitReason.MANUAL: "MAN", ExitReason.END: "END",
}


def _contract_tag(inst: Instrument, qty: int) -> str:
    """``HINDUNILVR-1960CE-L4``: the underlying, the contract and the lots — letters and digits only."""
    sym = re.sub(r"[^A-Z0-9]", "", str(inst.underlying or inst.symbol).upper())
    if inst.is_option:
        k = f"{inst.strike:g}".replace(".", "P") + (inst.option_type.value if inst.option_type else "")
    else:
        k = "FUT" if inst.kind is InstrumentKind.FUTURE else "EQ"
    return f"{sym}-{k}-L{int(qty) // max(1, int(inst.lot_size or 1))}"


#: 5paisa V4/Margin fields that carry the money free to trade, in the order they are trusted
LIVE_MARGIN_FIELDS = ("NetAvailableMargin", "AvailableMargin", "ALB")
#: whose fills a book mirrors — its card waits on that book's resting entry. Since 2026-09-26 every
#: book places its own entry; only the shadow opens on another book's fill
_MIRRORS = {"FUDKII_RT_Y_W1": "FUDKII_RT_Y"}
#: the books that take a FUDKII trigger in the trend's way, each judging it for itself
IN_TREND_BOOKS = (StrategyKey.FUDKII, StrategyKey.FUDKII_RT_X, StrategyKey.FUDKII_RT_N, StrategyKey.FUDKII_RT_Y)
#: The last minute any book may place a new NSE entry (operator, 2026-09-29: "3:15PM IST for NSE
#: trading for all NSE trades for all strategies except for MCX ones, FUDKII-RT-Y-F. last entry time
#: for FUDKII-RT-Y-F will be before 3:23PM"). Paper and live alike; MCX keeps its own session. Before
#: this, no paper entry checked the clock: the graded-F shadow bought at 15:30, after the close
#: (2026-09-29, −₹15,116).
NSE_LAST_ENTRY_HM = "15:15"
LAST_ENTRY_HM: dict[str, str] = {StrategyKey.FUDKII_RT_Y_F.value: "15:22"}
#: "exit latest by 3:25PM IST not after for all NSE trades" (operator, 2026-09-29): every book keeps the
#: session's 15:20 flatten — moving it to 15:24 cost −₹29,932 over 24 Aug–28 Sep in the replay (89 trades
#: held to the close, every in-trend book worse) — and the graded-F shadow, whose entries run to 15:22,
#: flattens from 15:24, out by 15:25.
FORCE_FLAT_HM: dict[str, str] = {StrategyKey.FUDKII_RT_Y_F.value: "15:24"}
#: The 15:15 plan (operator, 2026-10-03: "if at 15:15 we are waiting for SL which is very near, we should
#: exit asap, in case the SL is away, then we wait if the target is close by ... by 15:15 you will know if
#: we need to exit at 15:20"): at this minute every open NSE position's distance to its stop line and to
#: its next target is RECORDED with what the rule would do — never acted on yet (30 Sep - 1 Oct: one
#: position of nine had its stop that near, none a target). ``EOD_PLAN_NEAR_PCT`` is "near", % of the mid.
EOD_PLAN_HM = "15:15"
EOD_PLAN_NEAR_PCT = 3.0
#: How long after the NSE open a trigger carried from the last session's close waits for its stock's
#: first print before it expires.
CARRY_WINDOW_S = 300.0
#: the feed probe's segments, from a frame's own Exch + ExchType (``ops/feed_rate.py``)
#: bar close → decision start, p95, above which the latency check fails while a segment is open
DECISION_LATENCY_P95_MAX_S = 15.0
FEED_SEGMENTS: dict[str, str] = {"NC": "NSE_EQ", "ND": "NSE_FO", "MD": "MCX_FO", "NU": "NSE_CDS"}
#: the books that take the counter-trend fade of it (CT-X's plan), each judging it for itself
FADE_BOOKS = (StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y)
#: On an in-trend book's card for a trigger the route sends COUNTER-TREND, the live button is the
#: counter-trend book's: RT-Y's exits fade in CT-Y, everything else's in CT-X, which owns the fade
#: (operator, 2026-09-29: "the CTA has to be the OTM we are to buy in counter-trend")
COUNTER_OF = {
    StrategyKey.FUDKII: StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_RT_X: StrategyKey.FUDKII_CT_X,
    StrategyKey.FUDKII_RT_N: StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_RT_Y: StrategyKey.FUDKII_CT_Y,
}
#: the books offered a trigger FUDKII grades F and does not publish (operator, 2026-09-28): the
#: graded-F shadow alone, until each book's own grade rule is proven (phase19)
UNPUBLISHED_BOOKS = (StrategyKey.FUDKII_RT_Y_F,)


@dataclass(slots=True)
class _EntryPlan:
    """Everything a filled entry needs to become a position — carried by a resting limit order
    until the market comes to it (exec/resting.py)."""

    sig: Signal
    underlying: Instrument
    inst: Instrument
    option_sl: float
    option_targets: tuple[float, ...]
    delta: float
    outlay: float
    wallet: Wallet | None
    book: Any
    book_limits: RiskLimits
    decided_at: float
    ref: float
    #: the book placing this entry (its position's strategy)
    key: str = ""
    #: the signal is this book's own and its ledger row records the decision; False = the book
    #: takes another book's signal (an RT book the FUDKII trigger, CT-Y CT-X's fade) and records
    #: its decision on its card
    owns_row: bool = True
    #: ``FII-RTX-260926-192510-007`` — the entry's id base; the position's exits and targets carry it
    order_ref: str = ""
    #: where the hold went: into the position's cost (booked) or back to the purse (released)
    booked: bool = False
    released: bool = False
    #: the entry's outcome so far ``(decision, reason)`` — what an operator take is told
    outcome: tuple[str, str] = ("", "")


@dataclass
class StrategyContext:
    """The Context a strategy sees. Deliberately the smallest possible surface."""

    engine: Engine
    _state: dict[str, Any] = field(default_factory=dict)

    def bars(self, symbol: str, tf: str, n: int) -> Sequence[UnifiedBar]:
        return self.engine.store.bars(symbol, tf, n)

    def volume_reading(self, symbol: str, ts: int) -> VolumeReading:
        return self.engine._volume_reading(symbol, ts)

    def market_volume_surge(self, ts: int) -> tuple[float | None, int]:
        return self.engine.n50_volume_surge(ts)

    def oi_reading(self, symbol: str) -> OiReading:
        return self.engine.oi_reading(symbol)

    def oi_relative(self, symbol: str) -> tuple[float | None, int]:
        return self.engine.oi_relative(symbol)

    def zones(self, symbol: str) -> list[Zone]:
        return self.engine.zones_for(symbol)

    def exchange(self, symbol: str) -> str:
        inst = self.engine.underlyings.get(symbol)
        return inst.exch if inst else "N"

    def session_phase(self, symbol: str, ts: int) -> str:
        return self.engine.session_phase(symbol, ts)

    @property
    def state(self) -> MutableMapping[str, Any]:
        return self._state


class AsOfContext:
    """A strategy's Context with every series cut at one past bar — how the boot replay asks what
    a strategy would have said at a boundary the process was not running for. Reads only the store
    and the day's zones, which is all FUDKII reads, so nothing after ``until`` can leak in."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.until = 0
        self._state: dict[str, Any] = {}
        self._series: dict[tuple[str, str], tuple[list[UnifiedBar], list[int]]] = {}

    def upto(self, symbol: str, tf: str, n: int | None = None) -> list[UnifiedBar]:
        key = (symbol, tf)
        hit = self._series.get(key)
        if hit is None:
            rows = self.engine.store.bars(symbol, tf)
            hit = self._series[key] = (rows, [b.ts for b in rows])
        rows, stamps = hit
        cut = bisect_right(stamps, self.until)
        return rows[max(0, cut - n):cut] if n else rows[:cut]

    def bars(self, symbol: str, tf: str, n: int) -> Sequence[UnifiedBar]:
        return self.upto(symbol, tf, n)

    def zones(self, symbol: str) -> list[Zone]:
        return self.engine.zones_for(symbol)

    def exchange(self, symbol: str) -> str:
        inst = self.engine.underlyings.get(symbol)
        return inst.exch if inst else "N"

    def session_phase(self, symbol: str, ts: int) -> str:
        return self.engine.session_phase(symbol, ts)

    # The four readings FUKAA asks for (strategy/base.py Context). Only FUDKII replays through here
    # today, but a context missing a method is how the backtester failed every symbol for a day
    # (review, 2026-10-03). The volume reading is read off the store's slots up to the bar, which is
    # point-in-time; OI is not — the engine holds only its latest levels — so it answers "unknown".
    def volume_reading(self, symbol: str, ts: int) -> VolumeReading:
        return self.engine._volume_reading(symbol, ts, self.upto(symbol, DECISION_TF))

    def market_volume_surge(self, ts: int) -> tuple[float | None, int]:
        return None, 0

    def oi_reading(self, symbol: str) -> OiReading:
        return OiReading(doubt="no point-in-time OI in a replay")

    def oi_relative(self, symbol: str) -> tuple[float | None, int]:
        return None, 0

    @property
    def state(self) -> MutableMapping[str, Any]:
        return self._state


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.bus = Bus()
        self.store = BarStore()
        self.ledger = Ledger(settings.db_url)
        self.costs = CostModel(settings)
        #: the parent's limits: 4 lots a trade, the cap every book carries for itself (operator,
        #: 2026-09-26: "how can we ever buy 30 lots of the same trade?" — uncapped, it bought up to 83
        #: in the Sep replay and 80 live). Its TARGETS stay its own — the equity levels projected
        #: through delta: "the parents' targets come from that parent's own logic and strategy and
        #: not borrowed or adopted from its variants or twins"
        # 4 lots under ₹75,000, stepping further OTM when they cost more (operator, 2026-09-27)
        self.limits = RiskLimits(max_lots=4, fixed_lots_under_inr=FIXED_LOTS_UNDER_INR)
        self.exits = ExitEngine(self.limits)
        # FUDKII_RT_X trades FUDKII's entries under a different exit policy, so it gets its own
        # engine rather than a flag inside the shared one — the two must never be able to drift
        # into each other, and a second RiskLimits makes that structural.
        self.exits_rt = ExitEngine(RT_X_LIMITS)
        # Three RT exit policies twinned off the same FUDKII fills (docs/PIVOTS.md §6): X is the
        # touch/sustain ladder with a single 3 % line, N the immediate-arming 2 % dwell book that
        # ran on 2026-09-23, Y the third vertical. MCX rides X's policy in its own purse.
        self._exits_by_strategy = {
            StrategyKey.FUDKII_RT_X.value: self.exits_rt,
            # its own limits: the NSE books' fixed 4 lots under ₹75,000 is not for MCX
            StrategyKey.FUDKII_RT_MCX.value: ExitEngine(RT_MCX_LIMITS),
            StrategyKey.FUDKII_RT_N.value: ExitEngine(RT_N_LIMITS),
            StrategyKey.FUDKII_RT_Y.value: ExitEngine(RT_Y_LIMITS),
            StrategyKey.FUDKII_CT_X.value: ExitEngine(CT_X_LIMITS),
            StrategyKey.FUDKII_CT_Y.value: ExitEngine(CT_Y_LIMITS),
            # the wide-stop shadow: RT-Y's policy with the equity stop 1 % further out
            StrategyKey.FUDKII_RT_Y_W1.value: ExitEngine(RT_Y_W1_LIMITS),
            # the graded-F shadow: RT-Y's policy, 25 % cap included, on the triggers RT-Y never sees
            StrategyKey.FUDKII_RT_Y_F.value: ExitEngine(RT_Y_F_LIMITS),
            StrategyKey.FUDKII_CT_M.value: ExitEngine(CT_M_LIMITS),
        }
        #: Each twin is checked against its own pool — 30 slots, its own lot cap — rather than
        #: skipping the check entirely, which is what it did when first written.
        self.exposure_rt = ExposureBook(RT_X_LIMITS)
        self._exposure_by_strategy = {k: ExposureBook(e.limits) for k, e in self._exits_by_strategy.items()}
        #: Published by the exit loop each tick, read by the API. One computation, so the card
        #: and the decision can never disagree.
        self.position_marks: dict[str, dict[str, Any]] = {}
        self.exposure = ExposureBook(self.limits)
        self.calendar = TradingCalendar.from_file(settings.data_dir / "holidays.txt")
        self.telegram = Telegram(
            settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else None,
            settings.telegram_chat_id,
        )
        self.health = HealthMonitor()
        self.committee = CommitteeService(self, settings, decision_tf=DECISION_TF)
        self.archive = DailyArchive(
            settings.data_dir / "archive",
            enabled=settings.archive_enabled,
            keep_sessions=settings.archive_keep_sessions,
            keep_held_sessions=settings.archive_keep_held_sessions,
            keep_research_sessions=settings.archive_keep_research_sessions,
        )
        #: the tick tape: every held, considered and carded contract and its legs, once a second
        self.tape = Tape(self.archive, enabled=settings.tape_enabled, legs_for=self._tape_legs)
        #: the full tape: every NSE code the feed quotes, once a second on change (ops/fulltape.py)
        self._fulltape_skip_cache: dict[str, bool] = {}
        self.fulltape = FullTape(
            settings.data_dir / "archive" / "tape_full", enabled=settings.tape_full_enabled,
            keep_days=settings.tape_full_keep_days, skip=self._fulltape_skip,
        )
        #: scrip codes currently on the depth channel because something needs their book. The
        #: archive sample is subscribed at boot and never reconciled away.
        self._depth_following: set[str] = set()
        self._depth_pinned: set[str] = set()
        self.depth_syncs = 0
        self.depth_adds = 0
        self.depth_drops = 0
        self._tape_legs_cache: dict[str, tuple[str, list[tuple[str, str]]]] = {}
        self._autopilot_day = ""

        self.http = httpx.AsyncClient(timeout=30)
        self.auth = Authenticator(settings, self.http)
        self.rest = FivePaisaREST(settings, self.http, self.auth)
        self.catalogue_loader = CatalogueLoader(settings, self.rest.scrip_master_csv)
        #: One daily pivot ladder per traded leg — the front future and the OTM strikes — as
        #: distinct from the underlying's own. A premium sitting on its own S1 is a different
        #: proposition from one in mid-air, whatever the equity is doing.
        self.leg_pivots = LegPivotLoader(self.rest)
        self.feed = FivePaisaFeed(
            settings,
            self.auth,
            on_tick=self._on_tick,
            on_depth=self._on_depth,
            on_oi=self._on_oi,
            on_connect=self._on_feed_connect,
        )
        #: the feed hub this engine serves to its twin (venue/fivepaisa/hub.py), when it does
        self.feed_hub: HubServer | None = None
        self.aggregator = Aggregator(self.store, on_bar_close=self._on_bar_close)
        #: Advisory books — they read the same closed bars the decision path does and emit
        #: alerts only. Deliberately outside the gateway: nothing here can place an order.
        self.alerts = AlertEngine(self)
        self._segment_by_code: dict[str, Segment] = {}
        self.micro = MicroAggregator(
            tf_seconds=TF_SECONDS[DECISION_TF],
            bucket_of=lambda code, ts: bucket_start(
                self._segment_by_code.get(code, Segment.NSE_EQ), ts, DECISION_TF
            ),
        )
        self.reconciler = BarReconciler(self.rest, self.store, lambda sym: self.underlyings.get(sym))
        self.groups: dict[str, ScripGroup] = {}
        self.universe_builder: UniverseBuilder | None = None
        self.option_oi: dict[str, dict[str, float]] = {}
        #: traded volume per scrip code, from the snapshot the selector already fetches. Kept
        #: because the strike chooser ranks on volume as well as open interest.
        self.option_volume: dict[str, float] = {}
        #: every option strike the universe selected — the tick path writes their volume from the feed
        self._option_codes: set[str] = set()
        self._decision_tasks: set[asyncio.Task[Any]] = set()
        #: the broker's time of each code's last print (the feed frame's TickDt, or a REST row's): a
        #: carried trigger enters on its stock's FIRST print TRADED this session — yesterday's close
        #: replayed at a subscribe after the open arrives now but traded yesterday (review, 2026-10-03:
        #: this held the receive time, and a REST row wrote the broker's into the same map)
        self._ltp_traded_ts: dict[str, float] = {}
        #: the triggers carried from the last session's close still waiting for their first print
        self._carry_day: date | None = None
        self._carry_pending: list[dict[str, Any]] = []
        self._sweep_task: asyncio.Task[Any] | None = None
        self._intraday_rebuild_day: str = ""
        #: PAPER limit orders (exec/resting.py) and the ones working right now, by client order id
        self.limit_policy = LimitPolicy(
            enabled=settings.paper_limit_orders,
            entry_wait_s=settings.paper_limit_entry_wait_s,
            entry_chase_pct=settings.paper_limit_entry_chase_pct,
            entry_recheck_s=settings.paper_limit_entry_recheck_s,
            entry_hold_s=settings.paper_limit_entry_hold_s,
            entry_cap_pct=settings.paper_limit_entry_cap_pct,
            entry_race_check_s=settings.paper_limit_entry_race_check_s,
            entry_race_pct=settings.paper_limit_entry_race_pct,
            entry_cross_after_hold=settings.paper_limit_entry_cross_after_hold,
            exit_reprice_s=settings.paper_limit_exit_reprice_s,
            exit_cross_stop_s=settings.paper_limit_exit_cross_stop_s,
            exit_cross_urgent_s=settings.paper_limit_exit_cross_urgent_s,
            exit_cross_other_s=settings.paper_limit_exit_cross_other_s,
            rest_targets=settings.paper_limit_rest_targets,
            exit_urgent_stops=settings.paper_limit_exit_urgent_stops,
            exit_fast_fall_pct=settings.paper_limit_exit_fast_fall_pct,
            exit_fast_window_s=settings.paper_limit_exit_fast_window_s,
            exit_tight_ticks=settings.paper_limit_exit_tight_ticks,
        )
        self._resting: dict[str, Resting] = {}
        #: positions whose target ladder is being synced now (a fill inside the sync re-enters it)
        self._target_sync: set[str] = set()
        #: each held option's mid, a read a second for the last two minutes — the urgent-stop rule's "falling fast"
        self._mid_hist: dict[str, deque[tuple[float, float]]] = {}
        #: the IST date the 15:15 plan was last recorded for
        self._eod_plan_day = ""
        #: book → (drift, first seen) of a ``deployed`` that disagrees with the book's open positions
        #: and resting entries; corrected on the second sighting (``_reconcile_deployed``)
        self._deployed_drift: dict[str, tuple[float, float]] = {}
        self._deployed_fix: dict[str, dict[str, float]] = {}
        self._margin_cache: tuple[float, dict[str, Any]] = (0.0, {})
        #: "signal|books" already handled this session — a re-run is never entered twice
        self._handled_signals: set[str] = set()
        #: (book, YYMMDD) → the book's last order number that day (``_order_ref``)
        self._order_seq: dict[tuple[str, str], int] = {}
        #: client_order_id → (wallet, hold) of an entry at the broker, neither resting nor a position yet
        self._inflight_holds: dict[str, tuple[Wallet, float]] = {}
        #: (book, symbol) of an entry being decided or placed — neither resting nor a position yet
        self._entering: set[tuple[str, str]] = set()
        self.matcher = PaperMatcher(
            self.costs,
            max_book_age_ms=settings.paper_max_book_age_ms,
            open_max_book_age_ms=settings.paper_open_max_book_age_ms,
            open_window_ist=(settings.paper_open_window_from_ist, settings.paper_open_window_to_ist),
        )
        self.live_exec: LiveExecutor | None = None
        self.reconciler_positions: Reconciler | None = None
        self.reconciler_ready = False
        self.gateway = Gateway(
            matcher=self.matcher,
            mode=self.mode,
            halted=self.halted,
            book_for=self.book_for,
            ltp_for=self.ltp_for,
            caps=LiveCaps(
                segments=tuple(s.value for s in settings.live_segment_list),
                max_notional_inr=settings.live_max_qty_rupees,
                max_positions=settings.live_max_positions,
                max_orders_per_day=settings.live_max_orders_per_day,
                daily_loss_inr=settings.live_daily_loss_inr,
                entry_cutoff_ist=settings.live_entry_cutoff_ist,
                breaker_consecutive_rejects=settings.live_breaker_consecutive_rejects,
            ),
        )

        self.fudkii = Fudkii(FudkiiConfig())
        self.fukaa = Fukaa(FukaaConfig())
        self.contexts: dict[StrategyKey, StrategyContext] = {
            k: StrategyContext(self) for k in ALL_KEYS
        }

        self.wallets: dict[str, Wallet] = {}
        self.positions: dict[str, Position] = {}
        self.underlyings: dict[str, Instrument] = {}
        self.books: dict[str, BookSnapshot] = {}
        self.quotes: dict[str, Quote] = {}
        self.ltps: dict[str, float] = {}
        self._zone_cache: dict[str, tuple[str, ZoneBuild]] = {}
        #: names with no zones today and why ("history", "provisional", "basis") — bars/zones.py
        self.zone_refusals: dict[str, str] = {}
        #: sessions whose daily and 30m series are on different price bases (a corporate action):
        #: symbol -> (day, official close, 30m close) — the 15:15 auction bar was withheld for them;
        #: shown on the zones health line, cleared when the series heals and at the day roll
        self._basis_mismatch: dict[str, tuple[str, float, float]] = {}
        #: the front future's candles per symbol for the current trigger bar (see _fut_context)
        self._fut_cache: dict[str, tuple[int, dict[str, Any] | None]] = {}
        #: each front future's daily candles, fetched once a day: (scrip code, day) -> rows
        self._fut_daily_rows: dict[tuple[str, date], list[dict[str, Any]]] = {}
        #: (name, bar) -> the futures fetch in flight, shared by its concurrent readers
        self._fut_inflight: dict[tuple[str, int], asyncio.Future[dict[str, Any] | None]] = {}
        #: the choice's wait for strikes it cannot price yet (0 = never wait: the replay's frozen clock)
        self.quote_wait_s = QUOTE_WAIT_S
        #: snapshot rows with no bid/ask that confirmed / kept a held quote, and confirmed a held book
        self.snapshot_confirmed = self.snapshot_kept = self.snapshot_book_confirmed = 0
        #: snapshot rows that marked an open position's contract from the broker's last price
        self.snapshot_held_marked = 0
        #: code -> when a snapshot's last trade contradicted the held quote: that quote is known old,
        #: and the choice waits for a newer one (``_choose_option``)
        self._quote_outdated: dict[str, float] = {}
        #: the market-wide volume check, per 30m bar (``_market_volume``)
        self._vol_market: dict[int, MarketVolume] = {}
        #: the after-close bar audit (``_audit_bars``): the day it last ran, its task, its result
        self._bar_audit_day = ""
        self._bar_audit_task: asyncio.Task[None] | None = None
        self._bar_audit: dict[str, Any] | None = None
        #: every signal handled today, by id — what an operator take re-enters from
        self._signals_today: dict[str, Signal] = {}
        #: market breadth measured at each trigger, by signal id — the RT-Y gate reads the number the
        #: trigger was logged with, not a later one
        self._breadth_at: dict[str, dict[str, Any]] = {}
        #: triggers CT-Y faded on the 09:45 gap rule — their CT-X fade is not mirrored into CT-Y
        self._gap_faded: set[str] = set()
        #: positions with an exit order in flight: a second exit for the same position (the
        #: operator's SKIP racing the exit loop) must not send a second order
        self._exits_in_flight: set[str] = set()
        # -- the FUDKII rescan: every signal of the day, whether or not the live path saw its bar --
        #: (symbol, bar ts) the live decision path actually decided — the rescan never second-guesses these
        self._decided: set[tuple[str, int]] = set()
        #: decision-frame buckets whose close has been handed to a decision (see _on_bar_close)
        self._closes_seen: set[tuple[str, int]] = set()
        self._duplicate_closes = 0
        self._silence_reconnects = 0
        self._last_silence_reconnect = 0.0
        #: (symbol, bar ts) the rescan has evaluated on a confirmed bar
        self._fudkii_scanned: set[tuple[str, int]] = set()
        #: exchange-confirmation attempts per unconfirmed bar, so a bucket REST never serves is not
        #: asked for every five minutes all afternoon
        self._confirm_attempts: dict[tuple[str, int], int] = {}
        self._decided_saved = 0
        #: per position: definitive exit rejections so far, and when the next attempt may go.
        #: A rejected exit used to keep its client_order_id, so every retry was refused as a
        #: duplicate and the position could never close (TATASTEEL RT-X/RT-N, 2026-09-25 14:03).
        self._exit_attempts: dict[str, int] = {}
        self._exit_retry_at: dict[str, float] = {}
        self._exit_quote_ts: dict[str, float] = {}
        self._fudkii_scan_lock = asyncio.Lock()
        self._fudkii_scan_task: asyncio.Task[Any] | None = None
        self.last_fudkii_scan: dict[str, Any] = {}
        #: when this process started deciding live; a bar that closed before it was never seen
        self._live_from_ts = float("inf")
        #: every symbol decides in its own task, so a 09:45 burst of sixteen signals would be
        #: thirty-two concurrent historical calls; the broker client has no limiter of its own
        self._fut_sem = asyncio.Semaphore(4)
        #: the futures reads of triggers FUDKII did not publish (the graded-F shadow's dried-volume gate)
        self._fut_sem_low = asyncio.Semaphore(2)
        # -- the pivot data plane (docs/PIVOTS.md) --
        self.daily_cache = DailyCache(settings.data_dir / "daily")
        self._daily_failed: set[str] = set()  # 1d fetch raised; the repair loop retries every pass
        self._daily_confirmed: dict[str, date] = {}  # asked once for this expected session already
        self._daily_provisional_asked: dict[str, float] = {}  # a provisional name, when last re-asked
        self._daily_due = False  # a full refetch is owed: day roll or a refresh slot
        self._legs_due = False  # a full leg reload is owed: day roll
        self._legs_reanchor_due = False  # re-band the OTM legs on the session's real spot
        self._legs_reanchor_done: set[str] = set()
        #: (counter book, trigger) -> (when, plan): the counter-trend button's preview (``_counter_cta``)
        self._counter_preview: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._daily_refresh_done: set[str] = set()  # "YYYY-MM-DD HH:MM" slots already run
        #: the alert-ring reset slots already run, same shape. Seeded here, at construction, and
        #: not in the universe path: a process that boots after the slot has nothing to clear, and
        #: seeding it anywhere that a boot can skip means the first housekeeping tick wipes the
        #: session's own signals.
        self._alerts_reset_done: set[str] = (
            {f"{ist_today().isoformat()} {settings.alerts_reset_ist}"}
            if ist_hm(time.time()) >= settings.alerts_reset_ist
            else set()
        )
        self._pivot_repair_task: asyncio.Task[Any] | None = None
        self._last_pivot_repair = 0.0
        # -- each name's own implied vol (docs/PIVOTS.md §6): its VIX, for the option ladder --
        self.iv_history = IvHistory(settings.data_dir / "iv")
        self.stock_iv: dict[str, tuple[float, float]] = {}  # symbol -> (ATM IV, ts)
        self._last_iv_refresh = 0.0
        self._future_to_underlying: dict[str, str] = {}
        #: each future's latest OI level (code -> (OI, when)) and the previous session's closing OI —
        #: what an OI change is computed from (``bars/oi_read.py``); the broker's own change field
        #: is 0.0 on every frame
        self._fut_oi: dict[str, tuple[float, float]] = {}
        self._oi_ref: dict[str, float] = {}
        self._oi_ref_day: date | None = None
        #: where each reference came from — "nse" (the exchange's bhavcopy), "archive" (our own last
        #: print of that session) or "preopen" (today's first print before the open)
        self._oi_ref_src: dict[str, str] = {}
        #: are 5paisa's own OI change fields ever non-zero? The percent never was (824,031 frames on
        #: 1 Oct); nothing had ever looked at the absolute one. Counted, so the engine answers it.
        self.oi_frames = {"frames": 0, "change_nonzero": 0, "change_pct_nonzero": 0}
        #: the exchange's closing OI per session (``market/fo_bhavcopy.py``) and the session whose
        #: file the references were last taken from
        self.oi_daily = OiDailyStore(settings.data_dir / "oi_daily")
        self._oi_bhav_day: date | None = None
        self._oi_bhav_tried = 0.0
        self._oi_bhav_task: asyncio.Task[Any] | None = None
        #: each future's OI as candles (``bars/oi_candles.py``), from the prints — never from the
        #: broker's change field
        self.oi_candles = OiCandleBuilder()
        #: how fast the price feed really is, and whether it delivers every trade (``ops/feed_rate.py``)
        self.feed_rate = FeedRate()
        #: per-duty error accounting for the advisory loops (ops/guard.py) — /api/health duty_errors
        self.guards = Guards()
        #: bar close → the decision starting, seconds, the last few hundred 30m decisions
        self._decision_lat: deque[float] = deque(maxlen=300)
        self._front_code: dict[tuple[str, date], str | None] = {}
        self._n50_oi: tuple[int, dict[str, float]] = (-1, {})
        self._n50_vol: dict[int, tuple[float | None, int]] = {}
        self._stale_positions: set[str] = set()
        self._shadow_exits: set[str] = set()
        self._mode = Mode.SHADOW
        self._armed_until: float | None = None
        self._halted = False
        self._halt_reason = ""
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self.started_ts = time.time()
        self.boot_notes: list[str] = []
        #: true until the market boot (catalogue, backfill, feed) has finished. The web server
        #: opens before it, so the pages answer during the ~2.5 minutes the backfill takes.
        self.booting = True

    # -- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        await self.start_core()
        await self.start_market()

    async def start_core(self) -> None:
        """The fast half: the ledger, the books, and the session's alerts as they were before the
        restart. Everything the pages need to answer, in well under a second."""
        await self.ledger.init()
        await self._load_control()
        await self._load_wallets()
        await self._load_positions()
        await self._load_order_seq()
        await self._load_breakers()
        # the wallets' own records made true before anything reads them: a new day rolled over
        # (the process may have been down at midnight), both breakers re-read, deployed money
        # matched to the positions actually open
        await self._wallet_upkeep(time.time(), boot=True)
        for cid in await self.ledger.known_client_order_ids():
            self.gateway.remember(cid)
        self.alerts.store_dir = self.s.data_dir / "alerts"
        self.alerts.restore(self._last_alerts_reset_ts())
        self._load_decided()

    async def start_market(self) -> None:
        """The slow half: the catalogue, the backfill, the replay of today's closed bars into the
        alert books, and the feed. The web server is already up while this runs."""

        warn = self.calendar.missing_holidays_warning()
        if warn:
            self.boot_notes.append(warn)
            log.warning("calendar.no_holidays", detail=warn)
        ignored = sorted(k for k in self.s.model_fields_set if k.startswith("cost_"))
        if ignored:
            # Kept in Settings only so an old .env still boots; nothing reads them (review,
            # 2026-10-03). A value set there and believed is how ₹40 vs ₹20 went unnoticed.
            note = (f"ignored: {', '.join('KN_' + k.upper() for k in ignored)} — every charge comes from "
                    f"{self.s.data_dir / 'charges.toml'} (the /charges page)")
            self.boot_notes.append(note)
            log.warning("config.cost_keys_ignored", keys=ignored)

        if not self.s.has_credentials:
            self.boot_notes.append(
                "no 5paisa credentials — the engine is API-only. 5paisa has no anonymous feed, so "
                "even PAPER needs a session. Set KN_FP_* in backend/.env."
            )
            log.warning("engine.no_credentials")
        elif self.s.engine_enabled:
            await self._wait_for_broker()
            await self._boot_market()
            await self._catch_up_alerts()

        self._banner()
        self._tasks = [
            asyncio.create_task(self._clock(), name="clock"),
            asyncio.create_task(self._housekeeping(), name="housekeeping"),
        ]
        if self.s.engine_enabled and self.s.feed_enabled and self.s.has_credentials:
            # one broker socket for two engines (data/engine.json "feed_hub", 2026-10-04): serve it to
            # the twin, or read the twin's instead of opening one
            hub = read_hub_config(self.s.data_dir)
            if hub.connect is not None:
                self.feed.hub = hub.connect
            elif hub.serve is not None:
                try:
                    self.feed_hub = HubServer(self.feed, *hub.serve)
                    await self.feed_hub.start()
                except OSError as exc:  # the trading engine never waits on its twin's plumbing
                    self.feed_hub = None
                    log.error("feed_hub.serve_failed", address=f"{hub.serve[0]}:{hub.serve[1]}", error=str(exc)[:160])
            self._tasks.append(asyncio.create_task(self.feed.run(), name="feed"))
            self._tasks.append(asyncio.create_task(self._keep_held_quotes(), name="held-quotes"))
            self._live_from_ts = time.time()
        self.booting = False

    async def _wait_for_broker(self, first_delay_s: float = 15.0, max_delay_s: float = 300.0) -> None:
        """Hold the market boot until the broker answers a login, retrying with backoff.

        The boot needs the broker for everything after the catalogue: the backfill, the position
        reconcile, the feed. 2026-09-25 11:30: macOS cached a failed DNS lookup for
        openapi.5paisa.com, every backfill call failed at once, the reconcile's login raised, and
        the process exited — taking the pages with it. The web server is up before this runs, so
        waiting here keeps the alerts page and the day book serving while the network recovers,
        and the boot then continues on its own. A login refused in 5paisa's midnight window waits
        the same way instead of exiting."""
        delay = first_delay_s
        while True:
            try:
                await self.auth.token()
                if delay != first_delay_s:
                    log.info("engine.broker_reachable")
                    self.boot_notes.append("broker reachable again — market boot resumed")
                return
            except Exception as exc:  # noqa: BLE001 - any failure here is "not yet", never fatal
                log.warning("engine.waiting_for_broker", error=str(exc)[:160], retry_in_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay_s)

    def _last_alerts_reset_ts(self) -> float:
        """The most recent 00:30 IST reset at or before now. A session saved before it belongs to
        a day the page has already been emptied for."""
        now = datetime.now(IST)
        hh, mm = (int(x) for x in self.s.alerts_reset_ist.split(":"))
        cut = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < cut:
            cut -= timedelta(days=1)
        return cut.timestamp()

    async def _catch_up_alerts(self) -> None:
        """Replay today's closed decision bars into the alert books and FUDKII, so a restart — or
        a process that was not running at a boundary — does not leave the page blank. Advisory:
        nothing here reaches the gateway, and a replayed FUDKII trigger says it was not traded."""
        try:
            tf_s = TF_SECONDS[DECISION_TF]
            now = time.time()
            today = ist_today()
            bars = [
                b
                for sym in self.underlyings
                for b in self.store.bars(sym, DECISION_TF)
                if ist_day(b.ts) == today and b.ts + tf_s <= now
            ]
            bars.sort(key=lambda b: (b.ts, b.symbol))
            if not bars:
                return
            start = datetime(today.year, today.month, today.day, tzinfo=IST).timestamp()
            known = {
                str(r.get("signal_id") or "")
                for r in await self.ledger.rows_between("signals", start, start + 86_400)
            }
            ctx = AsOfContext(self)
            replay = Fudkii(self.fudkii.cfg)

            def history_of(bar: UnifiedBar) -> list[UnifiedBar]:
                ctx.until = bar.ts
                return ctx.upto(bar.symbol, bar.tf, ALERT_LOOKBACK)

            def fudkii_signals(bar: UnifiedBar) -> list[Signal]:
                ctx.until = bar.ts
                return list(replay.on_bar(ctx, bar).signals)

            began = time.time()
            # FUDKII is left to the rescan below, which also writes the ledger the book tabs read
            got = await self.alerts.catch_up(bars, history_of=history_of, fudkii=None, known_signal_ids=known)
            log.info("alerts.catch_up_done", took_s=round(time.time() - began, 1), **got)
        except Exception as exc:  # noqa: BLE001 - advisory; the engine boots regardless
            log.warning("alerts.catch_up_failed", error=str(exc)[:200])
        try:
            await self.scan_fudkii()
        except Exception as exc:  # noqa: BLE001
            log.warning("fudkii.scan_failed", error=str(exc)[:200])

    async def scan_fudkii(self, *, max_confirm_attempts: int = 3) -> dict[str, Any]:
        """Every FUDKII signal of the day, checked on the exchange's own 30m candles.

        The live path decides each bar as it closes. It never sees a bar that closed while the
        process was down or still booting, and it skips one the exchange had not confirmed in time
        (a partial bar after a restart). This finds both: FUDKII is a pure function of the bars up
        to its bar, the day's zones and the session phase, so running it on history cut at each
        such bar is exactly what it would have said. Unconfirmed bars are fetched from the broker
        first. Bars the live path decided are left alone — its decision stands.

        A signal found here that the ledger does not hold is recorded with a MISSED_* decision and
        its reason, stamped at its bar's close, so the book tabs, the day book and the alerts page
        all list it — and all say it was not traded. Nothing here reaches the gateway.
        """
        async with self._fudkii_scan_lock:
            began = time.time()
            tf_s = TF_SECONDS[DECISION_TF]
            today = ist_today()
            start = datetime(today.year, today.month, today.day, tzinfo=IST).timestamp()
            todo = [
                b
                for sym in self.underlyings
                for b in self.store.bars(sym, DECISION_TF)
                if ist_day(b.ts) == today
                and b.ts + tf_s <= began
                and (sym, b.ts) not in self._decided
                and (sym, b.ts) not in self._fudkii_scanned
            ]
            todo.sort(key=lambda b: (b.ts, b.symbol))
            confirmed = unconfirmed = 0
            for b in todo:
                if b.source is not BarSource.PARTIAL:
                    continue
                key = (b.symbol, b.ts)
                n = self._confirm_attempts.get(key, 0)
                if n >= max_confirm_attempts or not (self.reconciler_ready and self.s.has_credentials):
                    continue
                self._confirm_attempts[key] = n + 1
                chk = await self.reconciler.reconcile_bar(b, timeout_s=self.s.decision_reconcile_timeout_s)
                confirmed += 1 if chk.found else 0
            ledger = {r["signal_id"]: r for r in await self.ledger.rows_between("signals", start, start + 86_400)}
            ctx = AsOfContext(self)
            replay = Fudkii(self.fudkii.cfg)
            found: list[tuple[Signal, UnifiedBar, list[UnifiedBar]]] = []
            for i, b in enumerate(todo):
                ctx.until = b.ts
                cur = next((x for x in reversed(ctx.upto(b.symbol, DECISION_TF, 3)) if x.ts == b.ts), b)
                if cur.source is BarSource.PARTIAL:
                    unconfirmed += 1  # not marked scanned: the next pass asks the exchange again
                    continue
                self._fudkii_scanned.add((b.symbol, b.ts))
                try:
                    for sig in replay.on_bar(ctx, cur).signals:
                        found.append((sig, cur, ctx.upto(b.symbol, DECISION_TF, ALERT_LOOKBACK)))
                except Exception as exc:  # noqa: BLE001 - one name must not cost the rest
                    log.warning("fudkii.scan_bar_failed", symbol=b.symbol, ts=b.ts, error=str(exc)[:160])
                if i % 100 == 99:
                    await asyncio.sleep(0)  # the pages keep answering while the day is scanned
            recorded = restored = 0
            for sig, bar, history in found:
                sj = sig.to_json()
                closed = float(bar.ts + tf_s)
                row = ledger.get(sig.signal_id)
                if row is None:
                    if closed < self._live_from_ts:
                        code, why = (
                            "MISSED_ENGINE_DOWN",
                            "not traded — the engine was not running, or still starting, when this bar "
                            "closed; found by the rescan of the exchange's 30m candles",
                        )
                    else:
                        code, why = (
                            "MISSED_UNCONFIRMED_BAR",
                            "not traded — the live decision was skipped because the exchange had not "
                            "confirmed this bar in time; found by the rescan once it had",
                        )
                    await self.ledger.insert_signal(sj, code, why, created_ts=closed)
                    ledger[sig.signal_id] = {"decision": code, "decision_reason": why}
                    recorded += 1
                    log.info("fudkii.rescan_found", symbol=sig.symbol, ts=sig.ts, direction=sig.direction.value, decision=code)
                    skipped: str | None = why
                else:
                    dec = str(row.get("decision") or "")
                    skipped = str(row.get("decision_reason") or "") if dec.startswith("MISSED") else None
                if self.alerts.adopt_rebuilt(sj, bar, history, skipped=skipped):
                    restored += 1
            self.last_fudkii_scan = {
                "ts": time.time(),
                "took_s": round(time.time() - began, 2),
                "bars_scanned": len(todo) - unconfirmed,
                "bars_unconfirmed": unconfirmed,
                "bars_confirmed_now": confirmed,
                "signals_found": len(found),
                "recorded_as_missed": recorded,
                "rows_added_to_alerts": restored,
            }
            log.info("fudkii.rescan", **self.last_fudkii_scan)
            return self.last_fudkii_scan

    def _decided_path(self) -> Path:
        return self.s.data_dir / "decided.json"

    def _load_decided(self) -> None:
        """The bars the live path decided earlier today, so a restart's rescan does not re-judge a
        bar a previous run already decided (and call its answer 'missed')."""
        try:
            d = json.loads(self._decided_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if d.get("day") == ist_today().isoformat():
            self._decided |= {(str(k[0]), int(k[1])) for k in d.get("keys") or []}
            self._decided_saved = len(self._decided)

    def _save_decided(self, keys: list[tuple[str, int]] | None = None) -> None:
        """Persist today's decided buckets. ``keys`` is a copy taken ON the event loop when this runs
        in a worker thread: iterating the live set there raced ``_decide`` adding to it ("set changed
        size during iteration"), and the one try around housekeeping then skipped every duty after
        it (review, 2026-10-03)."""
        snapshot = list(self._decided) if keys is None else keys
        if len(snapshot) == self._decided_saved:
            return
        path = self._decided_path()
        tmp = path.with_suffix(".tmp")
        today = ist_today()
        keep = sorted(k for k in snapshot if ist_day(k[1]) == today)
        tmp.write_text(json.dumps({"day": today.isoformat(), "keys": keep}), encoding="utf-8")
        os.replace(tmp, path)
        self._decided_saved = len(snapshot)

    async def fudkii_today(self) -> list[dict[str, Any]]:
        """Today's FUDKII signals from 09:00 IST, one row each, as the ledger holds them."""
        today = ist_today()
        start = datetime(today.year, today.month, today.day, tzinfo=IST).timestamp()
        latest: dict[str, dict[str, Any]] = {}
        for r in await self.ledger.rows_between("signals", start, start + 86_400):
            if r.get("strategy") == StrategyKey.FUDKII.value:
                latest[r["signal_id"]] = r
        out = []
        for r in sorted(latest.values(), key=lambda r: (r.get("ts") or 0, r.get("symbol") or "")):
            out.append({
                "signal_id": r["signal_id"],
                "symbol": r.get("symbol"),
                "direction": r.get("direction"),
                "grade": r.get("grade"),
                "bar_ts": r.get("ts"),
                "fired_ts": r.get("created_ts"),
                "entry": r.get("entry"),
                "stop": r.get("stop"),
                "targets": r.get("targets"),
                "reason": r.get("reason"),
                "decision": r.get("decision"),
                "decision_reason": r.get("decision_reason"),
            })
        return out

    async def stop(self) -> None:
        self._stop.set()
        try:
            self._save_decided()
        except Exception as exc:  # noqa: BLE001
            log.warning("decided.save_failed", error=str(exc)[:120])
        try:
            self.alerts.save()
        except Exception as exc:  # noqa: BLE001 - a failed save must not block the shutdown
            log.warning("alerts.save_failed", error=str(exc)[:120])
        await self.feed.stop()
        if self.feed_hub is not None:
            await self.feed_hub.stop()
        await self.committee.stop()
        try:
            await asyncio.to_thread(self.archive.flush, final=True)
        except Exception as exc:  # noqa: BLE001 - shutting down; the archive must not block it
            log.warning("archive.final_flush_failed", error=str(exc))
        try:
            await asyncio.to_thread(self.fulltape.write, self.fulltape.take())
        except Exception as exc:  # noqa: BLE001 - shutting down; the tape must not block it
            log.warning("tape_full.final_write_failed", error=str(exc))
        if self._pivot_repair_task is not None and not self._pivot_repair_task.done():
            self._pivot_repair_task.cancel()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._persist_wallets()
        await self.ledger.close()
        await self.http.aclose()

    def _banner(self) -> None:
        caps = self.gateway.caps
        log.info(
            "engine.boot",
            mode=self._mode.value,
            armed_until=self._armed_until,
            segments=[s.value for s in self.s.segment_list],
            universe=len(self.underlyings),
            strategies=[k.value for k in ALL_KEYS],
            fukaa_top_n=self.fukaa.cfg.top_n if self.fukaa.cfg.top_n is not None else "OFF",
            fukaa_max_same_direction=(
                self.fukaa.cfg.max_same_direction
                if self.fukaa.cfg.max_same_direction is not None
                else "OFF"
            ),
            live_caps={
                "segments": list(caps.segments),
                "max_notional_inr": caps.max_notional_inr,
                "max_positions": caps.max_positions,
            },
            notes=self.boot_notes,
        )

    async def _boot_market(self) -> None:
        cat = await self.catalogue_loader.ensure()
        self.live_exec = LiveExecutor(self.rest, self.costs)
        self.gateway.live = self.live_exec
        self.reconciler_ready = True
        self.reconciler_positions = Reconciler(self.rest)

        # Pass 1 — who is in the universe. scripFinder's rule: every root with a derivative.
        self.universe_builder = UniverseBuilder(
            cat,
            UniversePolicy(
                band_pct=self.s.universe_band_pct,
                strikes_per_side=self.s.universe_strikes_per_side,
                include_indices=self.s.universe_include_indices,
            ),
        )
        groups = self._apply_universe_filters(
            self.universe_builder.build_underlyings(self.s.segment_list, ist_today())
        )
        self.groups = groups
        self.underlyings = {g.root: g.underlying for g in groups.values()}
        for g in groups.values():
            for inst in (g.underlying, *g.futures):
                self._segment_by_code[inst.scrip_code] = inst.segment
        universe = [g.underlying for g in groups.values()]
        log.info(
            "engine.universe",
            **self.universe_builder.summary(groups, depth_symbols=self.s.depth_archive_list),
            sample=[g.root for g in list(groups.values())[:8]],
        )
        for inst in universe:
            self.aggregator.track(inst)
        self._seed_daily_from_cache(universe)
        await self._backfill(universe)
        self._seed_1m_from_archive(universe)
        self._set_closing_auction_bars(universe)  # before anything reads the 30m series
        self._daily_refresh_done = {
            f"{ist_today().isoformat()} {slot}"
            for slot in self.s.daily_refresh_hm
            if ist_hm(time.time()) >= slot
        }

        # Pass 2 — which strikes, now that the previous close is known from the daily backfill.
        n_opts = self.universe_builder.select_all(groups, self._prev_close, ist_today())
        for g in groups.values():
            for o in g.options:
                self._segment_by_code[o.scrip_code] = o.segment
                self._option_codes.add(o.scrip_code)
        self._future_to_underlying = {f.scrip_code: g.root for g in groups.values() for f in g.futures}
        self._seed_oi_reference()

        # Futures are OI sources, not bar sources: on NSE the front future's `symbol` is the cash
        # symbol, and tracking it wrote futures ticks into the equity's bars (found 2026-09-21).
        # `subscriptions()` puts them on mf+oi only; the aggregator ignores untracked codes.
        subs = UniverseBuilder.subscriptions(
            groups.values(), depth_symbols=self.s.depth_archive_list
        )
        # The archive sample is the only standing depth subscription; the rolling set is added and
        # removed by `_sync_depth` around it and may never drop these.
        self._depth_pinned = {i.scrip_code for i in subs["md"]}
        self._depth_following = set(self._depth_pinned)
        # India VIX rides the cash feed and is not a tradeable root, so it never enters the
        # universe — it is subscribed for its price alone, and only where it means something.
        # MCX bands on the contract's own realised vol instead: an equity-index implied vol says
        # nothing about crude, and a confident wrong regime is worse than no regime.
        if any(seg is not Segment.MCX_FO for seg in self.s.segment_list):
            vix = self.catalogue_loader.catalogue.get(INDIA_VIX_SCRIP)
            if vix is not None:
                subs["mf"] = [*subs["mf"], vix]
                log.info("engine.india_vix_subscribed", scrip=INDIA_VIX_SCRIP)
            else:
                log.warning("engine.india_vix_missing", scrip=INDIA_VIX_SCRIP)
        await self.feed.subscribe("mf", subs["mf"])
        await self.feed.subscribe("md", subs["md"])
        await self.feed.subscribe("oi", subs["oi"])
        await self._subscribe_open_positions()
        self._leg_task = asyncio.create_task(self._load_leg_pivots(groups.values()))
        log.info(
            "engine.subscribed",
            mf=len(subs["mf"]), md=len(subs["md"]), oi=len(subs["oi"]),
            options=n_opts, underlyings=len(universe),
        )
        if self.reconciler_positions is not None:
            await self.reconciler_positions.run(
                self._venue_positions(),
                at_venue=self.mode() in (Mode.LIVE, Mode.LIVE_CAPPED),
            )

    def _venue_positions(self) -> list[Position]:
        """The positions that exist at the broker: the LIVE books' only. The paper books' positions
        do not — counted, each one was a PHANTOM that froze every book within 60 s (audit,
        2026-09-26). The reconciler sums them per contract: two live books on one strike are one
        net position at the broker."""
        return [p for p in self.positions.values() if p.strategy in self.LIVE_BOOKS]

    async def _live_funds_short(self, intent: OrderIntent) -> str | None:
        """None when the broker account has the money for this live entry, else why not. Every live
        book draws on the one account, so the paper purses prove nothing about it. Fails CLOSED: no
        answer from the broker, or no margin field recognised in it, refuses the entry.
        The field names are 5paisa's V4/Margin ``EquityMargin`` row as documented publicly —
        CONFIRM on the first armed session (docs/FIVEPAISA_FACTS.md lists no fields)."""
        now = time.time()
        if now - self._margin_cache[0] > 15.0:
            try:
                row = await asyncio.wait_for(self.rest.margin(), timeout=5.0)
            except Exception as exc:  # noqa: BLE001 — fail closed, and say why
                return f"broker funds unknown ({str(exc)[:80]}) — live entry refused"
            self._margin_cache = (now, row)
        row = self._margin_cache[1] or {}
        field = next((f for f in LIVE_MARGIN_FIELDS if f in row), None)
        if field is None:
            return f"no margin field in the broker's answer ({', '.join(sorted(row)[:8])}) — live entry refused"
        avail = float(row[field] or 0.0)
        need = (intent.ref_price or intent.limit_price or 0.0) * intent.qty * intent.instrument.multiplier
        if need > avail:
            return f"broker {field} ₹{avail:,.0f} < this entry's ₹{need:,.0f} — live entry refused"
        return None

    def _prev_close(self, symbol: str) -> float | None:
        bars = self.store.bars(symbol, "1d")
        prior = [b for b in bars if ist_day(b.ts) < ist_today()]
        return prior[-1].close if prior else (bars[-1].close if bars else None)

    def _apply_universe_filters(self, groups: dict[str, ScripGroup]) -> dict[str, ScripGroup]:
        """An explicit ``universe_file`` wins; otherwise the whole derivatives universe, capped by
        ``max_universe`` (None = uncapped, and the banner says so)."""
        if self.s.universe_file and self.s.universe_file.exists():
            wanted = {
                ln.split("#", 1)[0].strip().upper()
                for ln in self.s.universe_file.read_text().splitlines()
                if ln.split("#", 1)[0].strip()
            }
            groups = {r: g for r, g in groups.items() if r in wanted}
            missing = sorted(wanted - set(groups))
            if missing:
                log.warning("universe.file_symbols_missing", symbols=missing[:20], n=len(missing))
        if self.s.max_universe is not None and len(groups) > self.s.max_universe:
            # Stocks before indices, then alphabetical: a cap should trim the tail, not the core.
            ordered = sorted(groups.values(), key=lambda g: (g.note.startswith("index"), g.root))
            groups = {g.root: g for g in ordered[: self.s.max_universe]}
        return groups

    async def _backfill(self, universe: list[Instrument]) -> None:
        """Warm the 30m series and the daily series (for pivots) from REST.

        Rate-limit friendly, but not one call at a time: 486 calls in sequence with a 0.15 s pause
        took 153 s, and a mid-session restart was blind for all of it (review, 2026-10-03). At most
        ``backfill_concurrency`` calls are in flight, each still paced, and the daily series is not
        asked for a name whose cached candles already end at the previous session's final one —
        most names on a normal boot (``_seed_daily_from_cache`` ran first).
        """
        end = ist_today()
        start_intraday = end - timedelta(days=max(7, self.s.backfill_days // 2))
        first = end
        for _ in range(BACKFILL_SESSIONS):
            first = self.calendar.previous_trading_day(first)
        start_intraday = min(start_intraday, first - timedelta(days=1))
        start_daily = end - timedelta(days=400)  # a year of dailies for monthly pivots
        expected_prev = self.calendar.previous_trading_day(end)
        counts = {"ok": 0, "failed": 0, "daily_from_cache": 0}
        sem = asyncio.Semaphore(max(1, self.s.backfill_concurrency))

        def daily_current(inst: Instrument) -> bool:
            prev = previous_session(self.store.bars(inst.symbol, "1d"), end)
            return (prev is not None and is_official(prev) and ist_day(prev.ts) == expected_prev
                    and not is_provisional(prev, inst.segment))

        async def one(inst: Instrument, tf: str, start: date) -> None:
            async with sem:
                try:
                    rows = await self.rest.candles(inst, tf, start.isoformat(), end.isoformat())
                except Exception as exc:  # noqa: BLE001
                    counts["failed"] += 1
                    if tf == "1d":
                        self._daily_failed.add(inst.symbol)
                    log.warning("backfill.failed", symbol=inst.symbol, tf=tf, error=str(exc))
                    return
                finally:
                    await asyncio.sleep(0.15)
            if not rows:
                if tf == "1d":
                    self._daily_failed.add(inst.symbol)
                return
            if tf == "1d":
                self._seed_daily(inst, rows)
            else:
                self.aggregator.seed(inst, tf, rows, ts_of=ist_naive_to_ts)  # lower frames rebuild from ticks
            counts["ok"] += 1

        jobs = []
        for inst in universe:
            jobs.append(one(inst, DECISION_TF, start_intraday))
            if daily_current(inst):
                counts["daily_from_cache"] += 1
            else:
                jobs.append(one(inst, "1d", start_daily))
        began = time.time()
        await asyncio.gather(*jobs)
        log.info("backfill.done", series_ok=counts["ok"], failed=counts["failed"],
                 daily_from_cache=counts["daily_from_cache"], took_s=round(time.time() - began, 1))

    def _set_closing_auction_bars(self, universe: list[Instrument], *, now: float | None = None) -> int:
        """An NSE stock's 15:15 bar is its closing auction — one print at the official close, open =
        high = low = close. The broker's 30m history gets it wrong two ways (operator, 2026-10-02):
        it DROPS the bucket for 4-18 of the NIFTY50 on a back-filled day, and the rows it does serve
        are a stray trade after the auction — 1,599 of 1,773 held for past days were not the
        official close (median 0.25 % off, up to 3.4 %, on 1-500 shares). FUDKII's SuperTrend and
        Bollinger read that bar: the 25 Sep - 1 Oct replay changed 44 trades on it. Every session
        with its 14:45 bar gets its 15:15 bar set to the daily candle's official close (the broker's
        end-of-day row, stamped at midnight — never the provisional 09:15 one), its volume from the
        engine's minute archive where that day was recorded, else the bar's own when it already was
        the auction print, else 0 (the volume reading never reads this slot). Today's is set only
        once the session has closed and its end-of-day row exists — until then the live build's
        auction print stands (``_reconcile_then_decide`` keeps it). Stocks only: an index's last
        bar is a real bar. Returns the bars set."""
        now = time.time() if now is None else now
        today = ist_day(now)
        after_close = now >= session_close_ts(Segment.NSE_EQ, today)
        changed = names = 0
        minutes: dict[date, Any] = {}

        def auction_volume(symbol: str, d: date, t0: int) -> float | None:
            if d not in minutes:
                try:
                    got = self.archive.read_day("bars", d.isoformat(), columns=["symbol", "ts", "v"])
                    minutes[d] = got if len(got) else None
                except Exception as exc:  # noqa: BLE001 — an unreadable archive costs the volume, never the bar
                    log.warning("archive.auction_volume_failed", day=d.isoformat(), error=str(exc)[:120])
                    minutes[d] = None
            df = minutes[d]
            if df is None:
                return None
            m = df[(df.symbol == symbol) & (df.ts >= t0) & (df.ts < t0 + 900)]
            return float(m.drop_duplicates("ts", keep="last").v.sum()) if len(m) else None

        for inst in universe:
            if inst.segment is not Segment.NSE_EQ or inst.kind is not InstrumentKind.EQUITY:
                continue
            series = self.store.bars(inst.symbol, DECISION_TF)
            if not series:
                continue
            self._basis_mismatch.pop(inst.symbol, None)  # re-judged below: a healed series clears it
            official = {ist_day(b.ts): b.close for b in self.store.bars(inst.symbol, "1d")
                        if is_official(b) and b.close > 0 and to_ist(b.ts).time() == dt_time(0, 0)}
            have = {int(b.ts): b for b in series}
            n = 0
            for d in sorted({ist_day(b.ts) for b in series}):
                if d > today or (d == today and not after_close) or d not in official:
                    continue
                t1445 = int(from_ist(datetime.combine(d, dt_time(14, 45))))
                t1515 = t1445 + 1800
                if t1445 not in have:
                    continue
                c = official[d]
                if not basis_ok(c, have[t1445].close):
                    # the daily series is adjusted for a corporate action and the 30m is not: the
                    # "official close" would land the 15:15 bar ~60 % away and wreck SuperTrend and
                    # Bollinger for ~50 bars (review, 2026-10-03)
                    self._basis_mismatch[inst.symbol] = (d.isoformat(), c, have[t1445].close)
                    continue
                held = have.get(t1515)
                tick = inst.tick_size or 0.05
                if held is not None and held.high == held.low and abs(held.close - c) < tick / 2 and "closingAuction" in held.extra:
                    continue  # already the auction print
                vol = auction_volume(inst.symbol, d, t1515)
                if vol is None:
                    vol = held.volume if (held is not None and abs(held.close - c) < tick / 2) else 0.0
                self.store.replace_closed(UnifiedBar(
                    symbol=inst.symbol, scrip_code=inst.scrip_code, tf=DECISION_TF, ts=t1515, open=c, high=c, low=c, close=c,
                    volume=vol, source=BarSource.REST, complete=True, prev_close=have[t1445].prev_close,
                    extra={"closingAuction": "the daily candle's official close",
                           **({"replaced": {"o": held.open, "h": held.high, "l": held.low, "c": held.close, "v": held.volume}}
                              if held is not None else {})},
                ))
                n += 1
            if n:
                changed += n
                names += 1
        log.info("bars.closing_auction_set", names=names, bars=changed)
        return changed

    def _seed_1m_from_archive(self, universe: list[Instrument], day: date | None = None) -> int:
        """Today's 1m bars back from the engine's own archive. The 1m series is built from live
        ticks alone and every closed 1m bar is archived (flushed at shutdown too), so without this a
        restart emptied it: every card's underlying path read "appears once 1m bars accrue" until the
        next session's ticks (2026-09-29, restarts at 15:37 and 18:39). No broker call; nothing that
        decides a trade reads the 1m series. Returns the bars seeded."""
        day = day or ist_today()
        try:
            df = self.archive.read_day("bars", day.isoformat(), columns=["symbol", "ts", "o", "h", "l", "c", "v"])
            if df.empty:
                return 0
        except Exception as exc:  # noqa: BLE001 — an unreadable archive costs the paths, never the boot
            log.warning("archive.1m_seed_failed", day=day.isoformat(), error=str(exc)[:120])
            return 0
        by_symbol = {i.symbol: i for i in universe}
        n = names = 0
        for sym, g in df.groupby("symbol"):
            inst = by_symbol.get(str(sym))
            if inst is None:
                continue
            rows = g.sort_values("ts").drop_duplicates("ts", keep="last")
            bars = [
                UnifiedBar(symbol=inst.symbol, scrip_code=inst.scrip_code, tf="1m", ts=int(r.ts), open=float(r.o), high=float(r.h),
                           low=float(r.l), close=float(r.c), volume=float(r.v), source=BarSource.LIVE, complete=True)
                for r in rows.itertuples()
            ]
            self.store.seed(inst.symbol, "1m", bars)
            n += len(bars)
            names += 1
        log.info("archive.1m_seeded", day=day.isoformat(), names=names, bars=n)
        return n

    def _seed_daily(self, inst: Instrument, rows: list[dict[str, Any]]) -> None:
        bars = [
            UnifiedBar(
                symbol=inst.symbol,
                scrip_code=inst.scrip_code,
                tf="1d",
                ts=int(ist_naive_to_ts(r["dt"])),
                open=r["o"],
                high=r["h"],
                low=r["l"],
                close=r["c"],
                volume=r["v"],
                complete=True,
                # The broker's own daily candle (it matches NSE bhavcopy to the paisa), never a
                # roll-up of intraday bars: 5paisa's intraday candles stop at 15:15, and a daily
                # built from them carries the wrong close and misses any late-session high or low.
                source=BarSource.REST,
            )
            for r in rows
        ]
        self._install_daily(inst, bars)
        # The official series is what the pivots read, so anything computed on the old one is void;
        # and the cache holds the last known official candles for a boot the broker cannot serve.
        self._zone_cache.pop(inst.symbol, None)
        self._daily_failed.discard(inst.symbol)
        self.daily_cache.save(inst.symbol, self.store.bars(inst.symbol, "1d"))

    async def _subscribe_open_positions(self) -> list[Instrument]:
        """A restored position's contract is subscribed whether or not today's strike shortlist
        still contains it. The shortlist is picked around the current spot; a contract bought
        yesterday six strikes away is not in it, and on 2026-09-23 the DIXON 14000 CE twin came
        back from a restart with no quote, no evaluation and no exit — silently."""
        held = {
            p.instrument.scrip_code: p.instrument
            for p in self.positions.values()
            if p.status == "OPEN" and p.qty_remaining > 0
        }
        if not held:
            return []
        insts = list(held.values())
        await self.feed.subscribe("mf", insts)
        await self._follow_depth(insts)  # tracked, so `_sync_depth` hands it back on the exit
        await self.feed.subscribe("oi", [i for i in insts if i.kind is not InstrumentKind.EQUITY])
        log.info("positions.resubscribed", instruments=[i.name or i.scrip_code for i in insts])
        return insts

    def _install_daily(self, inst: Instrument, fresh: list[UnifiedBar]) -> None:
        """``fresh`` over what the store holds, one bar per session (``one_per_session``)."""
        held = self.store.bars(inst.symbol, "1d")
        merged = one_per_session((held, fresh), lambda d: session_open_ts(inst.segment, d))
        self.store.replace_series(inst.symbol, "1d", merged)

    def _seed_daily_from_cache(self, universe: list[Instrument]) -> None:
        """The last known official candles, before REST is asked. A boot while the broker's
        historical endpoint is down then resumes on real levels; ``store.seed`` lets the REST
        refetch win over these the moment it answers."""
        hit = 0
        cat = self.catalogue_loader.catalogue
        for inst in universe:
            if (inst.segment is Segment.MCX_FO and inst.kind is InstrumentKind.FUTURE
                    and cat.front_future(inst.symbol, on=ist_today()) is not inst):
                # a rolled commodity: the cache holds the expiring month's candles, which are not this
                # contract's — without the REST answer it has no levels rather than the wrong ones
                continue
            bars = self.daily_cache.load(inst.symbol, inst.scrip_code)
            if bars:
                self._install_daily(inst, bars)
                hit += 1
        log.info("daily.cache_seeded", names=hit, of=len(universe))

    async def _refetch_daily(self, symbols: list[str]) -> int:
        """Refetch the official daily series for ``symbols``. A call that raises stays in
        ``_daily_failed`` and is retried every pass; an empty answer is an answer."""
        end = ist_today()
        start = end - timedelta(days=400)
        ok = 0
        for sym in symbols:
            inst = self.underlyings.get(sym)
            if inst is None:
                continue
            try:
                rows = await self.rest.candles(inst, "1d", start.isoformat(), end.isoformat())
            except Exception as exc:  # noqa: BLE001 - the loop comes back for it
                self._daily_failed.add(sym)
                log.debug("daily.refetch_failed", symbol=sym, error=str(exc)[:80])
                continue
            self._daily_failed.discard(sym)
            if rows:
                self._seed_daily(inst, rows)
                ok += 1
            await asyncio.sleep(0.15)
        return ok

    def daily_audit(self) -> Any:
        """Every underlying's daily series, classified by whether it can carry today's pivots."""
        return audit_daily(
            {sym: self.store.bars(sym, "1d") for sym in self.underlyings}, ist_today(), self.calendar,
            segments={sym: inst.segment for sym, inst in self.underlyings.items()},
            quiet=self.quiet_listed(),
        )

    def quiet_listed(self) -> frozenset[str]:
        """``data/quiet.txt``: names the operator keeps but calls quiet while they do not trade
        (operator, 2026-10-03: "keep cardamom but name it quiet for now. show alerts but dont trade
        till there is liquidity"). One symbol a line, ``#`` comments; read on every call, so an edit
        needs no restart."""
        try:
            text = (self.s.data_dir / "quiet.txt").read_text()
        except OSError:
            return frozenset()
        return frozenset(w.upper() for line in text.splitlines() if (w := line.split("#", 1)[0].strip()))

    def quiet_reason(self, symbol: str) -> str | None:
        """Why ``symbol`` takes no trade now: listed quiet AND its daily series behind — no recent
        trade at the broker. None once it trades again (its previous session is current)."""
        if symbol.upper() not in self.quiet_listed():
            return None
        inst = self.underlyings.get(symbol)
        one = audit_daily({symbol: self.store.bars(symbol, "1d")}, ist_today(), self.calendar,
                          segments={symbol: inst.segment} if inst is not None else None, quiet={symbol})
        if symbol not in one.quiet:
            return None
        prev = previous_session(self.store.bars(symbol, "1d"), ist_today())
        last = ist_day(prev.ts).isoformat() if prev is not None else "never"
        return (f"{symbol} is quiet (data/quiet.txt): no daily candle at the broker since {last} — "
                "alerts only, no trade until it trades again")

    async def _pivot_repair(self) -> None:
        """docs/PIVOTS.md §3–4: refetch what the audit flags, reload what the ladders lack.

        A name is asked about once per expected session unless the call itself failed: if the
        broker has nothing newer for it, asking again every two minutes would not change the
        answer, and two hundred names asking would be the retry storm the backfill avoids.
        """
        try:
            if self._daily_due:
                self._daily_due = False
                await self._refetch_daily(list(self.underlyings))
                # the official closes just arrived: yesterday's 15:15 bar (the 08:30 refresh) and,
                # after the close, today's become the auction print (``_set_closing_auction_bars``)
                self._set_closing_auction_bars(list(self.underlyings.values()))
            a = self.daily_audit()
            # A call that raised is retried every pass whatever the audit calls the name; a
            # dormant name (nothing at the broker) is asked once per session and left alone; a
            # provisional one every PROVISIONAL_RETRY_S until the end-of-day candle lands.
            now = time.time()
            prov = set(a.provisional)
            wanted = sorted(
                {sym for sym in a.needs_refresh if sym not in prov and self._daily_confirmed.get(sym) != a.expected_prev}
                | {sym for sym in prov if now - self._daily_provisional_asked.get(sym, 0.0) >= PROVISIONAL_RETRY_S}
                | {sym for sym in self._daily_failed if sym in self.underlyings}
            )
            for sym in wanted:
                if sym in prov:
                    self._daily_provisional_asked[sym] = now
                elif a.expected_prev is not None:
                    self._daily_confirmed[sym] = a.expected_prev
            if wanted:
                n = await self._refetch_daily(wanted[:REPAIR_BATCH])
                log.info("daily.repaired", refetched=n, asked=len(wanted), audit=a.summary())
            # Never alongside the boot's bulk load: a boot after 12:30 found both re-anchor slots
            # due and fetched the same ~2,000 ladders a second time, concurrently. The flags stay
            # set, so the re-anchor runs on the next pass — on the live spot, for the gap only.
            if (self._legs_due or self._legs_reanchor_due or self.leg_pivots.failed_codes) and not self.leg_pivots.running:
                legs = self._expected_legs(self.groups.values())
                # A re-anchor is not a reload: _expected_legs re-bands the OTM set on the LIVE
                # spot, and everything already held stays. Only the strikes the gap brought into
                # band are fetched.
                todo = legs if self._legs_due else self.leg_pivots.missing(legs)
                reanchor, self._legs_due, self._legs_reanchor_due = self._legs_reanchor_due, False, False
                if todo:
                    n = await self.leg_pivots.load(todo, ist_today())
                    if reanchor:
                        log.info("legs.reanchored", fetched=n, asked=len(todo))
        except Exception as exc:  # noqa: BLE001 - advisory levels never stall the engine
            log.warning("pivot_repair.failed", error=str(exc))

    # -- control -----------------------------------------------------------------------------------

    async def _load_control(self) -> None:
        row = await self.ledger.get_control()
        mode = Mode(row.get("mode", "SHADOW"))
        armed = row.get("armed_until")
        if mode in (Mode.LIVE, Mode.LIVE_CAPPED) and (armed is None or armed < time.time()):
            self.boot_notes.append(
                f"{mode.value} arming expired — booting into PAPER. Re-arm explicitly."
            )
            mode = Mode.PAPER
            await self.ledger.set_mode(mode.value, None)
        self._mode, self._armed_until = mode, armed
        self._halted = bool(row.get("halted"))
        self._halt_reason = str(row.get("halt_reason") or "")

    def mode(self) -> Mode:
        if self._mode in (Mode.LIVE, Mode.LIVE_CAPPED):
            if self._armed_until is None or self._armed_until < time.time():
                return Mode.PAPER
        return self._mode

    def halted(self) -> tuple[bool, str]:
        if self._halted:
            return True, self._halt_reason
        if self.reconciler_positions is not None and self.reconciler_positions.frozen:
            return True, f"reconcile: {self.reconciler_positions.freeze_reason}"
        # the order breaker is per book now (exec/gateway.py): a tripped book stops its own
        # entries; it no longer halts — and flattens — every book
        return False, ""

    async def set_mode(self, mode: Mode, *, armed_minutes: int | None = None) -> dict[str, Any]:
        armed = None
        if mode in (Mode.LIVE, Mode.LIVE_CAPPED):
            if not armed_minutes:
                raise ValueError(f"{mode.value} requires armed_minutes — arming is never implicit")
            armed = time.time() + armed_minutes * 60
        self._mode, self._armed_until = mode, armed
        await self.ledger.set_mode(mode.value, armed)
        await self.ledger.event("mode", {"mode": mode.value, "armed_until": armed})
        self.telegram.fire_and_forget(
            f"🔁 kotsin-nse mode → <b>{mode.value}</b>"
            + (f" (armed {armed_minutes}m)" if armed_minutes else "")
        )
        log.warning("engine.mode", mode=mode.value, armed_until=armed)
        return {"mode": mode.value, "armed_until": armed}

    async def set_halt(self, halted: bool, reason: str = "") -> None:
        self._halted, self._halt_reason = halted, reason
        await self.ledger.set_halt(halted, reason)
        await self.ledger.event("halt", {"halted": halted, "reason": reason})
        self.telegram.fire_and_forget(
            f"{'🛑 HALT' if halted else '✅ resume'} kotsin-nse — {reason or 'manual'}"
        )

    # -- market data --------------------------------------------------------------------------------

    async def _on_tick(self, tick: dict[str, Any]) -> None:
        code = str(tick["scrip_code"])
        self.feed_rate.on_tick(
            code,
            FEED_SEGMENTS.get(f"{tick.get('exch') or ''}{tick.get('exch_type') or ''}", "OTHER"),
            float(tick.get("recv_ts") or time.time()),
            int(tick.get("last_qty") or 0),
            int(tick.get("total_qty") or 0),
        )
        if code in self._option_codes and (total := int(tick.get("total_qty") or 0)) > 0:
            # the strike's traded volume from the feed itself: it was written only from REST
            # snapshots, so a streaming strike read 0 and the liquidity comparison in the strike
            # choice compared 0 against everything (review, 2026-10-03)
            self.option_volume[code] = float(total)
        ltp = float(tick.get("ltp") or 0)
        if ltp > 0:
            seen = float(tick.get("recv_ts") or time.time())
            traded = float(tick.get("ts") or seen)  # the frame's TickDt; its arrival when it has none
            self.ltps[code] = ltp
            self._ltp_traded_ts[code] = traded
            self.quotes[code] = Quote(
                ltp=ltp,
                bid=float(tick.get("bid") or 0),
                ask=float(tick.get("ask") or 0),
                ts=seen,
                traded_ts=traded,
            )
        await self.aggregator.on_tick(tick)
        # no TICK publish: nothing subscribes to it (/api/system: 'subscribers': []) and it ran on
        # every frame (review, 2026-10-03). The BAR publish stays — once a bar, the hook for a reader.

    async def _on_depth(self, depth: dict[str, Any]) -> None:
        code = str(depth["scrip_code"])
        ts = float(depth.get("recv_ts") or time.time())
        self.books[code] = BookSnapshot(scrip_code=code, bids=depth["bids"], asks=depth["asks"], ts=ts)
        self.micro.on_depth(code, depth["bids"], depth["asks"], ts)

    async def _on_oi(self, oi: dict[str, Any]) -> None:
        fut_code = str(oi["scrip_code"])
        recv = float(oi.get("recv_ts") or time.time())
        self.oi_frames["frames"] += 1
        if oi.get("oi_change"):
            self.oi_frames["change_nonzero"] += 1
        if oi.get("oi_change_pct"):
            self.oi_frames["change_pct_nonzero"] += 1
        self.archive.oi(
            fut_code,
            recv,
            float(oi["open_interest"]),
            float(oi["oi_change_pct"]) if oi.get("oi_change_pct") is not None else None,
            change=float(oi["oi_change"]) if oi.get("oi_change") is not None else None,
            tick_ts=oi.get("tick_ts"),
            ltp=float(oi["ltp"]) if oi.get("ltp") else None,
            volume=float(oi["volume"]) if oi.get("volume") else None,
        )
        symbol = self._future_to_underlying.get(fut_code)
        if symbol is None:
            # Not a future we map to an underlying → an option strike. Keep its OI; the Options
            # page and any OI-aware selection read it from here.
            self.option_oi[fut_code] = {
                "oi": float(oi["open_interest"]),
                "change_pct": float(oi["oi_change_pct"]),
                "ts": float(oi.get("recv_ts") or time.time()),
            }
            return
        self._note_oi(fut_code, float(oi["open_interest"]), recv)
        inst = self.underlyings.get(symbol)
        if inst is None:
            return
        # a stock's bars carry its FRONT month's OI — the near and next month both printing here
        # wrote whichever came last (found 2026-10-02) — and the change we computed, never the
        # broker's field, which is 0.0 on every frame
        if fut_code != self._front_future_code(symbol):
            return
        ref = self._oi_ref.get(fut_code)
        self.aggregator.set_oi(
            inst.scrip_code,
            oi=int(oi["open_interest"]),
            change_pct=round((float(oi["open_interest"]) / ref - 1.0) * 100.0, 3) if ref else None,  # type: ignore[arg-type]
            fut_code=fut_code,
        )

    def _note_oi(self, code: str, oi: float, ts: float) -> None:
        """Keep a future's OI level; at the first print of a new day the levels held from the day
        before become the previous-close reference (an engine running through midnight); a print
        before the open is the previous close itself (OI does not move pre-open).

        The reference is the PREVIOUS TRADING DAY's close, so the held levels roll into it only when
        that day changes. Rolled on any new calendar day, a Saturday boot's references — NSE's own
        closes for Friday — were replaced on Monday's first print by 5paisa's last Friday print
        re-sent at the Saturday subscribe: −0.05 % read where NSE's close gives +1.00 % (review,
        2026-10-03). Thursday → Friday still rolls; Saturday → Monday, or across a holiday, does not."""
        day = ist_day(ts)
        seg = self._segment_by_code.get(code, Segment.NSE_FO)
        if self._oi_ref_day != day:
            if (self._oi_ref_day is not None
                    and self.calendar.previous_trading_day(day) != self.calendar.previous_trading_day(self._oi_ref_day)):
                rolled = {c: lv[0] for c, lv in self._fut_oi.items() if ist_day(lv[1]) < day and lv[0] > 0}
                self._oi_ref.update(rolled)
                self._oi_ref_src.update(dict.fromkeys(rolled, "archive"))
            self._oi_ref_day = day
        if code not in self._oi_ref and oi > 0 and ts < session_open_ts(seg, day):
            self._oi_ref[code] = oi
            self._oi_ref_src[code] = "preopen"
        self._fut_oi[code] = (oi, ts)
        self.oi_candles.on_print(code, oi, ts, seg, trading_day=self.calendar.is_trading_day(day))

    def _seed_oi_reference(self, *, today: date | None = None) -> int:
        """The previous session's closing OI for every future we map, from the engine's own OI archive
        (the last print of the day before), else today's first print before the open. A future with
        neither has no reference — its readings are doubtful until tomorrow, never guessed."""
        today = today or ist_today()
        codes = set(self._future_to_underlying)
        prev = self.calendar.previous_trading_day(today)
        refs: dict[str, float] = {}
        src: dict[str, str] = {}
        for source, read in (
            ("nse", lambda want: self._oi_from_bhavcopy(prev, want)),
            ("archive", lambda want: self._oi_from_archive(prev, want, last=True)),
            ("preopen", lambda want: self._oi_from_archive(today, want, last=False, before=session_open_ts(Segment.NSE_EQ, today))),
        ):
            missing = codes - set(refs)
            if not missing:
                break
            found = read(missing)
            refs.update(found)
            src.update(dict.fromkeys(found, source))
        self._oi_ref, self._oi_ref_src, self._oi_ref_day = refs, src, today
        if self.oi_daily.has(prev):
            self._oi_bhav_day = prev
        log.info("oi.reference_seeded", futures=len(codes), with_reference=len(refs),
                 by_source={k: sum(1 for v in src.values() if v == k) for k in ("nse", "archive", "preopen")})
        return len(refs)

    def _oi_from_bhavcopy(self, day: date, codes: set[str]) -> dict[str, float]:
        try:
            return self.oi_daily.closes(day, codes)
        except Exception as exc:  # noqa: BLE001 — an unreadable file is no reference, never a failed boot
            log.warning("oi.bhavcopy_read_failed", day=day.isoformat(), error=str(exc)[:120])
            return {}

    async def _ensure_oi_bhavcopy(self, *, today: date | None = None) -> bool:
        """Hold the previous session's F&O bhavcopy and take every reference the exchange has from
        it: its closing OI is the official one, so it replaces an archive print or a pre-open one
        (operator, 2026-10-03). NSE publishes it in the evening, so this is asked again until it
        answers. Returns whether the file is held."""
        today = today or ist_today()
        prev = self.calendar.previous_trading_day(today)
        if not self.oi_daily.has(prev):
            df = await fetch_fo_bhavcopy(self.http, prev)
            if df is None or df.empty:
                return False
            try:
                await asyncio.to_thread(self.oi_daily.write, prev, df)
            except Exception as exc:  # noqa: BLE001 — a disk that refuses is the archive's references, not a crash
                log.warning("oi.bhavcopy_write_failed", day=prev.isoformat(), error=str(exc)[:120])
                return False
        if self._oi_ref_day != today:
            self._seed_oi_reference(today=today)  # the day turned: everything from the file first
        else:
            official = await asyncio.to_thread(self._oi_from_bhavcopy, prev, set(self._future_to_underlying))
            self._oi_ref.update(official)
            self._oi_ref_src.update(dict.fromkeys(official, "nse"))
        self._oi_bhav_day = prev
        log.info("oi.bhavcopy_reference", day=prev.isoformat(),
                 from_nse=sum(1 for v in self._oi_ref_src.values() if v == "nse"), futures=len(self._future_to_underlying))
        return True

    def _oi_from_archive(self, day: date, codes: set[str], *, last: bool, before: float | None = None) -> dict[str, float]:
        if not codes:
            return {}
        try:
            # the day file and any parts today's flushes left (ops/archive.py)
            df = self.archive.read_day("oi", day.isoformat(), columns=["scrip_code", "ts", "oi"])
        except Exception as exc:  # noqa: BLE001 — no archive is no reference, never a failed boot
            log.warning("oi.archive_read_failed", day=day.isoformat(), error=str(exc)[:120])
            return {}
        if df.empty:
            return {}
        df = df[df.scrip_code.astype(str).isin(codes) & (df.oi > 0)]
        if before is not None:
            df = df[df.ts < before]
        df = df.sort_values("ts")
        rows = df.groupby(df.scrip_code.astype(str)).oi
        return {str(c): float(v) for c, v in (rows.last() if last else rows.first()).items()}

    def _front_future_code(self, symbol: str) -> str | None:
        key = (symbol, ist_today())
        if key not in self._front_code:
            f = self.catalogue_loader.catalogue.front_future(symbol, on=key[1])
            self._front_code[key] = f.scrip_code if f is not None else None
        return self._front_code[key]

    def oi_reading(self, symbol: str, *, now: float | None = None) -> OiReading:
        """``symbol``'s OI change since the previous close, per ``bars/oi_read.py``: the current month,
        and in its last three sessions the current and next month summed."""
        today = ist_today()
        futs = sorted((f for f in self.catalogue_loader.catalogue.futures_by_symbol.get(symbol.upper(), [])
                       if f.expiry and f.expiry[:10] >= today.isoformat() and f.scrip_code in self._future_to_underlying),
                      key=lambda f: f.expiry)
        if not futs:
            return OiReading(doubt="no unexpired future on the OI feed")
        expiry = date.fromisoformat(futs[0].expiry[:10])
        sessions = sum(1 for k in range((expiry - today).days + 1) if self.calendar.is_trading_day(today + timedelta(days=k)))
        # Only a level received during TODAY's session is today's OI. 5paisa's OI frames carry no
        # broker time, and at a subscribe it re-sends each contract's last print: on a Saturday
        # JSWSTEEL's last 1 Oct print arrived fresh and read +1.05 % against NSE's official close
        # (review, 2026-10-03). Before the open, or on a closed day, the reading is doubtful.
        since = (session_open_ts(Segment.NSE_FO, today) if self.calendar.is_trading_day(today) else float("inf"))
        return read_oi([f.scrip_code for f in futs], sessions_left=sessions, levels=self._fut_oi, refs=self._oi_ref,
                       now=time.time() if now is None else now, ref_sources=self._oi_ref_src, session_open=since)

    def oi_view(self, symbol: str) -> dict[str, Any]:
        """Everything behind one underlying's OI reading: each future's level now, the previous close
        it is measured from and where that came from, and its OI candles."""
        symbol = symbol.upper()
        reading = self.oi_reading(symbol)
        today = ist_today()
        futs = sorted((f for f in self.catalogue_loader.catalogue.futures_by_symbol.get(symbol, [])
                       if f.expiry and f.expiry[:10] >= today.isoformat()), key=lambda f: f.expiry)
        legs = []
        for f in futs[:3]:
            lv = self._fut_oi.get(f.scrip_code)
            day = self.oi_candles.forming(f.scrip_code, "1d")
            legs.append({
                "code": f.scrip_code, "expiry": f.expiry[:10], "oi": lv[0] if lv else None, "at": lv[1] if lv else None,
                "ref": self._oi_ref.get(f.scrip_code), "refSource": self._oi_ref_src.get(f.scrip_code),
                "day": day.to_json() if day is not None else None,
                "candles30m": [c.to_json() for c in self.oi_candles.series(f.scrip_code, "30m", 14)],
            })
        return {"symbol": symbol, "reading": reading.to_json(), "refDay": self.calendar.previous_trading_day(today).isoformat(),
                "bhavcopyDay": self._oi_bhav_day.isoformat() if self._oi_bhav_day else None, "legs": legs,
                "brokerFields": dict(self.oi_frames)}

    def oi_relative(self, symbol: str, *, now: float | None = None) -> tuple[float | None, int]:
        """How far ``symbol``'s OI change stands from the NIFTY50's at this minute (a z-score in their
        own spread) — ``(None, n)`` when it cannot be read on enough members."""
        now = time.time() if now is None else now
        own = self.oi_reading(symbol, now=now)
        if not own.ok:
            return None, 0
        minute = int(now // 60)
        if self._n50_oi[0] != minute:
            self._n50_oi = (minute, {m: r.change_pct for m in NIFTY50 if (r := self.oi_reading(m, now=now)).ok})  # type: ignore[misc]
        peers = [v for m, v in self._n50_oi[1].items() if m != symbol]
        return relative_z(own.change_pct, peers)  # type: ignore[arg-type]

    def n50_volume_surge(self, t_ts: float) -> tuple[float | None, int]:
        """The NIFTY50's mean volume surge at the bar starting ``t_ts`` (each member's checked
        reading) — what a stock's own surge is measured against, since an opening bar is heavy for
        every name (09:45 triggers passed FUKAA's 4x test 43 % of the time, later bars 18 %)."""
        key = int(t_ts)
        if key not in self._n50_vol:
            v = [r.surge_t for m in NIFTY50 if m in self.underlyings and (r := self._volume_reading(m, t_ts)).ok]
            self._n50_vol[key] = (sum(v) / len(v), len(v)) if len(v) >= 25 else (None, len(v))  # type: ignore[arg-type]
            if len(self._n50_vol) > 64:
                self._n50_vol.pop(next(iter(self._n50_vol)))
        return self._n50_vol[key]

    def book_for(self, scrip_code: str) -> BookSnapshot | None:
        return self.books.get(scrip_code)

    def ltp_for(self, scrip_code: str) -> float | None:
        return self.ltps.get(scrip_code)

    # -- pivots --------------------------------------------------------------------------------------

    def zones_for(self, symbol: str) -> list[Zone]:
        """Daily + weekly + monthly pivot zones, clustered at the volatility regime's width.

        The *levels* come from completed periods only and are fixed for the session. The width
        they are merged at is not: it is ``k x ATR`` with ``k`` set by India VIX on NSE and by
        the contract's own realised vol on MCX, so the same pivots cluster differently in a calm
        tape and a violent one. The cache is therefore keyed by the regime as well as the day —
        a band change recomputes, anything else is served from cache, because clustering thirty
        levels for two hundred symbols on every bar would be the hottest thing in the process.

        An open position is unaffected by a band change: its stop was stamped onto the position
        at entry and never moves. Only *new* signals see the new width.
        """
        built = self._zone_build(symbol)
        return built.zones if built is not None else []

    def _zone_build(self, symbol: str) -> ZoneBuild | None:
        """Today's zones AND the classic points they were clustered from, from the ONE builder — or
        None where it refuses the name. ``zones_for`` and ``_pivot_points`` (gate B's "key level
        ahead", the counter legs) both read it, so a name without levels has no pivots either
        (review, 2026-10-03: the points ignored a provisional candle and a basis mismatch)."""
        today = ist_today()
        regime = self.volatility_regime(symbol)
        key = f"{today.isoformat()}:{regime.band.value}"
        hit = self._zone_cache.get(symbol)
        if hit and hit[0] == key:
            return hit[1]
        inst = self.underlyings.get(symbol)
        # One builder for live and the backtest (bars/zones.py): the width from the sessions BEFORE
        # today (a restart no longer changes it), no levels from a provisional daily candle or from
        # a daily series on another price basis than the 30m one. A refusal is deliberately not
        # cached: the name gets real levels the moment the repair loop lands the official candle.
        built = build_zones(self.store.bars(symbol, "1d"), self.store.bars(symbol, DECISION_TF), today,
                            k=regime.k, segment=inst.segment if inst is not None else Segment.NSE_EQ)
        if built.refused:
            self.zone_refusals[symbol] = f"{built.refused}: {built.detail}"
            if built.refused == "basis":
                log.warning("zones.basis_mismatch", symbol=symbol, detail=built.detail)
            return None
        self.zone_refusals.pop(symbol, None)
        self._zone_cache[symbol] = (key, built)
        return built

    def volatility_regime(self, symbol: str) -> Regime:
        """India VIX for NSE, the contract's own realised vol for MCX."""
        inst = self.underlyings.get(symbol)
        if inst is not None and inst.segment is Segment.MCX_FO:
            return regime_for_commodity(self._atr_pct_history(symbol))
        return regime_for_equity(self.india_vix())

    def india_vix(self) -> float | None:
        """Last India VIX print. None when it is not subscribed or has not ticked."""
        return self.ltps.get(INDIA_VIX_SCRIP)

    def regime_snapshot(self) -> dict[str, Any]:
        """The volatility regime in force — the ``k`` every NSE zone is clustered at, and the VIX
        print it came from. ``vixPrint`` None means the feed has not delivered the index tick and
        the fallback ``k`` is in use; a number nobody can see is a number nobody can question."""
        vix = self.india_vix()
        return {"vixPrint": vix, "nse": regime_for_equity(vix).to_json(), "stockIvNames": len(self.stock_iv)}

    def _atr_pct_history(self, symbol: str) -> list[float]:
        """Daily ATR as a percent of close, one per session — the commodity vol baseline."""
        dailies = self.store.bars(symbol, '1d', 40)
        out: list[float] = []
        for i in range(15, len(dailies)):
            a = atr(dailies[: i + 1], 14)
            c = dailies[i].close
            if a and c > 0:
                out.append(a / c * 100)
        return out

    def session_phase(self, symbol: str, ts: int) -> str:
        inst = self.underlyings.get(symbol)
        return session_phase_of(inst.segment if inst else Segment.NSE_EQ, ts)

    async def _on_feed_connect(self, reconnect: bool) -> None:
        if not reconnect:
            return
        marked = self.aggregator.on_reconnect()
        log.warning("feed.gap_repaired", forming_marked_partial=marked)
        await self.ledger.event("feed.reconnected", {"forming_marked_partial": marked, "silence_reconnects": self._silence_reconnects})

    # -- decision path -----------------------------------------------------------------------------

    async def _on_bar_close(self, bar: UnifiedBar) -> None:
        await self.bus.publish(Topic.BAR, bar)
        if bar.tf == "1m":
            self.archive.bar(bar)
        # Advisory books run on every frame — MCX_BB15 decides on 15m and FUDKII-RT on 1m, so
        # this must sit above the decision-timeframe return, not inside it. The decision frame's
        # books wait for the exchange's candle, like the decision itself (_reconcile_then_decide):
        # they used to read the live build, which the reconciler then corrected under them.
        if bar.tf != DECISION_TF:
            self.alerts.on_bar(bar)
            return
        # One decision per bucket, whatever the feed does: a bucket closed twice was decided twice
        # and counted twice toward every open position's time stop.
        key = (bar.symbol, int(bar.ts))
        if key in self._decided or key in self._closes_seen:
            self._duplicate_closes += 1
            log.warning("decide.duplicate_close", symbol=bar.symbol, ts=bar.ts)
            return
        if len(self._closes_seen) > 20_000:
            self._closes_seen = {k for k in self._closes_seen if ist_day(k[1]) == ist_today()}
        self._closes_seen.add(key)
        for pos in self.positions.values():
            if pos.status == "OPEN" and pos.underlying.symbol == bar.symbol:
                pos.bars_held += 1
        micro = self.micro.for_bar(bar.scrip_code, bar.ts)
        if micro:
            bar.extra["micro"] = micro
            self.archive.micro(bar.scrip_code, bar.ts, micro)
        # Off the tick path: at 15:15 IST two hundred 30m bars close in the same second and each
        # waits on a REST round trip. Awaiting that inside the feed handler would stall every
        # symbol's ticks. Concurrency is bounded by the reconciler's semaphore.
        task = asyncio.create_task(self._reconcile_then_decide(bar))
        self._decision_tasks.add(task)
        task.add_done_callback(self._decision_tasks.discard)

    async def _reconcile_then_decide(self, bar: UnifiedBar) -> None:
        """Hold the decision until the exchange's own candle is installed — or the timeout.

        The live build is right most of the time (87.8% exact on 1m, measured; better on 30m). A
        strategy reads the bar forever, so "most of the time" is the wrong standard for the one
        bar it decides on. REST serves a bucket within seconds of its close.
        """
        current = bar
        stock = (inst := self.underlyings.get(bar.symbol)) is not None and inst.segment is Segment.NSE_EQ \
            and inst.kind is InstrumentKind.EQUITY
        if stock and not on_session_grid(Segment.NSE_EQ, bar.ts, DECISION_TF, until=NSE_EQ_CONTINUOUS_UNTIL):
            # the closing auction: the live build is its one print; the broker's row for the bucket is
            # a stray trade after it (1,599 of 1,773 past ones off the official close) — never
            # installed over it (operator, 2026-10-02)
            log.info("bars.auction_kept_live", symbol=bar.symbol, ts=bar.ts, close=bar.close)
        elif self.reconciler_ready and self.s.has_credentials:
            check = await self.reconciler.reconcile_bar(
                bar, timeout_s=self.s.decision_reconcile_timeout_s
            )
            if not check.found or check.error:
                if bar.source is BarSource.PARTIAL:
                    # The socket joined this bucket mid-way and the exchange could not confirm it.
                    # What we hold is a fragment — after the 23:46 restart, literally one closing
                    # snapshot — and a strategy must never mistake that for a bar. No decision.
                    self.reconciler.decisions_skipped_partial += 1
                    log.warning(
                        "decide.skipped_partial", symbol=bar.symbol, ts=bar.ts, reason=check.error or "no REST bucket"
                    )
                    return
                self.reconciler.decisions_on_live_bar += 1
            latest = self.store.last(bar.symbol, DECISION_TF)
            if latest is not None and latest.ts == bar.ts:
                current = latest
                if "micro" in bar.extra and "micro" not in current.extra:
                    current.extra["micro"] = bar.extra["micro"]
        self.alerts.on_bar(current)
        self._decision_lat.append(max(0.0, time.time() - (bar.ts + TF_SECONDS.get(bar.tf, 0))))
        async with self.guards.duty("decide", symbol=current.symbol, ts=int(current.ts)):
            await self._decide(current)

    async def _intraday_universe_rebuild(self) -> None:
        if self.universe_builder is None:
            return
        try:
            cat = await self.catalogue_loader.ensure(force=True)
            self.universe_builder.cat = cat
            before = {o.scrip_code for g in self.groups.values() for o in g.options}
            self.universe_builder.select_all(self.groups, self._prev_close, ist_today())
            fresh = [o for g in self.groups.values() for o in g.options if o.scrip_code not in before]
            for o in fresh:
                self._segment_by_code[o.scrip_code] = o.segment
                self._option_codes.add(o.scrip_code)
            if fresh:
                # Price and OI only. Depth follows what is about to be priced (`_sync_depth`) —
                # putting a strike listed this morning on the depth channel here would restore the
                # load the narrowing removed, and untracked, so nothing would ever hand it back.
                await self.feed.subscribe("mf", fresh)
                await self.feed.subscribe("oi", fresh)
            log.info("universe.intraday_rebuild", new_strikes=len(fresh))
        except Exception as exc:  # noqa: BLE001 - a failed rebuild keeps the overnight universe
            log.warning("universe.intraday_rebuild_failed", error=str(exc))

    async def _autopilot(self) -> None:
        try:
            await self.committee.autopilot_once()
        except Exception as exc:  # noqa: BLE001 - advisory; logged, never raised into the loop
            log.warning("committee.autopilot_failed", error=str(exc))

    async def _decide(self, bar: UnifiedBar) -> None:
        self._decided.add((bar.symbol, bar.ts))
        base_out = self.fudkii.on_bar(self.contexts[StrategyKey.FUDKII], bar)
        derived = Outcome()
        fukaa_ctx = self.contexts[StrategyKey.FUKAA]
        if base_out.signals:
            for sig in base_out.signals:
                derived.extend(self.fukaa.on_signal(fukaa_ctx, bar, sig))
        else:
            derived.extend(self.fukaa.on_bar(fukaa_ctx, bar))

        admitted, dropped = select(derived.signals, self.fukaa.cfg)
        for s in dropped:
            await self.ledger.insert_signal(s.to_json(), "SELECTION_DROPPED", "top-n / direction cap")

        for rej in (*base_out.rejections, *derived.rejections):
            await self.ledger.insert_rejection(rej.to_json())

        # Every trigger — a SuperTrend flip with the close through the band — is decided on the moment
        # it is generated, whether FUDKII publishes it as its own signal or not, and every path runs
        # side by side, not in series (operator, 2026-09-28: "push them in parallel not in series").
        # A published trigger reaches the in-trend books and the fade route (CT-X, CT-Y's gap fade)
        # as before; one FUDKII grades F and does not publish reaches the graded-F shadow alone
        # (UNPUBLISHED_BOOKS) until each book's own grade rule is proven (phase19). FUKAA runs beside
        # them. Each path is guarded: one path's error never costs another its entry.
        triggers = [*base_out.signals, *base_out.triggers]
        for sig in base_out.triggers:
            # its own row first: the parent's decision on its trigger, found by the cards and the
            # Shadow page, and never mistaken for a FUDKII signal by a later write (the first row
            # wins) (review, 2026-09-28). Known to today's book, so a card's TAKE finds it.
            parent = (sig.context or {}).get("parent") or {}
            await self.ledger.insert_signal(sig.to_json(), "NOT_PUBLISHED",
                                            f"FUDKII's own {parent.get('gate', 'grade')}: {parent.get('reason') or f'grade {sig.grade}'}")
            self._signals_today[sig.signal_id] = sig
        for sig in triggers:
            await self._log_breadth(sig)  # read by RT-Y's gate B and the gap fade: before either runs
        jobs = []
        for sig in triggers:
            if self._after_close(sig):
                # decided at or after the NSE close (the 15:15 bar completes at 15:30): no book can
                # trade it now — carried to the next session's 09:15 open (operator, 2026-09-29)
                jobs.append(self._guarded("carry.failed", sig, self._carry_queue(sig)))
            elif published(sig):  # the in-trend books keep the parent's grade
                jobs.append(self._guarded("signal.failed", sig, self._handle_signal(sig, bar, books=IN_TREND_BOOKS, adopt=True)))
                jobs.append(self._guarded("counter.failed", sig, self._handle_counter(sig, bar)))
            else:
                jobs.append(self._guarded("signal.failed", sig, self._handle_unpublished(sig, bar)))
        for sig in admitted:
            if self.fukaa.cfg.shadow and sig.strategy is StrategyKey.FUKAA:
                jobs.append(self._guarded("signal.failed", sig, self._fukaa_shadow(sig)))
            else:
                jobs.append(self._guarded("signal.failed", sig, self._handle_signal(sig, bar)))
        if jobs:
            await asyncio.gather(*jobs)

    async def _fukaa_shadow(self, sig: Signal) -> None:
        """FUKAA in shadow (operator, 2026-10-02): the signal it would trade, recorded with every
        input it read — never sent to a book, so its wallet never moves — until its thresholds are
        re-fitted on these readings."""
        try:
            alignment = self._fukaa_alignment(sig)
        except Exception as exc:  # noqa: BLE001 — a label never costs the shadow its record
            log.warning("fukaa.alignment_failed", symbol=sig.symbol, error=str(exc)[:120])
            alignment = {}
        await self.ledger.insert_signal(sig.to_json(), "SHADOW", f"FUKAA shadow — {sig.reason}")
        await self.ledger.event("fukaa.shadow", {"signal_id": sig.signal_id, "symbol": sig.symbol,
                                                 "direction": sig.direction.value, "entry": sig.entry, "stop": sig.stop,
                                                 "targets": list(sig.targets), "rr": sig.rr, "evidence": dict(sig.evidence),
                                                 "inputs": dict(sig.context.get("inputs") or {}), "alignment": alignment})
        log.info("fukaa.shadow", symbol=sig.symbol, signal=sig.signal_id, reason=sig.reason,
                 with_market=alignment.get("withMarket"), oi_quadrant=alignment.get("oiQuadrant"))

    def _fukaa_alignment(self, sig: Signal) -> dict[str, Any]:
        """The two directions the FUKAA study (operator, 2026-10-02) read on every trigger, logged
        with its shadow signal for the re-fit after the 27 Oct expiry — labels, never gates:

        * ``withMarket`` — breadth, the share of the NSE universe beyond its day open the signal's
          way, as logged at the parent trigger (what RT-Y's gate B reads): with the market above 50 %.
          In the study (NIFTY50 against the previous close) counter-trend alerts snapped back.
        * ``oiQuadrant`` — the price since the previous close against the OI change (long / short
          build-up, short covering, long unwinding); ``oiAgrees`` when OI builds the signal's way."""
        bull = sig.direction is Direction.BULLISH
        ctx = self._breadth_at.get(sig.source_signal_id) or self._breadth_at.get(sig.signal_id)
        if ctx is None:
            ctx = self.market_breadth(sig.direction)
        share = ctx.get("share")
        prev = previous_session(self.store.bars(sig.symbol, "1d", 40), ist_day(sig.ts))
        pch = round((sig.entry / prev.close - 1.0) * 100.0, 3) if prev is not None and prev.close else None
        oi = sig.evidence.get("oi_change_pct")
        quad = oi_quadrant(pch, oi)
        return {"breadth": share, "withMarket": None if share is None else share > 0.5, "priceChangePct": pch,
                "oiChangePct": oi, "oiQuadrant": quad,
                "oiAgrees": None if quad is None else quad == ("long build-up" if bull else "short build-up")}

    async def _handle_unpublished(self, sig: Signal, bar: UnifiedBar) -> None:
        """A trigger FUDKII does not publish, offered to ``UNPUBLISHED_BOOKS`` (the graded-F shadow).
        Each such book's own gates run FIRST, from what the engine already holds — the breadth, the
        pivots, the gap, the volume reading — and only a trigger a book would take goes on to the
        strike choice: at a 09:45 bar some twenty of these arrive with the one or two published
        triggers, and a strike choice each would subscribe and quote twenty chains for entries the
        gates then refuse. The book's decision is the same either way; the gates run again inside
        ``_enter_book_once`` on the same readings."""
        underlying = self.underlyings.get(sig.symbol)
        if underlying is None:
            return
        books = tuple(k for k in UNPUBLISHED_BOOKS if self.book_trades(k.value, underlying.segment))
        if not books:
            return  # an MCX trigger: no book here trades it, and RT-MCX takes only published ones
        takers = []
        for key in books:
            lim = self.limits_for(key.value)
            late = self._past_last_entry(key.value, underlying)
            if late:  # before any broker read: nothing past the book's last entry minute is bought
                await self._book_skip(key.value, sig, late, gate="entry_cutoff")
                continue
            # gate B first: breadth, a pivot just ahead and the 09:45 gap are in memory; the dried-volume
            # read below asks the broker for the future's candles — only for a trigger gate B passes
            ctx = self._trigger_ctx(sig.signal_id, sig.direction)
            early = rt_gate_reasons(ctx, lim)
            if early:
                gate, why = early[0]
                await self._book_skip(key.value, sig, why, gate=gate, gates=[g for g, _ in early], breadth=ctx.get("share"))
                continue
            vol = (
                asyncio.ensure_future(self._volume_surges_safe(underlying, low_priority=True))
                if lim.dried_volume_v
                else None
            )
            try:
                hit = await self._book_gates(key, sig, underlying, vol)
            finally:
                if vol is not None and not vol.done():
                    vol.cancel()
            if hit is None:
                takers.append(key)
                continue
            gate, why, extra = hit
            await self._book_skip(key.value, sig, why, gate=gate, **extra)
        if takers:
            await self._handle_signal(sig, bar, books=tuple(takers), adopt=False)

    @staticmethod
    async def _guarded(event: str, sig: Signal, job: Awaitable[Any]) -> None:
        """One of a bar's concurrent paths: its failure is logged and stays its own."""
        try:
            await job
        except Exception as exc:
            log.exception(event, symbol=sig.symbol, strategy=sig.strategy.value, error=str(exc))

    async def _handle_signal(
        self, sig: Signal, bar: UnifiedBar | None, *, adopt: bool = True, books: Sequence[StrategyKey] | None = None,
        take: bool = False,
    ) -> dict[str, dict[str, str]]:
        """One signal into the books that take it, each judging it for ITSELF, all at once (operator,
        2026-09-26: "a fudkii signal goes to all variants at the same time at once. each variant and
        twin assess it at the same time in parallel to decide if it fullfill teh specific logic of
        that particular strategy, ifyes, then it gets qualified for trade , if not, stat teh reason
        and record it").

        Decided ONCE, for every book, is what belongs to the trigger and the contract: the
        underlying, its segment, the strike (the selector's choice), the δ-projected levels and the
        future's volume reading — a reason there stops every book. Everything else is each book's
        own (``_enter_book``, run concurrently): its halt, its gates, its size from its own purse,
        its exposure, its money and its own order — a reason there stops that book alone.
        ``books`` defaults to the signal's own book (a fade's CT-X, CT-Y's gap fade, a routed
        commodity trigger, an operator take, FUKAA). Returns each book's outcome
        ``{book: {"decision", "reason"}}`` — what an operator TAKE shows."""
        decided_at = time.time()  # the signal's own timestamp on the order trail
        owner = sig.strategy.value
        keys = [k.value for k in books] if books else [owner]
        handled = f"{sig.signal_id}|{','.join(keys)}"
        if not take and handled in self._handled_signals:
            # the same signal for the same books a second time (a re-run): never twice. A TAKE is the
            # operator asking again on purpose — the per-book checks decide it (audit, 2026-09-26: a
            # FUDKII take was dropped in silence while the RT books' entries rested on its trigger)
            log.info("signal.already_handled", strategy=owner, symbol=sig.symbol, signal=sig.signal_id)
            return {k: {"decision": "ALREADY_HANDLED", "reason": "this trigger was already handled for this book"} for k in keys}
        self._handled_signals.add(handled)

        async def everyone(decision: str, why: str) -> dict[str, dict[str, str]]:
            await self.ledger.insert_signal(sig.to_json(), decision, why)
            return {k: {"decision": decision, "reason": why} for k in keys}

        self._signals_today[sig.signal_id] = sig
        await self.bus.publish(Topic.SIGNAL, sig)
        if adopt:
            self.alerts.adopt_signal(sig.to_json(), bar)
        underlying = self.underlyings.get(sig.symbol)
        if underlying is None:
            return await everyone("NO_UNDERLYING", "not in the universe")
        # a quiet name: the trigger and its alert stand, no book trades it (an operator TAKE still may)
        if not take and (quiet := self.quiet_reason(sig.symbol)):
            log.info("signal.quiet", symbol=sig.symbol, signal=sig.signal_id)
            return await everyone("QUIET", quiet)
        segment_book = next((k for k, seg in self.SEGMENT_BOOKS.items() if seg is underlying.segment), None)
        if (sig.strategy is StrategyKey.FUDKII and published(sig) and segment_book is not None
                and not self.book_trades(sig.strategy.value, underlying.segment)):
            # A commodity trigger. FUDKII's purse never trades MCX and RT-MCX only mirrored FUDKII
            # FILLS — so no commodity trigger was ever entered by anyone (every one was booked
            # WRONG_SEGMENT). It goes to the segment's own book directly, with the trigger as its
            # source; the ENTRY card is the trigger's, so it is not adopted twice.
            await self.ledger.insert_signal(sig.to_json(), "ROUTED", f"{segment_book} trades {underlying.segment.value}")
            routed = replace(sig, strategy=StrategyKey(segment_book), source_signal_id=sig.signal_id)
            log.info("signal.routed", symbol=sig.symbol, to=segment_book)
            return await self._handle_signal(routed, bar, adopt=False)

        gated = books is not None
        # The volume reading is the trigger's, read once for every book that gates on it (the
        # future's REST calls, not two per book) — and waited on by those books alone: a book
        # without the gate (FUDKII) places its order at once, and a failed read blocks no one
        # (review, 2026-09-26: read before the gather, FUDKII's order waited on it and an error in
        # it cost every book its entry). It needs the trigger, not the strike, so it runs WHILE the
        # strike is chosen — it used to start after (review, 2026-10-03).
        vol = (
            asyncio.ensure_future(self._volume_surges_safe(underlying, low_priority=not published(sig)))
            if gated and any(self.limits_for(k).dried_volume_v for k in keys)
            else None
        )
        selection = await self._select_instrument(underlying, sig)
        if not selection.ok or selection.instrument is None:
            if vol is not None:
                vol.cancel()
            log.info("signal.no_instrument", symbol=sig.symbol, reason=selection.reason)
            return await everyone("NO_INSTRUMENT", selection.reason)

        inst = selection.instrument
        # Subscribe the contract we are about to hold on BOTH channels. `mf` gives the LTP the exit
        # engine prices against; `md` gives the ladder the paper matcher walks. Without `md` every
        # paper fill silently took the degraded LTP+slippage path, which is the thing PaperMatcher
        # exists to avoid.
        await self.feed.subscribe("mf", [inst])
        await self._follow_depth([inst])
        delta = (
            estimate_delta(spot=sig.entry, strike=inst.strike, option_type=inst.option_type)
            if inst.is_option
            else 1.0
        )
        option_sl, option_targets = map_levels_to_option(
            equity_entry=sig.entry,
            equity_stop=sig.stop,
            equity_targets=sig.targets,
            option_premium=selection.premium,
            delta=delta,
        )
        results = await asyncio.gather(
            *(
                self._enter_book(
                    StrategyKey(k), sig, underlying, inst, selection.premium, delta, option_sl, option_targets, decided_at,
                    owns_row=k == owner, gated=gated, take=take, vol=vol,
                )
                for k in keys
            ),
            return_exceptions=True,
        )
        if vol is not None and not vol.done():
            vol.cancel()  # no gating book got as far as reading it
        out: dict[str, dict[str, str]] = {}
        for k, res in zip(keys, results, strict=True):
            if isinstance(res, BaseException):  # one book's fault never costs another book its entry
                log.error("book.entry_failed", book=k, symbol=sig.symbol, error=str(res), exc_info=res)
                out[k] = {"decision": "ERROR", "reason": str(res)[:200]}
            else:
                out[k] = res
        return out

    async def _enter_book(self, key: StrategyKey, sig: Signal, *args: Any, **kw: Any) -> dict[str, str]:
        """One trade per book per name, also while its entry is still being placed. The resting
        check and EXPOSURE see a resting entry and an open position, but not an entry between the
        two — a LIVE order waits on the broker for up to ~30 s, and a TAKE in that window was a
        second position on the same trigger (review, 2026-09-26). Claimed synchronously, released
        when the entry is resting, booked or refused."""
        slot = (key.value, sig.symbol)
        if slot in self._entering:
            return await self._enter_book_once(key, sig, *args, busy=True, **kw)
        self._entering.add(slot)
        try:
            return await self._enter_book_once(key, sig, *args, **kw)
        finally:
            self._entering.discard(slot)

    async def _enter_book_once(
        self, key: StrategyKey, sig: Signal, underlying: Instrument, inst: Instrument, premium: float, delta: float,
        raw_sl: float, option_targets: tuple[float, ...], decided_at: float, *, owns_row: bool, gated: bool,
        take: bool = False, vol: asyncio.Future[dict[str, tuple[float, float]]] | None = None, busy: bool = False,
    ) -> dict[str, str]:
        """ONE book's decision on a signal, and its own entry order. ``owns_row``: the signal is
        this book's (FUDKII on its trigger, CT-X on its fade) and its ledger row records the
        decision; otherwise the book takes another book's signal (RT-X/RT-N/RT-Y the trigger, CT-Y
        the fade) and its decision is recorded on its card. A book's size, money, halt and exposure
        are its own: the parent short of money or halted costs its twins nothing, and a twin sizes
        4 lots from its own purse whatever the parent could afford (operator, 2026-09-26).
        Returns the outcome ``{"decision", "reason"}``."""
        book_key = key.value
        label = BOOK_LABELS.get(book_key, book_key)

        async def refuse(decision: str, why: str, gate: str | None = None, **extra: Any) -> dict[str, str]:
            log.info("book.refused", book=book_key, symbol=sig.symbol, decision=decision, why=why)
            if owns_row:
                await self.ledger.insert_signal(sig.to_json(), decision, why)
            else:
                await self._book_skip(book_key, sig, why, gate=gate or decision.lower(), **extra)
            return {"decision": decision, "reason": why}

        if self._entry_resting(book_key, sig.signal_id, sig.symbol) is not None:
            return await refuse("ALREADY_RESTING", f"{label} already has an entry working on {sig.symbol}")
        if busy:
            # The entry being placed owns this signal's row and card and writes its outcome when it
            # fills; the first row written wins, so this attempt must not write one ahead of it.
            why = f"{label} is already placing an entry on {sig.symbol}"
            log.info("book.refused", book=book_key, symbol=sig.symbol, decision="ALREADY_ENTERING", why=why)
            await self.ledger.event("book.already_entering", {"book": book_key, "signal_id": sig.signal_id, "symbol": sig.symbol})
            return {"decision": "ALREADY_ENTERING", "reason": why}
        if key is StrategyKey.FUDKII and not published(sig):
            # the parent's own grade (or the last bar's fortress floor): not FUDKII's trade, recorded
            # as its decision on the trigger's row — its twins judge the same trigger themselves
            parent = (sig.context or {}).get("parent") or {}
            return await refuse("NOT_PUBLISHED", f"FUDKII's own {parent.get('gate', 'grade')}: {parent.get('reason') or f'grade {sig.grade}'}")
        late = self._past_last_entry(book_key, underlying)
        if late:
            return await refuse("PAST_ENTRY_CUTOFF", late, gate="entry_cutoff")
        halted, halt_why = self.halted()
        if halted:
            # refused HERE, before any order: sent, each book's order was rejected by the gateway
            # and counted toward the breaker (audit, 2026-09-26: 12 rejects in three halted triggers)
            return await refuse("ENGINE_HALTED", f"engine halted — {halt_why}")
        if self.gateway.book_tripped(book_key):
            return await refuse("BREAKER", f"{label}'s order breaker is tripped after {self.gateway.caps.breaker_consecutive_rejects} "
                                           "consecutive rejected orders — reset it on the Risk page")
        if not self.book_trades(book_key, underlying.segment):
            return await refuse("WRONG_SEGMENT", f"{label} does not trade {underlying.segment.value}")
        wallet = self.wallets[book_key]
        if wallet.halted:
            return await refuse("WALLET_HALTED", f"{label} halted — {wallet.halt_reason}")
        if gated and (hit := await self._book_gates(key, sig, underlying, vol)) is not None:
            gate, why, extra = hit
            return await refuse("SKIPPED", why, gate=gate, **extra)
        if (breach := self._stop_breached(sig, underlying, book_key)) is not None:
            return await refuse("STOP_BREACHED", breach)
        book = self._exits_by_strategy.get(book_key)
        book_limits = book.limits if book is not None else self.limits
        option_sl = self.floored_option_stop(key, premium, raw_sl, inst.tick_size) if inst.is_option else raw_sl
        exposure = self._exposure_by_strategy.get(book_key, self.exposure)
        sizing = size_position(
            instrument=inst,
            premium=premium,
            option_stop=option_sl,
            # The costs-against-T1 test belongs to the book that OWNS the signal and knows its T1 now
            # (FUDKII and FUKAA on their triggers, CT-X on its fade, CT-Y on its gap fade, a take).
            # A book taking another's signal on its own ladder (RT-X/N/Y, CT-Y on CT-X's fade) never
            # had it: its T1 is its contract's own level, and the owner's T1 is not its business
            # (operator: "the twins decide for themselves").
            option_target1=None if (book_limits.own_ladder and not owns_row) else (option_targets[0] if option_targets else None),
            balance=wallet.balance,
            available=wallet.available,
            limits=book_limits,
            costs=self.costs,
        )
        if not sizing.ok:
            return await refuse("NOT_SIZED", f"{label}: {sizing.reason}")
        verdict = exposure.check(
            strategy=book_key,
            underlying=sig.symbol,
            outlay=sizing.outlay,
            positions=list(self.positions.values()),
            total_capital=wallet.balance,  # each book is checked against its own purse
        )
        if not verdict.allowed:
            return await refuse("EXPOSURE", verdict.reason)
        now = time.time()
        ref = self._order_ref(book_key, now)
        intent = OrderIntent(
            strategy=book_key,
            instrument=inst,
            side=OrderSide.BUY,
            qty=sizing.qty,
            purpose=Purpose.ENTRY,
            signal_id=sig.signal_id,
            # "FII-RTX-260926-192510-007-EN-HINDUNILVR-1960CE-L4": the book, the IST date and time,
            # the book's order number that day, then what it is (operator, 2026-09-26)
            client_order_id=f"{ref}-{'TK' if take else 'EN'}-{_contract_tag(inst, sizing.qty)}",
            reason=sig.reason,
            ref_price=premium,
        )
        # Take the money BEFORE the first await. The sizer read `wallet.available` and the
        # exposure check read the open positions in this same synchronous block, but placing the
        # order and writing the ledger suspend the task — and every symbol's 30m bar closes at the
        # same instant, each in its own decision task. Left until after the fill, two entries on
        # one boundary both sized against the same rupees and the book deployed each of them. The
        # hold is provisional: released if nothing fills, and replaced by the fill's real cost.
        if not wallet.reserve(sizing.outlay, now):
            return await refuse("WALLET", f"₹{sizing.outlay:,.0f} outlay > ₹{wallet.available:,.0f} left in {label}")
        plan = _EntryPlan(
            sig=sig, underlying=underlying, inst=inst, option_sl=option_sl, option_targets=option_targets, delta=delta,
            outlay=sizing.outlay, wallet=wallet, book=book, book_limits=book_limits, decided_at=decided_at, ref=premium,
            key=book_key, owns_row=owns_row, order_ref=ref,
        )
        # Every entry ends in exactly one state: resting (the hold stays with the order), booked (the
        # hold became the position's cost) or released. Any error on the way releases a hold that
        # reached none of them (audit, 2026-09-26: an exception after the reserve leaked it).
        try:
            if self._limit_mode() and self.s.paper_limit_entries:
                await self._place_entry_limit(intent, plan)
                return {"decision": plan.outcome[0] or "RESTING", "reason": plan.outcome[1]}
            submitted_at = time.time()
            # while the order is at the broker (a live entry waits up to ~30 s) its hold is neither a
            # resting entry nor a position: the money check must still count it (audit, 2026-09-26)
            self._inflight_holds[intent.client_order_id] = (wallet, sizing.outlay)
            try:
                result = await self._submit(intent, verdict_ok=verdict.allowed, verdict_reason=verdict.reason)
            finally:
                self._inflight_holds.pop(intent.client_order_id, None)
            audit = self._market_audit("entry", decided_at, submitted_at, result, inst.scrip_code)
            if result.fill is not None:
                await self._book_entry(plan, result, audit)  # in memory first: a ledger error cannot lose it
                try:
                    await self.ledger.insert_order(_order_json(result.order, audit), result.decision.value)
                    if owns_row:
                        await self.ledger.insert_signal(sig.to_json(), result.decision.value, result.order.note or sig.reason)
                except Exception as exc:
                    log.exception("entry.record_failed", book=book_key, symbol=sig.symbol, error=str(exc))
                return {"decision": result.decision.value, "reason": f"{result.fill.qty} @ {result.fill.price:g}"}
            wallet.release(sizing.outlay, time.time())
            plan.released = True
            note = result.order.note or result.decision.value
            await self.ledger.insert_order(_order_json(result.order, audit), result.decision.value)
            if owns_row:
                await self.ledger.insert_signal(sig.to_json(), result.decision.value, note)
            else:
                await self._book_skip(book_key, sig, f"entry not filled — {note}", gate="not_filled")
            return {"decision": result.decision.value, "reason": note}
        except BaseException:
            if not plan.booked and not plan.released and intent.client_order_id not in self._resting:
                wallet.release(sizing.outlay, time.time())
                plan.released = True
            raise

    def _order_ref(self, book: str, now: float) -> str:
        """``FII-RTX-260926-192510-007``: the book's code, the IST date and time, and the book's
        order number that day — unique, and all within the 38 characters of an id the broker keeps
        (``rest.place_order``), so the descriptive rest of the id may be cut without a collision."""
        t = to_ist(now)
        day = t.strftime("%y%m%d")
        n = self._order_seq.get((book, day), 0) + 1
        self._order_seq[(book, day)] = n
        return f"{ORDER_CODES.get(book, book[:7].upper())}-{day}-{t.strftime('%H%M%S')}-{n:03d}"

    async def _load_order_seq(self) -> None:
        """The books' order numbers for today, from the ledger's orders and the loaded positions'
        refs — a restart never reuses one (a wide-stop shadow's ref is on no order until it exits)."""
        start = datetime.combine(ist_today(), datetime.min.time(), tzinfo=IST).timestamp()
        by_code = {v: k for k, v in ORDER_CODES.items()}
        rows = await self.ledger.rows_between("orders", start, start + 86_400)
        ids = [str(o.get("client_order_id") or "") for o in rows]
        ids += [str(p.exec_log.get("ref") or "") for p in self.positions.values()]
        # a book's live orders today — those that reached the broker — so a restart does not
        # hand it a fresh LIVE_CAPPED order allowance
        live = {m.value for m in LIVE_MODES}
        for o in rows:
            if o.get("mode") in live and o.get("decision") in (Decision.SUBMITTED.value, Decision.REJECTED_BROKER.value):
                b = str(o.get("strategy") or "")
                self.gateway.live_orders_by_book[b] = self.gateway.live_orders_by_book.get(b, 0) + 1
        for cid in ids:
            m = _ORDER_REF_RE.match(cid)
            if m and m.group(1) in by_code:
                key = (by_code[m.group(1)], m.group(2))
                self._order_seq[key] = max(self._order_seq.get(key, 0), int(m.group(3)))

    async def _load_breakers(self) -> None:
        """A tripped order breaker survives a restart: the ledger's trips and resets, replayed in
        order (a deploy used to clear every breaker). A run of rejects short of a trip starts again."""
        rows = await self.ledger.recent(
            events, 500, where=events.c.kind.in_(("gateway.book_breaker", "gateway.breaker_reset")),
        )
        tripped: set[str] = set()
        for r in reversed(rows):  # oldest first
            book = r.get("book")
            if r.get("reset"):
                if book:
                    tripped.discard(book)
                else:
                    tripped.clear()
            elif book:
                tripped.add(book)
        for b in tripped:
            self.gateway.tripped_books.add(b)
            self.gateway.rejects_by_book[b] = self.gateway.caps.breaker_consecutive_rejects
        if tripped:
            log.warning("gateway.breakers_restored", books=sorted(tripped))

    async def reset_breaker(self, book: str | None = None) -> dict[str, Any]:
        """The operator resets one book's order breaker, or every book's — recorded, so a restart
        does not bring back a breaker that was reset."""
        self.gateway.reset_breaker(book)
        await self.ledger.event("gateway.breaker_reset", {"book": book, "reset": True})
        log.warning("gateway.breaker_reset", book=book or "all")
        return self.gateway.stats()

    async def reconcile_now(self) -> dict[str, Any]:
        """The Risk page's "reconcile now": the broker's net positions against the LIVE books', at
        once. The buttons called the bar reconciler (``self.reconciler``), which has no ``run`` —
        a 500, and a freeze nobody could lift (review, 2026-09-26)."""
        if self.reconciler_positions is None:
            raise RuntimeError("no broker session")
        report = await self.reconciler_positions.run(self._venue_positions(), at_venue=self.mode() in LIVE_MODES)
        return {**report.to_json(), "frozen": self.reconciler_positions.frozen,
                "freeze_reason": self.reconciler_positions.freeze_reason}

    async def acknowledge_reconcile(self) -> dict[str, Any]:
        """Operator override: accept the broker's state as it is and let entries resume."""
        if self.reconciler_positions is None:
            raise RuntimeError("no broker session")
        previous = self.reconciler_positions.freeze_reason
        self.reconciler_positions.acknowledge()
        await self.ledger.event("reconcile.acknowledged", {"previous": previous})
        return {"frozen": self.reconciler_positions.frozen, "previous": previous}

    async def _book_gates(
        self, key: StrategyKey, sig: Signal, underlying: Instrument,
        reading: asyncio.Future[dict[str, tuple[float, float]]] | None = None,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """The book's own entry gates, from its own limits — ``(gate, why, extra)`` or None when the
        signal qualifies: dried volume (RT-X, RT-Y), gate B (RT-Y: breadth, a key pivot just ahead,
        a 09:45 gap with the trade), and CT-Y standing aside from a CT-X fade on a trigger it has
        already gap-faded. A reading that cannot be taken never blocks."""
        lim = self.limits_for(key.value)
        if lim.dried_volume_v:
            # shielded: one book's task cancelled never cancels the reading the others wait on
            vol = (await asyncio.shield(reading) if reading is not None
                   else await self._volume_surges_safe(underlying, low_priority=not published(sig)))
            dry = [leg for leg, (s_t, s_t1) in vol.items() if dried_volume(s_t, s_t1, v=lim.dried_volume_v)]
            if dry:
                why = "dried volume " + ", ".join(f"{leg} {vol[leg][0]:.2f}/{vol[leg][1]:.2f}" for leg in dry) + f" < {lim.dried_volume_v}"
                return "dried_volume", why, {}
        if lim.breadth_min is not None or lim.skip_pivot_ahead_atr is not None or lim.skip_open_gap_datr is not None:
            ctx = self._trigger_ctx(sig.signal_id, sig.direction)
            gates = rt_gate_reasons(ctx, lim)
            if gates:
                gate, why = gates[0]
                return gate, why, {"gates": [g for g, _ in gates], "breadth": ctx.get("share")}
        if key is StrategyKey.FUDKII_CT_Y and sig.source_signal_id and sig.source_signal_id in self._gap_faded:
            return "gap_fade", "CT-Y holds its own 09:45 gap fade on this trigger — not the CT-X fade as well", {}
        return None

    @staticmethod
    def _force_flat_hm(book: str, segment: Segment) -> str:
        """When ``book``'s positions in ``segment`` are flattened: the session's, or the book's own
        later NSE flatten (``FORCE_FLAT_HM``)."""
        own = FORCE_FLAT_HM.get(book) if segment is not Segment.MCX_FO else None
        return own or f"{spec(segment).force_flat:%H:%M}"

    def _past_force_flat(self, book: str, segment: Segment, now: float) -> bool:
        return ist_hm(now) >= self._force_flat_hm(book, segment) if book in FORCE_FLAT_HM else past_force_flat(segment, now)

    def _past_last_entry(self, book: str, underlying: Instrument, *, now: float | None = None) -> str | None:
        """Why ``book`` may not place a new entry now, or None: past its last NSE entry minute
        (``NSE_LAST_ENTRY_HM``, the graded-F shadow's own in ``LAST_ENTRY_HM``). MCX is not
        restricted here (its own session)."""
        if underlying.segment is Segment.MCX_FO:
            return None
        now = time.time() if now is None else now
        last = LAST_ENTRY_HM.get(book, NSE_LAST_ENTRY_HM)
        if ist_hm(now) <= last:
            return None
        return (f"past {BOOK_LABELS.get(book, book)}'s last NSE entry minute {last} IST ({to_ist(now):%H:%M:%S}) — "
                f"its positions are flattened from {self._force_flat_hm(book, underlying.segment)}")

    def _after_close(self, sig: Signal) -> bool:
        """A trigger decided at or after its NSE session's close — the 15:15 bar, complete at 15:30.
        MCX is not carried (its own session)."""
        und = self.underlyings.get(sig.symbol)
        if und is None or und.segment is Segment.MCX_FO:
            return False
        close = f"{spec(und.segment).close:%H:%M}"
        return ist_hm(sig.ts) < close <= ist_hm(sig.ts + TF_SECONDS[DECISION_TF])

    async def _carry_queue(self, sig: Signal) -> None:
        """A trigger decided after the close cannot be traded that day: it waits for the next
        session's open (operator, 2026-09-29: "Carry to next 09:15 open"), kept in the ledger so a
        restart overnight loses nothing, with the breadth it was logged with. A published trigger's
        row says so; an unpublished one keeps its NOT_PUBLISHED row (FUDKII's own grade)."""
        pub = published(sig)
        why = "after the close — the 15:15 bar is decided at 15:30; carried to the next session's 09:15 open"
        self._signals_today[sig.signal_id] = sig
        if pub:
            self.alerts.adopt_signal(sig.to_json(), None)
            await self.ledger.insert_signal(sig.to_json(), "CARRIED", why)
        await self.ledger.event("carry.queued", {
            "signal_id": sig.signal_id, "symbol": sig.symbol, "published": pub, "why": why,
            "signal": sig.to_json(), "breadth": self._breadth_at.get(sig.signal_id),
        })
        log.info("carry.queued", symbol=sig.symbol, signal=sig.signal_id, published=pub)

    async def _carry_load(self, day: date, open_ts: float) -> list[dict[str, Any]]:
        """The triggers carried to ``day``'s open: queued since the last session and not yet
        entered, dropped or expired (a restart during the open does not enter one twice)."""
        prev = self.calendar.previous_trading_day(day)
        since = from_ist(datetime.combine(prev, datetime.min.time()))  # from the last session's day
        rows = await self.ledger.rows_between("events", since, open_ts + CARRY_WINDOW_S + 3600)
        settled = {r.get("signal_id") for r in rows if r.get("kind") in ("carry.done", "carry.dropped", "carry.expired")}
        return [r for r in rows if r.get("kind") == "carry.queued" and r["ts"] < open_ts and r.get("signal_id") not in settled]

    async def _carry_tick(self, now: float) -> None:
        """At the NSE open, each trigger carried from the last session's close enters on its stock's
        first print of the day — ``_carry_enter`` decides, and drops one that opened through its
        stop. A stock that does not print within CARRY_WINDOW_S of the open (or an engine that was
        down through it) expires the trigger. Called every second by the clock."""
        if self.booting:
            return
        day = ist_day(now)
        if not self.calendar.is_trading_day(day):
            return
        open_ts = session_open_ts(Segment.NSE_EQ, day)
        if now < open_ts:
            return
        if self._carry_day != day:
            self._carry_day = day
            self._carry_pending = await self._carry_load(day, open_ts)
        for item in list(self._carry_pending):
            und = self.underlyings.get(item["symbol"])
            code = und.scrip_code if und is not None else ""
            px, at = self.ltps.get(code), self._ltp_traded_ts.get(code, 0.0)
            if px and at >= open_ts and now < open_ts + CARRY_WINDOW_S:
                self._carry_pending.remove(item)
                task = asyncio.get_running_loop().create_task(self._carry_enter(item, float(px), at))
                self._decision_tasks.add(task)
                task.add_done_callback(self._decision_tasks.discard)
            elif now >= open_ts + CARRY_WINDOW_S:
                self._carry_pending.remove(item)
                why = ("no print within 5 minutes of the open" if und is not None else "not in today's universe")
                await self.ledger.event("carry.expired", {"signal_id": item["signal_id"], "symbol": item["symbol"], "why": why})
                log.warning("carry.expired", symbol=item["symbol"], signal=item["signal_id"], why=why)

    async def _carry_enter(self, item: dict[str, Any], px: float, at: float) -> None:
        """One carried trigger at the open: through its stop, dropped; else — in favour (the open on
        the trade's side of yesterday's close) or in the zone (between the stop and that close) —
        re-issued at the open and routed as it would have been: a published trigger to the in-trend
        books, an unpublished one to the graded-F shadow. The re-issued signal takes the 08:45 slot,
        so its card reads "fired 09:15" and no FUDKII bar of the day can share its id; its entry is
        the open, and the targets it has already passed are dropped."""
        orig = _signal_from_json(item["signal"])
        sg = 1 if orig.direction is Direction.BULLISH else -1
        base = {"signal_id": orig.signal_id, "symbol": orig.symbol, "first_print": px, "at": at}
        if sg * (px - orig.stop) <= 0:
            await self.ledger.event("carry.dropped", {**base, "why": f"opened through the stop: first print {px:g}, stop {orig.stop:g}"})
            log.info("carry.dropped", symbol=orig.symbol, first_print=px, stop=orig.stop)
            return
        where = "in favour" if sg * (px - orig.entry) >= 0 else "in the zone"
        slot = int(session_open_ts(Segment.NSE_EQ, ist_day(at))) - TF_SECONDS[DECISION_TF]
        carried = replace(
            orig, ts=slot, entry=px, targets=tuple(t for t in orig.targets if sg * (t - px) > 0), gates=(),
            reason=f"carried from {to_ist(orig.ts + TF_SECONDS[DECISION_TF]):%d %b %H:%M} — {where} at the open {px:g} "
                   f"(close {orig.entry:g}, stop {orig.stop:g}) — {orig.reason}",
            context={**dict(orig.context), "carried": {"from": orig.signal_id, "firstPrint": px, "where": where, "prevClose": orig.entry}},
        )
        if item.get("breadth") is not None:
            self._breadth_at[carried.signal_id] = item["breadth"]  # the market as the trigger was logged
        self._signals_today[carried.signal_id] = carried
        await self.ledger.event("carry.done", {**base, "carried_id": carried.signal_id, "where": where, "published": item["published"]})
        log.info("carry.entering", symbol=orig.symbol, where=where, first_print=px, carried=carried.signal_id)
        if item["published"]:
            await self._handle_signal(carried, None, books=IN_TREND_BOOKS, adopt=True)
        else:
            parent = (orig.context or {}).get("parent") or {}
            await self.ledger.insert_signal(carried.to_json(), "NOT_PUBLISHED",
                                            f"carried — FUDKII's own {parent.get('gate', 'grade')}: {parent.get('reason') or f'grade {orig.grade}'}")
            await self._handle_unpublished(carried, None)

    def _stop_breached(self, sig: Signal, underlying: Instrument, book: str | None = None) -> str | None:
        """The trade is dead before it starts: the underlying is already through the stop ``book`` will
        hold (``_book_stop``: the signal's own, or the book's floored one). SBILIFE, 2026-09-24 09:45:
        the stop sat 10 paise under the close, the stock printed through it and every book was stopped
        out 67 ms after its fill. Checked before an entry is placed and on every look at a resting one.
        None when the price is not known."""
        ltp = self.ltps.get(underlying.scrip_code)
        stop = self._book_stop(sig, book)
        if not ltp or not stop:
            return None
        through = ltp <= stop if sig.direction is Direction.BULLISH else ltp >= stop
        return f"stop already breached — {sig.symbol} {ltp:g} through its stop {stop:g}" if through else None

    async def _book_skip(self, book: str, sig: Signal, why: str, *, gate: str, **extra: Any) -> None:
        """A book did not take a signal it was offered: recorded for its card with the reason. The
        event keeps its historical kind (``rt_twin.skipped``) — the card, the day book and the shadow
        page all read it."""
        log.info("book.skipped", book=book, symbol=sig.symbol, reason=why, gate=gate)
        self.alerts.mark_skipped(sig.signal_id, book=book, reason=why)
        await self.ledger.event("rt_twin.skipped", {
            "book": book, "signal_id": sig.signal_id, "symbol": sig.symbol, "reason": why, "gate": gate, **extra,
        })

    async def _book_entry(self, plan: _EntryPlan, result: Any, audit: dict[str, Any]) -> None:
        """A filled entry becomes the book's position — from an immediate fill or a resting limit
        alike: the book's wallet swaps its hold for the real cost. Each book entered on its own
        order; only a shadow (the wide-stop RT-Y) opens on another book's fill."""
        sig, inst, wallet, book, book_limits = plan.sig, plan.inst, plan.wallet, plan.book, plan.book_limits
        pos = Position(
            id=new_id("pos"),
            strategy=plan.key,
            instrument=inst,
            underlying=plan.underlying,
            side=PosSide.LONG,  # both books BUY premium; direction lives on `direction`
            qty=result.fill.qty,
            entry=result.fill.price,
            opened_ts=result.fill.ts,
            signal_id=sig.signal_id,
            direction=sig.direction,
            equity_entry=sig.entry,
            equity_sl=sig.stop,
            equity_targets=tuple(sig.targets),
            option_sl=plan.option_sl,
            option_targets=plan.option_targets,
            grade=sig.grade,
            note=f"delta≈{plan.delta:.2f} (estimated)",
            entry_charges=round(result.fill.charges, 2),
            exec_log={"entry": audit, "exits": [], **({"ref": plan.order_ref} if plan.order_ref else {})},
        )
        pos.equity_atr = round(atr(self.store.bars(sig.symbol, DECISION_TF, 60), 14) or 0.0, 4)
        self._floor_equity_stop(pos, book_limits, plan.key)  # before the premium cap, which still binds
        self._protect_option_stop(pos, book_limits, plan.key, result.fill.ts)
        if book is not None and book_limits.own_ladder:
            self._stamp_own_ladder(pos, inst, book_limits, sig.symbol)
        elif book_limits.targets_from_own_ladder:
            own, _, note = self._own_ladder_for(sig.symbol, inst, pos.entry, book_limits)
            if own:
                pos.option_targets = own
                pos.option_t1 = own[0]
            pos.note += " · targets: " + (note if own else "no own ladder, δ-projected")
        self.positions[pos.id] = pos
        # swap the provisional hold for what the fill actually cost
        wallet.release(plan.outlay, result.fill.ts)
        self._commit_outlay(wallet, pos.entry * pos.qty * inst.multiplier, result.fill.ts, pos)
        wallet.apply_charges(result.fill.charges, result.fill.ts)
        plan.booked = True
        try:
            await self.ledger.upsert_position(_position_json(pos))
            await self.ledger.upsert_wallet(wallet.strategy, wallet.to_json())
        except Exception as exc:
            # the position and the money are right in memory and are written again on the next
            # change; the order row, the card and the shadow must still follow (review, 2026-09-26)
            log.exception("position.record_failed", book=plan.key, symbol=sig.symbol, error=str(exc))
        if plan.key != StrategyKey.FUKAA.value:
            self.alerts.mark_entered(sig.signal_id, ts=result.fill.ts, price=pos.entry, qty=pos.qty, book=plan.key)
        # A shadow failing must never cost the real entry: the position is registered and its
        # wallet charged by this point, and the shadow is strictly additive.
        try:
            await self._open_shadow_twins(pos, inst, result.fill.ts, result.fill.charges, planned_sl=sig.stop)
        except Exception as exc:
            log.exception("shadow.failed", of=pos.id, symbol=pos.underlying.symbol, error=str(exc))
        log.info(
            "position.open",
            strategy=pos.strategy,
            symbol=pos.underlying.symbol,
            instrument=inst.name or inst.scrip_code,
            qty=pos.qty,
            entry=pos.entry,
            option_sl=pos.option_sl,
            grade=pos.grade,
            mode=self.mode().value,
        )
        if StrategyKey(pos.strategy) not in SHADOW_BOOKS:  # a shadow's entry is a comparison, not a trade to watch
            self.telegram.fire_and_forget(
                f"🟢 {pos.strategy} {pos.underlying.symbol} {sig.direction.value} "
                f"{inst.name or inst.scrip_code} qty {pos.qty} @ {pos.entry:.2f} "
                f"SL {pos.option_sl:.2f} grade {pos.grade} [{self.mode().value}]"
            )

    # -- paper limit orders (exec/resting.py) ---------------------------------------------------------

    def _limit_mode(self) -> bool:
        return self.mode() is Mode.PAPER and self.limit_policy.enabled

    def _touch(self, scrip_code: str, now: float) -> tuple[float | None, float | None, float | None, float | None]:
        """``(bid, ask, ltp, age_ms)`` — the depth book when it is fresh enough to trade on, else
        the snapshot quote, else nothing two-sided."""
        ltp = self.ltps.get(scrip_code)
        b = self.books.get(scrip_code)
        if b is not None and (b.best_bid or b.best_ask) and b.age_ms(now) <= self.matcher.age_limit_ms(now):
            return b.best_bid, b.best_ask, ltp, b.age_ms(now)
        q = self.quotes.get(scrip_code)
        if q is not None and (now - q.ts) <= self.s.position_quote_max_age_s:
            return (q.bid or None), (q.ask or None), ltp, (now - q.ts) * 1000
        return None, None, ltp, None

    def _depth(self, scrip_code: str, now: float) -> dict[str, Any] | None:
        """Five levels a side of the contract's depth book, price and quantity, and its age — for the order
        trail (operator, 2026-10-03: "check lots on sell"). None when the contract has no depth book."""
        b = self.books.get(scrip_code)
        if b is None or not (b.bids or b.asks):
            return None
        return {"bids": [[p, q] for p, q in b.bids[:5]], "asks": [[p, q] for p, q in b.asks[:5]], "ageMs": round(b.age_ms(now))}

    def _note_mid(self, scrip_code: str, now: float, mid: float) -> None:
        """A held option's mid, at most a read a second, kept two minutes."""
        h = self._mid_hist.setdefault(scrip_code, deque())
        if h and now - h[-1][0] < 1.0:
            return
        h.append((now, mid))
        while h and now - h[0][0] > 120.0:
            h.popleft()

    def _option_fall(self, scrip_code: str, now: float, mid: float | None) -> float | None:
        """The option's mid now against ``exit_fast_window_s`` ago, % — None without a read from then."""
        w = self.limit_policy.exit_fast_window_s
        h = self._mid_hist.get(scrip_code)
        if not h or not mid:
            return None
        then = [m for t, m in h if now - w - 15 <= t <= now - w]
        if not then or then[-1] <= 0:
            return None
        return (mid / then[-1] - 1) * 100

    def _entry_resting(self, strategy: str, signal_id: str, symbol: str) -> Resting | None:
        return next(
            (r for r in self._resting.values() if r.kind == "entry" and r.intent.strategy == strategy
             and (r.intent.signal_id == signal_id or r.ctx.sig.symbol == symbol)),
            None,
        )

    def _pending_for(self, book: str, signal_ids: set[Any]) -> dict[str, Any] | None:
        """The resting entry a card is waiting on: this book's own (the wide-stop shadow waits on
        RT-Y's)."""
        parents = {book, _MIRRORS.get(book, "")}
        now = time.time()
        for r in self._resting.values():
            if r.kind == "entry" and r.intent.signal_id in signal_ids and r.intent.strategy in parents:
                return r.audit(book=r.intent.strategy, restingS=round(now - r.placed_ts, 1),
                               contract=r.intent.instrument.name or r.intent.instrument.scrip_code)
        return None

    def _exit_resting(self, position_id: str) -> Resting | None:
        return next((r for r in self._resting.values() if r.kind == "exit" and r.intent.position_id == position_id), None)

    def _targets_resting(self, position_id: str) -> list[Resting]:
        """Every target sell resting for the position, lowest rung first."""
        return sorted((r for r in self._resting.values() if r.kind == "target" and r.intent.position_id == position_id),
                      key=lambda r: r.ctx[1])

    def _target_resting(self, position_id: str) -> Resting | None:
        """The NEXT rung's resting sell (the lowest), or None."""
        rs = self._targets_resting(position_id)
        return rs[0] if rs else None

    # -- target sells placed in advance (exec/resting.py, risk/exits.py) --------------------------------

    async def _ensure_resting_targets(self, pos: Position, now: float) -> None:
        """Keep EVERY rung's SELL resting for a book whose target is a touch — the whole ladder from the
        fill, each rung for its lots (operator, 2026-10-03: "adding all targets immediately as we know
        ... to make the most of first come first serve"; before, the next rung only).

        An order whose price still stands is KEPT, and with it its place in the queue, even when its rung
        number or size moved; a size is changed where the order rests. Only a rung whose price changed
        is re-placed ("in case there is an edit in target 2, let target 3 and 4 be as is"), and a rung no
        longer wanted is cancelled. Cancels and size cuts go first, new orders last, so the quantity on
        sale never exceeds what is held. While another exit of the position is working nothing moves."""
        if pos.id in self._target_sync:
            return  # a fill inside this sync re-enters it; the outer pass finishes the ladder
        if (pos.id in self._exits_in_flight or self._exit_resting(pos.id) is not None
                or now < self._exit_retry_at.get(pos.id, 0.0)):
            # an exit is working: it already took off what it needed (``_exit``), so the rungs still
            # resting keep their place in the queue until it is done
            return
        want: list[tuple[int, float, int]] = []
        if (
            self._limit_mode() and self.s.paper_limit_exits and self.limit_policy.rest_targets
            and pos.status == "OPEN" and pos.qty_remaining > 0 and self.positions.get(pos.id) is pos
        ):
            want = self._exits_by_strategy.get(pos.strategy, self.exits).resting_ladder(pos)
        cur = self._targets_resting(pos.id)
        if not want and not cur:
            return
        self._target_sync.add(pos.id)
        try:
            at_price: dict[float, list[Resting]] = {}
            for r in cur:
                at_price.setdefault(round(r.limit, 4), []).append(r)
            keep: list[tuple[Resting, int, int]] = []
            place: list[tuple[int, float, int]] = []
            for rung, price, qty in want:
                same = at_price.get(round(price, 4))
                if same:
                    keep.append((same.pop(0), rung, qty))
                else:
                    place.append((rung, price, qty))
            for r in [r for rs in at_price.values() for r in rs]:
                await self._cancel_target_order(pos, r, now, "the ladder moved on" if want else "no target to rest")
            for r, rung, qty in sorted(keep, key=lambda k: k[2] - k[0].intent.qty):  # size cuts before size raises
                if r.ctx[1] != rung or r.intent.qty != qty:
                    was = (r.ctx[1] + 1, r.intent.qty)
                    r.ctx = (pos, rung)
                    r.intent = replace(r.intent, qty=qty, reason=f"T{rung + 1} {r.limit:g} target sell, placed in advance")
                    if r.order is not None and hasattr(r.order, "qty"):
                        r.order.qty = qty
                    self._target_trail(pos, r, now, f"kept at {r.limit:g} — was T{was[0]} × {was[1]}")
            if sum(r.intent.qty for r, _, _ in keep) + sum(q for _, _, q in place) > pos.qty_remaining:
                log.error("target.ladder_oversold", position=pos.id, held=pos.qty_remaining, want=want)
                return  # never more on sale than is held — the ladder is rebuilt next pass
            fresh: list[Resting] = []
            for rung, price, qty in place:
                placed = await self._place_target(pos, rung, price, qty, now)
                if placed is not None:
                    fresh.append(placed)
            if fresh or keep:
                await self.ledger.upsert_position(_position_json(pos))
        finally:
            self._target_sync.discard(pos.id)
        for r in sorted(fresh, key=lambda r: r.ctx[1]):  # a rung the market is already through fills now, lowest first
            await self._advance_one(r, now, first=True)

    async def _place_target(self, pos: Position, rung: int, price: float, qty: int, now: float) -> Resting | None:
        ref = pos.exec_log.get("ref")
        if ref:
            k = int(pos.exec_log.get("tgtSeq", 0)) + 1
            pos.exec_log["tgtSeq"] = k
            cid = f"{ref}-T{rung + 1}V{k}-{_contract_tag(pos.instrument, pos.qty)}"
        else:  # a position opened before readable ids
            cid = f"{pos.id}|TGT|T{rung + 1}|{new_id('r')}"
        intent = OrderIntent(
            strategy=pos.strategy, instrument=pos.instrument, side=OrderSide.SELL, qty=qty, purpose=Purpose.EXIT,
            signal_id=pos.signal_id, client_order_id=cid,
            reason=f"T{rung + 1} {price:g} target sell, placed in advance", position_id=pos.id, ref_price=price, limit_price=price,
        )
        res = self.gateway.place_limit(intent)
        if res.decision is not Decision.RESTING:
            await self.ledger.insert_order(_order_json(res.order), res.decision.value)
            log.warning("target.not_placed", position=pos.id, rung=rung + 1, reason=res.order.note)
            return None
        bid, ask, _, _ = self._touch(pos.instrument.scrip_code, now)
        r = Resting(intent=intent, kind="target", limit=price, placed_ts=now, deadline_s=0.0, signal_ts=pos.opened_ts, ref=price,
                    why=f"T{rung + 1} sell placed in advance — fills on a touch", book_at_place=(bid, ask), last_check=now,
                    ctx=(pos, rung), depth_at_place=self._depth(pos.instrument.scrip_code, now))
        r.order = res.order
        self._resting[intent.client_order_id] = r
        self._target_trail(pos, r, now, "placed")
        log.info("limit.placed", kind="target", strategy=pos.strategy, symbol=pos.underlying.symbol, rung=rung + 1, limit=price, qty=qty)
        return r

    def _target_trail(self, pos: Position, r: Resting, now: float, outcome: str) -> None:
        """A resting target's life on the position's order trail: placed, kept, filled, cancelled (why)."""
        trail = pos.exec_log.setdefault("targets", [])
        trail.append({"rung": r.ctx[1] + 1, "limit": r.limit, "qty": r.intent.qty, "placedTs": r.placed_ts, "ts": now, "outcome": outcome})
        del trail[:-40]

    async def _cancel_target_order(self, pos: Position, r: Resting, now: float, why: str) -> None:
        """Take one resting target sell off the book."""
        if self._resting.pop(r.intent.client_order_id, None) is None:  # before the first await: nothing can fill it now
            return
        bid, ask, _, _ = self._touch(pos.instrument.scrip_code, now)
        res = self.gateway.cancel_resting(r.order, f"T{r.ctx[1] + 1} resting sell {r.limit:g} cancelled — {why}")
        audit = r.audit(cancelledTs=now, cancelReason=why, bookAtCancel={"bid": bid, "ask": ask},
                        depthAtCancel=self._depth(pos.instrument.scrip_code, now), waitS=round(now - r.placed_ts, 3), outcome="cancelled")
        self._target_trail(pos, r, now, f"cancelled — {why}")
        await self.ledger.insert_order(_order_json(res.order, audit), "TARGET_CANCELLED")
        log.info("target.cancelled", position=pos.id, rung=r.ctx[1] + 1, limit=r.limit, why=why)

    async def _cancel_resting_targets(self, pos: Position, now: float, why: str) -> None:
        """Take EVERY resting target sell of the position off before any other exit is sent — never two
        SELLs for the same lots (operator, 2026-10-03: "cancel the target sells first, then immediately
        exit")."""
        rs = self._targets_resting(pos.id)
        for r in reversed(rs):  # the highest first
            await self._cancel_target_order(pos, r, now, why)
        if rs and pos.status == "OPEN":
            await self.ledger.upsert_position(_position_json(pos))

    async def _make_room(self, pos: Position, qty: int, now: float, why: str) -> None:
        """A sell of ``qty`` is about to go out beside the resting targets: cancel from the highest rung
        down until targets and the sell together are no more than is held."""
        rs = self._targets_resting(pos.id)
        while rs and sum(r.intent.qty for r in rs) + qty > pos.qty_remaining:
            await self._cancel_target_order(pos, rs.pop(), now, why)

    async def _target_filled(self, r: Resting, now: float, price: float, bid: float | None, ask: float | None, age: float | None) -> None:
        """A resting target sell was touched: booked as a TARGET exit with the same state changes the
        exit engine's touch makes (risk/exits.py ``resting_target_filled``). Rungs book lowest first: a
        higher rung touched before the one below has booked waits for it; a rung below the ladder is
        stale and comes off."""
        pos, rung = r.ctx
        if pos.id in self._exits_in_flight:
            return  # another exit is being sent — it takes this order off the book itself
        if rung < pos.targets_hit:
            await self._cancel_target_order(pos, r, now, "the ladder moved on")
            return
        if rung > pos.targets_hit:
            return  # the rung below books first; this one stays where it rests
        self._resting.pop(r.intent.client_order_id, None)
        self._exits_in_flight.add(pos.id)  # an operator SKIP arriving mid-booking stands down, as for any exit
        try:
            mid = (bid + ask) / 2 if bid and ask else None
            result = self.gateway.fill_resting(r.order, r.intent, price=price, mid=mid, book_age_ms=age, now=now)
            decision = self._exits_by_strategy.get(pos.strategy, self.exits).resting_target_filled(pos, now, result.fill.price, mid)
            decision = replace(decision, qty=int(result.fill.qty))
            audit = r.audit(filledTs=now, fillPrice=result.fill.price, bookAtFill={"bid": bid, "ask": ask},
                            depthAtFill=self._depth(pos.instrument.scrip_code, now), waitS=round(now - r.placed_ts, 3),
                            outcome=f"T{rung + 1} resting limit filled")
            self._target_trail(pos, r, now, "filled")
            log.info("limit.filled", kind="target", strategy=pos.strategy, symbol=pos.underlying.symbol, rung=rung + 1,
                     price=result.fill.price, qty=result.fill.qty)
            await self.ledger.insert_order(_order_json(result.order, audit), result.decision.value)
            await self._book_exit(pos, decision, result, now, self._exit_attempts.get(pos.id, 0), audit)
        finally:
            self._exits_in_flight.discard(pos.id)
        if pos.status == "OPEN":
            await self._ensure_resting_targets(pos, now)  # the rest of the ladder re-checked, as it stands

    def _market_audit(self, kind: str, signal_ts: float, placed_ts: float, result: Any, scrip_code: str) -> dict[str, Any]:
        """The trail of an immediate fill (limit orders off, or a cross): when, at what, against which book."""
        bid, ask, _, _ = self._touch(scrip_code, placed_ts)
        fill = getattr(result, "fill", None)
        return {
            "kind": kind, "signalTs": signal_ts, "placedTs": placed_ts, "limit": None, "why": "market — walked the book",
            "bookAtPlace": {"bid": bid, "ask": ask}, "depthAtPlace": self._depth(scrip_code, placed_ts), "reprices": [],
            "filledTs": fill.ts if fill else None, "fillPrice": fill.price if fill else None,
            "waitS": round(fill.ts - placed_ts, 3) if fill else None, "outcome": "filled" if fill else "not filled",
        }

    async def _place_entry_limit(self, intent: OrderIntent, plan: _EntryPlan) -> None:
        """BUY LIMIT at the signal price if the book still straddles it, else at the mid — never
        over the cap (the signal price + ``entry_cap_pct``)."""
        now = time.time()
        bid, ask, _, _ = self._touch(plan.inst.scrip_code, now)
        tick = plan.inst.tick_size or 0.05
        cap = entry_cap(plan.ref, self.limit_policy.entry_cap_pct, tick)
        limit, why = entry_limit(plan.ref, bid, ask, tick, cap)
        if limit is None:
            limit, why = plan.ref, "at the signal price — no book to read"
        intent = replace(intent, limit_price=limit)
        res = self.gateway.place_limit(intent)
        if res.decision is not Decision.RESTING:
            if plan.wallet is not None:
                plan.wallet.release(plan.outlay, now)
            plan.released = True
            plan.outcome = (res.decision.value, res.order.note or res.decision.value)
            await self.ledger.insert_order(_order_json(res.order), res.decision.value)
            if plan.owns_row:
                await self.ledger.insert_signal(plan.sig.to_json(), res.decision.value, res.order.note or plan.sig.reason)
            else:
                await self._book_skip(plan.key, plan.sig, f"entry refused — {res.order.note or res.decision.value}", gate="order_refused")
            return
        r = Resting(intent=intent, kind="entry", limit=limit, placed_ts=now, deadline_s=self.limit_policy.entry_wait_s,
                    signal_ts=plan.decided_at, ref=plan.ref, why=why, book_at_place=(bid, ask), last_check=now, ctx=plan,
                    depth_at_place=self._depth(plan.inst.scrip_code, now))
        r.order = res.order
        self._resting[intent.client_order_id] = r
        plan.outcome = ("RESTING", f"limit {limit:g} {why}")
        if plan.owns_row:
            await self.ledger.settle_signal(
                plan.sig.to_json(), Decision.RESTING.value,
                f"limit {limit:g} {why} (book {bid if bid else '—'}/{ask if ask else '—'})",
            )
        log.info("limit.placed", kind="entry", strategy=intent.strategy, symbol=plan.sig.symbol, limit=limit, ref=plan.ref,
                 bid=bid, ask=ask, why=why)
        await self._advance_one(r, now, first=True)

    async def _advance_resting(self, now: float) -> None:
        """Every resting limit, once per exit-loop tick: filled, repriced, crossed or cancelled — each
        position's target sells lowest rung first, so a jump through several books them in order."""
        for r in sorted(self._resting.values(), key=lambda r: (r.kind == "target", r.ctx[1] if r.kind == "target" else 0)):
            try:
                await self._advance_one(r, now)
            except Exception as exc:  # one order's fault must not stall the others
                log.exception("limit.advance_failed", order=r.intent.client_order_id, error=str(exc))

    async def _advance_one(self, r: Resting, now: float, *, first: bool = False) -> None:
        key = r.intent.client_order_id
        if self._resting.get(key) is not r:
            return
        code = r.intent.instrument.scrip_code
        buy = r.intent.side is OrderSide.BUY
        bid, ask, ltp, age = self._touch(code, now)
        if r.kind == "target":
            pos, _rung = r.ctx
            if pos.status != "OPEN" or pos.qty_remaining <= 0 or self.positions.get(pos.id) is not pos:
                await self._cancel_resting_targets(pos, now, "the position is closed")
            # a touch, on a book or quote fresh enough to trade on (age None = nothing fresh)
            elif age is not None and touch_fills(r.limit, bid, ltp):
                await self._target_filled(r, now, max(r.limit, bid) if (first and bid) else r.limit, bid, ask, age)
            return
        if r.kind == "entry":
            # Before any fill: an entry whose book (or the engine) has halted, or whose stop the
            # underlying is already through, is cancelled — never filled (audit, 2026-09-26: a
            # book halted while its entry rested still bought; SBILIFE bought through its stop).
            plan: _EntryPlan = r.ctx
            halted, why = self.halted()
            if not halted and plan.wallet is not None and plan.wallet.halted:
                halted, why = True, f"{BOOK_LABELS.get(plan.key, plan.key)} halted — {plan.wallet.halt_reason}"
            if not halted and self.gateway.book_tripped(plan.key):
                halted, why = True, f"{BOOK_LABELS.get(plan.key, plan.key)}'s order breaker is tripped"
            dead = self._stop_breached(plan.sig, plan.underlying, plan.key)
            if halted or dead:
                await self._entry_missed(r, now, bid, ask, dead or why)
                return
        # only a print the order has not seen yet is a trade through it; a stale last price is not
        fresh_ltp = ltp if (not first and ltp is not None and ltp != r.ltp_seen) else None
        r.ltp_seen = ltp
        if fills(buy, r.limit, bid, ask, fresh_ltp):
            # at placement a marketable limit takes the touch (price improvement); resting, it fills at its own price
            price = r.limit
            if first and buy and ask and ask < r.limit:
                price = ask
            elif first and not buy and bid and bid > r.limit:
                price = bid
            self._resting.pop(key, None)
            mid = (bid + ask) / 2 if bid and ask else None
            result = self.gateway.fill_resting(r.order, r.intent, price=price, mid=mid, book_age_ms=age, now=now)
            audit = r.audit(filledTs=now, fillPrice=result.fill.price, bookAtFill={"bid": bid, "ask": ask},
                            depthAtFill=self._depth(code, now), waitS=round(now - r.placed_ts, 3), outcome="filled at the limit")
            log.info("limit.filled", kind=r.kind, strategy=r.intent.strategy, symbol=r.intent.instrument.symbol,
                     price=result.fill.price, wait_s=round(now - r.placed_ts, 1))
            if r.kind == "entry":
                await self._entry_filled(r, result, audit)
            else:
                await self._exit_filled(r, result, audit)
            return
        elapsed = now - r.placed_ts
        tick = r.intent.instrument.tick_size or 0.05
        if r.kind == "entry":
            pol = self.limit_policy
            cap = entry_cap(r.ref, pol.entry_cap_pct, tick)
            if (pol.entry_race_pct is not None and not r.race_checked and elapsed >= pol.entry_race_check_s
                    and elapsed < r.deadline_s):
                # the one look: the OPTION racing ahead on its signal price takes the ask, under the cap
                r.race_checked = True
                run = option_run_pct(r.ref, bid, ask, ltp)
                go, note = race_call(run, ask, cap, pol)
                r.momentum.append({"atS": round(elapsed, 1), "runPct": round(run, 2) if run is not None else None, "note": note})
                if go and ask:
                    log.info("limit.momentum", strategy=r.intent.strategy, symbol=r.intent.instrument.symbol, run_pct=run,
                             ask=ask, ref=r.ref, cap=cap)
                    await self._entry_take_ask(r, now, bid, ask, age, f"crossed at the ask after {elapsed:.0f} s — {note}")
                    return
            if (pol.entry_cross_after_hold and elapsed >= pol.entry_hold_s and elapsed < r.deadline_s
                    and ask and ask > 0 and (cap is None or ask <= cap)):
                # "fill order at the least price within 3% cap as soon as possible": the ask is the
                # lowest price it can be bought at now, and it is within the cap
                run = option_run_pct(r.ref, bid, ask, ltp)
                ran = f", the option {run:+.1f}% on its signal price" if run is not None else ""
                log.info("limit.crossed_after_hold", strategy=r.intent.strategy, symbol=r.intent.instrument.symbol, ask=ask, ref=r.ref, cap=cap)
                await self._entry_take_ask(r, now, bid, ask, age,
                                           f"crossed at the ask {ask:g} after {elapsed:.0f} s — within the cap {cap if cap is not None else '—'}{ran}")
                return
            if elapsed >= r.deadline_s:
                chase = pol.entry_chase_pct
                if chase is not None and ask and r.ref and ask <= r.ref * (1 + chase / 100) and (cap is None or ask <= cap):
                    # not filled at the limit, but the market is still within `chase`% of the signal
                    # price: take the ask rather than lose the trigger (the misses were the winners)
                    log.info("limit.chased", strategy=r.intent.strategy, symbol=r.intent.instrument.symbol, ask=ask, ref=r.ref)
                    await self._entry_take_ask(r, now, bid, ask, age,
                                               f"crossed at the ask after {r.deadline_s:g} s (within {chase:g}% of the signal price)")
                    return
                run = option_run_pct(r.ref, bid, ask, ltp)
                ran = f" — the option {run:+.1f}% on its signal price" if run is not None else ""
                await self._entry_missed(r, now, bid, ask, f"limit not filled in {r.deadline_s:g} s{ran}")
                return
            if elapsed >= pol.entry_hold_s and not pol.entry_cross_after_hold and now - r.last_check >= pol.entry_recheck_s:
                # after the hold, a signal price that has left the book is followed to the mid — under the
                # cap. Not when crossing after the hold: a limit raised to the cap would fill AT the cap
                # when the ask dips under it, where taking the ask pays the lower price
                r.last_check = now
                if bid and ask and r.ref is not None and not (bid <= r.ref <= ask):
                    new, _ = entry_limit(r.ref, bid, ask, tick, cap)
                    if new is not None and new != r.limit:
                        r.limit = new
                        r.intent = replace(r.intent, limit_price=new)
                        r.reprices.append((now, new))
            return
        if elapsed >= r.deadline_s:
            await self._exit_cross(r, now, bid, ask)
            return
        if now - r.last_check >= self.limit_policy.exit_reprice_s:
            r.last_check = now
            new = exit_limit(bid, ask, elapsed, r.deadline_s, tick)
            if new is not None and new != r.limit:
                r.limit = new
                r.intent = replace(r.intent, limit_price=new)
                r.reprices.append((now, new))

    async def _entry_take_ask(self, r: Resting, now: float, bid: float | None, ask: float, age: float | None, outcome: str) -> None:
        """An unfilled entry buys the ask — the race or the chase rule's call."""
        self._resting.pop(r.intent.client_order_id, None)
        mid = (bid + ask) / 2 if bid and ask else None
        result = self.gateway.fill_resting(r.order, r.intent, price=ask, mid=mid, book_age_ms=age, now=now)
        audit = r.audit(filledTs=now, fillPrice=result.fill.price, bookAtFill={"bid": bid, "ask": ask},
                        depthAtFill=self._depth(r.intent.instrument.scrip_code, now), waitS=round(now - r.placed_ts, 3), outcome=outcome)
        await self._entry_filled(r, result, audit)

    async def _entry_filled(self, r: Resting, result: Any, audit: dict[str, Any]) -> None:
        plan: _EntryPlan = r.ctx
        plan.outcome = (result.decision.value, f"{result.fill.qty} @ {result.fill.price:g}")
        # the position first, in memory: a ledger error after it cannot lose a fill (audit, 2026-09-26)
        await self._book_entry(plan, result, audit)
        try:
            await self.ledger.insert_order(_order_json(result.order, audit), result.decision.value)
            if plan.owns_row:
                await self.ledger.settle_signal(plan.sig.to_json(), result.decision.value, plan.sig.reason)
        except Exception as exc:
            log.exception("entry.record_failed", book=plan.key, symbol=plan.sig.symbol, error=str(exc))

    async def _entry_missed(self, r: Resting, now: float, bid: float | None, ask: float | None, why: str) -> None:
        """No fill: the book's trigger is recorded as missed, with the book it was missed in."""
        self._resting.pop(r.intent.client_order_id, None)
        plan: _EntryPlan = r.ctx
        if plan.wallet is not None:
            plan.wallet.release(plan.outlay, now)
        plan.released = True
        note = f"{why} — limit {r.limit:g}, book {bid if bid else '—'}/{ask if ask else '—'} at cancel"
        plan.outcome = (Decision.LIMIT_UNFILLED.value, note)
        res = self.gateway.cancel_resting(r.order, note)
        audit = r.audit(cancelledTs=now, cancelReason=why, bookAtCancel={"bid": bid, "ask": ask},
                        depthAtCancel=self._depth(r.intent.instrument.scrip_code, now), waitS=round(now - r.placed_ts, 3), outcome="missed")
        await self.ledger.insert_order(_order_json(res.order, audit), res.decision.value)
        if plan.owns_row:
            await self.ledger.settle_signal(plan.sig.to_json(), res.decision.value, note)
        else:
            await self._book_skip(plan.key, plan.sig, f"entry missed — {note}", gate="missed")
        await self.ledger.event("limit.missed", {"signal_id": plan.sig.signal_id, "symbol": plan.sig.symbol,
                                                 "book": r.intent.strategy, "limit": r.limit, "ref": r.ref, "why": why,
                                                 "bid": bid, "ask": ask, "wait_s": round(now - r.placed_ts, 3)})
        log.info("limit.missed", strategy=r.intent.strategy, symbol=plan.sig.symbol, limit=r.limit, why=why)

    async def _load_leg_pivots(self, groups: Any) -> None:
        """Previous-session pivots for the future and eight OTM strikes of every F&O name.

        Deliberately off the boot path: roughly two thousand REST calls, and the open should not
        wait on them. Publishes per leg as it goes, so a name is usable the moment its own legs
        land rather than when the last one does.
        """
        try:
            legs = self._expected_legs(groups)
            if legs:
                await self.leg_pivots.load(legs, ist_today())
                self._seed_iv_history()
        except Exception as exc:  # noqa: BLE001 - advisory levels never stall the engine
            log.warning("legs.load_failed", error=str(exc))

    def _seed_iv_history(self) -> None:
        """A name's IV history from the candles the legs already fetched: the nearest call and
        put of the front expiry, each past session's close against the parent's close that day.
        Approximate — the strike was not at the money every day — and only for names with fewer
        than MIN_HISTORY live sessions, which the live points then replace."""
        today = ist_today()
        seeded = 0
        for g in self.groups.values():
            if not g.option_expiry or g.equity is None:
                continue
            _, n = self.iv_history.median_before(g.root, today)
            if n >= MIN_HISTORY:
                continue
            spots = {ist_day(b.ts).isoformat(): b.close for b in self.store.bars(g.root, "1d") if is_official(b)}
            spot_now = self.ltps.get(g.equity.scrip_code) or g.close or 0.0
            series = []
            for kind in ("CE", "PE"):
                same = [lp for lp in self.leg_pivots.for_root(g.root) if lp.kind == kind and lp.closes]
                if not same:
                    continue
                near = min(same, key=lambda lp: abs(lp.strike - spot_now))
                rows = [{"dt": d, "c": c} for d, c in sorted(near.closes.items())]
                series.append(seed_points(rows, spots, strike=near.strike, call=kind == "CE", expiry=g.option_expiry))
            pts = merge_points(*series) if series else []
            if pts and self.iv_history.seed(g.root, pts):
                seeded += 1
        if seeded:
            self.iv_history.save_all()
        log.info("iv.seeded", names=seeded)

    def _refresh_stock_iv(self) -> None:
        """Every name's ATM implied vol from the quotes in hand — its own VIX, once a minute."""
        now = time.time()
        today = ist_today()
        for g in self.groups.values():
            if not g.option_expiry or g.equity is None or not g.options:
                continue
            spot = self.ltps.get(g.equity.scrip_code)
            if not spot:
                continue
            strike = min({o.strike for o in g.options}, key=lambda k: abs(k - spot))
            mids: dict[OptionType, float] = {}
            for o in g.options:
                if o.strike != strike:
                    continue
                q = self.quotes.get(o.scrip_code)
                if q is None or now - q.ts > 120 or q.mid <= 0:
                    continue
                mids[o.option_type] = q.mid
            iv = atm_iv(spot, strike, years_to_expiry(g.option_expiry, today), mids.get(OptionType.CE), mids.get(OptionType.PE))
            if iv is None:
                continue
            self.stock_iv[g.root] = (iv, now)
            self.iv_history.record(g.root, today, iv)

    def name_regime(self, symbol: str) -> Any:
        """The name's own implied regime — today's ATM IV against its own median — or India VIX's
        regime while it has too little history to be banded against itself."""
        iv = self.stock_iv.get(symbol)
        med, n = self.iv_history.median_before(symbol, ist_today())
        return regime_for_name(iv[0] if iv else None, med, n, fallback=regime_for_equity(self.india_vix()))

    def option_ladder_tolerance(self, symbol: str, strike: float, option_type: OptionType, premium: float) -> tuple[float, Any]:
        """The option ladder's merge tolerance: the parent's k × ATR30 through delta, over the
        premium. Falls back to the flat percent when the parent has no ATR yet."""
        from .instrument.select import estimate_delta

        reg = self.name_regime(symbol)
        atr_v = atr(self.store.bars(symbol, DECISION_TF, 60), 14) or 0.0
        spot = self.ltps.get(getattr(self.underlyings.get(symbol), "scrip_code", "")) or 0.0
        delta = abs(estimate_delta(spot=spot, strike=strike, option_type=option_type)) if spot else 0.5
        tol = ladder_tolerance_pct(reg.k, atr_v, delta, premium)
        return (tol if tol > 0 else OPTION_CLUSTER_TOL_PCT), reg

    def stock_iv_snapshot(self, symbol: str) -> dict[str, Any]:
        iv = self.stock_iv.get(symbol)
        med, n = self.iv_history.median_before(symbol, ist_today())
        reg = self.name_regime(symbol)
        return {
            "atmIv": round(iv[0], 4) if iv else None,
            "ts": iv[1] if iv else None,
            "medianIv": round(med, 4) if med else None,
            "sessions": n,
            "band": reg.band.value,
            "clusterK": reg.k,
            "source": reg.source,
            "detail": reg.detail,
        }

    def _expected_legs(self, groups: Any) -> list[Instrument]:
        """The front future and the eight OTM strikes of every F&O name — the ladders the book reads."""
        legs: list[Instrument] = []
        cat = self.catalogue_loader.catalogue
        for g in groups:
            if g.futures:
                legs.append(g.futures[0])
            if not g.option_expiry:
                continue
            spot = (self.ltps.get(g.equity.scrip_code) if g.equity else None) or g.close or 0.0
            # The FULL chain, not g.options: that is the subscribed shortlist — five strikes a
            # side around spot, so half of it is in the money and the OTM filter leaves fewer
            # than eight. The catalogue has every listed strike.
            chain = [
                o
                for ot in (OptionType.CE, OptionType.PE)
                for o in cat.chain(g.root, g.option_expiry, ot)
            ]
            legs.extend(otm_legs(chain=chain, spot=spot))
        return legs

    def market_breadth(self, direction: Direction, at_ts: int | None = None) -> dict[str, Any]:
        """Does the market agree with the breakout? The share of the NSE universe (indices
        included, as in the replay) trading beyond its own day open in ``direction``: each name's
        live price — its latest 30m close before any tick — against the open of its first 30m
        bar today. A bearish name is one at or below its open. ``share`` is None when fewer than
        20 names have a bar today (a gate reading None never blocks)."""
        start = session_open_ts(Segment.NSE_EQ, ist_today())
        agree = n = 0
        for sym, und in self.underlyings.items():
            if und.segment is Segment.MCX_FO:
                continue
            recent = self.store.bars(sym, DECISION_TF, 16)
            today = [b for b in recent if b.ts >= start]
            forming = self.store.forming(sym, DECISION_TF)
            if forming is not None and forming.ts >= start and (not today or forming.ts > today[-1].ts):
                today.append(forming)
            if not today:
                continue
            px = self.ltps.get(und.scrip_code) or today[-1].close
            n += 1
            agree += (px > today[0].open) if direction is Direction.BULLISH else (px <= today[0].open)
        out: dict[str, Any] = {"share": round(agree / n, 3) if n >= 20 else None, "agree": agree, "names": n, "direction": direction.value, "ts": time.time()}
        if at_ts is not None:
            # the market's own volume at the trigger's bar: is the whole tape quiet, or this stock?
            mv = self._market_volume(int(at_ts))
            if mv.names - mv.doubtful >= 20 and mv.median_t is not None and mv.median_t1 is not None:
                out["mktSurgeT"] = round(mv.median_t, 3)
                out["mktSurgeT1"] = round(mv.median_t1, 3)
            if mv.alarm:
                out["mktVolAlarm"] = mv.alarm
        return out

    def trigger_context(self, sig: Signal) -> dict[str, Any]:
        """Labels stamped on every trigger for the paper A/B — logged, not gated (operator,
        2026-09-26). Each is what the Sep 1–25 replay measured, the same way:

        * ``efficiency`` — net move over the last 8 closed 30m bars / the path travelled, signed
          the trade's way (1 = a straight line, ~0 = chop). Inconsistent across halves: a label.
        * ``volBand`` — the MCX own-volatility logic on the stock's daily ATR% (vs its 20-session
          median). No band separated winners from losers in Sep: a label.
        * ``gapDatr`` — today's first 30m open against the previous close, in daily ATRs, signed
          the trade's way. 09:45 triggers gapping WITH the trade (>= 0.3) averaged −2.0 % / −5.7 %
          in the two halves, against −0.7 % / −1.3 % without.
        * ``pivotsAhead`` — classic 1d/1wk/1mo key levels within 0.5 ATR30 AHEAD of the close.
          Trades with one averaged −1.5 % / −1.6 % against +0.1 % / +1.0 % without.
        * ``openBar`` — the trigger is the session's first 30m bar (fires at 09:45)."""
        sign = 1 if sig.direction is Direction.BULLISH else -1
        out: dict[str, Any] = {}
        bars = [b for b in self.store.bars(sig.symbol, DECISION_TF, 40) if b.ts <= sig.ts][-8:]
        if len(bars) >= 3:
            path = abs(bars[0].close - bars[0].open) + sum(abs(b.close - a.close) for a, b in pairwise(bars))
            out["efficiency"] = round(sign * (bars[-1].close - bars[0].open) / path, 3) if path > 0 else 0.0
        und = self.underlyings.get(sig.symbol)
        seg = und.segment if und else Segment.NSE_EQ
        reg = regime_for_commodity(self._atr_pct_history(sig.symbol))
        out["volBand"] = reg.band.value if reg.source == "realised" else None
        today = ist_today()
        start = session_open_ts(seg, today)
        out["openBar"] = int(sig.ts) == int(start)
        recent = self.store.bars(sig.symbol, DECISION_TF, 40)
        if recent and int(recent[-1].ts) == int(sig.ts):
            vr = self._volume_reading(sig.symbol, sig.ts, recent)
            if vr.ok:
                out["volSurgeT"], out["volSurgeT1"] = round(vr.surge_t, 3), round(vr.surge_t1, 3)  # type: ignore[arg-type]
            else:
                out["volDoubt"] = vr.doubt
        dailies = list(self.store.bars(sig.symbol, "1d", 40))
        prev = previous_session(dailies, today)
        first = next((b for b in self.store.bars(sig.symbol, DECISION_TF, 20) if b.ts >= start), None)
        datr = atr([b for b in dailies if b.ts <= prev.ts], 14) if prev is not None else None
        if prev is not None and first is not None and datr:
            out["gapDatr"] = round(sign * (first.open - prev.close) / datr, 3)
        atr30 = atr(self.store.bars(sig.symbol, DECISION_TF, 60), 14) or 0.0
        if atr30 > 0:
            ahead = sorted(
                (
                    # 4 dp: a gate compares against its reach, and a level at 0.503 ATR rounded to
                    # 0.50 was read as "within 0.5" (GAIL, ADANIENT in the 19-25 Sep parity check)
                    (p.label, round(sign * (p.price - sig.entry) / atr30, 4))
                    for p in self._pivot_points(sig.symbol)
                    if p.label.split(".", 1)[-1] in KEY_LEVELS and 0 <= sign * (p.price - sig.entry) <= PIVOT_SCAN_ATR * atr30
                ),
                key=lambda x: x[1],
            )
            out["pivotsAhead"] = [f"{lab} +{round(d, 2):g} ATR" for lab, d in ahead if d <= PIVOT_AHEAD_ATR]
            out["pivotsAheadAtr"] = [[lab, d] for lab, d in ahead]  # what a gate with its own reach reads
            out["atr30"] = round(atr30, 4)
        return out

    def _trigger_ctx_for(self, pos: Position) -> dict[str, Any]:
        return self._trigger_ctx(pos.signal_id, pos.direction)

    def _trigger_ctx(self, signal_id: str, direction: Direction) -> dict[str, Any]:
        """The context the trigger was logged with; failing that (an operator take on the parent),
        measured now from the day's signal; failing that, breadth alone — and a measure that
        cannot be taken never blocks."""
        ctx = self._breadth_at.get(signal_id)
        if ctx is not None:
            return ctx
        sig = self._signals_today.get(signal_id)
        ctx = self.market_breadth(direction)
        if sig is not None:
            try:
                ctx.update(self.trigger_context(sig))
            except Exception as exc:  # noqa: BLE001 — a gate that cannot measure does not block
                log.warning("regime.context_failed", symbol=sig.symbol, error=str(exc)[:120])
        return ctx

    async def _log_breadth(self, sig: Signal) -> None:
        """Breadth at every FUDKII trigger, taken or not — the paper A/B's record: the RT-Y gate
        reads it, and the gated-out triggers' RT-X / RT-N results say what the gate saved or cost."""
        if sig.strategy is not StrategyKey.FUDKII or sig.signal_id in self._breadth_at:
            return
        und = self.underlyings.get(sig.symbol)
        if und is None or und.segment is Segment.MCX_FO:
            return
        try:
            br = self.market_breadth(sig.direction, at_ts=int(sig.ts))
        except Exception as exc:  # noqa: BLE001 — a measurement never costs a trigger its entry
            log.warning("regime.breadth_failed", symbol=sig.symbol, error=str(exc)[:120])
            return
        try:
            br.update(self.trigger_context(sig))
            br.update(volume_labels(br))
        except Exception as exc:  # noqa: BLE001 — descriptive labels never cost a trigger its entry
            log.warning("regime.context_failed", symbol=sig.symbol, error=str(exc)[:120])
        self._breadth_at[sig.signal_id] = br
        log.info("regime.breadth", symbol=sig.symbol, direction=sig.direction.value, share=br["share"], names=br["names"])
        await self.ledger.event("regime.breadth", {"signal_id": sig.signal_id, "symbol": sig.symbol, **br})

    async def _open_shadow_twins(self, of: Position, inst: Instrument, ts: float, charges: float, *, planned_sl: float = 0.0) -> None:
        """Mirror a book's fresh entry into each book that shadows it (``SHADOW_OF``): the same
        contract, size, price, instant and ladder, one rule changed. The wide-stop shadow moves the
        equity stop ``equity_stop_buffer_pct`` further from entry and re-projects the option stop
        for it — at entry here, and every ``reproject_stop_s`` after, as RT-Y's own stop is."""
        for shadow_key, source in SHADOW_OF.items():
            if source.value != of.strategy:
                continue
            engine_for = self._exits_by_strategy[shadow_key.value]
            wallet = self.wallets.get(shadow_key.value)
            cost = of.entry * of.qty * inst.multiplier
            if wallet is None:
                continue
            if wallet.halted:
                await self.ledger.event("rt_twin.skipped", {"book": shadow_key.value, "signal_id": of.signal_id, "symbol": of.underlying.symbol, "reason": f"halted — {wallet.halt_reason}"})
                continue
            if wallet.available < cost:
                await self.ledger.event("rt_twin.skipped", {"book": shadow_key.value, "signal_id": of.signal_id, "symbol": of.underlying.symbol, "reason": f"wallet: {wallet.available:,.0f} available < {cost:,.0f}"})
                continue
            verdict = self._exposure_by_strategy[shadow_key.value].check(
                strategy=shadow_key.value, underlying=of.underlying.symbol, outlay=cost,
                positions=list(self.positions.values()), total_capital=wallet.balance,
            )
            if not verdict.allowed:
                await self.ledger.event("rt_twin.skipped", {"book": shadow_key.value, "signal_id": of.signal_id, "symbol": of.underlying.symbol, "reason": f"exposure: {verdict.reason}"})
                continue
            # its own order ref: its exits are its own orders, never the source's ids
            # the wide shadow widens the PLAN's stop, as it always has — not RT-Y's floored one (1 Oct:
            # 0.5 ATR30 is at most ~1 % of price, so the plan's stop 1 % further is still the wider; an
            # extra dip only deepened its stop-outs, SWIGGY and MAXHEALTH) — and never sits nearer than
            # the stop RT-Y itself holds
            shadow = replace(of, id=new_id("pos"), strategy=shadow_key.value,
                             equity_sl=planned_sl if planned_sl > 0 else of.equity_sl,
                             note=f"{of.note.split(' · stop floored')[0]} · shadow of {of.id}",
                             exec_log={"entry": dict(of.exec_log.get("entry") or {}), "exits": [],
                                       "ref": self._order_ref(shadow_key.value, ts)})
            self._widen_stop(shadow, engine_for.limits, not_nearer_than=of.equity_sl)
            self.positions[shadow.id] = shadow
            self._commit_outlay(wallet, cost, ts, shadow)
            wallet.apply_charges(charges, ts)
            await self.ledger.upsert_position(_position_json(shadow))
            await self.ledger.upsert_wallet(wallet.strategy, wallet.to_json())
            log.info("shadow.open", book=shadow_key.value, shadow=shadow.id, of=of.id, symbol=of.underlying.symbol,
                     equity_sl=shadow.equity_sl, option_sl=shadow.option_sl)

    def _floor_equity_stop(self, pos: Position, lim: RiskLimits, key: str) -> None:
        """A planned underlying stop nearer than ``min_equity_stop_atr`` ATR30 to the trigger's close sits
        inside one bar's noise (HDFCLIFE 2026-10-01: 0.18 ATR, taken out by a print AT it): moved out to
        the floor, and the option stop projected through the delta at entry for the new level. The
        book's premium cap (``_protect_option_stop``, after this) still bounds the option loss. No ATR
        at the fill: the stop as planned, never a guess."""
        k = lim.min_equity_stop_atr
        if not k or pos.equity_atr <= 0 or pos.equity_sl <= 0 or pos.equity_entry <= 0:
            return
        floor = k * pos.equity_atr
        if abs(pos.equity_entry - pos.equity_sl) >= floor:
            return
        planned = pos.equity_sl
        bull = pos.direction is Direction.BULLISH
        pos.equity_sl = round(pos.equity_entry - floor if bull else pos.equity_entry + floor, 2)
        if pos.instrument.is_option:
            delta = estimate_delta(spot=pos.equity_entry, strike=pos.instrument.strike, option_type=pos.instrument.option_type)
            option_sl, _ = map_levels_to_option(
                equity_entry=pos.equity_entry, equity_stop=pos.equity_sl, equity_targets=(), option_premium=pos.entry, delta=delta,
            )
            pos.option_sl = self.floored_option_stop(StrategyKey(key), pos.entry, option_sl, pos.instrument.tick_size)
            pos.initial_option_sl = pos.option_sl
            pos.r_unit = abs(pos.entry - pos.option_sl)
        pos.note += f" · stop floored {planned:g} -> {pos.equity_sl:g} ({k:g} ATR30)"
        log.info("stop.floored", book=key, symbol=pos.underlying.symbol, planned=planned, floored=pos.equity_sl,
                 atr30=pos.equity_atr, option_sl=pos.option_sl)

    def _book_stop(self, sig: Signal, book: str | None) -> float:
        """The underlying stop ``book`` will actually hold: the signal's, or — for a book with a stop
        floor — the floored level ``_floor_equity_stop`` sets at the fill, so a print at a stop the book
        does not use never kills its entry before it starts."""
        stop = sig.stop
        k = self.limits_for(book).min_equity_stop_atr if book else None
        if not k or not stop or not sig.entry:
            return stop
        a = atr(self.store.bars(sig.symbol, DECISION_TF, 60), 14) or 0.0
        if a <= 0 or abs(sig.entry - stop) >= k * a:
            return stop
        return round(sig.entry - k * a if sig.direction is Direction.BULLISH else sig.entry + k * a, 2)

    def _widen_stop(self, pos: Position, lim: RiskLimits, *, not_nearer_than: float = 0.0) -> None:
        """``equity_stop_buffer_pct`` further from entry on the underlying (a bullish trade's stop
        × 0.99 at 1 %, a bearish one's × 1.01) — never nearer to the entry than ``not_nearer_than``
        (the source book's own stop) — and the option stop projected through the delta at entry for
        that wider level, floored as the book's own is."""
        buf = lim.equity_stop_buffer_pct
        if not buf or pos.equity_sl <= 0:
            return
        k = buf / 100
        bull = pos.direction is Direction.BULLISH
        pos.equity_sl = round(pos.equity_sl * (1 - k) if bull else pos.equity_sl * (1 + k), 2)
        if not_nearer_than > 0 and (pos.equity_sl > not_nearer_than if bull else pos.equity_sl < not_nearer_than):
            pos.equity_sl = not_nearer_than
        if pos.instrument.is_option and pos.equity_entry > 0:
            delta = estimate_delta(spot=pos.equity_entry, strike=pos.instrument.strike, option_type=pos.instrument.option_type)
            option_sl, _ = map_levels_to_option(
                equity_entry=pos.equity_entry, equity_stop=pos.equity_sl, equity_targets=(), option_premium=pos.entry, delta=delta,
            )
            pos.option_sl = self.floored_option_stop(StrategyKey(pos.strategy), pos.entry, option_sl, pos.instrument.tick_size)
            # its own initial stop and R, not the source's: the copy carried RT-Y's, capped at 25 % since
            # 2026-09-29, so the shadow's R-multiples were measured against a stop it does not have (review)
            pos.initial_option_sl = pos.option_sl
            pos.r_unit = abs(pos.entry - pos.option_sl)

    # -- the trigger-card page ----------------------------------------------------------------------

    async def book_cards(self, book: str, day: date | None = None) -> dict[str, Any]:
        """One card per FUDKII trigger of the session, read for one book: the trigger's own
        numbers (the same for every book), then what THIS book did with it — mirrored, skipped and
        why, faded, or nothing to mirror — with the position live or closed. Assembled from the
        ledger (so a restart loses nothing) plus the live marks for open positions."""
        key = StrategyKey(book)
        day = day or ist_today()
        start = datetime(day.year, day.month, day.day, tzinfo=IST).timestamp()
        end = start + 86_400
        signals = await self.ledger.rows_between("signals", start, end)
        positions = await self.ledger.rows_between("positions", start, end)
        trades = await self.ledger.rows_between("trades", start, end)
        orders = await self.ledger.rows_between("orders", start, end)
        events = await self.ledger.rows_between("events", start, end)
        latest: dict[str, dict[str, Any]] = {}
        for sgn in signals:  # a take re-enters the same id: the last row is the decision that stands
            latest[sgn["signal_id"]] = sgn
        # A trigger FUDKII did not publish (NOT_PUBLISHED) is carded for the parent, with its own
        # reason, and for the graded-F shadow — the one book offered it — which is carded on those
        # ALONE: it never sees a published trigger (its Alerts tab, operator 2026-09-29). No trading
        # book is offered one (2026-09-28).
        sees_unpublished = key in (StrategyKey.FUDKII, StrategyKey.FUDKII_RT_Y_F)
        only_unpublished = key is StrategyKey.FUDKII_RT_Y_F
        parents = sorted(
            (
                sgn
                for sgn in latest.values()
                if sgn["strategy"] == StrategyKey.FUDKII.value
                and self.book_trades(key.value, self._segment_of(sgn["symbol"]))
                and (sees_unpublished or sgn.get("decision") != "NOT_PUBLISHED")
                and (not only_unpublished or sgn.get("decision") == "NOT_PUBLISHED")
            ),
            key=lambda x: x["ts"],
        )
        by_source = {sgn["source_signal_id"]: sgn for sgn in latest.values() if sgn.get("source_signal_id") and sgn["strategy"] == StrategyKey.FUDKII_CT_X.value}
        #: an entry a book made on a trigger under its OWN id — RT-MCX's routed commodity trigger, an
        #: operator take — found through the trigger it came from
        own_by_source = {(sgn["strategy"], sgn["source_signal_id"]): sgn for sgn in latest.values() if sgn.get("source_signal_id")}
        pos_by_key = {(ps["strategy"], ps["signal_id"]): ps for ps in positions}
        trades_by_pos = {t["position_id"]: t for t in trades}
        exits_by_pos: dict[str, list[dict[str, Any]]] = {}
        for o in orders:
            if o.get("purpose") == "EXIT" and o.get("status") == "FILLED":
                exits_by_pos.setdefault(str(o.get("position_id")), []).append(o)
        ev_by_sig: dict[str, list[dict[str, Any]]] = {}
        for e in events:
            if e.get("signal_id"):
                ev_by_sig.setdefault(e["signal_id"], []).append(e)
        alert_cards = {
            (a.get("evidence") or {}).get("signalId"): a.get("card")
            # No limit: 500 was the old ring size, and a session's keepalives (one a minute per
            # living signal) can push an early ENTRY past it — the card would vanish from the page
            # while its ledger row stayed, which reads as a trigger that was never carded.
            for a in self.alerts.feed("FUDKII_RT")
            if a.get("kind") == "ENTRY"
        }
        counter_books = {StrategyKey.FUDKII_CT_X.value, StrategyKey.FUDKII_CT_Y.value}
        nse_books = (
            StrategyKey.FUDKII.value, StrategyKey.FUDKII_RT_X.value, StrategyKey.FUDKII_RT_N.value, StrategyKey.FUDKII_RT_Y.value,
            StrategyKey.FUDKII_CT_X.value, StrategyKey.FUDKII_CT_Y.value,
        )

        def position_on(b: str, sid: str, fade: dict[str, Any] | None) -> dict[str, Any] | None:
            """Book ``b``'s position on the trigger ``sid``, resolved the way the card resolves its
            own: a fade book by the fade's id, an in-trend book by the trigger's id or by its own
            entry that names the trigger as its source (RT-MCX's routed entry, an operator take)."""
            if b in counter_books:
                gap_b = own_by_source.get((StrategyKey.FUDKII_CT_Y.value, sid)) if b == StrategyKey.FUDKII_CT_Y.value else None
                if gap_b is not None:  # CT-Y's own 09:45 gap fade on this trigger
                    return pos_by_key.get((b, gap_b["signal_id"]))
                return pos_by_key.get((b, fade["signal_id"])) if fade else None
            hit = pos_by_key.get((b, sid))
            if hit is None and (own_b := own_by_source.get((b, sid))) is not None:
                hit = pos_by_key.get((b, own_b["signal_id"]))
            return hit

        def book_row(b: str, hit: dict[str, Any] | None) -> dict[str, Any]:
            inst_j = (hit or {}).get("instrument") or {}
            if inst_j.get("option_type") in ("CE", "PE"):
                b_side = inst_j["option_type"]
            elif hit is not None:
                b_side = "LONG" if hit.get("direction") == "BULLISH" else "SHORT"
            else:
                b_side = None
            closed = hit is not None and hit.get("status") != "OPEN"
            trade = trades_by_pos.get(hit["id"]) if closed else None
            return {
                "book": b, "label": BOOK_LABELS.get(b, b),
                "status": "NONE" if hit is None else ("EXITED" if closed else "OPEN"),
                "side": b_side, "openedTs": (hit or {}).get("opened_ts"), "closedTs": (hit or {}).get("closed_ts") if closed else None,
                "exitReason": (hit or {}).get("exit_reason") if closed else None,
                "pnl": (trade or {}).get("net") if closed else None,
            }

        cards = []
        for sgn in parents:
            sid = sgn["signal_id"]
            evs = ev_by_sig.get(sid, [])
            route = next((e for e in reversed(evs) if e.get("kind") == "counter.route"), None)
            fade_x = by_source.get(sid)  # CT-X's fade (mirrored into CT-Y unless CT-Y gap-faded)
            gap_fade = own_by_source.get((StrategyKey.FUDKII_CT_Y.value, sid))
            fade = gap_fade if (key is StrategyKey.FUDKII_CT_Y and gap_fade is not None) else fade_x
            fade_evs = ev_by_sig.get(fade["signal_id"], []) if fade else []
            skip = next((e for e in reversed(evs + fade_evs) if e.get("kind") == "rt_twin.skipped" and e.get("book") == book), None)
            operator = [e for e in evs + fade_evs if str(e.get("kind", "")).startswith("operator.") and e.get("book") == book]
            entry_sig = fade if key.value in counter_books else sgn
            ps = pos_by_key.get((book, entry_sig["signal_id"])) if entry_sig else None
            own = own_by_source.get((book, sid)) if key.value not in counter_books else None
            if ps is None and own is not None:
                ps = pos_by_key.get((book, own["signal_id"]))
            live = None
            if ps and ps.get("status") == "OPEN" and ps["id"] in self.positions:
                lp = self.positions[ps["id"]]
                mk = self.position_marks.get(lp.id, {})
                mid = mk.get("mid") or self.ltps.get(lp.instrument.scrip_code) or 0.0
                realised = sum((float(o.get("avg_price") or 0) - lp.entry) * float(o.get("filled") or 0) for o in exits_by_pos.get(lp.id, []))
                live = {
                    "mid": mid, "quoteOk": mk.get("quote_ok"), "peak": lp.peak_mid, "line": lp.ratchet_sl, "optionSl": lp.option_sl,
                    "armedBy": lp.armed_by, "armedTs": lp.armed_ts, "targetsHit": lp.targets_hit, "qtyRemaining": lp.qty_remaining,
                    "qty": lp.qty, "ladder": list(lp.option_targets), "edm": lp.option_edm, "underlying": self.ltps.get(lp.underlying.scrip_code),
                    "unrealised": round((mid - lp.entry) * lp.qty_remaining * lp.instrument.multiplier, 2) if mid else None,
                    "realised": round(realised * lp.instrument.multiplier, 2),
                }
            # the books whose decision is the signal row itself; every other book records its own
            # (a skip with its reason, or its own resting entry) since each places its own order
            owns_row = key in (StrategyKey.FUDKII, StrategyKey.FUDKII_CT_X) or (key is StrategyKey.FUDKII_CT_Y and gap_fade is not None)
            pending = self._pending_for(book, {sid, (entry_sig or {}).get("signal_id"), (own or {}).get("signal_id")}) if ps is None else None
            # decided after the close and carried to the next open — for the books that will be offered it
            carry_q = next((e for e in evs if e.get("kind") == "carry.queued"), None)
            carried_here = carry_q is not None and key is not StrategyKey.FUDKII and (
                key in IN_TREND_BOOKS if carry_q.get("published") else key in UNPUBLISHED_BOOKS)
            if ps:
                state = "OPEN" if ps.get("status") == "OPEN" else "TRADED"
            elif pending is not None:
                state = "PENDING"
            elif carried_here and skip is None:
                state = "CARRIED"
            elif skip is not None and not owns_row:
                state = "SKIPPED"
            elif key is StrategyKey.FUDKII:
                state = sgn.get("decision") or "UNKNOWN"
            elif key is StrategyKey.FUDKII_CT_Y and gap_fade is not None:
                state = gap_fade.get("decision") or "UNKNOWN"
            elif key.value in counter_books:
                if route is None:
                    state = "NO_ROUTE"
                elif route.get("route") != "COUNTER":
                    state = "IN_TREND"
                elif fade is None:
                    state = "COUNTER_NO_PLAN"
                else:
                    state = fade.get("decision") or "UNKNOWN"
            elif skip is not None:
                state = "SKIPPED"
            elif own is not None:
                state = own.get("decision") or "UNKNOWN"
            elif str(sgn.get("decision", "")).endswith("FILLED"):
                state = "NOT_MIRRORED"
            elif sgn.get("decision") in _RESTING_DECISIONS:
                state = "PENDING"  # the parent's (or the halted parent's twin-feed) limit is still working
            else:
                state = "NO_FILL"
            if state in _RESTING_DECISIONS:
                state = "PENDING"
            # the trigger bar and the underlying's path since, for the sparkline
            bars30 = self.store.bars(sgn["symbol"], DECISION_TF, 80)
            candle = next(({"o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in bars30 if int(b.ts) == int(sgn["ts"])), None)
            vr = self._volume_reading(sgn["symbol"], sgn["ts"], [b for b in bars30 if b.ts <= sgn["ts"]])
            s_t, s_t1, base = vr.surge_t, vr.surge_t1, vr.baseline
            m1 = [b for b in self.store.bars(sgn["symbol"], "1m", 400) if b.ts >= sgn["ts"] - 1800]
            step = max(1, len(m1) // 120)
            spark = [[int(b.ts), b.close] for b in m1[::step]]
            # What the card describes: a fade book with a fade shows the FADE's direction, levels,
            # zones and plan; everything else shows the trigger. (NAM-INDIA, 2026-09-25: the CT-Y
            # card showed the bullish trigger's CE, stop and targets although CT-Y's trade is a PE.)
            vs = fade if (key.value in counter_books and fade is not None) else (
                own if (key is StrategyKey.FUDKII_CT_M and own is not None) else sgn)
            ctx = vs.get("context") or {}
            conf = ctx.get("confluence") or {}
            pros, cons = [], []
            if s_t and s_t >= 2.5:
                pros.append(f"volume surge {s_t:.1f}×")
            breadth = next((e for e in reversed(evs) if e.get("kind") == "regime.breadth"), None)
            b_share = (breadth or {}).get("share")
            if b_share is not None:
                # the share agreeing with the TRIGGER; a fade book reads the other side of it
                agree = (1 - b_share) if (key.value in counter_books and fade is not None) else b_share
                (pros if agree > 0.5 else cons).append(f"breadth {agree:.0%} of {breadth.get('names')} names agree")
            if breadth and not (key.value in counter_books and fade is not None):
                # the two trigger labels that held up in both halves of the Sep replay (see
                # trigger_context); for a fade card they describe the other side, so not shown there
                ahead = breadth.get("pivotsAhead") or []
                if ahead:
                    cons.append("pivot just ahead: " + ", ".join(ahead[:3]))
                gap = breadth.get("gapDatr")
                if breadth.get("openBar") and gap is not None and gap >= 0.3:
                    cons.append(f"09:45 gap with the trade: {gap:.2f} daily ATR")
            reads = (route or {}).get("reads") or []
            if any(r.get("volume") == "dried" for r in reads):
                cons.append("dried volume on " + "/".join(r["leg"] for r in reads if r.get("volume") == "dried"))
            if route and route.get("route") == "COUNTER":
                cons.append("routed COUNTER: " + str(route.get("summary") or ""))
            elif route and not ((route.get("wall") or {}).get("members")):
                pros.append("no wall ahead")
            room = float(conf.get("room_ratio") or 0)
            if room >= 2:
                pros.append(f"room {room:.1f} ATR")
            elif 0 < room < 0.5:
                cons.append(f"no room ({room:.2f} ATR)")
            stop_pct = abs(float(vs["entry"]) - float(vs["stop"])) / float(vs["entry"]) * 100 if vs.get("stop") else 0
            if 0 < stop_pct < 0.2:
                cons.append(f"stop {stop_pct:.2f}% away — inside one bar's noise")
            if float(conf.get("fortress") or 0) >= 9:
                cons.append(f"T1 is a {float(conf['fortress']):.1f} wall")
            ev = sgn.get("evidence") or {}
            bull = vs["direction"] == "BULLISH"
            zones = ctx.get("zones") or []
            clusters = sorted(
                ({"price": z["price"], "strength": z["strength"], "members": z["members"], "wall": z.get("wall", False),
                  "side": "ahead" if (z["price"] > float(vs["entry"])) == bull else "behind"}
                 for z in zones if abs(z["price"] - float(vs["entry"])) / float(vs["entry"]) <= 0.03),
                key=lambda z: z["price"], reverse=not bull,
            )
            und = self.underlyings.get(sgn["symbol"])
            plan = None
            fade_book = key.value in counter_books
            # A fade book with no fade has nothing to preview: falling back to the PARENT's signal
            # showed CT-Y a bullish trigger's CE (NAM-INDIA, 2026-09-25 14:45) as if CT-Y would buy it.
            unpublished = sgn.get("decision") == "NOT_PUBLISHED"
            if (ps is None and und is not None and (state not in ("IN_TREND", "NO_ROUTE") or key is StrategyKey.FUDKII)
                    and not (fade_book and entry_sig is None) and not unpublished):
                plan_sig = entry_sig if entry_sig is not None else sgn
                try:
                    plan = await self._plan_preview(key, plan_sig, und)
                except Exception as exc:  # noqa: BLE001 — a preview must never fail the page
                    plan = {"ok": False, "reason": f"preview failed: {exc}"[:120]}
            exit_plan = None
            if ps is not None:
                p_inst = Instrument(**ps["instrument"]) if isinstance(ps.get("instrument"), dict) else None
                if p_inst is not None:
                    seg = und.segment if und else Segment.NSE_EQ
                    lp = self.positions.get(ps["id"]) if ps.get("status") == "OPEN" else None
                    if lp is not None:
                        # the live trade: the stop as it stands now (re-projected every few seconds, stepped to
                        # breakeven and one rung behind), the lots left and the rungs taken — the ledger row only
                        # changes at a fill, a target placed or cancelled, or an exit
                        stop_now = max(lp.option_sl, lp.ratchet_sl)
                        exit_plan = self._exit_plan(key, p_inst, int(lp.qty_remaining), tuple(lp.option_targets), stop_now,
                                                    float(lp.equity_sl or 0), seg, hit=lp.targets_hit, rising=lp.ratchet_sl > lp.option_sl)
                    else:
                        exit_plan = self._exit_plan(key, p_inst, int(ps["qty"]), tuple(ps.get("option_targets") or ()), float(ps.get("option_sl") or 0), float(ps.get("equity_sl") or 0), seg)
            if skip is not None:
                route_label = "SKIP"
            elif key is StrategyKey.FUDKII_CT_Y and gap_fade is not None:
                route_label = "GAP FADE"
            elif route is not None:
                route_label = "COUNTER-TREND" if route.get("route") == "COUNTER" else "IN TREND"
            else:
                route_label = None
            # The side THIS book trades on this trigger, for the card's colour and contract: the held
            # contract's own side if there is a position; else the trigger's side for the in-trend
            # books and the opposite side for the fade books.
            trig_bull = sgn["direction"] == "BULLISH"
            inst_json = (ps or {}).get("instrument") or {}
            if inst_json.get("option_type") in ("CE", "PE"):
                side = inst_json["option_type"]
            elif inst_json.get("kind") == "FUTURE":
                side = "LONG" if (ps or {}).get("direction") == "BULLISH" else "SHORT"
            else:
                side = ("PE" if trig_bull else "CE") if fade_book else ("CE" if trig_bull else "PE")
            if und is not None and und.segment is Segment.MCX_FO and not inst_json:
                side = ("SHORT" if trig_bull else "LONG") if fade_book else ("LONG" if trig_bull else "SHORT")
            rt_card = alert_cards.get(entry_sig["signal_id"]) if (fade_book and entry_sig is not None) else (None if fade_book else alert_cards.get(sid))
            no_plan = next((e for e in reversed(evs) if e.get("kind") == "counter.no_plan"), None)
            cta = self._card_cta(book, key, state, ps, plan, route, skip, und, sgn, vs, fade_book, entry_sig is None, no_plan)
            if unpublished:
                # FUDKII's own grade kept it from publishing the trigger: no TAKE — the parent would
                # refuse it — and the card says why (review, 2026-09-28). The graded-F shadow's own tab:
                # a paper shadow decides these itself, so no operator TAKE there either
                why_not = ("paper shadow — it takes or skips these triggers itself; no operator TAKE"
                           if key is StrategyKey.FUDKII_RT_Y_F else f"not published — {sgn.get('decision_reason') or 'graded F'}")
                cta = {**cta, "action": "take", "enabled": False, "reason": why_not}
            if state == "CARRIED" or (carry_q is not None and key is StrategyKey.FUDKII):
                cta = {**cta, "action": "take", "enabled": False, "reason": "after the close — carried to the next session's 09:15 open"}
            if pending is not None:
                # a limit entry is working for this trigger: its fill (or its miss) decides; a TAKE now
                # would enter the book a second time when the mirrored fill lands
                cta = {"action": "take", "enabled": False, "contract": pending.get("contract"), "type": cta.get("type"),
                       "reason": f"limit {pending['limit']:g} resting {pending['restingS']:g} s ({pending['why']})"}
            # Every book that trades this segment, and what each did with the trigger — the card's row
            # of circles: who bought it, who still holds it (operator, 2026-09-26).
            if self._segment_of(sgn["symbol"]) is Segment.MCX_FO:
                seg_books = [StrategyKey.FUDKII_RT_MCX.value]
                if position_on(StrategyKey.FUDKII.value, sid, fade_x) is not None:
                    seg_books.insert(0, StrategyKey.FUDKII.value)
            else:
                seg_books = list(nse_books)
                if unpublished:  # the one book offered a trigger FUDKII did not publish
                    seg_books.append(StrategyKey.FUDKII_RT_Y_F.value)
            books_row = [book_row(b, position_on(b, sid, fade_x)) for b in seg_books]
            # RT-Y and CT-Y are never offered a trigger FUDKII did not publish: no chip for what they would
            # have done — the graded-F shadow's own dot in the row says what it did (review, 2026-09-29)
            verdicts = {"rtY": None, "ctY": None, "ctM": None} if unpublished else self._card_verdicts(sgn, evs, fade_x, gap_fade, pos_by_key)
            if cta.get("type") not in ("CE", "PE"):
                cta["type"] = side  # a future reads LONG / SHORT
            # every button says what it buys: lots, the price of one, and the money it needs
            cta = {**cta, **self._cta_size(ps, plan)}
            cta_counter = None
            ckey = COUNTER_OF.get(key)
            if (ckey is not None and route is not None and route.get("route") == "COUNTER" and not unpublished
                    and und is not None and und.segment is not Segment.MCX_FO):
                try:
                    cta_counter = await self._counter_cta(ckey, sgn, evs, fade_x, gap_fade, pos_by_key, trades_by_pos, und)
                except Exception as exc:  # noqa: BLE001 — a card must never fail the page (review, 2026-09-29)
                    cta_counter = {"book": ckey.value, "label": BOOK_LABELS.get(ckey.value, ckey.value), "action": "take",
                                   "enabled": False, "contract": None, "reason": f"counter-trend plan failed: {exc}"[:160]}
                if cta.get("action") == "take":
                    # the route says fade: the in-trend buy stays on the card, named and sized, greyed (the
                    # operator's rule, 2026-09-29) — the note says what the counter-trend button really is
                    clabel = BOOK_LABELS.get(ckey.value, ckey.value)
                    state = {"held": f"{clabel} holds the counter-trend trade", "taken": f"{clabel} traded the counter-trend side"}.get(
                        cta_counter["action"],
                        f"the {clabel} counter-trend buy is the live button" if cta_counter.get("enabled")
                        else f"no {clabel} counter-trend buy now ({cta_counter.get('reason') or 'no plan'})")
                    note = f"routed COUNTER-TREND ({route.get('summary') or route.get('reason')}) — {state}"
                    own_why = cta.get("reason") if not cta.get("enabled") else None
                    cta = {**cta, "enabled": False, "reason": f"{own_why} · {note}" if own_why else note}
            cards.append({
                "side": side,
                "atr": ev.get("atr"), "oi": ev.get("oi"), "oiChangePct": ev.get("oi_change_pct"), "clusters": clusters[:8],
                "futLevels": self._fut_levels(route, bull), "plan": plan, "exitPlan": exit_plan, "routeLabel": route_label,
                "signalId": sid, "symbol": sgn["symbol"], "direction": vs["direction"], "ts": sgn["ts"], "grade": vs.get("grade"),
                "rr": vs.get("rr"), "reason": vs.get("reason"), "entry": vs["entry"], "stop": vs["stop"], "targets": vs.get("targets"),
                "triggerDirection": sgn["direction"], "describes": "fade" if vs is not sgn else "trigger",
                "stopPct": round(stop_pct, 2), "confluence": conf, "evidence": sgn.get("evidence") or {}, "gates": sgn.get("gates") or [],
                "parentDecision": sgn.get("decision"), "parentReason": sgn.get("decision_reason"),
                "candle": candle, "surgeT": s_t, "surgeT1": s_t1, "baseline": base, "volumeDoubt": vr.doubt or None, "spark": spark,
                "route": route, "skip": skip, "operator": operator, "fade": fade, "state": state,
                "position": ps, "trade": trades_by_pos.get(ps["id"]) if ps else None, "exits": exits_by_pos.get(ps["id"], []) if ps else [],
                "live": live, "rtCard": rt_card, "pros": pros, "cons": cons, "cta": cta, "breadth": breadth, "books": books_row,
                "pending": pending, "execLog": (ps or {}).get("exec_log") or None,
                "restingTarget": self._resting_target_card(ps),
                "restingTargets": self._resting_targets_card(ps),
                "verdicts": verdicts, "ctaCounter": cta_counter,
            })
        counts: dict[str, int] = {}
        for c in cards:
            counts[c["state"]] = counts.get(c["state"], 0) + 1
        return {"book": book, "day": day.isoformat(), "wallet": self.wallets[book].to_json() if book in self.wallets else None, "counts": counts, "cards": cards, "nowTs": time.time()}

    @staticmethod
    def _cta_size(ps: dict[str, Any] | None, plan: dict[str, Any] | None) -> dict[str, Any]:
        """A button's size: the lots, the quantity, the price of one and the money in it — the held or
        traded position's own entry, else the plan's preview. Nothing when neither is known."""
        if ps:
            inst = ps.get("instrument") or {}
            qty, px = int(ps.get("qty") or 0), float(ps.get("entry") or 0)
            lot = int(inst.get("lot_size") or 1) or 1
            mult = float(inst.get("multiplier") or 1) or 1.0  # an MCX future's price is per unit of its multiplier
            return {"lots": qty // lot, "qty": qty, "premium": px, "outlay": round(px * qty * mult, 2)} if qty and px else {}
        if plan and plan.get("qty") and plan.get("premium"):
            return {"lots": plan.get("lots"), "qty": plan.get("qty"), "premium": plan.get("premium"), "outlay": plan.get("outlay")}
        return {}

    def _operator_fade(self, key: StrategyKey, sig: Signal) -> tuple[Signal | None, str]:
        """The fade a counter book enters on an operator TAKE of ``sig``, and why there is none."""
        und = self.underlyings.get(sig.symbol)
        zones = self.zones_for(sig.symbol)
        atr30 = atr(self.store.bars(sig.symbol, DECISION_TF, 60), 14) or 0.0
        tick = (und.tick_size if und else 0.05) or 0.05
        dec = CounterDecision("COUNTER", "operator take", NO_WALL)
        entry = flipped_signal(sig, key=key, zones=zones, atr=atr30, tick_size=tick, decision=dec, policy=self.fudkii.cfg.fade_grade_policy)
        if entry is not None:
            return entry, ""
        why = fade_refusal(sig, zones=zones, atr=atr30, tick_size=tick, policy=self.fudkii.cfg.fade_grade_policy)
        return None, str(why.get("reason") or "no fade plan")

    async def _counter_cta(
        self, ckey: StrategyKey, sgn: dict[str, Any], evs: list[dict[str, Any]], fade_x: dict[str, Any] | None,
        gap_fade: dict[str, Any] | None, pos_by_key: dict[tuple[str, str], dict[str, Any]],
        trades_by_pos: dict[str, dict[str, Any]], und: Instrument,
    ) -> dict[str, Any]:
        """The counter-trend button on an in-trend book's card: what ``ckey`` bought on this trigger
        (held or closed, with its money), else what an operator TAKE into ``ckey`` would buy now —
        the same fade plan ``operator_take`` enters — sized and priced by ``ckey``'s own limits."""
        label = BOOK_LABELS.get(ckey.value, ckey.value)
        base = {"book": ckey.value, "label": label}
        own = gap_fade if (ckey is StrategyKey.FUDKII_CT_Y and gap_fade is not None) else fade_x
        ps = pos_by_key.get((ckey.value, own["signal_id"])) if own else None
        if ps is not None:
            inst = ps.get("instrument") or {}
            held = ps.get("status") == "OPEN"
            trade = trades_by_pos.get(ps["id"]) if not held else None
            why = f"{label} holds it" if held else f"{label} traded it — closed ({ps.get('exit_reason') or 'exited'})" + (
                f", net ₹{float(trade['net']):,.0f}" if trade and trade.get("net") is not None else "")
            return {**base, "action": "held" if held else "taken", "enabled": False, "contract": inst.get("name") or inst.get("scrip_code"),
                    "type": inst.get("option_type"), "reason": why, **self._cta_size(ps, None)}
        pending = self._pending_for(ckey.value, {sgn["signal_id"], (own or {}).get("signal_id")})
        if pending is not None:
            return {**base, "action": "take", "enabled": False, "contract": pending.get("contract"),
                    "reason": f"limit {pending['limit']:g} resting {pending['restingS']:g} s ({pending['why']})"}
        if ckey is StrategyKey.FUDKII_CT_Y and (gap_fade is not None or any(e.get("kind") == "counter.gap_fade" for e in evs)):
            # the 09:45 gap fade decided this trigger for CT-Y: an operator fade would file under the same
            # id and be counted as the gap fade's trade in its A/B (review, 2026-09-29) — its decision stands
            why = (f"{(gap_fade or {}).get('decision') or 'planned'} — {(gap_fade or {}).get('decision_reason') or 'see the CT-Y tab'}"
                   if gap_fade is not None else "the gap fade was planned but not entered")
            return {**base, "action": "take", "enabled": False, "contract": None, "reason": f"CT-Y's 09:45 gap fade owns this trigger: {why}"}
        if any(p.status == "OPEN" and p.strategy == ckey.value and p.underlying.symbol == sgn["symbol"] for p in self.positions.values()):
            return {**base, "action": "take", "enabled": False, "contract": None, "reason": f"{label} already holds {sgn['symbol']} (another trigger)"}
        sig = self._signals_today.get(sgn["signal_id"])
        if sig is None:
            return {**base, "action": "take", "enabled": False, "contract": None, "reason": "not in today's book"}
        fade, why = self._operator_fade(ckey, sig)
        if fade is None:
            return {**base, "action": "take", "enabled": False, "contract": None, "reason": f"no fade plan — {why}"}
        # a preview quotes the fade's strikes: once per 15 s per card, not on every 2 s poll of the page
        ck = (ckey.value, sgn["signal_id"])
        hit = self._counter_preview.get(ck)
        if hit is not None and time.time() - hit[0] < COUNTER_PREVIEW_S:
            plan = hit[1]
        else:
            try:
                plan = await self._plan_preview(ckey, fade.to_json(), und)
            except Exception as exc:  # noqa: BLE001 — a preview must never fail the page
                plan = {"ok": False, "reason": f"preview failed: {exc}"[:120]}
            self._counter_preview[ck] = (time.time(), plan)
        return {**base, "action": "take", "enabled": bool(plan.get("ok")), "contract": plan.get("contract"), "type": plan.get("type"),
                "reason": None if plan.get("ok") else str(plan.get("reason") or "no plan"), **self._cta_size(None, plan)}

    def _resting_target_card(self, ps: dict[str, Any] | None) -> dict[str, Any] | None:
        """The next rung's target sell resting for this card's open position, if any."""
        rs = self._resting_targets_card(ps)
        return rs[0] if rs else None

    def _resting_targets_card(self, ps: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Every target sell resting for this card's open position, lowest rung first."""
        if not ps or ps.get("status") != "OPEN":
            return []
        return [{"rung": r.ctx[1] + 1, "limit": r.limit, "qty": r.intent.qty, "placedTs": r.placed_ts,
                 "lots": r.intent.qty // max(1, r.intent.instrument.lot_size)} for r in self._targets_resting(ps["id"])]

    def _card_verdicts(
        self, sgn: dict[str, Any], evs: list[dict[str, Any]], fade_x: dict[str, Any] | None,
        gap_fade: dict[str, Any] | None, pos_by_key: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        """What RT-Y, CT-Y and CT-M do with this trigger — a label on EVERY book's card (operator,
        2026-09-26: "mention on all respective strategies as label"); see ``trigger_verdicts``."""
        if self._segment_of(sgn["symbol"]) is Segment.MCX_FO:
            return {"rtY": None, "ctY": None, "ctM": None}
        return trigger_verdicts(
            sgn, evs, fade_x=fade_x, gap_fade=gap_fade,
            rt_y_held=(StrategyKey.FUDKII_RT_Y.value, sgn["signal_id"]) in pos_by_key,
            lim_y=self._exits_by_strategy[StrategyKey.FUDKII_RT_Y.value].limits,
        )

    def _card_cta(
        self, book: str, key: StrategyKey, state: str, ps: dict[str, Any] | None, plan: dict[str, Any] | None,
        route: dict[str, Any] | None, skip: dict[str, Any] | None, und: Instrument | None, sgn: dict[str, Any],
        vs: dict[str, Any], fade_book: bool, no_fade: bool, no_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The card's one button, always naming the contract it is about: TAKE when the book can
        enter now, SKIP (close) while it holds, and otherwise the same contract greyed out with the
        reason underneath — never a bare "no plan" (the operator asked for this 2026-09-25)."""
        label = BOOK_LABELS.get(book, book)
        if ps is not None:
            inst = ps.get("instrument") or {}
            name = inst.get("name") or inst.get("scrip_code")
            if ps.get("status") == "OPEN":
                return {"action": "skip", "enabled": True, "contract": name, "type": inst.get("option_type"), "reason": None}
            why = ps.get("exit_reason") or "closed"
            return {"action": "taken", "enabled": False, "contract": name, "type": inst.get("option_type"), "reason": f"traded — closed ({why})"}
        if plan is not None and plan.get("ok"):
            note = f"{label} skipped it: {(skip or {}).get('reason')} — TAKE overrides" if skip is not None else None
            return {"action": "take", "enabled": True, "contract": plan.get("contract"), "type": plan.get("type"), "reason": note}
        # Disabled: name the contract anyway, then say why.
        trig_bull = sgn["direction"] == "BULLISH"
        aim_dir = Direction.BEARISH if (fade_book and trig_bull) or (not fade_book and not trig_bull) else Direction.BULLISH
        aim = None
        if plan is not None and plan.get("contract"):
            aim = {k: plan.get(k) for k in ("contract", "scripCode", "strike", "type", "bid", "ask", "spreadPct")}
        elif und is not None:
            try:
                tgts = vs.get("targets") or []
                aim = self._aim_contract(und, sgn["symbol"], aim_dir, float(sgn["entry"]), float(tgts[0]) if tgts and not (fade_book and no_fade) else None)
            except Exception:  # noqa: BLE001 — the page must render even if the catalogue cannot answer
                aim = None
        if fade_book and no_fade:
            summary = str((route or {}).get("summary") or (route or {}).get("reason") or "").strip()
            if state == "IN_TREND":
                reason = f"{label} fades counter-trend triggers only; this one was routed IN TREND" + (f" ({summary})" if summary else "")
            elif state == "NO_ROUTE":
                reason = "no counter-trend route was recorded for this trigger"
            else:
                refusal = str((no_plan or {}).get("reason") or "no fade plan could be made")
                reason = f"routed COUNTER-TREND, but {refusal}" + (f" · route: {summary}" if summary else "")
        elif plan is None:
            reason = "no preview for this trigger (underlying not loaded)" if und is None else "no preview"
        else:
            reason = _humanise_reason(str(plan.get("reason") or "no plan"))
        mcx = und is not None and und.segment is Segment.MCX_FO
        blank = {"contract": None, "type": ("LONG" if aim_dir is Direction.BULLISH else "SHORT") if mcx else aim_dir.option_type.value}
        return {"action": "take", "enabled": False, **(aim or blank), "reason": reason}

    def _exit_plan(self, key: StrategyKey, inst: Instrument, qty: int, ladder: tuple[float, ...], option_sl: float, equity_stop: float,
                   segment: Segment, *, hit: int = 0, rising: bool = False) -> dict[str, Any]:
        """What leaves at which threshold, for this book — the card's "what happens next". For an open
        position the caller passes the LIVE stop, the lots left and the rungs already taken (``hit``),
        so the plan tracks the trade (operator, 2026-09-29: "does the SL get updated live?")."""
        lim = self._exits_by_strategy[key.value].limits if key.value in self._exits_by_strategy else self.limits
        lot = max(1, inst.lot_size)
        rows: list[dict[str, Any]] = []
        rungs = list(ladder[:4])
        if lim.own_ladder:
            for i, r in enumerate(rungs):
                if i < hit:
                    rows.append({"kind": "target", "at": f"T{i + 1} {r:.2f} ✓", "action": "taken", "qty": None})
                    continue
                last = i == len(rungs) - 1
                out = max(0, qty - (i - hit) * lot) if last else min(lot, qty)
                rows.append({"kind": "target", "at": f"T{i + 1} {r:.2f}", "action": "the rest" if last else "1 lot", "qty": out})
        else:
            left = qty
            for i, r in enumerate(rungs):
                if i < hit:
                    rows.append({"kind": "target", "at": f"T{i + 1} {r:.2f} ✓", "action": "taken", "qty": None})
                    continue
                share = lim.target_ladder[i] if i < len(lim.target_ladder) else 0.0
                out = min(left, (int(qty * share) // lot) * lot) if i < len(rungs) - 1 else left
                left -= out
                rows.append({"kind": "target", "at": f"T{i + 1} {r:.2f}", "action": f"{share:.0%}", "qty": out})
        if option_sl > 0:
            sus = f", {lim.sustain_s:.0f} s sustain" if lim.sustain_s else ""
            what = (f"stop now {option_sl:.2f} — the rising line (breakeven, the rung below, or the peak give-back)" if rising
                    else f"option stop {option_sl:.2f} (equity stop through δ{sus})")
            rows.append({"kind": "stop", "at": what, "action": "all", "qty": qty})
            if lim.sustain_s:
                rows.append({"kind": "stop", "at": f"hard floor {option_sl * (1 - lim.hard_floor_below_stop_pct / 100):.2f} — {lim.hard_floor_below_stop_pct:.0f}% through the stop, no sustain", "action": "all", "qty": qty})
        if equity_stop > 0:
            rows.append({"kind": "stop", "at": f"underlying {equity_stop:.2f} confirmed", "action": "all", "qty": qty})
        if lim.own_ladder:
            if lim.arm_mode == "immediate":
                trail = f"armed at once by the equity T1 or a 1m close over own R1: peak − {lim.peak_giveback_pct:.0f}% ({lim.trail_dwell_samples} reads)"
            elif lim.arm_at_pct is not None:
                confirm = f"{lim.trail_dwell_samples} reads" if lim.band_exit == "dwell" else f"{lim.sustain_s:.0f} s sustain"
                trail = (
                    f"T1 = max(own T1, entry +{lim.arm_at_pct:g}%): one lot out there and the SL to breakeven, "
                    f"SL one rung behind, then all remaining out on a {lim.peak_giveback_pct:g}% fall from the peak ({confirm})"
                )
            elif lim.arm_min_move:
                trail = f"armed once T1 (≥ {lim.arm_min_move:g}× expected move) is sustained: SL one rung behind, band max({lim.peak_giveback_pct:.0f}%, {lim.giveback_move_frac:g}× expected move), {lim.sustain_s:.0f} s sustain"
            else:
                trail = f"after T1 is sustained ({lim.sustain_s:.0f} s + a 1m close): SL steps to the rung below, line at peak − {lim.peak_giveback_pct:.0f}%, one read through"
        else:
            trail = f"trail arms at +{lim.trail_arm_pct:.0f}%: stop = peak − {lim.trail_giveback_pct:.0f}% of the gain; breakeven after T1"
        rows.append({"kind": "trail", "at": trail, "action": "all remaining", "qty": None})
        rows.append({"kind": "time", "at": f"force-flat {self._force_flat_hm(key.value, segment)} IST", "action": "all remaining", "qty": None})
        policy = {
            "FUDKII": "legacy: share ladder 40/30/20/10, trail 3%/40%",
            "FUDKII_RT_X": "RT-X: own MTF ladder · touch pays a lot, sustain steps the SL · 3% line",
            "FUDKII_RT_N": "RT-N: daily R1–R4 · immediate arm · 2% line, 3 reads",
            "FUDKII_RT_Y": "RT-Y: T1 = max(own T1, +5%) · SL one rung behind · all out on a 3% fall from the peak · option SL ≤ 25% under the premium",
            "FUDKII_RT_Y_F": "RT-Y graded F (shadow): RT-Y's gates and exits on the triggers FUDKII grades F",
            "FUDKII_CT_X": "CT-X: the fade under RT-X's exits",
            "FUDKII_CT_Y": "CT-Y: the fade under RT-Y's exits",
            "FUDKII_CT_M": "CT-M (shadow): CT-Y's fade on a trigger the market is clearly against (≤ 45 % agree)",
            "FUDKII_RT_MCX": "RT-MCX: RT-X's exits on commodities",
        }.get(key.value, key.value)
        return {"policy": policy, "rows": rows}

    async def _plan_preview(self, key: StrategyKey, sgn: dict[str, Any], underlying: Instrument) -> dict[str, Any]:
        """What THIS book would buy on the trigger right now: the contract the selector picks at
        the live market, the size under the book's limits and wallet, the δ-projected stop and
        the book's ladder. A preview, not an order — and honest about why there is none."""
        sig = Signal(
            strategy=key, symbol=sgn["symbol"], direction=Direction(sgn["direction"]), ts=int(sgn["ts"]),
            entry=float(sgn["entry"]), stop=float(sgn["stop"]), targets=tuple(float(t) for t in (sgn.get("targets") or ())),
        )
        sel = await self._select_instrument(underlying, sig, tape=False)
        if not sel.ok or sel.instrument is None:
            return {"ok": False, "reason": sel.reason}
        inst = sel.instrument
        delta = estimate_delta(spot=sig.entry, strike=inst.strike, option_type=inst.option_type) if inst.is_option else 1.0
        option_sl, option_targets = map_levels_to_option(
            equity_entry=sig.entry, equity_stop=sig.stop, equity_targets=sig.targets, option_premium=sel.premium, delta=delta
        )
        if inst.is_option:
            option_sl = self.floored_option_stop(key, sel.premium, option_sl, inst.tick_size)
        book = self._exits_by_strategy.get(key.value)
        lim = book.limits if book is not None else self.limits
        wallet = self.wallets.get(key.value)
        sizing = size_position(
            instrument=inst, premium=sel.premium, option_stop=option_sl, option_target1=option_targets[0] if option_targets else None,
            balance=wallet.balance if wallet else 0.0, available=wallet.available if wallet else 0.0, limits=lim, costs=self.costs,
        )
        ladder, edm, note = (self._own_ladder_for(sig.symbol, inst, sel.premium, lim) if (lim.own_ladder or lim.targets_from_own_ladder) else (option_targets, 0.0, "δ-projected"))
        if not ladder and lim.targets_from_own_ladder:
            ladder, note = option_targets, "δ-projected (no own ladder)"
        q = self.quotes.get(inst.scrip_code)
        return {
            "ok": sizing.ok, "reason": sizing.reason, "contract": inst.name or inst.scrip_code, "scripCode": inst.scrip_code,
            "strike": inst.strike, "type": inst.option_type.value if inst.option_type else None, "premium": sel.premium,
            "bid": q.bid if q else None, "ask": q.ask if q else None, "spreadPct": round(q.spread_pct, 2) if q and q.spread_pct is not None else None,
            "oi": getattr(q, "oi", None) if q else None, "delta": round(abs(delta), 2), "lots": sizing.lots, "qty": sizing.qty, "outlay": round(sizing.outlay, 0),
            "lotSize": inst.lot_size, "optionSl": option_sl, "ladder": list(ladder), "edm": edm, "ladderNote": note,
            "exitPlan": self._exit_plan(key, inst, sizing.qty, tuple(ladder), option_sl, sig.stop, underlying.segment) if sizing.ok else None,
        }

    def _aim_contract(self, underlying: Instrument, symbol: str, direction: Direction, entry: float, target1: float | None) -> dict[str, Any] | None:
        """The contract a book would aim at on this trigger with the liquidity gates left out — so
        a card whose button is disabled can still name the option it is about (NAM-INDIA
        2026-09-25: the button said only "no plan"). Read-only: cached quotes, no REST, no tape."""
        cat = self.catalogue_loader.catalogue
        if underlying.segment is Segment.MCX_FO:
            # the contract the chart reads (the universe's roll), not merely the nearest unexpired one
            inst = underlying if underlying.kind is InstrumentKind.FUTURE else cat.front_future(symbol)
        else:
            pol = self.selection_policy_for(StrategyKey.FUDKII)
            expiry = choose_expiry(cat.expiries(symbol), ist_today(), pol)
            if expiry is None:
                return None
            bullish = direction is Direction.BULLISH
            otm = [i for i in cat.chain(symbol, expiry, direction.option_type) if (i.strike > entry if bullish else i.strike < entry) and i.strike > 0]
            atr30 = atr(self.store.bars(symbol, DECISION_TF, 60), 14) or 0.0
            picks, _ = strike_candidates(
                otm=otm, spot=entry, direction=direction, atr=atr30, target1=target1,
                liquidity=self.liquidity_for(otm), delta_floor=pol.min_delta, oi_margin=pol.oi_margin,
            )
            inst = picks[0] if picks else min(otm, key=lambda i: abs(i.strike - entry), default=None)
        if inst is None:
            return None
        q = self.quotes.get(inst.scrip_code)
        return {
            "contract": inst.name or inst.scrip_code, "scripCode": inst.scrip_code, "strike": inst.strike,
            "type": inst.option_type.value if inst.option_type else None,
            "bid": q.bid if q else None, "ask": q.ask if q else None,
            "spreadPct": round(q.spread_pct, 2) if q and q.spread_pct is not None else None,
        }

    @staticmethod
    def _fut_levels(route: dict[str, Any] | None, bullish: bool) -> dict[str, Any] | None:
        """The future's side of the levels table: its trigger close, the nearest key level behind
        (the stop side) and the next two ahead, from the route event's leg summary."""
        leg = next((lg for lg in (route or {}).get("legs", []) if lg.get("name") == "future"), None)
        if not leg:
            return None
        close = float(leg["close"])
        lv = [(k, float(v)) for k, v in (leg.get("levels") or {}).items() if v]
        behind = [x for x in lv if (x[1] < close) == bullish]
        ahead = [x for x in lv if (x[1] > close) == bullish]
        behind.sort(key=lambda x: abs(close - x[1]))
        ahead.sort(key=lambda x: abs(x[1] - close))
        return {
            "close": close, "atr": leg.get("atr"), "surgeT": leg.get("surgeT"), "surgeT1": leg.get("surgeT1"), "volume": leg.get("volume"),
            "behind": {"label": behind[0][0], "price": behind[0][1]} if behind else None,
            "ahead": [{"label": a[0], "price": a[1]} for a in ahead[:3]],
        }

    async def operator_take(self, book: str, signal_id: str) -> dict[str, Any]:
        """The operator's override: enter THIS book on a trigger it declined or never reached — the
        same entry path as an automatic fill (selection at the current market, sizing under the
        book's limits), the fade plan for a counter book. Audited before it is attempted."""
        key = StrategyKey(book)
        sig = self._signals_today.get(signal_id)
        if sig is None:
            raise KeyError(f"no signal {signal_id} in today's book")
        if any(p.status == "OPEN" and p.strategy == book and p.underlying.symbol == sig.symbol for p in self.positions.values()):
            raise RuntimeError(f"{book} already holds {sig.symbol}")
        if self._pending_for(book, {signal_id}) is not None:
            raise RuntimeError(f"a limit entry is resting on {sig.symbol} for {book}; its fill decides")
        if (book, sig.symbol) in self._entering:
            raise RuntimeError(f"{book} is placing an entry on {sig.symbol}; its fill decides")
        if key in (StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y):
            fade, why = self._operator_fade(key, sig)  # the plan the card's counter-trend button showed
            if fade is None:
                raise RuntimeError(f"no fade plan — {why}")
            entry = fade
        else:
            # the trigger rides along as the source, so the card finds the position this take opens
            entry = replace(sig, strategy=key, reason=f"operator take · {sig.reason}", source_signal_id=sig.signal_id)
        await self.ledger.event("operator.take", {"book": book, "signal_id": signal_id, "entry_signal_id": entry.signal_id, "symbol": sig.symbol})
        log.info("operator.take", book=book, symbol=sig.symbol, signal=signal_id)
        before = set(self.positions)
        out = (await self._handle_signal(entry, None, take=True)).get(book) or {}
        # only a position THIS take opened (review, 2026-09-26: a refused take reported "entered"
        # off a position that was already there)
        pos = next((p for p in self.positions.values() if p.id not in before and p.strategy == book
                    and p.signal_id == entry.signal_id and p.status == "OPEN"), None)
        # never a silent "not entered": the book's own decision and why (audit, 2026-09-26)
        return {"book": book, "signalId": signal_id, "entered": pos is not None, "position": _position_json(pos) if pos else None,
                "decision": out.get("decision", ""), "reason": out.get("reason", "")}

    async def operator_skip(self, book: str, signal_id: str) -> dict[str, Any]:
        """The operator's other override: close THIS book's open position on a trigger now, at the
        market, reason MANUAL — through the ordinary exit path so the trade, the wallet and the
        card all see the same fill."""
        pos = next(
            (p for p in self.positions.values() if p.status == "OPEN" and p.strategy == book
             and (p.signal_id == signal_id or (self._signals_today.get(p.signal_id, None) is not None and self._signals_today[p.signal_id].source_signal_id == signal_id))),
            None,
        )
        if pos is None:
            raise KeyError(f"{book} holds nothing on {signal_id}")
        mk = self.position_marks.get(pos.id, {})
        ref = mk.get("mid") or self.ltps.get(pos.instrument.scrip_code) or pos.entry
        await self.ledger.event("operator.skip", {"book": book, "signal_id": signal_id, "position_id": pos.id, "symbol": pos.underlying.symbol, "ref_price": ref})
        self.alerts.mark_skipped(signal_id, book=book, reason="operator skip")
        log.info("operator.skip", book=book, symbol=pos.underlying.symbol, position=pos.id)
        await self._exit(pos, ExitDecision(pos.id, ExitReason.MANUAL, ref, pos.qty_remaining, "operator skip"), time.time())
        return {"book": book, "signalId": signal_id, "positionId": pos.id, "closed": pos.status != "OPEN", "ref": ref}

    async def reset_wallet(self, strategy: str, initial: float | None = None) -> Wallet:
        """Start a book's purse over — the operator's reset between experiments. Refused while the
        book holds an open position: a reset under an open trade would release capital that is
        still at risk. The old record is written to the event log first, so the curve it ends is
        not lost with it."""
        old = self.wallets.get(strategy)
        if old is None:
            raise KeyError(f"no wallet for {strategy}")
        if any(p.status == "OPEN" and p.strategy == strategy for p in self.positions.values()):
            raise RuntimeError(f"{strategy} has an open position — flatten before resetting")
        amount = float(initial) if initial else float(INITIAL_INR.get(StrategyKey(strategy), self.s.paper_initial_inr))
        fresh = Wallet.new(strategy, amount, now=time.time())
        self.wallets[strategy] = fresh
        await self.ledger.event("wallet.reset", {"strategy": strategy, "initial": amount, "previous": old.to_json()})
        await self.ledger.upsert_wallet(strategy, fresh.to_json())
        log.info("wallet.reset", strategy=strategy, initial=amount, previous_balance=round(old.balance, 2))
        return fresh

    def _own_ladder_for(self, symbol: str, inst: Instrument, entry: float, lim: RiskLimits) -> tuple[tuple[float, ...], float, str]:
        """The RT/CT books' targets: nothing delta-projected — the contract's own levels from its
        previous session(s) (LegPivotLoader, thin-bar and zero-range guarded), per the book's
        ladder mode. No ladder → the equity trigger only. Returns (rungs, expected move, note)."""
        own = self.leg_pivots.for_code(inst.scrip_code)
        tol, reg = self.option_ladder_tolerance(symbol, inst.strike, inst.option_type, entry)
        edm = self.expected_move(symbol, inst, entry)
        if own is None:
            rungs: list[float] = []
        elif lim.ladder_mode == "daily_r":
            rungs = [r for r in (own.levels.r1, own.levels.r2, own.levels.r3, own.levels.r4) if r > entry]
        else:
            rungs = [r["price"] for r in own.rungs_above(entry, tolerance_pct=tol)]
            if lim.arm_min_move and edm > 0:
                rungs = [r for r in rungs if r >= entry * (1 + lim.arm_min_move * edm)]
        targets = tuple(rungs[:4])
        note = (
            f"own {lim.ladder_mode} ladder, tol {tol:.1f}% (k {reg.k:.2f} {reg.band.value}, {reg.source}), expected move {edm * 100:.0f}%"
            if targets
            else "no own ladder, equity trigger only"
        )
        return targets, edm, note

    def _protect_option_stop(self, pos: Position, lim: RiskLimits, key: str, now: float) -> None:
        """The option stop at the fill, priced and/or capped when the book says so
        (``RiskLimits.priced_option_stop`` / ``max_premium_loss_pct``); the books that re-project
        their stop hold the same rules every re-projection (risk/exits.py ``_reproject_stop``)."""
        inst = pos.instrument
        if not inst.is_option or (not lim.priced_option_stop and lim.max_premium_loss_pct is None):
            return
        sl = pos.option_sl
        if lim.priced_option_stop and pos.equity_sl > 0 and inst.expiry:
            spot = self.ltps.get(pos.underlying.scrip_code) or pos.equity_entry
            priced = value_at(option_price=pos.entry, spot=spot, target_spot=pos.equity_sl, strike=inst.strike,
                              expiry=inst.expiry, now=now, call=inst.option_type is OptionType.CE)
            if priced is not None:
                sl = max(0.05, priced)
        if lim.max_premium_loss_pct is not None:
            sl = max(sl, pos.entry * (1 - lim.max_premium_loss_pct / 100))
        sl = self.floored_option_stop(StrategyKey(key), pos.entry, sl, inst.tick_size)  # never nearer than 8 ticks
        pos.option_sl = pos.initial_option_sl = round(sl, 2)
        pos.r_unit = abs(pos.entry - pos.initial_option_sl)

    def _stamp_own_ladder(self, pos: Position, inst: Instrument, lim: RiskLimits, symbol: str) -> None:
        targets, edm, note = self._own_ladder_for(symbol, inst, pos.entry, lim)
        pos.option_targets = targets
        pos.option_t1 = targets[0] if targets else 0.0
        pos.option_edm = edm
        pos.note += " · " + note

    async def _handle_counter(self, sig: Signal, bar: UnifiedBar) -> None:
        """The counter-trend route on a FUDKII trigger (strategy/counter.py). COUNTER → the fade is
        entered by CT-X through the ordinary entry path — the opposite OTM from the same selector,
        its own confluence plan, its own wallet — and mirrored into CT-Y. Every route is stamped on
        the ENTRY card so an in-trend trade shows the wall it ran into."""
        if sig.strategy is not StrategyKey.FUDKII:
            return
        underlying = self.underlyings.get(sig.symbol)
        if underlying is None or underlying.segment is Segment.MCX_FO:
            return
        try:
            await self._gap_fade(sig, bar, underlying)
        except Exception as exc:  # the gap fade may never cost the counter route
            log.exception("gap_fade.failed", symbol=sig.symbol, error=str(exc)[:160])
        try:
            await self._market_fade(sig, bar, underlying)
        except Exception as exc:  # nor the market fade (a shadow)
            log.exception("market_fade.failed", symbol=sig.symbol, error=str(exc)[:160])
        legs = await self._counter_legs(underlying, bar, low_priority=not published(sig))
        atr_v = legs[0].atr if legs else 0.0
        dec = counter_route(legs, bullish=sig.direction is Direction.BULLISH, st_flipped="ST flip" in sig.reason)
        self.alerts.mark_route(sig.signal_id, decision=dec.to_json())
        legs_json = [
            {
                "name": leg.name, "open": leg.open, "high": leg.high, "low": leg.low, "close": leg.close, "atr": round(leg.atr, 2),
                "surgeT": leg.surge_t, "surgeT1": leg.surge_t1, "volume": leg.volume,
                "levels": {p.label: round(p.price, 2) for p in leg.points},
            }
            for leg in legs
        ]
        await self.ledger.event("counter.route", {"signal_id": sig.signal_id, "symbol": sig.symbol, "legs": legs_json, **dec.to_json()})
        log.info("counter.route", symbol=sig.symbol, route=dec.route, reason=dec.reason)
        if dec.route != "COUNTER":
            return
        fade = flipped_signal(
            sig, key=StrategyKey.FUDKII_CT_X, zones=self.zones_for(sig.symbol), atr=atr_v,
            tick_size=underlying.tick_size or 0.05, decision=dec, policy=self.fudkii.cfg.fade_grade_policy,
        )
        if fade is None:
            why = fade_refusal(sig, zones=self.zones_for(sig.symbol), atr=atr_v, tick_size=underlying.tick_size or 0.05, policy=self.fudkii.cfg.fade_grade_policy)
            log.info("counter.no_plan", symbol=sig.symbol, reason=why["reason"])
            # the card reads this: the route alone cannot say why no fade was planned
            await self.ledger.event("counter.no_plan", {"signal_id": sig.signal_id, "symbol": sig.symbol, **why})
            # no signal row: the trigger's row is the parent's (its in-trend path, or NOT_PUBLISHED),
            # and with the two paths side by side a row written here could win the race to it and
            # erase the parent's own decision (review, 2026-09-28). The cards read the event.
            return
        # the fade reaches both fade books at once; CT-X's row is the fade's, CT-Y decides for itself
        await self._handle_signal(fade, bar, books=FADE_BOOKS)

    def gap_fade_plan(self, sig: Signal, ctx: dict[str, Any] | None) -> dict[str, Any] | None:
        """The 09:45 gap fade's plan, or None when the trigger is not one (operator, 2026-09-26).

        Eligible: a FUDKII trigger on the session's first 30m bar whose open gapped at least
        ``gap_fade_datr`` daily ATRs its OWN way — the first-bar trap RT-Y now stands aside from.
        The fade: the opposite direction from the trigger's close, the equity stop 1 ATR30 past the
        close (the replay's stop: +3.72 % / +5.87 % a trade in the two halves of Sep 1–25, 40
        trades, before costs), the walls on the fade's side as its targets — or, with none, one
        target 1 ATR30 away. Its RR is its own (T1 against the 1-ATR stop) and its grade a label."""
        lim = self._exits_by_strategy[StrategyKey.FUDKII_CT_Y.value].limits
        ctx = ctx or {}
        gap = ctx.get("gapDatr")
        if lim.gap_fade_datr is None or not ctx.get("openBar") or gap is None or gap < lim.gap_fade_datr:
            return None
        plan = self.fade_plan(sig, ctx)
        if plan is not None:
            plan["gapDatr"] = gap
        return plan

    def fade_plan(self, sig: Signal, ctx: dict[str, Any] | None) -> dict[str, Any] | None:
        """The fade of a trigger, as CT-Y's gap fade plans it: the opposite direction from the trigger's
        close, the equity stop 1 ATR30 past the close, the walls on the fade's side as its targets —
        or, with none, one target 1 ATR30 away. None off NSE cash or without an ATR30."""
        ctx = ctx or {}
        und = self.underlyings.get(sig.symbol)
        if und is None or und.segment is not Segment.NSE_EQ:
            return None
        atr30 = float(ctx.get("atr30") or 0.0) or (atr(self.store.bars(sig.symbol, DECISION_TF, 60), 14) or 0.0)
        if atr30 <= 0:
            return None
        tick = und.tick_size or 0.05
        rnd = lambda px: round(round(px / tick) * tick, 4)  # noqa: E731
        fade_bull = sig.direction is Direction.BEARISH
        sign = 1 if fade_bull else -1
        close = float(sig.entry)
        stop = rnd(close - sign * atr30)
        pol = self.fudkii.cfg.fade_grade_policy  # a counter-trend plan: the fade's grading
        # the confluence engine's own walls on the fade's side; its stop is not used — the fade's
        # stop is the replay's 1 ATR — so the noise filter that would skip the targets is off
        conf = compute_confluence(close=close, bullish=fade_bull, zones=self.zones_for(sig.symbol), atr_value=atr30,
                                  tick_size=tick, policy=replace(pol, min_stop_atr_filter=0.0))
        targets = list(conf.targets) or [rnd(close + sign * atr30)]
        rr = abs(targets[0] - close) / atr30
        grade = "A" if rr >= pol.rr_a else "B" if rr >= pol.rr_b else "C" if rr >= pol.rr_c else "F"
        return {
            "direction": (Direction.BULLISH if fade_bull else Direction.BEARISH).value, "side": "CE" if fade_bull else "PE",
            "entry": close, "stop": stop, "targets": targets, "rr": round(rr, 2), "grade": grade,
            "atr30": round(atr30, 4), "targetNote": "walls on the fade's side" if conf.targets else "no wall on the fade's side — 1 ATR30",
        }

    async def _market_fade(self, sig: Signal, bar: UnifiedBar | None, underlying: Instrument) -> None:
        """FUDKII-CT-M, a shadow (operator, 2026-10-03): fade a published NSE trigger the market is
        clearly against — at most CT_M_MARKET_AGAINST_MAX of the NSE names past today's open its way —
        with CT-Y's fade plan, under CT-M's own key and wallet. A trigger the market is not against is
        recorded on CT-M's card as a skip with the share; a breadth that cannot be read decides nothing."""
        key = StrategyKey.FUDKII_CT_M
        if underlying.segment is not Segment.NSE_EQ or not self.book_trades(key.value, underlying.segment):
            return
        ctx = self._trigger_ctx(sig.signal_id, sig.direction)  # as logged at the trigger, else measured now
        share = ctx.get("share")
        if share is None:
            await self._book_skip(key.value, sig, "market breadth could not be read — CT-M decides nothing", gate="breadth_unread")
            return
        if share > CT_M_MARKET_AGAINST_MAX:
            await self._book_skip(key.value, sig, f"market not against the trigger: {share:.0%} of {ctx.get('names')} names agree "
                                  f"> {CT_M_MARKET_AGAINST_MAX:.0%}", gate="market_with", breadth=share)
            return
        plan = self.fade_plan(sig, ctx)
        if plan is None:
            await self._book_skip(key.value, sig, "no fade plan (no ATR30)", gate="no_plan", breadth=share)
            return
        plan["breadth"] = share
        fade = replace(
            sig,
            strategy=key,
            direction=Direction(plan["direction"]),
            stop=plan["stop"],
            targets=tuple(plan["targets"]),
            grade=plan["grade"],
            rr=plan["rr"],
            reason=(f"MARKET FADE of {sig.signal_id}: {share:.0%} of {ctx.get('names')} names agree with the trigger "
                    f"≤ {CT_M_MARKET_AGAINST_MAX:.0%}; stop 1 ATR past the close"),
            source_signal_id=sig.signal_id,
            evidence={**dict(sig.evidence), "breadth": share, "rr": plan["rr"]},
            context={
                **dict(sig.context),
                "market_fade": plan,
                "confluence": {"stop": plan["stop"], "stop_zone": "1 ATR30 past the close", "targets": plan["targets"],
                               "target_zones": [plan["targetNote"]], "grade": plan["grade"], "rr": plan["rr"]},
            },
        )
        await self.ledger.event("counter.market_fade", {"signal_id": sig.signal_id, "symbol": sig.symbol, "fade_signal_id": fade.signal_id, **plan})
        log.info("market_fade", symbol=sig.symbol, side=plan["side"], breadth=share, stop=plan["stop"], t1=plan["targets"][0])
        await self._handle_signal(fade, bar, adopt=False)

    async def _gap_fade(self, sig: Signal, bar: UnifiedBar | None, underlying: Instrument) -> None:
        """CT-Y's entry on a 09:45 gap-with trigger: the plan above, through the ordinary entry
        path under CT-Y's own key (CT-X is untouched; CT-Y does not also mirror CT-X's fade)."""
        if sig.signal_id in self._gap_faded:
            return
        ctx = self._breadth_at.get(sig.signal_id)
        if ctx is None:
            ctx = self.trigger_context(sig)
        plan = self.gap_fade_plan(sig, ctx)
        if plan is None:
            return
        lim = self._exits_by_strategy[StrategyKey.FUDKII_CT_Y.value].limits
        event = {"signal_id": sig.signal_id, "symbol": sig.symbol, **plan}
        if lim.gap_fade_min_rr is not None and plan["rr"] < lim.gap_fade_min_rr:
            event["blocked"] = f"RR {plan['rr']:.2f} < {lim.gap_fade_min_rr:g}"
            await self.ledger.event("counter.gap_fade", event)
            log.info("gap_fade.blocked", symbol=sig.symbol, rr=plan["rr"])
            return
        fade = replace(
            sig,
            strategy=StrategyKey.FUDKII_CT_Y,
            direction=Direction(plan["direction"]),
            stop=plan["stop"],
            targets=tuple(plan["targets"]),
            grade=plan["grade"],
            rr=plan["rr"],
            reason=f"GAP FADE of {sig.signal_id}: 09:45 gap with the trigger {plan['gapDatr']:.2f} daily ATR; stop 1 ATR past the close",
            source_signal_id=sig.signal_id,
            evidence={**dict(sig.evidence), "gap_datr": plan["gapDatr"], "rr": plan["rr"]},
            context={
                **dict(sig.context),
                "gap_fade": plan,
                "confluence": {"stop": plan["stop"], "stop_zone": "1 ATR30 past the close", "targets": plan["targets"],
                               "target_zones": [plan["targetNote"]], "grade": plan["grade"], "rr": plan["rr"]},
            },
        )
        self._gap_faded.add(sig.signal_id)
        event["fade_signal_id"] = fade.signal_id
        await self.ledger.event("counter.gap_fade", event)
        log.info("gap_fade", symbol=sig.symbol, side=plan["side"], stop=plan["stop"], t1=plan["targets"][0], rr=plan["rr"])
        await self._handle_signal(fade, bar, adopt=False)

    async def _counter_legs(self, underlying: Instrument, bar: UnifiedBar, *, low_priority: bool = False) -> list[Leg]:
        """The equity's side of the trigger from the store, the front future's from the broker
        (``_fut_context``): candle, ATR30m, classic levels with their weights, volume surges."""
        from .instrument.legs import levels_from_candles, weekly_from_rows
        from .market.session import to_ist

        eq_bars = self.store.bars(underlying.symbol, DECISION_TF, 60)
        vr = self._volume_reading(underlying.symbol, bar.ts, [b for b in eq_bars if b.ts <= bar.ts])
        s_t, s_t1 = (vr.surge_t, vr.surge_t1) if vr.ok else (None, None)
        legs = [Leg(
            "equity", bar.open, bar.high, bar.low, bar.close, atr(eq_bars, 14) or 0.0,
            self._pivot_points(underlying.symbol), s_t, s_t1,
        )]
        ctx = await self._fut_context(underlying, low_priority=low_priority)
        if ctx is None:
            return legs
        trigger = to_ist(bar.ts).strftime("%Y-%m-%dT%H:%M")
        rows = [r for r in ctx["bars30"] if str(r["dt"])[:16] <= trigger]
        if not rows or str(rows[-1]["dt"])[:16] != trigger:
            return legs
        t = rows[-1]
        trs = [
            max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"]))
            for a, b in zip(rows[-15:-1], rows[-14:], strict=False)
        ]
        fr = self._fut_volume_reading(rows, bar.ts)
        f_t, f_t1 = (fr.surge_t, fr.surge_t1) if fr.ok else (None, None)
        today = ist_today()
        points: list[PivotPoint] = []
        got = levels_from_candles(ctx["rows1d"], today, min_volume=0.0)
        if got:
            points += pivot_points(got[0], "1d")
        wk = weekly_from_rows(ctx["front"], ctx["rows1d"], today)
        if wk:
            points += pivot_points(wk[0], "1wk")
        legs.append(Leg(
            "future", float(t["o"]), float(t["h"]), float(t["l"]), float(t["c"]),
            sum(trs) / len(trs) if trs else 0.0, points, f_t, f_t1,
        ))
        return legs

    async def _fut_context(self, underlying: Instrument, *, low_priority: bool = False) -> dict[str, Any] | None:
        """The front future's side of a trigger — its 30m candles for the last three sessions and
        its daily candles for the levels — fetched from the broker once per trigger bar (the engine
        holds no futures bars) and shared by the counter route and the dried-volume gate. None when
        there is no future or the broker does not answer: absent, never a verdict."""
        if underlying.segment is not Segment.NSE_EQ:
            return None
        front = self.catalogue_loader.catalogue.front_future(underlying.symbol, on=ist_today())
        if front is None:
            return None
        eq = self.store.bars(underlying.symbol, DECISION_TF, 1)
        bucket = int(eq[-1].ts) if eq else 0
        hit = self._fut_cache.get(underlying.symbol)
        if hit is not None and hit[0] == bucket:
            return hit[1]
        # The dried-volume gate and the fade route read this for the same bar at the same moment
        # since the two run side by side (2026-09-28): one fetch, shared, and a reader's
        # cancellation never cancels it for the others.
        key = (underlying.symbol, bucket)
        task = self._fut_inflight.get(key)
        if task is None:
            # a trigger FUDKII did not publish is read on its own, smaller queue: at a busy bar its
            # reads must never delay a published trigger's dried-volume gate (review, 2026-09-28)
            sem = self._fut_sem_low if low_priority else self._fut_sem
            task = asyncio.ensure_future(self._fut_context_fetch(underlying, front, bucket, sem))
            self._fut_inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._fut_inflight.pop(k, None))
        return await asyncio.shield(task)

    async def _fut_context_fetch(self, underlying: Instrument, front: Instrument, bucket: int,
                                 sem: asyncio.Semaphore) -> dict[str, Any] | None:
        """``_fut_context``'s broker reads, once per (name, bar)."""
        today = ist_today()
        start30 = self.calendar.previous_trading_day(self.calendar.previous_trading_day(today))
        try:
            async with sem:
                rows30 = await self.rest.candles(front, DECISION_TF, start30.isoformat(), today.isoformat())
                # the daily rows only set the previous sessions' levels: once a day per future, not
                # once per trigger — it was the second serial call on every trigger (review, 2026-10-03)
                held1d = self._fut_daily_rows.get((front.scrip_code, today))
                if held1d is None:
                    held1d = await self.rest.candles(front, "1d", (today - timedelta(days=35)).isoformat(), today.isoformat())
                    self._fut_daily_rows = {k: v for k, v in self._fut_daily_rows.items() if k[1] == today}
                    if held1d:
                        # only an answer with candles is held for the day: an empty 200 is asked again
                        # by the next bar, as before Stage 4 — held, the future had no daily or weekly
                        # levels for the rest of the session (review, 2026-10-03)
                        self._fut_daily_rows[(front.scrip_code, today)] = held1d
                rows1d = held1d
        except Exception as exc:  # noqa: BLE001 — a route input must never fail the fill path
            log.warning("fut.context_unknown", symbol=underlying.symbol, error=str(exc)[:120])
            return None
        # the future's rows on its session grid (its ATR, candle and volume all read them), and any
        # bucket the broker returned nothing for rebuilt from the day's 1m candles
        clean = self._snap_fut_rows(front, rows30)
        fill_failed = False
        if bucket:
            clean, fill_failed = await self._fill_fut_gaps(front, clean, bucket, sem=sem)
        ctx = {"front": front, "bars30": clean, "rows1d": rows1d}
        if not fill_failed:  # a 1m call that failed is asked again by the next reader of this bar
            self._fut_cache[underlying.symbol] = (bucket, ctx)
        return ctx

    @staticmethod
    def _snap_fut_rows(front: Instrument, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The future's 30m rows on its session grid. 5paisa stamps a candle with the minute of its
        first trade — TATAPOWER SEP 2026-09-07 ``10:46`` (146,450 shares) IS the 10:45 bucket; 19 %
        of the futures rows in the Aug–Sep history are stamped so — and the readers compare ``dt``
        against the trigger bucket as a string, so each in-session row is snapped to its bucket
        start, ``dt`` rewritten (a bucket's own on-grid row wins, should both come). Rows outside the
        session (a future trades 09:15–15:30) are not bars. One implementation: market/candles.py."""
        return snap_candles(rows, front.segment, DECISION_TF).rows

    async def _fill_fut_gaps(self, front: Instrument, rows: list[dict[str, Any]], t_ts: int, *,
                             sem: asyncio.Semaphore | None = None) -> tuple[list[dict[str, Any]], bool]:
        """A bucket among the ``FUT_GAP_SLOTS`` ending at the trigger (the volume reading's eight
        and the futures leg's 15-bar ATR) that the broker returned NO row for — 0.46 % of the
        session slots in the Aug–Sep history, once the first-trade stamps are snapped — is rebuilt
        from that day's 1m candles, whose sums reproduce the broker's 30m bars (670 of 670 on
        23–24 Sep). A slot the 1m candles hold nothing for either (no trade at all) stays missing
        and the reading is doubtful rather than shifted. Returns the rows and whether a 1m call
        failed (the caller then does not cache the context, so the next reader asks again)."""
        need = session_buckets_back(front.segment, t_ts, FUT_GAP_SLOTS, DECISION_TF, self.calendar)
        have = {int(ist_naive_to_ts(str(r["dt"]))): r for r in rows}
        missing = {s for s in need if s not in have}
        if not missing:
            return rows, False
        filled, failed = 0, False
        for d in sorted({ist_day(s) for s in missing}):
            try:
                async with sem or self._fut_sem:
                    m1 = await self.rest.candles(front, "1m", d.isoformat(), d.isoformat())
            except Exception as exc:  # noqa: BLE001 - a gap unfilled is a doubtful reading, never an error
                failed = True
                log.warning("fut.gap_fill_failed", symbol=front.symbol, day=d.isoformat(), error=str(exc)[:120])
                continue
            built: dict[int, dict[str, Any]] = {}
            for r in m1:  # oldest first: the bucket's open is its first minute, its close its last
                ts = ist_naive_to_ts(str(r["dt"]))
                if not in_session(front.segment, ts):
                    continue
                b = int(bucket_start(front.segment, ts, DECISION_TF))
                if b not in missing:
                    continue
                row = built.get(b)
                if row is None:
                    built[b] = {"dt": to_ist(b).strftime("%Y-%m-%dT%H:%M:%S"), "o": float(r["o"]), "h": float(r["h"]),
                                "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"]), "src": "1m"}
                else:
                    row["h"], row["l"] = max(row["h"], float(r["h"])), min(row["l"], float(r["l"]))
                    row["c"] = float(r["c"])
                    row["v"] += float(r["v"])
            for b, row in built.items():
                have[b] = row
                filled += 1
        log.info("fut.gaps_filled", symbol=front.symbol, missing=len(missing), filled=filled,
                 slots=[to_ist(s).strftime("%d %b %H:%M") for s in sorted(missing)][:6])
        return [have[k] for k in sorted(have)], failed

    def _segment_of(self, symbol: str) -> Segment | None:
        """The underlying's segment, from the live universe. None when the name is not in it —
        treated as 'not the reserved segment', so an unknown name is never hidden from the books
        that do trade everything, and never shown on the one that does not."""
        inst = self.underlyings.get(symbol)
        return inst.segment if inst is not None else None

    def _pivot_points(self, symbol: str) -> list[PivotPoint]:
        """The equity's classic levels for today — daily from the previous session, weekly and
        monthly from the previous completed periods — with their timeframe weights. None where the
        zones refuse the name (bars/zones.py: history, a provisional candle, a basis mismatch)."""
        built = self._zone_build(symbol)
        return list(built.points) if built is not None else []

    async def _volume_surges_safe(self, underlying: Instrument, *, low_priority: bool = False) -> dict[str, tuple[float, float]]:
        """``_volume_surges`` that never raises: a reading that cannot be taken is no reading, and
        no reading never blocks an entry. ``low_priority``: a trigger FUDKII did not publish —
        its futures read waits on the smaller queue (``_fut_context``)."""
        try:
            return await self._volume_surges(underlying, low_priority=low_priority)
        except Exception as exc:  # noqa: BLE001 - no reading is information, never an error to the books
            log.warning("volume.read_failed", symbol=underlying.symbol, error=str(exc))
            return {}

    async def _volume_surges(self, underlying: Instrument, *, low_priority: bool = False) -> dict[str, tuple[float, float]]:
        """``surge_T`` / ``surge_T-1`` of the last two closed 30m bars against the T-2…T-7 baseline
        (``volume_surges``, floor 1000) — for the underlying from the store, and for its front
        future from the broker's candles, since the engine holds no futures bars. A leg the data
        cannot answer for is left out: absent, not dried — and so is a DOUBTFUL one
        (``_volume_reading``: a missing, zero or flagged bar, or a market-wide alarm at the bar),
        logged with its reason. Read at twin time only, so the parent's fill path never waits on it."""
        out: dict[str, tuple[float, float]] = {}
        eq = self.store.bars(underlying.symbol, DECISION_TF, 40)
        if not eq:
            return out
        t_ts = eq[-1].ts
        vr = self._volume_reading(underlying.symbol, t_ts, eq)
        if vr.ok:
            out["equity"] = (vr.surge_t, vr.surge_t1)  # type: ignore[assignment]
        else:
            log.warning("volume.doubtful", symbol=underlying.symbol, leg="equity", why=vr.doubt)
        if underlying.segment is not Segment.NSE_EQ:
            return out
        ctx = await self._fut_context(underlying, low_priority=low_priority)
        if ctx is None:
            return out
        fr = self._fut_volume_reading(ctx["bars30"], t_ts)
        if fr.ok:
            out["future"] = (fr.surge_t, fr.surge_t1)  # type: ignore[assignment]
        else:
            log.warning("volume.doubtful", symbol=underlying.symbol, leg="future", why=fr.doubt)
        return out

    def _volume_reading(self, symbol: str, t_ts: float, bars: Sequence[UnifiedBar] | None = None) -> VolumeReading:
        """The checked 30m volume reading of ``symbol`` at the bar starting ``t_ts``
        (``bars/volume_read.py``): the eight session slots ending there, none missing, zero or
        flagged — else doubtful, with the reason, and it decides nothing.

        An NSE stock counts only its continuous session (09:15 … 14:45): its 15:15 bar is the
        closing auction (HEROMOTOCO 2026-09-24: 55 shares in the broker's candle against 354,142
        on the tape, so every 09:45 dried check once tested the trigger bar alone), and a day
        fetched after the close also carries a 15:45 post-close bar (2026-09-25, HDFCBANK 3,203
        shares) that read 0.00x as the bar before the next 09:45 trigger — 37 of 208 names read
        "dried" on 2026-09-28. Futures, indices and MCX count their whole session. A bar the whole
        market reads as broken (``_market_volume``) makes every reading at it doubtful."""
        inst = self.underlyings.get(symbol)
        seg = inst.segment if inst is not None else Segment.NSE_EQ
        stock = inst is None or (seg is Segment.NSE_EQ and inst.kind is InstrumentKind.EQUITY)
        if bars is None:
            bars = self.store.bars(symbol, DECISION_TF, 40)
        prev_day = None
        if seg is Segment.MCX_FO:
            # The holiday list is NSE's, and MCX trades evenings on some NSE holidays: a day before
            # T counts as a session if the store holds MCX bars on it, OR the calendar says it
            # trades — so a session missing from the store is a MISSING one (doubtful), never
            # skipped over onto the session before it (review, 2026-09-28).
            days = {ist_day(b.ts) for b in bars}

            def prev_day(d: date) -> date:
                cur = d - timedelta(days=1)
                for _ in range(30):
                    if cur in days or self.calendar.is_trading_day(cur):
                        return cur
                    cur -= timedelta(days=1)
                return cur
        r = read_volume(
            [VolBar(int(b.ts), float(b.volume), str(b.extra.get("volume_doubt") or "")) for b in bars],
            segment=seg, t_ts=t_ts, calendar=self.calendar, until=NSE_EQ_CONTINUOUS_UNTIL if stock else None,
            prev_day=prev_day,
        )
        if r.ok and stock and seg is Segment.NSE_EQ:
            mv = self._market_volume(int(t_ts))
            if mv.alarm:
                return VolumeReading(doubt=f"market-wide volume alarm at this bar: {mv.alarm}", kind="market")
        return r

    def _fut_volume_reading(self, rows: list[dict[str, Any]], t_ts: float) -> VolumeReading:
        """The front future's reading at the trigger's bar, from the broker's candles — the same
        checks; a future trades to 15:30, so its 15:15 bar is a real (15-minute) bar."""
        bars = [VolBar(int(ist_naive_to_ts(str(r["dt"]))), float(r.get("v") or 0.0)) for r in rows]
        return read_volume(bars, segment=Segment.NSE_FO, t_ts=t_ts, calendar=self.calendar)

    def volume_view(self, symbol: str, ts: int | None = None) -> dict[str, Any]:
        """Both volume readings of one bar (the last closed 30m one by default): the T-2…T-7 reading
        the gates use, and the same-slot reading (``bars/volume_read.slot_reading``) — side by side,
        so the operator can see where the intraday U-shape is mistaken for drying up."""
        symbol = symbol.upper()
        held = self.store.bars(symbol, DECISION_TF)
        if not held:
            return {"symbol": symbol, "error": "no 30m bars held"}
        t_ts = int(ts) if ts else int(held[-1].ts)
        r = self._volume_reading(symbol, t_ts)
        vb = [VolBar(int(b.ts), float(b.volume), str(b.extra.get("volume_doubt") or "")) for b in held]
        slot = slot_reading(vb, t_ts)
        return {"symbol": symbol, "ts": t_ts, "slot": slot.slot,
                "window": {"surgeT": r.surge_t, "surgeT1": r.surge_t1, "baseline": r.baseline, "doubt": r.doubt},
                "sameSlot": slot.to_json()}

    def _market_volume(self, ts: int) -> MarketVolume:
        """The market-wide check at one bar, once: every NSE stock's own reading there. A bar the
        whole tape reads as impossible — a median T-1 of 0.002 (2026-09-28 09:45), or a third of
        the names doubtful — is a data fault; it alarms once and no reading at it decides."""
        if not on_session_grid(Segment.NSE_EQ, ts, DECISION_TF, until=NSE_EQ_CONTINUOUS_UNTIL) or ist_day(ts) != ist_today():
            # the 15:15 auction bar is no reading, and an older session's bars are outside the
            # 40-bar windows read here: not judged — no alarm, nothing cached
            return MarketVolume(ts=ts)
        hit = self._vol_market.get(ts)
        if hit is not None:
            return hit
        readings = []
        for sym, und in self.underlyings.items():
            if und.segment is not Segment.NSE_EQ or und.kind is not InstrumentKind.EQUITY:
                continue
            bars = self.store.bars(sym, DECISION_TF, 40)
            if not bars or int(bars[-1].ts) < ts:
                continue  # this name has no bar at ts yet — not a reading
            readings.append(read_volume(
                [VolBar(int(b.ts), float(b.volume), str(b.extra.get("volume_doubt") or "")) for b in bars],
                segment=Segment.NSE_EQ, t_ts=ts, calendar=self.calendar, until=NSE_EQ_CONTINUOUS_UNTIL,
            ))
        mv = market_volume(ts, readings)
        if mv.names >= 50:  # too early in the bar's close to judge: judged again on the next ask
            self._vol_market[ts] = mv
            while len(self._vol_market) > 64:
                self._vol_market.pop(next(iter(self._vol_market)))
            if mv.alarm:
                log.error("volume.market_alarm", bar=to_ist(ts).strftime("%d %b %H:%M"), **mv.to_json())
                self.telegram.fire_and_forget(
                    f"⚠️ Volume data alarm, {to_ist(ts).strftime('%H:%M')} bar: {mv.alarm}. "
                    "No volume reading at this bar decides anything (dried-volume gate off, fade volume unknown).",
                    key=f"volalarm:{ts}",
                )
                try:
                    task = asyncio.get_running_loop().create_task(self.ledger.event("volume.market_alarm", mv.to_json()))
                    self._decision_tasks.add(task)
                    task.add_done_callback(self._decision_tasks.discard)
                except RuntimeError:  # no loop (a synchronous caller): the log and the alert stand
                    pass
        return mv

    async def _audit_bars(self, day: date) -> None:
        """After the close: every NSE stock's continuous 30m bars of ``day`` against the broker's
        candles fetched once more (``BarReconciler.audit_day``) — repaired or added where they
        differ, and an alert past ``BAR_AUDIT_ALERT_*``. What the bar-close check missed (a
        reconcile that timed out, a bucket never built) is fixed before the next session reads it."""
        syms = [s for s, i in self.underlyings.items() if i.segment is Segment.NSE_EQ and i.kind is InstrumentKind.EQUITY]
        try:
            res = await self.reconciler.audit_day(
                syms, day, keep=lambda inst, ts: on_session_grid(inst.segment, ts, DECISION_TF, until=NSE_EQ_CONTINUOUS_UNTIL),
            )
        except Exception as exc:  # noqa: BLE001 - an audit that fails is reported, never fatal
            log.warning("bars.audit_failed", day=day.isoformat(), error=str(exc)[:160])
            self._bar_audit = {"day": day.isoformat(), "error": str(exc)[:160], "ts": time.time()}
            return
        res["ts"] = time.time()
        res["of"] = len(syms)
        self._bar_audit = res
        bad = res["repaired"] + res["added"]
        log.info("bars.audit", **{k: v for k, v in res.items() if k != "examples"}, examples=res["examples"][:3])
        await self.ledger.event("bars.audit", res)
        if bad > max(BAR_AUDIT_ALERT_MIN, BAR_AUDIT_ALERT_SHARE * res["bars"]) or res["failed"] > 0.1 * max(1, len(syms)):
            self.telegram.fire_and_forget(
                f"⚠️ Bar audit {day:%d %b}: {res['repaired']} repaired, {res['added']} added of {res['bars']} bars; "
                f"{res['failed']} of {len(syms)} names unanswered — " + "; ".join(res["examples"][:3]),
                key=f"baraudit:{day.isoformat()}",
            )

    def _volume_check(self) -> Check:
        """The health line for volume data: the latest judged NSE bar's market-wide check — red
        while NSE is open and that bar alarmed, informational once it has closed (an alarm at 14:45
        is not a fault all evening; the Telegram alert said so at the time)."""
        mv = next(reversed(self._vol_market.values()), None) if self._vol_market else None
        if mv is None:
            return Check("volume_data", True, detail="no bar judged yet")
        when = to_ist(mv.ts).strftime("%d %b %H:%M")
        med = (f"median T {mv.median_t:.2f}x / T-1 {mv.median_t1:.2f}x" if mv.median_t is not None and mv.median_t1 is not None else "no median")
        detail = f"{when} bar: ALARM — {mv.alarm}" if mv.alarm else f"{when} bar: {mv.names} names, {med}, {mv.doubtful} doubtful"
        nse_open = is_open(Segment.NSE_EQ, time.time(), self.calendar)
        return Check("volume_data", not mv.alarm or not nse_open, detail=detail if nse_open else f"NSE closed · {detail}")

    def _bar_audit_check(self) -> Check:
        a = self._bar_audit
        if a is None:
            return Check("bar_audit", True, detail="not run since boot (runs after 15:40)")
        if a.get("error"):
            return Check("bar_audit", False, detail=f"{a['day']}: failed — {a['error']}")
        bad = a["repaired"] + a["added"]
        # what was repaired is fixed data (the alert said how much); only an audit that could not
        # look — too many names unanswered — leaves the day's bars unverified
        return Check(
            "bar_audit", a["failed"] <= 0.1 * max(1, a.get("of", 1)), value=float(bad),
            detail=f"{a['day']}: {a['exact']} of {a['bars']} bars exact, {a['repaired']} repaired, {a['added']} added, {a['failed']} names unanswered",
        )

    def expected_move(self, symbol: str, inst: Instrument, premium: float) -> float:
        """One day's expected move of the parent (its own IV, or its median) through delta, as a
        fraction of the premium — the unit RT-Y arms and gives back in."""
        from .instrument.select import estimate_delta

        iv = self.stock_iv.get(symbol)
        iv_v = iv[0] if iv else self.iv_history.median_before(symbol, ist_today())[0]
        spot = self.ltps.get(getattr(self.underlyings.get(symbol), "scrip_code", "")) or 0.0
        if not spot or not iv_v:
            return 0.0
        delta = abs(estimate_delta(spot=spot, strike=inst.strike, option_type=inst.option_type))
        return expected_move_frac(spot, iv_v, delta, premium)

    @staticmethod
    def selection_policy_for(key: StrategyKey) -> SelectionPolicy:
        """The selector's policy for a book: the shared one, minus the premium floor for the
        FUDKII family — which also passes over a strike whose 4 lots cost ₹75,000 or more for the
        next one out (operator, 2026-09-22: "try identifying far otm who's 4 lots rest within our
        cap"; the cap 2026-09-27). The MCX route selects a future, never through this."""
        if key in NO_PREMIUM_FLOOR:
            return replace(SELECTION_POLICY, min_premium=0.0, outlay_lots=4, outlay_under_inr=FIXED_LOTS_UNDER_INR)
        if key is StrategyKey.FUKAA:  # sizes on FUDKII's limits: the same 4 lots under ₹75,000
            return replace(SELECTION_POLICY, outlay_lots=4, outlay_under_inr=FIXED_LOTS_UNDER_INR)
        return SELECTION_POLICY

    #: Books that trade one exchange only. FUDKII-RT-MCX is the commodity book — its wallet is
    #: sized in CRUDEOIL lots and its exits were tuned on commodities — so an NSE fill reaching it
    #: is a bug, not a diversification. (There is no currency segment in this engine at all:
    #: ``config.Segment`` is NSE_EQ / NSE_FO / NSE_IDX / MCX_FO.)
    SEGMENT_BOOKS: ClassVar[dict[str, Segment]] = {StrategyKey.FUDKII_RT_MCX.value: Segment.MCX_FO}

    @classmethod
    def book_trades(cls, book: str, segment: Segment | None) -> bool:
        """Whether ``book`` may see an underlying in ``segment``. A book with no restriction
        takes everything except the segments another book is reserved for."""
        want = cls.SEGMENT_BOOKS.get(book)
        if want is not None:
            return segment is want
        return segment not in set(cls.SEGMENT_BOOKS.values())

    def limits_for(self, strategy: str) -> RiskLimits:
        """A book's own limits: its exit engine's, else the parent's (FUDKII, FUKAA)."""
        book = self._exits_by_strategy.get(strategy)
        return book.limits if book is not None else self.limits

    @staticmethod
    def floored_option_stop(key: StrategyKey, premium: float, option_sl: float, tick: float) -> float:
        """The δ-projected option stop, never nearer than MIN_STOP_TICKS below the premium for the
        books that trade cheap contracts."""
        if key not in NO_PREMIUM_FLOOR or premium <= 0:
            return option_sl
        tick = tick or 0.05
        return round(min(option_sl, max(tick, premium - MIN_STOP_TICKS * tick)), 2)

    async def _select_instrument(self, underlying: Instrument, sig: Signal, *, tape: bool = True) -> Any:
        """The contract this signal buys. ``tape=False`` for a *preview*: the trigger-card page
        asks this question for six books on every card it renders, and a read-only preview must
        not put six strikes a book onto the tape (109 codes were watched on a boot that had
        placed nothing — found 2026-09-23 on the first live boot of the tape)."""
        cat = self.catalogue_loader.catalogue
        now = time.time()
        pol = self.selection_policy_for(sig.strategy)
        if underlying.segment is Segment.MCX_FO:
            # the contract the chart reads and the trigger came from (the universe's roll: 5 days or
            # fewer to expiry reads the next month), never the nearest unexpired one behind its back
            front = underlying if underlying.kind is InstrumentKind.FUTURE else cat.front_future(sig.symbol)
            q = self.quotes.get(front.scrip_code) if front else None
            if front is not None and tape:
                self.tape.follow(sig.symbol, [front.scrip_code], role=ROLE_FUTURE, now=now)
            return select_future(front=front, quote=q, now=now)
        expiry = choose_expiry(cat.expiries(sig.symbol), ist_today(), pol)
        if expiry is None:
            return select_option(
                chain=[], quotes={}, spot=sig.entry, target1=None, direction=sig.direction, now=now
            )
        chain = cat.chain(sig.symbol, expiry, sig.direction.option_type)
        # Two candidates — one ATR out, and the confluence target — decided on liquidity and
        # floored on delta. The exit ladder is untouched: targets and stops are the confluence
        # engine's, for every book and twin. `_strike_watch` is what gets quoted and taped.
        atr30 = atr(self.store.bars(sig.symbol, DECISION_TF, 60), 14) or 0.0
        watch = self._strike_watch(chain, sig.entry, sig.direction, atr30, sig.targets)
        # The candidates AND the strikes around spot the fallback walks: quoting only the
        # candidates left the next-best strikes with no quote at all, so a one-sided first choice
        # lost the trigger (INDUSTOWER, RADICO, 2026-09-24 12:45).
        await self._ensure_quotes(chain, sig.entry, extra=watch)
        if tape:
            self.tape.follow(sig.symbol, [i.scrip_code for i in (watch or chain[:6])], now=now)
        # the decision waits for prices it does not know yet; a card preview never waits
        sel = await self._choose_option(chain=chain, sig=sig, pol=pol, atr30=atr30, watch=watch, wait=tape)
        if watch and sel.instrument is not None:
            picks, why = strike_candidates(
                otm=[i for i in watch if (i.strike > sig.entry) is (sig.direction is Direction.BULLISH)],
                spot=sig.entry, direction=sig.direction, atr=atr30,
                target1=sig.targets[0] if sig.targets else None,
                liquidity=self.liquidity_for(watch),
                delta_floor=pol.min_delta, oi_margin=pol.oi_margin,
            )
            if picks and picks[0].scrip_code != sel.instrument.scrip_code:
                log.info(
                    "strike.fell_back", symbol=sig.symbol, wanted=picks[0].strike,
                    took=sel.instrument.strike, why=why, reason=sel.reason,
                )
        if tape and sel.instrument is not None:
            await self._ensure_leg_ladder(sel.instrument)
        return sel

    async def _ensure_leg_ladder(self, inst: Instrument) -> None:
        """Guarantee the contract we are about to buy has its own previous-session ladder.

        The re-anchor above keeps the bulk set near the live spot, but it runs on a clock and the
        strike finally chosen can still sit outside it. Without a ladder an own-ladder book opens
        with no T1 and nothing to trail — which is what happened to BANKNIFTY on 2026-09-24, its
        traded 55300 PE eleven strikes below a set banded on the previous close. One REST call,
        alongside the quote fetch and bounded, so a slow broker delays an entry by no more than
        LEG_ENSURE_TIMEOUT_S; a miss is logged and the book falls back to the percentage arm.
        """
        if self.leg_pivots.for_code(inst.scrip_code) is not None:
            return
        try:
            await asyncio.wait_for(
                self.leg_pivots.ensure(inst, ist_today()), timeout=LEG_ENSURE_TIMEOUT_S
            )
        except TimeoutError:
            log.warning("legs.ensure_timeout", scrip=inst.scrip_code, name=inst.name or inst.symbol)
        except Exception as exc:  # noqa: BLE001 - advisory levels never block an entry
            log.warning("legs.ensure_failed", scrip=inst.scrip_code, error=str(exc)[:120])
        else:
            if self.leg_pivots.for_code(inst.scrip_code) is not None:
                log.info("legs.ensured", scrip=inst.scrip_code, name=inst.name or inst.symbol)

    def _strike_watch(
        self, chain: list[Instrument], spot: float, direction: Direction, atr30: float,
        targets: tuple[float, ...],
    ) -> list[Instrument]:
        """The strikes worth quoting and taping: the two candidates and everything between them,
        so the fallback has somewhere to walk. Empty when there is nothing OTM or no ATR."""
        if spot <= 0 or atr30 <= 0:
            return []
        bullish = direction is Direction.BULLISH
        otm = [i for i in chain if (i.strike > spot if bullish else i.strike < spot) and i.strike > 0]
        if not otm:
            return []
        levels = [spot + atr30 if bullish else spot - atr30]
        if targets:
            levels.append(targets[0])
        edges = [min(otm, key=lambda i: abs(i.strike - lv)).strike for lv in levels]
        lo, hi = min(spot, *edges), max(spot, *edges)
        return sorted((i for i in otm if lo <= i.strike <= hi), key=lambda i: abs(i.strike - spot))

    def liquidity_for(self, chain: list[Instrument]) -> dict[str, tuple[float, float]]:
        """``(volume, open interest)`` per scrip code, for ranking the strikes in the span."""
        out: dict[str, tuple[float, float]] = {}
        for i in chain:
            code = i.scrip_code
            oi = (self.option_oi.get(code) or {}).get("oi", 0.0)
            out[code] = (self.option_volume.get(code, 0.0), float(oi))
        return out

    async def _ensure_quotes(
        self, chain: list[Instrument], spot: float, *, extra: list[Instrument] | None = None
    ) -> None:
        """Snapshot-quote the handful of strikes near the money.

        One batched REST call, not an inline per-strike fetch. The old enricher took 3–23 seconds
        on a cache miss and *blocked signal publication* while it did.
        """
        if not chain:
            return
        near = sorted(chain, key=lambda i: abs(i.strike - spot))[:12]
        seen = {i.scrip_code for i in near}
        near += [i for i in (extra or []) if i.scrip_code not in seen]
        # Depth FIRST, and outside the staleness early-exit below: these are the strikes an order is
        # about to be priced against, and since depth only follows what is in use, a strike whose
        # price happens to be fresh would otherwise reach the matcher with no book at all.
        await self._follow_depth(near)
        now_ = time.time()
        limit_ms = self.matcher.age_limit_ms(now_)
        # A quote can be fresh while its depth book has aged past the matcher's limit (the socket
        # sends depth only on change) — the 09:45 stale-book entry rejections. Either one stale
        # means a snapshot.
        stale = [
            i for i in near
            if (q := self.quotes.get(i.scrip_code)) is None or now_ - q.ts > 20
            or (b := self.books.get(i.scrip_code)) is None or b.age_ms(now_) > limit_ms
        ]
        if not stale:
            return
        try:
            rows = await self.rest.market_feed(stale)
        except Exception as exc:  # noqa: BLE001
            log.warning("quotes.failed", n=len(stale), error=str(exc))
            return
        self._apply_snapshot(rows, time.time())
        await self.feed.subscribe("mf", stale)

    def _apply_snapshot(self, rows: dict[str, dict[str, Any]], now: float) -> None:
        """Install REST snapshot quotes: the quote, the LTP, and a one-level book wherever the real
        book is missing or past the matcher's age limit. A live 20-level book is never replaced.

        The broker's snapshot (V1/MarketFeed) carries the last price but NO bid or ask — all 47,448
        replies logged by 2026-09-28 had both at 0. Installed as a quote, it wiped good two-sided
        quotes the instant before a strike was chosen: HDFCBANK's 720 PE, 15.85 / 15.95 on the feed,
        read "one-sided" at the 09:45 trigger, and all three triggers that day (19 more on 22–25 Sep)
        were refused. So a snapshot without a two-sided price never replaces one. It CONFIRMS the
        held quote — and book — as current only when ``_still_stands``: the feed sends only changes,
        so silence on a live subscription whose last trade the broker agrees with is no change.

        An OPEN position's contract is the exception, as it always was (review, 2026-09-28): when the
        feed cannot vouch for its quote — disconnected, silent, or the broker has seen a trade the
        quote has not — it is marked from the broker's last price, fresh, as before this change.
        Kept at its old age instead, a feed outage over ``position_quote_max_age_s`` (25 Sep
        12:23–12:31, 8 minutes) turned every open position stale — only the equity stop and the
        backstops — and for the first minute its option mid was a price the broker had already
        contradicted.

        Two clocks (review, 2026-10-03). Every quote and book installed, confirmed or marked here is
        stamped ``now`` — when we saw it — because that is what every age guard asks. The row's
        ``traded_ts`` (5paisa's TickDt) decides only whether its price is NEWER than the one held
        (``Quote.superseded_by``): stamped into ``ts`` instead, a quiet held option read minutes "old"
        and ran on the equity stop alone, and a 5 s-cached older print could replace a fresh feed
        quote and leave its book unconfirmable."""
        limit = self.matcher.age_limit_ms(now)
        held_codes = {p.instrument.scrip_code for p in self.positions.values() if p.status == "OPEN"}
        for code, r in rows.items():
            ltp, bid, ask = float(r.get("ltp") or 0), float(r.get("bid") or 0), float(r.get("ask") or 0)
            two_sided = bid > 0 and ask > 0
            traded = float(r.get("traded_ts") or 0.0)
            held_q = self.quotes.get(code)
            held_two_sided = held_q is not None and held_q.bid > 0 and held_q.ask > 0
            newer = held_q is None or held_q.superseded_by(ltp, traded)
            if held_q is None or (two_sided and (newer or not held_two_sided)) or (not held_two_sided and newer):
                # labelled: a snapshot's 0/0 is "price unknown", never a one-sided market
                self.quotes[code] = Quote(ltp=ltp, bid=bid, ask=ask, ts=now, src="snapshot", traded_ts=traded)
            elif not newer and (held_q.src == "snapshot" or self._still_stands(code, held_q.ts, "mf")):
                # the broker has no newer trade: the same cached reply seen again, or a quiet contract
                # on a live feed — current as held, bid and ask kept, so its book can be confirmed too
                self.quotes[code] = replace(held_q, ts=now)
                self.snapshot_confirmed += 1
            elif code in held_codes and (newer or held_q.ltp > 0):
                # a held contract the feed cannot vouch for: marked fresh from the NEWEST trade known —
                # the broker's when it is newer, else the one held, never a cached older print (it
                # would move the mid backwards and could fire a false give-back) — and no bid / ask
                # the feed cannot vouch for
                px, at = (ltp, traded) if newer else (held_q.ltp, held_q.traded_ts)
                self.quotes[code] = Quote(ltp=px, bid=bid if newer else 0.0, ask=ask if newer else 0.0, ts=now,
                                          src="snapshot", traded_ts=at)
                self.snapshot_held_marked += 1
            else:
                self.snapshot_kept += 1  # held as it was: its own age says how old it is
                if newer:
                    # the broker has seen a trade the held quote has not (HCLTECH 1240 PE,
                    # 2026-09-28 11:15:04: 44.85 held, 45.65 traded — the feed's frame came 0.1 s on)
                    self._quote_outdated[code] = now
            if ltp > 0 and newer:
                # never a cached REST price over a newer feed print: 5paisa caches the snapshot for
                # 5 s, and an older price read as a fresh trade by a resting order (review, 2026-10-03)
                self.ltps[code] = ltp
                if traded > 0:
                    self._ltp_traded_ts[code] = traded
            if r.get("volume"):
                self.option_volume[code] = max(float(r["volume"]), self.option_volume.get(code, 0.0))
            held = self.books.get(code)
            if held is None or held.age_ms(now) > limit:
                # a side the snapshot does carry still makes a (one-sided) book to sell or buy into
                book = book_from_quote(
                    code, bid=bid, ask=ask,
                    bid_qty=int(r.get("bid_qty") or 0), ask_qty=int(r.get("ask_qty") or 0), ts=now,
                )
                if book is not None:
                    self.books[code] = book
                elif (held is not None and held.best_bid and held.best_ask and held_two_sided
                      # a book that disagrees with the live quote is from another time, whatever
                      # its subscription says (review, 2026-09-28: 12.00 / 12.20 beside 15.85 / 15.95)
                      and held.best_bid <= held_q.ltp <= held.best_ask  # type: ignore[union-attr]
                      and not newer and self._still_stands(code, held.ts, "md")):
                    self.books[code] = BookSnapshot(scrip_code=code, bids=held.bids, asks=held.asks, ts=now)
                    self.snapshot_book_confirmed += 1
                else:
                    log.debug("snapshot.no_book", scrip=code, bid=bid, ask=ask)

    def _still_stands(self, code: str, held_ts: float, channel: str) -> bool:
        """May a held quote (``mf``) or book (``md``) be read as current although the feed has sent
        nothing for it lately? Only when the contract is on that live subscription AND has been
        since before the value arrived — a value from an earlier subscription (depth drops a strike
        30 minutes after the selector last looked, and ``self.books`` keeps its last book) is from
        another time (review, 2026-09-28) — the feed has been connected since the value arrived (a
        reconnect gap could hide a change) and has spoken within ``FEED_LIVE_S``. Whether the broker
        has a newer trade is the caller's question (``Quote.superseded_by``). ``held_ts`` is an
        observation time, compared with the local subscribe / connect times."""
        fh = self.feed.health
        since_of = getattr(self.feed, "subscribed_since", None)
        since = since_of(channel, code) if since_of is not None else None
        silence = fh.silence_s
        return bool(
            since is not None and held_ts >= since
            and fh.connected and fh.connected_since is not None and held_ts >= fh.connected_since
            and silence is not None and silence < FEED_LIVE_S
        )

    async def _choose_option(
        self, *, chain: list[Instrument], sig: Signal, pol: SelectionPolicy, atr30: float,
        watch: list[Instrument], wait: bool,
    ) -> Selection:
        """The strike, chosen on prices that are KNOWN. The walk (``select_option``) passes over a
        strike it cannot price — no quote, a stale one, or only the broker's snapshot, which never has
        a bid or ask — and a strike subscribed a moment ago has nothing better until the feed's first
        frame (HDFCBANK 710 PE, 2026-09-28: 1.1 s; SONACOMS 770 PE, 2026-10-01 09:45:08: refused, and
        10.15 / 10.70 on the feed the same second). So the choice re-walks every ``QUOTE_POLL_S`` while
        a strike AHEAD of its current choice — or any, when it has none — can still be answered:

        * the strike is on the live ``mf`` subscription and the feed is speaking (else nothing comes);
        * intraday each such strike gets ``STRIKE_GRACE_S`` — one that never quotes (HDFCBANK OCT
          650/660 PE, 2026-09-28) costs that, not the whole wait — and the choice ``quote_wait_s``;
        * in the session's first ``OPEN_SETTLE_S`` a one-sided FEED quote is a book still filling
          (TATASTEEL 180 PE, 2026-10-01: 0 / 0 at 09:15:03, 2.42 / 2.53 at 09:15:27), so the choice may
          wait for it until the window closes — the carried triggers enter at the first print;
        * the choice itself, when the broker's last trade contradicts its held quote
          (``_quote_outdated``), waits for a newer frame within the same grace, and is read as it is
          if none comes.

        A strike that passes with nothing still open ahead of it is taken at once — a fast first
        choice never waits on a slow fallback. The wait is counted in polls (it ends in a replay
        too); ``quote_wait_s = 0`` never waits, nor does a card preview (``wait=False``)."""
        target1 = sig.targets[0] if sig.targets else None
        liquidity = self.liquidity_for(watch) if watch else None

        def walk() -> Selection:
            return select_option(chain=chain, quotes=self.quotes, spot=sig.entry, target1=target1,
                                 direction=sig.direction, now=time.time(), policy=pol, atr=atr30, liquidity=liquidity)

        sel = walk()
        if not wait or self.quote_wait_s <= 0:
            return sel
        und = self.underlyings.get(sig.symbol)
        t_start = time.time()
        open_until = session_open_ts(und.segment if und is not None else Segment.NSE_EQ, ist_today()) + OPEN_SETTLE_S
        cap_polls = int(max(self.quote_wait_s, open_until - t_start) / QUOTE_POLL_S)
        grace_polls = max(1, int(STRIKE_GRACE_S / QUOTE_POLL_S))
        spent: dict[str, int] = {}  # code -> polls waited on it
        polls, stop = 0, "answered"
        while True:
            at_open = time.time() < open_until
            ahead = self._answerable(sel, at_open=at_open)
            if not at_open:
                ahead = [c for c in ahead if spent.get(c, 0) < grace_polls]
            if not ahead:
                break
            if not self._feed_speaking():
                stop = "feed silent"
                break
            if polls >= cap_polls:
                stop = "cap"
                break
            await asyncio.sleep(QUOTE_POLL_S)
            polls += 1
            for c in ahead:
                spent[c] = spent.get(c, 0) + 1
            sel = walk()
        for c in spent:  # a newer quote has answered the contradiction — or the wait is over
            self._quote_outdated.pop(c, None)
        if sel.instrument is not None and sel.delta_shadow is not None:
            await self._log_delta_shadow(sig, sel, floor=pol.min_delta, walk_enforced=lambda: select_option(
                chain=chain, quotes=self.quotes, spot=sig.entry, target1=target1, direction=sig.direction, now=time.time(),
                policy=replace(pol, enforce_fallback_delta=True), atr=atr30, liquidity=liquidity))
        if polls or stop != "answered":
            strike = {i.scrip_code: i.strike for i in chain}
            log.info("quotes.awaited", symbol=sig.symbol, waited_ms=round((time.time() - t_start) * 1000), polls=polls,
                     awaited=sorted(strike[c] for c in spent if c in strike), stop=stop,
                     took=sel.instrument.strike if sel.instrument is not None else None, reason=sel.reason)
        return sel

    async def _log_delta_shadow(self, sig: Signal, sel: Selection, *, floor: float, walk_enforced: Any) -> None:
        """The fallback delta floor in shadow mode (operator, 2026-10-01): the strike is taken; the
        event says what an enforced floor would have bought instead — on the prices held this moment —
        or that it would have refused the trigger, so the two can be compared on outcomes."""
        alt = walk_enforced()
        payload = {
            "signal_id": sig.signal_id, "symbol": sig.symbol, "direction": sig.direction.value,
            "took": sel.instrument.strike if sel.instrument else None, "tookDelta": sel.delta_shadow,
            "floor": floor, "enforcedTook": alt.instrument.strike if alt.instrument else None,
            "enforcedReason": alt.reason,
        }
        log.info("strike.delta_shadow", **payload)
        await self.ledger.event("strike.delta_shadow", payload)

    def _answerable(self, sel: Selection, *, at_open: bool) -> list[str]:
        """The strikes ahead of ``sel``'s choice a live frame may still price: unpriced ones on the
        live subscription, the session's first minute's one-sided books, and the choice itself when
        its held quote is known old."""
        since_of = getattr(self.feed, "subscribed_since", None)
        codes = [*sel.unpriced, *(sel.one_sided if at_open else ())]
        out = [c for c in codes if since_of is None or since_of("mf", c) is not None]
        if sel.instrument is not None:
            code = sel.instrument.scrip_code
            q = self.quotes.get(code)
            if code in self._quote_outdated and (q is None or q.ts <= self._quote_outdated[code]):
                out.append(code)
        return out

    def _feed_speaking(self) -> bool:
        fh = self.feed.health
        silence = fh.silence_s
        return bool(fh.connected and silence is not None and silence < FEED_LIVE_S)

    async def _keep_held_quotes(self) -> None:
        """Re-quote held contracts that have gone quiet, every few seconds, while their segment is
        open. One batched call covers them all; nothing is called when nothing is stale."""
        while not self._stop.is_set():
            # a missed refresh is retried next pass — but counted, and a programming error fails
            # the tests: this one raised TypeError on every pass for days (review, 2026-10-03)
            async with self.guards.duty("held_quotes.refresh"):
                await self._refresh_held_quotes(time.time())
            await asyncio.sleep(HELD_QUOTE_POLL_S)

    async def _refresh_held_quotes(self, now: float) -> int:
        limit_ms = self.matcher.age_limit_ms(now)
        stale: dict[str, Instrument] = {}
        for p in self.positions.values():
            # the calendar is required: without it this raised TypeError on every pass with a position
            # open, swallowed as held_quotes.failed — no held quote was ever refreshed (review, 2026-10-03)
            if p.status != "OPEN" or not is_open(p.underlying.segment, now, self.calendar):
                continue
            code = p.instrument.scrip_code
            q, b = self.quotes.get(code), self.books.get(code)
            if q is None or now - q.ts > HELD_QUOTE_MAX_AGE_S or b is None or b.age_ms(now) > limit_ms:
                stale[code] = p.instrument
        if not stale:
            return 0
        rows = await asyncio.wait_for(self.rest.market_feed(list(stale.values())), timeout=5.0)
        self._apply_snapshot(rows, time.time())
        return len(rows)

    async def _refresh_exit_book(self, inst: Instrument, now: float) -> None:
        """Make sure an exit has a book to trade against.

        The depth socket only sends a book when it changes. A quiet contract — a ₹0.73 put nobody
        is trading — keeps a perfectly good book that simply ages past the matcher's limit, and the
        exit is refused as stale while the position bleeds. A snapshot quote stands a fresh
        one-level book behind it, exactly as the selector does for an entry. At most one quote per
        contract every two seconds, and bounded, so the exit loop is never held up for long."""
        code = inst.scrip_code
        held = self.books.get(code)
        if held is not None and held.age_ms(now) <= self.matcher.age_limit_ms(now):
            return
        if now - self._exit_quote_ts.get(code, 0.0) < 2.0:
            return
        self._exit_quote_ts[code] = now
        try:
            rows = await asyncio.wait_for(self.rest.market_feed([inst]), timeout=3.0)
        except Exception as exc:  # noqa: BLE001 - the matcher will say what it can't do
            log.warning("exit.quote_failed", scrip=code, error=str(exc)[:120])
            return
        if rows.get(code):
            self._apply_snapshot({code: rows[code]}, time.time())

    #: the books whose orders go to the broker in a LIVE mode. Every other book — each now places its
    #: own entry and exits — fills on PAPER against the same live book, so a LIVE session can never send
    #: one real order per book for a single trigger (before 2026-09-26 the twins copied the parent's
    #: fill, and their exits would have reached the broker for lots never bought there)
    LIVE_BOOKS = frozenset({StrategyKey.FUDKII.value})

    async def _submit(self, intent: OrderIntent, *, verdict_ok: bool, verdict_reason: str) -> Any:
        mode = self.mode()
        if mode in (Mode.LIVE, Mode.LIVE_CAPPED) and intent.strategy not in self.LIVE_BOOKS:
            return self.gateway.submit_paper(intent)
        if mode in (Mode.LIVE, Mode.LIVE_CAPPED):
            wallet = self.wallets[intent.strategy]
            if intent.purpose is Purpose.ENTRY and (short := await self._live_funds_short(intent)) is not None:
                return self.gateway.refused(intent, Decision.REJECTED_CAP, short)
            ctx = LiveContext(
                balance=wallet.balance,
                # each book's LIVE caps are its own (operator, 2026-09-26: "per-book caps")
                open_positions=len([p for p in self.positions.values() if p.status == "OPEN" and p.strategy == intent.strategy]),
                day_pnl_inr=wallet.day_pnl,
                now_hm_ist=ist_hm(time.time()),
                segment=intent.instrument.segment.value,
                exposure_ok=verdict_ok,
                exposure_reason=verdict_reason,
            )
            return await self.gateway.submit_live(intent, ctx=ctx)
        return self.gateway.submit(intent)

    # -- exit path -----------------------------------------------------------------------------------

    def _commit_outlay(self, wallet: Wallet, cost: float, now: float, pos: Position) -> None:
        """Deploy what the fill cost, and say so if it came to more than the book had left."""
        over = wallet.commit(cost, now)
        if over > 0:
            log.warning(
                "wallet.overdrawn",
                strategy=wallet.strategy,
                symbol=pos.underlying.symbol,
                cost=round(cost, 2),
                over=round(over, 2),
                deployed=round(wallet.deployed, 2),
                balance=round(wallet.balance, 2),
            )
            self.telegram.fire_and_forget(
                f"⚠️ {wallet.strategy} {pos.underlying.symbol}: fill cost ₹{cost:,.0f} was "
                f"₹{over:,.0f} more than the book had left — the slippage is deployed, not dropped",
                key=f"overdrawn:{wallet.strategy}",
            )

    async def _manage_positions(self) -> None:
        now = time.time()
        # resting limit orders first: a fill here changes what the positions below are
        await self._advance_resting(now)
        for pos in list(self.positions.values()):
            if pos.status != "OPEN":
                continue
            ltp = self.ltps.get(pos.instrument.scrip_code)
            if ltp is None or ltp <= 0:
                if pos.id not in self._stale_positions:
                    # Was silent. A position whose contract never prints is not being managed at
                    # all — no stop, no target, no force-flat — and nobody could tell.
                    log.warning(
                        "position.no_quote",
                        position=pos.id,
                        symbol=pos.underlying.symbol,
                        instrument=pos.instrument.name or pos.instrument.scrip_code,
                    )
                    self.telegram.fire_and_forget(
                        f"⚠️ {pos.strategy} {pos.underlying.symbol}: the contract has not printed "
                        f"since boot — the position is not being evaluated",
                        key=f"noquote:{pos.id}",
                    )
                self._stale_positions.add(pos.id)
                continue
            wallet = self.wallets[pos.strategy]
            # Only an OPERATOR halt closes positions. The gateway breaker and the reconcile freeze
            # stop new entries (self.halted() still reports them to the gateway), but they used to
            # force-close every book — the 2026-09-24 09:45 cascade — for a fault in the order path.
            halted = self._halted
            forced = self._past_force_flat(pos.strategy, pos.underlying.segment, now) or halted
            engine_for = self._exits_by_strategy.get(pos.strategy, self.exits)

            # An illiquid strike can stop ticking for minutes. Evaluating a stop against a price
            # that old is worse than not evaluating it — but staleness must never trap a position
            # past the force-flat, so a forced exit proceeds on the last known price and says so.
            q = self.quotes.get(pos.instrument.scrip_code)
            age = (now - q.ts) if q else None
            if age is not None and age > self.s.position_quote_max_age_s and not forced:
                if pos.id not in self._stale_positions:
                    self._stale_positions.add(pos.id)
                    log.warning(
                        "position.quote_stale",
                        position=pos.id,
                        symbol=pos.underlying.symbol,
                        instrument=pos.instrument.name or pos.instrument.scrip_code,
                        age_s=round(age, 1),
                    )
                    self.telegram.fire_and_forget(
                        f"⚠️ {pos.strategy} {pos.underlying.symbol}: no option quote for "
                        f"{age:.0f}s — only the equity stop and the backstops are being enforced",
                        key=f"stale:{pos.id}",
                    )
                stale_view = MarketView(
                    option_mid=None, spread_pct=None, quote_ok=False, option_ltp=ltp,
                    underlying_ltp=self.ltps.get(pos.underlying.scrip_code), now=now,
                    bars_held=pos.bars_held, past_force_flat=self._past_force_flat(pos.strategy, pos.underlying.segment, now),
                    halted=halted, daily_loss_hit=bool(wallet.daily_halt),
                )
                if (stale_exit := engine_for.evaluate_stale(pos, stale_view)) is not None:
                    await self._exit(pos, stale_exit, now)
                continue
            self._stale_positions.discard(pos.id)
            q = self.quotes.get(pos.instrument.scrip_code)
            mid = q.mid if q else None
            spread = (q.spread_pct / 100) if (q and q.spread_pct is not None) else None
            view = MarketView(
                option_mid=mid,
                spread_pct=spread,
                quote_ok=bool(q and (now - q.ts) <= self.s.position_quote_max_age_s),
                option_ltp=ltp,
                underlying_ltp=self.ltps.get(pos.underlying.scrip_code),
                now=now,
                bars_held=pos.bars_held,
                past_force_flat=self._past_force_flat(pos.strategy, pos.underlying.segment, now),
                halted=halted,
                daily_loss_hit=bool(wallet.daily_halt),
            )
            if view.quote_ok and mid:
                self._note_mid(pos.instrument.scrip_code, now, mid)
            # The exact numbers the exit is judged against, published so the card shows these
            # and not a second computation of them.
            self.position_marks[pos.id] = {
                "mid": mid,
                "spread_pct": spread,
                "quote_ok": view.quote_ok,
                "option_sl": pos.option_sl,
                "breach_since": pos.breach_since,
                "peak_mid": pos.peak_mid,
                "trail_dwell": pos.trail_dwell,
                "armed_by": pos.armed_by,
                "option_t1": pos.option_t1,
                "ts": now,
            }
            decision = engine_for.evaluate(pos, view)
            if decision is None:
                await self._ensure_resting_targets(pos, now)
                continue
            await self._exit(pos, decision, now)
        # after this tick's exits: the 15:15 plan records what is still open
        try:
            await self._record_eod_plan(now)
        except Exception as exc:  # a record must never cost the exits their pass
            log.exception("eod_plan.failed", error=str(exc))

    async def _record_eod_plan(self, now: float) -> None:
        """At ``EOD_PLAN_HM``, once a session: each open NSE position's distance to its stop line and to its
        next target, and what the 15:15 rule would do — an ``eod.plan`` event, never an order."""
        if ist_hm(now) < EOD_PLAN_HM:
            return
        day = to_ist(now).date().isoformat()
        if self._eod_plan_day == day:
            return
        self._eod_plan_day = day
        for pos in list(self.positions.values()):
            if pos.status != "OPEN" or pos.qty_remaining <= 0 or pos.underlying.segment is Segment.MCX_FO:
                continue
            code = pos.instrument.scrip_code
            bid, ask, ltp, _ = self._touch(code, now)
            mid = (bid + ask) / 2 if bid and ask else (ltp or None)
            q = self.quotes.get(code)
            ex = self._exits_by_strategy.get(pos.strategy, self.exits)
            view = MarketView(option_ltp=ltp or 0.0, underlying_ltp=self.ltps.get(pos.underlying.scrip_code), now=now,
                              bars_held=pos.bars_held, past_force_flat=False, option_mid=mid,
                              spread_pct=(q.spread_pct / 100) if (q and q.spread_pct is not None) else None, quote_ok=mid is not None)
            line = ex.stop_line(pos, view)
            resting = self._target_resting(pos.id)
            nxt = ex.resting_target(pos) if resting is None else None
            target = resting.limit if resting is not None else (nxt[1] if nxt else None)
            stop_pct = (mid - line) / mid * 100 if mid and line > 0 else None
            tgt_pct = (target - mid) / mid * 100 if mid and target else None
            flat = self._force_flat_hm(pos.strategy, pos.underlying.segment)
            if stop_pct is not None and stop_pct <= 0:
                verdict = f"exit now — already {-stop_pct:.1f}% through the stop line {line:g}"
            elif stop_pct is not None and stop_pct <= EOD_PLAN_NEAR_PCT:
                verdict = f"exit now — the stop line {line:g} is {stop_pct:.1f}% away"
            elif tgt_pct is not None and tgt_pct <= EOD_PLAN_NEAR_PCT:
                verdict = f"wait for the target {target:g}, {tgt_pct:.1f}% away"
            else:
                verdict = f"flatten at {flat} as now"
            await self.ledger.event("eod.plan", {
                "kind": "eod.plan", "positionId": pos.id, "book": pos.strategy, "symbol": pos.underlying.symbol,
                "contract": pos.instrument.name or code, "qty": pos.qty_remaining, "entry": pos.entry,
                "bid": bid, "ask": ask, "mid": round(mid, 2) if mid else None,
                "stopLine": line or None, "stopPct": round(stop_pct, 2) if stop_pct is not None else None,
                "target": target, "targetPct": round(tgt_pct, 2) if tgt_pct is not None else None,
                "nearPct": EOD_PLAN_NEAR_PCT, "flattenAt": flat, "verdict": verdict, "recordOnly": True,
            })
            log.info("eod.plan", book=pos.strategy, symbol=pos.underlying.symbol, verdict=verdict)

    async def _exit(self, pos: Position, decision: Any, now: float) -> None:
        """One exit in flight per position. The operator's SKIP and the exit loop both call this;
        while one is awaiting the venue the other used to send a second SELL for the same lots —
        on paper a double-booked P&L, live a naked short. The second caller now stands down, and
        whoever holds the slot re-reads the position before sending anything."""
        if pos.id in self._exits_in_flight:
            log.warning("exit.in_flight", position=pos.id, symbol=pos.underlying.symbol, reason=getattr(decision.reason, "value", decision.reason))
            return
        resting = self._exit_resting(pos.id)
        if resting is not None:
            # A limit exit is already working. The tick reprices and crosses it; a second SELL is
            # never sent — unless this decision is more urgent or larger (a stop behind a resting
            # target, the rest behind a one-lot tranche): then it replaces the resting one.
            _, old, _ = resting.ctx
            urgent = self.limit_policy.exit_deadline(decision.reason) < resting.deadline_s
            if not (urgent or decision.qty > old.qty):
                return
            self._resting.pop(resting.intent.client_order_id, None)
            self._exit_attempts[pos.id] = self._exit_attempts.get(pos.id, 0) + 1  # a fresh id for the new order
            log.info("limit.superseded", position=pos.id, was=old.reason.value, now=decision.reason.value, qty=decision.qty)
        self._exits_in_flight.add(pos.id)
        try:
            # any other exit — stop, trail, band, the equity-T1 arm, the exit engine's own target,
            # EOD, halt, daily loss, SKIP — takes the resting target sell off first
            why = f"{decision.reason.value}: {decision.note}"
            if decision.reason is ExitReason.TARGET and decision.qty < pos.qty_remaining:
                # the exit engine's own touch (the stock reaching its T1): only that rung's sell comes
                # off, and the rungs above keep their place unless the lots are needed
                for r in self._targets_resting(pos.id):
                    if r.ctx[1] == pos.targets_hit:
                        await self._cancel_target_order(pos, r, now, why)
                await self._make_room(pos, decision.qty, now, why)
            else:
                await self._cancel_resting_targets(pos, now, why)
            if pos.status != "OPEN" or pos.qty_remaining <= 0:
                return
            if decision.qty > pos.qty_remaining:  # decided before an earlier slice filled
                decision = replace(decision, qty=pos.qty_remaining)
            await self._exit_now(pos, decision, now)
        finally:
            self._exits_in_flight.discard(pos.id)

    async def _exit_now(self, pos: Position, decision: Any, now: float) -> None:
        if now < self._exit_retry_at.get(pos.id, 0.0):
            return  # backing off after a rejected exit; the decision is re-made next pass
        if self._limit_mode() and self.s.paper_limit_exits:
            await self._place_exit_limit(pos, decision, now)
            return
        await self._exit_market(pos, decision, now)

    async def _place_exit_limit(self, pos: Position, decision: Any, now: float) -> None:
        """SELL LIMIT at the mid, walked to the bid, crossed at the deadline its urgency sets — or, for a
        stop that is urgent (exec/resting.py ``urgent_stop``), sold into the bid at once."""
        await self._refresh_exit_book(pos.instrument, now)
        attempt = self._exit_attempts.get(pos.id, 0)
        code = pos.instrument.scrip_code
        bid, ask, _, _ = self._touch(code, now)
        run = self._option_fall(code, now, (bid + ask) / 2 if bid and ask else None)
        urgent = urgent_stop(decision.reason, bid, ask, run, self.limit_policy, pos.instrument.tick_size or 0.05)
        if urgent and bid:
            await self._exit_at_once(pos, decision, now, attempt, (bid, ask), run, urgent)
            return
        deadline = self.limit_policy.exit_deadline(decision.reason)
        limit = exit_limit(bid, ask, 0.0, deadline, pos.instrument.tick_size or 0.05)
        if limit is None:
            await self._exit_market(pos, decision, now)  # no bid to rest against: the market path decides
            return
        intent = OrderIntent(
            strategy=pos.strategy, instrument=pos.instrument, side=OrderSide.SELL, qty=decision.qty, purpose=Purpose.EXIT,
            signal_id=pos.signal_id, client_order_id=exit_client_order_id(pos, decision, attempt), reason=decision.note,
            position_id=pos.id, ref_price=decision.ref_price, limit_price=limit,
        )
        res = self.gateway.place_limit(intent)
        if res.decision is not Decision.RESTING:
            await self.ledger.insert_order(_order_json(res.order), res.decision.value)
            self._exit_attempts[pos.id] = attempt + 1  # its id is spent: the next pass sends a fresh one
            return
        r = Resting(intent=intent, kind="exit", limit=limit, placed_ts=now, deadline_s=deadline, signal_ts=now,
                    ref=decision.ref_price, why=f"{decision.reason.value}: the mid, walked to the bid, crossed after {deadline:g} s",
                    book_at_place=(bid, ask), last_check=now, ctx=(pos, decision, attempt), depth_at_place=self._depth(code, now))
        r.order = res.order
        self._resting[intent.client_order_id] = r
        log.info("limit.placed", kind="exit", strategy=pos.strategy, symbol=pos.underlying.symbol, limit=limit, bid=bid, ask=ask,
                 reason=decision.reason.value, deadline_s=deadline)
        await self._advance_one(r, now, first=True)

    async def _exit_at_once(self, pos: Position, decision: Any, now: float, attempt: int, book: tuple[float, float | None],
                            run: float | None, why: str) -> None:
        """An urgent stop: sold into the bid now, through the depth for every lot — no rest at the mid."""
        bid, ask = book
        code = pos.instrument.scrip_code
        intent = OrderIntent(
            strategy=pos.strategy, instrument=pos.instrument, side=OrderSide.SELL, qty=decision.qty, purpose=Purpose.EXIT,
            signal_id=pos.signal_id, client_order_id=exit_client_order_id(pos, decision, attempt), reason=decision.note,
            position_id=pos.id, ref_price=decision.ref_price, limit_price=bid,
        )
        r = Resting(intent=intent, kind="exit", limit=bid, placed_ts=now, deadline_s=0.0, signal_ts=now, ref=decision.ref_price,
                    why=f"{decision.reason.value}: sold into the bid at once — {why}", book_at_place=(bid, ask), last_check=now,
                    ctx=(pos, decision, attempt), depth_at_place=self._depth(code, now))
        r.momentum.append({"atS": 0.0, "runPct": round(run, 2) if run is not None else None, "note": why})
        log.info("exit.urgent", strategy=pos.strategy, symbol=pos.underlying.symbol, reason=decision.reason.value, bid=bid, ask=ask,
                 run_pct=round(run, 2) if run is not None else None, why=why)
        await self._exit_cross(r, now, bid, ask, outcome=f"sold into the bid at once — {why}")

    async def _exit_filled(self, r: Resting, result: Any, audit: dict[str, Any]) -> None:
        pos, decision, attempt = r.ctx
        await self.ledger.insert_order(_order_json(result.order, audit), result.decision.value)
        if pos.status != "OPEN" or pos.qty_remaining <= 0:
            log.warning("limit.exit_after_close", position=pos.id)
            return
        await self._book_exit(pos, decision, result, result.fill.ts, attempt, audit)

    async def _exit_cross(self, r: Resting, now: float, bid: float | None, ask: float | None, *, outcome: str | None = None) -> None:
        """The deadline: sell through the book at the bid, so an exit is never left working."""
        self._resting.pop(r.intent.client_order_id, None)
        pos, decision, attempt = r.ctx
        if pos.status != "OPEN" or pos.qty_remaining <= 0:
            return
        qty = min(r.intent.qty, pos.qty_remaining)
        depth = self._depth(pos.instrument.scrip_code, now)  # the book our sell walks, before it walks it
        cross = replace(r.intent, client_order_id=exit_client_order_id(pos, decision, attempt, cross=True), limit_price=None, qty=qty)
        result = await self._submit(cross, verdict_ok=True, verdict_reason="")
        fill = result.fill
        audit = r.audit(crossedTs=now, bookAtCross={"bid": bid, "ask": ask}, depthAtCross=depth, filledTs=fill.ts if fill else None,
                        fillPrice=fill.price if fill else None, waitS=round(now - r.placed_ts, 3),
                        outcome=(outcome or f"crossed at the bid after {r.deadline_s:g} s") if fill else f"cross failed: {result.order.note}")
        await self.ledger.insert_order(_order_json(result.order, audit), result.decision.value)
        if fill is None:
            n = attempt + 1
            self._exit_attempts[pos.id] = n
            self._exit_retry_at[pos.id] = now + min(2.0**n, 30.0)
            log.error("exit.cross_failed", position=pos.id, symbol=pos.underlying.symbol, reason=result.order.note)
            return
        await self._book_exit(pos, replace(decision, qty=qty), result, now, attempt, audit)

    async def _exit_market(self, pos: Position, decision: Any, now: float) -> None:
        if self.mode() is Mode.PAPER:
            await self._refresh_exit_book(pos.instrument, now)
        attempt = self._exit_attempts.get(pos.id, 0)
        intent = OrderIntent(
            strategy=pos.strategy,
            instrument=pos.instrument,
            side=OrderSide.SELL,
            qty=decision.qty,
            purpose=Purpose.EXIT,
            signal_id=pos.signal_id,
            client_order_id=exit_client_order_id(pos, decision, attempt),
            reason=decision.note,
            position_id=pos.id,
            ref_price=decision.ref_price,
        )
        placed = time.time()
        result = await self._submit(intent, verdict_ok=True, verdict_reason="")
        audit = self._market_audit("exit", now, placed, result, pos.instrument.scrip_code)
        await self.ledger.insert_order(_order_json(result.order, audit), result.decision.value)
        if result.decision is Decision.SHADOW_OK:
            # SHADOW places nothing, so a position carried in from a PAPER run can never close.
            # That is the mode working as intended — say so once, not once a second, and never as
            # an error: an alerting path that cries wolf is how the real alert gets ignored.
            if pos.id not in self._shadow_exits:
                self._shadow_exits.add(pos.id)
                log.info(
                    "exit.shadow_only",
                    position=pos.id,
                    symbol=pos.underlying.symbol,
                    reason=decision.reason.value,
                    note="SHADOW mode places nothing; switch to PAPER or LIVE to close it",
                )
            return
        if result.fill is None:
            if result.decision in _DEFINITIVE_REJECTIONS:
                # Nothing reached a venue, or the venue said no: the same exit may go again under a
                # fresh id — after a pause, so a contract nobody is bidding for cannot fire twelve
                # rejections in twelve seconds and trip the engine-wide breaker.
                n = attempt + 1
                self._exit_attempts[pos.id] = n
                self._exit_retry_at[pos.id] = now + min(2.0**n, 30.0)
            log.error(
                "exit.failed",
                position=pos.id,
                symbol=pos.underlying.symbol,
                reason=result.order.note,
                attempt=attempt,
            )
            self.telegram.fire_and_forget(
                f"⚠️ EXIT FAILED {pos.strategy} {pos.underlying.symbol}: {result.order.note}",
                key=f"exitfail:{pos.id}",
            )
            return
        await self._book_exit(pos, decision, result, now, attempt, audit)

    async def _book_exit(self, pos: Position, decision: Any, result: Any, now: float, attempt: int, audit: dict[str, Any]) -> None:
        """A filled exit, immediate or from a resting limit: the slice is booked, the wallet
        released, the trade written when the position is done."""
        pos.exec_log.setdefault("exits", []).append({**audit, "reason": decision.reason.value, "qty": int(result.fill.qty)})
        filled = max(0, min(decision.qty, int(result.fill.qty)))
        if filled < decision.qty:
            # The book took part of it (a one-level book truncates at the touch). Book what filled
            # and leave the rest OPEN; it goes again at once under a new id, since the old one is
            # spent. Booking decision.qty "sold" 1,950 PNBHOUSING contracts that never traded.
            self._exit_attempts[pos.id] = attempt + 1
            self._exit_retry_at.pop(pos.id, None)
            log.warning("exit.partial", position=pos.id, symbol=pos.underlying.symbol,
                        asked=decision.qty, filled=filled)
        else:
            self._exit_attempts.pop(pos.id, None)
            self._exit_retry_at.pop(pos.id, None)
        done = ExitDecision(decision.position_id, decision.reason, decision.ref_price, filled, decision.note)
        entry_before = pos.qty_remaining
        gross = apply_exit(
            pos, done, fill_price=result.fill.price, charges=result.fill.charges, now=now
        )
        wallet = self.wallets[pos.strategy]
        wallet.release(pos.entry * min(filled, entry_before) * pos.instrument.multiplier, now)
        wallet.apply_charges(result.fill.charges, now)
        wallet.apply_close(gross, now)
        # each wallet against ITS OWN book's limits — not the parent's for everyone
        tripped = wallet.check_breakers(self.limits_for(pos.strategy), now)
        if tripped:
            self.telegram.fire_and_forget(f"🛑 {pos.strategy} wallet halted: {tripped}")

        await self.ledger.upsert_position(_position_json(pos))
        await self.ledger.upsert_wallet(wallet.strategy, wallet.to_json())
        log.info(
            "position.exit",
            strategy=pos.strategy,
            symbol=pos.underlying.symbol,
            reason=decision.reason.value,
            qty=decision.qty,
            price=result.fill.price,
            gross=round(gross, 2),
            charges=round(result.fill.charges, 2),
        )
        if pos.status == "CLOSED":
            trade = _trade_from(pos, now)
            await self.ledger.insert_trade(_trade_json(trade))
            # Advisory and fire-and-forget: the review never delays or touches the trade path.
            self.committee.on_trade_closed(_trade_json(trade))
            self.positions.pop(pos.id, None)
            self.telegram.fire_and_forget(
                f"🔴 {pos.strategy} {pos.underlying.symbol} closed {decision.reason.value} "
                f"net ₹{trade.net:,.0f} ({trade.r_multiple:+.2f}R)"
            )

    # -- background tasks -------------------------------------------------------------------------------

    def _record_tape(self) -> None:
        """One second of tape: the open positions pin their contracts; candidates and card
        contracts were followed when they were looked at; the legs come from ``_tape_legs``."""
        held = [
            (
                p.instrument.scrip_code,
                p.underlying.symbol,
                ROLE_OPTION if p.instrument.is_option else ROLE_FUTURE,
            )
            for p in self.positions.values()
            if p.status == "OPEN"
        ]
        self.tape.sample(time.time(), self.quotes, held)
        try:
            self.fulltape.sample(time.time(), self.quotes)
        except Exception as exc:  # noqa: BLE001 - the recorder may never cost the clock its depth sync
            self.fulltape.errors += 1
            self.fulltape.last_error = str(exc)[:200]

    def _fulltape_skip(self, code: str) -> bool:
        """MCX stays off the full tape; an unknown code (India VIX) is kept."""
        hit = self._fulltape_skip_cache.get(code)
        if hit is None:
            inst = self.catalogue_loader.catalogue.get(code)
            hit = self._fulltape_skip_cache[code] = inst is not None and inst.segment is Segment.MCX_FO
        return hit

    def depth_wanted(self) -> set[str]:
        """Every scrip code whose ORDER BOOK something is about to read: an open position's
        contract, anything the tape is following (a live card, a strike the selector weighed) and
        the underlying legs of both. This is the set the matcher and the card walks price against;
        everything else in the universe rides the price channel and never touches the reader."""
        want = {p.instrument.scrip_code for p in self.positions.values() if p.status == "OPEN"}
        for code, w in self.tape.watched().items():
            want.add(code)
            want.update(leg for leg, _role in self._tape_legs(w["symbol"]))
        return want

    def _held_codes(self) -> set[str]:
        """The contracts of every OPEN position."""
        return {p.instrument.scrip_code for p in self.positions.values() if p.status == "OPEN"}

    async def _follow_depth(self, instruments: list[Instrument]) -> None:
        """Put these contracts on the depth channel now, ahead of an order, and register the same
        interest on the tape so ``_sync_depth`` knows they are wanted.

        Registering both in one call is the point. Subscribing depth without telling the tape made
        the reconciler drop the strikes the selector had just quoted, one second later and every
        second after — 142 unsubscribes in a single pass, ~24 a second sustained, and the socket
        reader 753 ms behind for the trouble (found live 2026-09-24 13:15). The tape is the single
        registry of what we are interested in; depth follows it, and nothing subscribes behind it.
        """
        if not self.s.depth_follow_enabled or self.feed is None:
            return
        now = time.time()
        for inst in instruments:
            self.tape.follow(inst.symbol, [inst.scrip_code], now=now)
        fresh = [i for i in instruments if i.scrip_code not in self._depth_following]
        if not fresh:
            return
        try:
            await self.feed.subscribe("md", fresh)
        except Exception as exc:  # noqa: BLE001 — depth is an input, never a reason to stop
            log.warning("depth.follow_failed", n=len(fresh), error=str(exc)[:120])
            return
        self._depth_following |= {i.scrip_code for i in fresh}
        self.depth_adds += len(fresh)

    async def _sync_depth(self) -> None:
        """Bring the depth channel in line with ``depth_wanted`` once a second. Adds first, so a
        contract is never dropped in the same pass that something else needs it; the archive
        sample is pinned and never dropped."""
        if not self.s.depth_follow_enabled or self.feed is None:
            return
        want = self.depth_wanted()
        if len(want) > self.s.depth_max_subscriptions:
            # Over the cap, what is HELD keeps its book first — an exit with no depth to sell into
            # is a refused exit. It used to keep the lowest scrip codes, which says nothing about
            # what is held (review, 2026-10-03). The rest fill the remaining slots as before.
            held_codes = self._held_codes()
            held = sorted(want & held_codes)
            rest = sorted(want - held_codes)
            want = set((held + rest)[: self.s.depth_max_subscriptions])
        cat = self.catalogue_loader.catalogue
        add = [i for c in want - self._depth_following if (i := cat.get(c)) is not None]
        drop = [
            i
            for c in self._depth_following - want - self._depth_pinned
            if (i := cat.get(c)) is not None
        ]
        if not add and not drop:
            self.depth_syncs += 1
            return
        try:
            if add:
                await self.feed.subscribe("md", add)
            if drop:
                await self.feed.unsubscribe("md", drop)
        except Exception as exc:  # noqa: BLE001 — depth is an input, never a reason to stop
            log.warning("depth.sync_failed", add=len(add), drop=len(drop), error=str(exc)[:120])
            return
        self._depth_following = (self._depth_following | {i.scrip_code for i in add}) - {
            i.scrip_code for i in drop
        }
        self.depth_syncs += 1
        self.depth_adds += len(add)
        self.depth_drops += len(drop)
        log.info("depth.synced", following=len(self._depth_following), added=len(add), dropped=len(drop))

    def _tape_legs(self, symbol: str) -> list[tuple[str, str]]:
        """The underlying's own codes for the tape — the equity (or the MCX future the levels are
        computed on) and, on NSE, the front future. Resolved once per symbol per day: the
        catalogue's front future rolls at expiry."""
        today = ist_today().isoformat()
        hit = self._tape_legs_cache.get(symbol)
        if hit is not None and hit[0] == today:
            return hit[1]
        legs: list[tuple[str, str]] = []
        u = self.underlyings.get(symbol)
        if u is not None:
            role = {InstrumentKind.EQUITY: ROLE_EQUITY, InstrumentKind.FUTURE: ROLE_FUTURE}.get(u.kind, ROLE_INDEX)
            legs.append((u.scrip_code, role))
            if u.segment is Segment.NSE_EQ:
                front = self.catalogue_loader.catalogue.front_future(symbol, on=ist_today())
                if front is not None:
                    legs.append((front.scrip_code, ROLE_FUTURE))
        self._tape_legs_cache[symbol] = (today, legs)
        return legs

    async def _clock(self) -> None:
        while not self._stop.is_set():
            try:
                await self.aggregator.flush_stale()
                await self._manage_positions()
                # Same tick as the exit evaluation, deliberately: the card must show the
                # numbers the stop is being judged against, not a second computation of them.
                self.alerts.refresh_live()
                # Last, so the tape carries the quotes the exit loop and the cards just used.
                self._record_tape()
                await self._sync_depth()
            except Exception as exc:
                log.exception("clock.failed", error=str(exc))
            try:
                await self._carry_tick(time.time())
            except Exception as exc:  # the carry at the open never costs the exits their tick
                log.exception("carry.failed", error=str(exc))
            await asyncio.sleep(1.0)

    async def _feed_watchdog(self, now: float) -> None:
        """A socket can read "connected" and deliver nothing — after a sleep/wake (the Mac slept at
        noon on 2026-09-25) the peer is gone and the 25 s ping + 45 s timeout takes up to 70 s to
        notice. In a session, with 200+ names subscribed, FEED_SILENCE_S without one frame is a
        dead feed: drop it and let the run loop reconnect. At most once a minute."""
        fh = self.feed.health
        if (
            fh.connected
            and fh.connected_since is not None
            and now - fh.connected_since > FEED_SILENCE_S * 2
            and fh.last_message_ts is not None
            and now - fh.last_message_ts > FEED_SILENCE_S
            and now - self._last_silence_reconnect > 60
            and any(is_open(g.segment, now, self.calendar) for g in self.groups.values())
        ):
            self._last_silence_reconnect = now
            self._silence_reconnects += 1
            await self.feed.reconnect(reason=f"silent {now - fh.last_message_ts:.0f}s in session")

    async def _housekeeping(self) -> None:
        last_reconcile = 0.0
        last_snapshot = 0.0
        last_archive = time.time()
        last_fulltape = time.time()
        last_day = ist_day(time.time()).isoformat()
        while not self._stop.is_set():
            now = time.time()
            day = ist_day(now).isoformat()
            hm = ist_hm(now)
            # One guard per duty (ops/guard.py): one try around all of them let any failure skip every
            # duty after it, and counted nothing (review, 2026-10-03).
            async with self.guards.duty("fudkii.rescan"):
                if (
                    not self.booting
                    and now - float(self.last_fudkii_scan.get("ts") or 0) > 300
                    and (self._fudkii_scan_task is None or self._fudkii_scan_task.done())
                ):
                    self._fudkii_scan_task = asyncio.create_task(self.scan_fudkii(), name="fudkii-rescan")

            async with self.guards.duty("decided.save"):
                if len(self._decided) != self._decided_saved:
                    await asyncio.to_thread(self._save_decided, list(self._decided))

            async with self.guards.duty("alerts.write"):
                if self.alerts.dirty and self.alerts.store_dir is not None:
                    snap = self.alerts.snapshot()  # on the loop, where the rings are mutated
                    await asyncio.to_thread(self.alerts.write, snap)

            async with self.guards.duty("archive.flush"):
                if now - last_archive > self.s.archive_flush_s:
                    last_archive = now
                    await asyncio.to_thread(self.archive.flush)

            async with self.guards.duty("tape_full.write"):
                if now - last_fulltape > self.s.tape_full_flush_s:
                    last_fulltape = now
                    rows = self.fulltape.take()  # handed over here, in the loop that appends to it
                    if rows:
                        try:
                            await asyncio.to_thread(self.fulltape.write, rows)
                        except Exception as exc:  # noqa: BLE001 - never costs the housekeeping after it
                            log.warning("tape_full.write_failed", error=str(exc)[:120])

            async with self.guards.duty("committee.autopilot"):
                # The nightly research loop, once per day after its hour, only with every segment
                # closed — it runs six backtests in a worker thread and spends Claude calls.
                if (
                    self.committee.autopilot_due(now, day, self._autopilot_day)
                    and not self.market_open_now()
                ):
                    self._autopilot_day = day
                    self._decision_tasks.add(asyncio.create_task(self._autopilot()))

            async with self.guards.duty("oi.candles_flush"):
                self.oi_candles.flush(now, self._segment_by_code)

            async with self.guards.duty("oi.bhavcopy"):
                if (
                    self.s.oi_bhavcopy_enabled
                    and self.s.engine_enabled
                    and self._future_to_underlying
                    and self._oi_bhav_day != self.calendar.previous_trading_day(ist_today())
                    and now - self._oi_bhav_tried > 600
                    and (self._oi_bhav_task is None or self._oi_bhav_task.done())
                ):
                    self._oi_bhav_tried = now
                    self._oi_bhav_task = asyncio.create_task(self._ensure_oi_bhavcopy(), name="oi-bhavcopy")

            async with self.guards.duty("day_roll"):
                if day != last_day:
                    last_day = day
                    self._roll_day_state(ist_day(now))
                    if self.s.has_credentials and self.s.engine_enabled:
                        await self.catalogue_loader.ensure()

            async with self.guards.duty("positions.reconcile"):
                if self.reconciler_positions is not None and now - last_reconcile > 60:
                    last_reconcile = now
                    # LIVE every minute; PAPER only while frozen, so a failed boot read (the broker
                    # slow at 09:00) heals on the next clean one instead of halting every book all day
                    if self.mode() in LIVE_MODES or self.reconciler_positions.frozen:
                        await self.reconciler_positions.run(self._venue_positions(), at_venue=self.mode() in LIVE_MODES)

            async with self.guards.duty("feed.token_rollover"):
                # The socket was opened with a token that dies at 23:59:59 IST. Once it has, drop
                # the socket so the run loop reconnects with a fresh login — otherwise it can sit
                # "connected" on a dead token and deliver nothing at the open. A minute of grace
                # keeps this from racing the expiry itself.
                if (
                    self.feed.health.connected
                    and self.feed.token_expires_at is not None
                    and now > self.feed.token_expires_at + 60
                ):
                    await self.feed.reconnect(reason="token expired")

            async with self.guards.duty("feed.watchdog"):
                await self._feed_watchdog(now)

            async with self.guards.duty("bars.sweep"):
                # Periodic REST sweep of the finer frames: the fidelity metric, and exact chart bars.
                if (
                    self.reconciler_ready
                    and self.underlyings
                    and (self._sweep_task is None or self._sweep_task.done())
                    and (
                        self.reconciler.last_sweep_ts is None
                        or now - self.reconciler.last_sweep_ts > self.s.bar_sweep_interval_s
                    )
                    and any(is_open(g.segment, now, self.calendar) for g in self.groups.values())
                ):
                    self._sweep_task = asyncio.create_task(
                        self.reconciler.sweep(list(self.underlyings))
                    )

            async with self.guards.duty("universe.rebuild"):
                # scripFinder's 09:20 IST intraday rebuild: refetch the master, re-pick strikes,
                # subscribe anything new. Strikes listed 09:00–09:15 are not in an overnight master.
                if self.reconciler_ready and ist_hm(now) >= "09:20" and self._intraday_rebuild_day != day:
                    self._intraday_rebuild_day = day
                    self._decision_tasks.add(asyncio.create_task(self._intraday_universe_rebuild()))

            async with self.guards.duty("volume.market_check"):
                # The market-wide volume check on every NSE bar, trigger or not — an alarm the minute
                # the data breaks, not the next time a trigger happens to read it. The bar judged is
                # the one that closed at least 20 s ago (its candles reconciled).
                if self.reconciler_ready and self.underlyings and not self.booting:
                    judged = int(bucket_start(Segment.NSE_EQ, now - 20, DECISION_TF)) - TF_SECONDS[DECISION_TF]
                    if judged not in self._vol_market and on_session_grid(Segment.NSE_EQ, judged, DECISION_TF, until=NSE_EQ_CONTINUOUS_UNTIL) \
                            and self.calendar.is_trading_day(ist_day(judged)) and ist_day(judged) == ist_day(now):
                        self._market_volume(judged)

            async with self.guards.duty("bars.audit"):
                # After the NSE close, the day's decision bars against the broker's once more.
                if (
                    self.reconciler_ready
                    and self.underlyings
                    and not self.booting
                    and self.calendar.is_trading_day(ist_day(now))
                    and ist_hm(now) >= BAR_AUDIT_HM
                    and self._bar_audit_day != day
                    and (self._bar_audit_task is None or self._bar_audit_task.done())
                ):
                    self._bar_audit_day = day
                    self._bar_audit_task = asyncio.create_task(self._audit_bars(ist_day(now)), name="bar-audit")

            async with self.guards.duty("iv.refresh"):
                # Each name's own VIX, once a minute, from the quotes already in hand.
                if self.reconciler_ready and self.groups and now - self._last_iv_refresh >= 60:
                    self._last_iv_refresh = now
                    self._refresh_stock_iv()

            async with self.guards.duty("alerts.reset"):
                # The alert page is emptied for the coming session, every book and twin together.
                stamp = f"{day} {self.s.alerts_reset_ist}"
                if hm >= self.s.alerts_reset_ist and stamp not in self._alerts_reset_done:
                    self._alerts_reset_done.add(stamp)
                    self.alerts.reset_day(day)
                    self._signals_today.clear()
                    self._counter_preview.clear()
                    self._fut_cache.clear()

            async with self.guards.duty("pivots.slots"):
                for slot in self.s.daily_refresh_hm:
                    stamp = f"{day} {slot}"
                    if hm >= slot and stamp not in self._daily_refresh_done:
                        self._daily_refresh_done.add(stamp)
                        self._daily_due = True
                for slot in self.s.legs_reanchor_hm:
                    stamp = f"{day} {slot}"
                    if hm >= slot and stamp not in self._legs_reanchor_done:
                        self._legs_reanchor_done.add(stamp)
                        self._legs_reanchor_due = True
                if (
                    self.reconciler_ready
                    and self.underlyings
                    and (self._pivot_repair_task is None or self._pivot_repair_task.done())
                    and (
                        self._daily_due
                        or self._legs_due
                        or self._legs_reanchor_due
                        or now - self._last_pivot_repair > self.s.pivot_repair_interval_s
                    )
                ):
                    self._last_pivot_repair = now
                    self._pivot_repair_task = asyncio.create_task(self._pivot_repair())

            async with self.guards.duty("wallets.upkeep"):
                await self._wallet_upkeep(now)

            async with self.guards.duty("gateway.breakers"):
                for book in self.gateway.take_new_trips():
                    await self.ledger.event("gateway.book_breaker", {"book": book, "rejects": self.gateway.rejects_by_book.get(book)})
                    self.telegram.fire_and_forget(f"🛑 {BOOK_LABELS.get(book, book)}: order breaker tripped — its entries stop until reset")
                    log.error("gateway.book_breaker", book=book)

            async with self.guards.duty("snapshot.persist"):
                if now - last_snapshot > 300:
                    last_snapshot = now
                    await self._persist_wallets()
                    await self.ledger.insert_health(self.health_snapshot())
                    await asyncio.to_thread(self.iv_history.save_all)

            await asyncio.sleep(5.0)

    # -- state -----------------------------------------------------------------------------------------

    async def _load_wallets(self) -> None:
        stored = await self.ledger.load_wallets()
        for key in ALL_KEYS:
            data = stored.get(key.value)
            self.wallets[key.value] = (
                Wallet.from_json(data)
                if data
                else Wallet.new(key.value, INITIAL_INR.get(key, self.s.paper_initial_inr))
            )
        await self._persist_wallets()

    async def _wallet_upkeep(self, now: float, *, boot: bool = False) -> None:
        """Every wallet made true from its OWN record, at boot and on every housekeeping tick —
        all three steps are idempotent, so running them often costs nothing:

        * **the day** — ``rollover`` compares today with the day the wallet last recorded. The old
          trigger was a day change seen by the running process, seeded with the boot day, so a
          process (re)started after midnight never rolled over that day: yesterday's daily halt
          and day P&L carried through it (audit, 2026-09-26). The closed day is logged with its
          opening and closing balance;
        * **both breakers** — re-read against the book's own limits, so a halt a book earned while
          the process was down, or before this fix, is on record before its first entry;
        * **deployed money** — matched to what the book actually holds (``_reconcile_deployed``)."""
        for key, w in self.wallets.items():
            changed = False
            closed = w.rollover(now)
            if closed is not None:
                changed = True
                await self.ledger.event("wallet.rollover", {"strategy": key, **closed, "newDay": w.day, "atBoot": boot})
                log.info("wallet.rollover", strategy=key, at_boot=boot, **closed)
            tripped = w.check_breakers(self.limits_for(key), now)
            if tripped:
                changed = True
                await self.ledger.event("wallet.halted", {"strategy": key, "reason": tripped, "atBoot": boot})
                self.telegram.fire_and_forget(f"🛑 {key} wallet halted: {tripped}")
            if self._reconcile_deployed(key, w, now, boot=boot):
                changed = True
                await self.ledger.event("wallet.deployed_corrected", {"strategy": key, **self._deployed_fix.pop(key, {}), "atBoot": boot})
            if changed:
                await self.ledger.upsert_wallet(key, w.to_json())

    def _deployed_expected(self, key: str, w: Wallet) -> float:
        """What ``deployed`` should read: the cost still held in the book's open positions (the
        entry path commits ``entry × qty``, each exit releases ``entry × qty sold``) plus the
        provisional hold of each of its resting entries."""
        held = sum(p.entry * p.qty_remaining * p.instrument.multiplier
                   for p in self.positions.values() if p.status == "OPEN" and p.strategy == key)
        holds = sum(r.ctx.outlay for r in self._resting.values()
                    if r.kind == "entry" and getattr(r.ctx, "wallet", None) is w)
        flying = sum(h for ww, h in self._inflight_holds.values() if ww is w)
        return held + holds + flying

    def _reconcile_deployed(self, key: str, w: Wallet, now: float, *, boot: bool) -> bool:
        """Correct a ``deployed`` that disagrees with the book's holdings. At boot at once (no
        order is in flight: resting orders do not survive a restart). While running, only when the
        SAME drift is seen twice at least 30 s apart — an entry between its hold and its position
        is a few milliseconds, never 30 s. RT-X/RT-N/RT-Y carried ₹24,326.25 from 2026-09-24
        09:45 to this fix: SBILIFE's twins were copied from a parent already closed, born CLOSED
        with their cost committed and never released (the copy was fixed on 2026-09-25; the money
        it stranded never was)."""
        drift = round(w.deployed - self._deployed_expected(key, w), 2)
        if abs(drift) < 1.0:
            self._deployed_drift.pop(key, None)
            return False
        seen = self._deployed_drift.get(key)
        if not boot:
            if seen is None or abs(seen[0] - drift) >= 1.0:
                self._deployed_drift[key] = (drift, now)
                return False
            if now - seen[1] < 30.0:
                return False
        was = w.deployed
        w.deployed = max(0.0, round(w.deployed - drift, 2))
        w.updated_ts = now
        self._deployed_drift.pop(key, None)
        self._deployed_fix[key] = {"was": round(was, 2), "now": w.deployed, "drift": drift}
        log.warning("wallet.deployed_corrected", strategy=key, was=round(was, 2), now=w.deployed, drift=drift, at_boot=boot)
        return True

    async def _persist_wallets(self) -> None:
        for w in self.wallets.values():
            await self.ledger.upsert_wallet(w.strategy, w.to_json())
            await self.ledger.snapshot_wallet(w.strategy, w.to_json())

    async def _load_positions(self) -> None:
        """Re-hydrate open positions from the ledger.

        CAN2 held its positions in an in-memory dict with no persistence and no re-hydration, so a
        restart orphaned every live trade while the log looked normal. This is the whole fix.
        """
        for row in await self.ledger.load_open_positions():
            try:
                pos = _position_from_json(row)
            except Exception as exc:  # noqa: BLE001
                log.error("position.rehydrate_failed", id=row.get("id"), error=str(exc))
                continue
            self.positions[pos.id] = pos
        if self.positions:
            log.warning("engine.rehydrated", positions=len(self.positions))

    # -- introspection -------------------------------------------------------------------------------

    def market_open_now(self) -> bool:
        now = time.time()
        segments = {g.segment for g in self.groups.values()} or set(self.s.segment_list)
        return any(is_open(seg, now, self.calendar) for seg in segments)

    def _roll_day_state(self, today: date) -> None:
        """Everything that is per IST day, reset in one place (review, 2026-10-03: the clears were
        scattered, and several maps keyed by time were never pruned). State keyed by a SIGNAL —
        breadth at a trigger, a gap fade — is left: the next morning's carry still reads it."""
        log.info("feed.rate_day", segments=self.feed_rate.snapshot())
        self.feed_rate.reset()
        self._alerts_reset_done.clear()
        self._zone_cache.clear()
        self.zone_refusals.clear()
        self._daily_due = self._legs_due = True
        self._daily_provisional_asked.clear()
        # 5paisa's TotalQty restarts at 0 each session; the snapshot writer keeps a running max, so a
        # REST-only strike kept yesterday's total until today's passed it (review, 2026-10-03)
        self.option_volume.clear()
        self._basis_mismatch.clear()
        self._legs_reanchor_done.clear()
        self._daily_refresh_done.clear()
        self._handled_signals.clear()
        start = session_open_ts(Segment.NSE_EQ, today) - 9.25 * 3600  # 00:00 IST
        self._front_code = {k: v for k, v in self._front_code.items() if k[1] >= today}
        self._fudkii_scanned = {k for k in self._fudkii_scanned if k[1] >= start}
        self._confirm_attempts = {k: v for k, v in self._confirm_attempts.items() if k[1] >= start}
        self._closes_seen = {k for k in self._closes_seen if k[1] >= start}
        self._vol_market = {k: v for k, v in self._vol_market.items() if k >= start}
        self._n50_vol = {k: v for k, v in self._n50_vol.items() if k >= start}

    def _duty_check(self) -> Check:
        """A programming error in any guarded duty in the last 15 minutes is a fault; market and I/O
        failures are shown, not failed on."""
        recent = self.guards.snapshot(since_s=900)
        bad = {k: v for k, v in recent.items() if v["programmingErrors"]}
        detail = "; ".join(f"{k}: {v['errors']}× {v['lastError']}" for k, v in list(recent.items())[:4]) or "no duty failed in 15 min"
        return Check("duty_errors", not bad, detail=detail, value=float(len(recent)))

    def _zones_check(self, daily: Any = None, now: float | None = None) -> Check:
        """No levels for a name because its daily and 30m series are on different price bases (a
        corporate action) is a data fault; so is a previous session still on 5paisa's provisional
        candle once the session is under way (``PROVISIONAL_ALARM_HM``) — read from the daily audit,
        which covers every name, not only those a trigger asked about. History refusals are shown."""
        now = time.time() if now is None else now
        reasons: dict[str, int] = {}
        for why in self.zone_refusals.values():
            reasons[why.split(":")[0]] = reasons.get(why.split(":")[0], 0) + 1
        basis = sorted(k for k, v in self.zone_refusals.items() if v.startswith("basis"))
        withheld = sorted(self._basis_mismatch)
        provisional = sorted(daily.provisional) if daily is not None else []
        late = (not self.booting and self.calendar.is_trading_day(ist_day(now)) and ist_hm(now) >= PROVISIONAL_ALARM_HM
                and bool(provisional))
        detail = (", ".join(f"{k} {n}" for k, n in sorted(reasons.items())) or "every name has levels") + (
            f" — basis mismatch: {', '.join(basis[:6])}" if basis else "") + (
            f" — 15:15 auction bar withheld (daily/30m basis): {', '.join(withheld[:6])}" if withheld else "") + (
            f" — provisional {'after ' + PROVISIONAL_ALARM_HM if late else 'candle'}: {len(provisional)} ({', '.join(provisional[:6])})"
            if provisional else "")
        ok = not basis and not withheld and not late
        return Check("zones", ok, detail=detail,
                     value=float(len(basis) + len(withheld) + (len(provisional) if late else 0)))

    def _latency_check(self) -> Check:
        """Bar close → the decision starting (the exchange-candle reconcile is inside it), and the
        broker's REST calls; slow is a fault only while a segment is open."""
        lat = sorted(self._decision_lat)
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None
        rest = self.rest.stats()
        slow = p95 is not None and p95 > DECISION_LATENCY_P95_MAX_S and self.market_open_now()
        detail = (f"decision p95 {p95:.1f}s over {len(lat)}" if p95 is not None else "no 30m decision yet") + (
            f"; 5paisa REST p50 {rest.get('latency_p50_s')}s p95 {rest.get('latency_p95_s')}s")
        return Check("latency", not slow, detail=detail, value=p95)

    def health_snapshot(self) -> dict[str, Any]:
        fh = self.feed.health
        open_now = self.market_open_now()
        # Outside the session a silent socket and a dead token are the normal state of affairs,
        # not faults. A health signal that is red every evening is one nobody reads (R17), so the
        # feed and session checks are informational until a segment is open. The broker's token
        # dies at 23:59:59 IST every day; `usable` is what a call actually needs.
        sess = self.auth.session
        daily = self.daily_audit()
        checks = [
            # The detail says what IS, not what once went wrong: the last error stayed on a connected
            # feed and the failure text on a fresh one (09:18, 28 Sep: "nodename nor servname…" and
            # "no message for over 2 minutes" beside two passing checks).
            Check(
                "feed_connected",
                (fh.connected or not self.s.feed_enabled) if open_now else True,
                detail=(
                    "market closed" if not open_now
                    else "feed disabled" if not self.s.feed_enabled
                    else f"connected · {fh.reconnects} reconnects since boot" if fh.connected
                    else f"disconnected — {fh.last_error or 'no error recorded'}"
                ),
            ),
            Check(
                "feed_fresh",
                (fh.silence_s is None or fh.silence_s < 120) if open_now else True,
                value=fh.silence_s,
                detail=(
                    "market closed" if not open_now
                    else "no message yet" if fh.silence_s is None
                    else f"no message for {fh.silence_s:.0f} s (over 2 minutes)" if fh.silence_s >= 120
                    else f"last message {fh.silence_s:.1f} s ago"
                ),
            ),
            Check(
                "broker_session",
                bool(sess and sess.usable) if open_now else True,
                value=round(sess.seconds_left / 60, 1) if sess else None,
                detail=("token expired — the next call re-logs in" if sess and not sess.usable else "")
                if open_now
                else "market closed",
            ),
            Check(
                "reconciled",
                self.reconciler_positions is None or not self.reconciler_positions.frozen,
                detail=self.reconciler_positions.freeze_reason if self.reconciler_positions else "",
            ),
            Check("breaker", not self.gateway.breaker_tripped,
                  detail=", ".join(f"{BOOK_LABELS.get(b, b)} tripped" for b in sorted(self.gateway.tripped_books))),
            Check(
                "bars_warm",
                sum(1 for sym in self.underlyings if self.store.count(sym, DECISION_TF) >= 21)
                >= max(1, len(self.underlyings) // 2)
                if self.underlyings
                else True,
                detail="fewer than half the universe has 21+ decision bars",
            ),
            # TORNTPHARM 2026-09-25: one 30m bucket closed — and was decided — 22 times. The
            # aggregator no longer reopens a bucket it closed and the decision path takes each
            # (symbol, bucket) once; this says so if either guard ever has to act.
            Check(
                "bars_close_once",
                self._duplicate_closes == 0,
                value=float(self._duplicate_closes),
                detail=(
                    f"{self._duplicate_closes} repeated 30m closes dropped since boot; "
                    f"{self.aggregator.late_ticks} ticks arrived for an already-closed bucket"
                ),
            ),
            self._volume_check(),
            self._bar_audit_check(),
            self._duty_check(),
            self._zones_check(daily),
            self._latency_check(),
            Check(
                "pivots_ready",
                (daily.ready and not self._daily_failed and not self.leg_pivots.failed_codes)
                if self.underlyings
                else True,
                detail=(
                    f"{daily.summary()}; legs {self.leg_pivots.loaded} loaded, "
                    f"{self.leg_pivots.failed} failed, {self.leg_pivots.refused} refused"
                ),
            ),
        ]
        return {
            **self.health.evaluate(checks),
            "mode": self.mode().value,
            "armed_until": self._armed_until,
            "halted": self.halted()[0],
            "feed": {
                "connected": fh.connected,
                # how far behind the socket reader is — see FeedHealth.note_dispatch
                "dispatch_lag_ms": round(fh.dispatch_lag_ms, 1),
                "dispatch_lag_max_ms": round(fh.dispatch_lag_max_ms, 1),
                "frames_behind": fh.frames_behind,
                "messages": fh.messages,
                "ticks": fh.ticks,
                "depth": fh.depth,
                "oi": fh.oi,
                "reconnects": fh.reconnects,
                "silence_s": fh.silence_s,
                "subscriptions": fh.subscriptions,
                # one broker socket for two engines: who serves, who reads, and the hop's delay
                "hub": self.feed_hub.stats() if self.feed_hub is not None else self.feed.hub_stats(),
            },
            "bars": self.aggregator.stats() | self.store.stats() | {"duplicate_closes": self._duplicate_closes},
            # broker snapshots with no bid/ask: held quotes confirmed as current / kept as they were
            "quotes": {"snapshot_confirmed": self.snapshot_confirmed, "snapshot_kept": self.snapshot_kept,
                       "snapshot_book_confirmed": self.snapshot_book_confirmed,
                       "snapshot_held_marked": self.snapshot_held_marked},
            "gateway": self.gateway.stats(),
            "rest": self.rest.stats(),
            "catalogue": self.catalogue_loader.catalogue.stats(),
            "universe": (
                self.universe_builder.summary(self.groups, depth_symbols=self.s.depth_archive_list)
                if self.universe_builder
                else {}
            ),
            "fidelity": self.reconciler.snapshot() if self.reconciler_ready else {},
            "micro": self.micro.stats(),
            "option_oi_tracked": len(self.option_oi),
            "oi_candles": self.oi_candles.stats(),
            "duties": self.guards.snapshot(),
            "feed_rate": self.feed_rate.snapshot(),
            "oi_broker_fields": dict(self.oi_frames),
            "oi_reference": {k: sum(1 for v in self._oi_ref_src.values() if v == k) for k in ("nse", "archive", "preopen")},
            "archive": self.archive.stats(),
            "tape": self.tape.stats(),
            "tape_full": self.fulltape.stats(),
            "depth": {
                "following": len(self._depth_following),
                "pinned_for_archive": len(self._depth_pinned),
                "syncs": self.depth_syncs,
                "added": self.depth_adds,
                "dropped": self.depth_drops,
                "cap": self.s.depth_max_subscriptions,
            },
            "telegram": self.telegram.stats(),
            "booting": self.booting,
            "positions_open": len([p for p in self.positions.values() if p.status == "OPEN"]),
            "positions_stale_quote": len(self._stale_positions),
            "boot_notes": self.boot_notes,
        }

    def strategy_stats(self) -> dict[str, Any]:
        return {
            StrategyKey.FUDKII.value: self.fudkii.stats.to_json(),
            StrategyKey.FUKAA.value: self.fukaa.stats.to_json(),
        }


# -- (de)serialisation helpers -----------------------------------------------------------------------


def _instrument_json(i: Instrument) -> dict[str, Any]:
    return {
        "scrip_code": i.scrip_code,
        "symbol": i.symbol,
        "segment": i.segment.value,
        "kind": i.kind.value,
        "name": i.name,
        "lot_size": i.lot_size,
        "tick_size": i.tick_size,
        "multiplier": i.multiplier,
        "expiry": i.expiry,
        "strike": i.strike,
        "option_type": i.option_type.value,
        "underlying": i.underlying,
    }


def _instrument_from(d: dict[str, Any]) -> Instrument:
    from .domain import OptionType

    return Instrument(
        scrip_code=d["scrip_code"],
        symbol=d["symbol"],
        segment=Segment(d["segment"]),
        kind=InstrumentKind(d["kind"]),
        name=d.get("name", ""),
        lot_size=int(d.get("lot_size", 1)),
        tick_size=float(d.get("tick_size", 0.05)),
        multiplier=int(d.get("multiplier", 1)),
        expiry=d.get("expiry", ""),
        strike=float(d.get("strike", 0)),
        option_type=OptionType(d.get("option_type", "")),
        underlying=d.get("underlying", ""),
    )


def _order_json(o: Any, audit: dict[str, Any] | None = None) -> dict[str, Any]:
    """The order row; ``audit`` is its trail — signal, limit placed, reprices, filled/crossed/
    cancelled, and the book at each (exec/resting.py)."""
    return {
        **({"exec": audit} if audit else {}),
        "id": o.id,
        "client_order_id": o.client_order_id,
        "strategy": o.strategy,
        "scrip_code": o.scrip_code,
        "symbol": o.symbol,
        "side": o.side.value,
        "purpose": o.purpose.value,
        "qty": o.qty,
        "mode": o.mode,
        "status": o.status,
        "signal_id": o.signal_id,
        "position_id": o.position_id,
        "reason": o.reason,
        "ts": o.ts,
        "avg_price": o.avg_price,
        "filled": o.filled,
        "charges": o.charges,
        "slippage_bps": o.slippage_bps,
        "broker_order_id": o.broker_order_id,
        "note": o.note,
    }


#: rejections after which nothing is working on the order anywhere, so a retry is a new order
_DEFINITIVE_REJECTIONS = frozenset({
    Decision.REJECTED_BOOK, Decision.REJECTED_BROKER, Decision.REJECTED_CAP,
    Decision.REJECTED_RISK, Decision.REJECTED_HALT,
})


def exit_client_order_id(pos: Position, decision: Any, attempt: int = 0, *, cross: bool = False) -> str:
    """Idempotency key for an exit: the same position, rung and reason must always produce the
    same id (a retry is the same order), and two positions must never share one. It was keyed on
    the signal — and the RT twin carries its parent's signal id, so on 2026-09-23 14:35 the twin's
    SL-EQ exit collided with the parent's and was refused as a duplicate, every second, 800 times,
    while the underlying sat through the stop.

    ``attempt`` counts DEFINITIVE rejections of this exit. An order a venue refused, or one the
    paper matcher never priced, is not working anywhere, so its retry is a new order and needs a
    new id; an order whose fate is unknown keeps the same id and stays blocked."""
    ref = (pos.exec_log or {}).get("ref")
    if ref:
        # "FII-RTX-260926-192510-007-SLE0R1X-HINDUNILVR-1960CE-L4": the position's own entry ref, what
        # the exit is and at which rung, the retry and the cross — all inside the broker's 38 characters
        code = EXIT_CODES.get(decision.reason, str(decision.reason.value)[:3].upper())
        return f"{ref}-{code}{pos.targets_hit}{f'R{attempt}' if attempt else ''}{'X' if cross else ''}-{_contract_tag(pos.instrument, pos.qty)}"
    base = f"{pos.id}|EXIT|{pos.targets_hit}|{decision.reason.value}"  # a position opened before readable ids
    base = f"{base}|r{attempt}" if attempt else base
    return f"{base}|X" if cross else base


def _position_json(p: Position) -> dict[str, Any]:
    return {
        "id": p.id,
        "strategy": p.strategy,
        "instrument": _instrument_json(p.instrument),
        "underlying": _instrument_json(p.underlying),
        "symbol": p.underlying.symbol,
        "scrip_code": p.instrument.scrip_code,
        "side": p.side.value,
        "qty": p.qty,
        "qty_remaining": p.qty_remaining,
        "entry": p.entry,
        "opened_ts": p.opened_ts,
        "signal_id": p.signal_id,
        "direction": p.direction.value,
        "equity_entry": p.equity_entry,
        "equity_sl": p.equity_sl,
        "equity_targets": list(p.equity_targets),
        "option_sl": p.option_sl,
        "option_targets": list(p.option_targets),
        "initial_option_sl": p.initial_option_sl,
        "r_unit": p.r_unit,
        "peak_r": p.peak_r,
        "mfe_r": p.mfe_r,
        "mae_r": p.mae_r,
        "charges": p.charges,
        "targets_hit": p.targets_hit,
        "status": p.status,
        "bars_held": p.bars_held,
        "closed_ts": p.closed_ts,
        "exit_price": p.exit_price,
        "exit_reason": p.exit_reason,
        "grade": p.grade,
        "note": p.note,
        "option_t1": p.option_t1,
        "armed_by": p.armed_by,
        "armed_ts": p.armed_ts,
        "ratchet_sl": p.ratchet_sl,
        "t_touch_ts": p.t_touch_ts,
        "t_close_ok": p.t_close_ok,
        "sustained_idx": p.sustained_idx,
        "option_edm": p.option_edm,
        "peak_mid": p.peak_mid,
        "trail_dwell": p.trail_dwell,
        "breach_since": p.breach_since,
        "equity_atr": p.equity_atr,
        "realised_gross": p.realised_gross,
        "entry_charges": p.entry_charges,
        "pnl": p.pnl,
        "line_breach_since": p.line_breach_since,
        "exec_log": p.exec_log,
    }


def _signal_from_json(d: dict[str, Any]) -> Signal:
    """A signal as ``Signal.to_json`` wrote it (its gates are not rebuilt — a carried trigger is
    re-judged by each book's own gates)."""
    return Signal(
        strategy=StrategyKey(d["strategy"]), symbol=d["symbol"], direction=Direction(d["direction"]), ts=int(d["ts"]),
        entry=float(d["entry"]), stop=float(d["stop"]), targets=tuple(float(t) for t in d.get("targets") or ()),
        grade=d.get("grade") or "", rr=float(d.get("rr") or 0.0), score=float(d.get("score") or 0.0),
        confidence=float(d.get("confidence") or 0.0), reason=d.get("reason") or "", evidence=dict(d.get("evidence") or {}),
        source_signal_id=d.get("source_signal_id") or "", context=dict(d.get("context") or {}),
    )


def _position_from_json(d: dict[str, Any]) -> Position:
    return Position(
        id=d["id"],
        strategy=d["strategy"],
        instrument=_instrument_from(d["instrument"]),
        underlying=_instrument_from(d["underlying"]),
        side=PosSide(d["side"]),
        qty=int(d["qty"]),
        entry=float(d["entry"]),
        opened_ts=float(d["opened_ts"]),
        signal_id=d["signal_id"],
        direction=Direction(d["direction"]),
        equity_entry=float(d.get("equity_entry", 0)),
        equity_sl=float(d.get("equity_sl", 0)),
        equity_targets=tuple(d.get("equity_targets", ())),
        option_sl=float(d.get("option_sl", 0)),
        option_targets=tuple(d.get("option_targets", ())),
        initial_option_sl=float(d.get("initial_option_sl", 0)),
        r_unit=float(d.get("r_unit", 0)),
        peak_r=float(d.get("peak_r", 0)),
        mfe_r=float(d.get("mfe_r", 0)),
        mae_r=float(d.get("mae_r", 0)),
        charges=float(d.get("charges", 0)),
        targets_hit=int(d.get("targets_hit", 0)),
        qty_remaining=int(d.get("qty_remaining", d["qty"])),
        status=d.get("status", "OPEN"),
        bars_held=int(d.get("bars_held", 0)),
        grade=d.get("grade", ""),
        note=d.get("note", ""),
        option_t1=float(d.get("option_t1", 0)),
        armed_by=str(d.get("armed_by", "")),
        armed_ts=d.get("armed_ts"),
        ratchet_sl=float(d.get("ratchet_sl", 0)),
        t_touch_ts=d.get("t_touch_ts"),
        t_close_ok=bool(d.get("t_close_ok", False)),
        sustained_idx=int(d.get("sustained_idx", -1)),
        option_edm=float(d.get("option_edm", 0)),
        peak_mid=float(d.get("peak_mid") or 0.0),
        trail_dwell=int(d.get("trail_dwell") or 0),
        breach_since=d.get("breach_since"),
        equity_atr=float(d.get("equity_atr") or 0.0),
        realised_gross=float(d.get("realised_gross") or 0.0),
        entry_charges=float(d.get("entry_charges") or 0.0),
        line_breach_since=d.get("line_breach_since"),
        exec_log=dict(d.get("exec_log") or {}),
    )


def _trade_from(p: Position, now: float) -> Trade:
    exit_price = p.exit_price or 0.0
    # The slices as they filled. The last fill's price times the whole quantity booked RT-X's
    # HEROMOTOCO (+₹1,015 real) as -₹552 on 2026-09-25. Positions saved before the slices were kept
    # fall back to the old figure.
    gross = p.realised_gross if p.realised_gross else (exit_price - p.entry) * p.dir_sign * p.qty * p.instrument.multiplier
    net = gross - p.charges - p.entry_charges
    r = net / (p.r_unit * p.qty * p.instrument.multiplier) if p.r_unit > 0 else 0.0
    return Trade(
        id=new_id("trd"),
        position_id=p.id,
        strategy=p.strategy,
        scrip_code=p.instrument.scrip_code,
        symbol=p.instrument.name or p.instrument.scrip_code,
        underlying=p.underlying.symbol,
        instrument_kind=p.instrument.kind.value,
        side=p.side,
        qty=p.qty,
        entry=p.entry,
        exit=p.exit_price or 0.0,
        gross=round(gross, 2),
        charges=round(p.charges + p.entry_charges, 2),
        net=round(net, 2),
        r_multiple=round(r, 3),
        mfe_r=round(p.mfe_r, 3),
        mae_r=round(p.mae_r, 3),
        exit_reason=p.exit_reason or ExitReason.MANUAL.value,
        opened_ts=p.opened_ts,
        closed_ts=p.closed_ts or now,
        duration_s=(p.closed_ts or now) - p.opened_ts,
        signal_id=p.signal_id,
        grade=p.grade,
        equity_entry=p.equity_entry,
        equity_sl=p.equity_sl,
        equity_targets=tuple(p.equity_targets),
        r_unit=p.r_unit,
        multiplier=p.instrument.multiplier,
    )


def _trade_json(t: Trade) -> dict[str, Any]:
    return {
        "id": t.id,
        "position_id": t.position_id,
        "strategy": t.strategy,
        "scrip_code": t.scrip_code,
        "symbol": t.symbol,
        "underlying": t.underlying,
        "instrument_kind": t.instrument_kind,
        "side": t.side.value,
        "qty": t.qty,
        "entry": t.entry,
        "exit": t.exit,
        "gross": t.gross,
        "charges": t.charges,
        "net": t.net,
        "r_multiple": t.r_multiple,
        "mfe_r": t.mfe_r,
        "mae_r": t.mae_r,
        "exit_reason": t.exit_reason,
        "opened_ts": t.opened_ts,
        "closed_ts": t.closed_ts,
        "duration_s": t.duration_s,
        "signal_id": t.signal_id,
        "grade": t.grade,
        "equity_entry": t.equity_entry,
        "equity_sl": t.equity_sl,
        "equity_targets": list(t.equity_targets),
        "r_unit": t.r_unit,
        "multiplier": t.multiplier,
    }
