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


# ---- the owner's five extra checks (4 Nov 2026 23:45: "the time of the retest, what PCR says, all 16 build-up
# combinations, monthly pivots, candle patterns") — recorded on every signal as ``extra``; enforced only when named in
# ``strategy_gate_extra``. On the two recorded days (docs/research/scripts/2026-11-04-wise-gate/extra-checks.txt) none
# improved the outcome and PCR, the full build-up and the candle patterns were worse when present; the retest after
# 12:30 was the one weak support (−0.2 to −6.5 bps gross by half-hour). Tags first, layers only after ten sessions.
EXTRAS = ("rt_before_1230", "rt_after_open", "pcr_with", "fno_full", "options_with", "near_monthly", "candle_any", "engulf", "doji_or_pin")


def candle_flags(side: int, retest: Any, confirm: Any, prev: Any) -> dict[str, bool]:
    """The retest bar a doji (body ≤ 15 % of its range) or a hammer / shooting star the trade's way (the far wick at
    least twice the body and half the range); the confirmation bar engulfing the bar before it in the trade's direction."""
    out = {"doji": False, "pin": False, "engulf": False}
    if retest is not None and retest.h > retest.l:
        body = abs(retest.c - retest.o)
        rng = retest.h - retest.l
        out["doji"] = body <= 0.15 * rng
        far = (min(retest.o, retest.c) - retest.l) if side > 0 else (retest.h - max(retest.o, retest.c))
        out["pin"] = far >= 2 * body and far >= 0.5 * rng
    if confirm is not None and prev is not None:
        if side > 0:
            out["engulf"] = confirm.c > confirm.o and confirm.o <= min(prev.o, prev.c) and confirm.c >= max(prev.o, prev.c) and (confirm.c - confirm.o) > abs(prev.c - prev.o)
        else:
            out["engulf"] = confirm.c < confirm.o and confirm.o >= max(prev.o, prev.c) and confirm.c <= min(prev.o, prev.c) and (confirm.o - confirm.c) > abs(prev.c - prev.o)
    return out


def extras(side: int, *, retest_hm: str, retest_bar: Any, confirm_bar: Any, prev_bar: Any, fno: Mapping[str, Any] | None,
           monthly: Mapping[str, float] | None, lo: float | None, hi: float | None, tol: float) -> dict[str, Any]:
    """The five checks' facts and verdicts at the decision bar."""
    fno = fno or {}
    fs, cs, ps = fno.get("futures_state"), fno.get("calls_state"), fno.get("puts_state")
    pcr, pcr_chg = fno.get("x_pcr_oi"), fno.get("x_pcr_oi_chg")
    calls_with = (cs in ("LB", "SC")) if side > 0 else (cs in ("SB", "LU"))
    puts_with = (ps in ("SB", "LU")) if side > 0 else (ps in ("LB", "SC"))
    near = [k for k, v in (monthly or {}).items() if lo is not None and hi is not None and lo - tol <= v <= hi + tol]
    cf = candle_flags(side, retest_bar, confirm_bar, prev_bar)
    return {"retest_hm": retest_hm, "rt_before_1230": retest_hm < "12:30", "rt_after_open": retest_hm >= "09:45",
            "pcr": pcr, "pcr_chg": pcr_chg, "pcr_with": pcr_chg is not None and ((pcr_chg > 0) if side > 0 else (pcr_chg < 0)),
            "combo": f"{fs}/{cs}/{ps}", "fno_full": (fs == "LB" and cs == "LB" and ps == "SB") if side > 0 else (fs == "SB" and cs == "SB" and ps == "LB"),
            "calls_with": calls_with, "puts_with": puts_with, "options_with": calls_with and puts_with,
            "near_monthly": bool(near), "monthly": near, **cf, "candle_any": cf["doji"] or cf["pin"] or cf["engulf"], "doji_or_pin": cf["doji"] or cf["pin"]}


def extra_passed(ex: Mapping[str, Any], required: tuple[str, ...]) -> tuple[bool, list[str]]:
    """Every named check must be true; the names that are not come back."""
    missing = [k for k in required if k and not ex.get(k)]
    return not missing, missing


FIB = (0.382, 0.5, 0.618)


def market_profile_tags(side: int, entry: float, mp: Mapping[str, Any] | None, tol: float) -> dict[str, Any]:
    """The day's market profile (TPO) at the decision bar: the entry's place against its value area and POC, the day
    type so far, and whether the retest side of the profile is poor (an unfinished auction) — the owner's market
    profile (5 Nov 2026), recorded, not enforced."""
    if not mp:
        return {"tpo": None, "tpo_with": False, "tpo_in_value": None, "tpo_day_type": None}
    poc, val, vah = mp.get("poc"), mp.get("val"), mp.get("vah")
    with_ = (entry > poc) if side > 0 else (entry < poc)
    in_value = (val is not None and vah is not None) and (val - tol <= entry <= vah + tol)
    poor_behind = bool(mp.get("poor_low")) if side > 0 else bool(mp.get("poor_high"))
    return {"tpo": {"poc": poc, "val": val, "vah": vah, "ib_high": mp.get("ib_high"), "ib_low": mp.get("ib_low"), "single_prints": mp.get("single_prints")},
            "tpo_with": bool(with_), "tpo_in_value": bool(in_value), "tpo_day_type": mp.get("day_type"), "tpo_poor_behind": poor_behind}


def fib_tags(side: int, retest_px: float, swing: Mapping[str, float] | None, tol: float) -> dict[str, Any]:
    """Where the retest sits in the bigger trend's swing (the last 20 sessions' high to low): the retracement ratio
    measured from the swing's extreme the trade leans on (0 = at the swing high for a long, 1 = at its low), the
    nearest Fibonacci level and whether the retest is within a tolerance of one — the owner's Fibonacci (5 Nov 2026)."""
    if not swing or swing["high"] <= swing["low"]:
        return {"fib_ratio": None, "fib_level": None, "fib_near": False, "trend20": None}
    rng = swing["high"] - swing["low"]
    ratio = (swing["high"] - retest_px) / rng if side > 0 else (retest_px - swing["low"]) / rng
    nearest = min(FIB, key=lambda f: abs(ratio - f))
    near = abs(ratio - nearest) * rng <= tol
    mid = (swing["high"] + swing["low"]) / 2
    trend = "up" if swing["close"] > mid else "down"
    return {"fib_ratio": round(ratio, 3), "fib_level": nearest if near else None, "fib_near": bool(near), "trend20": trend, "trend20_with": (trend == "up") if side > 0 else (trend == "down")}

