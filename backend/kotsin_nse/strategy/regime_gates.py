"""The regime gates and the labels that go with them — pure: context in, verdicts out.

Gate B (operator, 2026-09-26) keeps RT-Y out of a trigger when the market is not with it
(breadth), when a key pivot sits just ahead of the close, or when a 09:45 trigger gapped its own
way; CT-Y fades that last one. The engine gates with these, the trigger cards label every book with
them, and the Shadow page tabulates them — one definition, so the three can never disagree.
"""

from __future__ import annotations

from typing import Any

from ..risk.limits import CT_M_MARKET_AGAINST_MAX, RiskLimits

#: The gap the gap switch reads (``RiskLimits.gap_switch_rel_vol``): a 09:45 trigger that gapped this many daily
#: ATRs its own way — RT-Y's gap gate and CT-Y's gap fade threshold.
GAP_SWITCH_GAP_DATR = 0.3


def rule_gap(ctx: dict[str, Any] | None) -> float | None:
    """The 09:45 gap the gap rules read — CT-Y's gap fade, CT-Y-F, the gap switch, RT-Y / RT-Y-F's gap skip: ``gapDatr``
    (the open of the first 30m bar, the first continuous trade) as always, or ``gapOfficial`` (the pre-open print against
    the closing-auction close) when the trigger was logged under KN_GAP_SOURCE=official — its context says which in
    ``gapRule`` (CTB-19Oct, 2026-10-19). A context logged without it reads ``gapDatr``."""
    ctx = ctx or {}
    return ctx.get("gapOfficial") if ctx.get("gapRule") == "official" else ctx.get("gapDatr")


def gap_switch(ctx: dict[str, Any] | None, lim: RiskLimits) -> str | None:
    """Why a 09:45 gap is followed in trend by a book with the gap switch — its volume at least
    ``gap_switch_rel_vol`` times the NIFTY50's — or None (operator, 2026-10-06)."""
    ctx = ctx or {}
    x, gap, rv = lim.gap_switch_rel_vol, rule_gap(ctx), ctx.get("relVol")
    if x is None or not ctx.get("openBar") or gap is None or gap < GAP_SWITCH_GAP_DATR or rv is None or rv < x:
        return None
    return f"gap switch: 09:45 gap {gap:.2f} daily ATR its own way on {rv:.1f}x the NIFTY50's volume (≥ {x:g}x) — followed in trend"


def gap_fade_volume_block(ctx: dict[str, Any] | None, lim: RiskLimits) -> str | None:
    """Why a fade book does not fade a 09:45 gap: its volume at least ``gap_fade_max_rel_vol`` times the
    NIFTY50's. A volume that cannot be read never blocks."""
    x, rv = lim.gap_fade_max_rel_vol, (ctx or {}).get("relVol")
    if x is None or rv is None or rv < x:
        return None
    return f"volume {rv:.1f}x the NIFTY50's (≥ {x:g}x) — the gap is followed in trend, not faded"


def rt_gate_reasons(ctx: dict[str, Any] | None, lim: RiskLimits) -> list[tuple[str, str]]:
    """Why a gated in-trend book stands aside from a trigger, read from the context logged AT the
    trigger: breadth, a key pivot just ahead, a 09:45 gap its own way (gate B, operator 2026-09-26).
    Empty = take it. A measure that was never taken never blocks. A 09:45 gap on the volume of the
    gap switch (``gap_switch``) stands aside for neither a pivot nor the gap."""
    ctx = ctx or {}
    out: list[tuple[str, str]] = []
    share = ctx.get("share")
    if lim.breadth_min is not None and share is not None and share <= lim.breadth_min:
        out.append(("breadth", f"breadth {share:.0%} of {ctx.get('names')} names agree ≤ {lim.breadth_min:.0%} — the market is not with the breakout"))
    if gap_switch(ctx, lim) is not None:
        return out
    if lim.skip_pivot_ahead_atr is not None:
        if "pivotsAheadAtr" in ctx:
            near = [f"{lab} +{round(d, 2):g} ATR" for lab, d in ctx["pivotsAheadAtr"] if d <= lim.skip_pivot_ahead_atr]
        else:  # a context logged before the distances were kept: the 0.5-ATR labels
            near = list(ctx.get("pivotsAhead") or [])
        if near:
            out.append(("pivot_ahead", "pivot just ahead: " + ", ".join(near[:3]) + f" (within {lim.skip_pivot_ahead_atr:g} ATR30)"))
    gap = rule_gap(ctx)
    if lim.skip_open_gap_datr is not None and ctx.get("openBar") and gap is not None and gap >= lim.skip_open_gap_datr:
        out.append(("open_gap", f"09:45 trigger gapped {gap:.2f} daily ATR its own way (≥ {lim.skip_open_gap_datr:g}) — the first-bar trap"))
    return out


#: RT-Y's gate B, and the refusals that mean the entry was attempted and not filled
GATE_B = frozenset({"breadth", "pivot_ahead", "open_gap"})
MISSED_GATES = frozenset({"missed", "stop_breached", "not_filled", "order_refused"})


def trigger_verdicts(
    sgn: dict[str, Any], evs: list[dict[str, Any]], *, fade_x: dict[str, Any] | None, gap_fade: dict[str, Any] | None,
    rt_y_held: bool, lim_y: RiskLimits,
) -> dict[str, Any]:
    """What RT-Y and CT-Y do with one trigger: what happened where it did (a skip event, a
    position, a gap-fade event), what the rules WOULD do otherwise, from the context logged at the
    trigger. Volume is information only — the Sep replay showed no edge in it."""
    ctx = next((e for e in reversed(evs) if e.get("kind") == "regime.breadth"), None)
    skip = next((e for e in reversed(evs) if e.get("kind") == "rt_twin.skipped" and e.get("book") == "FUDKII_RT_Y"), None)
    would = rt_gate_reasons(ctx, lim_y)
    if skip is not None:
        # what actually happened, named as it happened (audit, 2026-09-26: a missed limit, a stop
        # already breached or an empty purse all read as a gate-B SKIP)
        rt_y: dict[str, Any] = _rt_y_skip_verdict(skip)
    elif rt_y_held:
        rt_y = {"action": "TAKE", "state": "taken", "gate": None, "why": []}
    elif would:
        rt_y = {"action": "SKIP", "state": "would skip", "gate": would[0][0], "why": [w for _, w in would]}
    else:
        rt_y = {"action": "TAKE", "state": "would take", "gate": None, "why": []}
    if rt_y["action"] == "TAKE":
        if ctx is None:
            rt_y["why"].append("no context logged")
        else:
            share = ctx.get("share")
            if share is not None:
                rt_y["why"].append(f"breadth {share:.0%} agree")
            if (switch := gap_switch(ctx, lim_y)) is not None:
                rt_y["why"].append(switch)
            elif not ctx.get("pivotsAhead"):
                rt_y["why"].append("no key pivot within 0.5 ATR ahead")
    fade_side = "PE" if sgn.get("direction") == "BULLISH" else "CE"
    gap_ev = next((e for e in reversed(evs) if e.get("kind") == "counter.gap_fade"), None)
    route = next((e for e in reversed(evs) if e.get("kind") == "counter.route"), None)
    if gap_ev is not None and not gap_ev.get("blocked"):
        ct_y = {"action": "GAP FADE", "side": gap_ev.get("side") or fade_side, "stop": gap_ev.get("stop"), "targets": gap_ev.get("targets") or [],
                "rr": gap_ev.get("rr"), "grade": gap_ev.get("grade"),
                "why": f"09:45 gap {float(gap_ev.get('gapDatr') or 0):.2f} daily ATR with the trigger — CT-Y fades it, "
                       f"stop {gap_ev.get('stopNote') or '1 ATR30 past the close'}"}
    elif gap_fade is not None:
        ct_y = {"action": "GAP FADE", "side": fade_side, "stop": gap_fade.get("stop"), "targets": gap_fade.get("targets") or [],
                "rr": gap_fade.get("rr"), "grade": gap_fade.get("grade"), "why": str(gap_fade.get("reason") or "")}
    elif fade_x is not None:
        ct_y = {"action": "FADE", "side": fade_side, "stop": fade_x.get("stop"), "targets": fade_x.get("targets") or [],
                "rr": fade_x.get("rr"), "grade": fade_x.get("grade"), "why": "routed COUNTER-TREND — CT-X's fade, mirrored into CT-Y"}
    else:
        no_plan = next((e for e in reversed(evs) if e.get("kind") == "counter.no_plan"), None)
        if gap_ev is not None:
            why = f"gap fade blocked: {gap_ev.get('blocked')}"
        elif route is None:
            why = "no counter-trend route recorded"
        elif route.get("route") != "COUNTER":
            why = "in trend — no fade"
        else:
            why = "routed COUNTER-TREND, but " + str((no_plan or {}).get("reason") or "no fade plan could be made")
        ct_y = {"action": "NONE", "side": fade_side, "stop": None, "targets": [], "rr": (no_plan or {}).get("rr"),
                "grade": (no_plan or {}).get("grade"), "why": why}
    return {"rtY": rt_y, "ctY": ct_y, "ctM": market_fade_verdict(evs, ctx, fade_side)}


def _rt_y_skip_verdict(skip: dict[str, Any]) -> dict[str, Any]:
    gate = str(skip.get("gate") or "")
    action, state = (
        ("SKIP", "gate B") if gate in GATE_B else
        ("SKIP", "graded F") if gate == "graded_f" else
        ("SKIP", "dried volume") if gate == "dried_volume" else
        ("MISSED", "missed") if gate in MISSED_GATES else
        ("NOT TAKEN", gate.replace("_", " ") or "refused")
    )
    return {"action": action, "state": state, "gate": gate, "why": [str(skip.get("reason") or "")]}


def graded_f_verdicts(sgn: dict[str, Any], evs: list[dict[str, Any]], *, rt_y_held: bool, lim_y: RiskLimits) -> dict[str, Any]:
    """What RT-Y, CT-Y and CT-M do with a trigger FUDKII grades F (operator, 2026-10-06: every RT and CT book is offered
    one and decides for itself): RT-Y takes it only through the gap switch; CT-Y fades only published triggers — its
    graded-F side is the CT-Y-F shadow's light-volume gap fade, shown in its place; CT-M fades published triggers only."""
    ctx = next((e for e in reversed(evs) if e.get("kind") == "regime.breadth"), None)
    skip = next((e for e in reversed(evs) if e.get("kind") == "rt_twin.skipped" and e.get("book") == "FUDKII_RT_Y"), None)
    if rt_y_held:
        rt_y: dict[str, Any] = {"action": "TAKE", "state": "taken", "gate": None, "why": [w for w in [gap_switch(ctx, lim_y)] if w]}
    elif skip is not None:
        rt_y = _rt_y_skip_verdict(skip)
    else:
        rt_y = {"action": "NONE", "state": "not decided", "gate": None, "why": ["no decision recorded on this graded-F trigger"]}
    fade_side = "PE" if sgn.get("direction") == "BULLISH" else "CE"
    blank = {"side": fade_side, "stop": None, "targets": [], "rr": None, "grade": None}
    f_ev = next((e for e in reversed(evs) if e.get("kind") == "counter.gap_fade_f"), None)
    f_skip = next((e for e in reversed(evs) if e.get("kind") == "rt_twin.skipped" and e.get("book") == "FUDKII_CT_Y_F"), None)
    if f_ev is not None and not f_ev.get("blocked"):
        ct_y = {"action": "GAP FADE via CT-Y-F", "side": f_ev.get("side") or fade_side, "stop": f_ev.get("stop"),
                "targets": f_ev.get("targets") or [], "rr": f_ev.get("rr"), "grade": f_ev.get("grade"),
                "why": f"graded F: the CT-Y-F shadow fades the 09:45 gap {float(f_ev.get('gapDatr') or 0):.2f} daily ATR on light volume, "
                       f"stop {f_ev.get('stopNote') or '1 ATR30 past the close'}"}
    else:
        why = (f"graded F — CT-Y-F: {f_ev['blocked']}" if f_ev is not None else
               f"graded F — CT-Y-F: {f_skip.get('reason')}" if f_skip is not None else
               "graded F — CT-Y fades only published triggers")
        ct_y = {**blank, "action": "NONE", "why": why}
    ct_m = {**blank, "action": "NONE", "breadth": (ctx or {}).get("share"), "why": "graded F — CT-M fades only published triggers"}
    return {"rtY": rt_y, "ctY": ct_y, "ctM": ct_m}


def market_fade_verdict(evs: list[dict[str, Any]], ctx: dict[str, Any] | None, fade_side: str) -> dict[str, Any]:
    """What FUDKII-CT-M, the market-against fade shadow (operator, 2026-10-03), does with one trigger:
    its fade (``counter.market_fade``), its skip with the share (``rt_twin.skipped``), or — for a trigger
    from before the book existed — what its rule WOULD do from the breadth logged at the trigger."""
    fade = next((e for e in reversed(evs) if e.get("kind") == "counter.market_fade"), None)
    skip = next((e for e in reversed(evs) if e.get("kind") == "rt_twin.skipped" and e.get("book") == "FUDKII_CT_M"), None)
    share = (ctx or {}).get("share")
    blank = {"side": fade_side, "stop": None, "targets": [], "rr": None, "grade": None}
    if fade is not None:
        b = float(fade.get("breadth") or 0.0)
        return {"action": "FADE", "side": fade.get("side") or fade_side, "stop": fade.get("stop"), "targets": fade.get("targets") or [],
                "rr": fade.get("rr"), "grade": fade.get("grade"), "breadth": b,
                "why": f"market against the trigger: {b:.0%} agree ≤ {CT_M_MARKET_AGAINST_MAX:.0%} — CT-M fades it, stop 1 ATR30 past the close"}
    if skip is not None:
        return {**blank, "action": "NONE", "breadth": skip.get("breadth"), "why": str(skip.get("reason") or "")}
    if share is None:
        return {**blank, "action": "NONE", "breadth": None, "why": "no breadth logged at the trigger"}
    if share <= CT_M_MARKET_AGAINST_MAX:
        return {**blank, "action": "WOULD FADE", "breadth": share,
                "why": f"{share:.0%} agree ≤ {CT_M_MARKET_AGAINST_MAX:.0%} — CT-M's rule would fade it"}
    return {**blank, "action": "NONE", "breadth": share, "why": f"market not against the trigger: {share:.0%} agree > {CT_M_MARKET_AGAINST_MAX:.0%}"}


#: the RT books' dried-volume threshold, and the surge level the Sep replay flagged
VOLUME_DRIED_V = 0.85
VOLUME_SURGE_X = 1.5


def volume_labels(ctx: dict[str, Any]) -> dict[str, Any]:
    """Three volume readings on a trigger, logged for the Shadow page's Volume tab — none gates.

    * ``volDried`` — the RT books' current rule: the trigger bar AND the one before under 0.85x
      the stock's own T-2…T-7 baseline (15:15 closing-auction bars excluded).
    * ``volDriedRel`` — the same rule on the stock's surge divided by the market's median surge at
      that bar: is THIS stock quiet, or is the whole tape quiet?
    * ``volSurge`` — the trigger bar at 1.5x its baseline or more.

    Sep 1-25 replay (RT-Y option %, before costs): the current rule's skips averaged +1.24 % against
    −0.46 % for the rest, but inside gate B's takes +0.61 % against +2.38 %; the market-relative
    version behaved the same (+1.32 % / −0.42 %); a surge averaged −1.24 % against +1.27 %
    (−0.71 / −1.84 in the two halves). The future's volume could be read for 67 triggers only."""
    t, t1 = ctx.get("volSurgeT"), ctx.get("volSurgeT1")
    mt, mt1 = ctx.get("mktSurgeT"), ctx.get("mktSurgeT1")
    out: dict[str, Any] = {}
    if t is not None and t1 is not None:
        out["volDried"] = bool(0 < t < VOLUME_DRIED_V and 0 < t1 < VOLUME_DRIED_V)
        out["volSurge"] = bool(t >= VOLUME_SURGE_X)
        if mt and mt1:
            rt, rt1 = t / mt, t1 / mt1
            out["volDriedRel"] = bool(0 < rt < VOLUME_DRIED_V and 0 < rt1 < VOLUME_DRIED_V)
    return out
