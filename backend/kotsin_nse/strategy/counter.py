"""FUDKII counter-trend routing — the reference stack's wall-strength COUNTER, ported.

The old stack (streamingcandle ``FudkiiSignalTrigger`` + ``RevisedRouteEvaluator``) went through
three counter-trend designs. The geometry fade of 2026-06 (one pivot within ``k1`` ATR, a
pierce-and-reject with a 0.40 body) replayed net-losing and was shadowed; the F14 candle-pattern
flip was hidden after 49 cases (flipped plan reached T1 first 35 % vs 45 %). What runs live since
2026-09-11 (``fudkii.rt.counter.execute.enabled=true``) is this one:

* **the wall** — every daily / weekly / monthly classic level that is *ahead* of the close in the
  signal's direction and either inside the trigger candle or just past its extreme (within
  ``REACH_ATR`` × ATR30m), scored by nearness rank (1.0 / 0.8 / 0.6 / 0.4 × the timeframe weight)
  and clustered within ``CLUSTER_ATR`` × ATR30m; the strongest cluster is the wall;
* **the rule** — a genuine SuperTrend flip *and* wall strength ≥ ``COUNTER_WALL_MIN`` (5.2: a daily
  level alone is not a wall, a daily plus a weekly is) → COUNTER: fade the signal with the opposite
  OTM, plan (stop, targets) recomputed by the same confluence engine for the flipped direction, one
  lot out per target;
* anything else → IN_TREND (the base signal stands; the wall numbers are still stamped so the
  card shows why).

Here the fade is not *instead of* the in-trend trade — it is its own pair of books (CT-X, CT-Y)
beside the in-trend mirrors, so the two exits and the two directions can be compared on the same
signals.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..bars.pivots import PivotPoint, Zone, compute_confluence
from ..domain import Direction
from .base import Signal
from .keys import StrategyKey

#: Live values (``fudkii.rt.counter.wall.*``, 2026-09-11).
COUNTER_WALL_MIN = 5.2
CLUSTER_ATR = 0.25
REACH_ATR = 0.5
#: nearness scheme: the nearest qualifying level counts in full, the next 80 %, then 60 %, then 40 %
NEARNESS_RANK = (1.0, 0.8, 0.6, 0.4)


@dataclass(frozen=True, slots=True)
class WallEval:
    strength: float
    members: tuple[str, ...]
    timeframes: str  # "1d/1wk"
    dist_atr: float | None  # nearest member's distance from the close, in ATR

    def grade(self, threshold: float = COUNTER_WALL_MIN) -> str:
        if self.strength <= 0:
            return "NONE"
        if self.strength >= threshold * 1.5:
            return "VERY STRONG"
        if self.strength >= threshold:
            return "STRONG"
        if self.strength >= threshold * 0.7:
            return "AVERAGE"
        return "WEAK"

    def to_json(self) -> dict:
        return {
            "strength": round(self.strength, 2),
            "members": list(self.members),
            "timeframes": self.timeframes,
            "distAtr": None if self.dist_atr is None else round(self.dist_atr, 2),
            "grade": self.grade(),
        }


NO_WALL = WallEval(0.0, (), "", None)


def evaluate_wall(
    points: list[PivotPoint],
    *,
    bullish: bool,
    close: float,
    high: float,
    low: float,
    atr: float,
    cluster_atr: float = CLUSTER_ATR,
    reach_atr: float = REACH_ATR,
) -> WallEval:
    """The strongest multi-timeframe cluster standing in the signal's way (nearness scheme)."""
    if atr <= 0 or close <= 0 or not points:
        return NO_WALL
    extreme = high if bullish else low
    ahead: list[PivotPoint] = []
    for p in points:
        if p.price <= 0:
            continue
        in_candle = low <= p.price <= high and (p.price >= close if bullish else p.price <= close)
        just_past = (
            extreme < p.price <= extreme + reach_atr * atr
            if bullish
            else extreme - reach_atr * atr <= p.price < extreme
        )
        if in_candle or just_past:
            ahead.append(p)
    if not ahead:
        return NO_WALL
    by_dist = sorted(ahead, key=lambda p: abs(p.price - close))
    score = {id(p): p.weight * (NEARNESS_RANK[i] if i < len(NEARNESS_RANK) else NEARNESS_RANK[-1]) for i, p in enumerate(by_dist)}
    best, group = 0.0, []
    for anchor in ahead:
        grp = [p for p in ahead if abs(p.price - anchor.price) <= cluster_atr * atr]
        total = sum(score[id(p)] for p in grp)
        if total > best:
            best, group = total, grp
    if not group:
        return NO_WALL
    nearest = min(abs(p.price - close) for p in group)
    return WallEval(
        strength=best,
        members=tuple(p.label for p in sorted(group, key=lambda p: p.price)),
        timeframes="/".join(sorted({p.tf for p in group})),
        dist_atr=nearest / atr,
    )


@dataclass(frozen=True, slots=True)
class CounterDecision:
    route: str  # "COUNTER" | "IN_TREND"
    reason: str
    wall: WallEval

    def to_json(self) -> dict:
        return {"route": self.route, "reason": self.reason, "wall": self.wall.to_json()}


def counter_route(
    points: list[PivotPoint],
    *,
    bullish: bool,
    close: float,
    high: float,
    low: float,
    atr: float,
    st_flipped: bool,
    wall_min: float = COUNTER_WALL_MIN,
) -> CounterDecision:
    """COUNTER when a genuine ST flip runs into a wall of at least ``wall_min``; IN_TREND otherwise."""
    w = evaluate_wall(points, bullish=bullish, close=close, high=high, low=low, atr=atr)
    what = f"{w.grade(wall_min)} wall {w.strength:.2f}" + (
        f" from {len(w.members)} level(s) {w.timeframes}, {w.dist_atr:.2f} ATR from close" if w.members else ""
    )
    if not st_flipped:
        return CounterDecision("IN_TREND", f"no genuine ST flip — {what}", w)
    if w.strength >= wall_min:
        return CounterDecision("COUNTER", f"wall-counter: {what} (≥ {wall_min}) — fade", w)
    return CounterDecision("IN_TREND", f"{what} (< {wall_min}) — with trend", w)


def flipped_signal(
    sig: Signal,
    *,
    key: StrategyKey,
    zones: list[Zone],
    atr: float,
    tick_size: float,
    decision: CounterDecision,
) -> Signal | None:
    """The fade: the same trigger, the opposite direction, its own confluence plan (the nearest zone
    behind the close on the flipped side is the stop, the walls ahead the targets). ``None`` when
    the flipped side has no wall to aim at — a fade with nowhere to go is not a trade."""
    flipped = Direction.BEARISH if sig.direction is Direction.BULLISH else Direction.BULLISH
    conf = compute_confluence(
        close=sig.entry, bullish=flipped is Direction.BULLISH, zones=zones, atr_value=atr, tick_size=tick_size
    )
    if not conf.targets or conf.stop <= 0:
        return None
    return replace(
        sig,
        strategy=key,
        direction=flipped,
        stop=conf.stop,
        targets=conf.targets,
        grade=conf.grade,
        rr=round(conf.rr, 3),
        reason=f"COUNTER fade of {sig.signal_id}: {decision.reason}",
        source_signal_id=sig.signal_id,
        evidence={**dict(sig.evidence), "counter_wall": round(decision.wall.strength, 2), "rr": round(conf.rr, 3)},
        context={
            **dict(sig.context),
            "counter": decision.to_json(),
            "confluence": {
                "stop": conf.stop, "stop_zone": conf.stop_zone, "targets": list(conf.targets),
                "target_zones": list(conf.target_zones), "grade": conf.grade, "rr": round(conf.rr, 3),
                "fortress": conf.fortress, "room_ratio": round(conf.room_ratio, 3), "note": conf.note,
            },
        },
    )
