"""Helpers the engine tests share."""

from __future__ import annotations

from typing import Any

from kotsin_nse.strategy.keys import STOP_MIRRORS, StrategyKey


def held(e: Any) -> list[Any]:
    """The engine's open positions, the stop-rule mirrors left out: a mirror copies every fill of its book
    (test_stop_mirrors.py pins them), so a test of which books entered reads the books themselves."""
    return [p for p in e.positions.values() if StrategyKey(p.strategy) not in STOP_MIRRORS]
