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
import re
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..bars.daily import previous_session
from ..bars.indicators import atr, bollinger, supertrend
from ..bars.unified import UnifiedBar
from ..config import Segment
from ..domain import Instrument, InstrumentKind, OptionType
from ..engine import SELECTION_POLICY, Engine, _position_json
from ..exec.gateway import Mode
from ..hotstocks.service import HotStocksService
from ..ledger.db import events, rejections, signals, trades
from ..market.session import IST, TF_SECONDS, ist_day, ist_hm, ist_today, to_ist
from ..strategy.catalog import BOOKS, LIVE_KEYS
from ..strategy.keys import ALL_KEYS, SHADOW_BOOKS, StrategyKey, describe_book
from . import daybook, export, peer, shadow
from .ws import Hub, handle, pump


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


def build_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="kotsin-nse", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    temp_page = daybook.TemporaryPage(engine.s.data_dir)
    api = APIRouter(prefix="/api")
    hot_stocks_service = HotStocksService(engine, engine.s.data_dir / "hotstocks-sectors.tsv")
    # this engine's name and its twin's books (phase 34 / phase 35 side by side, 2026-10-03)
    peer.register(api, engine.s.data_dir)

    # -- health & system ---------------------------------------------------------------------------

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
        return {
            "mode": engine.mode().value,
            "armed_until": engine._armed_until,
            "halted": engine.halted()[0],
            "halt_reason": engine.halted()[1],
            "wallets": [engine.wallets[k.value].to_json() for k in ALL_KEYS],
            "capital": round(capital, 2),
            "day_pnl": round(sum(w.day_pnl for w in trading), 2),
            "shadow_day_pnl": round(sum(w.day_pnl for k, w in engine.wallets.items() if k in shadows), 2),
            "positions": [
                _position_view(engine, p) for p in sorted(open_positions, key=lambda x: -x.opened_ts)
            ],
            "exposure": engine.exposure.snapshot([p for p in open_positions if p.strategy not in shadows], capital),
            "universe": len(engine.underlyings),
            "boot_notes": engine.boot_notes,
        }

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

    @api.get("/trades")
    async def recent_trades(limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
        # the book's name and its side of the trigger come from the registry as the rows are served:
        # rows already stored never carried them
        return [{**t, **describe_book(str(t.get("strategy") or ""))} for t in await engine.ledger.recent(trades, limit)]

    @api.get("/events")
    async def recent_events(limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
        return await engine.ledger.recent(events, limit)

    @api.get("/pnl")
    async def pnl() -> dict[str, Any]:
        """Net, gross and charges, split. Keeping them apart is the only way the cost structure
        stays visible — on the old book 81% of the round trip was flat brokerage."""
        rows = await engine.ledger.recent(trades, 1000)
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

    @api.get("/indicators/{symbol}")
    async def indicators(symbol: str, tf: str = "30m", n: int = Query(300, le=1000)) -> dict[str, Any]:
        """Bollinger and SuperTrend per bar — computed by the SAME functions, with the SAME live
        config, that FUDKII decides on. Not a charting-library reimplementation: if these lines
        disagree with a signal, the signal is wrong, not the chart."""
        cfg = engine.fudkii.cfg
        sym = symbol.upper()
        warm = max(cfg.bb_period, cfg.st_atr_period) + 1
        bars = engine.store.bars(sym, tf, n + warm + 60)
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
        A/B, the wide stop, the gap fade, CT-M's market fade, FUKAA's shadow, the labels) since the A/B began."""
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
            fukaa=shadow.fukaa_shadow_summary(signals=s_sigs, positions=s_pos, trades=s_trades, events=s_events),
            labels=shadow.label_summary(since_rows),
            volume=shadow.volume_summary(since_rows),
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
            "fukaa": data.fukaa, "labels": data.labels, "volume": data.volume,
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


def _position_view(engine: Engine, p: Any) -> dict[str, Any]:
    d = _position_json(p)
    ltp = engine.ltps.get(p.instrument.scrip_code)
    d["ltp"] = ltp
    d["unrealized"] = round(p.unrealized(ltp), 2) if ltp else None
    d["r_now"] = round(p.r_now(ltp), 3) if ltp else None
    d["underlying_ltp"] = engine.ltps.get(p.underlying.scrip_code)
    d["opened_ist"] = to_ist(p.opened_ts).strftime("%Y-%m-%d %H:%M:%S")
    return d


__all__ = ["build_app", "sa"]
