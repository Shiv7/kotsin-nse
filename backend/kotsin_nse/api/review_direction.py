"""Was the direction right, and should the trigger have been skipped: a compact block on every review card (operator,
2026-10-09: "focus is on choosing the right direction of trade. often the direction is not right and often we trade a
trigger that should have been skipped. we want to avoid these rabbit holes. take fewer trades but best ones").

RECORD-ONLY, and built only from what the review already reads at the decision: the route the trigger was given, the
market's breadth, which side of the trigger the book trades, the level ahead, the trigger bar's volume, the opening gap,
how far the stock had run by the fill, the NIFTY 50 index from its open to the trigger's close, OI and the grade. Each
read is marked with or against THE TRADE by a threshold the review or the engine already uses (``review.THRESHOLDS``,
``risk/limits.py`` RT_Y_LIMITS) — none is fitted here, and none is a verdict.

Then the outcome label, the same-option study's (dir_skip, ``oc_ft50``): after the trigger bar's close, which side the
stock reached 0.5 × ATR30 first, on the one-minute bars, to 15:15 that session. Both sides inside one minute: the minute's
open beyond a level decides, else "ambiguous" (the study broke those on the 1 s tape, which the review does not read). A
trigger closing at 15:15 or later is decided on the next session: not read here.

The candidate skip rules are a HOOK: ``candidate_skip`` reads ``<data_dir>/skip_rules.candidate.json`` when the research
writes one and shows nothing while it is absent. No rule is defined here. The file's shape, for whoever writes it::

    {"name": "...", "source": "...", "rules": [{"id": "RH02", "label": "trigger closes at/after 12:45",
                                                "all": [{"fact": "triggerCloseMin", "op": ">=", "value": 210}]}]}

``fact`` names a key of the block's ``facts`` (below); ``op`` one of < <= > >= == != in. A rule with a fact the record
does not hold is "unknown", never fired.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..risk.limits import RT_Y_LIMITS
from .review import THRESHOLDS

IST = timezone(timedelta(hours=5, minutes=30))
SKIP_RULES_FILE = "skip_rules.candidate.json"
#: the label's reach: 0.5 × ATR30 either way (the study's ``oc_ft50``)
LABEL_ATR = 0.5
#: a NIFTY move within this much of its open (% of it) has gone neither way (``live_trades.FLAT_PCT``)
FLAT_PCT = 0.05
#: the engine's own gap rules: RT-Y stands aside from a 09:45 trigger gapped this far its own way (gate B), unless its
#: volume is this many times the NIFTY 50's (the gap switch); CT-Y fades that gap
GAP_DATR = RT_Y_LIMITS.skip_open_gap_datr or 0.3
GAP_SWITCH_REL_VOL = RT_Y_LIMITS.gap_switch_rel_vol or 3.0


def _f(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _hm(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M") if ts else "—"


def _at(ts: float, h: int, m: int) -> float:
    d = datetime.fromtimestamp(ts, IST)
    return d.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def trigger_close_ts(bar_ts: float) -> float:
    """When the trigger's 30m bar closed: 30 minutes on, the 15:15 bar at the 15:30 close."""
    return _at(bar_ts, 15, 30) if datetime.fromtimestamp(bar_ts, IST).strftime("%H:%M") == "15:15" else bar_ts + 1800


def first_touch(bars: Sequence[Mapping[str, Any]], *, start: float, end: float, close: float, atr: float, k: float = LABEL_ATR) -> dict[str, Any]:
    """Which way the stock reached ``k`` × ATR from ``close`` first, on the one-minute bars from ``start`` to ``end``:
    "up", "down", "none" (neither by ``end``) or "ambiguous" (both inside one minute, the open beyond neither)."""
    lv = k * atr
    w = sorted((b for b in bars if start <= float(b["ts"]) < end and _f(b.get("h")) is not None and _f(b.get("l")) is not None),
               key=lambda b: float(b["ts"]))
    if not w:
        return {"first": None, "ts": None, "how": "no one-minute bars after the trigger's close", "level": k}
    for b in w:
        up, dn = float(b["h"]) - close >= lv, close - float(b["l"]) >= lv
        if not (up or dn):
            continue
        ts = float(b["ts"])
        if up and dn:
            op = _f(b.get("o"))
            if op is not None and op - close >= lv:
                return {"first": "up", "ts": ts, "how": "the minute opened beyond the level", "level": k}
            if op is not None and close - op >= lv:
                return {"first": "down", "ts": ts, "how": "the minute opened beyond the level", "level": k}
            return {"first": "ambiguous", "ts": ts, "how": "both levels inside one minute", "level": k}
        return {"first": "up" if up else "down", "ts": ts, "how": "1m bars", "level": k}
    return {"first": "none", "ts": None, "how": f"neither side by {_hm(end)}", "level": k}


def _from_open(bars: Sequence[Mapping[str, Any]], *, day_open_ts: float, upto: float) -> tuple[float | None, float | None, float | None]:
    """(the first bar's open from ``day_open_ts``, the last close at or before ``upto``, the move in % of the open)."""
    w = sorted((b for b in bars if day_open_ts <= float(b["ts"]) < upto), key=lambda b: float(b["ts"]))
    if not w:
        return None, None, None
    o, c = _f(w[0].get("o")), _f(w[-1].get("c"))
    if not o or c is None:
        return o, c, None
    return o, c, (c / o - 1) * 100


def _read(key: str, label: str, stance: str, detail: str, **data: Any) -> dict[str, Any]:
    """One read at the decision; ``stance`` is "with" or "against" the trade, "neutral" (a fact, no side), or "unknown"."""
    return {"key": key, "label": label, "stance": stance, "detail": detail, **data}


def direction_read(card: Mapping[str, Any], *, signal: Mapping[str, Any] | None, breadth: Mapping[str, Any] | None,
                   route: Mapping[str, Any] | None, bars: Sequence[Mapping[str, Any]], nifty_bars: Sequence[Mapping[str, Any]],
                   book_side: str, skip_rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The block: the decision-time reads (each with or against the trade), the right-direction label and, when the rules
    file exists, the candidate skip rules' verdict. ``card`` is the review's own row for the trade; ``book_side`` is
    "trend" or "counter-trend" (the book follows or fades the trigger)."""
    th = THRESHOLDS
    trade_bull = card.get("direction") == "BULLISH"
    fade = book_side == "counter-trend"
    trig_dir = str((signal or {}).get("direction") or (breadth or {}).get("direction") or "") or (
        ("BEARISH" if trade_bull else "BULLISH") if fade else ("BULLISH" if trade_bull else "BEARISH"))
    trig_bull = trig_dir == "BULLISH"
    ev = (signal or {}).get("evidence") or {}
    close = _f(ev.get("close"))
    atr = _f((breadth or {}).get("atr30")) or _f((card.get("stock") or {}).get("atr")) or _f(ev.get("atr"))
    bar_ts = _f((signal or {}).get("ts"))
    reads: list[dict[str, Any]] = []
    facts: dict[str, Any] = {"tradeDirection": card.get("direction"), "triggerDirection": trig_dir, "fade": fade, "book": card.get("book"),
                             "grade": card.get("grade")}

    # the route the trigger was given, against the side this book trades
    rt = str((route or {}).get("route") or "")
    if rt:
        follows = rt == "IN_TREND"
        stance = "with" if follows != fade else "against"
        reads.append(_read("route", "Route", stance, f"routed {'in trend' if follows else 'to the fade'}; this book "
                           f"{'fades' if fade else 'follows'} the trigger" + (f" — {(route or {}).get('summary')}" if (route or {}).get("summary") else ""),
                           route=rt))
        facts["routeWith"] = stance == "with"
    else:
        reads.append(_read("route", "Route", "unknown", "no route stored for this trigger"))

    checks = {c["key"]: c for c in card.get("checks") or ()}
    share = _f(checks.get("market", {}).get("share"))
    if share is not None:
        stance = "with" if share >= 0.5 else ("against" if share < th["breadth_against"] else "neutral")
        reads.append(_read("breadth", "Breadth", stance, f"{share * 100:.0f} % of the NSE names were moving the trade's way "
                           f"(against under {th['breadth_against'] * 100:.0f} %)", share=share))
        facts["breadthWith"] = share
    else:
        reads.append(_read("breadth", "Breadth", "unknown", "no breadth read for this trigger"))

    reads.append(_read("side", "Trend side", "neutral", "counter-trend: it fades the SuperTrend flip" if fade
                       else "with the trend: it trades the flip's own way"))

    ahead = (card.get("levels") or {}).get("ahead") or None
    wall = (route or {}).get("wall") if isinstance((route or {}).get("wall"), Mapping) else None
    wall_txt = (f"; the router's wall {wall.get('strength')} ({wall.get('grade')}) {wall.get('distAtr')} ATR from the close"
                if wall else "")
    if ahead and _f(ahead.get("atr")) is not None:
        a = float(ahead["atr"])
        stance = "against" if a <= th["wall_atr"] else "with"
        reads.append(_read("level", "Level ahead", stance, f"{ahead.get('label')} {ahead.get('price')} {a:.2f} ATR ahead of the entry "
                           f"(in the way within {th['wall_atr']:g}){wall_txt}", atr=a))
        facts["levelAheadAtr"] = a
    else:
        reads.append(_read("level", "Level ahead", "unknown", "no level ladder in the record for this trigger" + wall_txt))

    vt, vt1 = _f((breadth or {}).get("volSurgeT")), _f((breadth or {}).get("volSurgeT1"))
    dried = bool((breadth or {}).get("volDried") or (breadth or {}).get("volDriedRel"))
    rel, tier = _f((breadth or {}).get("relVol")), (breadth or {}).get("relVolTier")
    if vt is not None:
        low = vt < th["volume_low_x"] or dried
        reads.append(_read("volume", "Volume", "against" if low else "with",
                           f"trigger bar {vt:.2f}× its usual (T)" + (f", {vt1:.2f}× (T-1)" if vt1 is not None else "") + (" — dried up" if dried else "")
                           + (f"; {rel:.1f}× the NIFTY 50's ({tier})" if rel is not None else ""), volT=vt))
        facts.update(volSurgeT=vt, volSurgeT1=vt1, volDried=dried)
    else:
        reads.append(_read("volume", "Volume", "unknown", "no volume read for this trigger"))
    if rel is not None:
        facts["relVol"] = rel

    gap, open_bar = _f((breadth or {}).get("gapDatr")), bool((breadth or {}).get("openBar"))
    if gap is not None:
        facts.update(gapDatr=gap, openBar=open_bar)
        if open_bar and gap >= GAP_DATR:
            switch = rel is not None and rel >= GAP_SWITCH_REL_VOL
            follow_ok = switch  # the gap switch: on heavy volume the gap is followed in trend, not faded
            stance = ("with" if follow_ok else "against") if not fade else ("against" if follow_ok else "with")
            reads.append(_read("gap", "Opening gap", stance, f"09:45 trigger gapped {gap:.2f} daily ATR its own way (gate B at {GAP_DATR:g})"
                               + (f"; volume {rel:.1f}× the NIFTY 50's ≥ {GAP_SWITCH_REL_VOL:g}×: the gap switch follows it in trend" if switch else ""),
                               gapDatr=gap))
        else:
            reads.append(_read("gap", "Opening gap", "neutral", f"opening gap {gap:.2f} daily ATR the trigger's way"
                               + (" — decided on the first bar" if open_bar else ""), gapDatr=gap))

    chase = _f(checks.get("entry", {}).get("chaseAtr"))
    if chase is not None:
        reads.append(_read("chase", "Chase", "against" if chase >= th["chase_atr"] else "with",
                           f"the stock went {chase:+.2f} ATR the trade's way between the trigger close and the fill (chased from {th['chase_atr']:g})",
                           chaseAtr=chase))
        facts["chaseAtr"] = chase

    close_ts = trigger_close_ts(bar_ts) if bar_ts else None
    if close_ts:
        d_open = _at(close_ts, 9, 15)
        o, c, mv = _from_open(nifty_bars, day_open_ts=d_open, upto=close_ts)
        if mv is not None:
            with_trade = mv if trade_bull else -mv
            stance = "neutral" if abs(mv) < FLAT_PCT else ("with" if with_trade > 0 else "against")
            reads.append(_read("nifty", "NIFTY 50", stance, f"the index {mv:+.2f} % from its open {o:g} to {c:g} at the trigger's close "
                               f"{_hm(close_ts)}" + (f"; NIFTY 50 volume {float((breadth or {})['n50SurgeT']):.2f}× usual" if _f((breadth or {}).get("n50SurgeT")) else ""),
                               pct=mv))
            facts["niftyFromOpenPct"] = with_trade
        else:
            reads.append(_read("nifty", "NIFTY 50", "unknown", "no NIFTY 50 minute bars for this session"))
        facts["triggerCloseMin"] = round((close_ts - d_open) / 60)
        facts["triggerCloseHm"] = _hm(close_ts)

    oi = checks.get("oi") or {}
    if oi.get("quadrant"):
        stance = {"ok": "with", "bad": "against"}.get(str(oi.get("status")), "neutral")
        reads.append(_read("oi", "Open interest", stance, str(oi.get("detail") or ""), quadrant=oi.get("quadrant")))
        facts["oiStance"] = stance

    g = card.get("grade")
    reads.append(_read("grade", "Grade", "neutral", f"FUDKII graded the trigger {g}" if g else "no grade on record"))
    if close_ts:
        reads.append(_read("time", "Trigger closed", "neutral", f"the trigger's bar closed at {_hm(close_ts)}"))

    # the label: which way the stock reached 0.5 ATR30 first after the trigger closed
    label: dict[str, Any] = {"first": None, "ts": None, "how": "", "level": LABEL_ATR, "triggerRight": None, "tradeRight": None,
                             "minutes": None, "text": ""}
    if close_ts and close is not None and atr:
        end = _at(close_ts, 15, 15)
        if close_ts >= end:
            label.update(how="the trigger closed at or after 15:15: the next session decides, not read here")
        else:
            ft = first_touch(bars, start=close_ts, end=end, close=close, atr=atr)
            label.update(ft)
            if ft["first"] in ("up", "down"):
                up = ft["first"] == "up"
                label["triggerRight"] = up == trig_bull
                label["tradeRight"] = up == trade_bull
                label["minutes"] = round((ft["ts"] - close_ts) / 60)
        if label["first"] in ("up", "down"):
            label["text"] = (f"the stock reached {LABEL_ATR:g} ATR30 {'UP' if label['first'] == 'up' else 'DOWN'} first, {label['minutes']} min after "
                             f"the trigger's close {close:g} ({_hm(label['ts'])}): the {'right' if label['tradeRight'] else 'wrong'} direction for this trade")
        elif label["first"] == "none":
            label["text"] = f"the stock reached {LABEL_ATR:g} ATR30 neither way by 15:15 after the trigger's close {close:g}"
        elif label["first"] == "ambiguous":
            label["text"] = f"both sides of {LABEL_ATR:g} ATR30 inside one minute ({_hm(label['ts'])}): ambiguous on 1m bars"
        else:
            label["text"] = label["how"] or "not measurable"
    else:
        label["text"] = "no trigger close, ATR or bars on record: not measurable"
    with_n = sum(1 for r in reads if r["stance"] == "with")
    against = [r for r in reads if r["stance"] == "against"]
    return {
        "trigger": {"direction": trig_dir, "closeTs": close_ts, "close": close, "atr": atr},
        "trade": "BULLISH" if trade_bull else "BEARISH", "fade": fade, "reads": reads,
        "with": with_n, "against": len(against), "againstKeys": [r["key"] for r in against],
        "label": label, "facts": facts, "skip": candidate_skip(facts, skip_rules) if skip_rules else None,
    }


# -- the candidate skip rules: a hook ------------------------------------------------------------------------------------

_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b, "in": lambda a, b: a in b}


def load_skip_rules(data_dir: Path | None) -> dict[str, Any] | None:
    """The candidate skip rules, when the research has written them; None while the file is absent or unreadable."""
    if data_dir is None:
        return None
    try:
        raw = json.loads((data_dir / SKIP_RULES_FILE).read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("rules"), list) else None


def candidate_skip(facts: Mapping[str, Any], rules: Mapping[str, Any]) -> dict[str, Any]:
    """Would the candidate skip rules have skipped this trigger: each rule fired (every condition held), not fired, or
    unknown (a fact the record does not hold). Applies the file as written; defines nothing."""
    out = []
    for r in rules.get("rules") or ():
        if not isinstance(r, Mapping):
            continue
        state: bool | None = True
        for cond in r.get("all") or ():
            v = facts.get(str(cond.get("fact")))
            op = _OPS.get(str(cond.get("op")))
            if v is None or op is None:
                state = None
                break
            try:
                ok = bool(op(v, cond.get("value")))
            except TypeError:
                state = None
                break
            if not ok:
                state = False
                break
        out.append({"id": r.get("id"), "label": r.get("label"), "fired": state})
    return {"name": rules.get("name"), "source": rules.get("source"), "rules": out, "skipped": any(x["fired"] for x in out)}


__all__ = ["SKIP_RULES_FILE", "candidate_skip", "direction_read", "first_touch", "load_skip_rules", "trigger_close_ts"]
