"""The 5-read and the strike walls of one FUDKII trigger, read from the option recorder — RECORD-ONLY.

``research/read5.py`` holds the arithmetic (ported from the research, unchanged); ``ops/optrec.py`` holds the as-of
quotes, OI and volume. This module joins them the way ``research-data/mdoft/oct15/live9b_tm15.py`` did on its tape:

* ``snap(x)`` — the stock's last price at or before x (any age); the future's mid when two-sided and at most 60 s
  old, else its last trade; each leg's price (``opt_px``), its OI (the last print at or before x, else the previous
  session's), and its implied vol, time-matched: the future and the time to expiry taken at the moment of the price
  used (the quote time of a mid, else the start of the current run of equal last prices);
* the trigger read: X = snap(signal), B = snap(candle start) — for the opening candle the prices are the first print
  at or after 09:15 (its IV time-matched to that print) and OI is read at 09:16;
* each follow-up minute m = 1 … 16: X = snap(signal + m min) against B = snap(signal);
* the walls: the largest recorded OI call strike at or above the trigger's close and put strike at or below it, at
  signal + 10 s, held fixed; their OI and premium change over the 30 minutes up to a moment, and the state.

Every record says what it could not read, and why. Nothing here is read by a decision.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..domain import Instrument, OptionType
from ..market.session import IST
from ..research import read5 as R
from .optrec import OptionRecorder, hms

NAN = math.nan
#: reads whose two sides are change values (OI, IV, PREMIUM need the baseline and the moment; VOL a window)
SIDES = {"CE": ("CE1", "CE2"), "PE": ("PE1", "PE2")}


def hm(t: float) -> str:
    return datetime.fromtimestamp(float(t), IST).strftime("%H:%M")


def jnum(v: Any) -> Any:
    """A JSON-safe number: NaN / inf → None, numpy scalars → float."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return f if math.isfinite(f) else None


@dataclass(slots=True)
class Contracts:
    """One trigger's contracts: the legs (None where not listed), the front future, the cash/index code, and the
    legs' expiry chain (both types) the walls are picked from."""

    symbol: str
    expiry: str
    legs: dict[str, Instrument | None]
    fut: Instrument | None
    eq: Instrument | None
    chain: list[Instrument] = field(default_factory=list)

    def leg_codes(self) -> list[str]:
        return [i.scrip_code for i in self.legs.values() if i is not None]


def contracts(cat: Any, symbol: str, spot: float, today: date, eq: Instrument | None) -> tuple[Contracts | None, str]:
    """inputs14.build: the legs' expiry (the nearest at least 2 calendar days after today, from TODAY's date), CE1 /
    CE2 the first and second listed call strike above ``spot``, PE1 / PE2 the first and second put below it; FUT0 the
    nearest future expiring today or later; EQ the cash (or index) code. (None, why) when there is no NSE chain."""
    exp = R.legs_expiry(cat.expiries(symbol, on=today), today)
    if exp is None:
        return None, "no NSE option chain (MCX / no options listed)"
    ce = cat.chain(symbol, exp, OptionType.CE)
    pe = cat.chain(symbol, exp, OptionType.PE)
    picks = R.pick_legs([(i.strike, i) for i in ce], [(i.strike, i) for i in pe], spot)
    legs = {k: (v[1] if v is not None else None) for k, v in picks.items()}
    fut = cat.front_future(symbol, on=today)
    return Contracts(symbol=symbol, expiry=exp, legs=legs, fut=fut, eq=eq, chain=[*ce, *pe]), ""


# -- snapshots ---------------------------------------------------------------------------------------------------


def fwd_at(rec: OptionRecorder, fut: Instrument | None, t: float) -> tuple[float, str, float]:
    """research ``fwd_at``: the future at t — its two-sided mid if at most 60 s old, else its last trade at or before
    t. (price, source, its time)."""
    if fut is None:
        return NAN, "", NAN
    q = rec.quote(fut.scrip_code, t, 60)
    if q is None:
        return NAN, "", NAN
    if q["two"] and q["fresh"]:
        return q["mid"], "mid" + ("" if q["src"] == "ring" else "@minute"), q["eff"]
    return q["ltp"], "ltp" + ("" if q["src"] == "ring" else "@minute"), q["eff"]


def _iv(px: float, tp: float, rec: OptionRecorder, c: Contracts, inst: Instrument) -> dict[str, Any]:
    F, fsrc, ft = fwd_at(rec, c.fut, tp)
    if not R.isnum(F):
        return {"iv": NAN, "ivWhy": "no future price at the option's price time", "F": NAN, "FSrc": "", "FT": NAN}
    iv, why = R.iv76(px, F, inst.strike, R.tte_years(c.expiry, tp), inst.option_type.value)
    return {"iv": iv, "ivWhy": "" if why == "ok" else why, "F": F, "FSrc": fsrc, "FT": ft}


def leg_snap(rec: OptionRecorder, c: Contracts, inst: Instrument, x: float) -> dict[str, Any]:
    q = rec.quote(inst.scrip_code, x, 60)
    px, src = R.opt_px(q)
    o, ot, basis = rec.oi_at(inst.scrip_code, x)
    out: dict[str, Any] = {"px": px, "pxSrc": src, "pxT": NAN, "oi": o, "oiT": ot, "oiBasis": basis}
    if R.isnum(px) and q is not None:
        tp = q["eff"] if src == "mid" else q["run"]
        out["pxT"] = tp
        out.update(_iv(px, tp, rec, c, inst))
    else:
        out.update(iv=NAN, ivWhy="no price" if q is None else "no usable price", F=NAN, FSrc="", FT=NAN)
    return out


def snap(rec: OptionRecorder, c: Contracts, x: float) -> dict[str, Any]:
    """research ``snap(x)``."""
    out: dict[str, Any] = {"x": x}
    q = rec.quote(c.eq.scrip_code, x, 10**9) if c.eq is not None else None
    out["stock"] = q["ltp"] if q is not None else NAN
    out["F"] = fwd_at(rec, c.fut, x)[0]
    for k, inst in c.legs.items():
        out[k] = leg_snap(rec, c, inst, x) if inst is not None else {"px": NAN, "oi": NAN, "iv": NAN, "ivWhy": "not listed"}
    return out


def opening_baseline(rec: OptionRecorder, c: Contracts, c0: float, s: float) -> dict[str, Any]:
    """The 09:15 candle's baseline: snap(09:16) for OI; the stock and each leg priced at its first print at or after
    09:15 (before the signal), the leg's IV time-matched to that print's last-trade time."""
    B = snap(rec, c, c0 + 60)
    B["x"] = c0
    fa = rec.first_after_open(c.eq.scrip_code) if c.eq is not None else None
    B["stock"] = fa[1] if fa is not None and fa[0] < s else NAN
    for k, inst in c.legs.items():
        if inst is None:
            continue
        leg = B[k]
        fa = rec.first_after_open(inst.scrip_code)
        if fa is not None and fa[0] < s:
            leg.update(px=fa[1], pxSrc="first print", pxT=fa[2])
            leg.update(_iv(fa[1], fa[2], rec, c, inst))
        else:
            leg.update(px=NAN, pxSrc="", pxT=NAN, iv=NAN, ivWhy="no print from 09:15 before the signal", F=NAN, FSrc="", FT=NAN)
    return B


# -- volume ------------------------------------------------------------------------------------------------------


def baseline30(rec: OptionRecorder, code: str, sessions: Sequence[str], start: str, now: float) -> tuple[float, str]:
    """live9.py ``baseline_30m``: the mean of the 30m bars T-2..T-7 floored at 1,000; (NaN, why) when any is not on
    record."""
    if start not in R.GRID:
        return NAN, f"trigger candle {start} is not on the 30m grid"
    vols, whys = [], []
    for d, b in R.bars_back(sessions, start, 8)[2:8]:
        v, why = rec.bar30(code, d, b, now)
        vols.append(v)
        if why:
            whys.append(why)
    if len(vols) < 6:
        return NAN, "fewer than six baseline bars on the session calendar"
    base = R.baseline_30m(vols)
    return base, ("" if R.isnum(base) else (whys[0] if whys else "baseline not recorded"))


def leg_vol(rec: OptionRecorder, code: str, a: float, b: float, normal: float, normal_why: str) -> tuple[float, float, str]:
    v, why = rec.volume(code, a, b)
    if why:
        return NAN, normal, why
    if not R.isnum(normal):
        return v, NAN, normal_why
    return v, normal, ""


# -- the reads ---------------------------------------------------------------------------------------------------


def _missing(X: Mapping[str, Any], B: Mapping[str, Any], r: Mapping[str, tuple[float, float]], vol_why: Mapping[str, str],
             mv: float, atr: float, legs: Mapping[str, Instrument | None], x: float, b: float) -> dict[str, str]:
    """Why each read that cannot vote is missing a side: the leg (or bar) and the reason."""
    out: dict[str, str] = {}

    def not_listed(k: str) -> str:
        return f"{k} not listed"
    for read in ("OI", "VOL", "IV", "PREMIUM"):
        bits: list[str] = []
        for side, (a_, b_) in SIDES.items():
            if R.isnum(r[read][0 if side == "CE" else 1]):
                continue
            for k in (a_, b_):
                if legs.get(k) is None:
                    bits.append(not_listed(k))
                    continue
                xl, bl = X.get(k) or {}, B.get(k) or {}
                if read == "OI":
                    if not R.isnum(xl.get("oi")):
                        bits.append(f"{k} no OI print by {hms(x)}")
                    if not R.isnum(bl.get("oi")):
                        bits.append(f"{k} no OI print by {hms(b)}")
                elif read == "VOL":
                    if vol_why.get(k):
                        bits.append(f"{k} {vol_why[k]}")
                elif read == "IV":
                    if not R.isnum(xl.get("iv")):
                        bits.append(f"{k} no IV at {hms(x)} ({xl.get('ivWhy') or 'no price'})")
                    if not R.isnum(bl.get("iv")):
                        bits.append(f"{k} no IV at the baseline ({bl.get('ivWhy') or 'no price'})")
                elif read == "PREMIUM":
                    if not R.isnum(xl.get("px")):
                        bits.append(f"{k} not priced at {hms(x)}")
                    if not R.isnum(bl.get("px")):
                        bits.append(f"{k} not priced at the baseline")
        if bits:
            out[read] = "; ".join(dict.fromkeys(bits))
    if not R.isnum(mv):
        out["STOCK"] = ("no ATR30" if not (R.isnum(atr) and atr > 0) else
                        "no stock price at " + (hms(x) if not R.isnum(X.get("stock")) else "the baseline"))
    return out


def read_stage(X: Mapping[str, Any], B: Mapping[str, Any], vol: Mapping[str, tuple[float, float, str]], atr: float,
               legs: Mapping[str, Instrument | None], b_time: float) -> dict[str, Any]:
    """The five reads of one stage, their votes, the call and what was missing."""
    r, mv, votes = R.reads(X, B, {k: (v[0], v[1]) for k, v in vol.items()}, atr)
    nb, ns, nk, lead = R.decide(votes)
    missing = _missing(X, B, r, {k: v[2] for k, v in vol.items()}, mv, atr, legs, float(X["x"]), b_time)
    return {"r": r, "mv": mv, "votes": votes, "bull": nb, "bear": ns, "skip": nk, "lead": lead or "", "missing": missing}


def _vol_basis(stage: Mapping[str, Any]) -> str:
    return "5-read" if "VOL" not in stage["missing"] else "provisional 4-read: VOL not recorded"


def trigger_read(rec: OptionRecorder, c: Contracts, *, signal_id: str, symbol: str, direction: str, c0: float, s: float,
                 spot: float, atr: float, atr_src: str, sessions: Sequence[str], now: float
                 ) -> tuple[dict[str, Any], dict[str, Any], dict[str, float]]:
    """The 5-read AT the trigger: over the trigger candle (candle start → signal). Returns (the record, snap(signal)
    — the follow-up's baseline — and each leg's VOL baseline)."""
    first = hm(c0) == "09:15"
    X = snap(rec, c, s)
    B = opening_baseline(rec, c, c0, s) if first else snap(rec, c, c0)
    start = hm(c0)
    vol: dict[str, tuple[float, float, str]] = {}
    base: dict[str, tuple[float, str]] = {}
    for k, inst in c.legs.items():
        if inst is None:
            vol[k] = (NAN, NAN, "not listed")
            continue
        b30, bwhy = baseline30(rec, inst.scrip_code, sessions, start, now)
        base[k] = (b30, bwhy)
        vol[k] = leg_vol(rec, inst.scrip_code, c0, s, b30, bwhy)
    st = read_stage(X, B, vol, atr, c.legs, B["x"])
    w, a, n = R.split(st["votes"], direction)
    lead = st["lead"]
    legs_out: dict[str, Any] = {}
    for k, inst in c.legs.items():
        if inst is None:
            legs_out[k] = None
            continue
        legs_out[k] = {"code": inst.scrip_code, "strike": inst.strike, "type": inst.option_type.value,
                       "X": _leg_json(X[k]), "B": _leg_json(B[k]), "vol": jnum(vol[k][0]), "normal": jnum(vol[k][1]),
                       "volWhy": vol[k][2], "b30": jnum(base.get(k, (NAN, ""))[0]), "streamingSince": jnum(rec.streaming_since(inst.scrip_code))}
    no_px = [k for k, inst in c.legs.items() if inst is None or not R.isnum(X[k].get("px"))]
    record = {
        "signal_id": signal_id, "symbol": symbol, "direction": direction, "candle": f"{start}-{hm(c0 + 1800)}",
        "c0": c0, "s": s, "signalTime": hms(s), "spot": spot, "expiry": c.expiry,
        "fut": c.fut.scrip_code if c.fut else None, "eq": c.eq.scrip_code if c.eq else None,
        "atr30": jnum(atr), "atrSrc": atr_src,
        "window": (f"trigger candle: prices from the 09:15 open, OI from 09:16, to {hms(s)}" if first
                   else f"trigger candle {hms(c0)}-{hms(s)}"),
        "stock": {"X": jnum(X["stock"]), "B": jnum(B["stock"]), "moveAtr": jnum(st["mv"])},
        "legs": legs_out,
        "reads": {k: {"ce": jnum(st["r"][k][0]), "pe": jnum(st["r"][k][1]), "vote": st["votes"][k],
                      "missing": st["missing"].get(k, "")} for k in ("OI", "VOL", "IV", "PREMIUM")}
        | {"STOCK": {"move": jnum(st["mv"]), "vote": st["votes"]["STOCK"], "missing": st["missing"].get("STOCK", "")}},
        "votes": st["votes"], "bull": st["bull"], "bear": st["bear"], "skip": st["skip"], "lead": lead,
        "leadVsEngine": "" if not lead else ("with" if lead == direction else "against"),
        "with": w, "against": a, "noVote": n, "split": f"{len(w)}-{len(a)}",
        "missingReads": [k for k in R.READS if k in st["missing"]],
        "legsNoPriceAtSignal": no_px,
        "basis": _vol_basis(st),
        "stage0": _stage_json(0, s, st),
    }
    return record, X, {k: v[0] for k, v in base.items()}


def follow_stage(rec: OptionRecorder, c: Contracts, *, s: float, m: int, B: Mapping[str, Any], b30: Mapping[str, float],
                 atr: float) -> dict[str, Any]:
    """Minute m after the signal: X = snap(signal + m min) against B = snap(signal); the volume since the signal's
    minute against the baseline's per-minute share (live9.py)."""
    x = s + 60 * m
    X = snap(rec, c, x)
    m0 = int(s // 60 * 60)
    mins = max(1, int((x - m0) // 60))
    vol: dict[str, tuple[float, float, str]] = {}
    for k, inst in c.legs.items():
        if inst is None:
            vol[k] = (NAN, NAN, "not listed")
            continue
        base = b30.get(k, NAN)
        normal = base / 30.0 * mins if R.isnum(base) else NAN
        vol[k] = leg_vol(rec, inst.scrip_code, m0, x, normal, "no baseline")
    st = read_stage(X, B, vol, atr, c.legs, float(B["x"]))
    return _stage_json(m, x, st)


def _leg_json(d: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (jnum(v) if not isinstance(v, str) else v) for k, v in d.items()}


def _stage_json(m: int, x: float, st: Mapping[str, Any]) -> dict[str, Any]:
    return {"m": m, "x": x, "t": hms(x), "r": {k: [jnum(v[0]), jnum(v[1])] for k, v in st["r"].items()},
            "mv": jnum(st["mv"]), "votes": dict(st["votes"]), "bull": st["bull"], "bear": st["bear"], "skip": st["skip"],
            "lead": st["lead"], "missing": dict(st["missing"])}


def stage_row(stage: Mapping[str, Any]) -> dict[str, Any]:
    """A stored stage as build_calls' row (``OI_CE`` … ``stock_atr``, ``vote_OI`` …) for the label."""
    row: dict[str, Any] = {}
    for k in ("OI", "VOL", "IV", "PREMIUM"):
        ce, pe = stage["r"][k]
        row[f"{k}_CE"] = NAN if ce is None else ce
        row[f"{k}_PE"] = NAN if pe is None else pe
    row["stock_atr"] = NAN if stage["mv"] is None else stage["mv"]
    row.update({f"vote_{k}": stage["votes"].get(k, "") for k in R.READS})
    return row


def first_calls(stages: Sequence[Mapping[str, Any]], direction: str) -> dict[str, Any]:
    """The first lead (2 of 5), the first 3 of 5 and 4 of 5 (the trigger included), each with its moment, reads and —
    for 3 of 5 — build_calls' label."""
    out: dict[str, Any] = {"f2": None, "f3": None, "f4": None}
    entry = next((st for st in stages if st["lead"]), None)
    if entry is not None:
        out["f2"] = {"minute": entry["m"], "time": entry["t"], "x": entry["x"], "call": entry["lead"],
                     "vsEngine": "with engine" if entry["lead"] == direction else "against engine"}
    for n in (3, 4):
        hit = R.first_n([st["votes"] for st in stages], n)
        if hit is None:
            continue
        i, call, reads_ = hit
        st = stages[i]
        rec_: dict[str, Any] = {"minute": st["m"], "time": st["t"], "x": st["x"], "call": call, "reads": reads_,
                                "vsEngine": "with engine" if call == direction else "against engine"}
        if n == 3:
            lab = R.label(stage_row(st), call)
            rec_.update(split=lab["vote_split"], label=lab["label"], weakBecause=lab["weak_because"], thin=lab["thin_for"],
                        readsFor=lab["reads_for"], readsAgainst=lab["reads_against"])
        out[f"f{n}"] = rec_
    return out


# -- the walls ---------------------------------------------------------------------------------------------------


def pick_walls(rec: OptionRecorder, c: Contracts, spot: float, at: float) -> dict[str, Instrument | None]:
    """oct9_ext: among the legs' expiry strikes with an OI print today at or before ``at`` (no previous-day
    fallback), the largest-OI call at or above the trigger's close and the largest-OI put at or below it."""
    cands = []
    for inst in c.chain:
        o, _, _ = rec.oi_at(inst.scrip_code, at, allow_prev=False)
        if R.isnum(o):
            cands.append((inst.option_type.value, inst.strike, o, inst))
    picked = R.pick_walls(cands, spot)
    return {side: (v[2] if v is not None else None) for side, v in picked.items()}


def wall_at(rec: OptionRecorder, side: str, inst: Instrument, x: float, c0: float, prev_days: Sequence[str]) -> dict[str, Any]:
    """wall_state.py at moment x: the OI and premium change over the 30 minutes to x (a window starting at 09:15
    reads OI from 09:16 and the premium from the first print at or after 09:15), the state, its reading and
    oiChangePct against the last six non-zero 30m windows before the trigger candle."""
    code = inst.scrip_code
    day = datetime.fromtimestamp(x, IST).date().isoformat()
    open_ = R.at_ist(day, "09:15")
    x0 = x - 1800
    first = x0 <= open_ + 60
    oi0 = rec.oi_at(code, max(x0, open_ + 60) if first else x0)[0]
    oi1 = rec.oi_at(code, x)[0]
    if first:
        fa = rec.first_after_open(code)
        p0, src0 = (fa[1], "first print") if fa is not None and fa[0] < x else (NAN, "")
    else:
        p0, src0 = R.opt_px(rec.quote(code, x0, 60))
    p1, src1 = R.opt_px(rec.quote(code, x, 60))
    deltas = R.oi_deltas(rec.oi_windows(code, c0, list(prev_days)))
    w = R.wall_read(side, oi1, oi0, p1, p0, deltas)
    px_src = "quotes" if R.isnum(p0) and R.isnum(p1) else "none (not quoted on this engine's feed)"
    return {"strike": inst.strike, "code": code, "time": hms(x), "window": f"{hms(max(x0, open_))}-{hms(x)}",
            **{k: (jnum(v) if not isinstance(v, (str, bool)) and v is not None else v) for k, v in w.items()},
            "pxSrc": px_src, "px0Src": src0, "px1Src": src1}


def walls(rec: OptionRecorder, c: Contracts, picked: Mapping[str, Instrument | None], x: float, c0: float,
          prev_days: Sequence[str], spot: float, at: float) -> dict[str, Any]:
    out: dict[str, Any] = {"spot": spot, "pickedAt": hms(at), "moment": hms(x)}
    for side in ("CE", "PE"):
        inst = picked.get(side)
        out[side.lower()] = wall_at(rec, side, inst, x, c0, prev_days) if inst is not None else None
    ce, pe = out["ce"], out["pe"]
    out["overall"] = R.wall_overall((ce or {}).get("interp", "") or "", (pe or {}).get("interp", "") or "")
    return out


# -- the archive row and the card ----------------------------------------------------------------------------------


def flat_row(trig: Mapping[str, Any], follow: Mapping[str, Any] | None) -> dict[str, Any]:
    """One flat row per trigger (``archive/read5/<day>.parquet``), named as ``score5_all.csv``'s ``t_*`` / ``f3_*``
    columns, plus the walls; rewritten when the follow-up ends (the last write wins)."""
    row: dict[str, Any] = {"signal_id": trig["signal_id"], "symbol": trig["symbol"], "s": float(trig["s"]),
                           "signal_time": trig.get("signalTime") or hms(trig["s"]), "engine_direction": trig["direction"],
                           "candle_start": (trig.get("candle") or "")[:5], "scored": "no" if trig.get("excluded") else "yes",
                           "no_score_why": trig.get("excluded") or ""}
    if trig.get("excluded"):
        return row
    row.update(score_basis=trig["basis"], t_split=trig["split"], t_reads_with=", ".join(trig["with"]),
               t_reads_against=", ".join(trig["against"]), t_reads_no_vote=", ".join(trig["noVote"]),
               t_lead=trig["lead"], t_lead_vs_engine=trig["leadVsEngine"],
               reads_nan_at_trigger=", ".join(k for k in ("OI", "VOL", "IV", "PREMIUM") if k in trig["missingReads"]),
               legs_no_price_at_signal=",".join(trig["legsNoPriceAtSignal"]), atr30=trig["atr30"], atr30_src=trig["atrSrc"],
               expiry=trig["expiry"])
    for k in R.READS:
        row[f"t_vote_{k}"] = trig["votes"][k]
    for k in ("OI", "VOL", "IV", "PREMIUM"):
        row[f"t_{k}_CE"] = trig["reads"][k]["ce"]
        row[f"t_{k}_PE"] = trig["reads"][k]["pe"]
    row["t_stock_atr"] = trig["reads"]["STOCK"]["move"]
    for k, leg in trig["legs"].items():
        row[f"{k}_code"] = leg["code"] if leg else None
        row[f"{k}_strike"] = leg["strike"] if leg else None
    for tag, w in (("wall", trig.get("walls")), ("f3_wall", (follow or {}).get("wallsAtF3"))):
        for side in ("ce", "pe"):
            ws = (w or {}).get(side) or {}
            for f in ("strike", "state", "interp", "oiChg", "pxChg", "oicp", "significant"):
                v = ws.get(f)
                row[f"{tag}_{side}_{f}"] = v if not isinstance(v, bool) else ("yes" if v else "no")
        row[f"{tag}_overall"] = (w or {}).get("overall")
    f3 = (follow or {}).get("f3")
    if f3:
        row.update(f3_call=f3["call"], f3_time=f3["time"], f3_minute=f3["minute"], f3_vs_engine=f3["vsEngine"],
                   f3_split=f3["split"], f3_reads_for=f3["readsFor"], f3_reads_against=f3["readsAgainst"],
                   f3_label=f3["label"], f3_weak_because=f3["weakBecause"], f3_thin=f3["thin"])
    elif follow is not None and follow.get("complete"):
        row.update(f3_call="", f3_label="Skip", f3_weak_because="no 3 of 5 within 16 min")
    row["follow_state"] = "complete" if (follow or {}).get("complete") else ("running" if follow is not None else "")
    return row


def _fmt(v: Any, nd: int = 2) -> str:
    return "—" if v is None else f"{v:+.{nd}f}"


def card_lines(trig: Mapping[str, Any] | None, follow: Mapping[str, Any] | None, *, now: float,
               running: bool = False) -> list[str]:
    """The card's read-only text (record-only: nothing here is a gate)."""
    if trig is None:
        return []
    if trig.get("excluded"):
        return [f"5-read not taken: {trig['excluded']}"]
    miss = trig.get("reads") or {}
    nv = []
    for k in trig["noVote"]:
        why = (miss.get(k) or {}).get("missing")
        nv.append(f"{k} ({why})" if why else k)
    line1 = (f"5-read at trigger {trig['split']} with ({', '.join(trig['with']) or 'none'})"
             + (f" · against {', '.join(trig['against'])}" if trig["against"] else "")
             + (f" · no vote {'; '.join(nv)}" if nv else "")
             + (f" · {trig['basis']}" if trig["basis"] != "5-read" else ""))
    out = [line1]
    f3 = (follow or {}).get("f3")
    if f3:
        out.append(f"first 3 of 5: {f3['call']} {f3['time']} (+{f3['minute']} min, {f3['label']}) — {f3['vsEngine']}")
    elif follow is not None and follow.get("complete"):
        out.append("no 3 of 5 within 16 min")
    elif running:
        out.append("follow-up running")
    elif now > float(trig["s"]) + 17 * 60:
        out.append("follow-up interrupted (restart)")
    else:
        out.append("follow-up running")
    w = trig.get("walls") or {}
    bits = []
    for side in ("ce", "pe"):
        ws = w.get(side)
        if ws:
            bits.append(f"{side.upper()} {ws['strike']:g} {ws['state'] or 'not read'} → {ws['interp'] or '—'}"
                        + (f" (OI {_fmt(ws.get('oiChg'))} %, premium {_fmt(ws.get('pxChg'))} %)" if ws.get("oiChg") is not None else ""))
        else:
            bits.append(f"{side.upper()} none recorded")
    if w:
        out.append("walls " + " · ".join(bits) + f" · overall {w.get('overall') or '—'}")
    return out


__all__ = ["Contracts", "baseline30", "card_lines", "contracts", "first_calls", "flat_row", "follow_stage", "fwd_at",
           "jnum", "leg_snap", "opening_baseline", "pick_walls", "read_stage", "snap", "stage_row", "trigger_read",
           "wall_at", "walls"]
