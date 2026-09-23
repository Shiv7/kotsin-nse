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
import time
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import structlog

from .alerts.engine import AlertEngine
from .bars.aggregator import Aggregator
from .bars.daily import MIN_DAILY_BARS, REPAIR_BATCH, DailyCache, is_official, previous_session
from .bars.daily import audit as audit_daily
from .bars.indicators import atr, dried_volume, volume_surges
from .bars.micro import MicroAggregator
from .bars.periods import monthly, previous_complete, weekly
from .bars.pivots import (
    ZONE_TOLERANCE_PCT,
    PivotPoint,
    Zone,
    classic_pivots,
    cluster_zones,
    pivot_points,
)
from .bars.store import BarStore
from .bars.unified import BarSource, UnifiedBar
from .bars.verify import BarReconciler
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
from .exec.gateway import Decision, Gateway, LiveCaps, LiveContext, Mode
from .exec.live import LiveExecutor
from .exec.paper import BookSnapshot, PaperMatcher
from .exec.reconcile import Reconciler
from .instrument.catalogue import CatalogueLoader
from .instrument.legs import OPTION_CLUSTER_TOL_PCT, LegPivotLoader, otm_legs
from .instrument.select import (
    Quote,
    SelectionPolicy,
    choose_expiry,
    estimate_delta,
    map_levels_to_option,
    select_future,
    select_option,
)
from .instrument.universe import ScripGroup, UniverseBuilder, UniversePolicy
from .ledger.db import Ledger
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
    TF_SECONDS,
    TradingCalendar,
    bucket_start,
    is_open,
    ist_day,
    ist_hm,
    ist_naive_to_ts,
    ist_today,
    past_force_flat,
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
from .ops.health import Check, HealthMonitor
from .ops.tape import ROLE_EQUITY, ROLE_FUTURE, ROLE_INDEX, ROLE_OPTION, Tape
from .ops.telegram import Telegram
from .risk.costs import CostModel
from .risk.exits import ExitEngine, MarketView, apply_exit
from .risk.exposure import ExposureBook
from .risk.limits import CT_X_LIMITS, CT_Y_LIMITS, RT_N_LIMITS, RT_X_LIMITS, RT_Y_LIMITS, RiskLimits
from .risk.sizing import size_position
from .risk.wallet import Wallet
from .strategy.base import Outcome, Signal
from .strategy.counter import NO_WALL, CounterDecision, Leg, counter_route, flipped_signal
from .strategy.fudkii import Fudkii, FudkiiConfig
from .strategy.fukaa import Fukaa, FukaaConfig, select
from .strategy.keys import ALL_KEYS, INITIAL_INR, StrategyKey
from .venue.fivepaisa.auth import Authenticator
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
    StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y,
})
MIN_STOP_TICKS = 8


@dataclass
class StrategyContext:
    """The Context a strategy sees. Deliberately the smallest possible surface."""

    engine: Engine
    _state: dict[str, Any] = field(default_factory=dict)

    def bars(self, symbol: str, tf: str, n: int) -> Sequence[UnifiedBar]:
        return self.engine.store.bars(symbol, tf, n)

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


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.bus = Bus()
        self.store = BarStore()
        self.ledger = Ledger(settings.db_url)
        self.costs = CostModel(settings)
        self.limits = RiskLimits()
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
            StrategyKey.FUDKII_RT_MCX.value: self.exits_rt,
            StrategyKey.FUDKII_RT_N.value: ExitEngine(RT_N_LIMITS),
            StrategyKey.FUDKII_RT_Y.value: ExitEngine(RT_Y_LIMITS),
            StrategyKey.FUDKII_CT_X.value: ExitEngine(CT_X_LIMITS),
            StrategyKey.FUDKII_CT_Y.value: ExitEngine(CT_Y_LIMITS),
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
        )
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
        self._decision_tasks: set[asyncio.Task[Any]] = set()
        self._sweep_task: asyncio.Task[Any] | None = None
        self._intraday_rebuild_day: str = ""
        self.matcher = PaperMatcher(self.costs)
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
        self._zone_cache: dict[str, tuple[str, list[Zone]]] = {}
        #: the front future's candles per symbol for the current trigger bar (see _fut_context)
        self._fut_cache: dict[str, tuple[int, dict[str, Any] | None]] = {}
        #: every signal handled today, by id — what an operator take re-enters from
        self._signals_today: dict[str, Signal] = {}
        #: every symbol decides in its own task, so a 09:45 burst of sixteen signals would be
        #: thirty-two concurrent historical calls; the broker client has no limiter of its own
        self._fut_sem = asyncio.Semaphore(4)
        # -- the pivot data plane (docs/PIVOTS.md) --
        self.daily_cache = DailyCache(settings.data_dir / "daily")
        self._daily_failed: set[str] = set()  # 1d fetch raised; the repair loop retries every pass
        self._daily_confirmed: dict[str, date] = {}  # asked once for this expected session already
        self._daily_due = False  # a full refetch is owed: day roll or a refresh slot
        self._legs_due = False  # a full leg reload is owed: day roll
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

    # -- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        await self.ledger.init()
        await self._load_control()
        await self._load_wallets()
        await self._load_positions()
        for cid in await self.ledger.known_client_order_ids():
            self.gateway.remember(cid)

        warn = self.calendar.missing_holidays_warning()
        if warn:
            self.boot_notes.append(warn)
            log.warning("calendar.no_holidays", detail=warn)

        if not self.s.has_credentials:
            self.boot_notes.append(
                "no 5paisa credentials — the engine is API-only. 5paisa has no anonymous feed, so "
                "even PAPER needs a session. Set KN_FP_* in backend/.env."
            )
            log.warning("engine.no_credentials")
        elif self.s.engine_enabled:
            await self._boot_market()

        self._banner()
        self._tasks = [
            asyncio.create_task(self._clock(), name="clock"),
            asyncio.create_task(self._housekeeping(), name="housekeeping"),
        ]
        if self.s.engine_enabled and self.s.feed_enabled and self.s.has_credentials:
            self._tasks.append(asyncio.create_task(self.feed.run(), name="feed"))

    async def stop(self) -> None:
        self._stop.set()
        await self.feed.stop()
        await self.committee.stop()
        try:
            await asyncio.to_thread(self.archive.flush, final=True)
        except Exception as exc:  # noqa: BLE001 - shutting down; the archive must not block it
            log.warning("archive.final_flush_failed", error=str(exc))
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
            **self.universe_builder.summary(groups),
            sample=[g.root for g in list(groups.values())[:8]],
        )
        for inst in universe:
            self.aggregator.track(inst)
        self._seed_daily_from_cache(universe)
        await self._backfill(universe)
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
        self._future_to_underlying = {f.scrip_code: g.root for g in groups.values() for f in g.futures}

        # Futures are OI sources, not bar sources: on NSE the front future's `symbol` is the cash
        # symbol, and tracking it wrote futures ticks into the equity's bars (found 2026-09-21).
        # `subscriptions()` puts them on mf+oi only; the aggregator ignores untracked codes.
        subs = UniverseBuilder.subscriptions(groups.values())
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
                list(self.positions.values()),
                at_venue=self.mode() in (Mode.LIVE, Mode.LIVE_CAPPED),
            )

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

        Sequential and rate-limit friendly: the broker's historical endpoint is the slowest thing
        we touch, and hammering it at boot is how the old stack earned its retry storms.
        """
        end = ist_today()
        start_intraday = end - timedelta(days=max(7, self.s.backfill_days // 2))
        start_daily = end - timedelta(days=400)  # a year of dailies for monthly pivots
        ok = failed = 0
        for inst in universe:
            for tf, start in ((DECISION_TF, start_intraday), ("1d", start_daily)):
                try:
                    rows = await self.rest.candles(
                        inst, tf, start.isoformat(), end.isoformat()
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    if tf == "1d":
                        self._daily_failed.add(inst.symbol)
                    log.warning("backfill.failed", symbol=inst.symbol, tf=tf, error=str(exc))
                    continue
                if not rows:
                    if tf == "1d":
                        self._daily_failed.add(inst.symbol)
                    continue
                if tf == "1d":
                    self._seed_daily(inst, rows)
                else:
                    self.aggregator.seed(inst, tf, rows, ts_of=ist_naive_to_ts)
                    for lower in ("5m", "15m"):
                        _ = lower  # lower frames rebuild from live ticks; no extra REST calls
                ok += 1
                await asyncio.sleep(0.15)
        log.info("backfill.done", series_ok=ok, failed=failed)

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
        self.store.seed(inst.symbol, "1d", bars)
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
        await self.feed.subscribe("md", insts)
        await self.feed.subscribe("oi", [i for i in insts if i.kind is not InstrumentKind.EQUITY])
        log.info("positions.resubscribed", instruments=[i.name or i.scrip_code for i in insts])
        return insts

    def _seed_daily_from_cache(self, universe: list[Instrument]) -> None:
        """The last known official candles, before REST is asked. A boot while the broker's
        historical endpoint is down then resumes on real levels; ``store.seed`` lets the REST
        refetch win over these the moment it answers."""
        hit = 0
        for inst in universe:
            bars = self.daily_cache.load(inst.symbol, inst.scrip_code)
            if bars:
                self.store.seed(inst.symbol, "1d", bars)
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
            {sym: self.store.bars(sym, "1d") for sym in self.underlyings}, ist_today(), self.calendar
        )

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
            a = self.daily_audit()
            # A call that raised is retried every pass whatever the audit calls the name; a
            # dormant name (nothing at the broker) is asked once per session and left alone.
            wanted = sorted(
                {sym for sym in a.needs_refresh if self._daily_confirmed.get(sym) != a.expected_prev}
                | {sym for sym in self._daily_failed if sym in self.underlyings}
            )
            for sym in wanted:
                if a.expected_prev is not None:
                    self._daily_confirmed[sym] = a.expected_prev
            if wanted:
                n = await self._refetch_daily(wanted[:REPAIR_BATCH])
                log.info("daily.repaired", refetched=n, asked=len(wanted), audit=a.summary())
            if self._legs_due or self.leg_pivots.failed_codes:
                legs = self._expected_legs(self.groups.values())
                todo = legs if self._legs_due else self.leg_pivots.missing(legs)
                self._legs_due = False
                if todo:
                    await self.leg_pivots.load(todo, ist_today())
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
        if self.gateway.breaker_tripped:
            return True, "order gateway circuit breaker"
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
        ltp = float(tick.get("ltp") or 0)
        if ltp > 0:
            self.ltps[code] = ltp
            self.quotes[code] = Quote(
                ltp=ltp,
                bid=float(tick.get("bid") or 0),
                ask=float(tick.get("ask") or 0),
                ts=float(tick.get("recv_ts") or time.time()),
            )
        await self.aggregator.on_tick(tick)
        await self.bus.publish(Topic.TICK, tick)

    async def _on_depth(self, depth: dict[str, Any]) -> None:
        code = str(depth["scrip_code"])
        ts = float(depth.get("recv_ts") or time.time())
        self.books[code] = BookSnapshot(scrip_code=code, bids=depth["bids"], asks=depth["asks"], ts=ts)
        self.micro.on_depth(code, depth["bids"], depth["asks"], ts)

    async def _on_oi(self, oi: dict[str, Any]) -> None:
        fut_code = str(oi["scrip_code"])
        self.archive.oi(
            fut_code,
            float(oi.get("recv_ts") or time.time()),
            float(oi["open_interest"]),
            float(oi["oi_change_pct"]) if oi.get("oi_change_pct") is not None else None,
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
        inst = self.underlyings.get(symbol)
        if inst is None:
            return
        self.aggregator.set_oi(
            inst.scrip_code,
            oi=int(oi["open_interest"]),
            change_pct=float(oi["oi_change_pct"]),
            fut_code=fut_code,
        )

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
        today = ist_today()
        regime = self.volatility_regime(symbol)
        key = f"{today.isoformat()}:{regime.band.value}"
        hit = self._zone_cache.get(symbol)
        if hit and hit[0] == key:
            return hit[1]
        dailies = self.store.bars(symbol, "1d")
        # Deliberately not cached, and deliberately empty: a name whose previous session is still
        # the tick-built bar (its close is the last print, not the exchange's) gets no levels
        # rather than wrong ones, and gets real ones the moment the repair loop lands the official
        # candle — not at the next band change.
        prev = previous_session(dailies, today)
        if len(dailies) < MIN_DAILY_BARS or prev is None or not is_official(prev):
            return []
        points = self._pivot_points(symbol)
        atr_v = atr(self.store.bars(symbol, DECISION_TF, 60), 14)
        px = self.ltps.get(
            getattr(self.underlyings.get(symbol), 'scrip_code', '')
        ) or (dailies[-1].close if dailies else 0.0)
        # ATR expressed as a percentage of price, because cluster_zones works in percent — the
        # conversion is what makes 'k x ATR' and a percentage tolerance the same statement.
        tol = (
            regime.k * atr_v / px * 100
            if atr_v and px > 0
            else ZONE_TOLERANCE_PCT
        )
        zones = cluster_zones(points, tolerance_pct=tol)
        self._zone_cache[symbol] = (key, zones)
        return zones

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

    # -- decision path -----------------------------------------------------------------------------

    async def _on_bar_close(self, bar: UnifiedBar) -> None:
        await self.bus.publish(Topic.BAR, bar)
        if bar.tf == "1m":
            self.archive.bar(bar)
        # Advisory books run on every frame — MCX_BB15 decides on 15m and FUDKII-RT on 1m, so
        # this must sit above the decision-timeframe return, not inside it.
        self.alerts.on_bar(bar)
        if bar.tf != DECISION_TF:
            return
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
        if self.reconciler_ready and self.s.has_credentials:
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
        try:
            await self._decide(current)
        except Exception as exc:
            log.exception("decide.failed", symbol=bar.symbol, error=str(exc))

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
            if fresh:
                await self.feed.subscribe("mf", fresh)
                await self.feed.subscribe("md", fresh)
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

        for sig in (*base_out.signals, *admitted):
            await self._handle_signal(sig, bar)
        for sig in base_out.signals:
            # the fade is strictly additive: it may never cost the in-trend books their entry
            try:
                await self._handle_counter(sig, bar)
            except Exception as exc:
                log.exception("counter.failed", symbol=sig.symbol, error=str(exc))

    async def _handle_signal(self, sig: Signal, bar: UnifiedBar | None) -> None:
        self._signals_today[sig.signal_id] = sig
        await self.bus.publish(Topic.SIGNAL, sig)
        self.alerts.adopt_signal(sig.to_json(), bar)
        underlying = self.underlyings.get(sig.symbol)
        if underlying is None:
            await self.ledger.insert_signal(sig.to_json(), "NO_UNDERLYING", "not in the universe")
            return

        selection = await self._select_instrument(underlying, sig)
        if not selection.ok or selection.instrument is None:
            await self.ledger.insert_signal(sig.to_json(), "NO_INSTRUMENT", selection.reason)
            log.info("signal.no_instrument", symbol=sig.symbol, reason=selection.reason)
            return

        inst = selection.instrument
        # Subscribe the contract we are about to hold on BOTH channels. `mf` gives the LTP the exit
        # engine prices against; `md` gives the ladder the paper matcher walks. Without `md` every
        # paper fill silently took the degraded LTP+slippage path, which is the thing PaperMatcher
        # exists to avoid.
        await self.feed.subscribe("mf", [inst])
        await self.feed.subscribe("md", [inst])
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
        if inst.is_option:
            option_sl = self.floored_option_stop(sig.strategy, selection.premium, option_sl, inst.tick_size)

        wallet = self.wallets[sig.strategy.value]
        if wallet.halted:
            await self.ledger.insert_signal(sig.to_json(), "WALLET_HALTED", wallet.halt_reason)
            return
        # an RT/CT book entered directly (the counter-trend fade) sizes and pools under its own
        # limits; the base books keep theirs
        book = self._exits_by_strategy.get(sig.strategy.value)
        book_limits = book.limits if book is not None else self.limits
        exposure = self._exposure_by_strategy.get(sig.strategy.value, self.exposure)

        sizing = size_position(
            instrument=inst,
            premium=selection.premium,
            option_stop=option_sl,
            option_target1=option_targets[0] if option_targets else None,
            balance=wallet.balance,
            available=wallet.available,
            limits=book_limits,
            costs=self.costs,
        )
        if not sizing.ok:
            await self.ledger.insert_signal(sig.to_json(), "NOT_SIZED", sizing.reason)
            log.info("signal.not_sized", symbol=sig.symbol, reason=sizing.reason)
            return

        verdict = exposure.check(
            strategy=sig.strategy.value,
            underlying=sig.symbol,
            outlay=sizing.outlay,
            positions=list(self.positions.values()),
            total_capital=wallet.balance,  # each book is checked against its own purse
        )
        if not verdict.allowed:
            await self.ledger.insert_signal(sig.to_json(), "EXPOSURE", verdict.reason)
            return

        intent = OrderIntent(
            strategy=sig.strategy.value,
            instrument=inst,
            side=OrderSide.BUY,
            qty=sizing.qty,
            purpose=Purpose.ENTRY,
            signal_id=sig.signal_id,
            client_order_id=f"{sig.signal_id}|ENTRY",
            reason=sig.reason,
            ref_price=selection.premium,
        )
        result = await self._submit(intent, verdict_ok=verdict.allowed, verdict_reason=verdict.reason)
        await self.ledger.insert_order(_order_json(result.order), result.decision.value)
        await self.ledger.insert_signal(
            sig.to_json(), result.decision.value, result.order.note or sig.reason
        )
        if result.fill is None:
            return

        pos = Position(
            id=new_id("pos"),
            strategy=sig.strategy.value,
            instrument=inst,
            underlying=underlying,
            side=PosSide.LONG,  # both books BUY premium; direction lives on `direction`
            qty=result.fill.qty,
            entry=result.fill.price,
            opened_ts=result.fill.ts,
            signal_id=sig.signal_id,
            direction=sig.direction,
            equity_entry=sig.entry,
            equity_sl=sig.stop,
            equity_targets=tuple(sig.targets),
            option_sl=option_sl,
            option_targets=option_targets,
            grade=sig.grade,
            note=f"delta≈{delta:.2f} (estimated)",
        )
        if book is not None and book_limits.own_ladder:
            self._stamp_own_ladder(pos, inst, book_limits, sig.symbol)
        self.positions[pos.id] = pos
        wallet.reserve(pos.entry * pos.qty * inst.multiplier, result.fill.ts)
        wallet.apply_charges(result.fill.charges, result.fill.ts)
        await self.ledger.upsert_position(_position_json(pos))
        await self.ledger.upsert_wallet(wallet.strategy, wallet.to_json())
        # A twin failing must never cost the real entry. The primary position is already
        # registered and its wallet charged by this point; the mirror is strictly additive, so
        # it is wrapped rather than allowed to unwind a trade that succeeded.
        try:
            await self._open_rt_twin(pos, inst, result)
        except Exception as exc:
            log.exception("rt_twin.failed", of=pos.id, symbol=pos.underlying.symbol, error=str(exc))
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
        self.telegram.fire_and_forget(
            f"🟢 {pos.strategy} {pos.underlying.symbol} {sig.direction.value} "
            f"{inst.name or inst.scrip_code} qty {pos.qty} @ {pos.entry:.2f} "
            f"SL {pos.option_sl:.2f} grade {pos.grade} [{self.mode().value}]"
        )

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

    async def _open_rt_twin(self, pos: Position, inst: Instrument, result: Any) -> None:
        """Mirror a FUDKII entry into FUDKII_RT_X so only the exit policy differs.

        Same contract, same quantity, same fill price and the same instant. If the entries differed
        by even a tick the two equity curves would be comparing entries as well as exits, and the
        question being asked — does the RT exit policy do better — would no longer have a clean
        answer. The fill is *copied*, not re-matched: re-running the matcher would walk the ask
        ladder a second time and produce a different price for a trade that never happened twice.
        """
        # Commodities and equities keep separate purses: one CRUDEOIL lot is a different size of
        # bet from one BLUESTARCO lot, and a shared wallet would let whichever fired first decide
        # what the other could afford. NSE fills are mirrored into all three RT books; a
        # counter-trend fade entered by CT-X is mirrored into CT-Y.
        mcx = pos.underlying.segment is Segment.MCX_FO
        keys = {
            StrategyKey.FUDKII.value: [StrategyKey.FUDKII_RT_MCX] if mcx else [StrategyKey.FUDKII_RT_X, StrategyKey.FUDKII_RT_N, StrategyKey.FUDKII_RT_Y],
            StrategyKey.FUDKII_CT_X.value: [] if mcx else [StrategyKey.FUDKII_CT_Y],
        }.get(pos.strategy, [])
        if not keys:
            return
        cost = pos.entry * pos.qty * inst.multiplier
        opened = 0
        vol = (
            await self._volume_surges(pos.underlying)
            if any(self._exits_by_strategy[k.value].limits.dried_volume_v for k in keys)
            else None
        )
        for twin_key in keys:
            engine_for = self._exits_by_strategy[twin_key.value]
            lim_v = engine_for.limits.dried_volume_v
            if lim_v and vol:
                dry = [leg for leg, (s_t, s_t1) in vol.items() if dried_volume(s_t, s_t1, v=lim_v)]
                if dry:
                    why = "dried volume " + ", ".join(f"{leg} {vol[leg][0]:.2f}/{vol[leg][1]:.2f}" for leg in dry) + f" < {lim_v}"
                    log.info("rt_twin.skipped", book=twin_key.value, symbol=pos.underlying.symbol, reason=why)
                    self.alerts.mark_skipped(pos.signal_id, book=twin_key.value, reason=why)
                    await self.ledger.event("rt_twin.skipped", {"book": twin_key.value, "signal_id": pos.signal_id, "symbol": pos.underlying.symbol, "reason": why})
                    continue
            twin_wallet = self.wallets.get(twin_key.value)
            if twin_wallet is None or twin_wallet.halted:
                continue
            if twin_wallet.available < cost:
                log.info("rt_twin.skipped", book=twin_key.value, symbol=pos.underlying.symbol, reason="wallet")
                await self.ledger.event("rt_twin.skipped", {"book": twin_key.value, "signal_id": pos.signal_id, "symbol": pos.underlying.symbol, "reason": f"wallet: {twin_wallet.available:,.0f} available < {cost:,.0f}"})
                continue
            verdict = self._exposure_by_strategy[twin_key.value].check(
                strategy=twin_key.value,
                underlying=pos.underlying.symbol,
                outlay=cost,
                positions=list(self.positions.values()),
                total_capital=twin_wallet.balance,  # each book is checked against its own purse
            )
            if not verdict.allowed:
                log.info("rt_twin.skipped", book=twin_key.value, symbol=pos.underlying.symbol, reason=verdict.reason)
                await self.ledger.event("rt_twin.skipped", {"book": twin_key.value, "signal_id": pos.signal_id, "symbol": pos.underlying.symbol, "reason": f"exposure: {verdict.reason}"})
                continue
            twin = replace(
                pos,
                id=new_id("pos"),
                strategy=twin_key.value,
                note=f"{pos.note} · RT exit policy, twin of {pos.id}",
                targets_hit=0, ratchet_sl=0.0, armed_by="", armed_ts=None, sustained_idx=-1,
                t_touch_ts=None, t_close_ok=False, line_breach_since=None, peak_mid=0.0, trail_dwell=0,
            )
            lim = engine_for.limits
            if lim.own_ladder:
                self._stamp_own_ladder(twin, inst, lim, pos.underlying.symbol)
            self.positions[twin.id] = twin
            twin_wallet.reserve(cost, result.fill.ts)
            twin_wallet.apply_charges(result.fill.charges, result.fill.ts)
            await self.ledger.upsert_position(_position_json(twin))
            await self.ledger.upsert_wallet(twin_wallet.strategy, twin_wallet.to_json())
            log.info(
                "rt_twin.open",
                book=twin_key.value,
                twin=twin.id,
                of=pos.id,
                symbol=pos.underlying.symbol,
                qty=twin.qty,
                entry=twin.entry,
                targets=list(twin.option_targets),
            )
            opened += 1
        if opened:
            self.alerts.mark_entered(pos.signal_id, ts=result.fill.ts, price=pos.entry, qty=pos.qty)

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
        parents = sorted((sgn for sgn in latest.values() if sgn["strategy"] == StrategyKey.FUDKII.value), key=lambda x: x["ts"])
        by_source = {sgn["source_signal_id"]: sgn for sgn in latest.values() if sgn.get("source_signal_id") and sgn["strategy"] == StrategyKey.FUDKII_CT_X.value}
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
            for a in self.alerts.feed("FUDKII_RT", 500)
            if a.get("kind") == "ENTRY"
        }
        counter_books = {StrategyKey.FUDKII_CT_X.value, StrategyKey.FUDKII_CT_Y.value}
        cards = []
        for sgn in parents:
            sid = sgn["signal_id"]
            evs = ev_by_sig.get(sid, [])
            route = next((e for e in reversed(evs) if e.get("kind") == "counter.route"), None)
            fade = by_source.get(sid)
            fade_evs = ev_by_sig.get(fade["signal_id"], []) if fade else []
            skip = next((e for e in reversed(evs + fade_evs) if e.get("kind") == "rt_twin.skipped" and e.get("book") == book), None)
            operator = [e for e in evs + fade_evs if str(e.get("kind", "")).startswith("operator.") and e.get("book") == book]
            entry_sig = fade if key.value in counter_books else sgn
            ps = pos_by_key.get((book, entry_sig["signal_id"])) if entry_sig else None
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
            if ps:
                state = "OPEN" if ps.get("status") == "OPEN" else "TRADED"
            elif key is StrategyKey.FUDKII:
                state = sgn.get("decision") or "UNKNOWN"
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
            elif str(sgn.get("decision", "")).endswith("FILLED"):
                state = "NOT_MIRRORED"
            else:
                state = "NO_FILL"
            # the trigger bar and the underlying's path since, for the sparkline
            bars30 = self.store.bars(sgn["symbol"], DECISION_TF, 80)
            candle = next(({"o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in bars30 if int(b.ts) == int(sgn["ts"])), None)
            s_t, s_t1, base = volume_surges([b.volume for b in bars30 if b.ts <= sgn["ts"]], window=6, floor=1000.0)
            m1 = [b for b in self.store.bars(sgn["symbol"], "1m", 400) if b.ts >= sgn["ts"] - 1800]
            step = max(1, len(m1) // 120)
            spark = [[int(b.ts), b.close] for b in m1[::step]]
            ctx = sgn.get("context") or {}
            conf = ctx.get("confluence") or {}
            pros, cons = [], []
            if s_t and s_t >= 2.5:
                pros.append(f"volume surge {s_t:.1f}×")
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
            stop_pct = abs(float(sgn["entry"]) - float(sgn["stop"])) / float(sgn["entry"]) * 100 if sgn.get("stop") else 0
            if 0 < stop_pct < 0.2:
                cons.append(f"stop {stop_pct:.2f}% away — inside one bar's noise")
            if float(conf.get("fortress") or 0) >= 9:
                cons.append(f"T1 is a {float(conf['fortress']):.1f} wall")
            ev = sgn.get("evidence") or {}
            bull = sgn["direction"] == "BULLISH"
            zones = ctx.get("zones") or []
            clusters = sorted(
                ({"price": z["price"], "strength": z["strength"], "members": z["members"], "wall": z.get("wall", False),
                  "side": "ahead" if (z["price"] > float(sgn["entry"])) == bull else "behind"}
                 for z in zones if abs(z["price"] - float(sgn["entry"])) / float(sgn["entry"]) <= 0.03),
                key=lambda z: z["price"], reverse=not bull,
            )
            und = self.underlyings.get(sgn["symbol"])
            plan = None
            if ps is None and und is not None and (state not in ("IN_TREND", "NO_ROUTE") or key is StrategyKey.FUDKII):
                plan_sig = entry_sig if entry_sig is not None else sgn
                try:
                    plan = await self._plan_preview(key, plan_sig, und)
                except Exception as exc:  # noqa: BLE001 — a preview must never fail the page
                    plan = {"ok": False, "reason": f"preview failed: {exc}"[:120]}
            exit_plan = None
            if ps is not None:
                p_inst = Instrument(**ps["instrument"]) if isinstance(ps.get("instrument"), dict) else None
                if p_inst is not None:
                    exit_plan = self._exit_plan(key, p_inst, int(ps["qty"]), tuple(ps.get("option_targets") or ()), float(ps.get("option_sl") or 0), float(ps.get("equity_sl") or 0), und.segment if und else Segment.NSE_EQ)
            if skip is not None:
                route_label = "SKIP"
            elif route is not None:
                route_label = "COUNTER-TREND" if route.get("route") == "COUNTER" else "IN TREND"
            else:
                route_label = None
            cards.append({
                "atr": ev.get("atr"), "oi": ev.get("oi"), "oiChangePct": ev.get("oi_change_pct"), "clusters": clusters[:8],
                "futLevels": self._fut_levels(route, bull), "plan": plan, "exitPlan": exit_plan, "routeLabel": route_label,
                "signalId": sid, "symbol": sgn["symbol"], "direction": sgn["direction"], "ts": sgn["ts"], "grade": sgn.get("grade"),
                "rr": sgn.get("rr"), "reason": sgn.get("reason"), "entry": sgn["entry"], "stop": sgn["stop"], "targets": sgn.get("targets"),
                "stopPct": round(stop_pct, 2), "confluence": conf, "evidence": sgn.get("evidence") or {}, "gates": sgn.get("gates") or [],
                "parentDecision": sgn.get("decision"), "parentReason": sgn.get("decision_reason"),
                "candle": candle, "surgeT": s_t, "surgeT1": s_t1, "baseline": base, "spark": spark,
                "route": route, "skip": skip, "operator": operator, "fade": fade, "state": state,
                "position": ps, "trade": trades_by_pos.get(ps["id"]) if ps else None, "exits": exits_by_pos.get(ps["id"], []) if ps else [],
                "live": live, "rtCard": alert_cards.get(sid), "pros": pros, "cons": cons,
            })
        counts: dict[str, int] = {}
        for c in cards:
            counts[c["state"]] = counts.get(c["state"], 0) + 1
        return {"book": book, "day": day.isoformat(), "wallet": self.wallets[book].to_json() if book in self.wallets else None, "counts": counts, "cards": cards, "nowTs": time.time()}

    def _exit_plan(self, key: StrategyKey, inst: Instrument, qty: int, ladder: tuple[float, ...], option_sl: float, equity_stop: float, segment: Segment) -> dict[str, Any]:
        """What leaves at which threshold, for this book — the card's "what happens next"."""
        lim = self._exits_by_strategy[key.value].limits if key.value in self._exits_by_strategy else self.limits
        lot = max(1, inst.lot_size)
        rows: list[dict[str, Any]] = []
        rungs = list(ladder[:4])
        if lim.own_ladder:
            for i, r in enumerate(rungs):
                last = i == len(rungs) - 1
                out = max(0, qty - i * lot) if last else min(lot, qty)
                rows.append({"kind": "target", "at": f"T{i + 1} {r:.2f}", "action": "the rest" if last else "1 lot", "qty": out})
        else:
            left = qty
            for i, r in enumerate(rungs):
                share = lim.target_ladder[i] if i < len(lim.target_ladder) else 0.0
                out = min(left, (int(qty * share) // lot) * lot) if i < len(rungs) - 1 else left
                left -= out
                rows.append({"kind": "target", "at": f"T{i + 1} {r:.2f}", "action": f"{share:.0%}", "qty": out})
        if option_sl > 0:
            sus = f", {lim.sustain_s:.0f} s sustain" if lim.sustain_s else ""
            rows.append({"kind": "stop", "at": f"option stop {option_sl:.2f} (equity stop through δ{sus})", "action": "all", "qty": qty})
            if lim.sustain_s:
                rows.append({"kind": "stop", "at": f"hard floor {option_sl * (1 - lim.hard_floor_below_stop_pct / 100):.2f} — {lim.hard_floor_below_stop_pct:.0f}% through the stop, no sustain", "action": "all", "qty": qty})
        if equity_stop > 0:
            rows.append({"kind": "stop", "at": f"underlying {equity_stop:.2f} confirmed", "action": "all", "qty": qty})
        if lim.own_ladder:
            if lim.arm_mode == "immediate":
                trail = f"armed at once by the equity T1 or a 1m close over own R1: peak − {lim.peak_giveback_pct:.0f}% ({lim.trail_dwell_samples} reads)"
            elif lim.arm_min_move:
                trail = f"armed once T1 (≥ {lim.arm_min_move:g}× expected move) is sustained: SL one rung behind, band max({lim.peak_giveback_pct:.0f}%, {lim.giveback_move_frac:g}× expected move), {lim.sustain_s:.0f} s sustain"
            else:
                trail = f"after T1 is sustained ({lim.sustain_s:.0f} s + a 1m close): SL steps to the rung below, line at peak − {lim.peak_giveback_pct:.0f}%, one read through"
        else:
            trail = f"trail arms at +{lim.trail_arm_pct:.0f}%: stop = peak − {lim.trail_giveback_pct:.0f}% of the gain; breakeven after T1"
        rows.append({"kind": "trail", "at": trail, "action": "all remaining", "qty": None})
        rows.append({"kind": "time", "at": "force-flat " + ("23:20" if segment is Segment.MCX_FO else "15:20") + " IST", "action": "all remaining", "qty": None})
        policy = {
            "FUDKII": "legacy: share ladder 40/30/20/10, trail 3%/40%",
            "FUDKII_RT_X": "RT-X: own MTF ladder · touch pays a lot, sustain steps the SL · 3% line",
            "FUDKII_RT_N": "RT-N: daily R1–R4 · immediate arm · 2% line, 3 reads",
            "FUDKII_RT_Y": "RT-Y: arm at 0.5× expected move · SL one rung behind · band in expected-move units",
            "FUDKII_CT_X": "CT-X: the fade under RT-X's exits",
            "FUDKII_CT_Y": "CT-Y: the fade under RT-Y's exits",
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
        ladder, edm, note = (self._own_ladder_for(sig.symbol, inst, sel.premium, lim) if lim.own_ladder else (option_targets, 0.0, "δ-projected"))
        q = self.quotes.get(inst.scrip_code)
        return {
            "ok": sizing.ok, "reason": sizing.reason, "contract": inst.name or inst.scrip_code, "scripCode": inst.scrip_code,
            "strike": inst.strike, "type": inst.option_type.value if inst.option_type else None, "premium": sel.premium,
            "bid": q.bid if q else None, "ask": q.ask if q else None, "spreadPct": round(q.spread_pct * 100, 2) if q and q.spread_pct is not None else None,
            "oi": getattr(q, "oi", None) if q else None, "delta": round(abs(delta), 2), "lots": sizing.lots, "qty": sizing.qty, "outlay": round(sizing.outlay, 0),
            "lotSize": inst.lot_size, "optionSl": option_sl, "ladder": list(ladder), "edm": edm, "ladderNote": note,
            "exitPlan": self._exit_plan(key, inst, sizing.qty, tuple(ladder), option_sl, sig.stop, underlying.segment) if sizing.ok else None,
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
        if key in (StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y):
            und = self.underlyings.get(sig.symbol)
            dec = CounterDecision("COUNTER", "operator take", NO_WALL)
            entry = flipped_signal(sig, key=key, zones=self.zones_for(sig.symbol), atr=atr(self.store.bars(sig.symbol, DECISION_TF, 60), 14) or 0.0,
                                   tick_size=(und.tick_size if und else 0.05) or 0.05, decision=dec, policy=self.fudkii.cfg.grade_policy)
            if entry is None:
                raise RuntimeError("no wall on the flipped side — nothing to aim the fade at")
        else:
            entry = replace(sig, strategy=key, reason=f"operator take · {sig.reason}")
        await self.ledger.event("operator.take", {"book": book, "signal_id": signal_id, "entry_signal_id": entry.signal_id, "symbol": sig.symbol})
        log.info("operator.take", book=book, symbol=sig.symbol, signal=signal_id)
        await self._handle_signal(entry, None)
        pos = next((p for p in self.positions.values() if p.strategy == book and p.signal_id == entry.signal_id), None)
        return {"book": book, "signalId": signal_id, "entered": pos is not None, "position": _position_json(pos) if pos else None}

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
        legs = await self._counter_legs(underlying, bar)
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
            tick_size=underlying.tick_size or 0.05, decision=dec, policy=self.fudkii.cfg.grade_policy,
        )
        if fade is None:
            log.info("counter.no_plan", symbol=sig.symbol, reason="no wall on the flipped side")
            await self.ledger.insert_signal(sig.to_json(), "COUNTER_NO_PLAN", dec.reason)
            return
        await self._handle_signal(fade, bar)

    async def _counter_legs(self, underlying: Instrument, bar: UnifiedBar) -> list[Leg]:
        """The equity's side of the trigger from the store, the front future's from the broker
        (``_fut_context``): candle, ATR30m, classic levels with their weights, volume surges."""
        from .instrument.legs import levels_from_candles, weekly_from_rows
        from .market.session import to_ist

        eq_bars = self.store.bars(underlying.symbol, DECISION_TF, 60)
        s_t, s_t1, _ = volume_surges([b.volume for b in eq_bars], window=6, floor=1000.0)
        legs = [Leg(
            "equity", bar.open, bar.high, bar.low, bar.close, atr(eq_bars, 14) or 0.0,
            self._pivot_points(underlying.symbol), s_t, s_t1,
        )]
        ctx = await self._fut_context(underlying)
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
        f_t, f_t1, _ = volume_surges([float(r["v"]) for r in rows], window=6, floor=1000.0)
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

    async def _fut_context(self, underlying: Instrument) -> dict[str, Any] | None:
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
        today = ist_today()
        start30 = self.calendar.previous_trading_day(self.calendar.previous_trading_day(today))
        try:
            async with self._fut_sem:
                rows30 = await self.rest.candles(front, DECISION_TF, start30.isoformat(), today.isoformat())
                rows1d = await self.rest.candles(front, "1d", (today - timedelta(days=35)).isoformat(), today.isoformat())
        except Exception as exc:  # noqa: BLE001 — a route input must never fail the fill path
            log.warning("fut.context_unknown", symbol=underlying.symbol, error=str(exc)[:120])
            return None
        ctx = {"front": front, "bars30": rows30, "rows1d": rows1d}
        self._fut_cache[underlying.symbol] = (bucket, ctx)
        return ctx

    def _pivot_points(self, symbol: str) -> list[PivotPoint]:
        """The equity's classic levels for today — daily from the previous session, weekly and
        monthly from the previous completed periods — with their timeframe weights."""
        today = ist_today()
        dailies = self.store.bars(symbol, "1d")
        prev = previous_session(dailies, today)
        if len(dailies) < MIN_DAILY_BARS or prev is None or not is_official(prev):
            return []
        points: list[PivotPoint] = []
        lv = classic_pivots(prev.high, prev.low, prev.close)
        if lv:
            points += pivot_points(lv, "1d")
        for tf, periods in (("1wk", weekly(list(dailies))), ("1mo", monthly(list(dailies)))):
            p = previous_complete(periods, today)
            if p:
                lv = classic_pivots(p.high, p.low, p.close)
                if lv:
                    points += pivot_points(lv, tf)
        return points

    async def _volume_surges(self, underlying: Instrument) -> dict[str, tuple[float, float]]:
        """``surge_T`` / ``surge_T-1`` of the last two closed 30m bars against the T-2…T-7 baseline
        (``volume_surges``, floor 1000) — for the underlying from the store, and for its front
        future from the broker's candles, since the engine holds no futures bars. A leg the data
        cannot answer for is left out: absent, not dried. Read at twin time only, so the parent's
        fill path never waits on it."""
        from .market.session import to_ist

        out: dict[str, tuple[float, float]] = {}
        eq = self.store.bars(underlying.symbol, DECISION_TF, 12)
        s_t, s_t1, _ = volume_surges([b.volume for b in eq], window=6, floor=1000.0)
        if s_t is not None and s_t1 is not None:
            out["equity"] = (s_t, s_t1)
        if underlying.segment is not Segment.NSE_EQ or not eq:
            return out
        ctx = await self._fut_context(underlying)
        if ctx is None:
            return out
        trigger = to_ist(eq[-1].ts).strftime("%Y-%m-%dT%H:%M")
        # bars up to and including the trigger bar; the partial bar after it is not a reading
        vols = [float(r["v"]) for r in ctx["bars30"] if str(r["dt"])[:16] <= trigger]
        f_t, f_t1, _ = volume_surges(vols, window=6, floor=1000.0)
        if f_t is not None and f_t1 is not None:
            out["future"] = (f_t, f_t1)
        return out

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
        FUDKII family."""
        return replace(SELECTION_POLICY, min_premium=0.0) if key in NO_PREMIUM_FLOOR else SELECTION_POLICY

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
            front = cat.front_future(sig.symbol)
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
        await self._ensure_quotes(chain, sig.entry)
        if tape:
            # the strikes the selector is choosing between go on tape, chosen or not
            near = sorted(chain, key=lambda i: abs(i.strike - sig.entry))[:6]
            self.tape.follow(sig.symbol, [i.scrip_code for i in near], now=now)
        return select_option(
            chain=chain,
            quotes=self.quotes,
            spot=sig.entry,
            target1=sig.targets[0] if sig.targets else None,
            direction=sig.direction,
            now=now,
            policy=pol,
        )

    async def _ensure_quotes(self, chain: list[Instrument], spot: float) -> None:
        """Snapshot-quote the handful of strikes near the money.

        One batched REST call, not an inline per-strike fetch. The old enricher took 3–23 seconds
        on a cache miss and *blocked signal publication* while it did.
        """
        if not chain:
            return
        near = sorted(chain, key=lambda i: abs(i.strike - spot))[:12]
        stale = [i for i in near if (q := self.quotes.get(i.scrip_code)) is None or time.time() - q.ts > 20]
        if not stale:
            return
        try:
            rows = await self.rest.market_feed(stale)
        except Exception as exc:  # noqa: BLE001
            log.warning("quotes.failed", n=len(stale), error=str(exc))
            return
        for code, r in rows.items():
            self.quotes[code] = Quote(ltp=r["ltp"], bid=r["bid"], ask=r["ask"], ts=r["ts"])
            if r["ltp"] > 0:
                self.ltps[code] = r["ltp"]
        await self.feed.subscribe("mf", stale)

    async def _submit(self, intent: OrderIntent, *, verdict_ok: bool, verdict_reason: str) -> Any:
        mode = self.mode()
        if mode in (Mode.LIVE, Mode.LIVE_CAPPED):
            wallet = self.wallets[intent.strategy]
            ctx = LiveContext(
                balance=wallet.balance,
                open_positions=len([p for p in self.positions.values() if p.status == "OPEN"]),
                day_pnl_inr=wallet.day_pnl,
                now_hm_ist=ist_hm(time.time()),
                segment=intent.instrument.segment.value,
                exposure_ok=verdict_ok,
                exposure_reason=verdict_reason,
            )
            return await self.gateway.submit_live(intent, ctx=ctx)
        return self.gateway.submit(intent)

    # -- exit path -----------------------------------------------------------------------------------

    async def _manage_positions(self) -> None:
        now = time.time()
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
            halted, _ = self.halted()
            forced = past_force_flat(pos.underlying.segment, now) or halted

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
                        f"⚠️ {pos.strategy} {pos.underlying.symbol}: no quote for "
                        f"{age:.0f}s — the stop is not being evaluated",
                        key=f"stale:{pos.id}",
                    )
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
                past_force_flat=past_force_flat(pos.underlying.segment, now),
                halted=halted,
                daily_loss_hit=wallet.halted and wallet.halt_reason.startswith("DAILY_LOSS"),
            )
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
            engine_for = self._exits_by_strategy.get(pos.strategy, self.exits)
            decision = engine_for.evaluate(pos, view)
            if decision is None:
                continue
            await self._exit(pos, decision, now)

    async def _exit(self, pos: Position, decision: Any, now: float) -> None:
        intent = OrderIntent(
            strategy=pos.strategy,
            instrument=pos.instrument,
            side=OrderSide.SELL,
            qty=decision.qty,
            purpose=Purpose.EXIT,
            signal_id=pos.signal_id,
            client_order_id=exit_client_order_id(pos, decision),
            reason=decision.note,
            position_id=pos.id,
            ref_price=decision.ref_price,
        )
        result = await self._submit(intent, verdict_ok=True, verdict_reason="")
        await self.ledger.insert_order(_order_json(result.order), result.decision.value)
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
            log.error(
                "exit.failed",
                position=pos.id,
                symbol=pos.underlying.symbol,
                reason=result.order.note,
            )
            self.telegram.fire_and_forget(
                f"⚠️ EXIT FAILED {pos.strategy} {pos.underlying.symbol}: {result.order.note}",
                key=f"exitfail:{pos.id}",
            )
            return

        entry_before = pos.qty_remaining
        gross = apply_exit(
            pos, decision, fill_price=result.fill.price, charges=result.fill.charges, now=now
        )
        wallet = self.wallets[pos.strategy]
        wallet.release(pos.entry * min(decision.qty, entry_before) * pos.instrument.multiplier, now)
        wallet.apply_charges(result.fill.charges, now)
        wallet.apply_close(gross, now)
        tripped = wallet.check_breakers(self.limits, now)
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
            except Exception as exc:
                log.exception("clock.failed", error=str(exc))
            await asyncio.sleep(1.0)

    async def _housekeeping(self) -> None:
        last_reconcile = 0.0
        last_snapshot = 0.0
        last_archive = time.time()
        last_day = ist_day(time.time()).isoformat()
        while not self._stop.is_set():
            now = time.time()
            try:
                day = ist_day(now).isoformat()
                if now - last_archive > self.s.archive_flush_s:
                    last_archive = now
                    await asyncio.to_thread(self.archive.flush)

                # The nightly research loop, once per day after its hour, only with every segment
                # closed — it runs six backtests in a worker thread and spends Claude calls.
                if (
                    self.committee.autopilot_due(now, day, self._autopilot_day)
                    and not self.market_open_now()
                ):
                    self._autopilot_day = day
                    self._decision_tasks.add(asyncio.create_task(self._autopilot()))
                if day != last_day:
                    last_day = day
                    self._alerts_reset_done.clear()
                    self._zone_cache.clear()
                    self._daily_due = self._legs_due = True
                    self._daily_refresh_done.clear()
                    for w in self.wallets.values():
                        w.rollover(now)
                    if self.s.has_credentials and self.s.engine_enabled:
                        await self.catalogue_loader.ensure()

                if self.reconciler_positions is not None and now - last_reconcile > 60:
                    last_reconcile = now
                    if self.mode() in (Mode.LIVE, Mode.LIVE_CAPPED):
                        await self.reconciler_positions.run(
                list(self.positions.values()),
                at_venue=self.mode() in (Mode.LIVE, Mode.LIVE_CAPPED),
            )

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

                # scripFinder's 09:20 IST intraday rebuild: refetch the master, re-pick strikes,
                # subscribe anything new. Strikes listed 09:00–09:15 are not in an overnight master.
                if self.reconciler_ready and ist_hm(now) >= "09:20" and self._intraday_rebuild_day != day:
                    self._intraday_rebuild_day = day
                    self._decision_tasks.add(asyncio.create_task(self._intraday_universe_rebuild()))

                # Each name's own VIX, once a minute, from the quotes already in hand.
                if self.reconciler_ready and self.groups and now - self._last_iv_refresh >= 60:
                    self._last_iv_refresh = now
                    self._refresh_stock_iv()
                # The pivot data plane (docs/PIVOTS.md §3): refresh slots, then the periodic audit.
                hm = ist_hm(now)
                # The alert page is emptied for the coming session, every book and twin together.
                stamp = f"{day} {self.s.alerts_reset_ist}"
                if hm >= self.s.alerts_reset_ist and stamp not in self._alerts_reset_done:
                    self._alerts_reset_done.add(stamp)
                    self.alerts.reset_day(day)
                    self._signals_today.clear()
                    self._fut_cache.clear()
                for slot in self.s.daily_refresh_hm:
                    stamp = f"{day} {slot}"
                    if hm >= slot and stamp not in self._daily_refresh_done:
                        self._daily_refresh_done.add(stamp)
                        self._daily_due = True
                if (
                    self.reconciler_ready
                    and self.underlyings
                    and (self._pivot_repair_task is None or self._pivot_repair_task.done())
                    and (
                        self._daily_due
                        or self._legs_due
                        or now - self._last_pivot_repair > self.s.pivot_repair_interval_s
                    )
                ):
                    self._last_pivot_repair = now
                    self._pivot_repair_task = asyncio.create_task(self._pivot_repair())
                if now - last_snapshot > 300:
                    last_snapshot = now
                    await self._persist_wallets()
                    await self.ledger.insert_health(self.health_snapshot())
                    await asyncio.to_thread(self.iv_history.save_all)
            except Exception as exc:
                log.exception("housekeeping.failed", error=str(exc))
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
            Check(
                "feed_connected",
                (fh.connected or not self.s.feed_enabled) if open_now else True,
                detail=fh.last_error if open_now else "market closed",
            ),
            Check(
                "feed_fresh",
                (fh.silence_s is None or fh.silence_s < 120) if open_now else True,
                value=fh.silence_s,
                detail="no message for over 2 minutes" if open_now else "market closed",
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
            Check("breaker", not self.gateway.breaker_tripped),
            Check(
                "bars_warm",
                sum(1 for sym in self.underlyings if self.store.count(sym, DECISION_TF) >= 21)
                >= max(1, len(self.underlyings) // 2)
                if self.underlyings
                else True,
                detail="fewer than half the universe has 21+ decision bars",
            ),
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
                "messages": fh.messages,
                "ticks": fh.ticks,
                "depth": fh.depth,
                "oi": fh.oi,
                "reconnects": fh.reconnects,
                "silence_s": fh.silence_s,
                "subscriptions": fh.subscriptions,
            },
            "bars": self.aggregator.stats() | self.store.stats(),
            "gateway": self.gateway.stats(),
            "rest": self.rest.stats(),
            "catalogue": self.catalogue_loader.catalogue.stats(),
            "universe": self.universe_builder.summary(self.groups) if self.universe_builder else {},
            "fidelity": self.reconciler.snapshot() if self.reconciler_ready else {},
            "micro": self.micro.stats(),
            "option_oi_tracked": len(self.option_oi),
            "archive": self.archive.stats(),
            "tape": self.tape.stats(),
            "telegram": self.telegram.stats(),
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


def _order_json(o: Any) -> dict[str, Any]:
    return {
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


def exit_client_order_id(pos: Position, decision: Any) -> str:
    """Idempotency key for an exit: the same position, rung and reason must always produce the
    same id (a retry is the same order), and two positions must never share one. It was keyed on
    the signal — and the RT twin carries its parent's signal id, so on 2026-09-23 14:35 the twin's
    SL-EQ exit collided with the parent's and was refused as a duplicate, every second, 800 times,
    while the underlying sat through the stop."""
    return f"{pos.id}|EXIT|{pos.targets_hit}|{decision.reason.value}"


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
        "line_breach_since": p.line_breach_since,
    }


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
        line_breach_since=d.get("line_breach_since"),
    )


def _trade_from(p: Position, now: float) -> Trade:
    exit_price = p.exit_price or 0.0
    gross = (exit_price - p.entry) * p.dir_sign * p.qty * p.instrument.multiplier
    net = gross - p.charges
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
        charges=round(p.charges, 2),
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
