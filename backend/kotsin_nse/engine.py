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
from datetime import date, timedelta
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
from .instrument.catalogue import CatalogueLoader
from .instrument.quote import Quote
from .instrument.universe import ScripGroup, UniverseBuilder, UniversePolicy
from .ledger.db import Ledger
from .market.fo_bhavcopy import OiDailyStore
from .market.fo_bhavcopy import fetch_day as fetch_fo_bhavcopy
from .market.session import (
    TradingCalendar,
    is_open,
    ist_day,
    ist_hm,
    ist_naive_to_ts,
    session_open_ts,
)
from .market.session import (
    session_phase as session_phase_of,
)
from .ops.archive import DailyArchive
from .ops.health import Check, HealthMonitor
from .ops.telegram import Telegram
from .orderflow.meter import FlowMeter
from .risk.costs import CostModel
from .scan.board import OIRead, attach_setups
from .scan.live import LiveScanner
from .structure.service import from_store as structure_from_store
from .venue.fivepaisa.auth import Authenticator
from .venue.fivepaisa.rest import FivePaisaREST

log = structlog.get_logger(__name__)

#: the frame backfilled at boot and checked against the exchange on every close
PRIMARY_TF = "30m"


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
        self.archive = DailyArchive(settings.data_dir / "archive", enabled=settings.archive_enabled)

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
        self.feed_rate = FeedRateMeter()
        #: estimated order flow per minute for the underlyings and their front futures
        self.flow = FlowMeter()
        self._depth_last: dict[str, float] = {}
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
        self.reconciler = BarReconciler(self.rest, self.store, lambda sym: self.underlyings.get(sym))
        self.groups: dict[str, ScripGroup] = {}
        self.universe_builder: UniverseBuilder | None = None
        self.option_oi: dict[str, dict[str, float]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._sweep_task: asyncio.Task[Any] | None = None
        self._intraday_rebuild_day: str = ""
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
        if self.s.universe_depth:
            await self.feed.subscribe(Channel.DEPTH, [*subs["depth"], *fronts])
        await self.feed.subscribe(Channel.OI, subs["oi"])
        log.info(
            "engine.subscribed",
            source=self.feed.capabilities.name,
            quotes=len(subs["quotes"]), depth=len(subs["depth"]), oi=len(subs["oi"]),
            options=n_opts, underlyings=len(universe),
        )

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
                    # the other frames (2m … 15m) rebuild from live ticks; no extra REST calls
                    self.aggregator.seed(inst, tf, rows, ts_of=ist_naive_to_ts)
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
            self.flow.on_tick(
                code,
                ts if abs(ts - recv) < 120 else recv,
                float(tick.get("ltp") or 0),
                int(tick.get("total_qty") or 0),
                float(tick.get("bid") or 0),
                float(tick.get("ask") or 0),
            )
        elif code in self._option_tick_codes and self.s.archive_ticks:
            self.archive.tick(tick)
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
        if code in self._tick_archive_codes:
            self.flow.on_depth(code, ts, depth["bids"], depth["asks"])
            if ts - self._depth_last.get(code, 0.0) >= self.s.archive_depth_every_s:
                self._depth_last[code] = ts
                self.archive.depth(code, ts, depth["bids"], depth["asks"])

    async def _on_oi(self, oi: OIMsg) -> None:
        fut_code = str(oi["scrip_code"])
        self.archive.oi(
            fut_code,
            float(oi.get("recv_ts") or time.time()),
            float(oi["open_interest"]),
            float(oi["oi_change_pct"]) if oi.get("oi_change_pct") is not None else None,
        )
        if fut_code in self._front_future:
            self.flow.on_oi(fut_code, float(oi.get("recv_ts") or time.time()), float(oi["open_interest"]))
        symbol = self._future_to_underlying.get(fut_code)
        if symbol is None:
            # Not a future we map to an underlying → an option strike. Keep its OI; the Options
            # page reads it from here.
            self.option_oi[fut_code] = {
                "oi": float(oi["open_interest"]),
                "change_pct": float(oi["oi_change_pct"]),
                "ts": float(oi.get("recv_ts") or time.time()),
            }
            return
        inst = self.underlyings.get(symbol)
        if inst is None:
            return
        if fut_code not in self._front_future:
            # the next-month future: its OI is archived above, but stamping it on the bars made
            # every underlying's bar OI flip between two contracts (RELIANCE 139.8M ↔ 27.4M)
            return
        ref = self.oi_ref.get(fut_code)
        if ref:
            self.fut_oi_change[symbol] = (float(oi["open_interest"]) / ref - 1) * 100
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
                task = asyncio.create_task(self._scan(minute))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
        if bar.tf != PRIMARY_TF:
            return
        # Off the tick path: at 15:15 IST two hundred 30m bars close in the same second and each
        # waits on a REST round trip. Awaiting that inside the feed handler would stall every
        # symbol's ticks. Concurrency is bounded by the reconciler's semaphore.
        task = asyncio.create_task(self._reconcile(bar))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

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
            r for r, g in self.groups.items() if g.equity is not None or g.segment is Segment.MCX_FO
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
                    oi[sym] = OIRead(oi_change_pct=chg, price_change_pct=(st.last / st.prev_close - 1) * 100)
            now = time.time()

            def _run() -> None:
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
                    codes = list(self._front_future)
                    self.oi_ref = await asyncio.to_thread(self.oi_daily.closes, d, codes)
                    self.oi_ref_day = d
                    log.info("oi.reference", day=d.isoformat(), futures=len(self.oi_ref), of=len(codes))
                    return True
            d -= timedelta(days=1)
        log.warning("oi.no_reference", today=today.isoformat())
        return False

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
                self._inst_by_code[o.scrip_code] = o
            if fresh:
                await self.feed.subscribe(Channel.QUOTES, fresh)
                await self.feed.subscribe(Channel.DEPTH, fresh)
                await self.feed.subscribe(Channel.OI, fresh)
            log.info("universe.intraday_rebuild", new_strikes=len(fresh))
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
                if (
                    self._front_future
                    and self.oi_ref_day is None
                    and now - self._oi_ref_checked > 1800
                ):
                    self._oi_ref_checked = now
                    self._bg_tasks.add(asyncio.create_task(self._ensure_oi_reference()))
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

                # scripFinder's 09:20 IST intraday rebuild: refetch the master, re-pick strikes,
                # subscribe anything new. Strikes listed 09:00–09:15 are not in an overnight master.
                if self.reconciler_ready and ist_hm(now) >= "09:20" and self._intraday_rebuild_day != day:
                    self._intraday_rebuild_day = day
                    self._bg_tasks.add(asyncio.create_task(self._intraday_universe_rebuild()))

                if now - last_snapshot > 300:
                    last_snapshot = now
                    await self.ledger.insert_health(self.health_snapshot())
            except Exception as exc:
                log.exception("housekeeping.failed", error=str(exc))
            await asyncio.sleep(5.0)

    # -- state -----------------------------------------------------------------------------------------

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
