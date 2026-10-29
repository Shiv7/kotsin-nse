"""The daily order block, positional — the rule exactly as the research scored it (2026-10-29).

Zones come from ``structure.analyze`` on the last daily bars: a close through a confirmed daily swing (or the
first three sessions' range) with displacement — a fair value gap in the move or clearance of an average candle —
and the last opposite candle before it is the zone. A zone with no return yet is a **pending limit** at Z, the
zone's body edge. The limit counts as filled on the first session that trades ``FILL_THROUGH`` beyond Z (a resting
limit at the exact level fills less often than a touch); a zone whose first return only touched is not retried.
Stop: the zone's far side less a tenth of a candle (the engine's S1). Target: ``target_r`` × the risk. Out at the
close of the ``max_sessions``-th session. Exits read daily bars: a session opening through the stop exits at the
open (the gap), else the stop is checked before the target (the pessimistic reading of one bar).

Sides: long in every regime; short only while NIFTY's trailing 20-session return is at or below zero (the
marking chosen on 2021–23, held on 2024–25 and October 2026 — ``docs/research/2026-10-29-daily-ob-sides-regimes.md``).
Five years, members only, conservative fills and costs: +25 bps net a trade, every year positive; the long side
+46 to +65, the shorts flat except in falling markets. It runs in SHADOW to find out whether the limit fills at the
rate assumed and what a round trip really costs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from ..structure.engine import Bar5, analyze

NAME = "daily_ob"
#: daily bars the zones are read from (the research windows held 60 back plus the weeks ahead)
ZONE_BARS = 80
#: the limit at Z counts as filled once a session trades this fraction beyond it
FILL_THROUGH = 1e-3
#: NIFTY's trailing return that gates the short side, in sessions
GATE_SESSIONS = 20


@dataclass(slots=True)
class Zone:
    """A pending limit: a zone no session has returned to yet."""

    side: int  # +1 demand (long) / −1 supply (short)
    z: float
    stop: float
    low: float
    high: float
    ob_ts: int
    bos_ts: int
    impulse: str  # GAP | RANGE
    swept_before: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def pending_zones(bars: Sequence[Bar5], adr: float | None) -> list[Zone]:
    """The zones of ``bars`` (oldest first) that are intact and have never been returned to."""
    if len(bars) < 10:
        return []
    st = analyze(list(bars), prev_day=None, adr=adr)
    out = []
    for zn in st.zones:
        if zn.state != "READY" or zn.retests:
            continue
        out.append(Zone(1 if zn.side == "BULLISH" else -1, zn.z, zn.s1, zn.low, zn.high, zn.ob_ts, zn.bos_ts, zn.impulse, zn.swept_before))
    return out


def fills(side: int, z: float, bar: Bar5, through: float = FILL_THROUGH) -> bool:
    """Did this session trade through the limit at Z?"""
    return bar.l <= z * (1 - through) if side > 0 else bar.h >= z * (1 + through)


def returned(side: int, high: float, low: float, bar: Bar5) -> bool:
    """Did this session touch the zone at all (the first return, filled or not, ends the pending limit)?"""
    return bar.l <= high if side > 0 else bar.h >= low


def step(side: int, entry: float, stop: float, target: float, bar: Bar5, *, first: bool, held: int, max_sessions: int) -> tuple[float, str] | None:
    """One session of an open position. ``first`` = the fill session itself: no gap check (the fill came
    first), and the target counts only when the session *closes* beyond it. A daily bar cannot say whether
    a touch of the target came before or after the fill (TITAN, 29 Oct 2026: the day's low at 09:15, the
    fill at 14:05), but a close beyond the target must have come after it — the path from the fill at Z to
    that close crosses the target. The stop stays range-based: it lies beyond Z on the fill side, so a touch
    means price passed Z first (a session opening beyond the stop is charged as a stop too, pessimistic).
    Returns (exit price, how) when the trade ends, else None."""
    if not first and ((side > 0 and bar.o <= stop) or (side < 0 and bar.o >= stop)):
        return bar.o, "gap_stop"
    if (side > 0 and bar.l <= stop) or (side < 0 and bar.h >= stop):
        return stop, "stop"
    if first:
        hit = bar.c >= target if side > 0 else bar.c <= target
    else:
        hit = bar.h >= target if side > 0 else bar.l <= target
    if hit:
        return target, "target"
    if held >= max_sessions:
        return bar.c, "time"
    return None


def trailing_return(closes: Sequence[float], sessions: int = GATE_SESSIONS) -> float | None:
    """``closes`` oldest first, the last one the session before the entry; the return over ``sessions``."""
    if len(closes) <= sessions or closes[-1 - sessions] <= 0:
        return None
    return (closes[-1] / closes[-1 - sessions] - 1) * 100


def short_allowed(gate: str, nifty_ret: float | None) -> bool:
    if gate == "always":
        return True
    if gate == "off":
        return False
    return nifty_ret is not None and nifty_ret <= 0


__all__ = ["FILL_THROUGH", "GATE_SESSIONS", "NAME", "ZONE_BARS", "Zone", "fills", "pending_zones", "returned", "short_allowed", "step", "trailing_return"]
