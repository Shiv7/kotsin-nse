"""REST + WebSocket surface. The frontend reads only this.

Design rule: **the API never computes anything.** It reads engine state and ledger rows and returns
them. Two components disagreeing about the same rule — a dashboard with ``TIME_STOP_DAYS=3`` next to
an executor with ``maxhold.days=5``, and no document naming an authority — is the failure this
prevents.

The control endpoints are the ones that can lose money, so each states its safety property:
``/control/mode`` refuses a live mode without an explicit arming window, ``/control/kill`` always
works even when everything else is frozen.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..bars.daily import previous_session
from ..bars.indicators import SUPERTREND_CONVERGED_BARS, atr, bollinger, supertrend
from ..bars.unified import UnifiedBar
from ..config import Segment
from ..domain import Instrument, InstrumentKind, OptionType
from ..engine import SELECTION_POLICY, Engine, _position_json
from ..exec import depth_check
from ..exec.gateway import Mode
from ..hotstocks.service import HotStocksService
from ..ledger.db import events, rejections, signals, trades
from ..market.session import IST, TF_SECONDS, ist_day, ist_hm, ist_today, to_ist
from ..strategy.catalog import BOOKS, LIVE_KEYS
from ..strategy.keys import (
    ALL_KEYS,
    SHADOW_BOOKS,
    SHADOW_OF,
    STOP_MIRRORS,
    STOP_RULE_LABELS,
    StrategyKey,
    describe_book,
    stop_mirrors_of,
    stop_rule_of,
)
from . import daybook, export, live_trades, peer, review, review_direction, review_peers, shadow
from .ws import Hub, handle, pump

log = structlog.get_logger(__name__)


class ModeRequest(BaseModel):
    mode: str
    armed_minutes: int | None = Field(
        default=None, description="required for LIVE_CAPPED / LIVE; arming is never implicit"
    )


class HaltRequest(BaseModel):
    halted: bool
    reason: str = ""


class WalletResetRequest(BaseModel):
    initial: float | None = None


class BookActionRequest(BaseModel):
    signal_id: str


class ReviewSignalRequest(BaseModel):
    signal_id: str


class ReviewTradeRequest(BaseModel):
    run_id: str
    index: int = Field(ge=0, description="index into the run's trades list")


class ReviewCohortRequest(BaseModel):
    source: str = Field(description="'ledger' or 'backtest:<run id>'")
    strategy: str | None = None


class ExperimentRequest(BaseModel):
    hypothesis_id: str


class AutopilotRequest(BaseModel):
    source: str | None = Field(default=None, description="'ledger', 'backtest:<id>' or null = auto")


class ProposeRequest(BaseModel):
    title: str
    changes: list[dict[str, float | str]] = Field(description="[{path, value}] on BacktestParams")
    expected: str = ""
    rationale: str = ""
    segment: str = "NSE_EQ"


#: The trades page's book sets: the books that trade, the stop-rule mirrors, the other shadows, or every book.
BOOK_SETS = "^(trading|mirrors|shadows|all)$"


def _book_keys(books: str) -> list[StrategyKey] | None:
    """The books in a book set; None for ``all``."""
    return {
        "trading": [k for k in ALL_KEYS if k not in SHADOW_BOOKS],
        "mirrors": list(STOP_MIRRORS),
        "shadows": [k for k in SHADOW_BOOKS if k not in STOP_MIRRORS],
    }.get(books)


def _books_where(books: str) -> Any:
    keys = _book_keys(books)
    return None if keys is None else trades.c.strategy.in_([k.value for k in keys])


#: book → the book whose entry fill it copies: a stop-rule mirror its source, the wide-stop shadow RT-Y. They place
#: no entry order of their own, so the depth check judges their entry on the fill they copied.
ENTRY_SOURCES: dict[str, str] = {
    **{k.value: src.value for k, (src, _rule) in STOP_MIRRORS.items()},
    **{k.value: src.value for k, src in SHADOW_OF.items()},
}


#: closed trades whose depth check is kept worked out: a check is a few KB, and both engines close about 100 a day
DEPTH_CACHE_TRADES = 5_000


def _depth_work(rows: list[dict[str, Any]], order_json: list[str], position_json: list[str]) -> dict[str, dict[str, Any]]:
    """The checks themselves, run in a worker thread: parse the stored rows and judge each trade's fills."""
    return depth_check.check_trades(rows, [json.loads(j) for j in order_json], positions=[json.loads(j) for j in position_json],
                                    sources=ENTRY_SOURCES)


@dataclass(slots=True)
class DepthReads:
    """The depth check as the pages read it (exec/depth_check.py; operator, 2026-10-09: "go ahead build it n tell me
    where to view it"). Worked out from the stored orders when a page asks: no schema change, nothing written,
    nothing the engine reads.

    The API shares the engine's event loop (main.py), so the work is kept small and off it (review, 9 Oct 2026). A
    closed trade's orders are all stored before its row (engine ``_book_exit``), so its check never changes and is
    worked out once. Only FILLED orders are read, and they are parsed and judged in a worker thread. A summary is
    kept until another trade closes."""

    ledger: Any
    #: trade id → its check, the oldest dropped past ``DEPTH_CACHE_TRADES``
    checks: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    #: (scope, book set) → (from when, the trades table's count and newest close then, the summary)
    summaries: dict[tuple[str, str], tuple[float, tuple[int, float | None], dict[str, Any]]] = field(default_factory=dict)
    #: when the first order carrying a depth book was stored, once one has been
    first: float | None = None

    async def for_trades(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Trade id → its check. An entry is placed before its position opens (at most a minute's wait, the momentum
        look and the chase), so a new trade's orders are read from an hour before it opened."""
        todo = [t for t in rows if str(t.get("id")) not in self.checks]
        made: dict[str, dict[str, Any]] = {}
        if todo:
            opened = [float(t.get("opened_ts") or t.get("closed_ts") or 0.0) for t in todo]
            closed = [float(t.get("closed_ts") or 0.0) for t in todo]
            order_json = await self.ledger.filled_order_json(min(opened) - 3_600, max(*closed, *opened) + 120)
            position_json = await self.ledger.position_json([str(t.get("position_id") or "") for t in todo])
            made = await asyncio.to_thread(_depth_work, todo, order_json, position_json)
            for tid, dc in made.items():
                if dc.get("entryFrom"):
                    dc["entryFromLabel"] = describe_book(dc["entryFrom"])["strategy_label"]
                self.checks[tid] = dc
            while len(self.checks) > DEPTH_CACHE_TRADES:
                self.checks.popitem(last=False)
        out: dict[str, dict[str, Any]] = {}
        for t in rows:
            tid = str(t.get("id"))
            found = made.get(tid) or self.checks.get(tid)
            if found is not None:
                out[tid] = found
        return out

    async def summary(self, since: str, books: str) -> dict[str, Any]:
        """Per book: the closed trades' fills against their books (``depth_check.summarize``). ``today``: the trades
        closed this session; ``start``: every trade since the first order carried a depth book."""
        if since == "today":
            d = ist_today()
            start: float | None = datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()
        else:
            if self.first is None:
                self.first = await self.ledger.first_depth_order()
            start = self.first
        if start is None:
            return {"since": None, "scope": since, "bookSet": books, **depth_check.summarize([], {})}
        mark = await self.ledger.trades_mark(start)
        hit = self.summaries.get((since, books))
        if hit is not None and hit[0] == start and hit[1] == mark:
            return hit[2]
        keys = _book_keys(books)
        keep = None if keys is None else {k.value for k in keys}
        rows = [t for t in await self.ledger.rows_between("trades", start, time.time() + 86_400) if keep is None or t.get("strategy") in keep]
        checks = await self.for_trades(rows)
        s = await asyncio.to_thread(depth_check.summarize, rows, checks, order=[k.value for k in ALL_KEYS])

        def label(book: str) -> str:
            try:
                return StrategyKey(book).display_name
            except ValueError:
                return book

        shadows = {k.value for k in SHADOW_BOOKS}
        for b in s["books"]:
            b["label"] = label(b["book"])
            b["shadow"] = b["book"] in shadows
            for w in b["worst"]:
                w["label"] = b["label"]
        for w in s["worst"]:
            w["label"] = label(str(w["book"]))
        out = {"since": start, "scope": since, "bookSet": books, **s}
        self.summaries[(since, books)] = (start, mark, out)
        return out


#: the review's longest span of sessions in one read (each session reads its minute bars and its ledger rows)
REVIEW_MAX_DAYS = 10
#: the NIFTY 50 index in the minute archive: the direction block reads it from its open to the trigger's close
NIFTY_SYMBOL = "NIFTY"
#: past sessions whose minute bars are kept: they no longer change
REVIEW_BAR_DAYS = 4
#: how long a read of the twin's records is used again: a past session's for longer, today's while it is still trading
TWIN_TTL_PAST_S, TWIN_TTL_TODAY_S, TWIN_TTL_FAILED_S = 600.0, 30.0, 15.0
#: the twin reads kept at most (a 10-session read is about 3 MB); an expired one is dropped on the next read
TWIN_KEEP = 8
#: a past range's answer, encoded, is sent again for this long (the page asks once for a past range; a revisit is free),
#: at most this many answers and this many bytes in all (a 10-session range with its twins is about 15 MB: not kept)
REVIEW_PAST_TTL_S = 120.0
REVIEW_ANSWERS_KEEP = 4
REVIEW_ANSWERS_MAX_BYTES = 12_000_000


def _review_days(day: str | None, to: str | None) -> list[date]:
    """The sessions a review spans: ``day`` (today if absent) to ``to`` (the same day if absent), weekends left out."""
    try:
        d0 = date.fromisoformat(day) if day else ist_today()
        d1 = date.fromisoformat(to) if to else d0
    except ValueError as exc:
        raise HTTPException(400, f"day and to must be YYYY-MM-DD, not {day!r} / {to!r}") from exc
    if d1 < d0:
        raise HTTPException(400, f"to ({d1}) is before day ({d0})")
    # 10 sessions span at most 16 calendar days: a longer span is refused before a date list is built for it
    if (d1 - d0).days > 2 * REVIEW_MAX_DAYS:
        raise HTTPException(400, f"at most {REVIEW_MAX_DAYS} sessions in one review: {d0} to {d1} is {(d1 - d0).days + 1} days")
    span = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    days = [d for d in span if d.weekday() < 5] or [d0]
    if len(days) > REVIEW_MAX_DAYS:
        raise HTTPException(400, f"at most {REVIEW_MAX_DAYS} sessions in one review, not {len(days)}")
    return days


def _merge_summaries(sums: list[dict[str, Any]]) -> dict[str, Any]:
    """The sessions' summaries as one: counts and money added, the causes merged."""
    causes: dict[str, dict[str, Any]] = {}
    for s in sums:
        for c in s.get("causes") or ():
            m = causes.setdefault(c["key"], {"key": c["key"], "label": c["label"], "trades": 0, "net": 0.0})
            m["trades"] += c["trades"]
            m["net"] += c["net"]
    out = {k: sum(s.get(k) or 0 for s in sums) for k in ("trades", "closed", "open", "losses", "wins")}
    return {**out, "net": round(sum(s.get("net") or 0.0 for s in sums), 2), "causes": sorted(causes.values(), key=lambda c: c["net"])}


def _params_of(engine: Engine) -> Any:
    """Book → its limits as the review cites them, from the exit engine that runs it (api/review_peers.py ``params_json``)."""
    def of(book: str) -> dict[str, Any]:
        ex = engine._exits_by_strategy.get(book, engine.exits)
        return review_peers.params_json(ex.limits)
    return of


def _late_symbols(positions_: list[dict[str, Any]], signals_: list[dict[str, Any]]) -> set[str]:
    """The symbols whose trigger closed at or after 15:15 (the 14:45 and 15:15 bars): the direction label reads them on
    the next session, as the study does."""
    ts_of = {str(s.get("signal_id") or ""): s.get("ts") for s in signals_}
    out = set()
    for p in positions_:
        sid = str(p.get("signal_id") or "")
        ts = ts_of.get(review.parent_signal_id(sid)) or ts_of.get(sid)
        if isinstance(ts, int | float) and review_direction.closes_late(float(ts)) and p.get("symbol"):
            out.add(str(p["symbol"]))
    return out


@dataclass(slots=True)
class ReviewReads:
    """What the review page reads besides the ledger rows: the minute bars (a past session's kept), this engine's trades as
    the same-option block compares them, and the twin's (operator, 2026-10-09: "add there insights/pnl from another engine
    or/and strategy that traded the same OTM at that time"). Record-only: nothing here writes, and the twin is read with
    one GET on loopback (``peer.read_identity``; ``peer._client``), never a twin's twin."""

    engine: Engine
    #: past session → symbol → its one-minute bars (only a whole, non-empty read is kept)
    past_bars: OrderedDict[date, dict[str, list[dict[str, Any]]]] = field(default_factory=OrderedDict)
    #: the sessions asked for → (when read, the twin's records, what to say about the read)
    twin_reads: OrderedDict[tuple[str, ...], tuple[float, list[dict[str, Any]], dict[str, Any]]] = field(default_factory=OrderedDict)
    #: a past range's answer as sent: (sessions, mirrors, peers) → (when, how long it holds, the encoded bytes)
    answers: OrderedDict[tuple[Any, ...], tuple[float, float, bytes]] = field(default_factory=OrderedDict)

    async def bars(self, symbols: set[str], day: date) -> dict[str, list[dict[str, Any]]]:
        """Each symbol's one-minute bars for ``day``: today's from the engine's store (each corrected to the broker's candle
        as its minute closes), an earlier day's from the archive, read off the event loop. A past day's symbol is kept only
        from a whole read that found bars: a read that raced the engine writing the day (phase 35's midnight compaction,
        00:03-00:10 IST; phase 34's rewrite of the day file) or found nothing is read again next time, never kept as
        "no bars" (safety review, 10 Oct)."""
        e = self.engine
        out: dict[str, list[dict[str, Any]]] = {}
        if day == ist_today():
            start = datetime(day.year, day.month, day.day, tzinfo=IST).timestamp()
            for s in symbols:
                rows = [b.to_json() for b in e.store.bars(s, "1m") if b.ts >= start]
                if rows:
                    out[s] = rows
            missing = symbols - set(out)
            if missing:
                found, _whole = await asyncio.to_thread(_archive_bars, e.s.data_dir, missing, day)
                out.update(found)
            return out
        have = self.past_bars.get(day)
        if have is None:
            have = self.past_bars[day] = {}
            while len(self.past_bars) > REVIEW_BAR_DAYS:
                self.past_bars.popitem(last=False)
        missing = symbols - set(have)
        got: dict[str, list[dict[str, Any]]] = {}
        if missing:
            got, whole = await asyncio.to_thread(_archive_bars, e.s.data_dir, missing, day)
            if whole:
                have.update({s: rows for s, rows in got.items() if rows})
        return {s: have.get(s) or got.get(s) or [] for s in symbols}

    async def records(self, positions_: list[dict[str, Any]], trades_: list[dict[str, Any]], start: float, end: float,
                      name: str) -> list[dict[str, Any]]:
        """This engine's positions of the window as the same-option block reads them, with their refused exits."""
        raw = await self.engine.ledger.exit_order_json(start - 60, end + 3 * 86_400, ("REJECTED",))
        params_of = _params_of(self.engine)
        params = {b: params_of(b) for b in {str(p.get("strategy") or "") for p in positions_}}

        def work() -> list[dict[str, Any]]:
            return review_peers.build_records(positions_, trades_, live_trades._parse(raw), engine=name,
                                              params_of=lambda b: params.get(b, {}), now=time.time())
        return await asyncio.to_thread(work)

    async def twin(self, days: list[date]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The twin's records of ``days`` and what to say about the read. A twin that is down, runs code without the read,
        or sends records of another shape (the two engines deploy and roll back apart) costs the card only its other
        engine: this engine's peers are still shown, with a quiet note. Each record is checked before it is kept
        (``review_peers.record_problem``)."""
        ident = peer.read_identity(self.engine.s.data_dir)
        if not ident.peer_url:
            return [], {"name": None, "read": False, "note": ""}
        key = tuple(d.isoformat() for d in days)
        now = time.time()
        for k in [k for k, v in self.twin_reads.items() if now - v[0] >= TWIN_TTL_PAST_S]:
            del self.twin_reads[k]
        hit = self.twin_reads.get(key)
        past = days[-1] < ist_today()
        if hit is not None:
            ttl = TWIN_TTL_FAILED_S if not hit[2]["read"] else (TWIN_TTL_PAST_S if past else TWIN_TTL_TODAY_S)
            if now - hit[0] < ttl:
                return hit[1], hit[2]
        who = ident.peer_name or "the twin"
        recs: list[dict[str, Any]] = []
        state: dict[str, Any] = {"name": ident.peer_name, "read": False, "note": ""}
        try:
            async with peer._client() as client:
                r = await client.get(f"{ident.peer_url}/api/review/legs", params={"day": key[0], "to": key[-1]},
                                     headers={"Accept": "application/json"})
            body = r.json() if r.status_code == 200 else None
            if isinstance(body, dict) and isinstance(body.get("records"), list):
                recs, bad = review_peers.checked_records(body["records"])
                state = {"name": body.get("engine") or ident.peer_name, "read": True, "note": "", "count": len(recs)}
                if bad:
                    state.update(skipped=len(bad), note=f"{len(bad)} of {who}'s records are of a shape this code does not read and are left out "
                                                         f"(first: {bad[0]})")
                    log.warning("review.twin_records_skipped", twin=who, n=len(bad), first=bad[0])
            elif r.status_code == 404:
                state["note"] = f"{who} runs code without the same-option read: only this engine's books are compared"
            else:
                state["note"] = f"{who} answered {r.status_code}: only this engine's books are compared"
        except (httpx.HTTPError, ValueError) as exc:
            state["note"] = f"{who} is not answering ({exc.__class__.__name__}): only this engine's books are compared"
        self.twin_reads[key] = (now, recs, state)
        self.twin_reads.move_to_end(key)
        while len(self.twin_reads) > TWIN_KEEP:
            self.twin_reads.popitem(last=False)
        return recs, state

    def answer(self, key: tuple[Any, ...]) -> bytes | None:
        """A past range's answer as last sent, while it holds."""
        hit = self.answers.get(key)
        if hit is None or time.time() - hit[0] >= hit[1]:
            self.answers.pop(key, None)
            return None
        return hit[2]

    def keep_answer(self, key: tuple[Any, ...], body: bytes, ttl: float) -> None:
        """Keep a past range's answer, the oldest dropped first: never more than ``REVIEW_ANSWERS_KEEP`` answers or
        ``REVIEW_ANSWERS_MAX_BYTES`` in all, so the engine's memory does not grow with what was looked at."""
        if len(body) > REVIEW_ANSWERS_MAX_BYTES:
            return
        self.answers[key] = (time.time(), ttl, body)
        self.answers.move_to_end(key)
        while len(self.answers) > REVIEW_ANSWERS_KEEP or sum(len(v[2]) for v in self.answers.values()) > REVIEW_ANSWERS_MAX_BYTES:
            self.answers.popitem(last=False)


def _rule_fields(strategy: str) -> dict[str, str]:
    """A ledger row's stop rule and the book whose trade it is (a mirror's source; any other book itself)."""
    source, rule = stop_rule_of(strategy)
    return {"stop_rule": rule, "stop_rule_label": STOP_RULE_LABELS[rule], "source_book": source,
            "source_label": describe_book(source)["strategy_label"]}


def _excursions(t: dict[str, Any]) -> dict[str, Any]:
    """MFE and MAE in money as well as R (operator, 2026-10-04: "add the actual values of MFE and MAE in the
    table"): the option's price at the best and worst moment, and the rupees open then, before charges.
    Worked back from the stored R and R unit — the position kept no price — so a figure is within half a
    thousandth of an R unit of the price seen (a paisa or less on most contracts). A row without an R unit
    has neither. ``mark_basis`` says what the marks were taken on (see ``Position.mark_basis``)."""
    r_unit, entry = float(t.get("r_unit") or 0.0), float(t.get("entry") or 0.0)
    # "bid": marked on what the position could be sold for and folded with every fill (2026-10-04 on);
    # "last": a trade closed before that — the last trade alone, which a stop's fill can sit below
    out: dict[str, float | None] = {"mark_basis": "bid" if t.get("mark_basis") == "bid" else "last"}  # type: ignore[dict-item]
    if r_unit <= 0 or entry <= 0:
        return {**out, "mfe_price": None, "mae_price": None, "mfe_inr": None, "mae_inr": None}
    sign = -1.0 if t.get("side") == "SHORT" else 1.0
    units = float(t.get("qty") or 0) * float(t.get("multiplier") or 1)
    for k in ("mfe", "mae"):
        r = float(t.get(f"{k}_r") or 0.0)
        out[f"{k}_price"] = round(entry + sign * r * r_unit, 2)
        out[f"{k}_inr"] = round(r * r_unit * units, 2)
    return out


def build_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="kotsin-nse", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    temp_page = daybook.TemporaryPage(engine.s.data_dir)
    api = APIRouter(prefix="/api")
    hot_stocks_service = HotStocksService(engine, engine.s.data_dir / "hotstocks-sectors.tsv")
    # this engine's name and its twin's books (phase 34 / phase 35 side by side, 2026-10-03)
    peer.register(api, engine.s.data_dir)

    # -- health & system ---------------------------------------------------------------------------

    @api.get("/oi/{symbol}")
    async def oi_view(symbol: str) -> dict[str, Any]:
        """One underlying's OI: the change since the previous close, each future's level, the
        reference it is measured from (and whether that is the exchange's, ours or a pre-open print),
        and its OI candles."""
        return engine.oi_view(symbol)

    @api.get("/volume/{symbol}")
    async def volume_view(symbol: str, ts: int | None = None) -> dict[str, Any]:
        """A bar's volume read two ways: against the six bars before it (what the gates read) and
        against the same time slot on earlier sessions."""
        return engine.volume_view(symbol, ts)

    @api.get("/feed/rate")
    async def feed_rate(code: str | None = None) -> dict[str, Any]:
        """How often 5paisa's price frames arrive per contract, and whether every trade arrives
        (``ops/feed_rate.py``). Meaningful only once a session has traded for a while."""
        return {"segments": engine.feed_rate.snapshot(), "code": engine.feed_rate.code(code) if code else None}

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return engine.health_snapshot()

    @api.get("/system")
    async def system() -> dict[str, Any]:
        return {
            "version": "0.1.0",
            "started_ts": engine.started_ts,
            "uptime_s": round(time.time() - engine.started_ts, 1),
            "now_ist": ist_hm(time.time()),
            "settings": {
                "segments": [s.value for s in engine.s.segment_list],
                "api_port": engine.s.api_port,
                "engine_enabled": engine.s.engine_enabled,
                "feed_enabled": engine.s.feed_enabled,
                "paper_initial_inr": engine.s.paper_initial_inr,
                "max_universe": engine.s.max_universe,
            },
            "bus": engine.bus.stats(),
            "health": engine.health_snapshot(),
            "regime": engine.regime_snapshot(),
            "counts": await engine.ledger.counts(),
        }

    @api.get("/strategies")
    async def strategies() -> dict[str, Any]:
        """Config, gate histogram and liveness for each book.

        ``signals_emitted`` is the cheapest liveness test there is. In the old stack a topic's
        lifetime end-offset of zero proved one strategy had never fired in its entire life, and
        nobody had ever run the query.
        """
        stats = engine.strategy_stats()
        hist = await engine.ledger.gate_histogram()
        pnl = await engine.ledger.pnl_by_strategy()
        last = {k.value: await engine.ledger.last_signal(k.value) for k in ALL_KEYS}

        def _last(key: str) -> dict[str, Any] | None:
            row = last.get(key)
            if not row:
                return None
            return {
                "ts": row["ts"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "grade": row.get("grade"),
                "decision": row.get("decision"),
                "signal_id": row["signal_id"],
            }
        by_strategy: dict[str, list[dict[str, Any]]] = {}
        for row in hist:
            by_strategy.setdefault(row["strategy"], []).append(
                {"gate": row["binding_gate"], "count": row["n"]}
            )
        return {
            "fudkii": {
                "key": "FUDKII",
                "config": _dc(engine.fudkii.cfg),
                "gates": stats["FUDKII"],
                "binding": by_strategy.get("FUDKII", []),
                "wallet": engine.wallets["FUDKII"].to_json(),
                "pnl": pnl.get("FUDKII"),
                "last_signal": _last("FUDKII"),
            },
            "fukaa": {
                "key": "FUKAA",
                "config": _dc(engine.fukaa.cfg),
                "gates": stats["FUKAA"],
                "binding": by_strategy.get("FUKAA", []),
                "wallet": engine.wallets["FUKAA"].to_json(),
                "pnl": pnl.get("FUKAA"),
                "last_signal": _last("FUKAA"),
                "multipliers": {
                    "N": engine.fukaa.multiplier("N"),
                    "M": engine.fukaa.multiplier("M"),
                    "C": engine.fukaa.multiplier("C"),
                },
            },
        }

    # -- book ---------------------------------------------------------------------------------------

    @api.get("/overview")
    async def overview() -> dict[str, Any]:
        open_positions = [p for p in engine.positions.values() if p.status == "OPEN"]
        # the headline is the trading books': a shadow is a comparison (the wide stop re-trades RT-Y's
        # entries, the graded-F one trades what no trading book is offered) — its money is shown apart
        shadows = {k.value for k in SHADOW_BOOKS}
        trading = [w for k, w in engine.wallets.items() if k not in shadows]
        capital = sum(w.balance for w in trading)
        # a stop-rule mirror is not a position of its own on this page: it sits under the real trade it
        # copies, or — the real trade closed, the mirror still on its own rule — in a list apart
        mirrors = {k.value for k in STOP_MIRRORS}
        real = [p for p in open_positions if p.strategy not in mirrors]
        held = {(p.strategy, p.signal_id) for p in real}
        alone = [p for p in open_positions if p.strategy in mirrors and (stop_rule_of(p.strategy)[0], p.signal_id) not in held]
        rows, closed = await _mirror_rows(engine, real, alone)
        return {
            "mode": engine.mode().value,
            "armed_until": engine._armed_until,
            "halted": engine.halted()[0],
            "halt_reason": engine.halted()[1],
            "wallets": [engine.wallets[k.value].to_json() for k in ALL_KEYS if k not in STOP_MIRRORS],
            "capital": round(capital, 2),
            "day_pnl": round(sum(w.day_pnl for w in trading), 2),
            "shadow_day_pnl": round(sum(w.day_pnl for k, w in engine.wallets.items() if k in shadows), 2),
            "positions": [
                {**_position_view(engine, p), "stopRules": rows.get(p.id)} for p in sorted(real, key=lambda x: -x.opened_ts)
            ],
            "mirrors_alone": [
                {**_position_view(engine, p), "rule": stop_rule_of(p.strategy)[1], "source": stop_rule_of(p.strategy)[0],
                 "sourceLabel": StrategyKey(stop_rule_of(p.strategy)[0]).display_name, "stopRule": rows.get(p.id, [None])[0],
                 "realExit": closed.get((stop_rule_of(p.strategy)[0], p.signal_id))}
                for p in sorted(alone, key=lambda x: -x.opened_ts)
            ],
            "mirrors_open": sum(1 for p in open_positions if p.strategy in mirrors),
            "exposure": engine.exposure.snapshot([p for p in open_positions if p.strategy not in shadows], capital),
            "universe": len(engine.underlyings),
            "boot_notes": engine.boot_notes,
        }

    #: the review's reads kept between polls: a past day's minute bars, the twin's records (api/review_peers.py)
    review_reads = ReviewReads(engine)

    @api.get("/review")
    async def trade_review(
        day: str | None = None, to: str | None = None, mirrors: bool = Query(False), peers: bool = Query(False),
    ) -> Response:
        """Every trade of a session, each from its trigger to its exit and what the stock did after, with the checks
        and the root cause of a loss (api/review.py; operator, 2026-10-05: "be accurate in identifying the root cause
        of the loss for each trade").

        Operator, 2026-10-09: "add strategy and other filters ... also add there insights/pnl from another engine or/and
        strategy that traded the same OTM at that time ... focus is on choosing the right direction of trade". So: ``to``
        reviews every session from ``day`` to it (at most ``REVIEW_MAX_DAYS``); ``mirrors=1`` adds the stop-rule mirrors as
        cards of their own; every card carries its direction block (api/review_direction.py); ``peers=1`` adds the same
        option elsewhere — every other book here and on the twin that held this contract at the same time, and for each
        that made more, the exact rupee parts and the lesson (api/review_peers.py). Record-only: the twin is read with one
        GET on loopback (``/api/review/legs``), which never reads its own twin; a twin that is down, older or sends
        records of another shape costs only its own rows.

        Where the time goes: the minute-bar reads, the review and the comparison run in worker threads; the ledger reads,
        their JSON parse and the encoding of the answer run on the event loop. The answer is encoded once
        (``live_trades.encode``, not FastAPI's validating encoder), and a past range's answer is kept
        ``REVIEW_PAST_TTL_S`` (the page asks for a past range once rather than every 20 s)."""
        days = _review_days(day, to)
        past = days[-1] < ist_today()
        key = (tuple(d.isoformat() for d in days), bool(mirrors), bool(peers))
        if past and (kept := review_reads.answer(key)) is not None:
            return Response(kept, media_type="application/json")
        name = peer.read_identity(engine.s.data_dir).name
        skip_rules, skip_note = review_direction.read_skip_rules(engine.s.data_dir)
        labels = {k.value: k.display_name for k in StrategyKey}
        mirror_books = {k.value: (src.value, rule) for k, (src, rule) in STOP_MIRRORS.items()}
        shadows = {k.value for k in SHADOW_BOOKS if k not in STOP_MIRRORS}
        cards: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        positions_all: list[dict[str, Any]] = []
        trades_all: list[dict[str, Any]] = []
        thresholds: dict[str, float] = review.THRESHOLDS
        notes: list[str] = [skip_note] if skip_note else []
        for d in days:
            start = datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()
            end = start + 86_400
            positions_, trades_, signals_, events_ = await asyncio.gather(
                engine.ledger.rows_between("positions", start, end),
                engine.ledger.rows_between("trades", start, end + 3 * 86_400),  # an MCX trade carried overnight closes later
                engine.ledger.rows_between("signals", start - 3_600, end),
                engine.ledger.rows_between("events", start - 3_600, end),
            )
            symbols = {str(p.get("symbol") or "") for p in positions_} - {""}
            bars = await review_reads.bars(symbols | {NIFTY_SYMBOL}, d) if symbols else {}
            nifty = bars.get(NIFTY_SYMBOL, [])
            # a trigger closing at or after 15:15 is labelled on the next session, as the study labels it, once it is recorded
            late = _late_symbols(positions_, signals_)
            nxt: dict[str, list[dict[str, Any]]] = {}
            nxt_day: str | None = None
            if late:
                nd = d + timedelta(days=1)
                for _ in range(30):
                    if engine.calendar.is_trading_day(nd):
                        break
                    nd += timedelta(days=1)
                if nd <= ist_today():
                    nxt, nxt_day = await review_reads.bars(late, nd), nd.isoformat()

            def annotate(card: dict[str, Any], *, signal: Any, breadth: Any, route: Any, bars: Any, book: str, nifty: Any = nifty,
                         day_s: str = d.isoformat(), nxt: dict[str, list[dict[str, Any]]] = nxt, nxt_day: str | None = nxt_day) -> dict[str, Any]:
                return {"engine": name, "day": day_s, "directionRead": review_direction.direction_read(
                    card, signal=signal, breadth=breadth, route=route, bars=bars, nifty_bars=nifty,
                    book_side=describe_book(book)["trend"], skip_rules=skip_rules, next_bars=nxt.get(str(card.get("symbol") or "")),
                    next_day=nxt_day)}

            out = await asyncio.to_thread(
                review.review_day, positions=positions_, trades=trades_, signals=signals_, events=events_, bars_by_symbol=bars,
                session_end=min(time.time(), end) if d == ist_today() else end, labels=labels, mirror_books=mirror_books,
                include_mirrors=mirrors, annotate=annotate, shadow_books=shadows,
            )
            cards += out["trades"]
            summaries.append(out["summary"])
            thresholds = out["thresholds"]
            positions_all += positions_
            trades_all += trades_
        twin_state: dict[str, Any] | None = None
        if peers:
            start = datetime(days[0].year, days[0].month, days[0].day, tzinfo=IST).timestamp()
            end = datetime(days[-1].year, days[-1].month, days[-1].day, tzinfo=IST).timestamp() + 86_400
            own = await review_reads.records(positions_all, trades_all, start, end, name)
            twin, twin_state = await review_reads.twin(days)
            if twin_state.get("note"):
                notes.append(twin_state["note"])
            mine = {r["positionId"] for r in own}
            # the twin's rows from before it ran on its own are copies of this engine's (phase 35's 22 Sep - 1 Oct): one trade, not two
            twin = [r for r in twin if r.get("positionId") not in mine]
            ev = review_peers.load_evidence(engine.s.data_dir)
            peer_name = peer.read_identity(engine.s.data_dir).peer_name or "the twin"
            unread = peer_name if (twin_state.get("name") is not None or twin_state.get("note")) and not twin_state.get("read") else ""
            try:
                blocks = await asyncio.to_thread(review_peers.peer_blocks, cards, own + twin, engine=name, now=time.time(), ev=ev,
                                                 unread=unread)
            except Exception as exc:  # the twin's records, not this engine's, are the ones that can surprise
                if not twin:
                    raise
                log.warning("review.twin_compare_failed", error=repr(exc))
                twin_state = {**twin_state, "read": False}
                notes.append(f"{peer_name}'s records could not be compared ({exc.__class__.__name__}): only this engine's books are compared")
                blocks = await asyncio.to_thread(review_peers.peer_blocks, cards, own, engine=name, now=time.time(), ev=ev,
                                                 unread=peer_name)
            for c in cards:
                c["peers"] = blocks.get(str(c.get("id") or ""))
        body = live_trades.encode({
            "day": days[0].isoformat(), "to": days[-1].isoformat(), "days": [d.isoformat() for d in days], "engine": name,
            "trades": cards, "thresholds": thresholds, "summary": _merge_summaries(summaries), "notes": notes,
            "twin": twin_state, "skipRules": bool(skip_rules), "recordOnly": True})
        if past:
            failed = bool(peers and twin_state is not None and twin_state.get("note"))
            review_reads.keep_answer(key, body, TWIN_TTL_FAILED_S if failed else REVIEW_PAST_TTL_S)
        return Response(body, media_type="application/json")

    @api.get("/review/legs")
    async def trade_review_legs(day: str | None = None, to: str | None = None) -> Response:
        """This engine's trades of ``day`` (to ``to``) as the twin's review compares them (api/review_peers.py ``record``):
        every position, its fills, its exit legs, its refused exits and its book's parameters. Read by the twin's
        ``/api/review?peers=1`` with one GET; it never reads a twin itself, so two engines cannot forward forever."""
        days = _review_days(day, to)
        start = datetime(days[0].year, days[0].month, days[0].day, tzinfo=IST).timestamp()
        end = datetime(days[-1].year, days[-1].month, days[-1].day, tzinfo=IST).timestamp() + 86_400
        name = peer.read_identity(engine.s.data_dir).name
        positions_, trades_ = await asyncio.gather(
            engine.ledger.rows_between("positions", start, end), engine.ledger.rows_between("trades", start, end + 3 * 86_400))
        recs = await review_reads.records(positions_, trades_, start, end, name)
        return Response(live_trades.encode({"engine": name, "days": [d.isoformat() for d in days], "records": recs, "recordOnly": True}),
                        media_type="application/json")

    @api.get("/stop-rules")
    async def stop_rules(since: str = Query("today", pattern="^(today|start)$")) -> dict[str, Any]:
        """Every book against its two stop-rule mirrors (operator, 2026-10-04: "do i also see the bifurcation in
        overview? per combination"): per book and rule the trades closed under all three rules — net, wins,
        stops, each mirror against the current stop — and those still open, with their open P&L now. ``today``:
        the trades opened this session; ``start``: every trade since the mirrors began."""
        now = time.time()
        if since == "today":
            d = ist_today()
            start: float | None = datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()
        else:
            start = await engine.ledger.first_opened([k.value for k in STOP_MIRRORS])
        if start is None:
            return {"since": None, "scope": since, "rules": STOP_RULE_LABELS, "books": [], "total": None}
        pos_rows, trade_rows = await asyncio.gather(
            engine.ledger.rows_between("positions", start, now + 86_400), engine.ledger.rows_between("trades", start, now + 86_400)
        )
        s = shadow.stop_rules_summary(positions=pos_rows, trades=trade_rows)
        live: dict[tuple[str, str], float] = {}
        for p in engine.positions.values():
            if p.status != "OPEN" or p.opened_ts < start:
                continue
            ltp = engine.ltps.get(p.instrument.scrip_code)
            if ltp:
                k = stop_rule_of(p.strategy)
                live[k] = live.get(k, 0.0) + p.unrealized(ltp) + p.realised_gross
        books = [
            {"book": b, "label": StrategyKey(b).display_name, "shadow": StrategyKey(b) in SHADOW_BOOKS,
             "cells": {rule: {**c, "openGross": round(live[(b, rule)], 2) if (b, rule) in live else None} for rule, c in tally.items()}}
            for b, tally in s["books"].items()
        ]
        trading_live = {rule: sum(v for (b, r), v in live.items() if r == rule and StrategyKey(b) not in SHADOW_BOOKS) for rule in STOP_RULE_LABELS}
        total = {rule: {**c, "openGross": round(trading_live[rule], 2)} for rule, c in s["total_trading"].items()}
        return {"since": start, "scope": since, "rules": STOP_RULE_LABELS, "books": books, "total": total}

    @api.get("/positions")
    async def positions() -> list[dict[str, Any]]:
        out = []
        for p in engine.positions.values():
            row = _position_view(engine, p)
            # The exit loop's own numbers, not a second computation: if a stop is breached on this
            # row it is breached in the engine too.
            row["marks"] = engine.position_marks.get(p.id)
            out.append(row)
        return out

    #: the active-trades page's reads (api/live_trades.py): each signal's trigger and levels are read once
    live = live_trades.LiveTrades(engine, position_view=_position_view, rule_fields=_rule_fields, entry_sources=ENTRY_SOURCES)

    @api.get("/positions/live")
    async def positions_live() -> Response:
        """Every open position of this engine, every book — the shadows and the stop-rule mirrors too — one card each
        (operator, 2026-10-09: "in the dashabpord have a dedicated page for all actove trades ... in the form of cards"):
        the contract, its lots, the money so far and an exit now would cost, the order book's age against the paper
        matcher's own limit, the trigger and which way the stock has gone since, and the trade map (api/live_trades.py).
        Record-only: it reads, it never changes a trade, an order, a stop or a wallet. Polled every few seconds by both
        engines' pages, so it is kept light: the stored rows it reads are few and parsed off the event loop, and the answer
        goes out as plain JSON (60 rows: FastAPI's encoder took 10 ms of the loop for what json.dumps does in 1.4), a value
        that is not a number made null rather than failing the page (``live_trades.encode``)."""
        return Response(live_trades.encode(await live.view()), media_type="application/json")

    @api.get("/signals")
    async def recent_signals(limit: int = Query(100, le=500), unpublished: bool = False) -> list[dict[str, Any]]:
        # a trigger FUDKII graded F and did not publish (~20 at a busy bar) is listed only when asked
        # for: it would push the day's real signals out of the window (review, 2026-09-29)
        where = None if unpublished else signals.c.decision != "NOT_PUBLISHED"
        return await engine.ledger.recent(signals, limit, where=where)

    @api.get("/rejections")
    async def recent_rejections(
        limit: int = Query(100, le=500), strategy: str | None = None
    ) -> list[dict[str, Any]]:
        where = rejections.c.strategy == strategy if strategy else None
        return await engine.ledger.recent(rejections, limit, where=where)

    #: the depth check's reads, kept for the app's life: a closed trade's check is worked out once
    depth_reads = DepthReads(engine.ledger)
    #: "was the order book stale on the way out", read from the stored exits, also worked out once per closed trade
    stale_reads = live_trades.StaleBookReads(engine.ledger, limit_ms_at=engine.matcher.age_limit_ms)

    @api.get("/trades")
    async def recent_trades(
        limit: int = Query(100, le=500), books: str = Query("all", pattern=BOOK_SETS), depth: bool = Query(False),
        stale: bool = Query(False),
    ) -> list[dict[str, Any]]:
        # the book's name, its side of the trigger and its stop rule come from the registry as the rows are
        # served: rows already stored never carried them
        rows = await engine.ledger.recent(trades, limit, order_col="closed_ts", where=_books_where(books))
        # ``depth=1``, asked by the Trades page alone: each trade's fills against the books recorded with them, read
        # here, never stored, never acted on. Any other caller (the Signals page polls 500 rows) neither pays for it
        # nor gets it (review, 9 Oct 2026)
        checks: dict[str, dict[str, Any]] | None = None
        if depth:
            try:
                checks = await depth_reads.for_trades(rows)
            except Exception as exc:  # noqa: BLE001 - a record-only column must never cost the ledger its page
                log.warning("depth_check.failed", error=str(exc)[:160])
                checks = {}
        # ``staleBook`` (operator, 2026-10-09: "also in reason for exit: that orderbook was stale"): the exits refused for a
        # stale book and the age of the book the closing exit filled on — read from the stored orders here, beside the
        # exit reason, never written into it. With the depth check (the Trades page) or on its own (``stale=1``)
        stale_books: dict[str, dict[str, Any]] | None = None
        if depth or stale:
            try:
                stale_books = await stale_reads.for_trades(rows)
            except Exception as exc:  # noqa: BLE001 - a record-only column must never cost the ledger its page
                log.warning("stale_book.failed", error=str(exc)[:160])
                stale_books = {}
        return [
            {**t, **describe_book(str(t.get("strategy") or "")), **_rule_fields(str(t.get("strategy") or "")), **_excursions(t),
             **({"depthCheck": checks.get(str(t.get("id")))} if checks is not None else {}),
             **({"staleBook": stale_books.get(str(t.get("id")))} if stale_books is not None else {})}
            for t in rows
        ]

    @api.get("/depth-check")
    async def depth_check_view(
        since: str = Query("today", pattern="^(today|start)$"), books: str = Query("trading", pattern=BOOK_SETS)
    ) -> dict[str, Any]:
        """The paper fills against the books recorded with them, per book (operator, 2026-10-09: "go ahead build it
        n tell me where to view it"). For each book: the closed trades, their fills, how many were judged on a book,
        each verdict (full / partial / thin / print / no book), the total rupees the paper fills were kinder than that
        book, and the ten trades it flattered most. A fill on a print is counted, never priced; a fill several books
        share (a mirror's copied entry) counts once in the total. ``today`` covers trades closed this session;
        ``start`` every trade since the first order carried a depth book. Record-only: nothing here reaches an
        order, a stop, a wallet or a decision."""
        try:
            return await depth_reads.summary(since, books)
        except Exception as exc:  # said plainly on the card, never a stack trace
            log.warning("depth_check.summary_failed", error=str(exc)[:160])
            raise HTTPException(503, f"the depth check could not be read: {str(exc)[:160]}") from exc

    @api.get("/events")
    async def recent_events(limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
        return await engine.ledger.recent(events, limit)

    @api.get("/pnl")
    async def pnl(books: str = Query("all", pattern=BOOK_SETS)) -> dict[str, Any]:
        """Net, gross and charges, split. Keeping them apart is the only way the cost structure
        stays visible — on the old book 81% of the round trip was flat brokerage. ``books`` picks the
        set summed: a stop-rule mirror re-trades its book's fills, so adding it in counts a trade thrice."""
        rows = await engine.ledger.recent(trades, 1000, order_col="closed_ts", where=_books_where(books))
        if not rows:
            return {"trades": 0}
        gross = sum(r["gross"] for r in rows)
        charges = sum(r["charges"] for r in rows)
        net = sum(r["net"] for r in rows)
        wins = [r for r in rows if r["net"] > 0]
        by_reason: dict[str, dict[str, float]] = {}
        for r in rows:
            b = by_reason.setdefault(r["exit_reason"], {"n": 0, "net": 0.0})
            b["n"] += 1
            b["net"] += r["net"]
        return {
            "trades": len(rows),
            "gross": round(gross, 2),
            "charges": round(charges, 2),
            "net": round(net, 2),
            "charges_share_of_gross": round(charges / abs(gross) * 100, 1) if gross else None,
            "win_rate": round(len(wins) / len(rows) * 100, 1),
            "avg_r": round(sum(r["r_multiple"] for r in rows) / len(rows), 3),
            "by_exit_reason": {
                k: {"n": v["n"], "net": round(v["net"], 2)} for k, v in sorted(by_reason.items())
            },
        }

    # -- market -----------------------------------------------------------------------------------

    # -- review committee ---------------------------------------------------------------------------
    # The GETs read state and cost nothing (forensics is deterministic). The review POSTs spend
    # Claude calls, bounded by KN_COMMITTEE_MAX_RUNS_PER_DAY; the experiment POST runs the
    # backtester in a worker thread. None of them can touch a position.

    @api.get("/committee/status")
    async def committee_status() -> dict[str, Any]:
        return engine.committee.status()

    @api.get("/committee/forensics")
    async def committee_forensics(
        source: str = "ledger", strategy: str | None = None
    ) -> dict[str, Any]:
        try:
            return await engine.committee.forensics(source, strategy or None)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/committee/reviews")
    async def committee_reviews(
        limit: int = Query(50, le=500), kind: str | None = None
    ) -> list[dict[str, Any]]:
        from ..committee.service import public

        rows = [e for e in engine.committee.log.entries if not kind or e.get("kind") == kind]
        return [public(e) for e in rows[-limit:]][::-1]

    @api.get("/committee/reviews/{review_id}")
    async def committee_review(review_id: str) -> dict[str, Any]:
        e = engine.committee.log.get(review_id)
        if e is None:
            raise HTTPException(404, "unknown review")
        return e

    @api.get("/committee/hypotheses")
    async def committee_hypotheses() -> list[dict[str, Any]]:
        return engine.committee.log.hypotheses()[::-1]

    @api.get("/books/{book}/cards")
    async def book_cards(book: str, day: str | None = None) -> dict[str, Any]:
        """One card per FUDKII trigger of the session, as this book saw it."""
        try:
            d = date.fromisoformat(day) if day else None
            return await engine.book_cards(book.upper(), d)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/books/{book}/take")
    async def book_take(book: str, req: BookActionRequest) -> dict[str, Any]:
        try:
            return await engine.operator_take(book.upper(), req.signal_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/books/{book}/skip")
    async def book_skip(book: str, req: BookActionRequest) -> dict[str, Any]:
        try:
            return await engine.operator_skip(book.upper(), req.signal_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/wallets/{strategy}/reset")
    async def wallet_reset(strategy: str, req: WalletResetRequest | None = None) -> dict[str, Any]:
        """Start a book's purse over at ``initial`` (default: the book's opening capital). Refused
        with 409 while the book holds an open position."""
        try:
            w = await engine.reset_wallet(strategy.upper(), req.initial if req else None)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return w.to_json()

    @api.post("/committee/review/signal")
    async def committee_review_signal(req: ReviewSignalRequest) -> dict[str, Any]:
        try:
            return await engine.committee.review_signal(req.signal_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/committee/review/trade")
    async def committee_review_trade(req: ReviewTradeRequest) -> dict[str, Any]:
        try:
            return await engine.committee.review_backtest_trade(req.run_id, req.index)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/committee/review/cohort")
    async def committee_review_cohort(req: ReviewCohortRequest) -> dict[str, Any]:
        try:
            return await engine.committee.review_cohort(req.source, req.strategy or None)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/committee/hypotheses")
    async def committee_propose(req: ProposeRequest) -> dict[str, Any]:
        """A person's hypothesis, graded exactly like the committee's — the loop needs no key."""
        from ..committee.schemas import ParamChange

        try:
            changes = [ParamChange(path=str(c["path"]), value=float(c["value"])) for c in req.changes]
            return engine.committee.propose(
                title=req.title,
                changes=changes,
                expected=req.expected,
                rationale=req.rationale,
                segment=req.segment,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/committee/autopilot/run")
    async def committee_autopilot(req: AutopilotRequest) -> dict[str, Any]:
        """One night's loop now: cohort review → veto → experiments → memory. Blocks until done."""
        try:
            return await engine.committee.autopilot_once(req.source)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/committee/experiments/run")
    async def committee_run_experiment(req: ExperimentRequest) -> dict[str, Any]:
        try:
            return engine.committee.start_experiment(req.hypothesis_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/universe")
    async def universe() -> list[dict[str, Any]]:
        out = []
        for sym, inst in sorted(engine.underlyings.items()):
            g = engine.groups.get(sym)
            out.append(
                {
                    "symbol": sym,
                    "scrip_code": inst.scrip_code,
                    "segment": inst.segment.value,
                    "kind": inst.kind.value,
                    "ltp": engine.ltps.get(inst.scrip_code),
                    "bars_30m": engine.store.count(sym, "30m"),
                    "bars_1d": engine.store.count(sym, "1d"),
                    "zones": len(engine.zones_for(sym)),
                    "futures": [f.scrip_code for f in g.futures] if g else [],
                    "options": len(g.options) if g else 0,
                    "option_expiry": g.option_expiry if g else None,
                    "prev_close": g.close if g else None,
                    "note": g.note if g else "",
                }
            )
        return out

    @api.get("/groups/{symbol}")
    async def group(symbol: str) -> dict[str, Any]:
        g = engine.groups.get(symbol.upper())
        if g is None:
            raise HTTPException(404, f"{symbol} is not in the universe")
        return {
            **g.to_json(),
            "futures_detail": [
                {"scrip_code": f.scrip_code, "expiry": f.expiry, "ltp": engine.ltps.get(f.scrip_code)}
                for f in g.futures
            ],
            "options_detail": [
                {
                    "scrip_code": o.scrip_code,
                    "strike": o.strike,
                    "type": o.option_type.value,
                    "ltp": engine.ltps.get(o.scrip_code),
                    "oi": engine.option_oi.get(o.scrip_code),
                }
                for o in g.options
            ],
        }

    @api.get("/micro/{symbol}")
    async def micro(symbol: str) -> dict[str, Any]:
        """Book-derived microstructure for the underlying, live. No tape on this venue, so no
        Kyle λ and no VPIN — the response says so rather than inventing them."""
        inst = engine.underlyings.get(symbol.upper())
        if inst is None:
            raise HTTPException(404, f"{symbol} is not in the universe")
        return {"symbol": symbol.upper(), "live": engine.micro.live(inst.scrip_code), **engine.micro.stats()}

    @api.get("/fidelity")
    async def fidelity() -> dict[str, Any]:
        """How often the live bar build disagrees with the exchange's own candle, by timeframe."""
        return engine.reconciler.snapshot()

    @api.get("/all-strategies")
    async def all_strategies() -> dict[str, Any]:
        """Every book the old stack ran, with live state for the two this engine computes.

        A tab whose book is not ported says so. The alternative — an empty tab fed by an endpoint
        that returns ``[]`` — is indistinguishable from a quiet market, and that ambiguity is what
        let a strategy sit dead for six weeks in the old stack with a dashboard showing no signals.
        """
        stats = engine.strategy_stats()
        hist = await engine.ledger.gate_histogram()
        pnl = await engine.ledger.pnl_by_strategy()
        binding: dict[str, list[dict[str, Any]]] = {}
        for row in hist:
            binding.setdefault(row["strategy"], []).append(
                {"gate": row["binding_gate"], "count": row["n"]}
            )

        cfg = {"FUDKII": _dc(engine.fudkii.cfg), "FUKAA": _dc(engine.fukaa.cfg)}
        open_by_strategy: dict[str, int] = {}
        for p in engine.positions.values():
            if p.status == "OPEN":
                open_by_strategy[p.strategy] = open_by_strategy.get(p.strategy, 0) + 1

        books = []
        for b in BOOKS:
            row = b.to_json()
            if b.status == "live":
                row["live"] = {
                    "config": cfg.get(b.key, {}),
                    "gates": stats.get(b.key, {}),
                    "binding": binding.get(b.key, []),
                    "wallet": engine.wallets[b.key].to_json() if b.key in engine.wallets else None,
                    "pnl": pnl.get(b.key),
                    "last_signal": await engine.ledger.last_signal(b.key),
                    "open_positions": open_by_strategy.get(b.key, 0),
                }
            books.append(row)

        return {
            "books": books,
            "live": list(LIVE_KEYS),
            "ported": sum(1 for b in BOOKS if b.status == "live"),
            "total": len(BOOKS),
            "mode": engine.mode().value,
            "universe": len(engine.underlyings),
            "segments": [s.value for s in engine.s.segment_list],
        }

    @api.get("/alerts")
    async def alerts(book: str | None = None, limit: int | None = Query(None, ge=1)) -> dict[str, Any]:
        """Every alert of the session by default. ``limit`` is a caller's choice, not a policy:
        the page shows the whole day and the rings are emptied at 00:30 IST."""
        """Realtime firings from the ported books. Advisory: none of these can place an order."""
        return {
            "alerts": engine.alerts.feed(book, limit),
            "resetIst": engine.s.alerts_reset_ist,
            **engine.alerts.stats(),
            "now_ist": ist_hm(time.time()),
        }

    @api.get("/fudkii/today")
    async def fudkii_today() -> dict[str, Any]:
        """Every FUDKII signal of the session from 09:00 IST, as the ledger holds it — live ones
        with what the books did, and the ones the rescan found with why nothing traded them."""
        rows = await engine.fudkii_today()
        # a trigger FUDKII graded F and did not publish is listed, not counted as its signal
        unpublished = sum(1 for r in rows if r.get("decision") == "NOT_PUBLISHED")
        return {"count": len(rows) - unpublished, "unpublished": unpublished, "signals": rows, "lastScan": engine.last_fudkii_scan}

    @api.post("/fudkii/scan")
    async def fudkii_scan() -> dict[str, Any]:
        """Run the rescan now rather than at its five-minute tick."""
        return await engine.scan_fudkii()

    @api.get("/leg-pivots")
    async def leg_pivots() -> dict[str, Any]:
        """Previous-session pivots for the legs actually traded — future and OTM strikes."""
        return engine.leg_pivots.stats()

    @api.get("/leg-pivots/{symbol}")
    async def leg_pivots_for(symbol: str) -> dict[str, Any]:
        g = engine.groups.get(symbol.upper())
        if g is None:
            raise HTTPException(404, f"{symbol} is not in the universe")
        # Every leg loaded for this root, not only the subscribed shortlist: the ladders are
        # computed on the full OTM set, and showing the subscribed few would hide most of them.
        rows = [lp.to_json() for lp in engine.leg_pivots.for_root(symbol.upper())]
        return {
            "symbol": symbol.upper(),
            "underlyingZones": [
                {"price": round(z.price, 2), "strength": round(z.strength, 2), "members": z.members}
                for z in engine.zones_for(symbol.upper())
            ],
            "legs": sorted(rows, key=lambda r: (r["kind"], r["strike"])),
            **engine.leg_pivots.stats(),
        }

    # -- hot stocks -------------------------------------------------------------------------------

    @api.get("/hot-stocks")
    async def hot_stocks(refresh: bool = False) -> dict[str, Any]:
        """CAN1: the F&O universe ranked on the v2 score.

        The score's flow, delivery and relative-strength buckets are built from NSE's own published
        data (bhavcopy, disclosed deals, index levels) — not the broker, which serves none of it.
        Anything the exchange did not publish is reported in ``unavailable`` and scores zero in its
        bucket rather than being guessed at.
        """
        return await hot_stocks_service.rank(force=refresh)

    @api.get("/can2")
    async def can2() -> dict[str, Any]:
        """The live book beside CAN1 — this engine's own open positions and wallets."""
        return hot_stocks_service.live_book(
            [
                _position_view(engine, p)
                for p in engine.positions.values()
                if p.status == "OPEN"
            ]
        )

    @api.get("/hot-stocks/{symbol}")
    async def hot_stock(symbol: str) -> dict[str, Any]:
        book = await hot_stocks_service.rank()
        for c in book["fno"] + book["nonFno"]:
            if c["symbol"] == symbol.upper():
                return c
        raise HTTPException(404, f"{symbol} is not ranked")

    @api.get("/bars/{symbol}")
    async def bars(symbol: str, tf: str = "30m", n: int = Query(200, le=1000)) -> dict[str, Any]:
        if tf != "1d" and tf not in TF_SECONDS:
            raise HTTPException(400, f"unknown timeframe {tf!r}; known: {sorted(TF_SECONDS)} or 1d")
        rows = engine.store.bars(symbol.upper(), tf, n)
        if not rows:
            raise HTTPException(404, f"no {tf} bars for {symbol}")
        forming = engine.store.forming(symbol.upper(), tf)
        return {
            "symbol": symbol.upper(),
            "tf": tf,
            "bars": [_bar_view(b) for b in rows],
            "forming": _bar_view(forming) if forming else None,
            "zones": [
                {"price": round(z.price, 2), "strength": round(z.strength, 2), "members": z.members}
                for z in engine.zones_for(symbol.upper())
            ],
        }

    @api.get("/volume/relative")
    async def volume_relative(ts: int | None = None) -> dict[str, Any]:
        """Every NSE stock's volume at a closed 30m bar (the last by default): against its own usual half hour
        and against the NIFTY50 at the same bar, highest first (``Engine.volume_table``)."""
        return engine.volume_table(ts)

    @api.get("/indicators/{symbol}")
    async def indicators(symbol: str, tf: str = "30m", n: int = Query(300, le=1000)) -> dict[str, Any]:
        """Bollinger and SuperTrend per bar — computed by the SAME functions, with the SAME live
        config, that FUDKII decides on. Not a charting-library reimplementation: if these lines
        disagree with a signal, the signal is wrong, not the chart."""
        cfg = engine.fudkii.cfg
        sym = symbol.upper()
        # every shown bar has the converged window behind it, as FUDKII's decision does
        bars = engine.store.bars(sym, tf, n + SUPERTREND_CONVERGED_BARS)
        if not bars:
            raise HTTPException(404, f"no {tf} bars for {sym}")
        closes = [b.close for b in bars]
        st_pts = supertrend(bars, cfg.st_atr_period, cfg.st_mult)
        rows: list[dict[str, Any]] = []
        for i, b in enumerate(bars):
            bb = bollinger(closes[: i + 1], cfg.bb_period, cfg.bb_mult)
            p = st_pts[i]
            rows.append(
                {
                    "ts": b.ts,
                    "bb_upper": round(bb.upper, 4) if bb else None,
                    "bb_middle": round(bb.middle, 4) if bb else None,
                    "bb_lower": round(bb.lower, 4) if bb else None,
                    "st_value": round(p.value, 4) if p else None,
                    "st_trend": p.trend if p else None,
                }
            )
        return {
            "symbol": sym,
            "tf": tf,
            "params": {
                "bb_period": cfg.bb_period,
                "bb_mult": cfg.bb_mult,
                "st_atr_period": cfg.st_atr_period,
                "st_mult": cfg.st_mult,
            },
            "rows": rows[-n:],
        }

    @api.get("/chain/{symbol}")
    async def chain(symbol: str, expiry: str | None = None) -> dict[str, Any]:
        """The option chain, plus **which strike the engine would actually buy right now**.

        The second half is the point. The rule is "OTM at entry, roughly ATM at the confluence T1",
        and it is the step that decides what a signal costs — a chain you can look at but whose
        selection you cannot see is how an enrichment step quietly picks a strike nobody would have
        chosen. This runs the real `select_option` against the live quotes for both directions and
        reports the choice, or the reason there isn't one.
        """
        from ..domain import Direction, OptionType
        from ..instrument.select import choose_expiry, estimate_delta, select_option

        cat = engine.catalogue_loader.catalogue
        sym = symbol.upper()
        exps = cat.expiries(sym)
        if not exps:
            raise HTTPException(
                404,
                f"no option chain for {sym} — the chain comes from the scrip master, which needs a "
                f"broker session (see boot notes)",
            )
        chosen_expiry = expiry if expiry in exps else (
            choose_expiry(exps, date.today(), Engine.selection_policy_for(StrategyKey.FUDKII)) or exps[0]
        )
        underlying = engine.underlyings.get(sym)
        spot = engine.ltps.get(underlying.scrip_code) if underlying else None

        by_strike: dict[float, dict[str, Any]] = {}
        for ot in ("CE", "PE"):
            for inst in cat.chain(sym, chosen_expiry, OptionType(ot)):
                q = engine.quotes.get(inst.scrip_code)
                row = by_strike.setdefault(
                    inst.strike, {"strike": inst.strike, "lot_size": inst.lot_size}
                )
                row[ot] = {
                    "scrip_code": inst.scrip_code,
                    "ltp": q.ltp if q else None,
                    "bid": q.bid if q else None,
                    "ask": q.ask if q else None,
                    "spread_pct": round(q.spread_pct, 2) if q and q.spread_pct else None,
                    "age_s": round(time.time() - q.ts, 1) if q else None,
                    "delta_est": round(
                        estimate_delta(spot=spot or 0, strike=inst.strike, option_type=OptionType(ot)), 3
                    )
                    if spot
                    else None,
                }

        selection: dict[str, Any] = {}
        if spot:
            bars = engine.store.bars(sym, "30m", 1)
            for direction in (Direction.BULLISH, Direction.BEARISH):
                rows_for = cat.chain(sym, chosen_expiry, direction.option_type)
                sel = select_option(
                    chain=rows_for,
                    quotes=engine.quotes,
                    spot=spot,
                    target1=None,
                    direction=direction,
                    now=time.time(),
                    policy=Engine.selection_policy_for(StrategyKey.FUDKII),
                )
                selection[direction.value] = {
                    "ok": sel.ok,
                    "reason": sel.reason,
                    "anchor": sel.anchor,
                    "strike": sel.instrument.strike if sel.instrument else None,
                    "scrip_code": sel.instrument.scrip_code if sel.instrument else None,
                    "name": sel.instrument.name if sel.instrument else None,
                    "premium": sel.premium,
                    "spread_pct": sel.spread_pct,
                }
            _ = bars

        fam = Engine.selection_policy_for(StrategyKey.FUDKII)
        return {
            "symbol": sym,
            "spot": spot,
            "expiry": chosen_expiry,
            "expiries": exps,
            "rows": [by_strike[k] for k in sorted(by_strike)],
            "selection": selection,
            "stockIv": engine.stock_iv_snapshot(sym),
            # the FUDKII family's policy (no premium floor); FUKAA keeps the shared one
            "policy": {
                "min_days_to_expiry": fam.min_days_to_expiry,
                "min_premium": fam.min_premium,
                "max_premium": fam.max_premium,
                "max_spread_pct": fam.max_spread_pct,
                "max_quote_age_s": fam.max_quote_age_s,
                "shared_min_premium": SELECTION_POLICY.min_premium,
            },
        }

    # -- research -----------------------------------------------------------------------------------

    @api.get("/backtests")
    async def backtests(limit: int = Query(25, le=100)) -> list[dict[str, Any]]:
        """Saved backtest summaries, newest first.

        Read-only: a backtest is run from the CLI (`kotsin-nse backtest`), not from the UI. A long
        sweep should not be able to compete with the trading loop for the same event loop.
        """
        from ..research.backtest import load_summaries

        return load_summaries(engine.s.data_dir / "backtests", limit)

    @api.get("/backtests/{run_id}")
    async def backtest_detail(run_id: str) -> dict[str, Any]:
        from ..research.backtest import load_run

        path = engine.s.data_dir / "backtests" / f"{run_id}.json"
        if not path.exists() or ".." in run_id or "/" in run_id:
            raise HTTPException(404, f"no backtest {run_id}")
        run = load_run(path)
        # the decision payloads (`details`) are served one trade at a time by /trades/{index}
        return {"summary": run.get("summary"), "trades": run.get("trades") or []}

    @api.get("/backtests/{run_id}/trades/{index}")
    async def backtest_trade(
        run_id: str, index: int, before: int = Query(80, le=400), after: int = Query(16, le=200)
    ) -> dict[str, Any]:
        """One trade, explained — bars, the strategy's own lines, zones, levels, path, FUKAA's
        verdict. Computed by research.debug from the stored decision; served, not derived, here."""
        from ..research.debug import trade_view
        from ..research.history import HistoryStore

        path = engine.s.data_dir / "backtests" / f"{run_id}.json"
        if not path.exists() or ".." in run_id or "/" in run_id:
            raise HTTPException(404, f"no backtest {run_id}")
        try:
            return await asyncio.to_thread(
                trade_view, path, index, HistoryStore(engine.s.data_dir / "history"), before=before, after=after
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/rl/runs")
    async def rl_runs(limit: int = Query(25, le=100)) -> list[dict[str, Any]]:
        from ..research.rl.exit_policy import list_runs

        return list_runs(engine.s.data_dir / "rl", limit)

    @api.get("/rl/runs/{name}")
    async def rl_run(name: str) -> dict[str, Any]:
        from ..research.rl.exit_policy import load_run as load_rl_run

        try:
            return load_rl_run(engine.s.data_dir / "rl", name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/history")
    async def history_coverage() -> list[dict[str, Any]]:
        from ..research.history import HistoryStore

        store = HistoryStore(engine.s.data_dir / "history")
        out: list[dict[str, Any]] = []
        for tf in ("30m", "1d"):
            for sym in store.symbols(tf):
                cov = store.coverage(sym, tf)
                out.append(
                    {
                        "symbol": sym,
                        "tf": tf,
                        "from": cov[0] if cov else None,
                        "to": cov[1] if cov else None,
                        "bars": len(store.load(sym, tf)),
                    }
                )
        return out

    # -- control -----------------------------------------------------------------------------------

    @api.post("/control/mode")
    async def set_mode(req: ModeRequest) -> dict[str, Any]:
        try:
            mode = Mode(req.mode.upper())
        except ValueError as exc:
            raise HTTPException(400, f"unknown mode {req.mode!r}") from exc
        try:
            return await engine.set_mode(mode, armed_minutes=req.armed_minutes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/control/halt")
    async def set_halt(req: HaltRequest) -> dict[str, Any]:
        await engine.set_halt(req.halted, req.reason)
        return {"halted": req.halted, "reason": req.reason}

    @api.post("/control/kill")
    async def kill() -> dict[str, Any]:
        """Halt, then flatten everything through the broker's own bulk square-off.

        Uses the broker's square-off rather than our position list on purpose: the moment this
        button is pressed is exactly the moment our view of the book is least trustworthy.
        """
        await engine.set_halt(True, "KILL")
        flattened = 0
        if engine.live_exec is not None and engine.mode() in (Mode.LIVE, Mode.LIVE_CAPPED):
            await engine.live_exec.square_off_all()
            flattened = len([p for p in engine.positions.values() if p.status == "OPEN"])
        return {"halted": True, "square_off_requested": flattened}

    @api.post("/control/reconcile")
    async def reconcile() -> dict[str, Any]:
        """The broker's positions against the LIVE books' (the position reconciler, not the bar one)."""
        try:
            return await engine.reconcile_now()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/control/acknowledge")
    async def acknowledge() -> dict[str, Any]:
        try:
            return await engine.acknowledge_reconcile()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/temporary")
    async def temporary_create(hours: float = Query(daybook.TTL_HOURS, gt=0, le=168)) -> dict[str, Any]:
        """Open (or reopen) the shareable day-book page for today's session."""
        t = temp_page.create(ist_today(), hours=hours)
        return {"url": "/temporary", **t.to_json()}

    @api.delete("/temporary")
    async def temporary_revoke() -> dict[str, Any]:
        temp_page.revoke()
        return {"revoked": True}

    @api.get("/temporary")
    async def temporary_status() -> dict[str, Any]:
        t = temp_page.read()
        return {"live": t is not None, **(t.to_json() if t else {})}

    @api.post("/control/reset-breaker")
    async def reset_breaker(book: str | None = Query(None)) -> dict[str, Any]:
        """Reset one book's order breaker (``?book=FUDKII_RT_X``), or every book's."""
        return await engine.reset_breaker(book)

    app.include_router(api)

    def _gap_market(symbols: set[str], day: date) -> dict[str, dict[str, float]]:
        """Per symbol: the previous official close, the daily ATR and today's session open — the
        three inputs the gap readings need and a signal row does not carry. Best effort: a symbol
        the store cannot answer for simply has no gap columns."""
        out: dict[str, dict[str, float]] = {}
        for sym in symbols:
            dailies = engine.store.bars(sym, "1d")
            prev = previous_session(dailies, day)
            intraday = engine.store.bars(sym, "30m", 40)
            todays = [b for b in intraday if ist_day(b.ts) == day]
            if prev is None or not todays:
                continue
            out[sym] = {
                "prev_close": prev.close,
                "atr1d": atr(list(dailies), 14) or 0.0,
                "open": todays[0].open,
            }
        return out

    @app.get("/temporary", response_class=HTMLResponse)
    async def temporary_page() -> HTMLResponse:
        """The shareable day book. Renders from the ledger on every request, and is gone the first
        time it is asked for after its expiry — the read deletes the ticket, so nothing lingers."""
        page = await _temporary_html()
        if page is None:
            return HTMLResponse(daybook.EXPIRED_HTML, status_code=410)
        return HTMLResponse(page)

    @app.get("/temporary.xlsx")
    async def temporary_xlsx() -> Response:
        """The same page as a workbook — one sheet per table, prose on a Notes sheet. Read off the
        rendered HTML, so the download is exactly what the page shows (operator, 2026-09-25:
        anything shared on /temporary must also download in a format that fits it)."""
        page = await _temporary_html()
        if page is None:
            return HTMLResponse(daybook.EXPIRED_HTML, status_code=410)
        title, _, _ = export.tables_from_html(page)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "temporary"
        return Response(
            content=export.html_to_xlsx(page),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{slug}.xlsx"', "Cache-Control": "no-store"},
        )

    async def _shadow(day_s: str | None) -> shadow.ShadowData:
        """Every shadow tab's data: one session's triggers with their labels — the day asked for,
        else today, else the latest day that has any trigger — and the running tallies (the RT-Y
        A/B, the wide stop, the gap fade, CT-M's market fade, CT-B1's B1 gap fade, FUKAA's shadow, the labels) since the A/B began."""
        today = ist_today()
        lookback = datetime(today.year, today.month, today.day, tzinfo=IST).timestamp() - 21 * 86_400
        recent = await engine.ledger.rows_between("signals", lookback, time.time() + 86_400)
        days = sorted({ist_day(float(r["ts"])).isoformat() for r in recent if r.get("strategy") == "FUDKII"})
        if day_s:
            try:
                day = date.fromisoformat(day_s)
            except ValueError as exc:
                raise HTTPException(400, f"day must be YYYY-MM-DD, not {day_s!r}") from exc
        else:
            day = today if (not days or today.isoformat() in days) else date.fromisoformat(days[-1])
        start = datetime(day.year, day.month, day.day, tzinfo=IST).timestamp()
        names = ("signals", "positions", "trades", "events")
        sigs, positions, trades_, events_ = await asyncio.gather(
            *(engine.ledger.rows_between(n, start, start + 86_400) for n in names)
        )
        lim_y = engine._exits_by_strategy[StrategyKey.FUDKII_RT_Y.value].limits
        rows = shadow.shadow_rows(signals=sigs, positions=positions, trades=trades_, events=events_, lim_y=lim_y)
        a = daybook.AB_START
        since = datetime(a.year, a.month, a.day, tzinfo=IST).timestamp()
        s_sigs, s_pos, s_trades, s_events = await asyncio.gather(
            *(engine.ledger.rows_between(n, since, time.time() + 86_400) for n in names)
        )
        since_rows = shadow.shadow_rows(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events, lim_y=lim_y)
        return shadow.ShadowData(
            day=day, days=days, rows=rows,
            ab=daybook.ab_summary(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events),
            wide=shadow.wide_stop_summary(positions=s_pos, trades=s_trades),
            graded_f=shadow.graded_f_summary(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events),
            gap=shadow.gap_fade_summary(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events),
            market_fade=shadow.market_fade_summary(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events),
            b1=shadow.b1_summary(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events),
            fukaa=shadow.fukaa_shadow_summary(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events),
            labels=shadow.label_summary(since_rows),
            volume=shadow.volume_summary(since_rows),
            stop_rules=shadow.stop_rules_summary(positions=s_pos, trades=s_trades),
        )

    def _charges_status() -> dict[str, Any]:
        """The charges file in force, and what a typical round trip costs under it per product."""
        st = engine.costs.rates.status()
        examples = []
        for what, inst, px, qty in (
            ("NSE option · ₹20 premium · 4 lots of 500 (₹40,000 premium)",
             Instrument("0", "EX", Segment.NSE_FO, InstrumentKind.OPTION, lot_size=500, strike=0.0, option_type=OptionType.CE), 20.0, 2000),
            ("NSE future · ₹1,500 · 1 lot of 500 (₹7.5 lakh)",
             Instrument("0", "EX", Segment.NSE_FO, InstrumentKind.FUTURE, lot_size=500), 1500.0, 500),
            ("NSE cash intraday · ₹1,500 × 20 shares (₹30,000)",
             Instrument("0", "EX", Segment.NSE_EQ, InstrumentKind.EQUITY, lot_size=1), 1500.0, 20),
        ):
            ch = engine.costs.round_trip(inst, px, px, qty)
            examples.append({"what": what, "product": engine.costs.product_of(inst), "charges": ch.to_json(),
                             "pct_of_turnover": round(ch.total / (px * qty) * 100, 3)})
        st["examples"] = examples
        return st

    @api.get("/charges")
    async def charges_json() -> dict[str, Any]:
        """Every charge rate in force, per product, the file it came from, and whether the last edit parsed."""
        return _charges_status()

    @app.get("/charges", response_class=HTMLResponse)
    async def charges_page() -> HTMLResponse:
        """Where the charges live (operator, 2026-09-26: "a separate area where all charges are
        parked, that can be changed anytime"). Read-only here; the file is the place to edit."""
        st = _charges_status()
        esc = html.escape
        keys = [f["key"] for f in st["fields"]]
        head = "".join(f'<th title="{esc(f["unit"])} — {esc(f["about"])}">{esc(f["key"])}</th>' for f in st["fields"])
        rows = "".join(
            f'<tr><td class="l">{esc(p["title"])}<br><span class="dim">[{esc(p["product"])}]</span></td>'
            + "".join(f"<td>{p[k]:g}</td>" for k in keys) + "</tr>"
            for p in st["products"]
        )
        legend = "".join(f'<tr><td class="l">{esc(f["key"])}</td><td class="l">{esc(f["unit"])}</td><td class="l">{esc(f["about"])}</td></tr>'
                         for f in st["fields"])
        ex = "".join(
            f'<tr><td class="l">{esc(e["what"])}</td><td>₹{e["charges"]["brokerage"]:,.2f}</td><td>₹{e["charges"]["stt"]:,.2f}</td>'
            f'<td>₹{e["charges"]["exchange"]:,.2f}</td><td>₹{e["charges"]["sebi"]:,.2f}</td><td>₹{e["charges"]["stamp"]:,.2f}</td>'
            f'<td>₹{e["charges"]["gst"]:,.2f}</td><td><b>₹{e["charges"]["total"]:,.2f}</b></td><td>{e["pct_of_turnover"]:.3f}%</td></tr>'
            for e in st["examples"]
        )
        err = (f'<p class="refused" style="padding:10px 12px;border-radius:6px">The last edit did not apply — {esc(st["error"])}. '
               f'The previous values are still in force.</p>') if st["error"] else ""
        loaded = datetime.fromtimestamp(st["loadedAt"], IST).strftime("%d %b %H:%M:%S") if st["loadedAt"] else "never (defaults in force)"
        basis = "per executed order, whatever the number of lots" if st["basis"] == "order" else "per lot"
        page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Charges</title><style>{daybook._CSS}</style></head><body>
<header><div><h1>Charges</h1><p class="dim">Every rate the engine costs a trade with — paper fills, the ledger, the backtests. Defaults from {esc(st["source"])}.</p></div></header>
{err}
<p>Edit <code>{esc(str(st["path"] or "—"))}</code> and save; the engine re-reads it within a second, no restart. Last read: {loaded}.
Every percentage is charged ONCE on each leg's total turnover (price × total quantity) — four lots are one turnover.
Brokerage is charged <b>{basis}</b> (<code>[brokerage] basis</code>). GST {st["gstPct"]:g}% on brokerage + exchange + SEBI.
The bid/ask spread is not a charge: fills pay it by trading against the order book.</p>
<h2>Rates in force, per product</h2>
<div class="scroll"><table><thead><tr><th class="l">Product</th>{head}</tr></thead><tbody>{rows}</tbody></table></div>
<h2>What one round trip costs (bought and sold at the same price)</h2>
<div class="scroll"><table><thead><tr><th class="l">Example</th><th>Brokerage</th><th>STT/CTT</th><th>Exchange</th><th>SEBI</th><th>Stamp</th><th>GST</th><th>Total</th><th>% of turnover</th></tr></thead>
<tbody>{ex}</tbody></table></div>
<h2>What each column is</h2>
<div class="scroll"><table><thead><tr><th class="l">Column</th><th class="l">Unit</th><th class="l">Meaning</th></tr></thead><tbody>{legend}</tbody></table></div>
</body></html>"""
        return HTMLResponse(page)

    @app.get("/shadow", response_class=HTMLResponse)
    async def shadow_page(day: str | None = Query(None)) -> HTMLResponse:
        """Everything logged and labelled on each trigger — breadth, trend efficiency, own
        volatility, the gap, the pivots ahead — what gate B and the 09:45 gap fade make of it, and
        what every book did; the RT-Y A/B on top (operator, 2026-09-26). Always available locally."""
        return HTMLResponse(shadow.render_shadow(await _shadow(day)), headers={"Cache-Control": "no-store"})

    @app.get("/shadow.xlsx")
    async def shadow_xlsx(day: str | None = Query(None)) -> Response:
        """The Shadow page as a workbook — every tab's tables, and each brief on the Notes sheet —
        read off the rendered page like /temporary.xlsx."""
        data = await _shadow(day)
        d = data.day
        page = shadow.render_shadow(data)
        return Response(
            content=export.html_to_xlsx(page),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="shadow-{d.isoformat()}.xlsx"', "Cache-Control": "no-store"},
        )

    @api.get("/shadow")
    async def shadow_json(day: str | None = Query(None)) -> dict[str, Any]:
        """The same as JSON, for a daily read without opening the page."""
        data = await _shadow(day)
        return {
            "day": data.day.isoformat(), "days": data.days, "ab": data.ab, "rows": data.rows,
            "wideStop": data.wide, "gradedF": data.graded_f, "gapFade": data.gap, "marketFade": data.market_fade,
            "b1": data.b1, "fukaa": data.fukaa, "labels": data.labels, "volume": data.volume,
            "tabs": [{"id": t.id, "title": t.title, "brief": asdict(t.brief)} for t in shadow.TABS],
        }

    async def _temporary_html() -> str | None:
        ticket = temp_page.read()
        if ticket is None:
            return None
        day = date.fromisoformat(ticket.day)
        start = datetime(day.year, day.month, day.day, tzinfo=IST).timestamp()
        names = ("signals", "positions", "trades", "events")
        signals, positions, trades, events = await asyncio.gather(
            *(engine.ledger.rows_between(n, start, start + 86_400) for n in names)
        )
        rows = daybook.assemble(
            signals=signals, positions=positions, trades=trades, events=events,
            market=_gap_market({s["symbol"] for s in signals if s.get("symbol")}, day),
            vix=engine.india_vix(),
        )
        return daybook.render(rows, ticket, await _ab())

    async def _ab() -> dict[str, Any]:
        """The RT-Y gate's A/B since it started — every session, not only the ticket's day."""
        a = daybook.AB_START
        start = datetime(a.year, a.month, a.day, tzinfo=IST).timestamp()
        names = ("signals", "positions", "trades", "events")
        signals, positions, trades, events = await asyncio.gather(
            *(engine.ledger.rows_between(n, start, time.time() + 86_400) for n in names)
        )
        return daybook.ab_summary(signals=signals, positions=positions, trades=trades, events=events)

    @api.get("/ab/rt-y")
    async def ab_rt_y() -> dict[str, Any]:
        """The same A/B as JSON, for a daily read without opening the page."""
        return await _ab()

    # One socket of state diffs: every forming bar and every LTP, once a second. The chart's
    # live candle and the position LTPs come from here, not from polling the REST surface.
    hub = Hub()

    def _snapshot() -> dict[str, Any]:
        return {
            "ts": time.time(),
            "mode": engine.mode().value,
            "ltps": dict(engine.ltps),
            "forming": {f"{b.symbol}:{b.tf}": b.to_json() for b in engine.store.forming_all()},
        }

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await handle(ws, hub)

    @app.on_event("startup")
    async def _start_pump() -> None:
        app.state.ws_pump = asyncio.create_task(pump(hub, _snapshot, 1.0))

    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str) -> FileResponse:
            """The SPA shell — served with caching off, deliberately.

            Vite fingerprints every asset (``index-DXC8QuCb.js``), so those are immutable and may
            be cached forever. ``index.html`` is the opposite: it is the only file that names which
            fingerprint is current, and a rebuild changes that name. Served with no
            ``Cache-Control``, browsers apply a heuristic freshness lifetime to a 200 carrying
            ``last-modified`` — so after a deploy they keep asking for a bundle that no longer
            exists, get a 404 for the only script on the page, and render nothing at all. A blank
            app with a healthy API, on every page at once.
            """
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(
                dist / "index.html",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

    return app


def _dc(obj: Any) -> dict[str, Any]:
    from dataclasses import asdict, is_dataclass

    if not is_dataclass(obj):
        return {}
    out: dict[str, Any] = {}
    for k, v in asdict(obj).items():
        out[k] = v if isinstance(v, (int, float, str, bool, type(None), list)) else str(v)
    return out


def _bar_view(b: UnifiedBar) -> dict[str, Any]:
    d = b.to_json()
    d["ist"] = to_ist(b.ts).strftime("%Y-%m-%d %H:%M")
    return d


async def _mirror_rows(
    engine: Engine, real: list[Any], alone: list[Any]
) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[str, str], dict[str, Any]]]:
    """The Overview's stop-rule rows. Under each real open position, its two mirrors as the card strip shows
    them — open with where their stop stands now, or closed with the exit — keyed by the real position's id.
    For a mirror still open on its own rule after the real trade closed: its row, keyed by its own id, and
    the real trade's exit, keyed by (book, signal)."""
    if not real and not alone:
        return {}, {}
    oldest, now = min(p.opened_ts for p in (*real, *alone)), time.time()
    pos_rows, trade_rows = await asyncio.gather(
        engine.ledger.rows_between("positions", oldest, now + 86_400), engine.ledger.rows_between("trades", oldest, now + 86_400)
    )
    by_key = {(r.get("strategy"), r.get("signal_id")): r for r in pos_rows}
    by_key.update({(p.strategy, p.signal_id): _position_json(p) for p in engine.positions.values() if p.status == "OPEN"})
    trade_of = {t.get("position_id"): t for t in trade_rows}

    def row(rule: str, key: str, p: dict[str, Any] | None) -> dict[str, Any]:
        return engine._stop_rule_row(rule, key, p, trade_of.get(p["id"]) if p else None)

    out = {p.id: [row(rule, m.value, by_key.get((m.value, p.signal_id))) for rule, m in stop_mirrors_of(p.strategy).items()] for p in real}
    real_exit: dict[tuple[str, str], dict[str, Any]] = {}
    for m in alone:
        src, rule = stop_rule_of(m.strategy)
        out[m.id] = [row(rule, m.strategy, _position_json(m))]
        rp = by_key.get((src, m.signal_id))
        if rp is not None and rp.get("status") != "OPEN":
            t = trade_of.get(rp["id"])
            real_exit[(src, m.signal_id)] = {"exitReason": rp.get("exit_reason"), "exitPrice": rp.get("exit_price"), "closedTs": rp.get("closed_ts"),
                                             "net": float(t["net"]) if t else None, "byOperator": "closed by the operator" in str(m.note)}
    return {k: v for k, v in out.items() if v}, real_exit


def _archive_day_files(data_dir: Path, day: date) -> list[Path]:
    """The bar archive's files for ``day`` as the archive reads them: the day file, then any parts in order (phase 35
    flushes parts and compacts them after the day; phase 34 rewrites the day file) — never a file still being written
    under its temp name (``*.tmp.parquet``: both engines write through one, and the old ``{day}*.parquet`` glob read it)."""
    d = data_dir / "archive" / "bars"
    main = d / f"{day.isoformat()}.parquet"
    parts = sorted(p for p in d.glob(f"{day.isoformat()}.part-*.parquet") if not p.name.endswith(".tmp.parquet")) if d.exists() else []
    return ([main] if main.exists() else []) + parts


def _archive_bars(data_dir: Path, symbols: set[str], day: date) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """The one-minute bars of ``symbols`` for ``day`` from the archive (as built from the feed), every file of the day read
    once for all of them (it was read again for each symbol, on the event loop: a review of 8 Oct took 11 s that way), and
    whether the read was whole. A file that cannot be read (the engine compacting or rewriting it as this reads) makes the
    read not whole, so the caller keeps nothing from it and reads again."""
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    if not symbols:
        return out, True
    try:
        import pandas as pd

        files = _archive_day_files(data_dir, day)
        if not files:
            return out, True
        want = sorted(symbols)
        frame = pd.concat([pd.read_parquet(f, columns=["symbol", "ts", "o", "h", "l", "c", "v"], filters=[("symbol", "in", want)]) for f in files])
        frame = frame.drop_duplicates(["symbol", "ts"], keep="last").sort_values(["symbol", "ts"])
        for sym, g in frame.groupby("symbol"):
            out[str(sym)] = [{"ts": float(r.ts), "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c), "v": float(r.v)}
                             for r in g.itertuples()]
    except Exception:  # noqa: BLE001 - a review without its chart still answers; not whole, so nothing of it is kept
        return {s: [] for s in symbols}, False
    return out, True


def _position_view(engine: Engine, p: Any) -> dict[str, Any]:
    d = _position_json(p)
    ltp = engine.ltps.get(p.instrument.scrip_code)
    d["ltp"] = ltp
    d["unrealized"] = round(p.unrealized(ltp), 2) if ltp else None
    d["r_now"] = round(p.r_now(ltp), 3) if ltp else None
    d["max_dd_inr"] = p.max_dd_inr()
    d["underlying_ltp"] = engine.ltps.get(p.underlying.scrip_code)
    d["opened_ist"] = to_ist(p.opened_ts).strftime("%Y-%m-%d %H:%M:%S")
    return d


__all__ = ["build_app", "sa"]
