"""The same option, elsewhere: on a reviewed trade's card, every other trade on the same option contract while it was held,
in this engine and its twin, and for each one that made more, the exact rupees by which it did and the lesson the pooled
record supports (operator, 2026-10-09: "add there insights/pnl from another engine or/and strategy that traded the same
OTM at that time and if it made better profit than that startegy on the card. mention its features/parameters that led
to better outcomes which the current strategy can learn from. dont make it superficial, touch depth of things").

RECORD-ONLY. Built at request time from what the engines stored: the positions and their execution logs, the closed
trades, the exit orders refused for a stale book, and each book's limits as the engine holds them. Nothing here moves an
order, a stop or a wallet; nothing in engine.py, exec/ or risk/ imports it (tests/test_review_peers.py and the import
contract in pyproject.toml). The twin's trades come to this module as plain records its own ``/api/review/legs`` built
(api/routes.py reads them, GET only, loopback only, one hop).

The spec is the same-option study's card_spec.md (9 Oct 2026; p1_legs → p5_cards). How each number is worked out:

* **a trade as its peers see it** (``record``): q = qty, e = the entry fill, c = charges, x = e + gross / q (the ledger's
  own qty-weighted exit), r = the option's signal price at the entry decision (``exec_log.entry.ref``; = e without one).
  Its exit legs from the execution log: target rungs (``kind`` "target") and the final legs. A trade whose legs do not
  reproduce the ledger (before 29 Sep 09:45 there is no log) gets one final leg at x, its decision price equal to its fill,
  and says "no exit audit".
* **a refused exit** (MODELLED, always its own part): the first EXIT order REFUSED (the paper matcher would not price it
  on a book that old) with a price — its stop's executable price, else the bid then, else its ``ref`` — at t_r. Every leg
  filled at or after t_r is replaced by one final leg at that price: x_cf. rho = x − x_cf, per unit: what the refusal
  moved, assuming the refused order would have filled at the price the engine recorded then.
* **the exit anatomy**, on the x_cf legs: F = the final legs' qty-weighted fill, D = their decision price (``ref`` where
  the rule fired), X = F − D (the execution), T = Σ rungs qty_k (fill_k − F) / q (what the rungs sold above the final
  exit), rs = the share of the qty the rungs sold. x = D + X + T + rho, exactly.
* **the pair** (a = the trade that made less, b = more, Δ = net_b − net_a), symmetric in size, q̄ = (q_a + q_b) / 2,
  p = x − e: size (q_b − q_a)(p_a + p_b) / 2; entry timing −q̄ (r_b − r_a); entry paid −q̄ ((e_b − r_b) − (e_a − r_a));
  exit decision q̄ w (D_b − D_a) and exit fills q̄ w (X_b − X_a), where w = 1 − min(rs_a, rs_b) is the share of the qty
  NOT sold at a rung by both; target rungs = q̄ (x_cf_b − x_cf_a) less those two; refusal q̄ (rho_b − rho_a); charges
  −(c_b − c_a). They sum to Δ exactly. The study's spec puts the final exit's difference on the whole qty (w = 1) and
  lets the rungs part take it back; the two agree whenever either trade sold no rung, and differ only in how the same
  exit total is split when both did (TCS 9 Oct: both sold the same two rungs at 36.40 and 42.75, then the trail at 41.35
  against 40.35 — here the whole ₹450 is the final exit on the 450 left, not "rungs −₹450, exit +₹900").
* **the stop rule**: when a and b run different stop rules (current / E / adaptive), a's own sibling a′ (the same fill,
  the mirror under b's rule) is used: a → a′ → b, and the target-rung, exit-decision and exit-fills parts of the a → a′
  link are the **stop rule** part. Two siblings: the whole exit difference is the stop rule.
* **the lesson**: the study's deterministic rules (card_spec.md §5.4) on the pooled tables (``review_evidence``, or a
  newer ``<data_dir>/peer_evidence.json``), graded for the operator: "systematic (held across days and both engines)",
  "suggestive", or "one-off / paper artefact", always with the counts behind it. A refusal or a halt is never a lesson.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..strategy.keys import SHADOW_BOOKS, stop_rule_of
from .review_evidence import EVIDENCE

IST = timezone(timedelta(hours=5, minutes=30))

#: the parts, in the order a card lists them when they tie
PARTS = ("entry_time", "entry_paid", "size", "ladder", "exit_rule", "exit_exec", "stop_rule", "refusal", "charges")
PART_LABEL = {
    "entry_time": "entry timing", "entry_paid": "entry fill", "size": "size", "ladder": "target rungs",
    "exit_rule": "exit decision", "exit_exec": "exit fills", "stop_rule": "stop rule", "refusal": "stale-book refusal (modelled)",
    "charges": "charges",
}
SHORT = {"FUDKII": "FUDKII", "FUDKII_RT_X": "RT-X", "FUDKII_RT_N": "RT-N", "FUDKII_RT_Y": "RT-Y", "FUDKII_RT_Y_F": "RT-Y-F",
         "FUDKII_RT_Y_W1": "RT-Y-W1", "FUDKII_CT_X": "CT-X", "FUDKII_CT_Y": "CT-Y", "FUDKII_CT_M": "CT-M",
         "FUDKII_CT_Y_F": "CT-Y-F", "FUDKII_RT_MCX": "RT-MCX", "FUKAA": "FUKAA"}
BOOK_ORDER = ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_RT_Y_W1", "FUDKII_RT_Y_F", "FUDKII_CT_X",
              "FUDKII_CT_Y", "FUDKII_CT_M", "FUDKII_CT_Y_F")
CLASS_ORDER = {"trading": 0, "shadow": 1, "mirror": 2}
RULE_ORDER = {"current": 0, "E": 1, "A": 2}
RULE_NAME = {"current": "current stop", "E": "stock stop E (60 s)", "A": "stock stop A (adaptive)"}
HALTS = ("HALT", "FORCED", "DAILY_LOSS")
#: the two engines the pooled record knows by name: phase 34 = A (the contrasts' "x"), phase 35 = B ("y")
ENGINE_A, ENGINE_B = "phase 34", "phase 35"
SHADOW_VALUES = frozenset(k.value for k in SHADOW_BOOKS)
MIRROR_OF_RE = re.compile(r"stop-rule mirror of (pos-[0-9a-f]+)")
EVIDENCE_FILE = "peer_evidence.json"


# -- small pieces ------------------------------------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _map(v: Any) -> Mapping[str, Any]:
    """``v`` when it is a mapping, else an empty one: a stored record's optional parts."""
    return v if isinstance(v, Mapping) else {}


def inr(v: float | None, sign: bool = False) -> str:
    """Rupees the Indian way, rounded to the rupee: ₹1,23,456; "−" for a loss, "+" when ``sign``."""
    if v is None:
        return "—"
    s = str(round(abs(v)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        grp: list[str] = []
        while len(head) > 2:
            grp.insert(0, head[-2:])
            head = head[:-2]
        if head:
            grp.insert(0, head)
        s = ",".join(grp) + "," + tail
    neg = v < 0 and s != "0"
    return ("−" if neg else ("+" if sign and s != "0" else "")) + "₹" + s


def hms(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S") if ts else "—"


def px(v: float | None) -> str:
    return "—" if v is None else (f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 1e7 else f"{v:.0f}")


def pct(a: float | None, b: float | None) -> str:
    return "—" if (a is None or not b) else f"{(a / b - 1) * 100:+.1f} %"


def short_book(base: str, rule: str) -> str:
    """RT-Y, RT-Y stop-E, RT-Y stop-A: the study's book labels."""
    return SHORT.get(base, base) + {"current": "", "E": " stop-E", "A": " stop-A"}.get(rule, "")


def refusal_kind(text: str) -> str:
    """What a refused exit was for, from the reason the engine gave it."""
    t = str(text)
    if "force-flat" in t:
        return "EOD"
    if t.startswith("halted"):
        return "HALT"
    if "daily loss" in t:
        return "DAILY_LOSS"
    if t.startswith(("stock", "underlying")):
        return "SL-EQ"
    if t.startswith("option"):
        return "SL-OP"
    if t.startswith("hard SL"):
        return "TRAIL"
    return "OTHER"


STALE_RE = re.compile(r"book for .+? is (\d+(?:\.\d+)?) ms old(?: \(limit (\d+(?:\.\d+)?)\))?")


def _age(text: str) -> tuple[float | None, float | None]:
    m = STALE_RE.search(text or "")
    return (float(m[1]), float(m[2]) if m[2] else None) if m else (None, None)


def mechanism(reason: str, trig_on: str | None, level: float | None, entry: float, sl0: float | None, cap: float | None,
              rule: str) -> str:
    """The final leg's exit mechanism in words, from its stop record (card_spec.md §3)."""
    if reason in HALTS:
        return "halt/forced"
    if reason == "EOD":
        return "day-end 15:20"
    if reason == "TARGET":
        return "last target rung"
    if reason == "TRAIL":
        return "trail / give-back"
    if reason == "SL-EQ":
        return "stock stop"
    if reason == "SL-OP":
        if level is not None and entry and level >= entry - 1e-9:
            return "raised stop (breakeven / rung / trail line)"
        if trig_on == "option bid":
            return "premium cap (25 % on the bid)"
        if level is not None and entry and cap:
            if abs(level - entry * (1 - cap / 100)) <= max(0.06, entry * 0.004):
                return f"capped option stop ({cap:.0f} % below paid)"
        if rule != "current" and level is not None and sl0 is not None and level > sl0 + 1e-9:
            return "raised stop (breakeven / rung / trail line)"
        return "option stop"
    return reason or "—"


def mech_class(m: str) -> str:
    if m.startswith("raised"):
        return "RAISED"
    if m.startswith(("trail", "last target")):
        return "TRAIL"
    if m.startswith("day-end"):
        return "EOD"
    if m.startswith("halt"):
        return "HALT"
    return "STOP"


# -- the books' parameters ---------------------------------------------------------------------------------------------

#: (field, label, how to say a value), in card_spec.md §5.3's order, then the ones the exit lines cite
PARAMS: tuple[tuple[str, str, Callable[[Any], str]], ...] = (
    ("max_premium_loss_pct", "premium cap", lambda v: "none" if v is None else f"{v:g} % below paid"),
    ("equity_stop_buffer_pct", "stock stop buffer", lambda v: "plan's stop" if v is None else f"plan's stop + {v:g} %"),
    ("stop_mode", "stop judged on", lambda v: "the option (sustained)" if v == "option" else "the stock"),
    ("arm_at_pct", "arms at", lambda v: "first rung touch" if v is None else f"+{v:g} % on the premium"),
    ("arm_mode", "arming", lambda v: str(v)),
    ("ladder_mode", "ladder", lambda v: {"mtf": "the option's own daily+weekly rungs", "daily_r": "the option's daily R1-R4"}.get(v, str(v))),
    ("own_ladder", "own ladder", lambda v: "yes" if v else "no (equity targets via delta)"),
    ("peak_giveback_pct", "give-back from the peak", lambda v: "none (40 % trail after +3 %)" if v is None else f"{v:g} %"),
    ("sustain_s", "option-stop sustain", lambda v: "one read" if v is None else f"{v:g} s"),
    ("time_stop_bars", "time stop", lambda v: "none" if v is None else f"{v:g} × 30m bars"),
    ("min_stop_ticks", "min stop distance", lambda v: f"{v:g} ticks"),
    ("band_exit", "give-back exit", lambda v: str(v)),
    ("post_arm_sustain", "rung stop sustained", lambda v: "yes" if v else "no"),
    ("sl_lag", "rung stop lags one rung", lambda v: "yes" if v else "no"),
    ("eq_stop_area_pct_s", "stock-stop confirm", lambda v: "fixed 60 s" if v in (None, "inf") else f"{v:g} %·s or 60 s"),
    ("trail_dwell_samples", "give-back reads", lambda v: f"{v:g} in a row"),
    ("trail_arm_pct", "FUDKII trail arms at", lambda v: f"+{v:g} %"),
    ("trail_giveback_pct", "FUDKII trail gives back", lambda v: f"{v:g} % of the gain"),
    ("breakeven_after_t1", "breakeven after T1", lambda v: "yes" if v else "no"),
    ("priced_option_stop", "option stop priced at the stock's stop", lambda v: "yes" if v else "no"),
)
#: the parameters behind each exit mechanism: when that mechanism decided a pair, these are the ones that differ
MECH_PARAMS = {
    "RAISED": ("arm_at_pct", "arm_mode", "sl_lag", "post_arm_sustain", "band_exit", "trail_arm_pct", "trail_giveback_pct",
               "breakeven_after_t1"),
    "TRAIL": ("peak_giveback_pct", "trail_dwell_samples", "band_exit", "trail_giveback_pct", "ladder_mode", "own_ladder"),
    "STOP": ("max_premium_loss_pct", "sustain_s", "stop_mode", "eq_stop_area_pct_s", "equity_stop_buffer_pct",
             "priced_option_stop", "min_stop_ticks"),
}


def params_json(limits: Any) -> dict[str, Any]:
    """A book's limits as the card cites them: JSON-safe (an infinity is "inf")."""
    if limits is None or not is_dataclass(limits):
        return {}
    have = {f.name for f in fields(limits)}
    out: dict[str, Any] = {}
    for k, _lab, _say in PARAMS:
        if k not in have:
            continue
        v = getattr(limits, k)
        out[k] = "inf" if isinstance(v, float) and math.isinf(v) else v
    return out


def param_diffs(pa: Mapping[str, Any], pb: Mapping[str, Any], keys: Iterable[str] | None = None) -> list[dict[str, str]]:
    """Each parameter whose value differs between two books: "label: this → peer"."""
    want = set(keys) if keys is not None else None
    out = []
    for k, lab, say in PARAMS:
        if want is not None and k not in want:
            continue
        if k not in pa and k not in pb:
            continue
        va, vb = pa.get(k), pb.get(k)
        if va != vb:
            out.append({"key": k, "label": lab, "this": say(va), "peer": say(vb)})
    return out


# -- one trade, as its peers see it ------------------------------------------------------------------------------------


def _leg(x: Mapping[str, Any], closed_ts: float) -> dict[str, Any]:
    st = _map(x.get("stop"))
    fill = _num(x.get("fillPrice"))
    return {"rung": x.get("kind") == "target", "reason": x.get("reason"), "qty": _num(x.get("qty")) or 0.0, "fill": fill,
            "ref": _num(x.get("ref")) if _num(x.get("ref")) is not None else fill, "ts": _num(x.get("filledTs")) or closed_ts,
            "why": x.get("why"), "level": _num(st.get("level")), "triggerOn": st.get("triggerOn"),
            "triggerPx": _num(st.get("triggerPrice")), "triggerTs": _num(st.get("triggerTs")), "executable": _num(st.get("executable")),
            "bid": _num(st.get("bidAtTrigger"))}


def refusals_of(orders: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Position id → its exits REFUSED by the paper matcher (orders: purpose EXIT, status REJECTED), oldest first, each with
    the moment, the price it would have sold at and the book's age."""
    out: dict[str, list[dict[str, Any]]] = {}
    for o in orders:
        if not isinstance(o, Mapping) or o.get("purpose") != "EXIT" or o.get("status") != "REJECTED":
            continue
        pid = str(o.get("position_id") or "")
        if not pid:
            continue
        ex = _map(o.get("exec"))
        st = _map(ex.get("stop"))
        bap = _map(ex.get("bookAtPlace"))
        age, limit = _age(str(ex.get("outcome") or ""))
        if age is None:
            age, limit = _age(str(o.get("note") or ""))
        executable = next((v for v in (_num(st.get("executable")), _num(bap.get("bid")), _num(ex.get("ref")), _num(ex.get("limit")))
                           if v is not None), None)
        t = _num(st.get("triggerTs")) or _num(ex.get("signalTs")) or _num(o.get("ts")) or 0.0
        out.setdefault(pid, []).append({"ts": t, "kind": refusal_kind(str(o.get("reason") or "")), "text": str(o.get("reason") or ""),
                                        "executable": executable, "ref": _num(ex.get("ref")), "ageMs": age, "limitMs": limit})
    for v in out.values():
        v.sort(key=lambda r: r["ts"])
    return out


def _anatomy(rec: dict[str, Any], legs: list[dict[str, Any]], refused: list[dict[str, Any]]) -> dict[str, Any]:
    """The exit anatomy on the counterfactual legs (the module's docstring); refusals with no price are counted only."""
    q, closed_ts = rec["q"], rec["closedTs"] or 0.0
    cf, rho, first = legs, 0.0, None
    priced = [r for r in refused if r["executable"] is not None]
    if priced:
        f0 = priced[0]
        after = [lg for lg in legs if lg["ts"] >= f0["ts"] - 1e-3]
        if after:
            first = f0
            qa = sum(lg["qty"] for lg in after)
            rho = sum(lg["qty"] * (lg["fill"] - f0["executable"]) for lg in after) / q
            cf = [lg for lg in legs if lg["ts"] < f0["ts"] - 1e-3] + [
                {"rung": False, "reason": f0["kind"], "qty": qa, "fill": f0["executable"],
                 "ref": f0["ref"] if f0["ref"] is not None else f0["executable"], "ts": f0["ts"]}]
    fin = [lg for lg in cf if not lg["rung"]] or [cf[-1]]
    fq = sum(lg["qty"] for lg in fin)
    F = sum(lg["qty"] * lg["fill"] for lg in fin) / fq
    D = sum(lg["qty"] * lg["ref"] for lg in fin) / fq
    rungs = [lg for lg in cf if lg["rung"] and lg is not fin[0]]
    T = sum(lg["qty"] * (lg["fill"] - F) for lg in rungs) / q
    actual_fin = [lg for lg in legs if not lg["rung"]] or [legs[-1]]
    lf = actual_fin[-1]
    ins_rungs = [lg for lg in legs if lg["rung"] and lg is not lf]
    p = rec["position"]
    level = lf["level"] if lf["level"] is not None else p["optionSlFinal"]
    mech = mechanism(str(rec["exitReason"] or ""), lf["triggerOn"], level, rec["e"], p["optionSlInitial"],
                     _num(rec["params"].get("max_premium_loss_pct")), rec["rule"])
    return {
        "F": F, "D": D, "X": F - D, "T": T, "rho": rho, "xCf": F + T, "rs": sum(lg["qty"] for lg in rungs) / q,
        "rungs": [{"price": lg["fill"], "qty": lg["qty"], "ts": lg["ts"]} for lg in ins_rungs],
        "rungShare": sum(lg["qty"] for lg in ins_rungs) / q,
        "final": {"reason": rec["exitReason"], "ts": lf["ts"] or closed_ts, "fill": lf["fill"], "ref": lf["ref"], "level": lf["level"],
                  "triggerOn": lf["triggerOn"], "triggerPx": lf["triggerPx"], "why": lf["why"]},
        "mech": mech,
        "refused": None if first is None else {**first, "qty": sum(lg["qty"] for lg in legs if lg["ts"] >= first["ts"] - 1e-3)},
        "nRefused": len(refused), "nRefusedUnpriced": len(refused) - len(priced),
    }


def record(pos: Mapping[str, Any], trade: Mapping[str, Any] | None, refused: list[dict[str, Any]], *, engine: str,
           params: Mapping[str, Any], now: float) -> dict[str, Any] | None:
    """One position (any book: trading, shadow, stop-rule mirror), as a peer card reads it. None for a position the record
    cannot price (no qty or no entry) and for one closed without a trade (24 Sep: positions opened and closed in the same
    66 ms by the halt cascade left no trade row): there is nothing to compare."""
    if pos.get("status") == "CLOSED" and trade is None:
        return None
    ins = _map(pos.get("instrument"))
    book = str(pos.get("strategy") or "")
    base, rule = stop_rule_of(book)
    closed = pos.get("status") == "CLOSED" and trade is not None
    q = _num((trade or {}).get("qty")) or _num(pos.get("qty")) or 0.0
    e = _num((trade or {}).get("entry")) or _num(pos.get("entry")) or 0.0
    if q <= 0 or e <= 0:
        return None
    lot = _num(ins.get("lot_size")) or 1.0
    el = _map(pos.get("exec_log"))
    en = _map(el.get("entry"))
    bap = _map(en.get("bookAtPlace"))
    m = MIRROR_OF_RE.search(str(pos.get("note") or ""))
    r_en = _num(en.get("ref"))
    rec: dict[str, Any] = {
        "engine": engine, "positionId": str(pos.get("id") or ""), "tradeId": str((trade or {}).get("id") or "") or None,
        "book": book, "baseBook": base, "rule": rule, "cls": "mirror" if rule != "current" else ("shadow" if book in SHADOW_VALUES else "trading"),
        "label": short_book(base, rule), "scripCode": str(ins.get("scrip_code") or pos.get("scrip_code") or ""),
        "contract": ins.get("name") or pos.get("symbol"), "symbol": pos.get("symbol"), "direction": pos.get("direction"),
        "signalId": pos.get("signal_id"), "grade": pos.get("grade"), "mirrorOf": m[1] if m else None,
        "status": "CLOSED" if closed else "OPEN", "openedTs": _num(pos.get("opened_ts")) or 0.0,
        "closedTs": _num((trade or {}).get("closed_ts")) or _num(pos.get("closed_ts")) if closed else None,
        "q": q, "lots": q / lot, "lotSize": lot, "e": e, "rEn": r_en if r_en is not None else e, "enAudit": r_en is not None,
        "c": _num((trade or {}).get("charges")) if closed else None, "net": _num((trade or {}).get("net")) if closed else None,
        "gross": _num((trade or {}).get("gross")) if closed else None, "exitReason": (trade or {}).get("exit_reason") if closed else None,
        "entryAudit": None if not en else {
            "ref": r_en, "fill": _num(en.get("fillPrice")), "signalTs": _num(en.get("signalTs")), "filledTs": _num(en.get("filledTs")),
            "waitS": _num(en.get("waitS")), "reprices": len(en.get("reprices") or []), "limit": _num(en.get("limit")),
            "bid": _num(bap.get("bid")), "ask": _num(bap.get("ask")), "outcome": en.get("outcome")},
        "position": {
            "optionSlInitial": _num(pos.get("initial_option_sl")), "optionSlFinal": _num(pos.get("option_sl")),
            "optionT1": _num(pos.get("option_t1")), "optionTargets": [t for t in (_num(v) for v in pos.get("option_targets") or ()) if t is not None],
            "armedBy": pos.get("armed_by"), "armedTs": _num(pos.get("armed_ts")), "peakMid": _num(pos.get("peak_mid")),
            "ratchetSl": _num(pos.get("ratchet_sl")), "trailDwell": _num(pos.get("trail_dwell")), "equitySl": _num(pos.get("equity_sl")),
            "equityEntry": _num(pos.get("equity_entry")), "targetsHit": _num(pos.get("targets_hit")), "mfeR": _num(pos.get("mfe_r")),
            "maeR": _num(pos.get("mae_r")), "rUnit": _num(pos.get("r_unit"))},
        "params": dict(params), "refusals": refused, "audit": False, "anat": None, "x": None,
    }
    if not closed:
        return rec
    gross = rec["gross"] if rec["gross"] is not None else ((_num((trade or {}).get("exit")) or e) - e) * q
    x = e + gross / q
    rec["x"] = x
    if rec["c"] is None:
        rec["c"] = 0.0
    if rec["net"] is None:
        rec["net"] = gross - rec["c"]
    closed_ts = rec["closedTs"] or 0.0
    legs = sorted((_leg(lg, closed_ts) for lg in el.get("exits") or () if isinstance(lg, Mapping)), key=lambda lg: lg["ts"])
    legs = [lg for lg in legs if lg["fill"] is not None and lg["qty"] > 0]
    audit = bool(legs) and abs(sum(lg["qty"] for lg in legs) - q) < 0.5 and abs(sum(lg["qty"] * lg["fill"] for lg in legs) / q - x) < 0.006
    if not audit:
        legs = [{"rung": False, "reason": rec["exitReason"], "qty": q, "fill": x, "ref": x, "ts": closed_ts, "why": None, "level": None,
                 "triggerOn": None, "triggerPx": None, "triggerTs": None, "executable": None, "bid": None}]
    rec["audit"] = audit
    a = _anatomy(rec, legs, refused)
    # the identity the decomposition rests on; a log that cannot hold it is read as having no audit
    if abs(a["xCf"] + a["rho"] - x) > 1e-6:
        legs = [{**legs[-1], "rung": False, "qty": q, "fill": x, "ref": x, "ts": closed_ts}]
        rec["audit"] = False
        a = _anatomy(rec, legs, refused)
    rec["anat"] = a
    return rec


def build_records(positions: Sequence[Mapping[str, Any]], trades: Sequence[Mapping[str, Any]], orders: Iterable[Mapping[str, Any]], *,
                  engine: str, params_of: Callable[[str], Mapping[str, Any]], now: float) -> list[dict[str, Any]]:
    """Every position of the window as a record: the closed ones with their exit anatomy, the open ones as they stand."""
    trade_of = {str(t.get("position_id") or ""): t for t in trades}
    refused = refusals_of(orders)
    out = []
    for p in positions:
        pid = str(p.get("id") or "")
        r = record(p, trade_of.get(pid), refused.get(pid, []), engine=engine, params=params_of(str(p.get("strategy") or "")), now=now)
        if r is not None:
            out.append(r)
    return out


# -- the pair ----------------------------------------------------------------------------------------------------------


def overlaps(a: Mapping[str, Any], b: Mapping[str, Any], now: float) -> bool:
    """Both held the option at the same time: a.opened < b.closed and b.opened < a.closed (an open one is held to now)."""
    a1, b1 = a["closedTs"] or now, b["closedTs"] or now
    return bool(a["openedTs"] < b1 and b["openedTs"] < a1)


def peers_of(t: Mapping[str, Any], records: Iterable[Mapping[str, Any]], now: float) -> list[Mapping[str, Any]]:
    """Every other trade on the SAME option contract (the same scrip code) whose holding time overlapped this one's."""
    me = (t["engine"], t["positionId"])
    return [r for r in records if r["scripCode"] == t["scripCode"] and t["scripCode"] and (r["engine"], r["positionId"]) != me
            and overlaps(t, r, now)]


def decompose(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, float]:
    """net_b − net_a in its nine parts (the module's docstring); they sum to it exactly."""
    qa, qb = a["q"], b["q"]
    qbar = (qa + qb) / 2
    pa, pb = a["x"] - a["e"], b["x"] - b["e"]
    A, B = a["anat"], b["anat"]
    w = 1.0 - min(A["rs"], B["rs"])
    exit_rule, exit_exec = qbar * w * (B["D"] - A["D"]), qbar * w * (B["X"] - A["X"])
    return {"size": (qb - qa) * (pa + pb) / 2, "entry_time": -qbar * (b["rEn"] - a["rEn"]),
            "entry_paid": -qbar * ((b["e"] - b["rEn"]) - (a["e"] - a["rEn"])),
            "ladder": qbar * (B["xCf"] - A["xCf"]) - exit_rule - exit_exec, "exit_rule": exit_rule, "exit_exec": exit_exec,
            "refusal": qbar * (B["rho"] - A["rho"]), "charges": -(b["c"] - a["c"]), "stop_rule": 0.0}


def source_key(r: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """(engine, the position whose fill it is): a mirror's source (its note, else the same book, trigger and open second),
    any other trade itself."""
    if r["mirrorOf"]:
        return (r["engine"], r["mirrorOf"])
    if r["rule"] != "current":
        for s in records:
            if (s["engine"] == r["engine"] and s["rule"] == "current" and s["book"] == r["baseBook"] and s["signalId"] == r["signalId"]
                    and abs(s["openedTs"] - r["openedTs"]) < 1.0):
                return (r["engine"], s["positionId"])
    return (r["engine"], r["positionId"])


def relation(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    same_fill = a["engine"] == b["engine"] and abs(a["e"] - b["e"]) < 1e-9 and a["q"] == b["q"] and abs(a["openedTs"] - b["openedTs"]) < 1.0
    if a["engine"] == b["engine"] and a["baseBook"] == b["baseBook"]:
        return "stop-rule sibling (same fill)"
    if same_fill:
        return "same fill, other book"
    if a["engine"] == b["engine"]:
        return "same engine, own fills"
    if a["baseBook"] == b["baseBook"] and a["rule"] == b["rule"]:
        return "other engine, same book"
    return "other engine, other book"


def pair_parts(a: Mapping[str, Any], b: Mapping[str, Any], sib: Mapping[tuple[str, str], Mapping[str, Mapping[str, Any]]],
               src: Mapping[tuple[str, str], tuple[str, str]]) -> tuple[dict[str, float], Mapping[str, Any] | None, str]:
    """The parts of net_b − net_a with the stop rule split off through a's own sibling under b's rule, that sibling (or
    None), and a note when the stop rule could not be separated."""
    ka, kb = src[(a["engine"], a["positionId"])], src[(b["engine"], b["positionId"])]
    if ka == kb:  # siblings: the whole exit difference is the stop rule
        p = decompose(a, b)
        p["stop_rule"] = p["ladder"] + p["exit_rule"] + p["exit_exec"]
        p.update(ladder=0.0, exit_rule=0.0, exit_exec=0.0)
        return p, None, ""
    if a["rule"] != b["rule"]:
        a2 = sib.get(ka, {}).get(b["rule"])
        if a2 is not None and a2.get("anat") is not None:
            p1, p2 = decompose(a, a2), decompose(a2, b)
            p = {k: p1[k] + p2[k] for k in p1}
            p["stop_rule"] = p1["ladder"] + p1["exit_rule"] + p1["exit_exec"]
            for k in ("ladder", "exit_rule", "exit_exec"):
                p[k] -= p1[k]
            return p, a2, ""
        return decompose(a, b), None, "stop rule not separable: this fill has no sibling under the peer's rule"
    return decompose(a, b), None, ""


def whole_rupees(parts: Mapping[str, float], total: float) -> dict[str, int]:
    """Each part to the rupee so that they still add up to the gap to the rupee (largest remainder)."""
    target = round(total)
    floor = {k: math.floor(v) for k, v in parts.items()}
    short = target - sum(floor.values())
    order = sorted(parts, key=lambda k: -(parts[k] - floor[k]))
    if short >= 0:
        for k in order[:short]:
            floor[k] += 1
    else:  # cannot happen for a true sum; kept so a rounding quirk never breaks the page
        for k in order[::-1][: -short]:
            floor[k] -= 1
    return floor


# -- the words ---------------------------------------------------------------------------------------------------------


def _who(r: Mapping[str, Any]) -> str:
    return f"{r['engine'] or 'this engine'}'s {r['label']}"


def _exit_side(r: Mapping[str, Any]) -> dict[str, Any]:
    """One side's exit, as the exit lines cite it: the rule that fired, when and where, the stop levels and the rungs."""
    a, p = r["anat"], r["position"]
    ref = a["refused"]
    return {
        "who": _who(r), "mech": a["mech"], "reason": a["final"]["reason"], "ts": a["final"]["ts"], "decision": a["final"]["ref"],
        "fill": a["final"]["fill"], "avgExit": r["x"], "level": a["final"]["level"], "triggerOn": a["final"]["triggerOn"],
        "triggerPx": a["final"]["triggerPx"], "optionSlInitial": p["optionSlInitial"], "optionSlFinal": p["optionSlFinal"],
        "armedBy": p["armedBy"], "armedMin": round((p["armedTs"] - r["openedTs"]) / 60, 1) if p["armedTs"] else None,
        "peakMid": p["peakMid"], "trailDwell": p["trailDwell"], "equitySl": p["equitySl"],
        "rungs": a["rungs"], "rungShare": a["rungShare"], "heldMin": round(((r["closedTs"] or 0) - r["openedTs"]) / 60, 1),
        "refused": None if ref is None else {"ts": ref["ts"], "kind": ref["kind"], "executable": ref["executable"], "ageMs": ref["ageMs"],
                                             "limitMs": ref["limitMs"], "text": ref["text"]},
        "audit": r["audit"],
    }


def _exit_label(z: Mapping[str, Any]) -> tuple[str, float | None, float]:
    ref = z["anat"]["refused"]
    if ref is not None:
        return f"a {ref['kind']} attempt refused on a stale book", ref["ts"], z["anat"]["D"]
    return z["anat"]["mech"], z["anat"]["final"]["ts"], z["anat"]["D"]


def part_line(k: str, v: float, t: Mapping[str, Any], p: Mapping[str, Any], chain: Mapping[str, Any] | None) -> str:
    """One part in words (card_spec.md §5.2), every number from the two records."""
    a, b = "this trade", _who(p)
    if chain is not None and k in ("exit_rule", "exit_exec", "ladder"):
        t, a = chain, f"this fill under {b}'s stop rule ({_who(chain)})"
    A, B = t["anat"], p["anat"]
    w = 1.0 - min(A["rs"], B["rs"])
    units = round((t["q"] + p["q"]) / 2 * w)
    if k == "exit_rule":
        lt, tt, dt_ = _exit_label(t)
        lp, tp, dp = _exit_label(p)
        return (f"exit decision {inr(v, True)}: {a} left on {lt} at {hms(tt)} (decision price {px(dt_)}), {b} on {lp} at {hms(tp)} "
                f"(decision price {px(dp)}); {px(dp - dt_)} a unit on the {units:,} units not sold at a rung by both")
    if k == "exit_exec":
        return (f"exit fills {inr(v, True)}: fill minus decision price was {px(A['X'])} here and {px(B['X'])} for {b}, per unit, "
                f"on the same {units:,} units")
    if k == "ladder":
        def rungs(z: Mapping[str, Any]) -> str:
            rs = z["anat"]["rungs"]
            return (f"{len(rs)} rung(s) at {', '.join(px(r['price']) for r in rs)} ({z['anat']['rungShare'] * 100:.0f} % of its qty)" if rs
                    else "no rung")
        return f"target rungs {inr(v, True)}: {b} sold {rungs(p)}; {a} sold {rungs(t)}"
    if k == "stop_rule":
        tail = (f". This same fill under {b}'s rule ({_who(chain)}) left on {chain['anat']['mech']} at {hms(chain['anat']['final']['ts'])}"
                if chain is not None else "")
        return f"stop rule {inr(v, True)}: this trade ran the {RULE_NAME[t['rule']]} and {b} the {RULE_NAME[p['rule']]}{tail}"
    if k == "refusal":
        sides = [(z, who) for z, who in ((t, a), (p, b)) if z["anat"]["refused"] is not None]
        bits = []
        for z, who in sides:
            r_ = z["anat"]["refused"]
            age = f"book {r_['ageMs'] / 1000:.1f} s old" + (f", limit {r_['limitMs'] / 1000:g} s" if r_["limitMs"] else "") if r_["ageMs"] else "a stale book"
            bits.append(f"{who}'s {r_['kind']} exit was refused at {hms(r_['ts'])} ({age}; \"{r_['text'][:70]}\"; executable then "
                        f"{px(r_['executable'])}). It got out at {hms(z['anat']['final']['ts'])} on {z['anat']['mech']} at {px(z['anat']['final']['fill'])}")
        return f"stale-book refusal {inr(v, True)} (MODELLED): " + "; ".join(bits)
    if k == "entry_paid":
        def ent(z: Mapping[str, Any]) -> str:
            ea = z["entryAudit"] or {}
            extra = []
            if ea.get("waitS") is not None:
                extra.append(f"filled {ea['waitS']:.0f} s after the signal")
            if ea.get("reprices"):
                extra.append(f"{ea['reprices']} re-price(s)")
            return f"paid {px(z['e'])} against the signal price {px(z['rEn'])} ({px(z['e'] - z['rEn'])} over)" + (f", {', '.join(extra)}" if extra else "")
        return f"entry fill {inr(v, True)}: {ent(t)} here; {b} {ent(p)}"
    if k == "entry_time":
        ta, tb = (t["entryAudit"] or {}).get("signalTs"), (p["entryAudit"] or {}).get("signalTs")
        return (f"entry timing {inr(v, True)}: the option's signal price was {px(t['rEn'])} here (signal {hms(ta)}) vs {px(p['rEn'])} "
                f"for {b} (signal {hms(tb)})")
    if k == "size":
        per_lot = ((t["x"] - t["e"]) + (p["x"] - p["e"])) / 2 * ((t["lotSize"] + p["lotSize"]) / 2)
        return (f"size {inr(v, True)}: {t['lots']:g} lots here vs {p['lots']:g}; the extra {p['lots'] - t['lots']:+g} lots at "
                f"{inr(per_lot, True)} a lot (the two trades' average outcome)")
    if k == "charges":
        return f"charges {inr(v, True)}: {inr(t['c'])} here vs {inr(p['c'])}"
    return f"{k} {inr(v, True)}"


# -- the lesson ---------------------------------------------------------------------------------------------------------

GRADE_SYSTEMATIC = "systematic (held across days and both engines)"
GRADE_SUGGESTIVE = "suggestive"
GRADE_ONE_OFF = "one-off / paper artefact"


def grade_of(strength: str) -> str:
    """The study's strength label (card_spec.md §5.4) as the operator grades a lesson."""
    if strength.startswith("consistent: both"):
        return GRADE_SYSTEMATIC
    if strength.startswith(("consistent in", "suggestive")):
        return GRADE_SUGGESTIVE
    return GRADE_ONE_OFF


def _sh(a: float, b: float) -> float:
    return a / (a + b) if (a + b) else 0.0


def strength(n: int, s: float, n_in: int, s_in: float, n_out: int, s_out: float, s_a: float | None, s_b: float | None,
             windows: Mapping[str, str], n_min: int = 15) -> str:
    """The deterministic "how sure" (card_spec.md §5.4); s = the share of the pooled, non-tied cases pointing the card's way."""
    if n < 5:
        return "anecdote (fewer than 5 cases on record)"
    if s >= 0.7:
        both = n_in >= 3 and n_out >= 3 and s_in >= 0.5 and s_out >= 0.5
        eng = (s_a is None or s_a >= 0.5) and (s_b is None or s_b >= 0.5)
        if n >= n_min and both and eng:
            return "consistent: both windows and both engines"
        if n >= n_min:
            w = windows.get("OUT", "OUT") if (n_out >= 3 and s_out >= 0.5) else windows.get("IN", "IN")
            return f"consistent in {w} only; the other window disagrees or is too thin"
        return f"suggestive ({n} cases, under 15)"
    if s <= 0.3:
        return "this trade is the exception: the pooled record points the other way"
    return "mixed: the pooled record splits"


def _holm(row: Mapping[str, Any]) -> str:
    ph = _num(row.get("p_holm"))
    tried = EVIDENCE.get("contrastsTried")
    if ph is None:
        return ""
    return (f"; not significant once every test is counted ({tried} contrasts, Holm p {ph:.2f})" if ph >= 0.05
            else f"; significant after counting {tried} contrasts (Holm p {ph:.2f})")


def contrast_evidence(ev: Mapping[str, Any], con: str, card_says_y: bool) -> dict[str, Any]:
    """A pooled contrast row ("RT-X -> RT-Y") read the card's way: its sentence, strength and count."""
    S = ev.get("contrasts") or {}
    row = S.get(con)
    if row is None:
        x, _, y = con.partition(" -> ")
        rev = f"{y} -> {x}"
        if rev not in S:
            return {"text": "no pooled record for this pair", "strength": "anecdote (fewer than 5 cases on record)", "n": 0,
                    "grade": GRADE_ONE_OFF, "contrast": con}
        con, card_says_y, row = rev, not card_says_y, S[rev]
    uy, ux = int(row["units_y_better"] or 0), int(row["units_x_better"] or 0)
    yi, xi, yo, xo = (int(row[k] or 0) for k in ("y_better_IN", "x_better_IN", "y_better_OUT", "x_better_OUT"))
    s = _sh(uy, ux) if card_says_y else _sh(ux, uy)
    si = _sh(yi, xi) if card_says_y else _sh(xi, yi)
    so = _sh(yo, xo) if card_says_y else _sh(xo, yo)
    engine_family = str(row.get("family") or "").startswith("engine")
    sum_a, sum_b = _num(row.get("sum_A")) or 0.0, _num(row.get("sum_B")) or 0.0
    sa = None if abs(sum_a) < 1 or engine_family else (1.0 if (sum_a > 0) == card_says_y else 0.0)
    sb = None if abs(sum_b) < 1 or engine_family else (1.0 if (sum_b > 0) == card_says_y else 0.0)
    st = strength(uy + ux, s, yi + xi, si, yo + xo, so, sa, sb, ev.get("windows") or {})
    x, _, y = con.partition(" -> ")
    if engine_family:
        x, y = f"{ENGINE_A}'s {x}", f"{ENGINE_B}'s {y}"
    eng = "" if abs(sum_a) < 1 and abs(sum_b) < 1 else f"; {ENGINE_A} {inr(sum_a, True)}, {ENGINE_B} {inr(sum_b, True)}"
    win = ev.get("windows") or {}
    text = (f"over {int(row['n_units'])} option-triggers both held ({int(row['n_obs'])} engine copies, {int(row['n_days'])} sessions), "
            f"{y} did better on {uy}, {x} on {ux}, and the rest were equal. {y} minus {x}: {inr(row['sum_value'], True)} "
            f"({win.get('IN', 'IN')} {inr(row['sum_IN'], True)}, {win.get('OUT', 'OUT')} {inr(row['sum_OUT'], True)}{eng})"
            + _holm(row))
    return {"text": text, "strength": st, "n": uy + ux, "grade": grade_of(st), "contrast": con}


def first_out_evidence(ev: Mapping[str, Any], family: str, book: str, mech: str, card_says_hold: bool) -> dict[str, Any]:
    """The pooled first-out row for this mechanism: how often holding past it paid."""
    row = next((r for r in ev.get("firstOut") or () if r["family"] == family and r["book"] == book and r["mech"] == mech), None)
    if row is None:
        return {"text": "no other case of this rule firing first on a shared fill", "strength": "anecdote (fewer than 5 cases on record)",
                "n": 0, "grade": GRADE_ONE_OFF}
    g = {k: int(row[k] or 0) for k in ("holding_better_n", "early_better_n", "holding_better_IN", "early_better_IN", "holding_better_OUT",
                                        "early_better_OUT", "holding_better_A", "early_better_A", "holding_better_B", "early_better_B")}
    hb, eb = g["holding_better_n"], g["early_better_n"]
    hi, ei, ho, eo = g["holding_better_IN"], g["early_better_IN"], g["holding_better_OUT"], g["early_better_OUT"]
    ha, ea, hB, eB = g["holding_better_A"], g["early_better_A"], g["holding_better_B"], g["early_better_B"]
    if card_says_hold:
        s, si, so = _sh(hb, eb), _sh(hi, ei), _sh(ho, eo)
        sa, sb = (_sh(ha, ea) if ha + ea else None), (_sh(hB, eB) if hB + eB else None)
    else:
        s, si, so = _sh(eb, hb), _sh(ei, hi), _sh(eo, ho)
        sa, sb = (_sh(ea, ha) if ha + ea else None), (_sh(eB, hB) if hB + eB else None)
    st = strength(hb + eb, s, hi + ei, si, ho + eo, so, sa, sb, ev.get("windows") or {})
    who = SHORT.get(book, book) + ("" if family == "current" else " stop-E")
    win = ev.get("windows") or {}
    text = (f"{who}'s {mech} fired first while a sibling book on the same fill held {int(row['early_exits'])} times "
            f"({int(row['units'])} option-triggers, {int(row['days'])} sessions). The holders did better {hb} times and the early exit {eb} times "
            f"({win.get('IN', 'IN')} {hi}/{ei}, {win.get('OUT', 'OUT')} {ho}/{eo}; {ENGINE_A} {ha}/{ea}, {ENGINE_B} {hB}/{eB}). In total the holders made "
            f"{inr(row['held_sum'], True)} over the early exit. Those early exits came a median {(_num(row['median_exit_min']) or 0):.0f} min after "
            f"the fill, at {(_num(row['median_exit_vs_paid_pct']) or 0):+.1f} % of the premium paid")
    return {"text": text, "strength": st, "n": hb + eb, "grade": grade_of(st)}


def _raise_rule(book: str) -> str:
    return {"FUDKII": "FUDKII's trail arms at +3 % and gives back 40 % of the gain on one read, and its stop goes to breakeven after T1",
            "FUDKII_RT_X": "RT-X arms on the first touch of its own first rung, steps the stop to breakeven there and leaves the band on one read",
            "FUDKII_RT_N": "RT-N arms at once (stock T1 touch or its own R1 1-minute close) with a 2 % give-back"}.get(book, "")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _no_lesson(key: str, why: str, strength: str, text: str) -> dict[str, Any]:
    return {"key": key, "grade": GRADE_ONE_OFF, "gradeWhy": why, "strength": strength, "decided": None, "systematic": None,
            "params": [], "evidence": [], "text": text}


def lesson(t: Mapping[str, Any], p: Mapping[str, Any], parts: Mapping[str, float], gap: float, ev: Mapping[str, Any]) -> dict[str, Any]:
    """The first rule that holds (card_spec.md §5.4), in two lines: what decided THIS pair (the mechanism that fired first, or
    the stop rule, or the engine), graded on its own pooled record, and — only when the pooled record holds it across both
    windows and both engines — the systematic parameter difference between the two books, with every parameter in it. The
    headline ``grade`` is the stronger of the two; each line keeps its own. A refusal or a halt is never a lesson."""
    refs = ev.get("refusals") or {}
    if parts["refusal"] >= 0.5 * gap:
        return _no_lesson("artefact_refusal", "paper artefact: a stale-book refusal decided it", "paper artefact",
                          "No lesson. A stale-book refusal (the option's book was over the paper matcher's age limit) blocked an exit, and the "
                          "trade that stayed in was priced later. A live order would have gone through. On record, "
                          f"{refs.get('trades', '—')} of {ev.get('trades', '—')} closed trades met a stale-book refusal; the refused exits moved "
                          f"{inr(refs.get('favour'))} in the trade's favour and {inr(refs.get('against'))} against it.")
    if t["exitReason"] in HALTS or p["exitReason"] in HALTS:
        side = t if t["exitReason"] in HALTS else p
        return _no_lesson("artefact_halt", "operator action: a halt or a forced flatten closed one side", "operator action",
                          f"No lesson. {_cap(_who(side)) if side is p else 'This trade'} was closed by {side['exitReason']} at "
                          f"{hms(side['anat']['final']['ts'])} — a halt or a forced flatten, not a book rule.")
    decided: dict[str, Any]
    systematic: dict[str, Any] | None = None
    if t["engine"] == p["engine"] and t["baseBook"] == p["baseBook"]:
        e = contrast_evidence(ev, f"{t['label']} -> {p['label']}", True)
        e["what"] = f"{t['label']} vs {p['label']} on the same fill"
        key = "stop_rule"
        decided = {**e, "params": param_diffs(t["params"], p["params"], ("stop_mode", "eq_stop_area_pct_s", "max_premium_loss_pct", "sustain_s")),
                   "text": f"The stop rule alone made this difference. Pooled, {e['text']}"}
    elif t["engine"] != p["engine"] and t["baseBook"] == p["baseBook"] and t["rule"] == p["rule"]:
        e = contrast_evidence(ev, f"{SHORT.get(t['baseBook'], t['baseBook'])} -> {SHORT.get(p['baseBook'], p['baseBook'])}",
                              p["engine"] == ENGINE_B)
        e["what"] = f"{ENGINE_A} vs {ENGINE_B}, the same book"
        a_, b_ = (t, p) if t["engine"] == ENGINE_A else (p, t)
        reproj = (a_["engine"] == ENGINE_A and b_["engine"] == ENGINE_B and a_["baseBook"] != "FUDKII"
                  and (mech_class(a_["anat"]["mech"]) == "STOP" or mech_class(b_["anat"]["mech"]) == "STOP")
                  and abs((a_["position"]["optionSlFinal"] or 0) - (b_["position"]["optionSlFinal"] or 0)) > 0.01)
        why = (f"The one rule the engines run differently decided it. {_cap(ENGINE_A)} re-derives the option stop from the live delta every 10 s, "
               f"so its line rose to {px(a_['position']['optionSlFinal'])} as the stock moved against the trade; {ENGINE_B} holds the line set "
               f"at the fill ({px(b_['position']['optionSlFinal'])}). On record this decided only 3 option-triggers — an anecdote, not a lesson."
               if reproj else "The engines run the same rules here; the difference is feed timing on one exit or fill.")
        key = "engine"
        decided = {**e, "params": [], "text": f"Same book and rule in the other engine. {why} Pooled {ENGINE_A} vs {ENGINE_B}, {e['text']}"}
    else:
        pair = contrast_evidence(ev, f"{t['label']} -> {p['label']}", True)
        pair["what"] = f"{p['label']}'s exits over {t['label']}'s on the same trigger"
        if pair["grade"] == GRADE_SYSTEMATIC:
            systematic = {**pair, "params": param_diffs(t["params"], p["params"]),
                          "text": f"{p['label']}'s exit set over {t['label']}'s: {pair['text']}"}
        gap_s = p["anat"]["final"]["ts"] - t["anat"]["final"]["ts"]
        if abs(gap_s) < 5:
            key = "same_time"
            decided = {**pair, "what": "both left within 5 s", "params": [],
                       "text": "Both left within 5 s of each other: the difference is in the fills and the rungs; no rule fired at a different time"}
            if systematic is None:
                decided["text"] += f". For this book pair, {pair['text']} [{pair['strength']}]"
        else:
            first = t if gap_s > 0 else p
            fam = "current" if first["rule"] == "current" else "stock-stop mirror"
            fo = first_out_evidence(ev, fam, first["baseBook"], first["anat"]["mech"], card_says_hold=first is t)
            fo["what"] = f"{first['label']}'s {first['anat']['mech']} firing first"
            mc = mech_class(first["anat"]["mech"])
            lvl = first["anat"]["final"]["level"] if first["anat"]["final"]["level"] else first["position"]["optionSlFinal"]
            lvl_s = f"{pct(lvl, first['e'])} of the premium paid" if lvl else "—"
            mins = f"{(first['anat']['final']['ts'] - first['openedTs']) / 60:.0f}"
            if first is t:
                key = {"RAISED": "early_raised_stop", "TRAIL": "early_trail", "STOP": "stop_fired_peer_held"}.get(mc, "other")
                rr = _raise_rule(t["baseBook"])
                what = {"RAISED": f"This trade's stop had been raised to {lvl_s} and fired {mins} min after the fill, while {_who(p)} held."
                                  + (f" {rr}. RT-Y arms only at +5 %, keeps the rung stop one rung behind and needs 75 s through it." if rr else ""),
                        "TRAIL": f"This trade's give-back sold {mins} min after the fill, at {pct(t['anat']['D'], t['e'])} of paid. {_cap(_who(p))} rode on "
                                 f"to {p['anat']['mech']}.",
                        "STOP": f"This trade's {t['anat']['mech']} fired {mins} min after the fill, at {pct(t['anat']['D'], t['e'])} of paid. "
                                f"{_cap(_who(p))}'s stop was wider or slower; it came back and left on {p['anat']['mech']} at {pct(p['anat']['D'], p['e'])}."
                        }.get(mc, "")
            else:
                key = {"RAISED": "peer_locked_gain", "TRAIL": "peer_locked_gain", "STOP": "peer_cut_loss"}.get(mc, "other")
                what = {"RAISED": f"{_cap(_who(p))} locked the gain: its stop had been raised to {lvl_s} and fired {mins} min after the fill. This trade "
                                  f"held and gave it back, leaving on {t['anat']['mech']} at {pct(t['anat']['D'], t['e'])} of paid.",
                        "TRAIL": f"{_cap(_who(p))}'s give-back locked {pct(p['anat']['D'], p['e'])} of paid {mins} min after the fill. This trade held and "
                                 f"left on {t['anat']['mech']} at {pct(t['anat']['D'], t['e'])}.",
                        "STOP": f"{_cap(_who(p))}'s {p['anat']['mech']} cut the loss {mins} min after the fill, at {pct(p['anat']['D'], p['e'])} of paid. "
                                f"This trade's stop was further away or slower; it left on {t['anat']['mech']} at {pct(t['anat']['D'], t['e'])}."
                        }.get(mc, "")
            decided = {**fo, "params": param_diffs(t["params"], p["params"], MECH_PARAMS.get(mc)),
                       "text": f"{what} Pooled, {fo['text']}"}
            if systematic is None:
                decided["text"] += f". For this book pair, {pair['text']} [{pair['strength']}]"
    evidence = [decided] + ([systematic] if systematic is not None else [])
    head = systematic if systematic is not None else decided
    text = f"{decided['text']} [{decided['strength']}]."
    if systematic is not None:
        text += (f" Systematic across the record — {systematic['text']} [{systematic['strength']}]. The parameters in that set: "
                 + ("; ".join(f"{d['label']} {d['this']} → {d['peer']}" for d in systematic["params"]) or "none differ") + ".")
    return {"key": key, "grade": head["grade"], "gradeWhy": f"{head['what']}: {head['strength']}, {head['n']} cases",
            "strength": head["strength"], "decided": decided, "systematic": systematic, "params": decided["params"],
            "evidence": evidence, "text": text}


# -- the card's block --------------------------------------------------------------------------------------------------


def _peer_row(r: Mapping[str, Any], t: Mapping[str, Any]) -> dict[str, Any]:
    a = r["anat"]
    return {"engine": r["engine"], "positionId": r["positionId"], "tradeId": r["tradeId"], "book": r["book"], "label": r["label"],
            "baseBook": r["baseBook"], "variant": r["rule"], "cls": r["cls"], "status": r["status"], "openedTs": r["openedTs"],
            "closedTs": r["closedTs"], "entry": r["e"], "exitAvg": r["x"], "finalExit": a["final"]["fill"] if a else None,
            "lots": r["lots"], "qty": r["q"], "net": r["net"], "exitReason": r["exitReason"], "mech": a["mech"] if a else None,
            "refused": len(r["refusals"]), "sameEngine": r["engine"] == t["engine"], "contract": r["contract"]}


def _rank(r: Mapping[str, Any], t: Mapping[str, Any]) -> tuple[Any, ...]:
    """The study's tie order for the best peer: the class, this engine first, the book order, the rule, the earlier open."""
    return (-(r["net"] or 0.0), CLASS_ORDER.get(r["cls"], 9), int(r["engine"] != t["engine"]),
            BOOK_ORDER.index(r["baseBook"]) if r["baseBook"] in BOOK_ORDER else 99, RULE_ORDER.get(r["rule"], 9), r["openedTs"])


def peer_block(t: Mapping[str, Any], records: Sequence[Mapping[str, Any]], *, now: float, ev: Mapping[str, Any],
               sib: Mapping[tuple[str, str], Mapping[str, Mapping[str, Any]]], src: Mapping[tuple[str, str], tuple[str, str]]) -> dict[str, Any]:
    """The card's "same option elsewhere" block: every peer, and for every one that made more, the parts and the lesson."""
    peers = peers_of(t, records, now)
    rows = sorted((_peer_row(r, t) for r in peers), key=lambda x: (x["net"] is None, -(x["net"] or 0.0), x["openedTs"]))
    base = {"contract": t["contract"], "scripCode": t["scripCode"], "rows": rows, "n": len(peers), "better": [], "betterExists": False,
            "betterCount": 0, "class": "no peer", "headline": "", "outcome": None, "pooledTo": ev.get("pooledTo")}
    if not peers:
        base["headline"] = "No other book or engine held this option at the same time."
        return base
    if t["status"] != "CLOSED":
        base.update({"class": "open", "headline": "peers: open — this trade is still running; the comparison comes when it closes"})
        return base
    closed = [r for r in peers if r["status"] == "CLOSED" and r["anat"] is not None]
    nets = [r["net"] for r in closed] + [t["net"]]
    if closed:
        base["outcome"] = ("every variant lost" if all(n < 0 for n in nets) else "every variant won" if all(n >= 0 for n in nets)
                           else "mixed")
    if not closed:
        base.update({"class": "open", "headline": "peers: open — every peer on this option is still running"})
        return base
    thr = max(100.0, 0.005 * t["e"] * t["q"])
    better = sorted((r for r in closed if r["net"] > t["net"] + 1.0), key=lambda r: _rank(r, t))
    best = min(closed, key=lambda r: _rank(r, t))
    delta = best["net"] - t["net"]
    worse = sum(1 for r in closed if r["net"] < t["net"] - 1)
    if delta < thr:
        base["class"] = "best or within threshold"
        base["headline"] = (f"No book or engine did meaningfully better on this option. The best peer, {_who(best)}, made {inr(best['net'])}, "
                            f"{inr(delta, True)} against this trade's {inr(t['net'])}. {worse} of {len(closed)} peers did worse.")
        if t["net"] < 0 and best["net"] < 0:
            base["headline"] += " Every book on this option lost, so the trigger or its direction was the problem, not the exit."
    else:
        base["class"] = ("peer lost less (both lost)" if (t["net"] < 0 and best["net"] < 0) else
                         "peer won where this lost" if t["net"] < 0 <= best["net"] else "peer won more")
        n_match = sum(1 for r in closed if r is not best and r["net"] >= best["net"] - 1)
        base["headline"] = (f"{_who(best)} made {inr(delta)} more on this same option: {inr(best['net'])} against {inr(t['net'])}"
                            + (f" ({n_match} other peer(s) matched it)" if n_match else "") + "."
                            + (" Every book on this option lost; the best only lost less. The trigger or its direction was the problem, "
                               "not the exit." if base["class"] == "peer lost less (both lost)" else ""))
    # the better peers, identical outcomes grouped under the one the study's tie order puts first
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for r in better:
        k = (r["engine"], round(r["net"], 2), round(r["x"], 4), round(r["e"], 4), r["q"], round(r["anat"]["final"]["ts"]))
        groups.setdefault(k, []).append(r)
    out = []
    for members in groups.values():
        # identical outcomes: the one closest to this trade explains it best (its own sibling, then its own book, then its rule)
        members.sort(key=lambda r: (r["engine"] != t["engine"], r["baseBook"] != t["baseBook"], r["rule"] != t["rule"], _rank(r, t)))
        p = members[0]
        gap = p["net"] - t["net"]
        parts, chain, note = pair_parts(t, p, sib, src)
        resid = gap - sum(parts.values())
        rupees = whole_rupees(parts, gap)
        meaningful = gap >= thr
        lines = [{"key": k, "label": PART_LABEL[k], "inr": parts[k], "rupees": rupees[k], "text": part_line(k, parts[k], t, p, chain),
                  "major": abs(parts[k]) >= max(100.0, 0.10 * gap)}
                 for k in sorted(PARTS, key=lambda k: -abs(parts[k])) if abs(parts[k]) >= 0.5]
        out.append({
            "peer": _peer_row(p, t), "members": [_peer_row(m, t) for m in members], "gap": gap, "gapRupees": round(gap),
            "parts": parts, "rupees": rupees, "residual": resid, "lines": lines, "relation": relation(t, p),
            "chainVia": None if chain is None else _peer_row(chain, t), "note": note, "meaningful": meaningful,
            "exits": {"this": _exit_side(chain or t), "peer": _exit_side(p), "thisIsChain": chain is not None},
            "paramDiffs": param_diffs(t["params"], p["params"]) if (t["baseBook"] != p["baseBook"] or t["rule"] != p["rule"]) else [],
            "lesson": lesson(t, p, parts, gap, ev) if meaningful else {
                "key": "within_threshold", "grade": None, "gradeWhy": "", "strength": "", "decided": None, "systematic": None,
                "params": [], "evidence": [],
                "text": f"Within {inr(thr)} (₹100 or 0.5 % of the premium) of this trade: no lesson is read into it."},
        })
    base["better"] = out
    base["betterCount"] = len(better)
    base["betterExists"] = any(g["meaningful"] for g in out)
    base["best"] = None if delta < thr else _peer_row(best, t)
    base["delta"] = delta
    return base


def sibling_maps(records: Sequence[Mapping[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Mapping[str, Any]]], dict[tuple[str, str], tuple[str, str]]]:
    """(engine, source position) → {stop rule: record}, and each record's source key."""
    src = {(r["engine"], r["positionId"]): source_key(r, records) for r in records}
    sib: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = {}
    for r in records:
        if r["status"] == "CLOSED" and r["anat"] is not None:
            sib.setdefault(src[(r["engine"], r["positionId"])], {})[r["rule"]] = r
    return sib, src


def peer_blocks(cards: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]], *, engine: str, now: float,
                ev: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Position id → its card's block, for every card of this engine."""
    sib, src = sibling_maps(records)
    mine = {r["positionId"]: r for r in records if r["engine"] == engine}
    out = {}
    for c in cards:
        t = mine.get(str(c.get("id") or ""))
        if t is not None:
            out[t["positionId"]] = peer_block(t, records, now=now, ev=ev, sib=sib, src=src)
    return out


# -- the evidence ------------------------------------------------------------------------------------------------------

_loaded: dict[str, Any] = {}


def load_evidence(data_dir: Path | None) -> dict[str, Any]:
    """The pooled tables: ``<data_dir>/peer_evidence.json`` when one was written after a close (re-read when it changes),
    else the snapshot shipped with the code (``review_evidence.EVIDENCE``, pooled to 9 Oct 2026)."""
    if data_dir is not None:
        f = data_dir / EVIDENCE_FILE
        try:
            mt = f.stat().st_mtime
        except OSError:
            mt = None
        if mt is not None:
            if _loaded.get("path") == str(f) and _loaded.get("mtime") == mt:
                return _loaded["ev"]
            try:
                ev = json.loads(f.read_text())
                if isinstance(ev, dict) and isinstance(ev.get("contrasts"), dict) and isinstance(ev.get("firstOut"), list):
                    _loaded.update(path=str(f), mtime=mt, ev=ev)
                    return ev
            except (OSError, ValueError):
                pass
    return EVIDENCE


__all__ = [
    "GRADE_ONE_OFF", "GRADE_SUGGESTIVE", "GRADE_SYSTEMATIC", "PARTS", "build_records", "decompose", "lesson", "load_evidence",
    "mechanism", "overlaps", "pair_parts", "param_diffs", "params_json", "peer_block", "peer_blocks", "peers_of", "record",
    "refusals_of", "sibling_maps", "whole_rupees",
]
