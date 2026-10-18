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

    # -- research -----------------------------------------------------------------------------------

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
