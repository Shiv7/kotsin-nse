"""What a stock is doing today, in words: the read a trader would give of its gap, its path, who is
behind the move and where it stands against yesterday's levels.

Built for the board from what the engine already holds — today's 1m bars, the previous session,
the per-minute order flow (``orderflow/meter.py``), the futures OI change, the sector, the pivot
zones — by fixed rules, each a named constant. It DESCRIBES what has happened so far; nothing in
it is a tested signal. The rules came from the 2026-10-23 open (SWIGGY gap and go, INFY gap and
fade, COFORGE rejected at the prior high, TCS held on fresh longs) and are hypotheses until the
recorded days say otherwise.

**Path** (a gap is the open beyond ``GAP_PCT`` of the previous close; moves in a normal day, ADR):

* ``GAP_AND_GO`` — further the gap's way than ``MOVE_ADR`` from the open;
* ``GAP_FILLED`` — back through the previous close;
* ``GAP_FADING`` — back from the open by ``MOVE_ADR`` but not through the close;
* ``GAP_HOLDING`` — within ``MOVE_ADR`` of the open either way;
* ``TREND_UP`` / ``TREND_DOWN`` / ``RANGE`` — no gap: ``MOVE_ADR`` from the open, or neither.

**Points**, each only when its rule holds: where it opened against yesterday's range; its side of
VWAP and since when; the futures OI behind the move (fresh positions, covering, unwinding, flat);
the buying or selling (the cash CVD now against its extreme of the day); a rejection at yesterday's
high or low; the book at the day's extreme (bids against asks while the flow still pointed the
other way); whether the sector moved with it; the next pivot zone in the way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

GAP_PCT = 0.5
MOVE_ADR = 0.25
OI_FRESH_PCT = 1.0  # the board's own neutral band (scan/board.py OI_NEUTRAL_PCT)
OI_FLAT_PCT = 0.5
REJECT_TOL_ADR = 0.10  # the day's high within this of yesterday's high counts as a test of it
REJECT_BACK_ADR = 0.30  # and price this far back from that high is a rejection
BOOK_FLIP_RATIO = 0.5  # bids under half the asks (or the mirror) at the extreme
CVD_FADE = 0.5  # CVD now under half its extreme of the day: the buying (selling) has faded
SECTOR_WITH_PCT = 0.5  # the sector's average move the stock's way, for "with its sector"


@dataclass(frozen=True, slots=True)
class StoryInput:
    symbol: str
    prev_close: float
    prev_high: float | None
    prev_low: float | None
    adr: float | None
    bars: Sequence[tuple[int, float, float, float, float, float]]  # today's closed 1m: ts, o, h, l, c, v
    cvd: Sequence[tuple[int, float]] = ()  # cash, per minute: ts, cumulative delta
    book: Sequence[tuple[int, float | None, float | None, float]] = ()  # ts, bid depth, ask depth, OFI
    oi_chg_pct: float | None = None  # front future vs the previous close
    sector: str = ""
    sector_pct: float | None = None  # the sector's average % move
    zones: Sequence[dict[str, Any]] = ()  # pivot clusters (structure/levels.py to_json)


@dataclass(slots=True)
class Story:
    symbol: str
    path: str
    tone: str  # bull | bear | neutral
    headline: str
    points: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _hm(ts: int) -> str:
    m = (ts + 19800) // 60 % 1440  # IST is UTC+5:30, no DST
    return f"{m // 60:02d}:{m % 60:02d}"


def _n(x: float) -> str:
    return f"{x:,.2f}" if x < 1000 else f"{x:,.1f}"


def _q(x: float) -> str:
    a = abs(x)
    s = f"{a / 1e6:.2f}M" if a >= 1e6 else f"{a / 1e3:.0f}k" if a >= 1e3 else f"{a:.0f}"
    return ("+" if x >= 0 else "−") + s


def _vwap_side(bars: Sequence[tuple[int, float, float, float, float, float]]) -> tuple[int, int | None]:
    """+1 above / −1 below the running VWAP at the last bar, and the bar it has been there since."""
    pv = vol = 0.0
    side, since = 0, None
    for ts, _o, h, lo, c, v in bars:
        if v > 0:
            pv += (h + lo + c) / 3 * v
            vol += v
        if vol <= 0:
            continue
        s = 1 if c >= pv / vol else -1
        if s != side:
            side, since = s, ts
    return side, since


def read(x: StoryInput) -> Story | None:
    if not x.bars or x.prev_close <= 0:
        return None
    o = x.bars[0][1]
    hi = max(b[2] for b in x.bars)
    lo = min(b[3] for b in x.bars)
    last = x.bars[-1][4]
    hi_ts = next(b[0] for b in x.bars if b[2] == hi)
    lo_ts = next(b[0] for b in x.bars if b[3] == lo)
    adr = x.adr or 0.0
    gap = (o / x.prev_close - 1) * 100
    now = (last / x.prev_close - 1) * 100
    move = last - o
    up_gap, down_gap = gap >= GAP_PCT, gap <= -GAP_PCT
    big = MOVE_ADR * adr if adr else abs(o) * 0.003

    if up_gap or down_gap:
        sign = 1 if up_gap else -1
        if move * sign >= big:
            path = "GAP_AND_GO"
        elif (last - x.prev_close) * sign <= 0:
            path = "GAP_FILLED"
        elif move * sign <= -big:
            path = "GAP_FADING"
        else:
            path = "GAP_HOLDING"
    else:
        sign = 1 if move >= 0 else -1
        path = "TREND_UP" if move >= big else "TREND_DOWN" if move <= -big else "RANGE"
    tone = "bull" if move > 0 and path not in ("GAP_FADING", "GAP_FILLED") else "bear" if move < 0 and path not in ("GAP_FADING", "GAP_FILLED") else "neutral"
    if path in ("GAP_FADING", "GAP_FILLED"):
        tone = "bear" if up_gap else "bull"
    words = {
        "GAP_AND_GO": f"gap {'up' if up_gap else 'down'} and go",
        "GAP_FILLED": f"gap {'up' if up_gap else 'down'} filled",
        "GAP_FADING": f"gap {'up' if up_gap else 'down'} fading",
        "GAP_HOLDING": f"gap {'up' if up_gap else 'down'} holding",
        "TREND_UP": "trending up from the open",
        "TREND_DOWN": "trending down from the open",
        "RANGE": "range around the open",
    }[path]
    headline = f"{words} · opened {gap:+.2f}%, now {now:+.2f}%"
    pts: list[str] = []

    # where it opened against yesterday's range
    if x.prev_high and o > x.prev_high:
        pts.append(f"Opened above yesterday's high {_n(x.prev_high)}" + (" — still above it" if last > x.prev_high else " — back below it now"))
    elif x.prev_low and o < x.prev_low:
        pts.append(f"Opened below yesterday's low {_n(x.prev_low)}" + (" — never got back above it" if hi < x.prev_low else " — traded back above it" if last >= x.prev_low else " — back below it now"))

    # VWAP
    side, since = _vwap_side(x.bars)
    if side and since is not None:
        first = since == x.bars[0][0]
        pts.append(f"{'Above' if side > 0 else 'Below'} VWAP {'all session' if first else 'since ' + _hm(since)}")

    # who is behind it: futures OI against the price
    if x.oi_chg_pct is not None:
        oi, up = x.oi_chg_pct, last >= x.prev_close
        if abs(oi) < OI_FLAT_PCT:
            pts.append(f"Futures OI flat ({oi:+.1f}%) — no new positions behind the move")
        elif oi >= OI_FRESH_PCT:
            pts.append(f"Futures OI {oi:+.1f}% — fresh {'longs behind the rise' if up else 'shorts behind the fall'}")
        elif oi <= -OI_FRESH_PCT:
            pts.append(f"Futures OI {oi:+.1f}% — {'short covering, not new buying' if up else 'longs unwinding, not new selling'}")
        else:
            pts.append(f"Futures OI {oi:+.1f}% — barely moved")

    # buying / selling: the cash CVD against its extreme of the day
    if x.cvd:
        vals = [v for _t, v in x.cvd]
        cur = vals[-1]
        peak_i = max(range(len(vals)), key=lambda i: vals[i])
        trough_i = min(range(len(vals)), key=lambda i: vals[i])
        if vals[peak_i] > 0 and cur < CVD_FADE * vals[peak_i] and peak_i < len(vals) - 1:
            pts.append(f"Buying faded: CVD peaked {_q(vals[peak_i])} at {_hm(x.cvd[peak_i][0])}, now {_q(cur)}")
        elif vals[trough_i] < 0 and cur > CVD_FADE * vals[trough_i] and trough_i < len(vals) - 1:
            pts.append(f"Selling eased: CVD bottomed {_q(vals[trough_i])} at {_hm(x.cvd[trough_i][0])}, now {_q(cur)}")
        elif cur != 0:
            pts.append(f"{'Buyers' if cur > 0 else 'Sellers'} in control: CVD {_q(cur)} (estimated from snapshots)")

    # a test of yesterday's high / low that failed
    if adr and x.prev_high and abs(hi - x.prev_high) <= REJECT_TOL_ADR * adr and hi - last >= REJECT_BACK_ADR * adr:
        pts.append(f"Rejected at yesterday's high {_n(x.prev_high)} (high {_n(hi)} at {_hm(hi_ts)}), now {_n(last)}")
    elif adr and x.prev_low and abs(lo - x.prev_low) <= REJECT_TOL_ADR * adr and last - lo >= REJECT_BACK_ADR * adr:
        pts.append(f"Held yesterday's low {_n(x.prev_low)} (low {_n(lo)} at {_hm(lo_ts)}), now {_n(last)}")

    # the book at the day's extreme
    if x.book:
        for ext_ts, at_top in ((hi_ts, True), (lo_ts, False)):
            near = [b for b in x.book if abs(b[0] - ext_ts) <= 180 and b[1] and b[2]]
            if not near:
                continue
            ratio = min(b[1] / b[2] for b in near) if at_top else max(b[1] / b[2] for b in near)
            flow = sum(b[3] for b in near)
            if at_top and ratio < BOOK_FLIP_RATIO and flow > 0 and hi - last >= big:
                pts.append(f"At the high the book flipped: bids {ratio:.2f}× the asks while buying was still positive")
            elif not at_top and ratio > 1 / BOOK_FLIP_RATIO and flow < 0 and last - lo >= big:
                pts.append(f"At the low the book flipped: bids {ratio:.1f}× the asks while selling was still on")

    # with the sector or on its own
    if x.sector_pct is not None and x.sector:
        if now * x.sector_pct > 0 and abs(x.sector_pct) >= SECTOR_WITH_PCT:
            pts.append(f"Moving with its sector: {x.sector} {x.sector_pct:+.2f}% on average")
        elif abs(now) >= 1.0:
            pts.append(f"Its own move: {x.sector} {x.sector_pct:+.2f}% on average")

    # the next pivot zone in the way
    ahead = [z for z in x.zones if z.get("kind") == ("RESISTANCE" if tone != "bear" else "SUPPORT")]
    if ahead:
        z = min(ahead, key=lambda z: abs(z.get("distance_pct") or 0))
        pts.append(
            f"Next {'resistance' if tone != 'bear' else 'support'} zone {_n(z['low'])}–{_n(z['high'])} "
            f"({'·'.join(z['members'])}), {z['distance_pct']:+.2f}%"
        )
    return Story(x.symbol, path, tone, headline, pts)


__all__ = ["Story", "StoryInput", "read"]
