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
* **at-pivot** (operator's rule, 2026-09-23 — read on the equity *and* the front future): the
  trigger candle closing *on* a key level is graded, not thresholded — nearness (full at the level,
  nothing 0.5 ATR away) × the level's strength (a lone daily 4.0, a daily+weekly wall 7.2, a tight
  CPR 12) × how far the close has crossed it (crossed by ≥ 0.5 ATR is a breakout: off) × volume
  (dried = full weight, average = half, a surge ≥ 2.5 on either leg vetoes; a rejection candle —
  extreme pierced, close back, body-opposed — counts for three quarters on its own). The best leg's
  score ≥ 0.5 → COUNTER. SBILIFE 2026-09-23: the future closed 0.01 ATR from its daily S1 on
  0.79 / 0.51 volume — a fade the wall rule cannot see, because the level was not ahead;
* anything else → IN_TREND (the base signal stands; the numbers are still stamped so the card
  shows why).

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
#: at-pivot: nearness runs out at this many ATR from the close; a close crossed this far beyond the
#: level is a breakout, not a fade
AT_PIVOT_ATR = 0.5
#: the extreme must have gone at least this far through the level for a close back on the near
#: side to count as a rejection (with a body-opposedness of at least REJECT_OPPOSED)
REJECT_PIERCE_ATR = 0.1
REJECT_OPPOSED = 0.40
#: volume, on the T-2…T-7 baseline: both bars under DRIED_V is no conviction; a trigger bar at or
#: over SURGE_CONVICTION on either leg is conviction and vetoes the fade
DRIED_V = 0.85
SURGE_CONVICTION = 2.5
COUNTER_SCORE_MIN = 0.5
#: the levels a close can sit "on": the CPR and the first two floors/ceilings of every timeframe
KEY_LEVELS = frozenset({"PIVOT", "TC", "BC", "R1", "S1", "R2", "S2"})


@dataclass(frozen=True, slots=True)
class Leg:
    """One instrument's side of the trigger: the equity, or its front future."""

    name: str
    open: float
    high: float
    low: float
    close: float
    atr: float
    points: list[PivotPoint]
    surge_t: float | None = None
    surge_t1: float | None = None

    @property
    def volume(self) -> str:
        if self.surge_t is None or self.surge_t1 is None:
            return "unknown"
        if self.surge_t >= SURGE_CONVICTION:
            return "surge"
        if 0 < self.surge_t < DRIED_V and 0 < self.surge_t1 < DRIED_V:
            return "dried"
        return "average"


@dataclass(frozen=True, slots=True)
class PivotRead:
    """How one leg's close sits against the nearest key pivot, and what that is worth as a fade."""

    leg: str
    level: float
    members: tuple[str, ...]
    strength: float
    dist_atr: float  # |close − level| / ATR
    crossed_atr: float  # signed: how far the close is beyond the level in the trade direction
    pierced_atr: float  # how far the extreme went beyond the level
    rejected: bool
    volume: str
    surge_t: float | None
    surge_t1: float | None
    score: float

    def to_json(self) -> dict:
        return {
            "leg": self.leg, "level": round(self.level, 2), "members": list(self.members),
            "strength": round(self.strength, 2), "distAtr": round(self.dist_atr, 2),
            "crossedAtr": round(self.crossed_atr, 2), "piercedAtr": round(self.pierced_atr, 2),
            "rejected": self.rejected, "volume": self.volume,
            "surgeT": None if self.surge_t is None else round(self.surge_t, 2),
            "surgeT1": None if self.surge_t1 is None else round(self.surge_t1, 2),
            "score": round(self.score, 2),
        }

    def summary(self) -> str:
        vol = (
            f"{self.volume} {self.surge_t:.2f}/{self.surge_t1:.2f}"
            if self.surge_t is not None and self.surge_t1 is not None
            else self.volume
        )
        return (
            f"{self.leg} {','.join(self.members)} {self.level:.2f} ({self.strength:.1f}) {self.dist_atr:.2f} ATR from "
            f"close, crossed {self.crossed_atr:+.2f} ATR{', rejected' if self.rejected else ''}, volume {vol} → score {self.score:.2f}"
        )


def at_pivot_read(leg: Leg, *, bullish: bool, other_volume: str = "unknown") -> PivotRead | None:
    """The key pivot (cluster) nearest the close and the fade score of sitting on it."""
    if leg.atr <= 0 or leg.close <= 0:
        return None
    key = [p for p in leg.points if p.price > 0 and p.label.split(".")[-1] in KEY_LEVELS]
    if not key:
        return None
    best: tuple[float, float, list[PivotPoint]] | None = None
    for anchor in key:
        grp = [p for p in key if abs(p.price - anchor.price) <= CLUSTER_ATR * leg.atr]
        near = min(abs(p.price - leg.close) for p in grp)
        strength = sum(p.weight for p in grp)
        cand = (near, -strength, grp)
        if best is None or cand[:2] < best[:2]:
            best = cand
    assert best is not None
    _, neg_strength, grp = best
    line = min(grp, key=lambda p: abs(p.price - leg.close)).price
    sign = 1 if bullish else -1
    crossed = sign * (leg.close - line) / leg.atr
    pierced = sign * ((leg.high if bullish else leg.low) - line) / leg.atr
    rng = leg.high - leg.low
    close_pos = (leg.close - leg.low) / rng if rng > 0 else 0.5
    opposed = (1 - close_pos) if bullish else close_pos
    rejected = pierced >= REJECT_PIERCE_ATR and crossed < 0 and opposed >= REJECT_OPPOSED
    dist = abs(crossed)
    nearness = max(0.0, 1 - dist / AT_PIVOT_ATR)
    strength = -neg_strength
    s = min(2.0, strength / COUNTER_WALL_MIN)
    cross_f = 1.0 if crossed <= 0 else max(0.0, 1 - crossed / AT_PIVOT_ATR)
    if leg.volume == "surge" or other_volume == "surge":
        vol_f = 0.0
    elif leg.volume == "dried":
        vol_f = 1.0
    elif other_volume == "dried":
        vol_f = 0.75
    else:
        vol_f = 0.5
    if rejected and vol_f > 0:
        vol_f = max(vol_f, 0.75)
    return PivotRead(
        leg=leg.name, level=line, members=tuple(p.label for p in sorted(grp, key=lambda p: p.price)),
        strength=strength, dist_atr=dist, crossed_atr=crossed, pierced_atr=pierced, rejected=rejected,
        volume=leg.volume, surge_t=leg.surge_t, surge_t1=leg.surge_t1,
        score=nearness * s * cross_f * vol_f,
    )


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
    wall_leg: str = ""
    reads: tuple[PivotRead, ...] = ()
    summary: str = ""

    def to_json(self) -> dict:
        return {
            "route": self.route, "reason": self.reason, "summary": self.summary,
            "wall": {**self.wall.to_json(), "leg": self.wall_leg},
            "reads": [r.to_json() for r in self.reads],
        }


def counter_route(legs: list[Leg], *, bullish: bool, st_flipped: bool, wall_min: float = COUNTER_WALL_MIN) -> CounterDecision:
    """COUNTER on a genuine ST flip into a wall ≥ ``wall_min`` on either leg, or on the best
    at-pivot read scoring ≥ ``COUNTER_SCORE_MIN``; IN_TREND otherwise. Every route carries the
    numbers of both legs."""
    legs = [leg for leg in legs if leg.atr > 0 and leg.close > 0]
    if not legs:
        return CounterDecision("IN_TREND", "insufficient data (no leg with an ATR)", NO_WALL)
    walls = {leg.name: evaluate_wall(leg.points, bullish=bullish, close=leg.close, high=leg.high, low=leg.low, atr=leg.atr) for leg in legs}
    wall_leg, wall = max(walls.items(), key=lambda kv: kv[1].strength)
    vols = {leg.name: leg.volume for leg in legs}
    reads = tuple(
        r for leg in legs
        if (r := at_pivot_read(leg, bullish=bullish, other_volume=next((v for n, v in vols.items() if n != leg.name), "unknown"))) is not None
    )
    best = max(reads, key=lambda r: r.score) if reads else None
    wall_txt = f"{wall.grade(wall_min)} wall {wall.strength:.2f}" + (
        f" ahead on the {wall_leg} from {len(wall.members)} level(s) {wall.timeframes}, {wall.dist_atr:.2f} ATR from close" if wall.members else " ahead"
    )
    if st_flipped and wall.strength >= wall_min:
        return CounterDecision(
            "COUNTER", f"wall-counter: {wall_txt} (≥ {wall_min}) — fade", wall, wall_leg, reads,
            f"wall {wall.strength:.1f} ({wall.timeframes}) on the {wall_leg}",
        )
    if best is not None and best.score >= COUNTER_SCORE_MIN:
        return CounterDecision(
            "COUNTER", f"at-pivot: {best.summary()} (≥ {COUNTER_SCORE_MIN}) — fade; {wall_txt}", wall, wall_leg, reads,
            f"at-pivot {best.leg} {','.join(best.members)} {best.dist_atr:.2f} ATR, {best.volume}, score {best.score:.2f}",
        )
    why = "no genuine ST flip; " if not st_flipped else ""
    at = f"; nearest pivot: {best.summary()}" if best is not None else ""
    return CounterDecision(
        "IN_TREND", f"{why}{wall_txt} (< {wall_min}){at} — with trend", wall, wall_leg, reads,
        (f"wall {wall.strength:.1f}" if wall.members else "no wall") + (f" · nearest pivot score {best.score:.2f}" if best else ""),
    )


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
