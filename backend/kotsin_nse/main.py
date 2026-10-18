"""Entry point. One process: engine + API + static UI on one port, plus the history fetch.

``uv run kotsin-nse`` boots the engine, mounts the API and serves ``frontend/dist`` from the same
port, so there is one systemd unit, one log and one thing to restart. The old stack needed five
JVMs, a Kafka, a Mongo, a Redis and a Vite dev server brought up in a specific order — and a
watchdog that relaunched all five inside 30 seconds with heap flags sized for a machine that no
longer existed.

Subcommands:

* ``serve`` (default) — the engine and the UI
* ``fetch-history`` — fill the Parquet cache from the broker's historical endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from datetime import date, timedelta
from pathlib import Path

import structlog
import uvicorn

from .api.routes import build_app
from .config import Settings, UnknownConfigKeys, assert_no_unknown_env
from .engine import Engine
from .log import configure_logging

log = structlog.get_logger(__name__)


def load_settings() -> Settings:
    assert_no_unknown_env()
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    (s.data_dir / "logs").mkdir(exist_ok=True)
    return s


# -- serve ----------------------------------------------------------------------------------------


async def serve(settings: Settings) -> None:
    engine = Engine(settings)
    app = build_app(engine)
    await engine.start()

    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    pid_file = settings.data_dir / "engine.pid"
    pid_file.write_text(str(os.getpid()))
    log.info("serving", url=f"http://{settings.api_host}:{settings.api_port}", pid=os.getpid())

    serve_task = asyncio.create_task(server.serve())
    await stop.wait()
    log.info("shutdown.begin")
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(serve_task, timeout=10)
    await engine.stop()
    pid_file.unlink(missing_ok=True)
    log.info("shutdown.done")


# -- research ---------------------------------------------------------------------------------------


async def fetch_history(settings: Settings, args: argparse.Namespace) -> None:
    """Fill the Parquet cache. Needs broker credentials; everything downstream of it does not."""
    import httpx

    from .domain import Instrument, InstrumentKind
    from .instrument.catalogue import CatalogueLoader
    from .research.history import HistoryStore, fetch
    from .venue.fivepaisa.auth import Authenticator
    from .venue.fivepaisa.rest import FivePaisaREST

    if not settings.has_credentials:
        print("5paisa credentials required — set KN_FP_* in backend/.env", file=sys.stderr)
        raise SystemExit(2)

    async with httpx.AsyncClient(timeout=60) as http:
        auth = Authenticator(settings, http)
        rest = FivePaisaREST(settings, http, auth)
        catalogue = await CatalogueLoader(settings, rest.scrip_master_csv).ensure()
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        instruments: list[Instrument] = []
        for sym in wanted:
            inst = catalogue.equity(sym) or catalogue.front_future(sym)
            if inst is None:
                print(f"  ! {sym} not in the scrip master — skipped", file=sys.stderr)
                continue
            instruments.append(inst)
        if not instruments:
            print("nothing to fetch", file=sys.stderr)
            raise SystemExit(1)
        store = HistoryStore(settings.data_dir / "history")
        counts = await fetch(
            rest,
            store,
            instruments,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
        )
    for key, n in sorted(counts.items()):
        print(f"{key:<24} {n:>7} bars")
    _ = InstrumentKind  # imported for the type union above


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kotsin-nse", description="NSE/MCX trading engine on 5paisa")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the engine, the API and the UI (default)")

    today = date.today()
    f = sub.add_parser("fetch-history", help="fill the Parquet history cache from the broker")
    f.add_argument("--symbols", required=True, help="comma-separated underlying symbols")
    f.add_argument("--start", default=(today - timedelta(days=365)).isoformat())
    f.add_argument("--end", default=today.isoformat())

    return p


def cli() -> None:
    args = build_parser().parse_args()
    try:
        settings = load_settings()
    except UnknownConfigKeys as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(settings.log_level, json_output=settings.env == "prod")
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    command = args.command or "serve"
    if command == "serve":
        asyncio.run(serve(settings))
    elif command == "fetch-history":
        asyncio.run(fetch_history(settings, args))
    else:  # pragma: no cover - argparse rejects anything else
        raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    cli()
