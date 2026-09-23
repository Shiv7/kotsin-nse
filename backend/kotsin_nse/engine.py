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
from datetime import date, timedelta
from typing import Any

import httpx
import structlog

from .alerts.engine import AlertEngine
from .bars.aggregator import Aggregator
from .bars.daily import MIN_DAILY_BARS, REPAIR_BATCH, DailyCache, is_official, previous_session
from .bars.daily import audit as audit_daily
from .bars.indicators import atr
from .bars.micro import MicroAggregator
from .bars.periods import monthly, previous_complete, weekly
from .bars.pivots import (
    ZONE_TOLERANCE_PCT,
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
    ladder_tolerance_pct,
    merge_points,
    regime_for_name,
    seed_points,
    years_to_expiry,
)
from .market.session import (
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
from .ops.telegram import Telegram
from .risk.costs import CostModel
from .risk.exits import ExitEngine, MarketView, apply_exit
from .risk.exposure import ExposureBook
from .risk.limits import RT_X_LIMITS, RiskLimits
from .risk.sizing import size_position
from .risk.wallet import Wallet
from .strategy.base import Outcome, Signal
from .strategy.fudkii import Fudkii, FudkiiConfig
from .strategy.fukaa import Fukaa, FukaaConfig, select
from .strategy.keys import ALL_KEYS, INITIAL_INR, StrategyKey
from .venue.fivepaisa.auth import Authenticator
from .venue.fivepaisa.rest import FivePaisaREST
from .venue.fivepaisa.ws import FivePaisaFeed

log = structlog.get_logger(__name__)

DECISION_TF = "30m"
SELECTION_POLICY = SelectionPolicy()


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
        self._exits_by_strategy = {
            StrategyKey.FUDKII_RT_X.value: self.exits_rt,
            StrategyKey.FUDKII_RT_MCX.value: self.exits_rt,
        }
        #: The twin is checked against its own pool — 30 slots, its own lot cap — rather than
        #: skipping the check entirely, which is what it did when first written.
        self.exposure_rt = ExposureBook(RT_X_LIMITS)
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
        self.archive = DailyArchive(settings.data_dir / "archive", enabled=settings.archive_enabled)
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
        # -- the pivot data plane (docs/PIVOTS.md) --
        self.daily_cache = DailyCache(settings.data_dir / "daily")
        self._daily_failed: set[str] = set()  # 1d fetch raised; the repair loop retries every pass
        self._daily_confirmed: dict[str, date] = {}  # asked once for this expected session already
        self._daily_due = False  # a full refetch is owed: day roll or a refresh slot
        self._legs_due = False  # a full leg reload is owed: day roll
        self._daily_refresh_done: set[str] = set()  # "YYYY-MM-DD HH:MM" slots already run
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
        points = []
        prev_day = [b for b in dailies if ist_day(b.ts) < today]
        if prev_day:
            d = prev_day[-1]
            lv = classic_pivots(d.high, d.low, d.close)
            if lv:
                points += pivot_points(lv, "1d")
        for tf, periods in (("1wk", weekly(list(dailies))), ("1mo", monthly(list(dailies)))):
            p = previous_complete(periods, today)
            if p:
                lv = classic_pivots(p.high, p.low, p.close)
                if lv:
                    points += pivot_points(lv, tf)
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

    async def _handle_signal(self, sig: Signal, bar: UnifiedBar) -> None:
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

        wallet = self.wallets[sig.strategy.value]
        if wallet.halted:
            await self.ledger.insert_signal(sig.to_json(), "WALLET_HALTED", wallet.halt_reason)
            return

        sizing = size_position(
            instrument=inst,
            premium=selection.premium,
            option_stop=option_sl,
            option_target1=option_targets[0] if option_targets else None,
            balance=wallet.balance,
            available=wallet.available,
            limits=self.limits,
            costs=self.costs,
        )
        if not sizing.ok:
            await self.ledger.insert_signal(sig.to_json(), "NOT_SIZED", sizing.reason)
            log.info("signal.not_sized", symbol=sig.symbol, reason=sizing.reason)
            return

        verdict = self.exposure.check(
            strategy=sig.strategy.value,
            underlying=sig.symbol,
            outlay=sizing.outlay,
            positions=list(self.positions.values()),
            total_capital=sum(w.balance for w in self.wallets.values()),
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
        if pos.strategy != StrategyKey.FUDKII.value:
            return
        # Commodities and equities keep separate purses: one CRUDEOIL lot is a different size of
        # bet from one BLUESTARCO lot, and a shared wallet would let whichever fired first decide
        # what the other could afford.
        twin_key = (
            StrategyKey.FUDKII_RT_MCX
            if pos.underlying.segment is Segment.MCX_FO
            else StrategyKey.FUDKII_RT_X
        )
        twin_wallet = self.wallets.get(twin_key.value)
        if twin_wallet is None or twin_wallet.halted:
            return
        cost = pos.entry * pos.qty * inst.multiplier
        if twin_wallet.available < cost:
            log.info("rt_twin.skipped", symbol=pos.underlying.symbol, reason="wallet")
            return
        verdict = self.exposure_rt.check(
            strategy=twin_key.value,
            underlying=pos.underlying.symbol,
            outlay=cost,
            positions=list(self.positions.values()),
            total_capital=sum(w.balance for w in self.wallets.values()),
        )
        if not verdict.allowed:
            log.info("rt_twin.skipped", symbol=pos.underlying.symbol, reason=verdict.reason)
            return
        twin = replace(
            pos,
            id=new_id("pos"),
            strategy=twin_key.value,
            note=f"{pos.note} · RT exit policy, twin of {pos.id}",
        )
        if self.exits_rt.limits.own_ladder:
            # Nothing delta-projected: the contract's own classic R1–R4 from its previous session
            # (LegPivotLoader, thin-bar and zero-range guarded). No ladder → the equity trigger only.
            own = self.leg_pivots.for_code(inst.scrip_code)
            tol, reg = self.option_ladder_tolerance(pos.underlying.symbol, inst.strike, inst.option_type, twin.entry)
            rungs = own.rungs_above(twin.entry, tolerance_pct=tol) if own is not None else []
            twin.option_targets = tuple(r["price"] for r in rungs[:4])
            twin.option_t1 = twin.option_targets[0] if twin.option_targets else 0.0
            twin.targets_hit = 0
            twin.ratchet_sl = 0.0
            twin.armed_by = ""
            twin.note += (
                f" · own classic ladder, tol {tol:.1f}% (k {reg.k:.2f} {reg.band.value}, {reg.source})"
                if twin.option_targets
                else " · no own ladder, equity trigger only"
            )
        self.positions[twin.id] = twin
        twin_wallet.reserve(cost, result.fill.ts)
        twin_wallet.apply_charges(result.fill.charges, result.fill.ts)
        await self.ledger.upsert_position(_position_json(twin))
        await self.ledger.upsert_wallet(twin_wallet.strategy, twin_wallet.to_json())
        log.info(
            "rt_twin.open",
            twin=twin.id,
            of=pos.id,
            symbol=pos.underlying.symbol,
            qty=twin.qty,
            entry=twin.entry,
        )
        self.alerts.mark_entered(pos.signal_id, ts=result.fill.ts, price=twin.entry, qty=twin.qty)

    async def _select_instrument(self, underlying: Instrument, sig: Signal) -> Any:
        cat = self.catalogue_loader.catalogue
        now = time.time()
        if underlying.segment is Segment.MCX_FO:
            front = cat.front_future(sig.symbol)
            q = self.quotes.get(front.scrip_code) if front else None
            return select_future(front=front, quote=q, now=now)
        expiry = choose_expiry(cat.expiries(sig.symbol), ist_today(), SELECTION_POLICY)
        if expiry is None:
            return select_option(
                chain=[], quotes={}, spot=sig.entry, target1=None, direction=sig.direction, now=now
            )
        chain = cat.chain(sig.symbol, expiry, sig.direction.option_type)
        await self._ensure_quotes(chain, sig.entry)
        return select_option(
            chain=chain,
            quotes=self.quotes,
            spot=sig.entry,
            target1=sig.targets[0] if sig.targets else None,
            direction=sig.direction,
            now=now,
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

    async def _clock(self) -> None:
        while not self._stop.is_set():
            try:
                await self.aggregator.flush_stale()
                await self._manage_positions()
                # Same tick as the exit evaluation, deliberately: the card must show the
                # numbers the stop is being judged against, not a second computation of them.
                self.alerts.refresh_live()
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
