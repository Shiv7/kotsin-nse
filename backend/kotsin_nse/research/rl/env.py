"""Gym-style environment for EXIT policies on one backtest trade.

Episode = one position from its fill bar to its close; step = one decision-frame bar; observation
= position state + point-in-time market features from the bar window; reward = change in net P&L
in R (initial risk), so symbols and price levels compare. The market simulation is the
backtester's: a stop is checked against the next bar's open (gap) and extreme, the last target
closes the trade at its price, the bar that contains the session's force-flat closes it at its
close, charges are the CostModel's on both legs, exits slip.

Actions only ever TIGHTEN the stop or exit — the risk layer stays in charge — and include the
backtester's own proportional trail (``trail_ladder``), so the hand rule set is expressible
exactly and the learner can only be compared against it on equal terms.

One deliberate difference: a bar that OPENS through the stop fills at the open here
(``stop_gap``); the backtester fills it at the stop, which is optimistic. Both policies are graded
in this environment, so the comparison is like for like.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

import numpy as np

from ...bars.unified import UnifiedBar
from ...config import Segment
from ...market.session import TF_SECONDS, ist_day, ist_hm, past_force_flat

OBS_COLUMNS = [
    "r_now",
    "peak_r",
    "drawdown_from_peak_r",
    "bars_held",
    "bars_to_flat",
    "t1_r",
    "peak_gain_pct",
    "ret_3",
    "ret_12",
    "rv_12",
    "surge_20",
    "vwap_dist_12",
    "pos_in_range_48",
    "hour_sin",
    "hour_cos",
]
#: every action except hold / exit_now sets the stop to a level and only ever tightens it
ACTIONS = (
    "hold",
    "lock_0r",
    "lock_1r",
    "lock_2r",
    "lock_3r",
    "trail_1_5r",
    "trail_1r",
    "trail_0_5r",
    "trail_ladder",  # the backtester's rule: stop = peak − giveback% of the peak gain
    "exit_now",
)
ACTION_INDEX = {a: i for i, a in enumerate(ACTIONS)}


def market_features(window: Sequence[UnifiedBar]) -> dict[str, float]:
    """Point-in-time features from a bar window ending at the current bar (≤ 49 bars used)."""
    n = len(window)
    cur = window[-1]
    closes = [b.close for b in window]

    def lret(k: int) -> float:
        return math.log(closes[-1] / closes[-1 - k]) if n > k and closes[-1 - k] > 0 else 0.0

    rv = 0.0
    if n >= 13:
        lr = [math.log(closes[i] / closes[i - 1]) for i in range(n - 12, n) if closes[i - 1] > 0]
        if len(lr) >= 3:
            rv = float(np.std(lr)) * math.sqrt(12)
    vols = [b.volume for b in window[-21:-1]]
    med = median(vols) if len(vols) >= 5 else 0.0
    surge = cur.volume / med if med > 0 else 0.0
    w12 = window[-12:]
    vv = sum(b.volume for b in w12)
    vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in w12) / vv if vv > 0 else 0.0
    vwap_dist = (cur.close / vwap - 1) if vwap > 0 else 0.0
    w48 = window[-48:]
    hi, lo = max(b.high for b in w48), min(b.low for b in w48)
    pos = (cur.close - lo) / (hi - lo) if hi > lo else 0.5
    hm = ist_hm(cur.ts)
    hour = int(hm[:2]) + int(hm[3:5]) / 60
    return {
        "ret_3": lret(3),
        "ret_12": lret(12),
        "rv_12": rv,
        "surge_20": surge,
        "vwap_dist_12": vwap_dist,
        "pos_in_range_48": pos,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
    }


def contains_force_flat(segment: Segment, ts: float, tf: str) -> bool:
    """The bar starting at ``ts`` contains the session's force-flat time."""
    return past_force_flat(segment, ts + TF_SECONDS.get(tf, 1800) - 1)


def bars_to_flat(bars: Sequence[UnifiedBar], i: int, segment: Segment, tf: str, max_look: int = 16) -> int:
    """How many more bars this position may live before the session flattens it."""
    day = ist_day(bars[i].ts)
    n = 0
    for b in bars[i + 1 : i + 1 + max_look]:
        if ist_day(b.ts) != day or contains_force_flat(segment, b.ts, tf):
            break
        n += 1
    return n


def compute_obs(
    *,
    side: int,
    entry: float,
    r_unit: float,
    peak_r: float,
    bars_held: int,
    bars_left: int,
    t1_r: float,
    window: Sequence[UnifiedBar],
) -> np.ndarray:
    cur = window[-1]
    r_now = (cur.close - entry) * side / r_unit if r_unit > 0 else 0.0
    f = market_features(window)
    row = {
        "r_now": r_now,
        "peak_r": peak_r,
        "drawdown_from_peak_r": peak_r - r_now,
        "bars_held": float(bars_held),
        "bars_to_flat": float(bars_left),
        "t1_r": t1_r,
        "peak_gain_pct": peak_r * r_unit / entry * 100 if entry > 0 else 0.0,
        **f,
    }
    return np.array([row[c] for c in OBS_COLUMNS], dtype=float)


def stop_after_action(
    action: str,
    *,
    side: int,
    entry: float,
    r_unit: float,
    stop: float,
    peak_r: float,
    giveback_pct: float,
) -> float:
    """The stop implied by an action (tighten-only; ``hold``/``exit_now`` leave it unchanged)."""
    target: float | None = None
    if action.startswith("lock_"):
        k = float(action[5:-1].replace("_", "."))
        target = entry + side * k * r_unit
    elif action == "trail_ladder":
        if peak_r > 0:
            target = entry + side * peak_r * (1 - giveback_pct / 100) * r_unit
    elif action.startswith("trail_"):
        k = float(action[6:-1].replace("_", "."))
        target = entry + side * (peak_r - k) * r_unit
    if target is None:
        return stop
    return max(stop, target) if side > 0 else min(stop, target)


@dataclass(slots=True)
class ExitEpisode:
    symbol: str
    segment: Segment
    side: int  # +1 long, −1 short
    entry: float  # fill price (slippage already applied)
    r_unit: float  # |entry − initial stop|
    entry_index: int  # bar index of the fill; the policy first observes this bar's close
    signal_ts: int
    targets_r: tuple[float, ...]  # targets in R from the fill
    qty: int
    multiplier: int
    charges: Callable[[float, float], float]  # (entry_px, exit_px) → ₹ for both legs
    slip_bps: float = 5.0
    fee_mult: float = 1.0
    tf: str = "30m"

    @property
    def t1_r(self) -> float:
        return self.targets_r[0] if self.targets_r else 0.0


@dataclass(slots=True)
class StepResult:
    obs: np.ndarray
    reward: float
    done: bool
    info: dict[str, Any] = field(default_factory=dict)


class ExitEnv:
    """Deterministic given (bars, episode, actions)."""

    def __init__(
        self,
        bars: Sequence[UnifiedBar],
        episode: ExitEpisode,
        *,
        max_bars: int | None = 8,
        giveback_pct: float = 40.0,
        window: int = 49,
    ) -> None:
        self.bars = bars
        self.ep = episode
        self.max_bars = max_bars
        self.giveback_pct = giveback_pct
        self.window = window
        self.i = episode.entry_index
        self.stop = episode.entry - episode.side * episode.r_unit
        self.peak_r = 0.0
        self.done = False
        self.exit_price: float | None = None
        self.exit_reason = ""
        self.fees_r = 0.0
        self.targets_hit = 0
        self._last_net_r = self._net_r(bars[self.i].close)
        # the fill bar is managed by the backtester too: a stop or target on it ends the
        # episode before the policy ever acts
        self._check_bar(bars[self.i], first=True)

    # ---- accounting ---------------------------------------------------------------------------
    def _r(self, px: float) -> float:
        return (px - self.ep.entry) * self.ep.side / self.ep.r_unit

    def _fees_r(self, exit_px: float) -> float:
        money = self.ep.charges(self.ep.entry, exit_px) * self.ep.fee_mult
        return money / (self.ep.r_unit * self.ep.qty * self.ep.multiplier)

    def _net_r(self, px: float) -> float:
        return self._r(px) - self._fees_r(px)

    def obs(self) -> np.ndarray:
        lo = max(0, self.i - self.window + 1)
        return compute_obs(
            side=self.ep.side,
            entry=self.ep.entry,
            r_unit=self.ep.r_unit,
            peak_r=self.peak_r,
            bars_held=self.bars_held,
            bars_left=bars_to_flat(self.bars, self.i, self.ep.segment, self.ep.tf),
            t1_r=self.ep.t1_r,
            window=self.bars[lo : self.i + 1],
        )

    def reset(self) -> np.ndarray:
        return self.obs()

    # ---- dynamics -------------------------------------------------------------------------------
    def _check_bar(self, nb: UnifiedBar, *, first: bool = False) -> StepResult | None:
        """The backtester's order on one bar: gap at the open, stop, last target, force-flat,
        time stop. Returns the terminal StepResult, or None when the trade lives on."""
        side = self.ep.side
        if not first and ((side > 0 and nb.open <= self.stop) or (side < 0 and nb.open >= self.stop)):
            return self._finish(nb.open, "stop_gap")
        adverse = nb.low if side > 0 else nb.high
        if (side > 0 and adverse <= self.stop) or (side < 0 and adverse >= self.stop):
            return self._finish(self.stop, "stop")
        favourable = nb.high if side > 0 else nb.low
        self.peak_r = max(self.peak_r, self._r(favourable))
        # One rung per bar, as the backtester takes them; the last rung closes the trade at its
        # price. (Moving the stop to entry after T1 is the policy's call — the ladder does it.)
        targets = self.ep.targets_r
        if self.targets_hit < len(targets) and self._r(favourable) >= targets[self.targets_hit]:
            self.targets_hit += 1
            if self.targets_hit >= len(targets):
                return self._finish(self.ep.entry + side * targets[-1] * self.ep.r_unit, "target", slip=False)
        if contains_force_flat(self.ep.segment, nb.ts, self.ep.tf):
            return self._finish(nb.close, "force_flat")
        # the backtester counts the fill bar as bar 1
        if self.max_bars is not None and self.bars_held >= self.max_bars:
            return self._finish(nb.close, "time_stop")
        return None

    def step(self, action: int) -> StepResult:
        if self.done:
            raise RuntimeError("episode finished")
        act = ACTIONS[action]
        b = self.bars[self.i]
        if act == "exit_now":
            return self._finish(b.close, "policy_exit")
        self.stop = stop_after_action(
            act,
            side=self.ep.side,
            entry=self.ep.entry,
            r_unit=self.ep.r_unit,
            stop=self.stop,
            peak_r=self.peak_r,
            giveback_pct=self.giveback_pct,
        )
        self.i += 1
        if self.i >= len(self.bars):
            self.i = len(self.bars) - 1
            return self._finish(self.bars[-1].close, "data_end")
        nb = self.bars[self.i]
        terminal = self._check_bar(nb)
        if terminal is not None:
            return terminal
        net = self._net_r(nb.close)
        reward = net - self._last_net_r
        self._last_net_r = net
        return StepResult(self.obs(), reward, False, {"r_now": self._r(nb.close)})

    def _finish(self, px: float, reason: str, *, slip: bool = True) -> StepResult:
        fill = px - self.ep.side * px * self.ep.slip_bps / 1e4 if slip else px
        self.done = True
        self.exit_price = fill
        self.exit_reason = reason
        self.fees_r = self._fees_r(fill)
        net = self._net_r(fill)
        reward = net - self._last_net_r
        self._last_net_r = net
        return StepResult(
            self.obs(),
            reward,
            True,
            {"exit": fill, "reason": reason, "net_r": net, "bars_held": self.bars_held},
        )

    @property
    def net_r(self) -> float:
        return self._last_net_r

    @property
    def bars_held(self) -> int:
        """Bars the position was managed, the fill bar included — ``BtTrade.bars_held``."""
        return self.i - self.ep.entry_index + 1


__all__ = [
    "ACTIONS",
    "ACTION_INDEX",
    "OBS_COLUMNS",
    "ExitEnv",
    "ExitEpisode",
    "StepResult",
    "bars_to_flat",
    "compute_obs",
    "contains_force_flat",
    "market_features",
    "stop_after_action",
]
