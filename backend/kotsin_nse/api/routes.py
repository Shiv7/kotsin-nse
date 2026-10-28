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
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..bars.unified import UnifiedBar
from ..engine import Engine
from ..exec.gateway import Mode
from ..ledger.db import events
from ..market.session import TF_SECONDS, ist_hm, to_ist
from .ws import Hub, handle, pump


class ModeRequest(BaseModel):
    mode: str
    armed_minutes: int | None = Field(
        default=None, description="required for LIVE_CAPPED / LIVE; arming is never implicit"
    )


class HaltRequest(BaseModel):
    halted: bool
    reason: str = ""


class JournalCall(BaseModel):
    """A discretionary call, logged as it is seen (``journal.py``)."""

    symbol: str
    side: str = Field(description="LONG or SHORT")
    kind: str = Field(default="entry", description="entry (would take it) or alert (watching)")
    entry: float | None = Field(default=None, description="defaults to the last price on the screen")
    stop: float | None = None
    target: float | None = None
    tags: list[str] = Field(default_factory=list)
    note: str = ""


def build_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="kotsin-nse", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    api = APIRouter(prefix="/api")

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
                "max_universe": engine.s.max_universe,
            },
            "bus": engine.bus.stats(),
            "health": engine.health_snapshot(),
            "counts": await engine.ledger.counts(),
        }

    @api.get("/overview")
    async def overview() -> dict[str, Any]:
        return {
            "mode": engine.mode().value,
            "armed_until": engine._armed_until,
            "halted": engine.halted()[0],
            "halt_reason": engine.halted()[1],
            "universe": len(engine.underlyings),
            "market_open": engine.market_open_now(),
            "boot_notes": engine.boot_notes,
        }

    @api.get("/events")
    async def recent_events(limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
        return await engine.ledger.recent(events, limit)

    # -- the strategy's shadow book (strategy/runner.py keeps it; this only reads) --------------------

    @api.get("/strategy")
    async def strategy_view(day: str | None = None) -> dict[str, Any]:
        r = engine.strategy
        if r is None:
            raise HTTPException(404, "the strategy is disabled (KN_STRATEGY_ENABLED=false)")
        if day and day != r.day:
            snap = r.read_day(day)
            if snap is None:
                raise HTTPException(404, f"no strategy record for {day}")
            return snap
        return r.snapshot()

    # -- the positional book (strategy/positional.py keeps it; this only reads) ----------------------

    @api.get("/positional")
    async def positional_view() -> dict[str, Any]:
        r = engine.positional
        if r is None:
            raise HTTPException(404, "the positional book is disabled (KN_POSITIONAL_ENABLED=false)")
        return r.snapshot()

    # -- the owner's call journal -------------------------------------------------------------------
    # Scoring happens in the engine (``Engine._score_journal``), from the bars; these only store and read.

    @api.get("/journal")
    async def journal(day: str | None = None, limit: int = Query(300, le=2000)) -> dict[str, Any]:
        return await engine.journal_view(day=day, limit=limit)

    @api.post("/journal")
    async def journal_add(req: JournalCall) -> dict[str, Any]:
        try:
            return await engine.journal_add(req.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/journal/{call_id}/close")
    async def journal_close(call_id: str, reason: str = "manual") -> dict[str, Any]:
        row = await engine.journal_close(call_id, reason)
        if row is None:
            raise HTTPException(404, f"no open call {call_id}")
        return row

    @api.post("/journal/{call_id}/cancel")
    async def journal_cancel(call_id: str) -> dict[str, Any]:
        row = await engine.journal_close(call_id, "cancelled", cancel=True)
        if row is None:
            raise HTTPException(404, f"no open call {call_id}")
        return row

    # -- market -----------------------------------------------------------------------------------

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

    @api.get("/feed")
    async def feed() -> dict[str, Any]:
        """Which source streams market data, what it can and cannot give, and how often it has
        actually updated each kind of contract this session — measured, not quoted."""
        c = engine.feed.capabilities
        return {
            "source": c.name,
            "capabilities": {
                "depth_levels": c.depth_levels,
                "trade_side": c.trade_side,
                "snapshots": c.snapshots,
                "note": c.note,
            },
            "rate": engine.feed_rate.snapshot(),
        }

    @api.get("/flow/{symbol}")
    async def flow(symbol: str, tf: int = Query(1, ge=1, le=30)) -> dict[str, Any]:
        """Today's estimated order flow per ``tf`` minutes for the stock and its front future:
        buy / sell volume (each snapshot's new volume by the quote rule — an estimate, the feed has
        no trade side), delta, cumulative delta, top-5 book imbalance and the future's OI."""
        sym = symbol.upper()
        g = engine.groups.get(sym)
        if g is None:
            raise HTTPException(404, f"{sym} is not in the universe")

        def rows(code: str | None) -> list[dict[str, Any]]:
            if code is None:
                return []
            out: dict[int, dict[str, Any]] = {}
            for m in engine.flow.minutes(code):
                k = m.ts - (m.ts % (tf * 60))
                r = out.setdefault(k, {"ts": k, "buy": 0.0, "sell": 0.0, "unknown": 0.0, "frames": 0, "imb": [], "oi": None})
                r["buy"] += m.buy
                r["sell"] += m.sell
                r["unknown"] += m.unknown
                r["frames"] += m.frames
                r["cum_delta"] = m.cum_delta
                if m.imbalance is not None:
                    r["imb"].append(m.imbalance)
                if m.oi is not None:
                    r["oi"] = m.oi
            for r in out.values():
                r["delta"] = r["buy"] - r["sell"]
                r["imbalance"] = round(sum(r["imb"]) / len(r["imb"]), 4) if r["imb"] else None
                del r["imb"]
            return [out[k] for k in sorted(out)]

        fut = g.futures[0].scrip_code if g.futures else None
        return {
            "symbol": sym,
            "tf_minutes": tf,
            "estimate": "snapshot quote rule — no trade side on this feed",
            # an MCX root has no cash leg: its underlying IS the front future, shown once
            "has_cash": g.equity is not None,
            "equity": rows(g.underlying.scrip_code) if g.equity is not None else [],
            "future": rows(fut),
            "future_code": fut,
        }

    @api.get("/analysis/{symbol}")
    async def analysis(symbol: str) -> dict[str, Any]:
        """One stock end to end (analysis/bundle.py): big picture, positioning, build-up across
        timeframes, levels and Fibonacci, structure and profile, today's reaction, microstructure,
        options and two hedged option-selling setups."""
        from ..analysis.bundle import build

        out = build(engine, symbol, time.time())  # on the loop: it reads the feed's dicts
        if out is None:
            raise HTTPException(404, f"no daily history for {symbol.upper()} — not an F&O stock we hold data for")
        return out

    @api.get("/dayread/{symbol}")
    async def dayread(symbol: str, day: str | None = None) -> dict[str, Any]:
        """The session on the 5-minute chart as the operator reads it (analysis/dayread.py):
        pivots and CPR tested, held or broken; swings and breaks of structure; each leg's futures
        OI and the strikes written or covered. Today from the engine; a past ``day`` from the archive."""
        from datetime import date

        from ..analysis.dayread import from_archive, from_engine
        from ..market.session import ist_day

        sym = symbol.upper()
        now = time.time()
        if day and date.fromisoformat(day) != ist_day(now):
            out = from_archive(engine.s.data_dir, sym, date.fromisoformat(day))
        else:
            out = from_engine(engine, sym, now)
        if out is None:
            raise HTTPException(404, f"no bars for {sym} on {day or ist_day(now).isoformat()}")
        return out

    @api.get("/analysis-symbols")
    async def analysis_symbols() -> list[dict[str, Any]]:
        """Every stock the Analysis page can open: live ones first, then those with cached history."""
        from ..instrument.universe import INDEX_ROOTS
        from ..research.history import HistoryStore

        live = sorted(engine.groups)
        cached = [x for x in HistoryStore(engine.s.data_dir / "history").symbols("1d") if x not in engine.groups and x not in INDEX_ROOTS]
        return [{"symbol": x, "live": True} for x in live] + [{"symbol": x, "live": False} for x in sorted(cached)]

    @api.get("/analysis/{symbol}/summary")
    async def analysis_summary(symbol: str, refresh: bool = False) -> dict[str, Any]:
        """Claude's written read of the same data. Never a call on its own: without ``refresh`` (the
        page's generate button) it only returns the last summary made, or ``{"none": true}``; with it,
        one paid API call, within the daily cap."""
        import anthropic

        from ..analysis.bundle import build, compact
        from ..analysis.llm import BudgetExceeded, Summarizer

        if not hasattr(app.state, "summarizer"):
            key = engine.s.anthropic_api_key.get_secret_value() if engine.s.anthropic_api_key else None
            app.state.summarizer = Summarizer(key)
        sm = app.state.summarizer
        if not sm.configured:
            raise HTTPException(503, "no Anthropic API key: add KN_ANTHROPIC_API_KEY=… to backend/.env and restart the engine")
        sym = symbol.upper()
        if not refresh:
            hit = sm.cached(sym)
            return {**hit, "cached": True} if hit else {"symbol": sym, "none": True, "calls_left_today": sm.calls_left()}
        bundle = build(engine, sym, time.time())
        if bundle is None:
            raise HTTPException(404, f"no daily history for {sym}")
        try:
            return await sm.summarize(sym, compact(bundle), refresh=refresh)
        except BudgetExceeded as exc:
            raise HTTPException(429, str(exc)) from exc
        except anthropic.AuthenticationError as exc:
            raise HTTPException(401, "the Anthropic API key was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise HTTPException(429, "Anthropic rate limit — try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise HTTPException(502, f"Anthropic API error {exc.status_code}: {exc.message}"[:300]) from exc
        except anthropic.APIConnectionError as exc:
            raise HTTPException(502, "could not reach the Anthropic API") from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @api.get("/micro/{symbol}")
    async def micro(symbol: str) -> dict[str, Any]:
        """Microstructure of the stock and its front future (orderflow/micro.py): microprice, queue
        imbalance, spread, book skew, Kyle's lambda, VPIN, efficiency ratio, realised volatility,
        Roll's spread, Amihud, walls pulled/filled, absorption and vacuum events. Snapshot-based
        estimates on a 5-level book."""
        sym = symbol.upper()
        g = engine.groups.get(sym)
        if g is None:
            raise HTTPException(404, f"{sym} is not in the universe")
        fut = g.futures[0] if g.futures else None
        return {
            "symbol": sym,
            "cash": engine.micro.snapshot(g.underlying.scrip_code) if g.equity is not None else None,
            "future": engine.micro.snapshot(fut.scrip_code) if fut else None,
            "future_name": fut.name if fut else None,
        }

    @api.get("/fidelity")
    async def fidelity() -> dict[str, Any]:
        """How often the live bar build disagrees with the exchange's own candle, by timeframe."""
        return engine.reconciler.snapshot()

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
        }

    @api.get("/fno/{symbol}")
    async def fno(symbol: str) -> dict[str, Any]:
        """Futures and options read together: each leg's state since its reference, the 64-state
        reading (score, bias, premium signal), and per expiry the walls, put-call ratio, straddle
        and IV. ``missing`` names whatever kept a part of it from being computed."""
        # on the loop, not a worker thread: it reads dicts the feed is writing (~15 ms of IV maths)
        out = engine.fno_snapshot(symbol.upper())
        if out is None:
            raise HTTPException(404, f"{symbol.upper()} has no future in the universe")
        return out

    @api.get("/chain/{symbol}")
    async def chain(symbol: str, expiry: str | None = None) -> dict[str, Any]:
        """The option chain for one expiry (default: the nearest), with the live quotes we hold."""
        from ..domain import OptionType

        cat = engine.catalogue_loader.catalogue
        sym = symbol.upper()
        exps = cat.expiries(sym)
        if not exps:
            raise HTTPException(
                404,
                f"no option chain for {sym} — the chain comes from the scrip master, which needs a "
                f"broker session (see boot notes)",
            )
        today = date.today().isoformat()
        chosen_expiry = expiry if expiry in exps else next((e for e in exps if e >= today), exps[0])
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
                    "oi": engine.option_oi.get(inst.scrip_code),
                }

        return {
            "symbol": sym,
            "spot": spot,
            "expiry": chosen_expiry,
            "expiries": exps,
            "rows": [by_strike[k] for k in sorted(by_strike)],
        }

    # -- the board ------------------------------------------------------------------------------------

    @api.get("/scan")
    async def scan() -> dict[str, Any]:
        """The live board, rebuilt two seconds after every minute's 1m bars close."""
        b = engine.scanner.board
        if b is None:
            raise HTTPException(404, "no scan yet — the board starts at 09:20 IST on a session with a feed")
        return {"mode": "live", **b.to_json()}

    replays: dict[str, Any] = {}

    async def _replay(day_s: str) -> Any:
        from ..research.history import HistoryStore
        from ..scan.replay import DayReplay

        try:
            day = date.fromisoformat(day_s)
        except ValueError as exc:
            raise HTTPException(400, f"day must be YYYY-MM-DD, got {day_s!r}") from exc
        if day_s not in replays:
            store = HistoryStore(engine.s.data_dir / "history")
            r = await asyncio.to_thread(DayReplay, store, day)
            if not r.days:
                raise HTTPException(404, f"no 1m history cached for {day_s} — run fetch-history --tf 1m")
            if len(replays) >= 3:
                replays.pop(next(iter(replays)))
            replays[day_s] = r
        return replays[day_s]

    @api.get("/scan/days")
    async def scan_days() -> list[str]:
        """Sessions a board can be replayed for: the days in the 1m cache of its best-covered stock."""
        from ..research.history import HistoryStore

        store = HistoryStore(engine.s.data_dir / "history")
        names = store.symbols("1m")
        if not names:
            return []

        def _days() -> list[str]:
            best: set[str] = set()
            for sym in names[:5]:
                ts = store.load(sym, "1m")["ts"]
                days = {to_ist(int(t)).date().isoformat() for t in ts[::60]}
                best = best if len(best) >= len(days) else days
            return sorted(best, reverse=True)

        return await asyncio.to_thread(_days)

    @api.get("/scan/replay")
    async def scan_replay(day: str, at: str = "15:30") -> dict[str, Any]:
        """The board as it stood at ``at`` (HH:MM IST) on a past ``day`` — the live code, fed from
        the history cache, reading only bars that had closed by then."""
        from ..market.session import ist_at

        r = await _replay(day)
        try:
            ts = ist_at(r.day, at)
        except ValueError as exc:
            raise HTTPException(400, f"at must be HH:MM, got {at!r}") from exc
        board = await asyncio.to_thread(r.board_at, ts)
        return {"mode": "replay", "day": day, "at": at, **r.summary(), **board.to_json()}

    # -- history ---------------------------------------------------------------------------------------

    @api.get("/structure/{symbol}")
    async def structure(symbol: str, day: str | None = None, at: str = "15:30") -> dict[str, Any]:
        """Order blocks, gaps, liquidity, sweeps and the current setup on 5-minute candles — for a
        past ``day`` as it stood at ``at`` (only bars closed by then), or live when ``day`` is omitted."""
        from ..market.session import ist_at
        from ..research.history import HistoryStore
        from ..structure.service import from_history, from_store

        sym = symbol.upper()
        if day:
            d = date.fromisoformat(day)
            st = await asyncio.to_thread(
                from_history, HistoryStore(engine.s.data_dir / "history"), sym, d, ist_at(d, at)
            )
        else:
            st = from_store(engine.store, sym, time.time())
        if st is None:
            raise HTTPException(404, f"no 5-minute bars for {sym}{' on ' + day if day else ' today'}")
        return {"symbol": sym, "day": day, "at": at if day else None, **st.to_json()}

    @api.get("/levels/{symbol}")
    async def levels(symbol: str, day: str | None = None, at: str = "15:30") -> dict[str, Any]:
        """Daily, weekly and monthly pivots (classic, CPR, Camarilla) and where they cluster into
        support and resistance zones — for a past ``day`` as of ``at``, or live when omitted."""
        from ..market.session import ist_at
        from ..research.history import HistoryStore
        from ..structure.service import levels_from_history, levels_from_store

        sym = symbol.upper()
        if day:
            d = date.fromisoformat(day)
            out = await asyncio.to_thread(levels_from_history, HistoryStore(engine.s.data_dir / "history"), sym, d, ist_at(d, at))
        else:
            out = levels_from_store(engine.store, sym, time.time())
        if out is None:
            raise HTTPException(404, f"no daily bars for {sym} before {day or 'today'}")
        return {"symbol": sym, "day": day, "at": at if day else None, **out}

    @api.get("/profile/{symbol}")
    async def profile(symbol: str, day: str | None = None, at: str = "15:30", days: int = Query(1, ge=1, le=20)) -> dict[str, Any]:
        """Volume profile (POC, value area, HVN/LVN) and market profile (TPO letters, initial
        balance, single prints, day type) — for a past ``day`` up to ``at``, or live. ``days`` > 1
        adds a composite volume profile over that many sessions ending on ``day``."""
        from ..market.session import ist_at
        from ..profile.service import from_history, from_store
        from ..research.history import HistoryStore

        sym = symbol.upper()
        if day:
            d = date.fromisoformat(day)
            out = await asyncio.to_thread(
                from_history, HistoryStore(engine.s.data_dir / "history"), sym, d, ist_at(d, at), days
            )
        else:
            from ..market.session import ist_day, session_open_ts

            g = engine.groups.get(sym)
            now = time.time()
            out = from_store(
                engine.store, sym, now, session_open_ts(g.segment, ist_day(now)) if g else None
            )
        if out is None:
            raise HTTPException(404, f"no 1m bars for {sym}{' on ' + day if day else ' today'}")
        return {"symbol": sym, "day": day, "at": at if day else None, **out}

    @api.get("/obtests")
    async def obtests(limit: int = Query(30, le=100)) -> list[dict[str, Any]]:
        """Saved runs of the order-block playbook backtest (run from the CLI: ``ob-backtest``)."""
        from ..research.obtest import list_runs

        return await asyncio.to_thread(list_runs, engine.s.data_dir / "obtests", limit)

    @api.get("/obtests/{run_id}")
    async def obtest(run_id: str) -> dict[str, Any]:
        path = engine.s.data_dir / "obtests" / f"{run_id}.json"
        if ".." in run_id or "/" in run_id or not path.exists():
            raise HTTPException(404, f"no run {run_id}")
        return json.loads(await asyncio.to_thread(path.read_text))

    @api.get("/history/range/{symbol}")
    async def history_range(symbol: str, start: int, end: int, tf: str = "60m") -> dict[str, Any]:
        """Candles across days from the 5-minute cache, cleaned and rolled up to ``tf`` (5m, 15m,
        60m, 240m) on the session grid — the chart behind a multi-day trade."""
        from ..research.history import HistoryStore
        from ..research.mtf import TF_MIN, clean_5m, resample

        if tf not in TF_MIN:
            raise HTTPException(400, f"tf must be one of {', '.join(TF_MIN)}")

        def _bars() -> list[dict[str, Any]]:
            df = HistoryStore(engine.s.data_dir / "history").load(symbol.upper(), "5m")
            df = df[(df["ts"] >= start) & (df["ts"] < end)]
            df = clean_5m(df)
            if df.empty:
                return []
            out = df if tf == "5m" else resample(df, tf)
            return [
                {"ts": int(r.ts), "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c), "v": float(r.v)}
                for r in out.itertuples()
            ]

        bars = await asyncio.to_thread(_bars)
        if not bars:
            raise HTTPException(404, f"no 5m history for {symbol} in that range — run fetch-history --tf 5m")
        return {"symbol": symbol.upper(), "tf": tf, "bars": bars}

    @api.get("/history/bars/{symbol}")
    async def history_bars(symbol: str, day: str, tf: str = "5m", until: str | None = None) -> dict[str, Any]:
        """A past session's candles from the 1m cache, rolled up to ``tf`` on the 09:15 grid —
        ``until`` (HH:MM IST) cuts them where a replay stands, so the chart never shows the future."""
        from ..market.session import ist_at
        from ..research.history import HistoryStore

        minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}.get(tf)
        if minutes is None:
            raise HTTPException(400, "tf must be one of 1m 3m 5m 15m 30m")
        from ..research.history import session_rows

        d = date.fromisoformat(day)
        lo, hi = ist_at(d, "09:15"), ist_at(d, until or "15:30")
        df = session_rows(HistoryStore(engine.s.data_dir / "history").load(symbol.upper(), "1m"), d)
        df = df[df["ts"] + 60 <= hi]
        if df.empty:
            raise HTTPException(404, f"no 1m history for {symbol} on {day}")
        step = minutes * 60
        df = df.assign(bucket=lo + ((df["ts"] - lo) // step) * step)
        g = df.groupby("bucket", sort=True)
        out = g.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
        bars = [
            {"ts": int(r.bucket), "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c), "v": float(r.v)}
            for r in out.itertuples()
        ]
        return {"symbol": symbol.upper(), "day": day, "tf": tf, "bars": bars}

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
        requested = False
        if engine.live_exec is not None and engine.mode() in (Mode.LIVE, Mode.LIVE_CAPPED):
            await engine.live_exec.square_off_all()
            requested = True
        return {"halted": True, "square_off_requested": requested}

    @api.post("/control/reset-breaker")
    async def reset_breaker() -> dict[str, Any]:
        engine.gateway.reset_breaker()
        return engine.gateway.stats()

    app.include_router(api)

    # One socket of state diffs: every forming bar and every LTP, once a second. The chart's
    # live candle and the LTPs come from here, not from polling the REST surface.
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
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(dist / "index.html")

    return app


def _bar_view(b: UnifiedBar) -> dict[str, Any]:
    d = b.to_json()
    d["ist"] = to_ist(b.ts).strftime("%Y-%m-%d %H:%M")
    return d


__all__ = ["build_app", "sa"]
