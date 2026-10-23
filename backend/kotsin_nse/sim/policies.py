"""Baselines every learned agent must beat, and one that no agent can.

* ``Flat`` — never trades: 0, the honest floor.
* ``Random`` — enters with probability ``p_enter`` a step, call or put at random, holds
  ``hold_s``: what "trading randomly" costs on this day (the spread and charges, mostly).
* ``Momentum`` — buys the call when the last 5 minutes rose and buying led the flow (puts mirrored),
  holds ``hold_s``: the simplest reading of the order flow.
* ``Oracle`` — LOOKS AHEAD: buys the call when the underlying will be ``edge_bps`` higher in
  ``hold_s``, the put when lower. Not a strategy — the ceiling: if even perfect direction cannot
  beat the costs at this horizon, nothing learned will.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from .env import BUY_CALL, BUY_PUT, EXIT, FEATURES, HOLD, OptionBuyEnv

F = {n: i for i, n in enumerate(FEATURES)}


class Policy:
    name = "policy"

    def act(self, env: OptionBuyEnv, obs: np.ndarray, can: list[bool]) -> int:
        raise NotImplementedError


class Flat(Policy):
    name = "flat"

    def act(self, env: OptionBuyEnv, obs: np.ndarray, can: list[bool]) -> int:
        return EXIT if can[EXIT] else HOLD


@dataclass
class Random(Policy):
    p_enter: float = 0.01
    hold_s: float = 300.0
    seed: int = 0
    name: str = "random"
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def act(self, env: OptionBuyEnv, obs: np.ndarray, can: list[bool]) -> int:
        if env.pos is not None:
            return EXIT if env.t - env.pos.entry_ts >= self.hold_s else HOLD
        if self._rng.random() < self.p_enter:
            a = self._rng.choice((BUY_CALL, BUY_PUT))
            return a if can[a] else HOLD
        return HOLD


@dataclass
class Momentum(Policy):
    ret_bps: float = 10.0
    flow: float = 0.1
    hold_s: float = 300.0
    name: str = "momentum"

    def act(self, env: OptionBuyEnv, obs: np.ndarray, can: list[bool]) -> int:
        if env.pos is not None:
            return EXIT if env.t - env.pos.entry_ts >= self.hold_s else HOLD
        r, d = obs[F["ret_5m_bps"]], obs[F["delta_share_5m"]]
        if r >= self.ret_bps and d >= self.flow and can[BUY_CALL]:
            return BUY_CALL
        if r <= -self.ret_bps and d <= -self.flow and can[BUY_PUT]:
            return BUY_PUT
        return HOLD


@dataclass
class Oracle(Policy):
    edge_bps: float = 15.0
    hold_s: float = 300.0
    name: str = "oracle (looks ahead)"

    def act(self, env: OptionBuyEnv, obs: np.ndarray, can: list[bool]) -> int:
        if env.pos is not None:
            return EXIT if env.t - env.pos.entry_ts >= self.hold_s else HOLD
        now, later = env.day.mid(env.t), env.day.mid(env.t + self.hold_s)
        if not now or not later:
            return HOLD
        move = (later / now - 1) * 1e4
        if move >= self.edge_bps and can[BUY_CALL]:
            return BUY_CALL
        if move <= -self.edge_bps and can[BUY_PUT]:
            return BUY_PUT
        return HOLD


__all__ = ["Flat", "Momentum", "Oracle", "Policy", "Random"]
