"""The engine: one asyncio process wiring feed → bars → gateway → ledger → API. No strategy.

Everything the old stack spread across five JVMs, Kafka, Redis and Mongo happens here, in order,
in one place. That is not minimalism for its own sake — the failure modes that cost the most time
in the old system were all *between* the hops.

Task layout:

* ``_feed`` — the broker socket, reconnecting on its own budget;
* ``_clock`` — 1 Hz: closes bars whose bucket ended;
* ``_housekeeping`` — archive flushes, health, day rollover, scrip-master refresh, REST bar sweeps.

This is the base: market data in, bars out, orders plumbed but never sent. A strategy plugs in at
``_on_bar_close`` (a closed bar, checked against the exchange's own candle) and submits through
``self.gateway``.

Mode lives in the control table and is read on every submit. LIVE requires an ``armed_until``: an
engine that restarts after expiry comes up in PAPER and says so.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import httpx
import structlog

from .bars.aggregator import Aggregator
from .bars.store import BarStore
from .bars.unified import BarSource, UnifiedBar
from .bars.verify import BarReconciler
from .bus import Bus, Topic
from .config import Segment, Settings
from .domain import Instrument, InstrumentKind
from .exec.gateway import Gateway, LiveCaps, Mode
from .exec.live import LiveExecutor
from .exec.paper import BookSnapshot, PaperMatcher
from .feed import build_source
from .feed.base import Channel, DepthMsg, OIMsg, TickMsg
from .feed.rate import FeedRateMeter
from .fno.chain import YEAR_S, OptQuote, StrikeRow, leg_of, read_chain
from .fno.positioning import WEAK_OI_PCT, Leg
from .fno.positioning import read as read_positioning
from .instrument.catalogue import CatalogueLoader
from .instrument.focus import focus_roots
from .instrument.quote import Quote
from .instrument.universe import INDEX_ROOTS, ScripGroup, UniverseBuilder, UniversePolicy
from .ledger.db import Ledger
from .market.fo_bhavcopy import OiDailyStore
from .market.fo_bhavcopy import fetch_day as fetch_fo_bhavcopy
from .market.fo_universe import SYMBOL_CHANGES_FILE, parse_symbol_changes
from .market.session import (
    TradingCalendar,
    is_open,
    ist_day,
    ist_hm,
    ist_naive_to_ts,
    session_close_ts,
    session_open_ts,
)
from .market.session import (
    session_phase as session_phase_of,
)
from .ops.archive import DailyArchive
from .ops.health import Check, HealthMonitor
from .ops.telegram import Telegram
from .orderflow.meter import FlowMeter
from .orderflow.micro import MicroMeter
from .risk.costs import CostModel
from .scan.board import OIRead, attach_setups
from .scan.live import LiveScanner
from .structure.service import from_store as structure_from_store
from .venue.fivepaisa.auth import Authenticator
from .venue.fivepaisa.rest import FivePaisaREST

log = structlog.get_logger(__name__)

#: the frame backfilled at boot and checked against the exchange on every close
PRIMARY_TF = "30m"
#: a root whose strikes were just re-centred waits this long before the next (a churning price
#: would otherwise re-subscribe its strikes every half minute)
RECENTER_MIN_S = 120.0


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.bus = Bus()
        self.store = BarStore()
        self.ledger = Ledger(settings.db_url)
        self.costs = CostModel(settings)
        self.calendar = TradingCalendar.from_file(
            settings.data_dir / "holidays.txt", settings.data_dir / "holidays-mcx.txt"
        )
        self.telegram = Telegram(
            settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else None,
            settings.telegram_chat_id,
        )
        self.health = HealthMonitor()
        self.archive = DailyArchive(
            settings.data_dir / "archive", enabled=settings.archive_enabled, depth_levels=settings.archive_depth_levels
        )

        self.http = httpx.AsyncClient(timeout=30)
        self.auth = Authenticator(settings, self.http)
        self.rest = FivePaisaREST(settings, self.http, self.auth)
        self.catalogue_loader = CatalogueLoader(settings, self.rest.scrip_master_csv)
        self.feed = build_source(
            settings,
            self.auth,
            on_tick=self._on_tick,
            on_depth=self._on_depth,
            on_oi=self._on_oi,
        )
        self.aggregator = Aggregator(self.store, on_bar_close=self._on_bar_close)
        self._segment_by_code: dict[str, Segment] = {}
        #: every subscribed contract by key — what the rate meter groups by
        self._inst_by_code: dict[str, Instrument] = {}
        #: quote frames worth archiving raw: the underlyings and their futures, never options
        self._tick_archive_codes: set[str] = set()
        #: option strikes: their quote frames are archived (intraday premiums) but get no order flow
        self._option_tick_codes: set[str] = set()
        #: the focus list's roots (``instrument/focus.py``) and the option strikes whose books are kept
        self.focus: list[str] = []
        self._focus_option_codes: set[str] = set()
        self._recentered_at: dict[str, float] = {}
        self._recenter_checked = 0.0
        self.feed_rate = FeedRateMeter()
        #: estimated order flow per minute for the underlyings and their front futures
        self.flow = FlowMeter()
        self.micro = MicroMeter()  # microstructure of the underlyings and front futures (orderflow/micro.py)
        self._depth_last: dict[str, float] = {}
        self._open_cache: dict[tuple[Segment, date], float] = {}
        self.scanner = LiveScanner(self.store)
        self._scanned_minute = 0
        #: near-future OI change % by underlying symbol, for the board's build-up label
        self.fut_oi_change: dict[str, float] = {}
        #: NSE's official closing OI (F&O bhavcopy) — the reference an OI change is measured from,
        #: because 5paisa's own change field is 0.0 on every frame
        self.oi_daily = OiDailyStore(settings.data_dir / "oi_daily")
        self.oi_ref: dict[str, float] = {}
        self.oi_ref_day: date | None = None
        self._front_future: dict[str, str] = {}  # front future code → underlying symbol
        self._oi_ref_checked = 0.0
        #: MCX has no bhavcopy here and 5paisa's change field is 0.0 there too: the reference is
        #: our own archive's closing OI of the previous MCX session, else the first OI seen today
        self.mcx_oi_ref: dict[str, float] = {}
        self._mcx_oi_ref_day: str | None = None
        #: underlying → when its OI reference was taken, where it is not the previous close
        self.oi_ref_since: dict[str, float] = {}
        #: contract → when its OI reference was taken, where it is not the previous close
        self.oi_ref_ts: dict[str, float] = {}
        #: contract → its latest OI (futures and options alike)
        self.oi_live: dict[str, float] = {}
        #: contract → the exchange's previous close, as every quote frame carries it (PClose)
        self.prev_close_by_code: dict[str, float] = {}
        self.reconciler = BarReconciler(self.rest, self.store, lambda sym: self.underlyings.get(sym))
        self.groups: dict[str, ScripGroup] = {}
        self.universe_builder: UniverseBuilder | None = None
        self.option_oi: dict[str, dict[str, float]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._sweep_task: asyncio.Task[Any] | None = None
        self._intraday_rebuild_day: str = ""
        self._rewarm_ts = 0.0
        self.matcher = PaperMatcher(self.costs)
        self.live_exec: LiveExecutor | None = None
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

        self.underlyings: dict[str, Instrument] = {}
        self.books: dict[str, BookSnapshot] = {}
        self.quotes: dict[str, Quote] = {}
        self.ltps: dict[str, float] = {}
        self._future_to_underlying: dict[str, str] = {}
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
        try:
            await asyncio.to_thread(self.archive.flush, final=True)
        except Exception as exc:  # noqa: BLE001 - shutting down; the archive must not block it
            log.warning("archive.final_flush_failed", error=str(exc))
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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
        self.focus = [r for r in await asyncio.to_thread(self._focus_list) if r in groups]
        for r in self.focus:
            self._select_strikes(groups[r], None)
        n_opts = sum(len(g.options) for g in groups.values())
        self._focus_option_codes = {o.scrip_code for r in self.focus for o in groups[r].options}
        log.info("universe.focus", roots=self.focus, strikes_each_side=self.s.focus_strikes_each_side,
                 option_books=len(self._focus_option_codes))
        for g in groups.values():
            for o in g.options:
                self._segment_by_code[o.scrip_code] = o.segment
        self._future_to_underlying = {f.scrip_code: g.root for g in groups.values() for f in g.futures}
        self._front_future = {g.futures[0].scrip_code: g.root for g in groups.values() if g.futures}

        # Futures are OI sources, not bar sources: on NSE the front future's `symbol` is the cash
        # symbol, and tracking it wrote futures ticks into the equity's bars (found 2026-09-21).
        # `subscriptions()` puts them on mf+oi only; the aggregator ignores untracked codes.
        subs = UniverseBuilder.subscriptions(
            groups.values(),
            futures_months=self.s.universe_futures_months,
            option_depth=self.s.universe_option_depth,
        )
        for g in groups.values():
            for inst in (g.underlying, *g.futures, *g.options):
                self._inst_by_code[inst.scrip_code] = inst
            self._tick_archive_codes.update(i.scrip_code for i in (g.underlying, *g.futures))
            self._option_tick_codes.update(o.scrip_code for o in g.options)
        await self.feed.subscribe(Channel.QUOTES, subs["quotes"])
        # the front future's book too: F&O order flow lives in the future as much as the cash
        fronts = [g.futures[0] for g in groups.values() if g.futures] if self.s.universe_future_depth else []
        # the focus list: its front future's book and its strikes' books
        deep = [i for r in self.focus for i in (*groups[r].futures[:1], *groups[r].options)]
        if self.s.universe_depth:
            books = {i.scrip_code: i for i in (*subs["depth"], *fronts, *deep)}
            await self.feed.subscribe(Channel.DEPTH, list(books.values()))
            self._tick_archive_codes.update(i.scrip_code for i in deep if i.kind is InstrumentKind.FUTURE)
        await self.feed.subscribe(Channel.OI, subs["oi"])
        log.info(
            "engine.subscribed",
            source=self.feed.capabilities.name,
            quotes=len(subs["quotes"]), depth=len(subs["depth"]), oi=len(subs["oi"]),
            options=n_opts, underlyings=len(universe),
        )

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        """A background task the engine keeps a reference to until it ends, whose exception is
        logged (an unobserved task exception otherwise surfaces, if at all, at garbage collection)."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("task.failed", task=name, error=repr(t.exception()))

        task.add_done_callback(_done)
        return task

    def _focus_list(self) -> list[str]:
        """The last session's most traded option roots plus the configured indices (fixed for the day)."""
        days = [d for d in self.oi_daily.days() if d < date.today()]
        if not days or self.s.focus_top_n <= 0:
            return []
        bhav = self.oi_daily.load(days[-1], columns=("symbol", "kind", "close", "volume"))
        f = self.s.data_dir / SYMBOL_CHANGES_FILE
        renames = parse_symbol_changes(f.read_text(errors="replace")) if f.exists() else {}
        indices = [x.strip().upper() for x in self.s.focus_indices.split(",") if x.strip()]
        return focus_roots(bhav, self.s.focus_top_n, indices, renames=renames)

    def _select_strikes(self, g: ScripGroup, around: float | None) -> None:
        """One root's strikes: ATM ± ``focus_strikes_each_side`` for the focus list, the usual N
        nearest otherwise; centred on ``around`` (the live future) or, without it, the close."""
        if self.universe_builder is None:
            return
        self.universe_builder.select_strikes(
            g, self._prev_close(g.underlying.symbol), date.today(), around=around,
            each_side=self.s.focus_strikes_each_side if g.root in self.focus else None,
        )

    async def _apply_strike_changes(self, added: list[Instrument], dropped: list[Instrument]) -> None:
        """Subscribe the strikes that came in, drop the ones that left; books only for the focus
        list. New strikes take their official closing OI from the bhavcopy, like the boot's."""
        for o in added:
            self._segment_by_code[o.scrip_code] = o.segment
            self._inst_by_code[o.scrip_code] = o
            self._option_tick_codes.add(o.scrip_code)
        focus_roots_ = set(self.focus)
        add_books = [o for o in added if o.underlying in focus_roots_]
        drop_books = [o for o in dropped if o.scrip_code in self._focus_option_codes]
        self._focus_option_codes.update(o.scrip_code for o in add_books)
        for o in dropped:
            self._option_tick_codes.discard(o.scrip_code)
            self._focus_option_codes.discard(o.scrip_code)
        if added:
            await self.feed.subscribe(Channel.QUOTES, added)
            await self.feed.subscribe(Channel.OI, added)
            if self.s.universe_depth and add_books:
                await self.feed.subscribe(Channel.DEPTH, add_books)
            if self.oi_ref_day is not None:
                self.oi_ref.update(
                    await asyncio.to_thread(self.oi_daily.closes, self.oi_ref_day, [o.scrip_code for o in added])
                )
        if dropped:
            await self.feed.unsubscribe(Channel.QUOTES, dropped)
            await self.feed.unsubscribe(Channel.OI, dropped)
            if self.s.universe_depth and drop_books:
                await self.feed.unsubscribe(Channel.DEPTH, drop_books)

    async def _recenter_strikes(self, now: float) -> None:
        """Every root whose live front future is a strike or more from its strikes' centre gets
        them re-picked around it (at most once a root per ``RECENTER_MIN_S``)."""
        added: list[Instrument] = []
        dropped: list[Instrument] = []
        moved: list[tuple[str, float, float]] = []
        for g in self.groups.values():
            if not g.options or not g.futures or g.segment is Segment.MCX_FO:
                continue
            ref = self.ltps.get(g.futures[0].scrip_code) or self.ltps.get(g.underlying.scrip_code)
            strikes = sorted({o.strike for o in g.options})
            steps = [b - a for a, b in pairwise(strikes) if b > a]
            if not ref or not g.center or not steps or now - self._recentered_at.get(g.root, 0.0) < RECENTER_MIN_S:
                continue
            if abs(ref - g.center) < min(steps):
                continue
            before = {o.scrip_code: o for o in g.options}
            was = g.center
            self._select_strikes(g, ref)
            after = {o.scrip_code: o for o in g.options}
            added += [after[c] for c in after.keys() - before.keys()]
            dropped += [before[c] for c in before.keys() - after.keys()]
            self._recentered_at[g.root] = now
            moved.append((g.root, round(was, 2), round(ref, 2)))
        if moved:
            await self._apply_strike_changes(added, dropped)
            log.info("universe.recentered", roots=len(moved), added=len(added), dropped=len(dropped), sample=moved[:5])

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
        """Warm the primary intraday series and a year of dailies from REST.

        Sequential and rate-limit friendly: the broker's historical endpoint is the slowest thing
        we touch, and hammering it at boot is how the old stack earned its retry storms.
        """
        end = date.today()
        start_intraday = end - timedelta(days=max(7, self.s.backfill_days // 2))
        start_daily = end - timedelta(days=400)
        ok = failed = 0
        # Started mid-session: today's 1m bars so far, so the board is right from the first scan
        # and not only from the moment the socket joined. Per segment: MCX opens at 09:00, and on
        # an NSE holiday it may be trading while NSE is not.
        now = time.time()

        def started(seg: Segment) -> bool:
            return self.calendar.is_trading_day(end, seg) and now >= session_open_ts(seg, end)
        # The historical endpoint drops the LATEST session unless the end runs past it
        # (research/history.py, measured 2026-10-18), so every request asks two days further.
        ask_to = (end + timedelta(days=2)).isoformat()
        for inst in universe:
            frames: list[tuple[str, date]] = [(PRIMARY_TF, start_intraday), ("1d", start_daily)]
            # an MCX underlying is its front future; on NSE the board reads the cash stock
            on_board = inst.kind is InstrumentKind.EQUITY or inst.segment is Segment.MCX_FO
            if on_board and started(inst.segment):
                frames.append(("1m", end))
            for tf, start in frames:
                try:
                    rows = await self.rest.candles(inst, tf, start.isoformat(), ask_to)
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
                    if tf == "1m":
                        # today's 2m … 15m from the same rows — no extra REST calls; before this
                        # they began at the boot, and the chart's 5m was a 404 for five minutes
                        for up in self.aggregator.timeframes:
                            if up not in ("1m", "1d", PRIMARY_TF):
                                self.aggregator.seed(inst, up, rows, ts_of=ist_naive_to_ts, until=now)
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

    async def _on_tick(self, tick: TickMsg) -> None:
        code = str(tick["scrip_code"])
        recv = float(tick.get("recv_ts") or time.time())
        inst = self._inst_by_code.get(code)
        self.feed_rate.on_quote(
            tick["source"],
            code,
            f"{inst.segment.value} {inst.kind.value.lower()}" if inst else "unmapped",
            recv,
            int(tick.get("total_qty") or 0),
        )
        if code in self._tick_archive_codes:
            if self.s.archive_ticks:
                self.archive.tick(tick)
            ts = float(tick.get("ts") or recv)
            t = ts if abs(ts - recv) < 120 else recv
            args = (
                code,
                t,
                float(tick.get("ltp") or 0),
                int(tick.get("total_qty") or 0),
                float(tick.get("bid") or 0),
                float(tick.get("ask") or 0),
            )
            auction = t < self._open_ts(inst.segment if inst else Segment.NSE_EQ, t)
            self.flow.on_tick(*args, auction=auction)
            self.micro.on_tick(*args, auction=auction)
        elif code in self._option_tick_codes and self.s.archive_ticks:
            self.archive.tick(tick)
        pc = float(tick.get("prev_close") or 0)
        if pc > 0:
            self.prev_close_by_code[code] = pc
        ltp = float(tick.get("ltp") or 0)
        if ltp > 0:
            self.ltps[code] = ltp
            self.quotes[code] = Quote(
                ltp=ltp,
                bid=float(tick.get("bid") or 0),
                ask=float(tick.get("ask") or 0),
                ts=recv,
            )
        await self.aggregator.on_tick(tick)
        await self.bus.publish(Topic.TICK, tick)

    async def _on_depth(self, depth: DepthMsg) -> None:
        code = str(depth["scrip_code"])
        ts = float(depth.get("recv_ts") or time.time())
        self.books[code] = BookSnapshot(scrip_code=code, bids=depth["bids"], asks=depth["asks"], ts=ts)
        if code in self._tick_archive_codes or code in self._focus_option_codes:
            self.flow.on_depth(code, ts, depth["bids"], depth["asks"])
            if code in self._tick_archive_codes:
                self.micro.on_depth(code, ts, depth["bids"], depth["asks"])
            if ts - self._depth_last.get(code, 0.0) >= self.s.archive_depth_every_s:
                self._depth_last[code] = ts
                self.archive.depth(code, ts, depth["bids"], depth["asks"])

    async def _on_oi(self, oi: OIMsg) -> None:
        code = str(oi["scrip_code"])
        ts = float(oi.get("recv_ts") or time.time())
        now_oi = float(oi["open_interest"])
        self.archive.oi(
            code, ts, now_oi, float(oi["oi_change_pct"]) if oi.get("oi_change_pct") is not None else None
        )
        self.oi_live[code] = now_oi
        if code in self._front_future:
            self.flow.on_oi(code, ts, now_oi)
        ref = self._oi_reference(code, now_oi, ts)
        chg = (now_oi / ref - 1) * 100 if ref else None
        symbol = self._future_to_underlying.get(code)
        if symbol is None:
            # Not a future we map to an underlying → an option strike. The Options page and the
            # F&O panel read it from here; the change is ours, 5paisa's field is 0.0 on every frame.
            self.option_oi[code] = {
                "oi": now_oi,
                "change_pct": chg if chg is not None else float(oi["oi_change_pct"]),
                "ts": ts,
            }
            return
        inst = self.underlyings.get(symbol)
        if inst is None:
            return
        if code not in self._front_future:
            # the next-month future: its OI is archived above, but stamping it on the bars made
            # every underlying's bar OI flip between two contracts (RELIANCE 139.8M ↔ 27.4M)
            return
        if chg is not None:
            self.fut_oi_change[symbol] = chg
            if code in self.oi_ref_ts:
                self.oi_ref_since[symbol] = self.oi_ref_ts[code]
        self.aggregator.set_oi(
            inst.scrip_code,
            oi=int(now_oi),
            change_pct=float(oi["oi_change_pct"]),
            fut_code=code,
        )

    def _oi_reference(self, code: str, oi: float = 0.0, ts: float | None = None) -> float | None:
        """What a contract's OI change is measured from: NSE's official closing OI (bhavcopy), the
        previous MCX session's close as we recorded it, else — MCX, once the archive was consulted
        today — the first OI seen this session (``oi_ref_ts`` says when)."""
        ref = self.oi_ref.get(code) or self.mcx_oi_ref.get(code)
        if (
            not ref
            and oi > 0
            and ts is not None
            and self._segment_by_code.get(code) is Segment.MCX_FO
            and self._mcx_oi_ref_day == ist_day(ts).isoformat()
        ):
            ref = self.mcx_oi_ref[code] = oi
            self.oi_ref_ts[code] = ts
        return ref or None

    def book_for(self, scrip_code: str) -> BookSnapshot | None:
        return self.books.get(scrip_code)

    def ltp_for(self, scrip_code: str) -> float | None:
        return self.ltps.get(scrip_code)

    def _open_ts(self, segment: Segment, ts: float) -> float:
        """The session open of ``ts``'s IST day, cached per segment and day (called on every tick)."""
        key = (segment, ist_day(ts))
        hit = self._open_cache.get(key)
        if hit is None:
            hit = self._open_cache[key] = session_open_ts(segment, key[1])
        return hit

    def session_phase(self, symbol: str, ts: int) -> str:
        inst = self.underlyings.get(symbol)
        return session_phase_of(inst.segment if inst else Segment.NSE_EQ, ts)

    # -- bars ----------------------------------------------------------------------------------------

    async def _on_bar_close(self, bar: UnifiedBar) -> None:
        await self.bus.publish(Topic.BAR, bar)
        if bar.tf == "1m":
            self.archive.bar(bar)
            minute = int(bar.ts) + 60
            if minute > self._scanned_minute:
                self._scanned_minute = minute
                self._spawn(self._scan(minute), "scan")
        if bar.tf != PRIMARY_TF:
            return
        # Off the tick path: at 15:15 IST two hundred 30m bars close in the same second and each
        # waits on a REST round trip. Awaiting that inside the feed handler would stall every
        # symbol's ticks. Concurrency is bounded by the reconciler's semaphore.
        self._spawn(self._reconcile(bar), "reconcile")

    async def _reconcile(self, bar: UnifiedBar) -> None:
        """Install the exchange's own candle over the live build.

        The live build is right most of the time (87.8% exact on 1m, measured; better on 30m).
        Anything that reads bars forever should read the exchange's, and REST serves a bucket
        within seconds of its close. A bucket the socket joined mid-way (PARTIAL) that REST cannot
        confirm stays a fragment and is counted.
        """
        if not (self.reconciler_ready and self.s.has_credentials):
            return
        try:
            check = await self.reconciler.reconcile_bar(bar, timeout_s=self.s.bar_reconcile_timeout_s)
            if not check.found or check.error:
                self.reconciler.live_bar_kept += 1
                if bar.source is BarSource.PARTIAL:
                    self.reconciler.partial_unconfirmed += 1
                    log.warning(
                        "bar.partial_unconfirmed",
                        symbol=bar.symbol,
                        ts=bar.ts,
                        reason=check.error or "no REST bucket",
                    )
        except Exception as exc:  # noqa: BLE001 - a failed check keeps the live build
            log.warning("bar.reconcile_failed", symbol=bar.symbol, error=str(exc))

    def scan_symbols(self) -> list[str]:
        """The board's universe: NSE stocks with F&O and the MCX commodities — not indices."""
        return sorted(
            r
            for r, g in self.groups.items()
            if r not in INDEX_ROOTS and (g.equity is not None or g.segment is Segment.MCX_FO)
        )

    def commodities(self) -> list[str]:
        """MCX roots: on the board as their own sector, never picked — the picks rule was built
        and tested on NSE stocks only."""
        return sorted(r for r, g in self.groups.items() if g.segment is Segment.MCX_FO)

    async def _scan(self, minute: int) -> None:
        """Two seconds after the minute's bars close — late enough for the whole universe's 1m
        bars to have closed (by tick or by the 1 Hz flush), early enough to be useful."""
        await asyncio.sleep(max(0.0, minute + 2 - time.time()))
        try:
            oi = {}
            for sym, chg in self.fut_oi_change.items():
                st = self.scanner.state(sym, time.time())
                if st is not None and st.prev_close > 0:
                    oi[sym] = OIRead(
                        oi_change_pct=chg,
                        price_change_pct=(st.last / st.prev_close - 1) * 100,
                        since=self.oi_ref_since.get(sym),
                    )
            now = time.time()

            def _run() -> None:
                if self.s.archive_enabled:
                    self._record_fno(now)
                board = self.scanner.scan(self.scan_symbols(), now, oi, commodities=self.commodities())

                attach_setups(board, lambda sym: structure_from_store(self.store, sym, now))

            await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001 - a failed scan waits for the next minute
            log.warning("scan.failed", error=str(exc))

    async def _ensure_oi_reference(self, today: date | None = None) -> bool:
        """Load the latest official closing OI before today: the store's copy, else NSE's file.
        NSE's own calendar decides which weekday was a session — a holiday simply has no file."""
        today = today or date.today()
        d = today - timedelta(days=1)
        for _ in range(10):
            if d.weekday() < 5:
                if not self.oi_daily.has(d):
                    df = await fetch_fo_bhavcopy(self.http, d)
                    if df is not None and not df.empty:
                        await asyncio.to_thread(self.oi_daily.write, d, df)
                if self.oi_daily.has(d):
                    # the strikes' closing OI too: the F&O panel measures every leg from it
                    codes = [*self._front_future, *(o.scrip_code for g in self.groups.values() for o in g.options)]
                    self.oi_ref = await asyncio.to_thread(self.oi_daily.closes, d, codes)
                    self.oi_ref_day = d
                    log.info("oi.reference", day=d.isoformat(), futures=len(self.oi_ref), of=len(codes))
                    return True
            d -= timedelta(days=1)
        log.warning("oi.no_reference", today=today.isoformat())
        return False

    async def _load_mcx_oi_reference(self, day: str) -> None:
        """The previous MCX session's closing OI of every MCX future and option, from our own
        archive — kept only where the engine was still recording in the last 15 minutes of that
        session. Where there is none, today's first recorded OI stands in (and says since when)."""
        prev = self.calendar.previous_trading_day(date.fromisoformat(day), Segment.MCX_FO)
        codes = [
            i.scrip_code
            for g in self.groups.values()
            if g.segment is Segment.MCX_FO
            for i in (*g.futures, *g.options)
        ]
        not_before = session_close_ts(Segment.MCX_FO, prev) - 900
        refs: dict[str, float] = {}
        first: dict[str, tuple[float, float]] = {}
        try:
            refs = await asyncio.to_thread(self.archive.closing_oi, prev.isoformat(), codes, not_before)
            missing = [c for c in codes if c not in refs]
            first = await asyncio.to_thread(self.archive.first_oi, day, missing)
        except Exception as exc:  # noqa: BLE001 - no reference: the first OI seen live stands in
            log.warning("oi.mcx_reference_failed", error=str(exc))
        self.mcx_oi_ref = dict(refs)
        for code, (ts, oi) in first.items():
            self.mcx_oi_ref[code] = oi
            self.oi_ref_ts[code] = ts
            if code in self._front_future:
                self.oi_ref_since[self._front_future[code]] = ts
        self._mcx_oi_ref_day = day
        log.info("oi.mcx_reference", day=prev.isoformat(), closes=len(refs), today_first=len(first), of=len(codes))

    async def _rewarm(self) -> None:
        cold = [g.underlying for g in self.groups.values() if self._prev_close(g.underlying.symbol) is None]
        log.info("engine.rewarm", underlyings=len(cold))
        await self._backfill(cold)
        await self._intraday_universe_rebuild()

    async def _intraday_universe_rebuild(self) -> None:
        if self.universe_builder is None:
            return
        try:
            cat = await self.catalogue_loader.ensure(force=True)
            self.universe_builder.cat = cat
            before = {o.scrip_code: o for g in self.groups.values() for o in g.options}
            for g in self.groups.values():
                self._select_strikes(g, g.center)  # the centre the strikes already follow, not the close
            after = {o.scrip_code: o for g in self.groups.values() for o in g.options}
            fresh = [after[c] for c in after.keys() - before.keys()]
            gone = [before[c] for c in before.keys() - after.keys()]
            await self._apply_strike_changes(fresh, gone)
            log.info("universe.intraday_rebuild", new_strikes=len(fresh), dropped=len(gone))
        except Exception as exc:  # noqa: BLE001 - a failed rebuild keeps the overnight universe
            log.warning("universe.intraday_rebuild_failed", error=str(exc))

    # -- background tasks -------------------------------------------------------------------------------

    async def _clock(self) -> None:
        while not self._stop.is_set():
            try:
                await self.aggregator.flush_stale()
            except Exception as exc:
                log.exception("clock.failed", error=str(exc))
            await asyncio.sleep(1.0)

    async def _housekeeping(self) -> None:
        last_snapshot = 0.0
        last_archive = time.time()
        last_day = ist_day(time.time()).isoformat()
        while not self._stop.is_set():
            now = time.time()
            try:
                day = ist_day(now).isoformat()
                for fm in self.flow.drain():
                    self.archive.flow(fm.to_json())
                if now - last_archive > self.s.archive_flush_s:
                    last_archive = now
                    await asyncio.to_thread(self.archive.flush)

                if day != last_day:
                    last_day = day
                    self.feed_rate.reset()
                    self.oi_ref, self.oi_ref_day, self.fut_oi_change = {}, None, {}
                    self.mcx_oi_ref, self._mcx_oi_ref_day, self.oi_ref_since = {}, None, {}
                    self.oi_ref_ts = {}
                if (
                    self._mcx_oi_ref_day != day
                    and self._front_future
                    and any(g.segment is Segment.MCX_FO for g in self.groups.values())
                ):
                    await self._load_mcx_oi_reference(day)
                if (
                    self._front_future
                    and self.oi_ref_day is None
                    and now - self._oi_ref_checked > 1800
                ):
                    self._oi_ref_checked = now
                    self._spawn(self._ensure_oi_reference(), "oi_reference")
                    if self.s.has_credentials and self.s.engine_enabled:
                        await self.catalogue_loader.ensure()

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

                # A boot whose broker login failed (2026-10-20: a restart reused a spent TOTP) backfilled
                # nothing, so no previous close and no strikes — and nothing ever retried. Retry here.
                if (
                    self.reconciler_ready
                    and now - self._rewarm_ts > 120
                    and self.auth.session is not None
                    and self.auth.session.usable
                    and any(self._prev_close(g.underlying.symbol) is None for g in self.groups.values())
                ):
                    self._rewarm_ts = now
                    self._spawn(self._rewarm(), "rewarm")

                # scripFinder's 09:20 IST intraday rebuild: refetch the master, re-pick strikes,
                # subscribe anything new. Strikes listed 09:00–09:15 are not in an overnight master.
                if self.reconciler_ready and ist_hm(now) >= "09:20" and self._intraday_rebuild_day != day:
                    self._intraday_rebuild_day = day
                    self._spawn(self._intraday_universe_rebuild(), "intraday_rebuild")

                if (
                    self.s.strike_recenter
                    and self.reconciler_ready
                    and now - self._recenter_checked > 30
                    and any(is_open(g.segment, now, self.calendar) for g in self.groups.values() if g.segment is not Segment.MCX_FO)
                ):
                    self._recenter_checked = now
                    await self._recenter_strikes(now)

                if now - last_snapshot > 300:
                    last_snapshot = now
                    await self.ledger.insert_health(self.health_snapshot())
            except Exception as exc:
                log.exception("housekeeping.failed", error=str(exc))
            await asyncio.sleep(5.0)

    # -- state -----------------------------------------------------------------------------------------

    # -- introspection -------------------------------------------------------------------------------

    def fno_snapshot(self, symbol: str, now: float | None = None) -> dict[str, Any] | None:
        """Futures and options read together for one underlying: each leg's move since its
        reference, the 64-state reading, and every selected expiry's chain. None when the root
        has no future (cash-only, or not in the universe)."""
        g = self.groups.get(symbol)
        if g is None or not g.futures:
            return None
        now = now or time.time()
        today = ist_day(now)
        fut = g.futures[0]
        missing: list[str] = []
        used_refs: list[str] = [fut.scrip_code]

        f_px = self.ltps.get(fut.scrip_code)
        f_prev = self.prev_close_by_code.get(fut.scrip_code)
        f_oi = self.oi_live.get(fut.scrip_code)
        f_ref = self._oi_reference(fut.scrip_code)
        fut_leg: Leg | None = None
        if f_px and f_prev and f_oi is not None and f_ref:
            fut_leg = Leg(price_chg_pct=(f_px / f_prev - 1) * 100, oi_chg=f_oi - f_ref, oi_chg_pct=(f_oi / f_ref - 1) * 100)
        else:
            missing.append("futures: " + ", ".join(
                n for n, ok in (("price", f_px), ("previous close", f_prev), ("OI", f_oi is not None), ("OI reference", f_ref)) if not ok
            ))

        prev_day = self.calendar.previous_trading_day(today, g.segment)
        prev_close_ts = session_close_ts(g.segment, prev_day)
        expiries: list[dict[str, Any]] = []
        near_calls: list[OptQuote] = []
        near_puts: list[OptQuote] = []
        for e in g.option_expiries:
            und = g.future_for(e) or fut
            fx = self.ltps.get(und.scrip_code)
            if not fx:
                missing.append(f"{e}: no price for its future {und.name or und.scrip_code}")
                continue
            by_strike: dict[float, dict[str, OptQuote]] = {}
            for o in g.options:
                if o.expiry != e:
                    continue
                used_refs.append(o.scrip_code)
                by_strike.setdefault(o.strike, {})[o.option_type.value] = OptQuote(
                    ltp=self.ltps.get(o.scrip_code),
                    prev_close=self.prev_close_by_code.get(o.scrip_code),
                    oi=self.oi_live.get(o.scrip_code),
                    oi_ref=self._oi_reference(o.scrip_code),
                )
            rows = [StrikeRow(k, v.get("CE"), v.get("PE")) for k, v in by_strike.items()]
            exp_ts = session_close_ts(g.segment, date.fromisoformat(e))
            cr = read_chain(
                rows,
                future=fx,
                future_prev=self.prev_close_by_code.get(und.scrip_code),
                t=max(exp_ts - now, 60.0) / YEAR_S,
                t_prev=max(exp_ts - prev_close_ts, 60.0) / YEAR_S,
            )
            near_calls += cr.near_calls
            near_puts += cr.near_puts
            expiries.append(
                {
                    "expiry": e,
                    "days": (date.fromisoformat(e) - today).days,
                    "future": {"code": und.scrip_code, "name": und.name, "expiry": und.expiry, "ltp": fx},
                    **cr.view,
                }
            )

        calls, puts = leg_of(near_calls), leg_of(near_puts)
        # an options leg counts only if its OI moved by a material share of the futures' own OI
        material = (f_oi or 0) * WEAK_OI_PCT / 100
        calls = replace(calls, min_contracts=material) if calls else None
        puts = replace(puts, min_contracts=material) if puts else None
        if calls is None:
            missing.append("calls: no priced near-the-money strike with OI")
        if puts is None:
            missing.append("puts: no priced near-the-money strike with OI")
        reading = read_positioning(fut_leg, calls, puts) if fut_leg and calls and puts else None
        since = [self.oi_ref_ts[c] for c in used_refs if c in self.oi_ref_ts]
        return {
            "symbol": symbol,
            "segment": g.segment.value,
            "ts": now,
            "future": {
                "code": fut.scrip_code,
                "name": fut.name,
                "expiry": fut.expiry,
                "ltp": f_px,
                "prev_close": f_prev,
                "oi": f_oi,
                "oi_ref": f_ref,
            },
            "legs": {
                "futures": fut_leg.to_json() if fut_leg else None,
                "calls": calls.to_json() if calls else None,
                "puts": puts.to_json() if puts else None,
            },
            "reading": reading.to_json() if reading else None,
            #: the latest moment an OI reference was taken at, when any is not a previous close
            "oi_since": max(since) if since else None,
            "missing": missing,
            "expiries": expiries,
        }

    @staticmethod
    def fno_row(snap: dict[str, Any]) -> dict[str, Any]:
        """``fno_snapshot`` flattened for the archive: the reading, each leg, the nearest expiry."""
        r = snap.get("reading") or {}
        legs = snap.get("legs") or {}
        e = (snap.get("expiries") or [{}])[0]
        row: dict[str, Any] = {
            "symbol": snap["symbol"],
            "ts": int(snap["ts"] // 60 * 60),
            "fut_ltp": snap["future"].get("ltp"),
            "fut_oi": snap["future"].get("oi"),
            "row": r.get("row"),
            "score": r.get("score"),
            "bias": r.get("bias"),
            "premium": r.get("premium"),
            "weak": ",".join(r.get("weak") or []),
            "oi_since": snap.get("oi_since"),
        }
        for name in ("futures", "calls", "puts"):
            leg = legs.get(name) or {}
            row[f"{name}_state"] = leg.get("state")
            row[f"{name}_px_chg"] = leg.get("price_chg_pct")
            row[f"{name}_oi_chg"] = leg.get("oi_chg")
            row[f"{name}_oi_chg_pct"] = leg.get("oi_chg_pct")
        for k in ("expiry", "atm", "straddle", "implied_move_pct", "iv", "iv_prev", "pcr_oi", "pcr_oi_chg"):
            row[f"x_{k}"] = e.get(k)
        for k in ("call_wall", "put_wall", "call_add", "put_add"):
            row[f"x_{k}"] = (e.get(k) or {}).get("strike")
        return row

    def _record_fno(self, now: float) -> None:
        """Worker thread: read every root with options and archive the minute's row. It only reads
        engine state (dict lookups, list walks), never iterates a dict the feed is writing."""
        for sym, g in list(self.groups.items()):
            if not g.options:
                continue
            try:
                snap = self.fno_snapshot(sym, now)
            except Exception as exc:  # noqa: BLE001 - one root's failure never costs the others
                log.warning("fno.snapshot_failed", symbol=sym, error=str(exc))
                continue
            if snap is not None:
                self.archive.fno(self.fno_row(snap))

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
                detail=(
                    "market closed" if not open_now
                    else "no message for over 2 minutes" if fh.silence_s is not None and fh.silence_s >= 120
                    else ""
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
            Check("breaker", not self.gateway.breaker_tripped),
            Check(
                "bars_warm",
                sum(1 for sym in self.underlyings if self.store.count(sym, PRIMARY_TF) >= 21)
                >= max(1, len(self.underlyings) // 2)
                if self.underlyings
                else True,
                detail="fewer than half the universe has 21+ primary bars",
            ),
        ]
        return {
            **self.health.evaluate(checks),
            "mode": self.mode().value,
            "armed_until": self._armed_until,
            "halted": self.halted()[0],
            "feed": {
                "source": self.feed.capabilities.name,
                "connected": fh.connected,
                "messages": fh.messages,
                "ticks": fh.ticks,
                "depth": fh.depth,
                "oi": fh.oi,
                "reconnects": fh.reconnects,
                "silence_s": fh.silence_s,
                "subscriptions": fh.subscriptions,
            },
            "feed_rate": self.feed_rate.snapshot(),
            "bars": self.aggregator.stats() | self.store.stats(),
            "gateway": self.gateway.stats(),
            "rest": self.rest.stats(),
            "catalogue": self.catalogue_loader.catalogue.stats(),
            "universe": self.universe_builder.summary(self.groups) if self.universe_builder else {},
            "fidelity": self.reconciler.snapshot() if self.reconciler_ready else {},
            "option_oi_tracked": len(self.option_oi),
            "orderflow": self.flow.stats(),
            "oi_reference": {
                "day": self.oi_ref_day.isoformat() if self.oi_ref_day else None,
                "futures": len(self.oi_ref),
                "with_change": len(self.fut_oi_change),
            },
            "scanner": {
                "scans": self.scanner.scans,
                "last_ts": self.scanner.board.ts if self.scanner.board else None,
            },
            "archive": self.archive.stats(),
            "telegram": self.telegram.stats(),
            "boot_notes": self.boot_notes,
        }
