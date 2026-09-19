"""structlog setup: console renderer in dev, JSON in prod. Timestamps are UTC; the UI renders IST."""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )
