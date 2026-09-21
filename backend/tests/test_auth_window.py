"""The TOTP window guard: one code per window, never at a window's edges.

The edge rule is measured, not theoretical — see ``WINDOW_EDGE_HEAD_S`` in ``auth.py``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kotsin_nse.venue.fivepaisa import auth as auth_mod
from kotsin_nse.venue.fivepaisa.auth import (
    TOTP_WINDOW_S,
    WINDOW_EDGE_HEAD_S,
    WINDOW_EDGE_TAIL_S,
    Authenticator,
)


class _Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self, t: float) -> None:
        self.t = t
        self.slept: list[float] = []

    def time(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class _Time:
    def __init__(self, clock: _Clock) -> None:
        self.time = clock.time

    def __getattr__(self, name: str):
        return getattr(time, name)


class _Asyncio:
    def __init__(self, clock: _Clock) -> None:
        self.sleep = clock.sleep

    def __getattr__(self, name: str):
        return getattr(asyncio, name)


def _auth(window_used: int | None = None) -> Authenticator:
    a = Authenticator.__new__(Authenticator)
    a._window_used = window_used
    return a


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    c = _Clock(0.0)
    monkeypatch.setattr(auth_mod, "time", _Time(c))
    monkeypatch.setattr(auth_mod, "asyncio", _Asyncio(c))
    return c


async def test_mid_window_logs_in_immediately(clock: _Clock) -> None:
    clock.t = 1_790_015_460 + 12.0  # 12 s into a window
    assert await _auth()._wait_for_fresh_window() == 1_790_015_460 // TOTP_WINDOW_S
    assert clock.slept == []


async def test_window_head_is_avoided(clock: _Clock) -> None:
    # 2026-09-22 00:01:00.2 IST, the exact instant the midnight re-login failed
    clock.t = 1_790_015_460 + 0.2
    window = await _auth()._wait_for_fresh_window()
    assert window == 1_790_015_460 // TOTP_WINDOW_S
    assert clock.slept == [pytest.approx(WINDOW_EDGE_HEAD_S - 0.2)]
    assert clock.t - window * TOTP_WINDOW_S >= WINDOW_EDGE_HEAD_S


async def test_window_tail_rolls_into_next_window(clock: _Clock) -> None:
    clock.t = 1_790_015_460 + TOTP_WINDOW_S - 0.5
    window = await _auth()._wait_for_fresh_window()
    assert window == 1_790_015_460 // TOTP_WINDOW_S + 1
    into = clock.t - window * TOTP_WINDOW_S
    assert WINDOW_EDGE_HEAD_S <= into <= TOTP_WINDOW_S - WINDOW_EDGE_TAIL_S


async def test_used_window_waits_for_the_next(clock: _Clock) -> None:
    base = 1_790_015_460
    clock.t = base + 10.0
    window = await _auth(window_used=base // TOTP_WINDOW_S)._wait_for_fresh_window()
    assert window == base // TOTP_WINDOW_S + 1
    assert len(clock.slept) == 1
    assert clock.t - window * TOTP_WINDOW_S == pytest.approx(WINDOW_EDGE_HEAD_S)
