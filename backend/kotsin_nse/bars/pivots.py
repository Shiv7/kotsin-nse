"""Pivots, multi-timeframe confluence zones, and the stop/target ladder built from them.

``ClassicPivotComputer`` is ported verbatim, including its **R3/S3 convention**: ``R3 = P + 2(H−L)``
(Zerodha Kite / 5paisa), *not* the classic floor-trader ``R3 = H + 2(P−L)``. On H=110 L=90 C=100
those give 140 and 130. The Kite number is deliberate and the original comment is worth keeping:

    A pivot has no mechanism except coordination: a level nobody else can see cannot attract the
    orders that make it act as support or resistance. Correctness here is "matches what the rest of
    the market is quoting", not "derivable from the prettiest algebra".

Above that sits the confluence engine FUDKII's stop and targets come from. Daily / weekly / monthly
pivots are weighted, merged into **zones** by price proximity, and then:

* **SL = the nearest zone on the wrong side of the trade**, tick-rounded. Strength-agnostic — the
  time-of-day multiplier that used to scale it was removed on 2026-07-05 and is not coming back.
* **Targets = the next zones in the trade's direction** that are strong enough to be a wall, with an
  outward psychological round-number snap.
* **Grade** folds reward:risk, the strength of the wall being traded into ("fortress") and how much
  clear air lies ahead ("room ratio") into A/B/C/F. ``F`` is blocked at publish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Timeframe = Literal["1d", "1wk", "1mo"]

#: Weight each timeframe's levels carry in a zone's strength. Live values from the NSE stack
#: (``fudkii.rt.counter.wall.*``): a monthly level is real but slow, a daily level is what today's
#: tape is actually reacting to.
TF_WEIGHT: dict[str, float] = {"1d": 4.0, "1wk": 3.2, "1mo": 2.0}

#: A zone must reach this strength to count as a wall. 5.2 means "one daily level is not a wall;
#: a daily plus a weekly is". Live value.
WALL_MIN_STRENGTH = 5.2

#: Two levels merge into one zone when they are within this fraction of price of each other.
ZONE_TOLERANCE_PCT = 0.25


@dataclass(frozen=True, slots=True)
class PivotLevels:
    pivot: float
    r1: float
    r2: float
    r3: float
    r4: float
    s1: float
    s2: float
    s3: float
    s4: float
    fib_r1: float
    fib_r2: float
    fib_r3: float
    fib_s1: float
    fib_s2: float
    fib_s3: float
    cam_r1: float
    cam_r2: float
    cam_r3: float
    cam_r4: float
    cam_s1: float
    cam_s2: float
    cam_s3: float
    cam_s4: float
    tc: float
    bc: float

    @property
    def cpr_width(self) -> float:
        return self.tc - self.bc

    def cpr_width_pct(self, price: float) -> float | None:
        return None if price <= 0 else self.cpr_width / price * 100


def classic_pivots(high: float, low: float, close: float) -> PivotLevels | None:
    """Standard + Fibonacci + Camarilla + CPR from one OHLC period. ``None`` on bad input —
    a zero high is a missing bar, not a pivot at zero."""
    if high <= 0 or low <= 0 or close <= 0:
        return None
    p = (high + low + close) / 3.0
    rng = high - low
    bc_raw = (high + low) / 2.0
    tc_raw = 2 * p - bc_raw
    return PivotLevels(
        pivot=p,
        r1=2 * p - low,
        s1=2 * p - high,
        r2=p + rng,
        s2=p - rng,
        r3=p + 2 * rng,
        s3=p - 2 * rng,
        r4=p + 3 * rng,
        s4=p - 3 * rng,
        fib_r1=p + 0.382 * rng,
        fib_r2=p + 0.618 * rng,
        fib_r3=p + 1.0 * rng,
        fib_s1=p - 0.382 * rng,
        fib_s2=p - 0.618 * rng,
        fib_s3=p - 1.0 * rng,
        cam_r1=close + rng * 1.1 / 12,
        cam_r2=close + rng * 1.1 / 6,
        cam_r3=close + rng * 1.1 / 4,
        cam_r4=close + rng * 1.1 / 2,
        cam_s1=close - rng * 1.1 / 12,
        cam_s2=close - rng * 1.1 / 6,
        cam_s3=close - rng * 1.1 / 4,
        cam_s4=close - rng * 1.1 / 2,
        tc=max(tc_raw, bc_raw),
        bc=min(tc_raw, bc_raw),
    )


@dataclass(frozen=True, slots=True)
class PivotPoint:
    price: float
    label: str  # e.g. "1d.R2"
    tf: str
    weight: float


#: Camarilla levels are populated for display but carry **zero** weight in the confluence engine —
#: they were disabled on 2026-04-13 and never re-enabled. Keeping them at 0.0 rather than deleting
#: them makes that an explicit, reversible decision instead of a silent omission.
STANDARD_LEVELS = ("pivot", "r1", "r2", "r3", "r4", "s1", "s2", "s3", "s4", "tc", "bc")
FIB_LEVELS = ("fib_r1", "fib_r2", "fib_r3", "fib_s1", "fib_s2", "fib_s3")
FIB_WEIGHT_FACTOR = 0.5


def pivot_points(levels: PivotLevels, tf: str, *, include_fib: bool = True) -> list[PivotPoint]:
    w = TF_WEIGHT.get(tf, 1.0)
    out = [
        PivotPoint(getattr(levels, name), f"{tf}.{name.upper()}", tf, w)
        for name in STANDARD_LEVELS
        if getattr(levels, name) > 0
    ]
    if include_fib:
        out += [
            PivotPoint(getattr(levels, name), f"{tf}.{name.upper()}", tf, w * FIB_WEIGHT_FACTOR)
            for name in FIB_LEVELS
            if getattr(levels, name) > 0
        ]
    return out


@dataclass(slots=True)
class Zone:
    price: float  # the cluster mean — what an order would actually be placed at
    strength: float
    members: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def is_wall(self) -> bool:
        return self.strength >= WALL_MIN_STRENGTH


def cluster_zones(points: list[PivotPoint], *, tolerance_pct: float = ZONE_TOLERANCE_PCT) -> list[Zone]:
    """Merge nearby levels into zones. Single-linkage on price, which is what the original did:
    walk the sorted levels and start a new zone whenever the gap exceeds the tolerance."""
    live = sorted((p for p in points if p.price > 0), key=lambda p: p.price)
    zones: list[Zone] = []
    bucket: list[PivotPoint] = []

    def flush() -> None:
        if not bucket:
            return
        total = sum(p.weight for p in bucket)
        mean = sum(p.price * p.weight for p in bucket) / total if total else bucket[0].price
        zones.append(Zone(price=mean, strength=total, members=[p.label for p in bucket]))

    for p in live:
        if bucket and abs(p.price - bucket[-1].price) / max(p.price, 1e-9) * 100 > tolerance_pct:
            flush()
            bucket = []
        bucket.append(p)
    flush()
    return zones


#: The snap may never move a target by more than this fraction of its distance from the close.
#: Without the cap a target 0.5% away could be snapped to the next round hundred — a 9.5% move —
#: which would turn a 1:1 trade into a fictional 10:1 one purely through rounding.
MAX_SNAP_FRACTION = 0.2


def round_figure_snap(
    price: float, *, up: bool, step: float | None = None, anchor: float | None = None
) -> float:
    """Nudge a target outward to the nearest psychological round number.

    A target of 1,247 is worse than 1,250 for a long: orders pile at the round number and the last
    three rupees are where the fill stops coming.

    Two safety properties:

    * the snap always moves **away** from entry, so it can only make a target harder to reach —
      it can never flatter a backtest;
    * with ``anchor`` (the decision price) it is **capped** at :data:`MAX_SNAP_FRACTION` of the
      original distance. A cosmetic rounding must not change the reward:risk of the trade.
    """
    if price <= 0:
        return price
    if step is None:
        step = 100.0 if price >= 2000 else 50.0 if price >= 500 else 10.0 if price >= 100 else 1.0
    q = price / step
    snapped = (int(q) + 1) * step if up and q != int(q) else int(q) * step
    if up and snapped < price:
        snapped += step
    if not up and snapped > price:
        snapped -= step
    if anchor is not None:
        distance = abs(price - anchor)
        if distance > 0 and abs(snapped - price) > MAX_SNAP_FRACTION * distance:
            return round(price, 2)
    return round(snapped, 2)


@dataclass(frozen=True, slots=True)
class Confluence:
    """The stop/target ladder a signal ships with."""

    stop: float
    targets: tuple[float, ...]
    grade: str
    rr: float
    fortress: float  # strength of the first wall ahead — how hard T1 will be
    room_ratio: float  # distance to the next wall, in ATRs
    zones_considered: int
    stop_zone: str = ""
    target_zones: tuple[str, ...] = ()
    note: str = ""

    @property
    def blocked(self) -> bool:
        return self.grade == "F"


@dataclass(frozen=True, slots=True)
class GradePolicy:
    """Every number that turns geometry into a letter, in one place.

    ``rr_hard_floor`` is the publish gate: below it the signal is ``F`` and never leaves the engine.
    The live stack graded ~61% of signals ``F``, which is a lot of discarded work — the counters in
    ``Confluence`` exist so that is measurable rather than folklore.
    """

    rr_hard_floor: float = 1.0
    rr_a: float = 2.5
    rr_b: float = 1.8
    rr_c: float = 1.2
    room_min_atr: float = 0.5  # below this there is no room ahead: cap at C
    fortress_block: float = 12.0  # a wall this strong with rr < 1 is an F even so
    max_targets: int = 4
    #: EXPLORATORY — 0 = off (the inherited behaviour). A 1-year backtest on 24 NSE names
    #: (bt-875d519a3777, 2026-09-21) found the median confluence stop 0.23% from entry, inside
    #: a single 30m bar's noise: 80% of exits were the stop and grade A averaged −1.73R. This
    #: floors the stop at N ATR from the close so 1R is a market distance, not a pivot-line
    #: distance. It is a hypothesis under test, not a validated parameter.
    min_stop_atr: float = 0.0
    #: EXPLORATORY — only a wall (strength ≥ WALL_MIN_STRENGTH) may be the stop zone; a lone fib
    #: line 0.2% below price is not a structural level.
    stop_requires_wall: bool = False


def compute_confluence(
    *,
    close: float,
    bullish: bool,
    zones: list[Zone],
    atr_value: float,
    tick_size: float = 0.05,
    policy: GradePolicy | None = None,
) -> Confluence:
    """Stop = nearest zone behind; targets = the next zones ahead; grade = RR × fortress × room.

    When no zone sits behind the close the stop falls back to ``1 ATR`` away and the note says so —
    the old engine silently produced a stop at zero in that case, which the executor then treated as
    "no stop".
    """
    pol = policy or GradePolicy()
    sign = 1 if bullish else -1

    def tick(px: float) -> float:
        return round(round(px / tick_size) * tick_size, 4) if tick_size > 0 else round(px, 4)

    behind = [z for z in zones if (z.price < close) == bullish]
    ahead = [z for z in zones if (z.price > close) == bullish]
    if pol.stop_requires_wall:
        walls_behind = [z for z in behind if z.is_wall]
        behind = walls_behind or behind  # no wall behind at all → fall back rather than skip
    behind.sort(key=lambda z: abs(close - z.price))
    ahead.sort(key=lambda z: abs(z.price - close))

    if behind:
        stop_zone = behind[0]
        stop, stop_label, note = tick(stop_zone.price), ",".join(stop_zone.members), ""
    else:
        stop = tick(close - sign * atr_value)
        stop_label, note = "", "no zone behind close — stop fell back to 1 ATR"

    if pol.min_stop_atr > 0 and abs(close - stop) < pol.min_stop_atr * atr_value:
        stop = tick(close - sign * pol.min_stop_atr * atr_value)
        note = (note + "; " if note else "") + f"stop widened to {pol.min_stop_atr:g} ATR floor"

    risk = abs(close - stop)
    if risk <= 0:
        return Confluence(stop, (), "F", 0.0, 0.0, 0.0, len(zones), stop_label, (), "zero risk")

    walls = [z for z in ahead if z.is_wall][: pol.max_targets]
    targets = tuple(tick(round_figure_snap(z.price, up=bullish, anchor=close)) for z in walls)
    target_labels = tuple(",".join(z.members) for z in walls)

    if not targets:
        return Confluence(
            stop, (), "F", 0.0, 0.0, 0.0, len(zones), stop_label, (), "no wall ahead — no target"
        )

    rr = abs(targets[0] - close) / risk
    fortress = walls[0].strength
    next_wall = walls[1].price if len(walls) > 1 else targets[0]
    room_ratio = abs(next_wall - close) / atr_value if atr_value > 0 else 0.0

    if rr < pol.rr_hard_floor or (rr < 1.0 and fortress >= pol.fortress_block):
        grade = "F"
    elif rr >= pol.rr_a and room_ratio >= pol.room_min_atr:
        grade = "A"
    elif rr >= pol.rr_b:
        grade = "B"
    elif rr >= pol.rr_c:
        grade = "C"
    else:
        grade = "F"
    if grade in ("A", "B") and room_ratio < pol.room_min_atr:
        grade = "C"
        note = (note + "; " if note else "") + f"room {room_ratio:.2f} ATR < {pol.room_min_atr}"

    return Confluence(
        stop=stop,
        targets=targets,
        grade=grade,
        rr=round(rr, 3),
        fortress=round(fortress, 2),
        room_ratio=round(room_ratio, 3),
        zones_considered=len(zones),
        stop_zone=stop_label,
        target_zones=target_labels,
        note=note,
    )
