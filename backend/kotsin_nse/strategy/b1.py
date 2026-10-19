"""CT-B1, the CTB-19Oct B1 gap fade — pure: a trigger's logged context and the day's zones in, the read out.

The operator's framework (19 Oct 2026): FUDKII counter-trend signals come in two families, B1 gap-fade and B2 wall-fade;
B1 is the 09:15-09:45 candle on NSE F&O names incl. the indices, where a real gap in the trigger's direction is traded
AGAINST the trigger. Agreed prices are the "1 AM definition": the official open (the pre-open print, the 1D open) against
the official close (the closing-auction print, the 1D close), in daily ATRs — ``gapOfficial`` on the trigger's context.

The rule (resume file CTB-19Oct_README.md, "B1 rule (proposal)"): the gap at least ``B1_GAP_DATR`` (0.25) its own way
AND no key pivot (PIVOT, TC, BC, R1, S1, R2, S2 of 1d / 1wk / 1mo — the context's ``pivotsAheadAtr``, as RT-Y's gate B
reads it) within ``B1_PIN_ATR`` (0.5) ATR30 ahead of the close: a gap pinned by a pivot is in trend (B1x, CANBK 19 Oct).
No volume, breadth or grade gate — group 1 ran 0.8x to 22x volume, grades A and F.

Recorded on every read, taken or not: ``at_025``, ``at_030`` (the 0.30 line the operator compares it with), the pivot
test and the conviction points the engine can compute (tiered, cap 5): the gap (0.25-0.5 0.5 | 0.5-1 1 | 1-1.5 1.5 |
>=1.5 2), the stretch of the 09:45 close from the official close in daily ATRs (0.8-1.5 0.5 | 1.5-2.5 1 | >=2.5 1.5), the
room to the first wall (strength >= 5.2) on the fade's side in ATR30 (0.5-1 0.5 | 1-2 1 | >=2 or none 1.5) and a wall in
the trigger's direction within 1 ATR30 (0.5). The fifth read — the 5-read against the trigger (3 0.5 | 4 1 | 5 1.5) —
is the research's (oct15/live9b_tm15.py: option OI, volume, IV, premium and the stock read minute by minute); the engine
does not compute it, and says so. Research, 7 sessions 9-19 Oct: conviction >= 3 7/7, below 3 4/10.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..bars.official_open import FIRST_BAR
from ..bars.pivots import WALL_MIN_STRENGTH, Zone
from ..risk.limits import B1_GAP_DATR, B1_GAP_LINE, B1_PIN_ATR
from .counter import KEY_LEVELS

#: conviction tiers: (from, points) — the highest tier reached counts
GAP_TIERS = ((0.25, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0))
STRETCH_TIERS = ((0.8, 0.5), (1.5, 1.0), (2.5, 1.5))
ROOM_TIERS = ((0.5, 0.5), (1.0, 1.0), (2.0, 1.5))
#: a wall in the trigger's direction this near (ATR30) is worth TRIG_WALL_POINTS
TRIG_WALL_ATR, TRIG_WALL_POINTS = 1.0, 0.5
CONVICTION_CAP = 5.0
#: the gate when the official open was not seen: the gap read is the first trade's, the one B1 replaced
OPEN_UNKNOWN = "open_unknown"
#: why the conviction is out of 5 with one read missing
NO_READ5 = "5-read (OI, volume, IV, premium, stock against the trigger: up to 1.5) not computed in the engine"


def _tier(x: float | None, tiers: Sequence[tuple[float, float]]) -> float:
    if x is None:
        return 0.0
    got = 0.0
    for start, pts in tiers:
        if x >= start:
            got = pts
    return got


def key_pins(ctx: dict[str, Any], reach: float = B1_PIN_ATR) -> list[tuple[str, float]]:
    """The key pivots within ``reach`` ATR30 ahead of the close, nearest first, as the context logged them."""
    return [(str(lab), float(d)) for lab, d in (ctx.get("pivotsAheadAtr") or [])
            if str(lab).split(".", 1)[-1] in KEY_LEVELS and float(d) <= reach]


def conviction(ctx: dict[str, Any], *, close: float, bullish: bool, zones: Sequence[Zone]) -> dict[str, Any]:
    """The conviction points the engine can compute, each with what it read. Walls are the day's zones of strength
    WALL_MIN_STRENGTH or more; the fade's side is against the trigger."""
    sign = 1 if bullish else -1
    gap = ctx.get("gapOfficial")
    atr30 = float(ctx.get("atr30") or 0.0)
    datr = float(ctx.get("dAtr") or 0.0)
    o_close = ctx.get("officialClose")
    stretch = round(sign * (close - float(o_close)) / datr, 4) if (o_close and datr > 0) else None
    walls = [z for z in zones if z.strength >= WALL_MIN_STRENGTH]
    fade_side = sorted((z for z in walls if -sign * (z.price - close) > 0), key=lambda z: abs(z.price - close))
    trig_side = sorted((z for z in walls if sign * (z.price - close) > 0), key=lambda z: abs(z.price - close))
    room = round(abs(fade_side[0].price - close) / atr30, 4) if (fade_side and atr30 > 0) else None
    trig = round(abs(trig_side[0].price - close) / atr30, 4) if (trig_side and atr30 > 0) else None
    pts = {
        "gap": _tier(gap, GAP_TIERS),
        "stretch": _tier(stretch, STRETCH_TIERS),
        # no wall on the fade's side is all the room there is; without an ATR30 the room cannot be read
        "room": (ROOM_TIERS[-1][1] if not fade_side else _tier(room, ROOM_TIERS)) if atr30 > 0 else 0.0,
        "trigWall": TRIG_WALL_POINTS if (trig is not None and trig <= TRIG_WALL_ATR) else 0.0,
    }
    return {
        "points": pts, "total": min(CONVICTION_CAP, sum(pts.values())), "of": CONVICTION_CAP, "read5": None,
        "note": NO_READ5, "stretchDatr": stretch, "roomAtr": room, "trigWallAtr": trig,
        "roomWall": None if not fade_side else {"price": fade_side[0].price, "strength": round(fade_side[0].strength, 2),
                                                "members": list(fade_side[0].members)},
        "trigWall": None if not trig_side else {"price": trig_side[0].price, "strength": round(trig_side[0].strength, 2),
                                                "members": list(trig_side[0].members)},
    }


def b1_read(ctx: dict[str, Any] | None, *, close: float, bullish: bool, zones: Sequence[Zone],
            pivots_known: bool = True) -> dict[str, Any]:
    """B1 on one 09:15-candle trigger: ``take`` and why, the gap and its lines, the pivot test and the conviction.
    ``gate`` names a refusal: ``not_open_bar``, ``no_gap_value``, ``open_unknown`` (``gapOpenSrc`` ``first_bar``: the
    process never saw the open — a boot or a reconnect after 09:15 — so the "official" gap is the first 30m bar's, the
    first trade's, the very number B1 replaced: UNIONBANK 19 Oct read 0.452 on it against 0.2495 official, AXISBANK
    15 Oct 0.283 against 0.093, PREMIERENE 19 Oct 0.145 against 0.252 — neither a fade nor a "no gap" can be told;
    review 19 Oct), ``no_gap`` (under 0.25 its own way), ``pivots_unknown`` (``pivots_known`` False: the engine holds no classic level for the name — its daily row not the
    broker's official one, or too few of them — so "no key pivot ahead" cannot be told from "none read"; B1 does not
    fade blind), ``pivot_pin``."""
    ctx = ctx or {}
    gap = ctx.get("gapOfficial")
    pins = key_pins(ctx)
    out: dict[str, Any] = {
        "gapOfficial": gap, "gapDatr": ctx.get("gapDatr"), "gapOpenSrc": ctx.get("gapOpenSrc"),
        "officialOpen": ctx.get("officialOpen"), "officialClose": ctx.get("officialClose"), "dAtr": ctx.get("dAtr"),
        "atr30": ctx.get("atr30"),
        "at_025": gap is not None and gap >= B1_GAP_DATR, "at_030": gap is not None and gap >= B1_GAP_LINE,
        "keyPivotAhead": bool(pins), "pins": [[lab, d] for lab, d in pins], "pivotsKnown": pivots_known,
        "conviction": conviction(ctx, close=close, bullish=bullish, zones=zones),
    }
    if not ctx.get("openBar"):
        return {**out, "take": False, "gate": "not_open_bar", "why": "B1 reads the 09:15 candle only"}
    if gap is None:
        return {**out, "take": False, "gate": "no_gap_value", "why": "no official gap — no daily ATR or previous close"}
    if out["gapOpenSrc"] == FIRST_BAR:
        return {**out, "take": False, "gate": OPEN_UNKNOWN,
                "why": f"official open not seen (the first 30m bar's open, gap {gap:.3f}); B1 does not fade the first-trade gap"}
    if gap < B1_GAP_DATR:
        return {**out, "take": False, "gate": "no_gap",
                "why": f"official gap {gap:.3f} daily ATR its own way < {B1_GAP_DATR:g} ({out['gapOpenSrc'] or 'open'})"}
    if not pivots_known:
        return {**out, "take": False, "gate": "pivots_unknown",
                "why": f"gap {gap:.3f}, but no classic level could be read for the name — the pin test is unknown"}
    if pins:
        lab, d = pins[0]
        return {**out, "take": False, "gate": "pivot_pin",
                "why": f"gap {gap:.3f} pinned: key pivot {lab} +{d:.2f} ATR30 ahead (within {B1_PIN_ATR:g}) — in trend, B1x"}
    return {**out, "take": True, "gate": None,
            "why": f"official gap {gap:.3f} daily ATR its own way (≥ {B1_GAP_DATR:g}), no key pivot within {B1_PIN_ATR:g} ATR30"}
