"""Exit policies over ``env.ACTIONS``: the backtester's hand rules (baseline), behaviour
policies for offline data collection, and a learned policy loaded from an artefact JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .env import ACTION_INDEX, ACTIONS, OBS_COLUMNS

_PEAK = OBS_COLUMNS.index("peak_r")
_T1 = OBS_COLUMNS.index("t1_r")
_GAIN = OBS_COLUMNS.index("peak_gain_pct")


class ExitPolicy(Protocol):
    name: str

    def act(self, obs: np.ndarray) -> int: ...


def phi(z: np.ndarray) -> np.ndarray:
    """Quadratic feature map on standardised observations: [1, z, z²] (2d+1 terms)."""
    z = np.asarray(z, dtype=float)
    if z.ndim == 1:
        return np.concatenate([[1.0], z, z * z])
    return np.concatenate([np.ones((z.shape[0], 1)), z, z * z], axis=1)


class LadderPolicy:
    """Reproduces ``research/backtest.py::_manage``: once T1 prints the stop moves to entry;
    once the peak gain reaches ``trail_arm_pct`` of entry the stop trails the peak by
    ``giveback_pct`` of the gain (the env applies the percentage); otherwise hold. Targets, the
    force-flat and the time stop are the environment's, not the policy's."""

    name = "ladder"

    def __init__(self, trail_arm_pct: float = 3.0) -> None:
        self.trail_arm_pct = trail_arm_pct

    def act(self, obs: np.ndarray) -> int:
        if float(obs[_GAIN]) >= self.trail_arm_pct:
            return ACTION_INDEX["trail_ladder"]
        t1 = float(obs[_T1])
        if t1 > 0 and float(obs[_PEAK]) >= t1:
            return ACTION_INDEX["lock_0r"]
        return ACTION_INDEX["hold"]


class HoldToTimeStopPolicy:
    name = "hold_only"

    def act(self, obs: np.ndarray) -> int:
        return ACTION_INDEX["hold"]


class RandomExitPolicy:
    """Behaviour policy for data collection: mostly hold, sometimes a random tightening/exit."""

    name = "random"

    def __init__(self, seed: int = 0, p_hold: float = 0.6) -> None:
        self.rng = np.random.default_rng(seed)
        self.p_hold = p_hold

    def act(self, obs: np.ndarray) -> int:
        if self.rng.random() < self.p_hold:
            return ACTION_INDEX["hold"]
        return int(self.rng.integers(1, len(ACTIONS)))


class EpsilonLadderPolicy:
    name = "eps_ladder"

    def __init__(self, eps: float = 0.2, seed: int = 0, trail_arm_pct: float = 3.0) -> None:
        self.eps = eps
        self.rng = np.random.default_rng(seed)
        self.ladder = LadderPolicy(trail_arm_pct)

    def act(self, obs: np.ndarray) -> int:
        if self.rng.random() < self.eps:
            return int(self.rng.integers(0, len(ACTIONS)))
        return self.ladder.act(obs)


class LearnedExitPolicy:
    """Greedy over Q(s, a) = w_a · phi((obs − mu) / sigma)."""

    name = "learned"

    def __init__(self, weights: np.ndarray, mu: np.ndarray, sigma: np.ndarray, meta: dict[str, Any] | None = None) -> None:
        self.weights = np.asarray(weights, dtype=float)  # (n_actions, n_phi)
        self.mu = np.asarray(mu, dtype=float)
        self.sigma = np.where(np.asarray(sigma, dtype=float) > 0, sigma, 1.0)
        self.meta = meta or {}

    def q_values(self, obs: np.ndarray) -> np.ndarray:
        return self.weights @ phi((np.asarray(obs, dtype=float) - self.mu) / self.sigma)

    def act(self, obs: np.ndarray) -> int:
        return int(np.argmax(self.q_values(obs)))

    def to_artefact(self) -> dict[str, Any]:
        return {
            "kind": "exit_policy",
            "obs_columns": OBS_COLUMNS,
            "actions": list(ACTIONS),
            "mu": self.mu.tolist(),
            "sigma": self.sigma.tolist(),
            "weights": self.weights.tolist(),
            "meta": self.meta,
        }

    @classmethod
    def from_artefact(cls, d: dict[str, Any]) -> LearnedExitPolicy:
        if d.get("obs_columns") != OBS_COLUMNS or d.get("actions") != list(ACTIONS):
            raise ValueError("artefact was trained with a different observation/action space")
        return cls(np.asarray(d["weights"]), np.asarray(d["mu"]), np.asarray(d["sigma"]), d.get("meta"))

    @classmethod
    def load(cls, path: Path | str) -> LearnedExitPolicy:
        return cls.from_artefact(json.loads(Path(path).read_text()))


def policy_by_name(name: str, seed: int = 0) -> ExitPolicy:
    if name == "ladder":
        return LadderPolicy()
    if name == "hold_only":
        return HoldToTimeStopPolicy()
    if name == "random":
        return RandomExitPolicy(seed)
    if name == "eps_ladder":
        return EpsilonLadderPolicy(seed=seed)
    p = Path(name)
    if p.suffix == ".json" and p.exists():
        return LearnedExitPolicy.load(p)
    raise ValueError(f"unknown exit policy {name!r}")


__all__ = [
    "EpsilonLadderPolicy",
    "ExitPolicy",
    "HoldToTimeStopPolicy",
    "LadderPolicy",
    "LearnedExitPolicy",
    "RandomExitPolicy",
    "phi",
    "policy_by_name",
]
