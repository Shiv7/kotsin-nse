"""The held retest of ANY wall, with the day's move — the owner's rule as he states it (3 Nov 2026:
"CPR is just one reference; the same concept at R1, pivot confluence, round numbers; we are price
action traders"), research row 23 (`docs/research/2026-11-03-level-retest.md`: +4.2 bps gross, −10.8
net on 98,259 first flips over the development window; shipped to SHADOW on the owner's call to be
read against that number).

Per symbol-day on closed 5m bars the walls are the CPR band, P, R1–R3, S1–S3, the previous day's
high and low, the previous week's pivots, the opening range's high and low (the first fifteen
minutes, a wall from the fourth bar on) and the round numbers inside the session's range so far.
A *flip* on a wall, per side: a close beyond it by the break margin from a state not beyond it (a
gap open beyond the wall is not a break: the first bar only sets the state) → the first later bar
within ``RT_BARS`` whose extreme comes back to the wall ± tol (a close back through the wall first
ends the attempt) → held if that bar closes at or beyond the wall − brk (the CPR: inside the band
or beyond, the live CPR rule's test) → confirmed on the first close beyond the retest bar's
extreme within ``CONFIRM_BARS``. ``with_move``: the break bar closed on the move's side of the day's
open. The runner takes the first confirmed flip per symbol-side-day that is with the move.

This module is the research script (`scripts/2026-11-03-level-retest/level_retest.py`) verbatim in
its logic, with bar *positions* from the session open where the live runner gives them (a bucket
the feed never delivered shifts no window), exactly as ``cpr_retest.sequence`` does.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from ..analysis.dayread import BREAK_FRAC
from ..structure.engine import Bar5
from . import cpr_retest as cr

NAME = "level_retest"
BAR_S = 300
#: the opening range is the first three 5m bars; its high and low are walls armed on the fourth bar's close (a break on
#: that bar itself is the state, not a flip — the research's definition, kept so the live book equals row 23)
OR_BARS = 3
#: the pullback must come within three hours of the break
RT_BARS = 36
CONFIRM_BARS = cr.CONFIRM_BARS
KINDS = ("cpr", "pivot", "pd", "weekly", "or", "round")


@dataclass(frozen=True, slots=True)
class Wall:
    name: str
    lo: float
    hi: float
    kind: str
    from_bar: int  # the bar position from which the wall exists
    conf: int  # other walls within a tolerance


@dataclass(slots=True)
class Flip:
    """One break of one wall on one side, and what followed."""

    side: int
    wall: str
    kind: str
    conf: int
    lo: float
    hi: float
    i_brk: int
    with_move: bool
    k: int | None = None  # the retest bar
    held: bool | None = None
    j: int | None = None  # the confirmation bar

    @property
    def i_rt(self) -> int | None:
        return self.k

    @property
    def name(self) -> str:
        if self.j is not None:
            return "confirmed"
        if self.k is None:
            return "break"
        return "retest_held" if self.held else "retest_failed"

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "stage": self.name}


def walls(levels: dict[str, float], weekly: dict[str, float] | None, orh: float | None, orl: float | None,
          lo_px: float, hi_px: float, tol: float) -> list[Wall]:
    """The walls in KINDS order (the order the runner takes the first confirmed flip in). Round numbers
    at ``cpr_retest.round_step`` across the session's range so far, two tolerances either side."""
    raw: list[tuple[str, float, float, str, int]] = [("CPR", min(levels["BC"], levels["TC"]), max(levels["BC"], levels["TC"]), "cpr", 0)]
    raw += [(k, levels[k], levels[k], "pivot", 0) for k in ("P", "R1", "R2", "R3", "S1", "S2", "S3") if k in levels]
    raw += [(k, levels[k], levels[k], "pd", 0) for k in ("PDH", "PDL") if k in levels]
    if weekly:
        raw += [(k, weekly[k], weekly[k], "weekly", 0) for k in ("WP", "WR1", "WR2", "WS1", "WS2") if k in weekly]
    if orh is not None and orl is not None:
        raw += [("ORH", orh, orh, "or", OR_BARS), ("ORL", orl, orl, "or", OR_BARS)]
    step = cr.round_step((lo_px + hi_px) / 2)
    r = math.floor((lo_px - 2 * tol) / step) * step
    while r <= hi_px + 2 * tol:
        if r > 0:
            raw.append((f"R{r:g}", r, r, "round", 0))
        r += step
    out = []
    for name, lo, hi, kind, fb in raw:
        conf = sum(1 for n2, lo2, hi2, _k, _f in raw if n2 != name and not (lo2 > hi + tol or hi2 < lo - tol))
        out.append(Wall(name, lo, hi, kind, fb, conf))
    return out


def _beyond(x: float, side: int, edge: float, brk: float) -> bool:
    """Closed beyond the wall's edge by the break margin, on ``side``."""
    return (x > edge + brk) if side > 0 else (x < edge - brk)


def _behind(x: float, side: int, far: float, brk: float) -> bool:
    """Closed back behind the wall's far edge by the break margin: the break is undone."""
    return (x < far - brk) if side > 0 else (x > far + brk)


def flips(bars: Sequence[Bar5], levels: dict[str, float], weekly: dict[str, float] | None, tol: float, *, open_ts: int | None = None) -> list[Flip]:
    """Every flip on the bars so far, in wall order then time. A flip is confirmed only on its own bar."""
    n = len(bars)
    if n == 0:
        return []
    pos = [int((b.ts - open_ts) // BAR_S) if open_ts is not None else i for i, b in enumerate(bars)]
    c = [b.c for b in bars]
    h = [b.h for b in bars]
    lo = [b.l for b in bars]
    day_open = bars[0].o
    brk = tol * BREAK_FRAC
    first3 = [i for i in range(n) if pos[i] < OR_BARS]
    orh = max(h[i] for i in first3) if len(first3) >= OR_BARS else None
    orl = min(lo[i] for i in first3) if len(first3) >= OR_BARS else None
    out: list[Flip] = []
    for w in walls(levels, weekly, orh, orl, min(lo), max(h), tol):
        start = next((i for i in range(n) if pos[i] >= w.from_bar), None)
        if start is None:
            continue
        for side in (1, -1):
            edge = w.hi if side > 0 else w.lo  # the side of the band price must clear
            far = w.lo if side > 0 else w.hi  # the hold must close at or beyond this edge − brk

            state = 1 if _beyond(c[start], side, edge, brk) else -1  # no break on the bar the wall first exists
            i = start + 1
            while i < n:
                if state != 1 and _beyond(c[i], side, edge, brk):
                    state = 1
                    fl = Flip(side, w.name, w.kind, w.conf, w.lo, w.hi, i, (c[i] > day_open) if side > 0 else (c[i] < day_open))
                    k = None
                    for q in range(i + 1, n):
                        if pos[q] > pos[i] + RT_BARS:
                            break
                        if (lo[q] <= edge + tol) if side > 0 else (h[q] >= edge - tol):
                            k = q
                            break
                        if _behind(c[q], side, far, brk):  # fell back through the wall before any retest: the break failed
                            break
                    if k is not None:
                        fl.k = k
                        fl.held = (c[k] >= far - brk) if side > 0 else (c[k] <= far + brk)
                        if fl.held:
                            trig = h[k] if side > 0 else lo[k]
                            fl.j = next((q for q in range(k + 1, n) if pos[q] <= pos[k] + CONFIRM_BARS and ((c[q] > trig) if side > 0 else (c[q] < trig))), None)
                        out.append(fl)
                        i = k + 1
                        state = 1  # after a retest (held or not) the wall re-arms only once price is back behind it
                        continue
                    out.append(fl)
                    i += 1
                    continue
                if state == 1 and _behind(c[i], side, far, brk):
                    state = -1
                i += 1
    return out


def confirmed_now(fls: Sequence[Flip], n: int) -> list[Flip]:
    """The flips whose confirmation bar is the latest bar, in wall order (the runner takes the first per side)."""
    return [f for f in fls if f.j == n - 1]


__all__ = ["CONFIRM_BARS", "KINDS", "NAME", "OR_BARS", "RT_BARS", "Flip", "Wall", "confirmed_now", "flips", "walls"]
