"""Trade review (operator, 2026-10-05: "see all detailed view of each trade and analytics on why it incurred a loss.
did we have to wait before entering, was a pivot around, was it a counter-trend? no volume? no oi? etc and be
accurate in identifying the root cause of the loss for each trade").

Every number here is read from the record — the position and its execution log, the trigger's evidence, the
breadth / route / fade events logged when the trigger was judged, and the underlying's one-minute bars — never
estimated where the record is silent: a question the record cannot answer says so. The root cause is chosen by
fixed rules in a fixed order (``ROOT_CAUSE_ORDER``), each citing the numbers that made it; every other check that
failed is listed as a contributing factor, not promoted to a cause.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

#: The thresholds every check uses, in the stock's ATR (30m, 14) unless named otherwise. Shown on the page.
THRESHOLDS: dict[str, float] = {
    "wall_atr": 0.5,          # a level this close ahead of the entry is in the way (RT-Y-F's pivot gate uses the same)
    "stall_atr": 0.15,        # the stock's best price within this of that level, and not this far past it: stopped there
    "chase_atr": 0.3,         # the stock had already moved this far the trade's way between the trigger and the fill
    "tight_stop_atr": 0.5,    # a stop nearer than this sits inside one bar's noise
    "gave_back_r": 0.5,       # the option was this far up (in R) before the loss
    "breadth_against": 0.45,  # fewer than this share of the NSE names moving the trade's way: the market was against
    "volume_low_x": 1.0,      # the trigger bar's volume under its usual level (T surge < 1×)
    "gap_atr": 0.5,           # an opening gap this large, decided on the first bar
    "later_right_atr": 1.0,   # after the stop, the stock went this far the trade's way from the entry the same session
    "early_window_s": 900.0,  # "should we have waited": the stock's dip in the first 15 minutes after the fill
    "excess_option_pct": 3.0,  # the option fell this many % more than the stock's move explains
    "entry_slip_r": 0.25,     # paid this many R over the option's mid when the order went in
    "breakeven_r": 0.25,      # a raised stop that closed within this many R of the entry: the trail at breakeven
}

COUNTER_TREND_BOOKS = ("FUDKII_CT_X", "FUDKII_CT_Y", "FUDKII_CT_M")
OPTION_STOP_EXITS = ("SL-OP",)
#: exits a rule or a person made, not the market: a halt, the daily loss limit, the 15:20 flatten, a manual close
RULE_EXITS = ("HALT", "DAILY_LOSS", "FORCED", "MANUAL")

#: The order the root cause is chosen in; the first rule that holds is the cause.
ROOT_CAUSE_ORDER = (
    "costs", "closed_by_rule", "exit_cost", "breakeven_stop", "option_stop", "tight_stop", "gave_back", "fade_failed", "pivot_rejection",
    "chased", "entry_cost", "too_early", "market_against", "oi_against", "no_volume", "went_nowhere", "unexplained",
)
ROOT_CAUSE_LABEL = {
    "costs": "Costs",
    "closed_by_rule": "Closed by a rule, not the market",
    "exit_cost": "Exit cost — the stop filled well below its level",
    "breakeven_stop": "Trail stop near breakeven",
    "option_stop": "Option stop, not the stock",
    "tight_stop": "Stop inside the noise",
    "entry_cost": "Entry cost",
    "gave_back": "Gave back a winner",
    "fade_failed": "Fade failed — the wall broke",
    "pivot_rejection": "Rejected at a level just ahead",
    "chased": "Chased the entry",
    "too_early": "Right idea, too early (stop inside the noise)",
    "market_against": "Market against the trade",
    "oi_against": "OI against the trade",
    "no_volume": "No volume behind the move",
    "went_nowhere": "Went nowhere — closed on time",
    "unexplained": "No single cause in the record",
    "win": "Win",
    "open": "Still open",
}


def parent_signal_id(signal_id: str) -> str:
    """A book's own trigger id (``FUDKII_CT_Y-SYM-TS-X``) back to the FUDKII trigger it came from."""
    return re.sub(r"^FUDKII_[A-Z0-9_]+-", "FUDKII-", signal_id or "")


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _delta_from_note(note: str) -> float | None:
    m = re.search(r"delta\s*≈?\s*([0-9.]+)", note or "")
    return _f(m.group(1)) if m else None


def _bar_at(bars: list[dict[str, Any]], ts: float) -> dict[str, Any] | None:
    """The one-minute bar that holds ``ts`` (else the last one before it)."""
    best = None
    for b in bars:
        if b["ts"] <= ts:
            best = b
        else:
            break
    return best


def _quadrant(bar_up: bool | None, oi_change: float | None) -> str | None:
    if bar_up is None or oi_change is None or oi_change == 0:
        return None
    if bar_up:
        return "long build-up" if oi_change > 0 else "short covering"
    return "short build-up" if oi_change > 0 else "long unwinding"


#: what each OI quadrant says about a trade the given way: fresh positions are the strong read (build-up), positions
#: closing the weak one (covering, unwinding) — "ok" confirms, "warn" is weak either way, "bad" is fresh money against it
_QUADRANT_FOR = {
    ("long build-up", 1): ("ok", "confirms the trade"), ("short covering", 1): ("warn", "weak support: shorts closing, not new longs"),
    ("short build-up", 1): ("bad", "against the trade: fresh shorts"), ("long unwinding", 1): ("warn", "weakly against: longs closing"),
    ("short build-up", -1): ("ok", "confirms the trade"), ("long unwinding", -1): ("warn", "weak support: longs closing, not new shorts"),
    ("long build-up", -1): ("bad", "against the trade: fresh longs"), ("short covering", -1): ("warn", "weakly against: shorts closing"),
}


def fx(x: float | None, nd: int = 2, sign: bool = False) -> str:
    """A number for a sentence; "—" where the record has none."""
    if x is None:
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _hm(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M") if ts else "—"


def _check(key: str, question: str, status: str, detail: str, **data: Any) -> dict[str, Any]:
    """One row of the checklist: ``status`` is "ok", "bad", "warn" or "unknown"."""
    return {"key": key, "question": question, "status": status, "detail": detail, **data}


def review_trade(
    pos: dict[str, Any], *, trade: dict[str, Any] | None, signal: dict[str, Any] | None, breadth: dict[str, Any] | None,
    route: dict[str, Any] | None, fade: dict[str, Any] | None, bars: list[dict[str, Any]], mirrors: list[dict[str, Any]],
    session_end: float, label: str = "",
) -> dict[str, Any]:
    """One trade, from the trigger to the exit and what the stock did after, with its checklist and root cause."""
    th = THRESHOLDS
    bars = sorted((b for b in bars if _f(b.get("c"))), key=lambda b: b["ts"])
    book = str(pos.get("strategy") or "")
    bull = str(pos.get("direction") or "").upper().startswith("BULL")
    d = 1 if bull else -1
    closed = pos.get("status") == "CLOSED"
    el = pos.get("exec_log") or {}
    ent = el.get("entry") or {}
    exits = [x for x in (el.get("exits") or []) if isinstance(x, dict)]
    last_exit = exits[-1] if exits else {}
    qty = int(_f(pos.get("qty")) or 0)
    entry = _f(pos.get("entry")) or 0.0
    eq_entry = _f(pos.get("equity_entry"))
    eq_sl = _f(pos.get("equity_sl"))
    eq_targets = [t for t in (pos.get("equity_targets") or []) if _f(t)]
    ev = (signal or {}).get("evidence") or {}
    atr = _f(pos.get("equity_atr")) or _f((breadth or {}).get("atr30")) or _f(ev.get("atr"))
    fill_ts = _f(ent.get("filledTs")) or _f(pos.get("opened_ts")) or 0.0
    signal_ts = _f(ent.get("signalTs"))
    exit_ts = _f(pos.get("closed_ts")) if closed else None
    counter = book in COUNTER_TREND_BOOKS
    delta = _delta_from_note(str(pos.get("note") or ""))

    def in_atr(x: float | None) -> float | None:
        return round(x / atr, 3) if (x is not None and atr) else None

    # -- the stock's path ------------------------------------------------------------------------------
    trig_close = _f(ev.get("close")) if not counter else (eq_entry or _f(ev.get("close")))
    fill_bar = _bar_at(bars, fill_ts)
    stock_at_fill = _f(fill_bar["c"]) if fill_bar else None
    hold = [b for b in bars if fill_ts - 60 < b["ts"] <= (exit_ts or session_end)]
    after = [b for b in bars if exit_ts is not None and b["ts"] > exit_ts]
    first15 = [b for b in bars if fill_ts - 60 < b["ts"] <= fill_ts + th["early_window_s"]]
    ref_px = eq_entry or stock_at_fill

    def fav(bs: list[dict[str, Any]]) -> float | None:
        if not bs or ref_px is None:
            return None
        return max(b["h"] for b in bs) - ref_px if bull else ref_px - min(b["l"] for b in bs)

    def adv(bs: list[dict[str, Any]]) -> float | None:
        if not bs or ref_px is None:
            return None
        return ref_px - min(b["l"] for b in bs) if bull else max(b["h"] for b in bs) - ref_px

    best_fav, worst_adv = fav(hold), adv(hold)
    stock_hit_stop = None
    stop_hit_ts = None
    if hold and eq_sl:
        for b in hold:
            if (b["l"] <= eq_sl) if bull else (b["h"] >= eq_sl):
                stock_hit_stop, stop_hit_ts = True, b["ts"]
                break
        else:
            stock_hit_stop = False
    exit_bar = _bar_at(bars, exit_ts) if exit_ts else None
    stock_at_exit = _f(exit_bar["c"]) if exit_bar else None
    later_fav = fav(after)
    later_stop_ts = next((b["ts"] for b in after if eq_sl and ((b["l"] <= eq_sl) if bull else (b["h"] >= eq_sl))), None)
    later_t1 = bool(after and eq_targets and any((b["h"] >= eq_targets[0]) if bull else (b["l"] <= eq_targets[0]) for b in after))
    dip15 = adv(first15)
    stop_dist = abs(eq_entry - eq_sl) if (eq_entry and eq_sl) else None

    # -- the levels around the entry ----------------------------------------------------------------------
    levels: dict[str, float] = {}
    for leg in (route or {}).get("legs") or []:
        if leg.get("name") == "equity":
            levels = {k: float(v) for k, v in (leg.get("levels") or {}).items() if _f(v)}
    ahead = None
    if levels and ref_px:
        cands = [(k, p, d * (p - ref_px)) for k, p in levels.items() if d * (p - ref_px) > 0]
        if cands:
            k, p, dist = min(cands, key=lambda c: c[2])
            ahead = {"label": k, "price": round(p, 2), "atr": in_atr(dist)}
    elif breadth and not counter and breadth.get("pivotsAheadAtr"):
        k, a = breadth["pivotsAheadAtr"][0]
        ahead = {"label": k, "price": round(ref_px + d * a * atr, 2) if (ref_px and atr) else None, "atr": round(float(a), 3)}
    stalled = None
    if ahead and ahead.get("price") and best_fav is not None and atr:
        best_px = ref_px + best_fav if bull else ref_px - best_fav
        gap_to_level = d * (ahead["price"] - best_px)  # > 0: the best price stayed short of the level
        stalled = bool(-th["stall_atr"] * atr <= gap_to_level <= th["stall_atr"] * atr)

    # -- the checks ----------------------------------------------------------------------------------------------
    checks: list[dict[str, Any]] = []
    wait_s = _f(ent.get("waitS"))
    chase = in_atr(d * (stock_at_fill - trig_close)) if (stock_at_fill is not None and trig_close) else None
    ref_opt, fill_opt = _f(ent.get("ref")), _f(ent.get("fillPrice")) or entry
    bap = ent.get("bookAtPlace") or {}
    mid_at_place = ((_f(bap.get("bid")) or 0) + (_f(bap.get("ask")) or 0)) / 2 if (_f(bap.get("bid")) and _f(bap.get("ask"))) else None
    slip_pct = round((fill_opt - mid_at_place) / mid_at_place * 100, 2) if (mid_at_place and fill_opt) else None
    chased = chase is not None and chase >= th["chase_atr"]
    r_unit = _f(pos.get("r_unit")) or 0.0
    slip_r = round((fill_opt - mid_at_place) / r_unit, 3) if (mid_at_place and fill_opt and r_unit) else None
    costly_entry = slip_r is not None and slip_r >= th["entry_slip_r"]
    entry_detail = []
    if wait_s is not None:
        entry_detail.append(f"filled {wait_s:.0f} s after the trigger" + (f", {len(ent.get('reprices') or [])} re-price(s)" if ent.get("reprices") else ""))
    if chase is not None:
        entry_detail.append(f"the stock {fx(chase, sign=True)} ATR the trade's way between the trigger close {trig_close:g} and the fill")
    if slip_pct is not None:
        entry_detail.append(f"option paid {fill_opt:g} vs the mid {mid_at_place:.2f} when the order went in ({slip_pct:+.1f} %"
                            + (f", {slip_r:+.2f} R" if slip_r is not None else "") + ")")
    if dip15 is not None and atr:
        entry_detail.append(f"in the 15 minutes after the fill the stock went {fx(in_atr(dip15))} ATR against at worst")
    checks.append(_check("entry", "Did we have to wait / did we chase the entry?",
                         "bad" if (chased or costly_entry) else ("ok" if chase is not None else "unknown"),
                         "; ".join(entry_detail) or "no entry record", waitS=wait_s, chaseAtr=chase, slipPct=slip_pct, slipR=slip_r, dip15Atr=in_atr(dip15)))

    if ahead:
        near = ahead["atr"] is not None and ahead["atr"] <= th["wall_atr"]
        txt = f"nearest level the trade's way: {ahead['label']} {ahead['price']} — {fx(ahead['atr'])} ATR from the entry"
        if stalled:
            txt += "; the stock's best price stopped at it"
        elif stalled is False and best_fav is not None:
            txt += f"; the stock's best was {fx(in_atr(best_fav))} ATR the trade's way"
        checks.append(_check("pivot", "Was a pivot / cluster in the way?", "bad" if (near and stalled) else ("warn" if near else "ok"), txt,
                             level=ahead, stalled=stalled))
    else:
        checks.append(_check("pivot", "Was a pivot / cluster in the way?", "unknown", "no level ladder in the record for this trigger"))

    side_txt = "counter-trend: it fades the SuperTrend flip" if counter else "with the trend: it trades the flip's own way"
    if fade:
        side_txt += f" — fade plan: stop {fade.get('stop')}, opening gap {fade.get('gapDatr')} ATR" if fade.get("gapDatr") is not None else f" — stop {fade.get('stop')}"
    checks.append(_check("trend", "Was it with the trend or counter-trend?", "warn" if counter else "ok", side_txt, counterTrend=counter))

    share = _f((breadth or {}).get("share"))
    parent_bull = str((breadth or {}).get("direction") or "").upper().startswith("BULL")
    share_trade = (share if parent_bull == bull else 1 - share) if share is not None else None
    if share_trade is not None:
        against = share_trade < th["breadth_against"]
        checks.append(_check("market", "Was the market with the trade?", "bad" if against else "ok",
                             f"{share_trade * 100:.0f} % of the NSE names were moving the trade's way at the trigger", share=round(share_trade, 3)))
    else:
        checks.append(_check("market", "Was the market with the trade?", "unknown", "no breadth read for this trigger"))

    vt, vt1 = _f((breadth or {}).get("volSurgeT")), _f((breadth or {}).get("volSurgeT1"))
    dried = bool((breadth or {}).get("volDried") or (breadth or {}).get("volDriedRel"))
    if vt is not None:
        low = vt < th["volume_low_x"] or dried
        checks.append(_check("volume", "Was there volume behind the move?", "bad" if low else "ok",
                             f"trigger bar volume {vt:.2f}× its usual level (T), {vt1:.2f}× the day before's (T-1)" + (" — dried up" if dried else "")
                             + (f"; band {breadth.get('volBand')}" if breadth and breadth.get("volBand") else ""), volT=vt, volT1=vt1))
    else:
        checks.append(_check("volume", "Was there volume behind the move?", "unknown", "no volume read for this trigger"))

    oi = _f(ev.get("oi_change_pct"))
    bar_up = parent_bull if breadth else (bull != counter)  # the trigger bar moved the FUDKII trigger's way
    quad = _quadrant(bar_up, oi)
    if quad:
        status, meaning = _QUADRANT_FOR[(quad, d)]
        checks.append(_check("oi", "Did open interest back the trade?", status,
                             f"OI {oi:+.2f} % with the trigger bar {'up' if bar_up else 'down'}: {quad} — {meaning}", oiChangePct=oi, quadrant=quad))
    else:
        checks.append(_check("oi", "Did open interest back the trade?", "unknown", "no OI change on the trigger bar"))

    gap = _f((breadth or {}).get("gapDatr"))
    if gap is not None:
        big = gap >= th["gap_atr"] and bool((breadth or {}).get("openBar"))
        checks.append(_check("gap", "Was it decided on an opening gap?", "warn" if big else "ok",
                             f"opening gap {gap:.2f} ATR{' — decided on the first bar' if (breadth or {}).get('openBar') else ''}", gapAtr=gap))

    if stop_dist is not None and atr:
        tight = stop_dist / atr < th["tight_stop_atr"]
        txt = f"stock stop {eq_sl:g}, {stop_dist / atr:.2f} ATR from the entry {eq_entry:g}"
        if stock_hit_stop is not None:
            txt += "; the stock reached it" if stock_hit_stop else f"; the stock never reached it (worst {fx(in_atr(worst_adv))} ATR against)"
        opt_sl = _f(pos.get("initial_option_sl")) or _f(pos.get("option_sl"))
        if delta and opt_sl and eq_entry and entry:
            eq_equiv = eq_entry - d * (entry - opt_sl) / delta
            txt += f"; the option stop {opt_sl:g} sits at a stock price of about {eq_equiv:.2f} (delta {delta:g})"
        checks.append(_check("stop", "Was the stop sensible?", "warn" if tight else "ok", txt, stopAtr=round(stop_dist / atr, 3), stockHitStop=stock_hit_stop))

    # -- the option against the stock at the exit --------------------------------------------------------
    excess = None
    if closed and delta and stock_at_fill is not None and stock_at_exit is not None and entry:
        exit_px = _f(pos.get("exit_price")) or 0.0
        implied = delta * d * (stock_at_exit - stock_at_fill)
        actual = exit_px - entry
        excess = round((actual - implied) / entry * 100, 2)  # < 0: the option lost more than the stock explains

    # -- execution costs --------------------------------------------------------------------------------------
    stop_rec = last_exit.get("stop") or {}
    exit_fill = _f(last_exit.get("fillPrice")) or _f(pos.get("exit_price"))
    exit_slip = None
    on_option = str((stop_rec or {}).get("triggerOn") or "").startswith("option")
    if stop_rec and exit_fill is not None:
        # an option-priced stop: the level is an option price, so the cost is level → fill; a stock-priced stop's level is
        # the STOCK's, and the option side's cost is only the walk from the bid at the trigger into the depth
        base = _f(stop_rec.get("level")) if on_option else _f(stop_rec.get("bidAtTrigger"))
        if base is not None:
            exit_slip = round((base - exit_fill) * qty, 2)
    entry_slip = round((fill_opt - mid_at_place) * qty, 2) if (mid_at_place and fill_opt) else None
    if closed and stop_rec and exit_fill is not None:
        exit_slip_r = exit_slip / (r_unit * qty) if (exit_slip is not None and r_unit and qty) else None
        lvl, trig_px, bid_px = _f(stop_rec.get("level")), _f(stop_rec.get("triggerPrice")), _f(stop_rec.get("bidAtTrigger"))
        if on_option:
            detail = (f"the option stop {fx(lvl)} fired on the {stop_rec.get('triggerOn')} at {fx(trig_px)}; the bid was {fx(bid_px)}; "
                      f"it sold at {fx(exit_fill)}" + (f" — ₹{exit_slip:,.0f} under the level ({exit_slip_r:+.2f} R)" if exit_slip is not None and exit_slip_r is not None else ""))
        else:
            detail = (f"the stock stop {fx(lvl)} fired at {fx(trig_px)}; the option's bid then was {fx(bid_px)}; it sold at {fx(exit_fill)}"
                      + (f" — ₹{exit_slip:,.0f} walking the depth" if exit_slip else ""))
        bad_exit = exit_slip_r is not None and exit_slip_r >= th["entry_slip_r"]
        checks.append(_check("exit", "Did the exit fill near its stop?", "bad" if bad_exit else ("warn" if (exit_slip_r or 0) >= 0.1 else "ok"),
                             detail, exitSlipInr=exit_slip, exitSlipR=None if exit_slip_r is None else round(exit_slip_r, 3)))
    gross = _f((trade or {}).get("gross"))
    charges = _f((trade or {}).get("charges"))
    net = _f((trade or {}).get("net"))

    # -- the root cause ----------------------------------------------------------------------------------------
    mfe_r, mae_r = _f(pos.get("mfe_r")) or 0.0, _f(pos.get("mae_r")) or 0.0
    reason = str(pos.get("exit_reason") or "")
    fails = {c["key"] for c in checks if c["status"] == "bad"}
    init_sl = _f(pos.get("initial_option_sl")) or _f(pos.get("option_sl"))
    tick = _f((pos.get("instrument") or {}).get("tick_size")) or 0.05
    stop_level = _f(stop_rec.get("level")) if (stop_rec and on_option) else None
    raised = bool(init_sl and stop_level and stop_level > init_sl + tick / 2)
    thesis_failed = bool(stock_hit_stop or later_stop_ts)  # the stock reached its own stop, during the trade or after it
    tight = bool(stop_dist and atr and stop_dist / atr < th["tight_stop_atr"])
    mirror_txt = ""
    done_m = [m for m in mirrors if m.get("net") is not None]
    if done_m and net is not None:
        mirror_txt = "; the stock-stop mirrors closed at " + ", ".join(f"{m['rule']} {m['net']:+,.0f}" for m in done_m)
    elif any(m.get("status") == "OPEN" for m in mirrors):
        mirror_txt = "; the stock-stop mirrors are still open"
    if not closed:
        cause, why = "open", "the trade is still open — its path so far is shown"
    elif net is not None and net >= 0:
        cause, why = "win", f"{reason} for {net:+,.0f}"
    elif gross is not None and gross >= 0:
        cause, why = "costs", f"it made ₹{gross:,.0f} before charges; ₹{charges or 0:,.0f} of charges turned it into ₹{net or 0:,.0f}"
    elif reason in RULE_EXITS:
        cause, why = "closed_by_rule", f"closed by {reason}, not by its stop or target, at {mae_r if (net or 0) < 0 else mfe_r:+.2f} R"
    elif on_option and exit_slip is not None and exit_slip > 0 and net is not None and (exit_slip >= abs(net) or (stop_level and stop_level >= entry > (exit_fill or 0))):
        parts = []
        trig_px, bid_px = _f(stop_rec.get("triggerPrice")), _f(stop_rec.get("bidAtTrigger"))
        if trig_px is not None and stop_level is not None:
            parts.append(f"the first price the engine saw was {trig_px:g}, {abs(stop_level - trig_px):.2f} past the stop {stop_level:g}")
        if bid_px is not None and trig_px is not None:
            parts.append(f"the bid then was {bid_px:g}")
        where = "" if not stop_level else (f", at the {entry:g} paid" if abs(stop_level - entry) < tick / 2 else (f", above the {entry:g} paid" if stop_level > entry else ""))
        cause, why = "exit_cost", (f"the stop was at {fx(stop_level)}" + where
                                   + f"; it sold at {fx(exit_fill)} — ₹{exit_slip:,.0f} below the stop level" + (" (" + "; ".join(parts) + ")" if parts else "")
                                   + f", against a ₹{abs(net):,.0f} loss")
    elif reason in OPTION_STOP_EXITS and raised and mfe_r < th["gave_back_r"] and (r_unit and abs((exit_fill or entry) - entry) / r_unit <= th["breakeven_r"]):
        cause, why = "breakeven_stop", (f"after {mfe_r:+.2f} R at best the trail had lifted the option stop from {fx(init_sl)} to {fx(stop_level)}; "
                                        f"it sold at {fx(exit_fill)} against the {entry:g} paid — charges and the spread made the loss")
    elif reason in OPTION_STOP_EXITS and raised and mfe_r >= th["gave_back_r"]:
        cause, why = "gave_back", f"it was {mfe_r:+.2f} R at best; the trailed option stop at {fx(stop_level)} let it fall back to {mae_r:+.2f} R"
    elif reason in OPTION_STOP_EXITS and not thesis_failed:
        why = (f"the option's own stop fired at {fx(exit_fill)} while the stock never reached its stop {eq_sl:g}: at worst it was "
               f"{fx(in_atr(worst_adv))} ATR against, of the {fx(in_atr(stop_dist))} ATR its stop allowed")
        if excess is not None and excess <= -th["excess_option_pct"]:
            why += f"; the option lost {abs(excess):.1f} % of its price more than the stock's move explains (spread / time value)"
        if raised:
            why += f"; its stop had been moved from {fx(init_sl)} to {fx(stop_level)} before it fired"
        cause, why = "option_stop", why + mirror_txt
    elif tight and thesis_failed:
        cause, why = "tight_stop", (f"the stock stop {eq_sl:g} was only {fx(in_atr(stop_dist))} ATR from the entry — inside one bar's noise"
                                    + (f"; the trade had been {mfe_r:+.2f} R first" if mfe_r > 0 else "")
                                    + (f"; then the stock went {fx(in_atr(later_fav))} ATR the trade's way" if (later_fav and atr and later_fav / atr >= th["later_right_atr"]) else ""))
    elif mfe_r >= th["gave_back_r"] or int(_f(pos.get("targets_hit")) or 0) >= 1:
        cause, why = "gave_back", f"it was {mfe_r:+.2f} R at best, then fell to {mae_r:+.2f} R and out on {reason}"
    elif counter and thesis_failed and fade:
        cause, why = "fade_failed", f"the fade bet on the stock turning; it went through the fade's stop {eq_sl:g} instead"
    elif "pivot" in fails:
        cause, why = "pivot_rejection", f"the stock's best stopped at {ahead['label']} {ahead['price']}, {fx(ahead['atr'])} ATR ahead of the entry, then turned"
    elif chased and thesis_failed:
        cause, why = "chased", f"the stock was already {fx(chase)} ATR the trade's way by the fill and gave it back to the stop"
    elif costly_entry and slip_r is not None and net is not None and r_unit and qty:
        cause, why = "entry_cost", f"the option cost {slip_r:+.2f} R over its mid at the order — ₹{(fill_opt - mid_at_place) * qty:,.0f} of the ₹{abs(net):,.0f} loss"
    elif thesis_failed and later_fav is not None and atr and (later_t1 or later_fav / atr >= th["later_right_atr"]):
        cause = "too_early"
        why = (f"stopped at {eq_sl:g} ({fx(in_atr(stop_dist))} ATR away), then the stock went {fx(in_atr(later_fav))} ATR the trade's way from the entry"
               + (f" and reached the first target {eq_targets[0]:g}" if later_t1 else "") + " the same session")
    elif "market" in fails:
        cause, why = "market_against", next(c["detail"] for c in checks if c["key"] == "market")
    elif "oi" in fails:
        cause, why = "oi_against", next(c["detail"] for c in checks if c["key"] == "oi")
    elif "volume" in fails:
        cause, why = "no_volume", next(c["detail"] for c in checks if c["key"] == "volume")
    elif reason in ("EOD", "TIME_STOP", "TIME") and not stock_hit_stop:
        cause, why = "went_nowhere", f"neither stop nor target in time: closed on {reason} at {mae_r if (net or 0) < 0 else mfe_r:+.2f} R"
    else:
        cause, why = "unexplained", "every recorded check passed; the stock reversed through the stop — nothing in the record singles out a cause"
    if cause not in ("open", "win", "option_stop") and reason in OPTION_STOP_EXITS and thesis_failed and not stock_hit_stop:
        why += f"; the option stop got out before the stock reached its stop (at {_hm(later_stop_ts)}){mirror_txt}"
    contributing = [c["detail"] for c in checks if c["status"] == "bad" and c["key"] not in _CAUSE_CHECK.get(cause, ())]

    # the minutes around the trade, for the chart
    t0 = (signal_ts or fill_ts) - 1800
    t1 = (exit_ts or session_end) + 1800
    chart = [{"ts": b["ts"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars if t0 <= b["ts"] <= t1]
    return {
        "id": pos.get("id"), "book": book, "label": label or book, "symbol": pos.get("symbol") or (pos.get("underlying") or {}).get("symbol"),
        "contract": (pos.get("instrument") or {}).get("name"), "direction": "BULLISH" if bull else "BEARISH", "grade": pos.get("grade"),
        "counterTrend": counter, "status": "CLOSED" if closed else "OPEN", "qty": qty, "entry": entry, "exit": exit_fill,
        "exitReason": reason or None, "openedTs": fill_ts, "closedTs": exit_ts, "signalTs": signal_ts,
        "net": net, "gross": gross, "charges": charges, "r": _f((trade or {}).get("r_multiple")),
        "mfeR": round(mfe_r, 3), "maeR": round(mae_r, 3), "maxDdR": _f(pos.get("max_dd_r")),
        "stock": {"triggerClose": trig_close, "atFill": stock_at_fill, "atExit": stock_at_exit, "entry": eq_entry, "stop": eq_sl,
                  "targets": eq_targets, "atr": atr, "bestAtr": in_atr(best_fav), "worstAtr": in_atr(worst_adv),
                  "hitStop": stock_hit_stop, "stopHitTs": stop_hit_ts, "laterBestAtr": in_atr(later_fav), "laterReachedT1": later_t1,
                  "laterHitStopTs": later_stop_ts},
        "option": {"entry": entry, "stop": _f(pos.get("initial_option_sl") or pos.get("option_sl")), "targets": pos.get("option_targets"),
                   "delta": delta, "excessPct": excess, "ref": ref_opt, "midAtPlace": mid_at_place},
        "execution": {"waitS": wait_s, "reprices": len(ent.get("reprices") or []), "entrySlipInr": entry_slip, "exitSlipInr": exit_slip,
                      "stop": stop_rec or None, "entryOutcome": ent.get("outcome")},
        "levels": {"ahead": ahead, "all": levels},
        "checks": checks,
        "cause": {"key": cause, "label": ROOT_CAUSE_LABEL[cause], "why": why, "contributing": contributing},
        "mirrors": mirrors,
        "chart": chart,
    }


#: the checks a cause already speaks for (not repeated as contributing)
_CAUSE_CHECK: dict[str, tuple[str, ...]] = {
    "tight_stop": ("stop",),
    "pivot_rejection": ("pivot",), "chased": ("entry",), "entry_cost": ("entry",), "exit_cost": ("exit",), "market_against": ("market",), "oi_against": ("oi",), "no_volume": ("volume",),
}


def review_day(
    *, positions: list[dict[str, Any]], trades: list[dict[str, Any]], signals: list[dict[str, Any]], events: list[dict[str, Any]],
    bars_by_symbol: dict[str, list[dict[str, Any]]], session_end: float, labels: dict[str, str], mirror_books: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    """Every trade a trading or shadow book opened in the session (the stop-rule mirrors ride on their source's
    row), each reviewed, and the session's losses grouped by root cause."""
    trade_of = {t.get("position_id"): t for t in trades}
    sig_of = {s.get("signal_id"): s for s in signals}
    ev_of: dict[tuple[str, str], dict[str, Any]] = {}
    for e in events:
        kind = str(e.get("kind") or "")
        if kind in ("regime.breadth", "counter.route", "counter.gap_fade", "counter.market_fade") and e.get("signal_id"):
            ev_of[(kind, e["signal_id"])] = e
    by_key = {(p.get("strategy"), p.get("signal_id"), round(float(p.get("opened_ts") or 0))): p for p in positions}
    out = []
    for p in sorted(positions, key=lambda x: float(x.get("opened_ts") or 0)):
        if p.get("strategy") in mirror_books:
            continue
        sid = str(p.get("signal_id") or "")
        parent = parent_signal_id(sid)
        mirrors = []
        for mk, (src, rule) in mirror_books.items():
            if src != p.get("strategy"):
                continue
            m = by_key.get((mk, sid, round(float(p.get("opened_ts") or 0))))
            if m is None:
                continue
            mt = trade_of.get(m.get("id"))
            mirrors.append({"rule": rule, "book": mk, "status": m.get("status"), "exitReason": m.get("exit_reason"),
                            "exitPrice": m.get("exit_price"), "net": mt.get("net") if mt else None})
        fade = ev_of.get(("counter.gap_fade", parent)) or ev_of.get(("counter.market_fade", parent))
        out.append(review_trade(
            p, trade=trade_of.get(p.get("id")), signal=sig_of.get(parent) or sig_of.get(sid),
            breadth=ev_of.get(("regime.breadth", parent)), route=ev_of.get(("counter.route", parent)),
            fade=fade if p.get("strategy") in COUNTER_TREND_BOOKS else None,
            bars=bars_by_symbol.get(str(p.get("symbol") or ""), []), mirrors=mirrors, session_end=session_end,
            label=labels.get(str(p.get("strategy")), str(p.get("strategy"))),
        ))
    causes: dict[str, dict[str, Any]] = {}
    for r in out:
        if r["cause"]["key"] in ("win", "open"):
            continue
        c = causes.setdefault(r["cause"]["key"], {"key": r["cause"]["key"], "label": r["cause"]["label"], "trades": 0, "net": 0.0})
        c["trades"] += 1
        c["net"] += r["net"] or 0.0
    closed = [r for r in out if r["status"] == "CLOSED"]
    return {
        "trades": out, "thresholds": THRESHOLDS,
        "summary": {"trades": len(out), "closed": len(closed), "open": len(out) - len(closed),
                    "losses": sum(1 for r in closed if (r["net"] or 0) < 0), "wins": sum(1 for r in closed if (r["net"] or 0) >= 0),
                    "net": round(sum(r["net"] or 0.0 for r in closed), 2),
                    "causes": sorted(causes.values(), key=lambda c: c["net"])},
    }
