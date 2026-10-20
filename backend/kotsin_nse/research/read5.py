"""The 5-read and the strike walls, recorded at every FUDKII trigger — RECORD-ONLY.

Operator, 2026-10-19: "yes - the engine should start recording the 5-read and strike-wall state live, for research
only, so CT-B1 and later B2 can use them". Nothing here is read by a gate, a book, a sizer or an exit: an import-linter
contract keeps ``strategy``, ``risk``, ``exec``, ``venue`` and ``feed`` from importing this module, and CT-B1's
conviction keeps ``read5: None``. Feeding it to a book is a separate operator decision.

Ported from the research that defined it, logic unchanged (research-data/mdoft):

* ``oct15/live9b_tm15.py`` — the five reads at the trigger (candle start → signal) and after it (+1 … +16 min,
  since the signal), the time-matched Black-76 IV (KTM=1, KPXT=trade, KTTE=price), the votes, ``decide`` and
  ``first_n``;
* ``oct14/code/inputs14.py`` — the legs: CE1/CE2 the first and second listed call strictly above the trigger's close,
  PE1/PE2 the first and second listed put strictly below it, on the nearest expiry at least 2 calendar days out;
* ``weak_v4/calls/build_calls.py`` — the Strong / Moderate / Weak label and its "thin" votes;
* ``buckets_oct19/score5/build_score5.py`` — the with / against / no-vote split against the engine's direction;
* ``opt_table/wall_state.py`` (+ ``oct9_ext.py``'s wall pick) — the strike walls, their state, interpretation and
  the overall read.

Pure: numbers in, numbers out. The live values (as-of quotes, OI, per-minute volume) come from ``ops/optrec.py``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time
from typing import Any, TypeGuard

from ..market.session import IST

NAN = float("nan")
READS: tuple[str, ...] = ("OI", "VOL", "IV", "PREMIUM", "STOCK")
LEGS: tuple[str, ...] = ("CE1", "CE2", "PE1", "PE2")
#: a two-sided quote this old or newer is priced at its mid; else the last trade (research ``opt_px``, fresh_s=60)
FRESH_S = 60.0
#: STOCK votes at a move of this many ATR30 either way
STOCK_VOTE_ATR = 0.25
#: VOL builds above this multiple of the normal volume
VOL_VOTE = 1.0
#: the VolSurge denominator's floor (live9.py ``baseline_30m``)
VOL_FLOOR = 1000.0
#: build_calls.py's "thin" agreeing votes (the C1 rule)
THIN = {"OI": 1.0, "IV": 0.5, "PREMIUM": 1.0}
VOL_MIN, VOL_NEAR, STOCK_MIN = 1.2, 0.10, 0.35
#: the follow-up's last minute (live9b: +1 … +16)
FOLLOW_MIN = 16
#: walls: |OI change| within this percent is FLAT; |oiChangePct| beyond this is significant (FUDKOI's NSE bar)
WALL_FLAT_PCT = 1.0
OICP_SIGNIFICANT = 150.0
#: the 30m session grid (oct9_ext / wall_state ``GRID``); the 15:15 bar ends at 15:30
GRID: tuple[str, ...] = ("09:15", "09:45", "10:15", "10:45", "11:15", "11:45", "12:15", "12:45", "13:15", "13:45",
                         "14:15", "14:45", "15:15")
STATE_INT: dict[str, dict[str, str]] = {
    "CE": {"SHORT BUILD-UP": "Bearish", "SHORT COVERING": "Bullish", "LONG BUILD-UP": "Bullish",
           "LONG UNWINDING": "Bearish", "FLAT": "Neutral"},
    "PE": {"SHORT BUILD-UP": "Bullish", "SHORT COVERING": "Bearish", "LONG BUILD-UP": "Bearish",
           "LONG UNWINDING": "Bullish", "FLAT": "Neutral"},
}
WHY: dict[str, str] = {"SHORT BUILD-UP": "writers adding", "SHORT COVERING": "writers covering",
                       "LONG BUILD-UP": "buyers adding", "LONG UNWINDING": "buyers exiting", "FLAT": "OI flat"}


def isnum(v: Any) -> TypeGuard[float]:
    """A real number: not None, not NaN (research ``isnum``)."""
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def at_ist(day: date | str, hm: str) -> float:
    """Epoch seconds of ``day`` at ``hm`` ("HH:MM" or "HH:MM:SS") IST — research ``ep``."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    parts = [int(p) for p in hm.split(":")] + [0]
    return datetime.combine(d, time(parts[0], parts[1], parts[2]), tzinfo=IST).timestamp()


# -- Black-76 (r = 0), research common_m.iv76 ----------------------------------------------------------------------


def _ncdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def b76(F: float, K: float, t: float, s: float, cp: str) -> float:
    st = s * math.sqrt(t)
    d1 = (math.log(F / K) + 0.5 * s * s * t) / st
    d2 = d1 - st
    if cp == "CE":
        return F * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - F * _ncdf(-d1)


def iv76(P: float, F: float, K: float, t: float, cp: str) -> tuple[float, str]:
    """Implied vol by bisection on [1e-4, 5], 70 steps; (NaN, why) when it cannot be solved."""
    if not (isnum(P) and isnum(F) and isnum(K) and isnum(t)) or P <= 0 or F <= 0 or K <= 0 or t <= 0:
        return NAN, "bad_input"
    intr = max(F - K, 0.0) if cp == "CE" else max(K - F, 0.0)
    if P <= intr + 1e-9:
        return NAN, "below_intrinsic"
    lo, hi = 1e-4, 5.0
    if b76(F, K, t, hi, cp) < P:
        return NAN, "above_500pct"
    for _ in range(70):
        mid = 0.5 * (lo + hi)
        if b76(F, K, t, mid, cp) > P:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi), "ok"


def tte_years(expiry: str, x: float) -> float:
    """Years from ``x`` to the expiry day's 15:30 IST, on a 365-day year (research ``tte_years``)."""
    return (at_ist(expiry[:10], "15:30") - x) / (365.0 * 86400.0)


# -- legs (inputs14.build) -----------------------------------------------------------------------------------------


def legs_expiry(expiries: Iterable[str], today: date, min_days: int = 2) -> str | None:
    """The legs' expiry: the nearest listed one at least ``min_days`` calendar days after ``today`` — from TODAY's
    date, never a pick a long-running process made at boot (inputs14: ``(expiry - day).days >= 2``)."""
    ok = sorted({e[:10] for e in expiries if e and (date.fromisoformat(e[:10]) - today).days >= min_days})
    return ok[0] if ok else None


def pick_legs[T](ce: Sequence[tuple[float, T]], pe: Sequence[tuple[float, T]], spot: float) -> dict[str, tuple[float, T] | None]:
    """CE1 / CE2: the first and second listed call strike strictly above ``spot``; PE1 / PE2: the first and second
    put strictly below it. ``ce`` / ``pe`` are ``(strike, payload)``; a strike equal to spot is neither side's. A
    side with fewer than two strikes leaves the missing leg None (``leg_not_listed``)."""
    up = sorted((x for x in ce if x[0] > spot), key=lambda x: x[0])
    dn = sorted((x for x in pe if x[0] < spot), key=lambda x: -x[0])
    return {"CE1": up[0] if len(up) > 0 else None, "CE2": up[1] if len(up) > 1 else None,
            "PE1": dn[0] if len(dn) > 0 else None, "PE2": dn[1] if len(dn) > 1 else None}


# -- prices ---------------------------------------------------------------------------------------------------------


def opt_px(q: Mapping[str, Any] | None) -> tuple[float, str]:
    """research ``opt_px``: the mid of a two-sided quote at most FRESH_S old, else the last trade (when > 0).
    ``q`` is an as-of quote (``ops.optrec.Quote5``-shaped mapping: ``two``, ``fresh``, ``mid``, ``ltp``)."""
    if q is None:
        return NAN, ""
    if q["two"] and q["fresh"]:
        return float(q["mid"]), "mid"
    return (float(q["ltp"]), "ltp") if q["ltp"] > 0 else (NAN, "")


# -- the reads and their votes (live9b) -----------------------------------------------------------------------------


def side_vote(ce: float, pe: float, thr: float = 0.0) -> str:
    """ce / pe: change values (building = > thr). 'BULL' / 'BEAR' / 'SKIP' / ''."""
    if not isnum(ce) or not isnum(pe):
        return ""
    cb, pb = ce > thr, pe > thr
    if cb and not pb:
        return "BULL"
    if pb and not cb:
        return "BEAR"
    if cb and pb:
        return "BULL" if ce > pe else ("BEAR" if pe > ce else "")
    if ce < thr and pe < thr:
        return "SKIP"
    return ""


def stock_vote(mv: float) -> str:
    return "BULL" if isnum(mv) and mv >= STOCK_VOTE_ATR else "BEAR" if isnum(mv) and mv <= -STOCK_VOTE_ATR else ""


def _leg(snap: Mapping[str, Any], k: str) -> Mapping[str, Any]:
    v = snap.get(k)
    return v if isinstance(v, Mapping) else {}


def oi_change(X: Mapping[str, Any], B: Mapping[str, Any], a: str, b: str) -> float:
    """(ΣOI_X − ΣOI_B) / ΣOI_B × 100 over both legs; both legs are required."""
    xa, xb, ba, bb = (_leg(X, a).get("oi"), _leg(X, b).get("oi"), _leg(B, a).get("oi"), _leg(B, b).get("oi"))
    o0 = float(ba) + float(bb) if isnum(ba) and isnum(bb) else NAN
    o1 = float(xa) + float(xb) if isnum(xa) and isnum(xb) else NAN
    return (o1 - o0) / o0 * 100 if isnum(o0) and isnum(o1) and o0 > 0 else NAN


def vol_ratio(vol: Mapping[str, tuple[float, float]], a: str, b: str) -> float:
    """Σvolume / Σnormal over both legs; both legs (volume and a positive normal) are required."""
    v = [vol.get(k, (NAN, NAN)) for k in (a, b)]
    if not all(isnum(z[0]) and isnum(z[1]) and z[1] > 0 for z in v):
        return NAN
    return sum(z[0] for z in v) / sum(z[1] for z in v)


def iv_change(X: Mapping[str, Any], B: Mapping[str, Any], a: str, b: str) -> float:
    """Mean (iv_X − iv_B) × 100 over the pair's legs that have both: one leg is enough."""
    ch = [(_leg(X, k)["iv"] - _leg(B, k)["iv"]) * 100 for k in (a, b)
          if isnum(_leg(X, k).get("iv")) and isnum(_leg(B, k).get("iv"))]
    return sum(ch) / len(ch) if ch else NAN


def px_change(X: Mapping[str, Any], B: Mapping[str, Any], a: str, b: str) -> float:
    """Mean (px_X / px_B − 1) × 100 over the pair's legs that have both: one leg is enough."""
    ch = [(_leg(X, k)["px"] / _leg(B, k)["px"] - 1) * 100 for k in (a, b)
          if isnum(_leg(X, k).get("px")) and isnum(_leg(B, k).get("px")) and _leg(B, k)["px"] > 0]
    return sum(ch) / len(ch) if ch else NAN


def reads(X: Mapping[str, Any], B: Mapping[str, Any], vol: Mapping[str, tuple[float, float]], atr: float
          ) -> tuple[dict[str, tuple[float, float]], float, dict[str, str]]:
    """live9b ``reads``: X the snapshot at x, B the baseline, ``vol`` leg -> (volume, normal) over the window, ``atr``
    the trigger's ATR30. Returns (read -> (CE value, PE value), the stock move in ATR30, read -> vote)."""
    r = {"OI": (oi_change(X, B, "CE1", "CE2"), oi_change(X, B, "PE1", "PE2")),
         "VOL": (vol_ratio(vol, "CE1", "CE2"), vol_ratio(vol, "PE1", "PE2")),
         "IV": (iv_change(X, B, "CE1", "CE2"), iv_change(X, B, "PE1", "PE2")),
         "PREMIUM": (px_change(X, B, "CE1", "CE2"), px_change(X, B, "PE1", "PE2"))}
    xs, bs = X.get("stock"), B.get("stock")
    mv = (float(xs) - float(bs)) / atr if isnum(xs) and isnum(bs) and isnum(atr) and atr > 0 else NAN
    votes = {"OI": side_vote(*r["OI"]), "VOL": side_vote(r["VOL"][0], r["VOL"][1], thr=VOL_VOTE), "IV": side_vote(*r["IV"]),
             "PREMIUM": side_vote(*r["PREMIUM"]), "STOCK": stock_vote(mv)}
    return r, mv, votes


def decide(votes: Mapping[str, str]) -> tuple[int, int, int, str | None]:
    """(bullish votes, bearish votes, SKIP votes, lead): a lead needs >= 2 votes and more than the other side; a 2-2
    tie is decided by STOCK."""
    nb = sum(v == "BULL" for v in votes.values())
    ns = sum(v == "BEAR" for v in votes.values())
    nk = sum(v == "SKIP" for v in votes.values())
    lead = None
    if nb >= 2 and nb > ns:
        lead = "Bullish"
    elif ns >= 2 and ns > nb:
        lead = "Bearish"
    elif nb >= 2 and nb == ns and votes.get("STOCK") in ("BULL", "BEAR"):
        lead = "Bullish" if votes["STOCK"] == "BULL" else "Bearish"
    return nb, ns, nk, lead


def first_n(timeline: Sequence[Mapping[str, str]], n: int) -> tuple[int, str, list[str]] | None:
    """The first stage (the trigger included) with >= n reads one way: (stage index, 'Bullish'/'Bearish', the reads).
    Bullish is checked first at each stage, as research does."""
    for i, votes in enumerate(timeline):
        for call, dv in (("Bullish", "BULL"), ("Bearish", "BEAR")):
            hit = [k for k in READS if votes.get(k) == dv]
            if len(hit) >= n:
                return i, call, hit
    return None


def split(votes: Mapping[str, str], direction: str) -> tuple[list[str], list[str], list[str]]:
    """The reads with, against and not voting relative to the engine's direction ('Bullish' / 'Bearish')."""
    dv, ov = ("BULL", "BEAR") if direction == "Bullish" else ("BEAR", "BULL")
    w = [k for k in READS if votes.get(k) == dv]
    a = [k for k in READS if votes.get(k) == ov]
    return w, a, [k for k in READS if k not in w and k not in a]


def thin(row: Mapping[str, Any], read: str, dv: str) -> bool:
    """build_calls ``thin``: an agreeing vote too small to count as solid. ``row`` carries ``<READ>_CE`` /
    ``<READ>_PE`` and ``stock_atr``."""
    if read == "STOCK":
        sa = row.get("stock_atr")
        return not isnum(sa) or abs(float(sa)) < STOCK_MIN
    ce, pe = row[f"{read}_CE"], row[f"{read}_PE"]
    win, oth = (ce, pe) if dv == "BULL" else (pe, ce)
    if read == "VOL":
        return bool(win < VOL_MIN or (isnum(oth) and abs(win - oth) <= VOL_NEAR * oth))
    return bool(win < THIN[read])


def label(row: Mapping[str, Any], call: str) -> dict[str, Any]:
    """build_calls ``label`` at the 3-of-5 moment: Strong (>= 4 for, <= 1 against, >= 3 solid), Moderate (3 for,
    <= 1 against, all 3 solid), else Weak — with why. ``row`` carries ``vote_<READ>`` too."""
    dv, ov = ("BULL", "BEAR") if call == "Bullish" else ("BEAR", "BULL")
    f = [k for k in READS if row[f"vote_{k}"] == dv]
    a = [k for k in READS if row[f"vote_{k}"] == ov]
    n = [k for k in READS if k not in f and k not in a]
    th = [k for k in f if thin(row, k, dv)]
    solid = len(f) - len(th)
    if len(f) >= 4 and len(a) <= 1 and solid >= 3:
        lab = "Strong"
    elif len(f) == 3 and len(a) <= 1 and solid == 3:
        lab = "Moderate"
    else:
        lab = "Weak"
    why = ("" if lab != "Weak" else
           "3-2 split" if len(a) >= 2 else
           f"only {solid} of {len(f)} agreeing votes not thin (thin: {', '.join(th)})")
    return dict(label=lab, weak_because=why, reads_for=", ".join(f), reads_against=", ".join(a),
                reads_no_vote=", ".join(n), thin_for=", ".join(th), n_for=len(f), n_against=len(a), n_solid_for=solid,
                vote_split=f"{len(f)}-{len(a)}")


def stage_row(r: Mapping[str, tuple[float, float]], mv: float, votes: Mapping[str, str]) -> dict[str, Any]:
    """One stage flattened the way the research timeline rows are (``OI_CE`` … ``stock_atr``, ``vote_OI`` …)."""
    out: dict[str, Any] = {f"{k}_{s}": r[k][i] for k in ("OI", "VOL", "IV", "PREMIUM") for i, s in enumerate(("CE", "PE"))}
    out["stock_atr"] = mv
    out.update({f"vote_{k}": votes.get(k, "") for k in READS})
    return out


# -- the VOL baseline (live9.py) ------------------------------------------------------------------------------------


def bars_back(sessions: Sequence[str], start: str, n: int) -> list[tuple[str, str]]:
    """research ``bar_seq_back``: the bar starting at ``start`` on the LAST of ``sessions``, then n-1 bars before it
    on the session grid, previous sessions included (``sessions`` oldest first, today last)."""
    out: list[tuple[str, str]] = []
    k, i = len(sessions) - 1, GRID.index(start)
    while len(out) < n and k >= 0:
        out.append((sessions[k], GRID[i]))
        i -= 1
        if i < 0:
            k, i = k - 1, len(GRID) - 1
    return out


def bar_bounds(day: str, start: str) -> tuple[float, float]:
    """A grid bar's [start, end): 30 minutes, and the 15:15 bar to 15:30."""
    a = at_ist(day, start)
    return a, (at_ist(day, "15:30") if start == "15:15" else a + 1800)


def baseline_30m(vols: Sequence[float]) -> float:
    """The mean volume of the 30m bars T-2..T-7 (the VolSurge denominator), floored at 1,000; NaN when any of the
    six is not verified (NaN) or fewer than six are given."""
    if len(vols) < 6 or not all(isnum(v) for v in vols):
        return NAN
    return max(sum(vols) / len(vols), VOL_FLOOR)


# -- the strike walls (wall_state.py, oct9_ext.py) ------------------------------------------------------------------


def pick_walls[T](oi: Iterable[tuple[str, float, float, T]], spot: float) -> dict[str, tuple[float, float, T] | None]:
    """The walls: the CE strike with the largest OI at or above ``spot`` and the PE strike with the largest OI at or
    below it, among the strikes given — ``(otype, strike, oi, payload)`` of the recorded strikes (an OI print today
    at the moment, no previous-day fallback). Ties go to the lower strike (research iterates the chain in order)."""
    best: dict[str, tuple[float, float, T] | None] = {"CE": None, "PE": None}
    for ot, k, o, p in sorted(oi, key=lambda x: (x[0], x[1])):
        if ot not in best or not isnum(o) or not isnum(spot):
            continue
        if (ot == "CE" and k >= spot) or (ot == "PE" and k <= spot):
            cur = best[ot]
            if cur is None or o > cur[1]:
                best[ot] = (k, o, p)
    return best


def wall_state(oi_chg: float, px_chg: float) -> str:
    if not isnum(oi_chg):
        return ""
    if abs(oi_chg) <= WALL_FLAT_PCT:
        return "FLAT"
    if not isnum(px_chg):
        return "OI UP (no premium data)" if oi_chg > 0 else "OI DOWN (no premium data)"
    if px_chg == 0:
        return "OI UP (premium unchanged)" if oi_chg > 0 else "OI DOWN (premium unchanged)"
    if oi_chg > 0:
        return "LONG BUILD-UP" if px_chg > 0 else "SHORT BUILD-UP"
    return "SHORT COVERING" if px_chg > 0 else "LONG UNWINDING"


def wall_interp(side: str, state: str) -> str:
    """CE wall: short build-up Bearish, short covering Bullish, long build-up Bullish, long unwinding Bearish; the PE
    wall the mirror; FLAT (or an OI move without a premium) Neutral; nothing read, ''."""
    return STATE_INT[side].get(state, "Neutral" if state else "")


def wall_overall(a: str, b: str) -> str:
    """Both walls agree: that; one Neutral: the other's; they disagree: Mixed."""
    s = {a, b} - {"", "Neutral"}
    if not s:
        return "Neutral" if "Neutral" in (a, b) else ""
    return s.pop() if len(s) == 1 else "Mixed"


def oicp(doi: float, deltas: Sequence[float], oi_chg: float) -> tuple[float, str, int]:
    """FUDKOI's oiChangePct: dOI over the mean |dOI| of the last six non-zero 30m windows before the trigger candle
    (today + the 2 previous sessions) × 100; fewer than three such windows: dOI / OI × 100 (``oi_chg``).
    Returns (value, basis, windows)."""
    base = sum(deltas) / len(deltas) if len(deltas) >= 3 else NAN
    basis = "vs last 6 non-zero 30m candles" if isnum(base) else "fewer than 3 baseline candles: dOI / OI"
    if isnum(doi) and isnum(base) and base > 0:
        return doi / base * 100, basis, len(deltas)
    return oi_chg, basis, len(deltas)


def wall_read(side: str, oi1: float, oi0: float, p1: float, p0: float, deltas: Sequence[float]) -> dict[str, Any]:
    """One wall at one moment: OI change, premium change, state, interpretation, oiChangePct and significance."""
    doi = oi1 - oi0 if isnum(oi1) and isnum(oi0) else NAN
    oi_chg = doi / oi0 * 100 if isnum(doi) and oi0 > 0 else NAN
    px_chg = (p1 / p0 - 1) * 100 if isnum(p1) and isnum(p0) and p0 > 0 else NAN
    ocp, basis, n = oicp(doi, deltas, oi_chg)
    st = wall_state(oi_chg, px_chg)
    return {"oi": oi1, "oi0": oi0, "doi": doi, "oiChg": oi_chg, "px": p1, "px0": p0, "pxChg": px_chg, "oicp": ocp,
            "oicpBasis": basis, "nBase": n, "significant": (abs(ocp) > OICP_SIGNIFICANT) if isnum(ocp) else None,
            "state": st, "why": WHY.get(st, ""), "interp": wall_interp(side, st)}


def oi_deltas(windows: Iterable[tuple[float, float]], want: int = 6) -> list[float]:
    """|dOI| of the non-zero windows, newest first, up to ``want`` — the (OI at start, OI at end) pairs come in that
    order (wall_state: today's finished bars before the trigger candle, then the previous sessions')."""
    out: list[float] = []
    for o0, o1 in windows:
        if isnum(o0) and isnum(o1) and o1 != o0:
            out.append(abs(o1 - o0))
        if len(out) >= want:
            break
    return out


__all__ = [
    "FOLLOW_MIN", "GRID", "LEGS", "NAN", "READS", "STATE_INT", "WHY",
    "at_ist", "b76", "bar_bounds", "bars_back", "baseline_30m", "decide", "first_n", "isnum", "iv76", "label",
    "legs_expiry", "oi_change", "oi_deltas", "oicp", "opt_px", "pick_legs", "pick_walls", "px_change", "reads",
    "side_vote", "split", "stage_row", "stock_vote", "thin", "tte_years", "vol_ratio", "wall_interp", "wall_overall",
    "wall_read", "wall_state",
]
