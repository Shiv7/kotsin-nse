"""The owner's four-layer gate on a confirmed signal (4 Nov 2026 22:40: "don't trade until you have equity, future,
option, market profile — do wise trading"). A signal is taken only when all four layers agree with the trade, judged at
the decision bar from what the engine already holds; the rest are recorded with ``why`` so the evening read can score
them. Scored on the two recorded days with every signal's context (docs/research/scripts/2026-11-04-wise-gate):
15 of 467 signals passed, gross +36 bps a signal against +9 for the rest, the gated first two +78 / +388 bps against the
book's −96 / +161 — two days, a description, not a test.

  equity   the retest fired on the move's side of the day's open (``with_move``) and the 1H supertrend is with the trade.
  future   the stock's futures state is a build-up with the trade: LB for a long, SB for a short. ``loose`` also accepts
           SC for a long and LU for a short.
  option   the F&O board's bias is with the trade (Bull / Bear) and the option wall on the trade's side (the call wall
           for a long, the put wall for a short) is at least one ADR away: room.
  profile  the day's developing volume profile (the 1m bars closed so far, at least thirty minutes): the entry on the
           trade's side of the POC and the retest's extreme not through the far edge of the value area by more than a
           tolerance — the pullback held value.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MIN_PROFILE_MINUTES = 30
STRICT_FUT = {1: ("LB",), -1: ("SB",)}
LOOSE_FUT = {1: ("LB", "SC"), -1: ("SB", "LU")}
LAYERS = ("equity", "future", "option", "profile")


def layers(side: int, entry: float, tol: float, adr: float, *, with_move: Any, tags: Mapping[str, Any] | None, fno: Mapping[str, Any] | None,
           retest_ext: float | None, vp: Mapping[str, Any] | None, future_loose: bool = False) -> dict[str, Any]:
    """Each layer's verdict and the facts it was judged on. ``vp`` = {poc, val, vah} or None (no profile yet)."""
    tags = tags or {}
    fno = fno or {}
    fut = fno.get("futures_state")
    bias = str(fno.get("bias") or "")
    cw, pw = fno.get("x_call_wall"), fno.get("x_put_wall")
    room = (cw - entry) if side > 0 and cw else (entry - pw) if side < 0 and pw else None
    equity = bool(with_move) and bool(tags.get("st60_al"))
    future = fut in (LOOSE_FUT if future_loose else STRICT_FUT)[1 if side > 0 else -1]
    option = (("Bull" in bias) if side > 0 else ("Bear" in bias)) and room is not None and adr > 0 and room >= adr
    poc = val = vah = None
    poc_with = va_held = False
    if vp is not None:
        poc, val, vah = vp.get("poc"), vp.get("val"), vp.get("vah")
        if poc is not None and val is not None and vah is not None:
            poc_with = (entry - poc) * side > 0
            va_held = retest_ext is not None and ((retest_ext >= val - tol) if side > 0 else (retest_ext <= vah + tol))
    profile = bool(poc_with and va_held)
    out = {"equity": equity, "future": future, "option": option, "profile": profile, "passed": equity and future and option and profile,
           "fut_state": fut, "bias": bias, "room": None if room is None else round(room, 2), "adr": adr, "poc": poc, "val": val, "vah": vah,
           "retest_ext": retest_ext, "poc_with": poc_with, "va_held": va_held, "future_loose": future_loose}
    return out


def why(gate: Mapping[str, Any]) -> str:
    """The recorded reason: which layers said no, with the fact that said it."""
    parts = []
    if not gate["equity"]:
        parts.append("equity (not with the move or against the 1H)")
    if not gate["future"]:
        parts.append(f"future ({gate.get('fut_state') or 'no state'})")
    if not gate["option"]:
        parts.append(f"option ({gate.get('bias') or 'no board'}, room {gate.get('room')})")
    if not gate["profile"]:
        parts.append("profile (no profile yet)" if gate.get("poc") is None else f"profile (poc {gate.get('poc')}, value {gate.get('val')}–{gate.get('vah')})")
    return "stack: " + "; ".join(parts) if parts else "stack: all four"

