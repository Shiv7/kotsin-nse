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
import time
from datetime import date
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..bars.indicators import bollinger, supertrend
from ..bars.unified import UnifiedBar
from ..engine import SELECTION_POLICY, Engine, _position_json
from ..exec.gateway import Mode
from ..hotstocks.service import HotStocksService
from ..ledger.db import events, rejections, signals, trades
from ..market.session import TF_SECONDS, ist_hm, to_ist
from ..strategy.catalog import BOOKS, LIVE_KEYS
from ..strategy.keys import ALL_KEYS
from .ws import Hub, handle, pump


class ModeRequest(BaseModel):
    mode: str
    armed_minutes: int | None = Field(
        default=None, description="required for LIVE_CAPPED / LIVE; arming is never implicit"
    )


class HaltRequest(BaseModel):
    halted: bool
    reason: str = ""


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
    api = APIRouter(prefix="/api")
    hot_stocks_service = HotStocksService(engine, engine.s.data_dir / "hotstocks-sectors.tsv")

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
        capital = sum(w.balance for w in engine.wallets.values())
        return {
            "mode": engine.mode().value,
            "armed_until": engine._armed_until,
            "halted": engine.halted()[0],
            "halt_reason": engine.halted()[1],
            "wallets": [engine.wallets[k.value].to_json() for k in ALL_KEYS],
            "capital": round(capital, 2),
            "day_pnl": round(sum(w.day_pnl for w in engine.wallets.values()), 2),
            "positions": [
                _position_view(engine, p) for p in sorted(open_positions, key=lambda x: -x.opened_ts)
            ],
            "exposure": engine.exposure.snapshot(open_positions, capital),
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
    async def recent_signals(limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
        return await engine.ledger.recent(signals, limit)

    @api.get("/rejections")
    async def recent_rejections(
        limit: int = Query(100, le=500), strategy: str | None = None
    ) -> list[dict[str, Any]]:
        where = rejections.c.strategy == strategy if strategy else None
        return await engine.ledger.recent(rejections, limit, where=where)

    @api.get("/trades")
    async def recent_trades(limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
        return await engine.ledger.recent(trades, limit)

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
    async def alerts(book: str | None = None, limit: int = Query(100, le=500)) -> dict[str, Any]:
        """Realtime firings from the ported books. Advisory: none of these can place an order."""
        return {
            "alerts": engine.alerts.feed(book, limit),
            **engine.alerts.stats(),
            "now_ist": ist_hm(time.time()),
        }

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
            choose_expiry(exps, date.today(), SELECTION_POLICY) or exps[0]
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
                    policy=SELECTION_POLICY,
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

        return {
            "symbol": sym,
            "spot": spot,
            "expiry": chosen_expiry,
            "expiries": exps,
            "rows": [by_strike[k] for k in sorted(by_strike)],
            "selection": selection,
            "policy": {
                "min_days_to_expiry": SELECTION_POLICY.min_days_to_expiry,
                "min_premium": SELECTION_POLICY.min_premium,
                "max_premium": SELECTION_POLICY.max_premium,
                "max_spread_pct": SELECTION_POLICY.max_spread_pct,
                "max_quote_age_s": SELECTION_POLICY.max_quote_age_s,
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
        if engine.reconciler is None:
            raise HTTPException(400, "no broker session")
        report = await engine.reconciler.run(list(engine.positions.values()))
        return report.to_json()

    @api.post("/control/acknowledge")
    async def acknowledge() -> dict[str, Any]:
        if engine.reconciler is None:
            raise HTTPException(400, "no broker session")
        engine.reconciler.acknowledge()
        return {"frozen": engine.reconciler.frozen}

    @api.post("/control/reset-breaker")
    async def reset_breaker() -> dict[str, Any]:
        engine.gateway.reset_breaker()
        return engine.gateway.stats()

    app.include_router(api)

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
