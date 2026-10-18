"""Order blocks the way they are defined in public (order-block.com's guide, ICT/SMC usage), made
mechanical. Every rule reads CLOSED 5-minute bars only, so a level never depends on the future.

* **Swing** — a high with ``SWING_SIDE`` lower highs on each side (a low, mirrored). Known only once
  the bars to its right have closed.
* **Break of structure (BOS)** — a 5-minute CLOSE beyond the last confirmed swing high/low, or beyond
  the opening range (the first ``OPENING_RANGE_BARS`` bars). A wick is never a break.
* **Order block** — on a bullish BOS, the last red candle before the move; bearish, the last green.
  The zone is that candle's low..high; ``Z`` (where the setup acts) is its body edge facing the
  move — the open of the red candle — and ``S1`` (where it is wrong) just beyond the far side.
* **Impulse** — the move only counts if it left a fair value gap, or the breaking close cleared the
  zone by at least one average candle (mean high − low of the last ``AVG_CANDLE_BARS`` bars).
* **Life cycle** — ready → first retest → retested → broken; broken = a 5-minute close through the
  far side. A retest is a bar that trades back into the zone after price had left it.
* **Fair value gap** — three candles where candle 1's high is below candle 3's low (bullish).
  Filled once price trades back through it.
* **Liquidity** — highs and lows no candle has closed beyond yet: untaken swing highs/lows (equal
  ones flagged), the day's high/low and open, the opening range, the previous day's high/low/close,
  unfilled gaps. Targets ``R1``/``R2`` are placed at the nearest of them ahead of ``Z``.
* **Sweep** — a bar that trades through a liquidity level and CLOSES back on the near side.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

SWING_SIDE = 2
OPENING_RANGE_BARS = 3  # 15 minutes of 5m bars
AVG_CANDLE_BARS = 20
EQUAL_TOL_ADR = 0.05  # two swing highs within 5 % of a normal day are "equal highs"
STOP_BUFFER_CANDLE = 0.1  # S1 sits this fraction of an average candle beyond the zone


@dataclass(frozen=True, slots=True)
class Bar5:
    ts: int  # bucket start, epoch s
    o: float
    h: float
    l: float  # noqa: E741 - OHLC
    c: float
    v: float


@dataclass(slots=True)
class Swing:
    i: int
    ts: int
    price: float
    kind: str  # HIGH | LOW
    taken_at: int | None = None  # ts of the first close beyond it
    equal: bool = False


@dataclass(slots=True)
class Gap:
    i: int
    ts: int
    low: float
    high: float
    side: str  # BULLISH | BEARISH
    filled_at: int | None = None


@dataclass(slots=True)
class Zone:
    side: str  # BULLISH (demand) | BEARISH (supply)
    ob_i: int
    ob_ts: int
    bos_i: int
    bos_ts: int
    bos_kind: str  # SWING | OPENING_RANGE
    low: float
    high: float
    z: float
    s1: float
    impulse: str  # GAP | RANGE
    state: str = "READY"  # READY | RETESTED_1 | RETESTED | BROKEN
    retests: int = 0
    retest_ts: list[int] = field(default_factory=list)
    broken_ts: int | None = None
    swept_before: bool = False  # a sweep of liquidity on the far side preceded the move

    @property
    def active(self) -> bool:
        return self.state != "BROKEN"


@dataclass(slots=True)
class Level:
    price: float
    kind: str  # SWING_HIGH, EQUAL_HIGHS, DAY_HIGH, OR_HIGH, PDH, PDL, PDC, DAY_OPEN, GAP, ...
    ts: int | None = None


@dataclass(slots=True)
class Sweep:
    ts: int
    level: float
    kind: str
    side: str  # BULLISH = swept lows and closed back above


@dataclass(slots=True)
class Setup:
    side: str
    zone_index: int
    z: float
    s1: float
    r1: float | None
    r2: float | None
    rr: float | None  # (R2 − Z) / (Z − S1), R1 when there is no R2
    extra: list[Level]
    state: str


@dataclass(slots=True)
class Structure:
    swings: list[Swing]
    gaps: list[Gap]
    zones: list[Zone]
    liquidity: list[Level]
    sweeps: list[Sweep]
    setup: Setup | None
    avg_candle: float | None
    opening_range: tuple[float, float] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "swings": [asdict(s) for s in self.swings],
            "gaps": [asdict(g) for g in self.gaps],
            "zones": [asdict(z) for z in self.zones],
            "liquidity": [asdict(lv) for lv in self.liquidity],
            "sweeps": [asdict(s) for s in self.sweeps],
            "setup": asdict(self.setup) if self.setup else None,
            "avg_candle": self.avg_candle,
            "opening_range": list(self.opening_range) if self.opening_range else None,
        }


def _swings(bars: Sequence[Bar5], side: int) -> list[Swing]:
    out: list[Swing] = []
    for i in range(side, len(bars) - side):
        hi = bars[i].h
        lo = bars[i].l
        if all(bars[j].h < hi for j in range(i - side, i + side + 1) if j != i):
            out.append(Swing(i, bars[i].ts, hi, "HIGH"))
        if all(bars[j].l > lo for j in range(i - side, i + side + 1) if j != i):
            out.append(Swing(i, bars[i].ts, lo, "LOW"))
    return out


def _gaps(bars: Sequence[Bar5]) -> list[Gap]:
    out: list[Gap] = []
    for i in range(2, len(bars)):
        a, c = bars[i - 2], bars[i]
        if a.h < c.l:
            out.append(Gap(i - 1, bars[i - 1].ts, a.h, c.l, "BULLISH"))
        elif a.l > c.h:
            out.append(Gap(i - 1, bars[i - 1].ts, c.h, a.l, "BEARISH"))
    for g in out:
        for b in bars[g.i + 2 :]:
            if (g.side == "BULLISH" and b.l <= g.low) or (g.side == "BEARISH" and b.h >= g.high):
                g.filled_at = b.ts
                break
    return out


def analyze(
    bars: Sequence[Bar5],
    *,
    prev_day: tuple[float, float, float] | None = None,  # (high, low, close)
    adr: float | None = None,
    swing_side: int = SWING_SIDE,
) -> Structure:
    """Everything above, over today's CLOSED 5-minute bars (oldest first)."""
    n = len(bars)
    if n == 0:
        return Structure([], [], [], [], [], None, None, None)
    ranges = [b.h - b.l for b in bars[-AVG_CANDLE_BARS:]]
    avg = sum(ranges) / len(ranges) if ranges else None
    or_bars = bars[:OPENING_RANGE_BARS]
    opening = (min(b.l for b in or_bars), max(b.h for b in or_bars)) if len(or_bars) == OPENING_RANGE_BARS else None

    swings = _swings(bars, swing_side)
    for s in swings:
        for b in bars[s.i + 1 :]:
            if (s.kind == "HIGH" and b.c > s.price) or (s.kind == "LOW" and b.c < s.price):
                s.taken_at = b.ts
                break
    tol = (adr or (avg or 0) * 4) * EQUAL_TOL_ADR
    for kind in ("HIGH", "LOW"):
        same = [s for s in swings if s.kind == kind]
        for a in range(len(same)):
            for b2 in range(a + 1, len(same)):
                if abs(same[a].price - same[b2].price) <= tol:
                    same[a].equal = same[b2].equal = True
    gaps = _gaps(bars)

    # -- sweeps: a wick through a level that closes back --------------------------------------------
    sweeps: list[Sweep] = []
    for i in range(1, n):
        b = bars[i]
        lows = [s for s in swings if s.kind == "LOW" and s.i + swing_side < i and (s.taken_at is None or s.taken_at >= b.ts)]
        highs = [s for s in swings if s.kind == "HIGH" and s.i + swing_side < i and (s.taken_at is None or s.taken_at >= b.ts)]
        for s in lows:
            if b.l < s.price <= b.c:
                sweeps.append(Sweep(b.ts, s.price, "EQUAL_LOWS" if s.equal else "SWING_LOW", "BULLISH"))
        for s in highs:
            if b.h > s.price >= b.c:
                sweeps.append(Sweep(b.ts, s.price, "EQUAL_HIGHS" if s.equal else "SWING_HIGH", "BEARISH"))
        if prev_day:
            if b.l < prev_day[1] <= b.c:
                sweeps.append(Sweep(b.ts, prev_day[1], "PDL", "BULLISH"))
            if b.h > prev_day[0] >= b.c:
                sweeps.append(Sweep(b.ts, prev_day[0], "PDH", "BEARISH"))

    # -- breaks of structure → order blocks ------------------------------------------------------------
    zones: list[Zone] = []
    used_ob: set[tuple[str, int]] = set()
    for k in range(1, n):
        b = bars[k]
        # the swing levels CONFIRMED by bar k (their right side closed before it) and not yet broken
        live_highs = [s for s in swings if s.kind == "HIGH" and s.i + swing_side < k and (s.taken_at is None or s.taken_at >= b.ts)]
        live_lows = [s for s in swings if s.kind == "LOW" and s.i + swing_side < k and (s.taken_at is None or s.taken_at >= b.ts)]
        prev_close = bars[k - 1].c
        events: list[tuple[str, str]] = []
        if live_highs and b.c > live_highs[-1].price >= prev_close:
            events.append(("BULLISH", "SWING"))
        elif opening and k >= OPENING_RANGE_BARS and b.c > opening[1] >= prev_close:
            events.append(("BULLISH", "OPENING_RANGE"))
        if live_lows and b.c < live_lows[-1].price <= prev_close:
            events.append(("BEARISH", "SWING"))
        elif opening and k >= OPENING_RANGE_BARS and b.c < opening[0] <= prev_close:
            events.append(("BEARISH", "OPENING_RANGE"))
        for side, kind in events:
            want_red = side == "BULLISH"
            j = next((x for x in range(k - 1, max(-1, k - 12), -1) if (bars[x].c < bars[x].o) == want_red and bars[x].c != bars[x].o), None)
            if j is None or (side, j) in used_ob:
                continue
            ob = bars[j]
            gap_in_move = any(g.side == side and j <= g.i <= k for g in gaps)
            clear = (b.c - ob.h) if side == "BULLISH" else (ob.l - b.c)
            if not gap_in_move and not (avg and clear >= avg):
                continue
            buf = (avg or 0) * STOP_BUFFER_CANDLE
            # the body edge price meets first on its way back: a red candle's open is its body top,
            # a green candle's open its body bottom
            z = ob.o
            s1 = ob.l - buf if side == "BULLISH" else ob.h + buf
            far = [sw for sw in sweeps if sw.side == side and bars[max(0, j - 6)].ts <= sw.ts <= ob.ts]
            used_ob.add((side, j))
            zones.append(
                Zone(side, j, ob.ts, k, b.ts, kind, ob.l, ob.h, z, s1, "GAP" if gap_in_move else "RANGE", swept_before=bool(far))
            )

    # -- life cycle -----------------------------------------------------------------------------------------
    for zn in zones:
        inside = False
        for b in bars[zn.bos_i + 1 :]:
            if (zn.side == "BULLISH" and b.c < zn.low) or (zn.side == "BEARISH" and b.c > zn.high):
                zn.state, zn.broken_ts = "BROKEN", b.ts
                break
            touches = b.l <= zn.high if zn.side == "BULLISH" else b.h >= zn.low
            if touches and not inside:
                zn.retests += 1
                zn.retest_ts.append(b.ts)
            inside = touches
        if zn.state != "BROKEN":
            zn.state = "READY" if zn.retests == 0 else "RETESTED_1" if zn.retests == 1 else "RETESTED"

    # -- liquidity ----------------------------------------------------------------------------------------------
    day_open = bars[0].o
    day_high = max(b.h for b in bars)
    day_low = min(b.l for b in bars)
    levels: list[Level] = [
        Level(s.price, ("EQUAL_" if s.equal else "SWING_") + s.kind + ("S" if s.equal else ""), s.ts)
        for s in swings
        if s.taken_at is None and s.i + swing_side < n
    ]
    levels += [Level(day_high, "DAY_HIGH"), Level(day_low, "DAY_LOW"), Level(day_open, "DAY_OPEN")]
    if opening:
        levels += [Level(opening[1], "OR_HIGH"), Level(opening[0], "OR_LOW")]
    if prev_day:
        levels += [Level(prev_day[0], "PDH"), Level(prev_day[1], "PDL"), Level(prev_day[2], "PDC")]
    levels += [Level((g.low + g.high) / 2, f"GAP_{g.side}", g.ts) for g in gaps if g.filled_at is None]

    setup = _setup(zones, levels, bars[-1].c)
    return Structure(swings, gaps, zones, levels, sweeps, setup, avg, opening)


def _setup(zones: list[Zone], levels: list[Level], last: float) -> Setup | None:
    """The newest intact zone; its targets are the nearest liquidity ahead of Z."""
    for idx in range(len(zones) - 1, -1, -1):
        zn = zones[idx]
        if not zn.active:
            continue
        if zn.side == "BULLISH":
            ahead = sorted({round(lv.price, 4): lv for lv in levels if lv.price > max(zn.z, zn.high)}.values(), key=lambda lv: lv.price)
        else:
            ahead = sorted({round(lv.price, 4): lv for lv in levels if lv.price < min(zn.z, zn.low)}.values(), key=lambda lv: -lv.price)
        r1 = ahead[0].price if ahead else None
        r2 = ahead[1].price if len(ahead) > 1 else None
        risk = abs(zn.z - zn.s1)
        target = r2 if r2 is not None else r1
        rr = round(abs(target - zn.z) / risk, 2) if target is not None and risk > 0 else None
        return Setup(zn.side, idx, zn.z, zn.s1, r1, r2, rr, ahead[2:5], zn.state)
    return None


def resample_5m(rows: Sequence[tuple[int, float, float, float, float, float]], session_open_ts: int) -> list[Bar5]:
    """(ts, o, h, l, c, v) 1m rows → 5m bars on the session grid."""
    out: dict[int, list[float]] = {}
    for ts, o, h, lo, c, v in rows:
        k = session_open_ts + ((ts - session_open_ts) // 300) * 300
        cur = out.get(k)
        if cur is None:
            out[k] = [o, h, lo, c, v]
        else:
            cur[1] = max(cur[1], h)
            cur[2] = min(cur[2], lo)
            cur[3] = c
            cur[4] += v
    return [Bar5(k, *vals) for k, vals in sorted(out.items())]
