"""Market data in, whoever supplies it. See ``feed/base.py`` for the seam."""

from __future__ import annotations

from ..config import Settings
from ..venue.fivepaisa.auth import Authenticator
from .base import MarketDataSource, OnDepth, OnOI, OnTick

#: sources that exist today; a vendor is added here when its adapter is written and tested
SOURCES = ("fivepaisa",)


def build_source(
    settings: Settings,
    auth: Authenticator,
    *,
    on_tick: OnTick,
    on_depth: OnDepth | None = None,
    on_oi: OnOI | None = None,
) -> MarketDataSource:
    if settings.data_source == "fivepaisa":
        from .fivepaisa import FivePaisaSource

        return FivePaisaSource(settings, auth, on_tick=on_tick, on_depth=on_depth, on_oi=on_oi)
    raise ValueError(f"unknown data source {settings.data_source!r}; known: {', '.join(SOURCES)}")
