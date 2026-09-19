"""Entry point. One process: engine + API + static UI on one port.

``uv run kotsin-nse`` boots the engine, mounts the API and serves ``frontend/dist`` from the same
port, so there is one systemd unit, one log and one thing to restart. The old stack needed five
JVMs, a Kafka, a Mongo, a Redis and a Vite dev server, brought up in a specific order — and a
watchdog that relaunched all five inside 30 seconds with heap flags sized for a machine that no
longer existed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
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


def cli() -> None:
    try:
        settings = load_settings()
    except UnknownConfigKeys as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(settings.log_level, json_output=settings.env == "prod")
    if not Path(settings.data_dir).exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    cli()
