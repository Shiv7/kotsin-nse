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
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
import structlog

from .bars.aggregator import Aggregator
from .bars.micro import MicroAggregator
from .bars.periods import monthly, previous_complete, weekly
from .bars.pivots import Zone, classic_pivots, cluster_zones, pivot_points
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
from .market.session import (
    TF_SECONDS,
    TradingCalendar,
    bucket_start,
    is_open,
    ist_day,
    ist_hm,
    ist_naive_to_ts,
    past_force_flat,
)
from .market.session import (
    session_phase as session_phase_of,
)
from .ops.health import Check, HealthMonitor
from .ops.telegram import Telegram
from .risk.costs import CostModel
from .risk.exits import ExitEngine, MarketView, apply_exit
from .risk.exposure import ExposureBook
from .risk.limits import RiskLimits
from .risk.sizing import size_position
from .risk.wallet import Wallet
from .strategy.base import Outcome, Signal
from .strategy.fudkii import Fudkii, FudkiiConfig
from .strategy.fukaa import Fukaa, FukaaConfig, select
from .strategy.keys import ALL_KEYS, StrategyKey
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
        self.exposure = ExposureBook(self.limits)
        self.calendar = TradingCalendar.from_file(settings.data_dir / "holidays.txt")
        self.telegram = Telegram(
            settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else None,
            settings.telegram_chat_id,
        )
        self.health = HealthMonitor()
        self.committee = CommitteeService(self, settings, decision_tf=DECISION_TF)

        self.http = httpx.AsyncClient(timeout=30)
        self.auth = Authenticator(settings, self.http)
        self.rest = FivePaisaREST(settings, self.http, self.auth)
        self.catalogue_loader = CatalogueLoader(settings, self.rest.scrip_master_csv)
        self.feed = FivePaisaFeed(
            settings,
            self.auth,
            on_tick=self._on_tick,
            on_depth=self._on_depth,
            on_oi=self._on_oi,
        )
        self.aggregator = Aggregator(self.store, on_bar_close=self._on_bar_close)
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
            self.universe_builder.build_underlyings(self.s.segment_list, date.today())
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
        await self._backfill(universe)

        # Pass 2 — which strikes, now that the previous close is known from the daily backfill.
        n_opts = self.universe_builder.select_all(groups, self._prev_close, date.today())
        for g in groups.values():
            for o in g.options:
                self._segment_by_code[o.scrip_code] = o.segment
        self._future_to_underlying = {f.scrip_code: g.root for g in groups.values() for f in g.futures}

        # Futures are OI sources, not bar sources: on NSE the front future's `symbol` is the cash
        # symbol, and tracking it wrote futures ticks into the equity's bars (found 2026-09-21).
        # `subscriptions()` puts them on mf+oi only; the aggregator ignores untracked codes.
        subs = UniverseBuilder.subscriptions(groups.values())
        await self.feed.subscribe("mf", subs["mf"])
        await self.feed.subscribe("md", subs["md"])
        await self.feed.subscribe("oi", subs["oi"])
        log.info(
            "engine.subscribed",
            mf=len(subs["mf"]), md=len(subs["md"]), oi=len(subs["oi"]),
            options=n_opts, underlyings=len(universe),
        )
        if self.reconciler_positions is not None:
            await self.reconciler_positions.run(list(self.positions.values()))

    def _prev_close(self, symbol: str) -> float | None:
        bars = self.store.bars(symbol, "1d")
        prior = [b for b in bars if ist_day(b.ts) < date.today()]
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
        end = date.today()
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
                    log.warning("backfill.failed", symbol=inst.symbol, tf=tf, error=str(exc))
                    continue
                if not rows:
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
            )
            for r in rows
        ]
        self.store.seed(inst.symbol, "1d", bars)

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
        """Daily + weekly + monthly pivot zones, cached per calendar day.

        Computed from **completed** periods only, so the levels are fixed for the session. Cached
        because clustering 30-odd levels for 200 symbols on every bar would be the single hottest
        thing in the process for no benefit.
        """
        today = date.today()
        key = today.isoformat()
        hit = self._zone_cache.get(symbol)
        if hit and hit[0] == key:
            return hit[1]
        dailies = self.store.bars(symbol, "1d")
        if len(dailies) < 25:
            self._zone_cache[symbol] = (key, [])
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
        zones = cluster_zones(points)
        self._zone_cache[symbol] = (key, zones)
        return zones

    def session_phase(self, symbol: str, ts: int) -> str:
        inst = self.underlyings.get(symbol)
        return session_phase_of(inst.segment if inst else Segment.NSE_EQ, ts)

    # -- decision path -----------------------------------------------------------------------------

    async def _on_bar_close(self, bar: UnifiedBar) -> None:
        await self.bus.publish(Topic.BAR, bar)
        if bar.tf != DECISION_TF:
            return
        for pos in self.positions.values():
            if pos.status == "OPEN" and pos.underlying.symbol == bar.symbol:
                pos.bars_held += 1
        micro = self.micro.for_bar(bar.scrip_code, bar.ts)
        if micro:
            bar.extra["micro"] = micro
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
            self.universe_builder.select_all(self.groups, self._prev_close, date.today())
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

    async def _select_instrument(self, underlying: Instrument, sig: Signal) -> Any:
        cat = self.catalogue_loader.catalogue
        now = time.time()
        if underlying.segment is Segment.MCX_FO:
            front = cat.front_future(sig.symbol)
            q = self.quotes.get(front.scrip_code) if front else None
            return select_future(front=front, quote=q, now=now)
        expiry = choose_expiry(cat.expiries(sig.symbol), date.today(), SELECTION_POLICY)
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
            view = MarketView(
                option_ltp=ltp,
                underlying_ltp=self.ltps.get(pos.underlying.scrip_code),
                now=now,
                bars_held=pos.bars_held,
                past_force_flat=past_force_flat(pos.underlying.segment, now),
                halted=halted,
                daily_loss_hit=wallet.halted and wallet.halt_reason.startswith("DAILY_LOSS"),
            )
            decision = self.exits.evaluate(pos, view)
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
            client_order_id=f"{pos.signal_id}|EXIT|{pos.targets_hit}|{decision.reason.value}",
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
            except Exception as exc:
                log.exception("clock.failed", error=str(exc))
            await asyncio.sleep(1.0)

    async def _housekeeping(self) -> None:
        last_reconcile = 0.0
        last_snapshot = 0.0
        last_day = ist_day(time.time()).isoformat()
        while not self._stop.is_set():
            now = time.time()
            try:
                day = ist_day(now).isoformat()
                if day != last_day:
                    last_day = day
                    self._zone_cache.clear()
                    for w in self.wallets.values():
                        w.rollover(now)
                    if self.s.has_credentials and self.s.engine_enabled:
                        await self.catalogue_loader.ensure()

                if self.reconciler_positions is not None and now - last_reconcile > 60:
                    last_reconcile = now
                    if self.mode() in (Mode.LIVE, Mode.LIVE_CAPPED):
                        await self.reconciler_positions.run(list(self.positions.values()))

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

                if now - last_snapshot > 300:
                    last_snapshot = now
                    await self._persist_wallets()
                    await self.ledger.insert_health(self.health_snapshot())
            except Exception as exc:
                log.exception("housekeeping.failed", error=str(exc))
            await asyncio.sleep(5.0)

    # -- state -----------------------------------------------------------------------------------------

    async def _load_wallets(self) -> None:
        stored = await self.ledger.load_wallets()
        for key in ALL_KEYS:
            data = stored.get(key.value)
            self.wallets[key.value] = (
                Wallet.from_json(data) if data else Wallet.new(key.value, self.s.paper_initial_inr)
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
