"""The Shadow page: every rule under test, one tab each, and what each book did.

Operator, 2026-09-26: "create a page for all shadow items and [the RT-Y A/B daily table] named
Shadow", then "each shadow in a different tab and name it well and explain the logic and pros cons
and what are we testing in the shadow against and for in the beginning of the list". A tab is one
``ShadowTab`` in ``TABS``: an id (the #anchor), a title, a ``Brief`` (what is tested, for, against,
the logic, pros, cons, how we decide) and a renderer over one ``ShadowData``. A new shadow is one
more entry in ``TABS``. Pure: ledger rows in, rows and HTML out; always available locally, no
ticket; the .xlsx is read off the same page, so every tab's tables and briefs are in it.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..market.session import IST
from ..risk.limits import RiskLimits
from ..strategy.keys import SHADOW_BOOKS, STOP_MIRRORS, StrategyKey, stop_mirrors_of
from ..strategy.regime_gates import GATE_B as GATE_B_GATES
from ..strategy.regime_gates import trigger_verdicts
from .daybook import _CSS, _fmt, contract_label, render_ab

#: the books whose status each trigger row shows, in the order the cards show them
BOOKS = ("FUDKII", "FUDKII_RT_X", "FUDKII_RT_N", "FUDKII_RT_Y", "FUDKII_CT_X", "FUDKII_CT_Y", "FUDKII_CT_M", "FUDKII_RT_MCX")
LABELS = {"FUDKII": "FUDKII", "FUDKII_RT_X": "RT-X", "FUDKII_RT_N": "RT-N", "FUDKII_RT_Y": "RT-Y",
          "FUDKII_CT_X": "CT-X", "FUDKII_CT_Y": "CT-Y", "FUDKII_CT_M": "CT-M", "FUDKII_RT_MCX": "RT-MCX",
          "FUDKII_RT_Y_F": "RT-Y-F", "FUDKII_RT_Y_W1": "RT-Y-W1"}
COUNTER = ("FUDKII_CT_X", "FUDKII_CT_Y")


def shadow_rows(
    *, signals: list[dict], positions: list[dict], trades: list[dict], events: list[dict], lim_y: RiskLimits
) -> list[dict[str, Any]]:
    """One row per FUDKII trigger: its labels, both verdicts, and each book's position on it."""
    latest: dict[str, dict] = {}
    for s in signals:  # a take re-enters the same id: the last row is the one that stands
        latest[s["signal_id"]] = s
    # RT-Y's gate-B A/B is about the triggers RT-Y judges: one FUDKII did not publish (NOT_PUBLISHED,
    # 2026-09-28) reaches only the graded-F shadow (its own tab) and is not a row of it
    parents = sorted((s for s in latest.values() if s.get("strategy") == "FUDKII" and s.get("decision") != "NOT_PUBLISHED"),
                     key=lambda s: s["ts"])
    fade_x_by = {s["source_signal_id"]: s for s in latest.values() if s.get("source_signal_id") and s.get("strategy") == "FUDKII_CT_X"}
    own_by = {(s["strategy"], s["source_signal_id"]): s for s in latest.values() if s.get("source_signal_id")}
    pos_by = {(p["strategy"], p["signal_id"]): p for p in positions}
    net_by = {t.get("position_id"): float(t.get("net") or 0.0) for t in trades}
    ev_by: dict[str, list[dict]] = {}
    for e in events:
        if e.get("signal_id"):
            ev_by.setdefault(e["signal_id"], []).append(e)

    out: list[dict[str, Any]] = []
    for sg in parents:
        sid = sg["signal_id"]
        evs = ev_by.get(sid, [])
        ctx = next((e for e in reversed(evs) if e.get("kind") == "regime.breadth"), None)
        fade_x = fade_x_by.get(sid)
        gap_fade = own_by.get(("FUDKII_CT_Y", sid))
        books: dict[str, dict[str, Any]] = {}
        for b in BOOKS:
            if b in COUNTER:
                ref = gap_fade if (b == "FUDKII_CT_Y" and gap_fade is not None) else fade_x
                hit = pos_by.get((b, ref["signal_id"])) if ref else None
            else:
                hit = pos_by.get((b, sid))
                if hit is None and (own := own_by.get((b, sid))) is not None:
                    hit = pos_by.get((b, own["signal_id"]))
            status = "NONE" if hit is None else ("OPEN" if hit.get("status") == "OPEN" else "EXITED")
            books[b] = {"status": status, "net": net_by.get(hit["id"]) if hit is not None and status == "EXITED" else None}
        verdicts = trigger_verdicts(sg, evs, fade_x=fade_x, gap_fade=gap_fade,
                                    rt_y_held=books["FUDKII_RT_Y"]["status"] != "NONE", lim_y=lim_y)
        ctx = ctx or {}
        out.append({
            "signal_id": sid, "symbol": sg.get("symbol"), "direction": sg.get("direction"),
            "fired": float(sg.get("created_ts") or (float(sg["ts"]) + 1800)), "rr": sg.get("rr"), "decision": sg.get("decision"),
            "breadth": ctx.get("share"), "names": ctx.get("names"), "efficiency": ctx.get("efficiency"), "volBand": ctx.get("volBand"),
            "gapDatr": ctx.get("gapDatr"), "openBar": ctx.get("openBar"), "pivotsAhead": list(ctx.get("pivotsAhead") or []),
            "logged": bool(ctx), "rtY": verdicts["rtY"], "ctY": verdicts["ctY"], "ctM": verdicts["ctM"], "books": books,
            "volSurgeT": ctx.get("volSurgeT"), "volSurgeT1": ctx.get("volSurgeT1"), "mktSurgeT": ctx.get("mktSurgeT"),
            "mktSurgeT1": ctx.get("mktSurgeT1"), "volDried": ctx.get("volDried"), "volDriedRel": ctx.get("volDriedRel"),
            "volSurge": ctx.get("volSurge"),
        })
    return out


@dataclass(frozen=True, slots=True)
class Brief:
    """What a tab opens with — written from what the code does, so it can be held to it."""

    name: str
    testing: str
    for_rule: str
    against: str
    logic: str
    pros: tuple[str, ...]
    cons: tuple[str, ...]
    decide: str


@dataclass(slots=True)
class ShadowData:
    """Everything the tabs render: one session's trigger rows, and the running tallies since the
    A/B began (2026-09-28)."""

    day: date
    days: list[str]
    rows: list[dict[str, Any]]
    ab: dict[str, Any]
    wide: dict[str, Any] = field(default_factory=dict)
    graded_f: dict[str, Any] = field(default_factory=dict)
    gap: dict[str, Any] = field(default_factory=dict)
    market_fade: dict[str, Any] = field(default_factory=dict)
    fukaa: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, Any] = field(default_factory=dict)
    volume: dict[str, Any] = field(default_factory=dict)
    stop_rules: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShadowTab:
    id: str
    title: str
    brief: Brief
    render: Callable[[ShadowData], str]


def _ist(ts: float | None) -> str:
    return datetime.fromtimestamp(float(ts), IST).strftime("%d %b %H:%M") if ts else "—"


def _inr(v: float | None) -> str:
    return "—" if v is None else f"{'−' if v < 0 else '+'}₹{abs(v):,.0f}"


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.0f}%" if d else "—"


def _book_cell(b: dict[str, Any]) -> str:
    if b["status"] == "NONE":
        return '<td class="dim">—</td>'
    if b["status"] == "OPEN":
        return '<td><span class="chip blocked">open</span></td>'
    cls = "neg" if (b["net"] or 0) < 0 else "pos"
    return f'<td class="{cls}">{_inr(b["net"])}</td>'


def _net_cell(v: float | None, status: str | None = None) -> str:
    if status == "OPEN":
        return '<td><span class="chip blocked">open</span></td>'
    if v is None:
        return '<td class="dim">—</td>'
    return f'<td class="{"neg" if v < 0 else "pos"}">{_inr(v)}</td>'


# -- the wide-stop shadow ------------------------------------------------------------------------

WIDE_BOOK = "FUDKII_RT_Y_W1"
#: RT-Y's option stop is capped at 25 % of the premium from this session; the wide-stop shadow's is
#: not (operator, 2026-09-28) — from here the pair differs in the cap as well as the 1 % buffer
RT_Y_CAP_FROM = date(2026, 9, 29)


def wide_stop_summary(*, positions: list[dict], trades: list[dict]) -> dict[str, Any]:
    """RT-Y against its wide-stop shadow, trade by trade: the shadow opens only on RT-Y's own
    entries, so each shadow position has exactly one RT-Y position on the same trigger."""
    trade_by = {t.get("position_id"): t for t in trades}
    y_by = {p["signal_id"]: p for p in positions if p.get("strategy") == "FUDKII_RT_Y"}

    def side(p: dict[str, Any] | None) -> dict[str, Any]:
        if p is None:
            return {"status": "NONE", "net": None, "reason": None, "closed": None, "stop": None}
        t = trade_by.get(p["id"])
        closed = p.get("status") != "OPEN"
        return {
            "status": "EXITED" if closed else "OPEN",
            "net": float(t["net"]) if (closed and t is not None) else None,
            "reason": (t or {}).get("exit_reason") or p.get("exit_reason"),
            "closed": p.get("closed_ts"), "stop": p.get("equity_sl"),
        }

    pairs = []
    for w in sorted((p for p in positions if p.get("strategy") == WIDE_BOOK), key=lambda p: float(p.get("opened_ts") or 0)):
        y = y_by.get(w["signal_id"])
        ys, ws = side(y), side(w)
        diff = ws["net"] - ys["net"] if (ws["net"] is not None and ys["net"] is not None) else None
        pairs.append({
            "signal_id": w["signal_id"], "symbol": w.get("symbol") or (w.get("underlying") or {}).get("symbol"),
            "opened": w.get("opened_ts"), "contract": (w.get("instrument") or {}).get("name"), "entry": w.get("entry"),
            "direction": w.get("direction"), "y": ys, "w": ws, "diff": diff,
        })
    done = [p for p in pairs if p["diff"] is not None]
    total = {
        "pairs": len(pairs), "closed": len(done),
        "y_net": sum(p["y"]["net"] for p in done), "w_net": sum(p["w"]["net"] for p in done),
        "y_win": sum(1 for p in done if p["y"]["net"] > 0), "w_win": sum(1 for p in done if p["w"]["net"] > 0),
        "better": sum(1 for p in done if p["diff"] > 0), "worse": sum(1 for p in done if p["diff"] < 0),
        "same": sum(1 for p in done if p["diff"] == 0),
        "y_worst": min((p["y"]["net"] for p in done), default=None), "w_worst": min((p["w"]["net"] for p in done), default=None),
    }
    total["diff"] = total["w_net"] - total["y_net"]
    cap_ts = datetime(RT_Y_CAP_FROM.year, RT_Y_CAP_FROM.month, RT_Y_CAP_FROM.day, tzinfo=IST).timestamp()
    capped = [p for p in done if float(p["opened"] or 0) >= cap_ts]
    since_cap = {
        "from": RT_Y_CAP_FROM.isoformat(), "closed": len(capped),
        "y_net": sum(p["y"]["net"] for p in capped), "w_net": sum(p["w"]["net"] for p in capped),
        "y_win": sum(1 for p in capped if p["y"]["net"] > 0), "w_win": sum(1 for p in capped if p["w"]["net"] > 0),
        "better": sum(1 for p in capped if p["diff"] > 0), "worse": sum(1 for p in capped if p["diff"] < 0),
        "same": sum(1 for p in capped if p["diff"] == 0),
    }
    since_cap["diff"] = since_cap["w_net"] - since_cap["y_net"]
    return {"pairs": pairs, "total": total, "since_cap": since_cap}


# -- the stop rules: every book against its two mirrors ----------------------------------------------

#: the books that have mirrors, in the order the tables list them
STOP_RULE_BOOKS = tuple(dict.fromkeys(src.value for src, _r in STOP_MIRRORS.values()))
_STOP_EXITS = ("SL-EQ", "SL-OP")


def stop_rules_summary(*, positions: list[dict], trades: list[dict]) -> dict[str, Any]:
    """Each book against its two stop-rule mirrors, trade by trade (operator, 2026-10-04: "compare which
    [stop rule] works best with which strategy"): a mirror opens only on its book's own fill, so each
    mirror position has exactly one source position on the same trigger. Per book and rule: the trades
    closed under all three rules, their net, wins, stops taken, and each mirror against the current stop;
    beside them the trades still OPEN under that rule and those WAITING (closed under it, open under
    another — counted once closed under all three). ``total`` sums every book, ``total_trading`` the
    trading books alone (a shadow book's trades are not the strategy's)."""
    net_by = {t.get("position_id"): float(t.get("net") or 0.0) for t in trades}
    reason_by = {t.get("position_id"): t.get("exit_reason") for t in trades}
    by_key = {(p.get("strategy"), p.get("signal_id")): p for p in positions}

    def side(p: dict[str, Any] | None) -> dict[str, Any]:
        if p is None:
            return {"status": "NONE", "net": None, "reason": None}
        if p.get("status") == "OPEN":
            return {"status": "OPEN", "net": None, "reason": None}
        return {"status": "EXITED", "net": net_by.get(p["id"]), "reason": reason_by.get(p["id"]) or p.get("exit_reason")}

    trades_out: list[dict[str, Any]] = []
    books: dict[str, dict[str, dict[str, Any]]] = {}
    for book in STOP_RULE_BOOKS:
        mirrors = stop_mirrors_of(book)
        tally = {rule: {"closed": 0, "net": 0.0, "wins": 0, "stops": 0, "better": 0, "worse": 0, "same": 0, "diff": 0.0,
                        "open": 0, "waiting": 0}
                 for rule in ("current", *mirrors)}
        for p in sorted((x for x in positions if x.get("strategy") == book), key=lambda x: float(x.get("opened_ts") or 0)):
            sid = p.get("signal_id")
            row = {"current": side(p), **{rule: side(by_key.get((m.value, sid))) for rule, m in mirrors.items()}}
            if all(r["status"] == "NONE" for k, r in row.items() if k != "current"):
                continue  # before the mirrors existed: not a comparison
            done = all(r["status"] == "EXITED" and r["net"] is not None for r in row.values())
            trades_out.append({"book": book, "signal_id": sid, "symbol": p.get("symbol"), "opened": p.get("opened_ts"),
                               "contract": (p.get("instrument") or {}).get("name"), "entry": p.get("entry"), "rules": row, "closed": done})
            if not done:
                for rule, r in row.items():
                    if r["status"] != "NONE":
                        tally[rule]["open" if r["status"] == "OPEN" else "waiting"] += 1
                continue
            cur = row["current"]["net"]
            for rule, r in row.items():
                t = tally[rule]
                t["closed"] += 1
                t["net"] += r["net"]
                t["wins"] += r["net"] > 0
                t["stops"] += r["reason"] in _STOP_EXITS
                if rule != "current":
                    d = r["net"] - cur
                    t["diff"] += d
                    t["better" if d > 1 else "worse" if d < -1 else "same"] += 1
        books[book] = tally
    fields = ("closed", "net", "wins", "stops", "better", "worse", "same", "diff", "open", "waiting")

    def tot(among: list[str]) -> dict[str, dict[str, float]]:
        return {rule: {k: sum(books[b][rule][k] for b in among) for k in fields} for rule in ("current", "E", "A")}

    trading = [b for b in books if StrategyKey(b) not in SHADOW_BOOKS]
    return {"books": books, "total": tot(list(books)), "total_trading": tot(trading), "trades": trades_out}


# -- the graded-F shadow -------------------------------------------------------------------------

GRADED_F_BOOK = "FUDKII_RT_Y_F"


def graded_f_summary(*, signals: list[dict], positions: list[dict], trades: list[dict], events: list[dict]) -> dict[str, Any]:
    """Every trigger FUDKII graded F and did not publish (NOT_PUBLISHED), and what the graded-F
    shadow did with it under RT-Y's rules: traded (its net once closed) or stood aside (the gate
    and why). An MCX trigger is not offered to it and is not a row."""
    latest: dict[str, dict] = {}
    for sg in signals:
        latest[sg["signal_id"]] = sg
    pos_by = {p["signal_id"]: p for p in positions if p.get("strategy") == GRADED_F_BOOK}
    trade_by: dict[str, float] = {}
    for t in trades:
        if t.get("position_id"):
            trade_by[t["position_id"]] = trade_by.get(t["position_id"], 0.0) + float(t.get("net") or 0.0)
    skip_by = {e["signal_id"]: e for e in events
               if e.get("kind") == "rt_twin.skipped" and e.get("book") == GRADED_F_BOOK and e.get("signal_id")}
    offered = {e["signal_id"] for e in events if e.get("kind") == "regime.breadth" and e.get("signal_id")}
    rows = []
    for sg in sorted(latest.values(), key=lambda x: x["ts"]):
        sid = sg["signal_id"]
        if sg.get("strategy") != "FUDKII" or sg.get("decision") != "NOT_PUBLISHED" or sid not in offered:
            continue
        p, sk = pos_by.get(sid), skip_by.get(sid)
        closed = p is not None and p.get("status") != "OPEN"
        rows.append({
            "signal_id": sid, "symbol": sg.get("symbol"), "direction": sg.get("direction"),
            "fired": float(sg.get("created_ts") or (float(sg["ts"]) + 1800)), "why_f": sg.get("decision_reason"),
            "targets": list(sg.get("targets") or []),
            "status": "NONE" if p is None else ("EXITED" if closed else "OPEN"),
            "contract": ((p or {}).get("instrument") or {}).get("name"), "entry": (p or {}).get("entry"),
            "exit_reason": (p or {}).get("exit_reason") if closed else None,
            "net": trade_by.get(p["id"]) if closed else None,
            "skip": (sk or {}).get("reason") if p is None else None, "gate": (sk or {}).get("gate") if p is None else None,
        })
    done = [r for r in rows if r["net"] is not None]
    total = {
        "triggers": len(rows), "traded": sum(1 for r in rows if r["status"] != "NONE"), "closed": len(done),
        "net": sum(r["net"] for r in done), "win": sum(1 for r in done if r["net"] > 0),
        "worst": min((r["net"] for r in done), default=None), "best": max((r["net"] for r in done), default=None),
    }
    return {"rows": rows, "total": total}


# -- the 09:45 gap fade --------------------------------------------------------------------------


def gap_fade_summary(*, signals: list[dict], positions: list[dict], trades: list[dict], events: list[dict]) -> dict[str, Any]:
    """Every trigger the 09:45 gap rule fired on: CT-Y's plan and trade, and what the in-trend
    books (the parent, RT-X, RT-N) made on the same trigger."""
    latest: dict[str, dict] = {}
    for s in signals:
        latest[s["signal_id"]] = s
    own_by = {(s["strategy"], s["source_signal_id"]): s for s in latest.values() if s.get("source_signal_id")}
    pos_by = {(p["strategy"], p["signal_id"]): p for p in positions}
    trade_by = {t.get("position_id"): t for t in trades}
    plans: dict[str, dict] = {}
    for e in events:
        if e.get("kind") == "counter.gap_fade" and e.get("signal_id"):
            plans[e["signal_id"]] = e

    def book(b: str, sid: str | None) -> dict[str, Any]:
        p = pos_by.get((b, sid)) if sid else None
        if p is None:
            return {"status": "NONE", "net": None}
        t = trade_by.get(p["id"])
        closed = p.get("status") != "OPEN"
        return {"status": "EXITED" if closed else "OPEN", "net": float(t["net"]) if (closed and t is not None) else None,
                "pos": p, "reason": (t or {}).get("exit_reason") or p.get("exit_reason")}

    rows = []
    for sid, ev in sorted(plans.items(), key=lambda kv: float((latest.get(kv[0]) or {}).get("ts") or 0)):
        trig = latest.get(sid) or {}
        fade = own_by.get(("FUDKII_CT_Y", sid))
        ct = book("FUDKII_CT_Y", fade["signal_id"] if fade else None)
        cpos = ct.get("pos") or {}
        rows.append({
            "signal_id": sid, "symbol": ev.get("symbol") or trig.get("symbol"), "trigger": trig.get("direction"),
            "fired": float(trig.get("created_ts") or (float(trig["ts"]) + 1800 if trig.get("ts") else 0)) or None,
            "gap": ev.get("gapDatr"), "side": ev.get("side"), "stop": ev.get("stop"), "targets": list(ev.get("targets") or []),
            "rr": ev.get("rr"), "grade": ev.get("grade"), "blocked": ev.get("blocked"),
            "contract": (cpos.get("instrument") or {}).get("name"), "fill": cpos.get("entry"), "ct": ct,
            "parent": book("FUDKII", sid), "x": book("FUDKII_RT_X", sid), "n": book("FUDKII_RT_N", sid),
        })
    done = [r for r in rows if r["ct"]["net"] is not None]

    def tot(k: str) -> float:
        return sum(r[k]["net"] for r in done if r[k]["net"] is not None)

    total = {"fired": len(rows), "blocked": sum(1 for r in rows if r["blocked"]), "closed": len(done),
             "ct_net": tot("ct"), "ct_win": sum(1 for r in done if r["ct"]["net"] > 0),
             "parent_net": tot("parent"), "x_net": tot("x"), "n_net": tot("n")}
    return {"rows": rows, "total": total}


# -- FUDKII-CT-M, the market-against fade ------------------------------------------------------------

MARKET_FADE_BOOK = "FUDKII_CT_M"


def _net_by_position(trades: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trades:
        if t.get("position_id"):
            out[t["position_id"]] = out.get(t["position_id"], 0.0) + float(t.get("net") or 0.0)
    return out


def _book_on(pos_by: dict, net_by: dict, b: str, sid: str | None) -> dict[str, Any]:
    p = pos_by.get((b, sid)) if sid else None
    if p is None:
        return {"status": "NONE", "net": None}
    closed = p.get("status") != "OPEN"
    return {"status": "EXITED" if closed else "OPEN", "net": net_by.get(p["id"]) if closed else None}


def market_fade_summary(*, signals: list[dict], positions: list[dict], trades: list[dict], events: list[dict]) -> dict[str, Any]:
    """Every published NSE trigger FUDKII-CT-M decided on (the market-against fade shadow, operator
    2026-10-03): its fade — the plan, the contract, the fill, the exit and the net — or its skip with the
    share, and what the books that trade the same trigger made on it (RT-X, RT-N, RT-Y, and CT-Y where
    it gap-faded)."""
    latest: dict[str, dict] = {}
    for sg in signals:
        latest[sg["signal_id"]] = sg
    own_by = {(sg["strategy"], sg["source_signal_id"]): sg for sg in latest.values() if sg.get("source_signal_id")}
    pos_by = {(p["strategy"], p["signal_id"]): p for p in positions}
    net_by = _net_by_position(trades)
    fades: dict[str, dict] = {}
    skips: dict[str, dict] = {}
    for e in events:
        if not e.get("signal_id"):
            continue
        if e.get("kind") == "counter.market_fade":
            fades[e["signal_id"]] = e
        elif e.get("kind") == "rt_twin.skipped" and e.get("book") == MARKET_FADE_BOOK:
            skips[e["signal_id"]] = e

    def fired(sid: str) -> float | None:
        trig = latest.get(sid) or {}
        return float(trig.get("created_ts") or (float(trig["ts"]) + 1800 if trig.get("ts") else 0)) or None

    rows = []
    for sid, ev in sorted(fades.items(), key=lambda kv: fired(kv[0]) or 0.0):
        trig = latest.get(sid) or {}
        own = own_by.get((MARKET_FADE_BOOK, sid))
        mp = pos_by.get((MARKET_FADE_BOOK, own["signal_id"])) if own else None
        m = _book_on(pos_by, net_by, MARKET_FADE_BOOK, own["signal_id"] if own else None)
        gap = own_by.get(("FUDKII_CT_Y", sid))
        rows.append({
            "signal_id": sid, "symbol": ev.get("symbol") or trig.get("symbol"), "trigger": trig.get("direction"), "fired": fired(sid),
            "breadth": ev.get("breadth"), "side": ev.get("side"), "stop": ev.get("stop"), "targets": list(ev.get("targets") or []),
            "rr": ev.get("rr"), "grade": ev.get("grade"),
            "contract": ((mp or {}).get("instrument") or {}).get("name"), "fill": (mp or {}).get("entry"),
            "exit_reason": (mp or {}).get("exit_reason") if m["status"] == "EXITED" else None,
            "not_taken": (own or {}).get("decision_reason") if mp is None else None,
            "ct_m": m, "rt_x": _book_on(pos_by, net_by, "FUDKII_RT_X", sid), "rt_n": _book_on(pos_by, net_by, "FUDKII_RT_N", sid),
            "rt_y": _book_on(pos_by, net_by, "FUDKII_RT_Y", sid),
            "ct_y": _book_on(pos_by, net_by, "FUDKII_CT_Y", gap["signal_id"]) if gap else {"status": "NONE", "net": None},
        })
    skipped = sorted(({"signal_id": sid, "symbol": e.get("symbol") or (latest.get(sid) or {}).get("symbol"),
                       "trigger": (latest.get(sid) or {}).get("direction"), "fired": fired(sid),
                       "breadth": e.get("breadth"), "gate": e.get("gate"), "reason": e.get("reason")} for sid, e in skips.items()),
                     key=lambda r: r["fired"] or 0.0)
    done = [r for r in rows if r["ct_m"]["net"] is not None]

    def same(k: str) -> float:
        return sum(r[k]["net"] for r in done if r[k]["net"] is not None)

    shares = [r["breadth"] for r in skipped if r["breadth"] is not None]
    total = {
        "fades": len(rows), "traded": sum(1 for r in rows if r["ct_m"]["status"] != "NONE"), "closed": len(done),
        "net": sum(r["ct_m"]["net"] for r in done), "win": sum(1 for r in done if r["ct_m"]["net"] > 0),
        "best": max((r["ct_m"]["net"] for r in done), default=None), "worst": min((r["ct_m"]["net"] for r in done), default=None),
        "rt_x_net": same("rt_x"), "rt_n_net": same("rt_n"), "rt_y_net": same("rt_y"), "ct_y_net": same("ct_y"),
        "skipped": len(skipped), "near": sum(1 for x in shares if x <= 0.50), "mid": sum(1 for x in shares if 0.50 < x <= 0.60),
        "far": sum(1 for x in shares if x > 0.60),
    }
    return {"rows": rows, "skipped": skipped, "total": total}


# -- FUKAA in shadow -------------------------------------------------------------------------------------


def fukaa_shadow_summary(*, signals: list[dict], positions: list[dict], trades: list[dict], events: list[dict]) -> dict[str, Any]:
    """Every signal FUKAA admitted in SHADOW (operator, 2026-10-02: its inputs fixed, recorded with every
    input, never traded): what it read, whether the market and the OI were with it, and what the books that
    trade the same FUDKII trigger made on it."""
    latest: dict[str, dict] = {}
    for sg in signals:
        latest[sg["signal_id"]] = sg
    own_by = {(sg["strategy"], sg["source_signal_id"]): sg for sg in latest.values() if sg.get("source_signal_id")}
    pos_by = {(p["strategy"], p["signal_id"]): p for p in positions}
    net_by = _net_by_position(trades)
    rows = []
    for e in sorted((e for e in events if e.get("kind") == "fukaa.shadow" and e.get("signal_id")), key=lambda e: float(e.get("ts") or 0)):
        ev, al = e.get("evidence") or {}, e.get("alignment") or {}
        sg = latest.get(e["signal_id"]) or {}
        parent = sg.get("source_signal_id") or ""
        targets = list(e.get("targets") or [])
        ct_m = own_by.get((MARKET_FADE_BOOK, parent))
        rows.append({
            "signal_id": e["signal_id"], "parent": parent, "symbol": e.get("symbol"), "direction": e.get("direction"),
            "fired": float(e.get("ts") or 0) or None, "entry": e.get("entry"), "stop": e.get("stop"), "t1": targets[0] if targets else None,
            "rr": e.get("rr"), "composite": ev.get("composite"), "promoted": bool(ev.get("promoted")),
            "surge": ev.get("surge_used"), "rel_volume": ev.get("rel_volume"), "momentum": ev.get("momentum_score"),
            "oi_change": ev.get("oi_change_pct"), "oi_z": ev.get("oi_rel_z"),
            "breadth": al.get("breadth"), "with_market": al.get("withMarket"), "price_change": al.get("priceChangePct"),
            "oi_quadrant": al.get("oiQuadrant"), "oi_agrees": al.get("oiAgrees"),
            "rt_x": _book_on(pos_by, net_by, "FUDKII_RT_X", parent), "rt_n": _book_on(pos_by, net_by, "FUDKII_RT_N", parent),
            "rt_y": _book_on(pos_by, net_by, "FUDKII_RT_Y", parent),
            "ct_m": _book_on(pos_by, net_by, MARKET_FADE_BOOK, ct_m["signal_id"] if ct_m else None),
        })

    def same(k: str) -> float:
        return sum(r[k]["net"] for r in rows if r[k]["net"] is not None)

    total = {
        "signals": len(rows), "promoted": sum(1 for r in rows if r["promoted"]),
        "with_market": sum(1 for r in rows if r["with_market"] is True), "counter": sum(1 for r in rows if r["with_market"] is False),
        "oi_agrees": sum(1 for r in rows if r["oi_agrees"] is True),
        "rt_x_net": same("rt_x"), "rt_n_net": same("rt_n"), "rt_y_net": same("rt_y"),
        "rt_x_closed": sum(1 for r in rows if r["rt_x"]["net"] is not None),
        "rt_y_closed": sum(1 for r in rows if r["rt_y"]["net"] is not None),
    }
    return {"rows": rows, "total": total}


# -- the labels ----------------------------------------------------------------------------------


def label_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """RT-X's outcome (it takes every trigger the parent fills) bucketed by each label, over the
    triggers since the A/B began — does a label separate winners from losers?"""
    def eff(r: dict[str, Any]) -> str | None:
        e = r.get("efficiency")
        return None if e is None else ("< 0.3 (choppy)" if e < 0.3 else "0.3 – 0.6" if e < 0.6 else "≥ 0.6 (clean)")

    groups: list[tuple[str, Callable[[dict[str, Any]], str | None]]] = [
        ("Breadth", lambda r: None if r.get("breadth") is None else ("> 50% agree" if r["breadth"] > 0.5 else "≤ 50% agree")),
        ("Trend efficiency", eff),
        ("Own-volatility band", lambda r: r.get("volBand")),
        ("Key pivot ≤ 0.5 ATR ahead", lambda r: None if not r.get("logged") else ("yes" if r.get("pivotsAhead") else "no")),
        ("09:45 gap its own way ≥ 0.3 dATR", lambda r: None if not r.get("logged") else (
            "yes" if (r.get("openBar") and (r.get("gapDatr") or 0) >= 0.3) else "no")),
    ]
    out = []
    for name, key in groups:
        buckets: dict[str, dict[str, Any]] = {}
        for r in rows:
            k = key(r)
            if k is None:
                continue
            b = buckets.setdefault(k, {"triggers": 0, "x_n": 0, "x_net": 0.0, "x_win": 0})
            b["triggers"] += 1
            x = r["books"]["FUDKII_RT_X"]
            if x["status"] == "EXITED" and x["net"] is not None:
                b["x_n"] += 1
                b["x_net"] += x["net"]
                b["x_win"] += x["net"] > 0
        out.append({"label": name, "buckets": dict(sorted(buckets.items()))})
    return {"groups": out, "triggers": len(rows)}


# -- rendering -----------------------------------------------------------------------------------


def _brief_html(b: Brief) -> str:
    li = lambda xs: "".join(f"<li>{html.escape(x)}</li>" for x in xs)  # noqa: E731
    return f"""<div class="brief">
<h3 class="bname">{html.escape(b.name)}</h3>
<p><b>What we are testing.</b> {html.escape(b.testing)}</p>
<p><b>For (the rule under test).</b> {html.escape(b.for_rule)}</p>
<p><b>Against (the baseline).</b> {html.escape(b.against)}</p>
<p><b>The logic.</b> {html.escape(b.logic)}</p>
<div class="pc"><div><b>Pros</b><ul>{li(b.pros)}</ul></div><div><b>Cons</b><ul>{li(b.cons)}</ul></div></div>
<p><b>How we decide.</b> {html.escape(b.decide)}</p>
</div>"""


def _triggers_table(rows: list[dict[str, Any]]) -> str:
    head = ["Fired", "Symbol", "Dir", "RR", "Breadth", "Trend eff.", "Own vol", "Gap dATR", "09:45", "Pivots ≤0.5 ATR ahead",
            "RT-Y", "RT-Y why", "CT-Y", "CT-Y plan", "CT-M", "CT-M why", *(LABELS[b] for b in BOOKS)]
    body = []
    for r in rows:
        y, c = r["rtY"], r["ctY"]
        m = r.get("ctM") or {"action": "NONE", "why": "—"}
        m_cls = "blocked" if m["action"] == "FADE" else "dim"
        # a gate's skip, a take, and — neither — a miss or another refusal (review, 2026-09-26: those read as "filled")
        y_cls = {"SKIP": "refused", "TAKE": "filled"}.get(y["action"], "blocked")
        c_cls = "blocked" if c["action"] in ("GAP FADE", "FADE") else "dim"
        breadth = "—" if r["breadth"] is None else f"{r['breadth']:.0%}"
        plan = (f'{c["side"]} · stop {_fmt(c["stop"])} · T1 {_fmt((c["targets"] or [None])[0])} · RR {_fmt(c["rr"])} ({c["grade"] or "—"})'
                if c["action"] != "NONE" else html.escape(c["why"]))
        body.append(
            f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
            f'<td class="{"pos" if r["direction"] == "BULLISH" else "neg"}">{"BULL" if r["direction"] == "BULLISH" else "BEAR"}</td>'
            f'<td>{_fmt(r["rr"])}</td><td>{breadth}</td>'
            f'<td>{_fmt(r["efficiency"])}</td><td class="l">{html.escape(r["volBand"] or "—")}</td><td>{_fmt(r["gapDatr"])}</td>'
            f'<td>{"yes" if r["openBar"] else "—"}</td><td class="l">{html.escape(", ".join(r["pivotsAhead"]) or "—")}</td>'
            f'<td><span class="chip {y_cls}">{html.escape(y["action"].lower() if y["state"] in ("taken", "skipped") else y["state"])}</span></td>'
            f'<td class="why l">{html.escape("; ".join(y["why"]) or "—")}</td>'
            f'<td><span class="chip {c_cls}">{html.escape(c["action"].lower())}</span></td><td class="why l">{plan}</td>'
            f'<td><span class="chip {m_cls}">{html.escape(m["action"].lower())}</span></td><td class="why l">{html.escape(str(m.get("why") or "—"))}</td>'
            + "".join(_book_cell(r["books"][b]) for b in BOOKS) + "</tr>"
        )
    left = ("Symbol", "Own vol", "Pivots ≤0.5 ATR ahead", "RT-Y why", "CT-Y plan", "CT-M why")
    return (
        f'<div class="scroll"><table><thead><tr>{"".join(f"<th class=l>{h}</th>" if h in left else f"<th>{h}</th>" for h in head)}</tr></thead>'
        f'<tbody>{"".join(body) or f"<tr><td colspan={len(head)} class=dim>no triggers on this day</td></tr>"}</tbody></table></div>'
    )


def _gate_b_skip(r: dict[str, Any]) -> bool:
    """RT-Y stood aside on gate B (breadth, a pivot ahead, the open gap) — not on dried volume, a
    missed limit or a refusal, which the gate-B tab does not test (review, 2026-09-26)."""
    y = r["rtY"]
    return y["action"] == "SKIP" and y.get("gate") in GATE_B_GATES


def _render_gate_b(d: ShadowData) -> str:
    t = d.ab["total"]
    ab_html = render_ab(d.ab) if d.ab["days"] else (
        f'<h2>RT-Y gate · paper A/B since {d.ab["since"]} · {t["y_n"]} of {d.ab["target"]} RT-Y trades</h2>'
        '<p class="dim">No session since the A/B began has a logged trigger yet — the table fills from the first one.</p>'
    )
    stood = [r for r in d.rows if _gate_b_skip(r)]
    stood_html = "".join(
        f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
        f'<td class="l">{html.escape(r["rtY"]["state"])}</td><td class="l">{html.escape(str(r["rtY"]["gate"] or "—"))}</td>'
        f'<td class="why l">{html.escape("; ".join(r["rtY"]["why"]))}</td>{_book_cell(r["books"]["FUDKII_RT_X"])}{_book_cell(r["books"]["FUDKII_RT_N"])}</tr>'
        for r in stood
    )
    return f"""{ab_html}
<h3>Gate B · what RT-Y stood aside from, {d.day.strftime("%d %b")} · {len(stood)}</h3>
<div class="scroll"><table><thead><tr><th>Fired</th><th class="l">Symbol</th><th class="l">State</th><th class="l">Gate</th><th class="l">Why</th><th>RT-X net</th><th>RT-N net</th></tr></thead>
<tbody>{stood_html or '<tr><td colspan="7" class="dim">none</td></tr>'}</tbody></table></div>"""


def _render_wide(d: ShadowData) -> str:
    w = d.wide or {"pairs": [], "total": {}}
    t = w.get("total") or {}
    body = "".join(
        f'<tr><td class="sym">{_ist(p["opened"])}</td><td class="sym l">{html.escape(str(p["symbol"]))}</td>'
        f'<td class="l">{html.escape(contract_label(str(p["contract"] or "—")))}</td><td>{_fmt(p["entry"])}</td>'
        f'<td>{_fmt(p["y"]["stop"])}</td><td>{_ist(p["y"]["closed"]) if p["y"]["status"] == "EXITED" else "—"}</td>'
        f'<td class="l">{html.escape(str(p["y"]["reason"] or "—"))}</td>{_net_cell(p["y"]["net"], p["y"]["status"])}'
        f'<td>{_fmt(p["w"]["stop"])}</td><td>{_ist(p["w"]["closed"]) if p["w"]["status"] == "EXITED" else "—"}</td>'
        f'<td class="l">{html.escape(str(p["w"]["reason"] or "—"))}</td>{_net_cell(p["w"]["net"], p["w"]["status"])}'
        f'{_net_cell(p["diff"])}</tr>'
        for p in w["pairs"]
    )
    n = t.get("closed", 0)
    tot = (
        f'<tr><td class="l"><b>since {d.ab["since"]}</b></td><td>{t.get("pairs", 0)}</td><td>{n}</td>'
        f'{_net_cell(t.get("y_net") if n else None)}<td>{_pct(t.get("y_win", 0), n)}</td>{_net_cell(t.get("y_worst"))}'
        f'{_net_cell(t.get("w_net") if n else None)}<td>{_pct(t.get("w_win", 0), n)}</td>{_net_cell(t.get("w_worst"))}'
        f'{_net_cell(t.get("diff") if n else None)}<td>{t.get("better", 0)} / {t.get("worse", 0)} / {t.get("same", 0)}</td></tr>'
    )
    c = w.get("since_cap") or {}
    m = c.get("closed", 0)
    if c:
        tot += (
            f'<tr><td class="l">since {c["from"]} · RT-Y capped at 25 %, wide not</td><td>—</td><td>{m}</td>'
            f'{_net_cell(c.get("y_net") if m else None)}<td>{_pct(c.get("y_win", 0), m)}</td><td class="dim">—</td>'
            f'{_net_cell(c.get("w_net") if m else None)}<td>{_pct(c.get("w_win", 0), m)}</td><td class="dim">—</td>'
            f'{_net_cell(c.get("diff") if m else None)}<td>{c.get("better", 0)} / {c.get("worse", 0)} / {c.get("same", 0)}</td></tr>'
        )
    return f"""<h3>Wide stop · running total</h3>
<div class="scroll"><table><thead><tr><th class="l">Period</th><th>Paired trades</th><th>Closed</th><th>RT-Y net</th><th>RT-Y win</th><th>RT-Y worst</th>
<th>Wide net</th><th>Wide win</th><th>Wide worst</th><th>Wide − RT-Y</th><th>Wide better / worse / same</th></tr></thead><tbody>{tot}</tbody></table></div>
<h3>Wide stop · every paired trade · {len(w["pairs"])}</h3>
<div class="scroll"><table><thead><tr><th>Opened</th><th class="l">Symbol</th><th class="l">Contract</th><th>Entry</th>
<th>RT-Y eq. stop</th><th>RT-Y exit</th><th class="l">RT-Y reason</th><th>RT-Y net</th>
<th>Wide eq. stop</th><th>Wide exit</th><th class="l">Wide reason</th><th>Wide net</th><th>Wide − RT-Y</th></tr></thead>
<tbody>{body or '<tr><td colspan="13" class="dim">no RT-Y trade since the shadow began</td></tr>'}</tbody></table></div>"""


def _render_stop_rules(d: ShadowData) -> str:
    sr = d.stop_rules or {"books": {}, "total": {}, "trades": []}
    labels = {"current": "Current", "E": "Stop E", "A": "Adaptive"}

    def cells(t: dict[str, Any], rule: str) -> str:
        n = t.get("closed", 0)
        out = f'<td>{n}</td>{_net_cell(t.get("net") if n else None)}<td>{_pct(t.get("wins", 0), n)}</td><td>{t.get("stops", 0)}</td>'
        if rule != "current":
            out += f'{_net_cell(t.get("diff") if n else None)}<td>{t.get("better", 0)} / {t.get("worse", 0)} / {t.get("same", 0)}</td>'
        return out

    head = ('<th class="l">Book</th>' + "".join(
        f'<th>{labels[r]} trades</th><th>{labels[r]} net</th><th>{labels[r]} win</th><th>{labels[r]} stops</th>'
        + ("" if r == "current" else f"<th>{labels[r]} − current</th><th>{labels[r]} better / worse / same</th>")
        for r in ("current", "E", "A")))
    body = "".join(
        f'<tr><td class="l">{html.escape(LABELS.get(b, b.replace("FUDKII_", "")))}</td>' + "".join(cells(t[r], r) for r in ("current", "E", "A")) + "</tr>"
        for b, t in sr["books"].items()
    )
    tot = sr.get("total") or {}
    if tot:
        body += '<tr><td class="l"><b>all books</b></td>' + "".join(cells(tot[r], r) for r in ("current", "E", "A")) + "</tr>"

    def rule_cells(r: dict[str, Any]) -> str:
        return f'<td class="l">{html.escape(str(r["reason"] or "—"))}</td>{_net_cell(r["net"], r["status"])}'

    rows = "".join(
        f'<tr><td class="sym">{_ist(t["opened"])}</td><td class="l">{html.escape(LABELS.get(t["book"], t["book"].replace("FUDKII_", "")))}</td>'
        f'<td class="sym l">{html.escape(str(t["symbol"] or "—"))}</td><td class="l">{html.escape(contract_label(str(t["contract"] or "—")))}</td>'
        f'<td>{_fmt(t["entry"])}</td>' + "".join(rule_cells(t["rules"][k]) for k in ("current", "E", "A"))
        + (_net_cell(t["rules"]["E"]["net"] - t["rules"]["current"]["net"]) + _net_cell(t["rules"]["A"]["net"] - t["rules"]["current"]["net"])
           if t["closed"] else '<td class="dim">—</td><td class="dim">—</td>') + "</tr>"
        for t in reversed(sr["trades"])
    )
    return f"""<h3>Stop rules · running total, per book</h3>
<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body or '<tr><td colspan="17" class="dim">no trade closed under all three rules yet</td></tr>'}</tbody></table></div>
<h3>Stop rules · every trade · {len(sr["trades"])}</h3>
<div class="scroll"><table><thead><tr><th>Opened</th><th class="l">Book</th><th class="l">Symbol</th><th class="l">Contract</th><th>Entry</th>
<th class="l">Current exit</th><th>Current net</th><th class="l">Stop E exit</th><th>Stop E net</th><th class="l">Adaptive exit</th><th>Adaptive net</th>
<th>E − current</th><th>Adaptive − current</th></tr></thead>
<tbody>{rows or '<tr><td colspan="13" class="dim">no mirrored trade yet — the mirrors open on the next fill of each book</td></tr>'}</tbody></table></div>"""


def _render_graded_f(d: ShadowData) -> str:
    g = d.graded_f or {"rows": [], "total": {}}
    t = g.get("total") or {}
    body = "".join(
        f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
        f'<td class="{"pos" if r["direction"] == "BULLISH" else "neg"}">{"BULL" if r["direction"] == "BULLISH" else "BEAR"}</td>'
        f'<td class="l">{html.escape(str(r["why_f"] or "—"))}</td><td class="l">{" · ".join(_fmt(x) for x in r["targets"]) or "—"}</td>'
        f'<td class="l">{html.escape(contract_label(str(r["contract"] or "—")))}</td><td>{_fmt(r["entry"])}</td>'
        f'<td class="l">{html.escape(str(r["exit_reason"] or r["skip"] or "—"))}</td>{_net_cell(r["net"], r["status"])}</tr>'
        for r in g["rows"]
    )
    n = t.get("closed", 0)
    tot = (
        f'<tr><td class="l"><b>since {d.ab["since"]}</b></td><td>{t.get("triggers", 0)}</td><td>{t.get("traded", 0)}</td><td>{n}</td>'
        f'{_net_cell(t.get("net") if n else None)}<td>{_pct(t.get("win", 0), n)}</td>{_net_cell(t.get("best"))}{_net_cell(t.get("worst"))}</tr>'
    )
    return f"""<h3>Graded F · running total</h3>
<div class="scroll"><table><thead><tr><th class="l">Period</th><th>Graded-F triggers</th><th>Traded</th><th>Closed</th><th>Net</th>
<th>Win</th><th>Best</th><th>Worst</th></tr></thead><tbody>{tot}</tbody></table></div>
<h3>Graded F · every trigger · {len(g["rows"])}</h3>
<div class="scroll"><table><thead><tr><th>Fired</th><th class="l">Symbol</th><th>Dir</th><th class="l">Why FUDKII graded it F</th>
<th class="l">Targets (raw pivots when no wall)</th><th class="l">Contract</th><th>Entry</th><th class="l">Exit / why it stood aside</th><th>Net</th></tr></thead>
<tbody>{body or '<tr><td colspan="9" class="dim">no graded-F trigger since the shadow began</td></tr>'}</tbody></table></div>"""


def _render_gap(d: ShadowData) -> str:
    g = d.gap or {"rows": [], "total": {}}
    t = g.get("total") or {}
    body = "".join(
        f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
        f'<td class="{"pos" if r["trigger"] == "BULLISH" else "neg"}">{"BULL" if r["trigger"] == "BULLISH" else "BEAR" if r["trigger"] else "—"}</td>'
        f'<td>{_fmt(r["gap"])}</td><td class="{"pos" if r["side"] == "CE" else "neg"}">{html.escape(str(r["side"] or "—"))}</td>'
        f'<td>{_fmt(r["stop"])}</td><td class="l">{" · ".join(_fmt(x) for x in r["targets"]) or "—"}</td>'
        f'<td>{_fmt(r["rr"])}</td><td>{html.escape(str(r["grade"] or "—"))}</td>'
        f'<td class="l">{html.escape(contract_label(str(r["blocked"] or r["contract"] or "—")))}</td><td>{_fmt(r["fill"])}</td>'
        f'<td class="l">{html.escape(str(r["ct"].get("reason") or "—"))}</td>{_net_cell(r["ct"]["net"], r["ct"]["status"])}'
        f'{_net_cell(r["parent"]["net"], r["parent"]["status"])}{_net_cell(r["x"]["net"], r["x"]["status"])}{_net_cell(r["n"]["net"], r["n"]["status"])}</tr>'
        for r in g["rows"]
    )
    n = t.get("closed", 0)
    tot = (
        f'<tr><td class="l"><b>since {d.ab["since"]}</b></td><td>{t.get("fired", 0)}</td><td>{t.get("blocked", 0)}</td><td>{n}</td>'
        f'{_net_cell(t.get("ct_net") if n else None)}<td>{_pct(t.get("ct_win", 0), n)}</td>'
        f'{_net_cell(t.get("parent_net") if n else None)}{_net_cell(t.get("x_net") if n else None)}{_net_cell(t.get("n_net") if n else None)}</tr>'
    )
    return f"""<h3>Gap fade · running total</h3>
<div class="scroll"><table><thead><tr><th class="l">Period</th><th>Fired</th><th>Blocked</th><th>Closed</th><th>CT-Y net</th><th>CT-Y win</th>
<th>Parent on the same</th><th>RT-X on the same</th><th>RT-N on the same</th></tr></thead><tbody>{tot}</tbody></table></div>
<h3>Gap fade · every 09:45 gap trigger · {len(g["rows"])}</h3>
<div class="scroll"><table><thead><tr><th>Fired</th><th class="l">Symbol</th><th>Trigger</th><th>Gap dATR</th><th>Fade</th><th>Stop (1 ATR30)</th>
<th class="l">Targets</th><th>RR</th><th>Grade</th><th class="l">Contract</th><th>Fill</th><th class="l">Exit</th><th>CT-Y net</th>
<th>Parent net</th><th>RT-X net</th><th>RT-N net</th></tr></thead>
<tbody>{body or '<tr><td colspan="16" class="dim">no 09:45 gap trigger since the A/B began</td></tr>'}</tbody></table></div>"""


def _pct_of(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _render_market_fade(d: ShadowData) -> str:
    g = d.market_fade or {"rows": [], "skipped": [], "total": {}}
    t = g.get("total") or {}
    body = "".join(
        f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
        f'<td class="{"pos" if r["trigger"] == "BULLISH" else "neg"}">{"BULL" if r["trigger"] == "BULLISH" else "BEAR" if r["trigger"] else "—"}</td>'
        f'<td>{_pct_of(r["breadth"])}</td><td class="{"pos" if r["side"] == "CE" else "neg"}">{html.escape(str(r["side"] or "—"))}</td>'
        f'<td>{_fmt(r["stop"])}</td><td class="l">{" · ".join(_fmt(x) for x in r["targets"]) or "—"}</td>'
        f'<td>{_fmt(r["rr"])}</td><td>{html.escape(str(r["grade"] or "—"))}</td>'
        f'<td class="l">{html.escape(contract_label(str(r["contract"] or r["not_taken"] or "—")))}</td><td>{_fmt(r["fill"])}</td>'
        f'<td class="l">{html.escape(str(r["exit_reason"] or "—"))}</td>{_net_cell(r["ct_m"]["net"], r["ct_m"]["status"])}'
        f'{_net_cell(r["rt_x"]["net"], r["rt_x"]["status"])}{_net_cell(r["rt_n"]["net"], r["rt_n"]["status"])}'
        f'{_net_cell(r["rt_y"]["net"], r["rt_y"]["status"])}{_net_cell(r["ct_y"]["net"], r["ct_y"]["status"])}</tr>'
        for r in g["rows"]
    )
    n = t.get("closed", 0)
    tot = (
        f'<tr><td class="l"><b>since {d.ab["since"]}</b></td><td>{t.get("fades", 0)}</td><td>{t.get("traded", 0)}</td><td>{n}</td>'
        f'{_net_cell(t.get("net") if n else None)}<td>{_pct(t.get("win", 0), n)}</td>{_net_cell(t.get("best"))}{_net_cell(t.get("worst"))}'
        f'{_net_cell(t.get("rt_x_net") if n else None)}{_net_cell(t.get("rt_n_net") if n else None)}{_net_cell(t.get("rt_y_net") if n else None)}</tr>'
    )
    near = [r for r in g.get("skipped", []) if r["breadth"] is not None and r["breadth"] <= 0.50][-15:]
    near_rows = "".join(
        f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
        f'<td class="{"pos" if r["trigger"] == "BULLISH" else "neg"}">{"BULL" if r["trigger"] == "BULLISH" else "BEAR" if r["trigger"] else "—"}</td>'
        f'<td>{_pct_of(r["breadth"])}</td><td class="why l">{html.escape(str(r["reason"] or "—"))}</td></tr>'
        for r in near
    )
    return f"""<h3>Market fade · running total</h3>
<div class="scroll"><table><thead><tr><th class="l">Period</th><th>Fades planned</th><th>Traded</th><th>Closed</th><th>CT-M net</th><th>CT-M win</th>
<th>Best</th><th>Worst</th><th>RT-X on the same</th><th>RT-N on the same</th><th>RT-Y on the same</th></tr></thead><tbody>{tot}</tbody></table></div>
<p class="sub">Skipped, market not against: {t.get("skipped", 0)} triggers — {t.get("near", 0)} at 46–50 % agreeing, {t.get("mid", 0)} at 51–60 %,
{t.get("far", 0)} above 60 %.</p>
<h3>Market fade · every fade · {len(g["rows"])}</h3>
<div class="scroll"><table><thead><tr><th>Fired</th><th class="l">Symbol</th><th>Trigger</th><th>Breadth</th><th>Fade</th><th>Stop (1 ATR30)</th>
<th class="l">Targets</th><th>RR</th><th>Grade</th><th class="l">Contract / why not taken</th><th>Fill</th><th class="l">Exit</th><th>CT-M net</th>
<th>RT-X net</th><th>RT-N net</th><th>RT-Y net</th><th>CT-Y net</th></tr></thead>
<tbody>{body or '<tr><td colspan="17" class="dim">no trigger the market was clearly against since CT-M began</td></tr>'}</tbody></table></div>
<h3>Market fade · the closest skips (46–50 % agreeing)</h3>
<div class="scroll"><table><thead><tr><th>Fired</th><th class="l">Symbol</th><th>Trigger</th><th>Breadth</th><th class="l">Why it stood aside</th></tr></thead>
<tbody>{near_rows or '<tr><td colspan="5" class="dim">none</td></tr>'}</tbody></table></div>"""


def _render_fukaa(d: ShadowData) -> str:
    g = d.fukaa or {"rows": [], "total": {}}
    t = g.get("total") or {}

    def yn(v: Any) -> str:
        return "—" if v is None else ("with" if v else "against")

    body = "".join(
        f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
        f'<td class="{"pos" if r["direction"] == "BULLISH" else "neg"}">{"BULL" if r["direction"] == "BULLISH" else "BEAR"}</td>'
        f'<td>{_fmt(r["entry"])}</td><td>{_fmt(r["stop"])}</td><td>{_fmt(r["t1"])}</td><td>{_fmt(r["rr"])}</td><td>{_fmt(r["composite"])}</td>'
        f'<td>{"T+1" if r["promoted"] else "—"}</td><td>{_fmt(r["surge"])}</td><td>{_fmt(r["rel_volume"])}</td><td>{_fmt(r["momentum"])}</td>'
        f'<td>{_fmt(r["oi_change"])}</td><td>{_fmt(r["oi_z"])}</td><td>{_pct_of(r["breadth"])} {yn(r["with_market"])}</td>'
        f'<td class="l">{html.escape(str(r["oi_quadrant"] or "—"))}</td><td>{yn(r["oi_agrees"])}</td>'
        f'{_net_cell(r["rt_x"]["net"], r["rt_x"]["status"])}{_net_cell(r["rt_n"]["net"], r["rt_n"]["status"])}'
        f'{_net_cell(r["rt_y"]["net"], r["rt_y"]["status"])}{_net_cell(r["ct_m"]["net"], r["ct_m"]["status"])}</tr>'
        for r in g["rows"]
    )
    tot = (
        f'<tr><td class="l"><b>since {d.ab["since"]}</b></td><td>{t.get("signals", 0)}</td><td>{t.get("promoted", 0)}</td>'
        f'<td>{t.get("with_market", 0)}</td><td>{t.get("counter", 0)}</td><td>{t.get("oi_agrees", 0)}</td>'
        f'{_net_cell(t.get("rt_x_net") if t.get("rt_x_closed") else None)}{_net_cell(t.get("rt_n_net") if t.get("rt_x_closed") else None)}'
        f'{_net_cell(t.get("rt_y_net") if t.get("rt_y_closed") else None)}</tr>'
    )
    return f"""<h3>FUKAA · running total</h3>
<div class="scroll"><table><thead><tr><th class="l">Period</th><th>Shadow signals</th><th>Promoted at T+1</th><th>Market with</th>
<th>Market against</th><th>OI building its way</th><th>RT-X on the same</th><th>RT-N on the same</th><th>RT-Y on the same</th></tr></thead>
<tbody>{tot}</tbody></table></div>
<h3>FUKAA · every shadow signal · {len(g["rows"])}</h3>
<div class="scroll"><table><thead><tr><th>Fired</th><th class="l">Symbol</th><th>Dir</th><th>Entry</th><th>Stop</th><th>T1</th><th>RR</th>
<th>Score</th><th>T+1</th><th>Volume ×</th><th>vs NIFTY50</th><th>Momentum</th><th>OI chg %</th><th>OI z</th><th>Market</th>
<th class="l">OI quadrant</th><th>OI its way</th><th>RT-X net</th><th>RT-N net</th><th>RT-Y net</th><th>CT-M net</th></tr></thead>
<tbody>{body or '<tr><td colspan="21" class="dim">no FUKAA shadow signal yet</td></tr>'}</tbody></table></div>"""


def volume_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """RT-X's and RT-Y's outcome bucketed by the three volume readings logged at the trigger, since
    the A/B began — does a quiet (or loud) trigger bar separate winners from losers, and is a quiet
    stock different from a quiet market?"""
    groups = (
        ("Dried — the RT books' rule (stock vs its own baseline)", "volDried"),
        ("Dried — against the market's median volume at that bar", "volDriedRel"),
        ("Surge — trigger bar ≥ 1.5x its baseline", "volSurge"),
    )
    out = []
    for name, key in groups:
        buckets: dict[str, dict[str, Any]] = {}
        for r in rows:
            v = r.get(key)
            if v is None:
                continue
            b = buckets.setdefault("yes" if v else "no", {"triggers": 0, "x_n": 0, "x_net": 0.0, "x_win": 0, "y_n": 0, "y_net": 0.0})
            b["triggers"] += 1
            x, y = r["books"]["FUDKII_RT_X"], r["books"]["FUDKII_RT_Y"]
            if x["status"] == "EXITED" and x["net"] is not None:
                b["x_n"] += 1
                b["x_net"] += x["net"]
                b["x_win"] += x["net"] > 0
            if y["status"] == "EXITED" and y["net"] is not None:
                b["y_n"] += 1
                b["y_net"] += y["net"]
        out.append({"label": name, "buckets": dict(sorted(buckets.items()))})
    return {"groups": out}


def _render_volume(d: ShadowData) -> str:
    groups = (d.volume or {}).get("groups") or []
    body = []
    for g in groups:
        for k, b in g["buckets"].items():
            body.append(
                f'<tr><td class="l">{html.escape(g["label"])}</td><td class="l">{k}</td><td>{b["triggers"]}</td><td>{b["x_n"]}</td>'
                f'{_net_cell(b["x_net"] / b["x_n"] if b["x_n"] else None)}<td>{_pct(b["x_win"], b["x_n"])}</td>'
                f'<td>{b["y_n"]}</td>{_net_cell(b["y_net"] / b["y_n"] if b["y_n"] else None)}</tr>'
            )
    rows = []
    for r in d.rows:
        f = lambda v: "—" if v is None else ("yes" if v else "no")  # noqa: E731
        rows.append(
            f'<tr><td class="sym">{_ist(r["fired"])}</td><td class="sym l">{html.escape(str(r["symbol"]))}</td>'
            f'<td class="{"pos" if r["direction"] == "BULLISH" else "neg"}">{"BULL" if r["direction"] == "BULLISH" else "BEAR"}</td>'
            f'<td>{_fmt(r["volSurgeT"])}</td><td>{_fmt(r["volSurgeT1"])}</td><td>{_fmt(r["mktSurgeT"])}</td><td>{_fmt(r["mktSurgeT1"])}</td>'
            f'<td>{f(r["volDried"])}</td><td>{f(r["volDriedRel"])}</td><td>{f(r["volSurge"])}</td>'
            + _book_cell(r["books"]["FUDKII_RT_X"]) + _book_cell(r["books"]["FUDKII_RT_Y"]) + "</tr>"
        )
    return f"""<h3>Volume · outcome by reading since {d.ab["since"]}</h3>
<div class="scroll"><table><thead><tr><th class="l">Reading</th><th class="l">Bucket</th><th>Triggers</th><th>RT-X trades</th><th>RT-X avg net</th>
<th>RT-X win</th><th>RT-Y trades</th><th>RT-Y avg net</th></tr></thead>
<tbody>{"".join(body) or '<tr><td colspan="8" class="dim">no logged trigger since the A/B began</td></tr>'}</tbody></table></div>
<h3>Volume · every trigger, {d.day.strftime("%d %b")} · {len(d.rows)}</h3>
<div class="scroll"><table><thead><tr><th>Fired</th><th class="l">Symbol</th><th>Dir</th><th>Stock T</th><th>Stock T−1</th><th>Market T</th><th>Market T−1</th>
<th>Dried (rule)</th><th>Dried vs market</th><th>Surge ≥1.5x</th><th>RT-X net</th><th>RT-Y net</th></tr></thead>
<tbody>{"".join(rows) or '<tr><td colspan="12" class="dim">no triggers on this day</td></tr>'}</tbody></table></div>"""


def _render_labels(d: ShadowData) -> str:
    groups = (d.labels or {}).get("groups") or []
    body = []
    for g in groups:
        for k, b in g["buckets"].items():
            avg = b["x_net"] / b["x_n"] if b["x_n"] else None
            body.append(
                f'<tr><td class="l">{html.escape(g["label"])}</td><td class="l">{html.escape(k)}</td><td>{b["triggers"]}</td>'
                f'<td>{b["x_n"]}</td>{_net_cell(avg)}<td>{_pct(b["x_win"], b["x_n"])}</td>{_net_cell(b["x_net"] if b["x_n"] else None)}</tr>'
            )
    return f"""<h3>Labels · RT-X's outcome by label since {d.ab["since"]}</h3>
<div class="scroll"><table><thead><tr><th class="l">Label</th><th class="l">Bucket</th><th>Triggers</th><th>RT-X trades</th><th>RT-X avg net</th>
<th>RT-X win</th><th>RT-X total</th></tr></thead>
<tbody>{"".join(body) or '<tr><td colspan="7" class="dim">no logged trigger since the A/B began</td></tr>'}</tbody></table></div>
<h3>Labels · every trigger, {d.day.strftime("%d %b")} · {len(d.rows)}</h3>
{_triggers_table(d.rows)}"""


GATE_B = Brief(
    name="Gate B · RT-Y regime gate",
    testing="Whether RT-Y makes more per trade by standing aside when the market, the chart or the open is against the trigger.",
    for_rule=(
        "RT-Y with gate B. It mirrors a FUDKII fill only when (1) more than 50% of the ~220 NSE names trade beyond their own "
        "09:15 open in the trigger's direction, (2) no daily, weekly or monthly key pivot (pivot, BC, TC, R1/S1, R2/S2) sits "
        "within 0.5 ATR30 ahead of the close, and (3) a 09:45 trigger did not gap 0.3 daily ATR or more its own way."
    ),
    against="RT-X (dried-volume check only) and RT-N (no entry gate) on the same triggers.",
    logic=(
        "Each gate reads what was logged at the trigger bar's close; the first gate that fails is the reason recorded and shown. "
        "A measure that could not be taken never blocks. \"Would take\" / \"would skip\" is what the gate says where the parent "
        "did not fill, so RT-Y had nothing to decide."
    ),
    pros=(
        "Sep 1–25 replay: gate-B takes made +1.13% / +2.84% a trade in the two halves, against −0.24% ungated.",
        "Every gate is logged per trigger, so each one can be judged on its own.",
    ),
    cons=(
        "All three rules were found on the same September data — these paper weeks are the out-of-sample test.",
        "About half the triggers are skipped, so 50 RT-Y trades take roughly two weeks.",
        "RT-Y's exits differ from RT-X's and RT-N's: the comparison is on the same triggers, not the same exits.",
    ),
    decide=(
        "After about 50 RT-Y trades: keep gate B if RT-Y's average net per trade beats RT-X's and RT-N's on the same "
        "triggers, and the triggers it kept RT-Y out of lost money in the ungated books."
    ),
)

STOP_RULES = Brief(
    name="Stop rules · current vs stop E vs adaptive",
    testing="Which stop rule each strategy should trade with: its current option stop, or the stock's own stop (two ways).",
    for_rule=(
        "Every book's two mirrors — the same fill (contract, size, price, instant, stop levels), the stop judged on the STOCK: "
        "stop E sells a decisive breach (0.10 % of price through, 0.35 % against the trade in 60 s, or already through on the "
        "first print) at once and a marginal one after 60 s through; adaptive confirms a marginal breach by magnitude × time "
        "(the integral of % through reaching 1.0 %·s: 0.05 % in 20 s, 0.02 % in 50 s, 60 s at most). Both sell when the option "
        "bid is 25 % under the premium paid, whatever the stock says, and sell into the bid at the trigger."
    ),
    against=(
        "The book itself: its current stop — the stock's stop drawn on the option through delta, the 75 s sustain on the option "
        "mid for the RT books, a single print for FUDKII, the 9 % hard floor, the stock through its stop."
    ),
    logic=(
        "A mirror opens only when its book fills and copies that fill exactly. Everything but the stop is the book's: targets, "
        "the rung ratchet, the give-back line, the trail, the 15:20 flatten. It keeps running on its own rule when you close the "
        "real trade by hand. Tape study 29 Sep – 1 Oct (73 trades): the option stop fired with the stock a median 13 % of the "
        "way to its own stop; 23 of 33 stock breaches were back inside within 5 min."
    ),
    pros=(
        "Three outcomes of one trade, side by side, with nothing else different.",
        "Replay 29 Sep – 1 Oct: stop E +₹12,107 and adaptive +₹14,702 against the current stop, mostly false stops avoided.",
    ),
    cons=(
        "A slow breach costs more under the stock's stop: the option stop had often sold a minute earlier (TECHM, SRF, 30 Sep).",
        "About 8–10 decided trades per book a week: judge pooled across books first, a book on its own only on a large gap.",
    ),
    decide=(
        "Friday 9 Oct: the all-books row first — does a stock-based stop beat the current one net of the slow breaches? Then a "
        "book splits off only where its gap is large, holds across days and has a reason in that book's own rules."
    ),
)


WIDE_STOP = Brief(
    name="Wide stop · RT-Y 1% past",
    testing="Whether giving RT-Y's equity stop 1% more room keeps more winners than it adds to the losers.",
    for_rule=(
        "The shadow book RT-Y · wide stop: every RT-Y entry — same contract, size, price and instant — under RT-Y's exits, "
        "with the equity stop 1% further from entry (a bullish trade's stop × 0.99, a bearish one's × 1.01) and the option "
        "stop re-projected through delta for that wider level."
    ),
    against=(
        "RT-Y itself, with the stop as planned (it exits on the touch). From 29 Sep RT-Y's option stop is also capped at 25% "
        "of the premium and the shadow's is not, so from that date the pair differs in two rules — its own row in the "
        "running total."
    ),
    logic=(
        "The shadow opens only when RT-Y opens, so the two books hold exactly the same trades. Everything else is RT-Y's: the +5% "
        "arm, the rung SL one step behind, the 3% give-back, the 75 s sustain, the hard floor 9% under the option stop, the "
        "15:20 flat. Its option stop keeps re-projecting every 10 s from the wider equity stop, as RT-Y's does from its own."
    ),
    pros=(
        "Fewer stop-outs on a wick: on 19–25 Sep, gate-B trades made +6.30% against +3.90% on the touch, with 88% winners.",
        "A live comparison on identical fills, not a replay.",
    ),
    cons=(
        "Unproven: on 1–18 Sep the same trades made +0.43% against +1.14%; over September the gain was +0.44 ± 1.86 points — noise.",
        "A bigger loss when the stop does go: worst trade −41% against −35%.",
        "On a cheap contract the re-projected option stop can sit near zero, leaving the equity stop to do all the work.",
    ),
    decide=(
        "After about 50 paired trades: adopt the wide stop for RT-Y only if the shadow's total net beats RT-Y's and its "
        "worst trade is not materially deeper."
    ),
)

GRADED_F = Brief(
    name="Graded F · RT-Y's rules",
    testing="Whether the triggers FUDKII grades F and does not publish make money under RT-Y's own rules.",
    for_rule=(
        "The shadow book RT-Y · graded F: every NSE trigger FUDKII does not publish (a SuperTrend flip with the close "
        "through the band, graded F — mostly no wall ahead for a target — or, rarely, the session's last bar below "
        "FUDKII's fortress floor), judged by RT-Y's gates (breadth, a key pivot "
        "just ahead, the 09:45 gap, dried volume) and traded under RT-Y's exits with its 25% premium cap. Where no pivot "
        "cluster makes a target, the raw pivots ahead, nearest first, are the ladder."
    ),
    against="Standing aside, which is what every trading book does on these triggers today.",
    logic=(
        "Its own entries and its own purse: RT-Y never sees these triggers, so nothing is mirrored. Its gates run first, "
        "from what the engine already holds; only a trigger they pass goes on to the strike choice, the 4 lots under "
        "₹75,000 and the resting limit entry, exactly as RT-Y's. The option stop is never more than 25% under the premium paid."
    ),
    pros=(
        "Measured live on real quotes and fills, beside the trading books, without touching them.",
        "Replay 1–28 Sep: 11 of 16 trades won.",
    ),
    cons=(
        "Replay 1–28 Sep: −₹2,364 net on 16 trades — three losers (DIXON −13,251, BLUESTARCO −12,782, SBICARD −7,340) "
        "outweighed eleven small winners.",
        "A graded-F trigger has no wall ahead by definition, so its stop is often 2–4 ATR30 away; the 25% cap is what "
        "limits the loss, not the chart.",
    ),
    decide=(
        "After about 30 closed trades: offer graded-F triggers to RT-Y itself only if this book's net is positive and its "
        "worst trade is no deeper than RT-Y's own."
    ),
)

GAP_FADE = Brief(
    name="Gap fade · CT-Y 09:45",
    testing="Whether a 09:45 trigger that gapped its own way is a trap worth trading the other way.",
    for_rule=(
        "CT-Y's gap fade: on a first-bar (09:15–09:45) trigger whose open gapped 0.3 daily ATR or more in the trigger's own "
        "direction, the opposite OTM option; equity stop 1 ATR30 past the trigger's close; targets the walls on the fade's "
        "side (1 ATR30 away if there are none); RT-Y's exits."
    ),
    against="What the in-trend books — the parent, RT-X and RT-N — made on the same triggers. RT-Y stands aside from these (gate B).",
    logic=(
        "The fade's RR is T1's distance over the 1-ATR stop, and its grade (A ≥ 2.5, B ≥ 1.8, C ≥ 1.2, else F) is a label: "
        "nothing is blocked on it. CT-Y does not also mirror CT-X's fade on a trigger it has gap-faded; CT-X is unchanged."
    ),
    pros=(
        "Sep 1–25 replay: +3.72% / +5.87% a trade in the two halves (40 trades), where the trigger itself lost −3.80%.",
        "19–25 Sep: 13 fades, +6.45% a trade, 11 winners.",
    ),
    cons=(
        "Only 40 historical trades, and the gap feature was found on the same data.",
        "Before costs; option spreads are widest at 09:45.",
        "A 1-ATR stop is wider than the confluence stop, so a loser costs more.",
    ),
    decide=(
        "After about 20 gap fades: keep it if CT-Y's net on them is positive after charges and beats the in-trend books on the "
        "same triggers."
    ),
)

MARKET_FADE = Brief(
    name="Market fade · CT-M ≤ 45 %",
    testing="Whether a published trigger the market is clearly against is worth trading the other way.",
    for_rule=(
        "The shadow book FUDKII-CT-M: every published NSE trigger at most 45 % of the market agrees with — breadth, the "
        "share of NSE names past today's open in the trigger's direction, logged at the trigger — is faded with CT-Y's "
        "plan: the opposite OTM, the stock stop 1 ATR30 past the trigger's close, the walls on the fade's side as targets "
        "(one target 1 ATR30 away when there are none), traded under CT-Y's exits."
    ),
    against=(
        "What the books that trade the trigger made on it: RT-X and RT-N take it in-trend; RT-Y stands aside from it "
        "(its breadth gate); CT-Y fades only the 09:45 gap ones."
    ),
    logic=(
        "Its own entries and its own ₹10 L purse; nothing is mirrored. Every published NSE trigger is decided: breadth "
        "at or under 45 % is a fade (the same plan CT-Y's gap fade uses), otherwise a skip on its card with the share; a "
        "breadth that could not be read decides nothing. 4 lots under ₹75,000 and the resting limit entry, as every "
        "book; exits: T1 = max(own T1, entry +5 %), the stop to breakeven after T1, a 3 % give-back from the peak, no "
        "25 % premium cap. A shadow: never in the day's totals, never a live order."
    ),
    pros=(
        "25 Sep – 1 Oct, actual replay through this code: 3 fades filled, all won, +₹19,127 net of charges (INFY, "
        "KALYANKJIL, ADANIENT).",
        "Following these triggers is worse: on the option model over 24 Aug – 1 Oct the same triggers traded in-trend "
        "lost −₹2,17,547.",
    ),
    cons=(
        "On the option model over 24 Aug – 1 Oct the rule itself lost: 73 fades, 55 % won, −₹80,934 — −₹76,238 to "
        "11 Sep, −₹4,696 after (75 % won). Unproven.",
        "In the replay, of 6 planned fades one entry did not fill and two had no option prices.",
        "A 1-ATR stop is wider than the confluence stop, so a loser costs more.",
    ),
    decide=(
        "After about 30 closed fades: keep it — and consider it as a trading book — only if its net after charges is "
        "positive in both halves of the sample and beats RT-X and RT-N on the same triggers."
    ),
)

FUKAA_SHADOW = Brief(
    name="FUKAA · in shadow",
    testing="Whether FUKAA — FUDKII's trigger admitted only when volume confirms it — is worth trading, now that its inputs are read correctly.",
    for_rule=(
        "FUKAA admits a FUDKII trigger only when the trigger bar or the one before ran at 4× its baseline volume or more and "
        "its composite score (volume, OI, momentum, RR) reaches 60; a trigger without the volume is watched one more bar "
        "(35 min) and promoted at that bar's close, refused through the stop. Its inputs since 2 Oct: the engine's checked "
        "volume reading, volume against the NIFTY50's mean, momentum on a 60-bar ATR, and the OI change against the "
        "previous session's close (the current month; current + next in the contract's last three sessions) with its "
        "z against the NIFTY50."
    ),
    against="What RT-X, RT-N and RT-Y made on the same FUDKII triggers.",
    logic=(
        "In shadow (FukaaConfig.shadow): an admitted signal is a SHADOW row and a fukaa.shadow event with every input; "
        "nothing reaches a book and its wallet never moves. Each event also carries its alignment — labels, never gates: "
        "whether the market was with the trigger (breadth over 50 %), the price-OI quadrant since the previous close "
        "(long or short build-up, short covering, long unwinding) and whether OI built the signal's way."
    ),
    pros=(
        "Before 2 Oct FUKAA could never fire: the broker's OI change field is 0.0 on every frame and its momentum was never "
        "computed. Its readings are now real.",
        "Volume confirmation is measured on every trigger, beside the books that trade them.",
    ),
    cons=(
        "23 Sep – 1 Oct with the fixed inputs (stock-path estimate): 7 signals, 2 won, about −₹11,900; no volume, OI, "
        "momentum or direction rule cleared costs.",
        "The OI readings so far are from the expiry week, when OI fell on most names.",
        "Its thresholds (4× volume, the OI scoring bands) predate the fixes and are not yet re-fitted.",
    ),
    decide=(
        "After the 27 Oct expiry, with about 30 shadow signals: re-fit the thresholds on these readings; trade it only if "
        "the re-fitted rule is positive after charges in both halves and beats RT-Y on the same triggers."
    ),
)

LABELS_BRIEF = Brief(
    name="Trigger labels",
    testing="Whether the measures logged on every trigger separate the winners from the losers.",
    for_rule=(
        "Breadth; trend efficiency (the net move over the path of the last 8 closed 30m bars, 1 = a straight line); the stock's "
        "own-volatility band (daily ATR% against its own 20-session median — the MCX logic); the 09:15 gap in daily ATRs; "
        "and the key pivots ahead."
    ),
    against="Their absence: RT-X's outcome — it takes every trigger the parent fills — bucketed by each label.",
    logic=(
        "Trend efficiency, the volatility band and volume are labels: nothing trades on them. Breadth, the pivots ahead and the "
        "09:45 gap are also RT-Y's gates (tab Gate B)."
    ),
    pros=("Costs nothing, and tests each label out of sample.", "Shows at a glance why a trigger was taken or skipped."),
    cons=(
        "Small buckets can mislead for weeks.",
        "Sep 1–25: trend efficiency and the own-volatility band showed no edge; breadth did (+0.45% above 50% against −1.52% "
        "at or below; 19–25 Sep +0.92% against −4.02%).",
    ),
    decide="Promote a label to a gate only if its buckets separate RT-X's average net in both halves of the paper period.",
)

VOLUME = Brief(
    name="Volume · dried & surge",
    testing="Whether the volume on the trigger bar says anything about the trade, and whether a quiet stock differs from a quiet market.",
    for_rule=(
        "Three readings logged on every trigger: the RT books' dried rule (the trigger bar AND the bar before under 0.85x the stock's "
        "own T-2…T-7 baseline); the same rule measured against the market's median volume at that bar; and a surge (trigger bar "
        "≥ 1.5x its baseline)."
    ),
    against="Triggers without that reading — RT-X's outcome (it takes every filled trigger), and RT-Y's where it traded.",
    logic=(
        "Nothing new trades on these. RT-X and RT-Y still skip a trigger the dried rule flags (gate 'dried_volume'). The 15:15 bar is "
        "a closing-auction print and is left out of every baseline, so the bar before a 09:45 trigger is the previous day's 14:45 bar."
    ),
    pros=(
        "Separates a quiet stock from a quiet market, which the rule itself cannot.",
        "Tests the surge warning the Sep replay found, with no money on it.",
    ),
    cons=(
        "Sep 1–25 (RT-Y option %, before costs): the dried rule's skips averaged +1.24% against −0.46% for the rest, but inside "
        "gate B's takes +0.61% against +2.38%: mixed. Against the market it behaved the same (+1.32% / −0.42%).",
        "The future's volume could be read for 67 triggers only (9 dried), too few to judge, so it is not used.",
        "A surge averaged −1.24% against +1.27% (−0.71% / −1.84% in the two halves); promising, but found on the same data.",
    ),
    decide=(
        "Drop or keep the dried rule, or promote the surge warning to a gate, only if its buckets separate RT-X's average net in both "
        "halves of the paper period."
    ),
)

#: The tabs, in order. A new shadow is one more entry here.
TABS: list[ShadowTab] = [
    ShadowTab("gate-b", "Gate B · RT-Y regime gate", GATE_B, _render_gate_b),
    ShadowTab("stop-rules", "Stop rules · current vs E vs adaptive", STOP_RULES, _render_stop_rules),
    ShadowTab("wide-stop", "Wide stop · RT-Y 1% past", WIDE_STOP, _render_wide),
    ShadowTab("graded-f", "Graded F · RT-Y's rules", GRADED_F, _render_graded_f),
    ShadowTab("gap-fade", "Gap fade · CT-Y 09:45", GAP_FADE, _render_gap),
    ShadowTab("market-fade", "Market fade · CT-M ≤45%", MARKET_FADE, _render_market_fade),
    ShadowTab("fukaa", "FUKAA · shadow", FUKAA_SHADOW, _render_fukaa),
    ShadowTab("volume", "Volume · dried & surge", VOLUME, _render_volume),
    ShadowTab("labels", "Trigger labels", LABELS_BRIEF, _render_labels),
]

_TAB_CSS = """
.tabbar{display:flex;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--line);margin:4px 0 18px}
.tabbar a{font:600 12px/1 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.05em;color:var(--ink2);text-decoration:none;
padding:10px 14px;border:1px solid transparent;border-bottom:0;border-radius:4px 4px 0 0;margin-bottom:-1px}
.tabbar a:hover{color:var(--ink)}
.tabbar a.on{color:var(--ink);background:var(--sunk);border-color:var(--line)}
.tabbar a:focus-visible{outline:2px solid var(--hold);outline-offset:2px}
.brief{background:var(--sunk);border:1px solid var(--soft);border-left:3px solid var(--hold);border-radius:3px;
padding:14px 16px;margin:0 0 18px;max-width:110ch}
.brief p{margin:0 0 8px;color:var(--ink2)} .brief b{color:var(--ink)}
.brief .bname{font:700 17px/1.2 "IBM Plex Sans Condensed",sans-serif;margin:0 0 10px;color:var(--ink)}
.brief .pc{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:4px 0 10px}
.brief ul{margin:4px 0 0;padding-left:18px;color:var(--ink2)} .brief li{margin:0 0 4px}
h3{font:600 11px/1 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.13em;text-transform:uppercase;
color:var(--ink3);margin:26px 0 9px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.daynav a{color:var(--ink2);text-decoration:none;margin:0 2px} .daynav a.cur{color:var(--ink);font-weight:600;text-decoration:underline}
"""

_TAB_JS = """
(function(){
  var ids=%s;
  function show(){
    var h=(location.hash||'').replace('#','');
    if(ids.indexOf(h)<0){h=ids[0];}
    ids.forEach(function(id){
      var s=document.getElementById(id), a=document.querySelector('.tabbar a[href="#'+id+'"]');
      if(s){s.hidden=(id!==h);} if(a){a.classList.toggle('on',id===h);a.setAttribute('aria-selected',id===h?'true':'false');}
    });
    document.querySelectorAll('.daynav a,.dl').forEach(function(a){a.href=a.href.split('#')[0]+(a.classList.contains('dl')?'':'#'+h);});
  }
  window.addEventListener('hashchange',show); show();
})();
"""


def render_shadow(d: ShadowData) -> str:
    """The page: a tab per rule under test, each opening with its brief. All tabs are in the
    document (the .xlsx reads every one); the script shows the one the #anchor names."""
    pretty = d.day.strftime("%d %B %Y")
    nav = " · ".join(
        f'<a href="/shadow?day={x}"{" class=cur" if x == d.day.isoformat() else ""}>{datetime.fromisoformat(x).strftime("%d %b")}</a>' for x in d.days[-10:]
    )
    tabbar = "".join(f'<a href="#{t.id}" role="tab">{html.escape(t.title)}</a>' for t in TABS)
    sections = "".join(
        f'<section class="tab" id="{t.id}" role="tabpanel">{_brief_html(t.brief)}{t.render(d)}</section>' for t in TABS
    )
    ids = "[" + ",".join(f'"{t.id}"' for t in TABS) + "]"
    stood = sum(1 for r in d.rows if _gate_b_skip(r))
    wt = (d.wide or {}).get("total") or {}
    ft = (d.graded_f or {}).get("total") or {}
    gt = (d.gap or {}).get("total") or {}
    mt = (d.market_fade or {}).get("total") or {}
    kt = (d.fukaa or {}).get("total") or {}
    st = ((d.stop_rules or {}).get("total") or {}).get("current") or {}
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Shadow — {html.escape(pretty)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>{_CSS}{_TAB_CSS}</style></head><body><div class="wrap">
<header>
  <div><p class="eyebrow">NSE options · paper · every rule under test, rendered live from the ledger</p>
  <h1>Shadow</h1>
  <p class="sub">One tab per rule under test. Each opens with what it tests, for and against what, the logic, pros and cons,
  and how we decide; the running totals start {html.escape(d.ab["since"])}. Trigger rows are for {html.escape(pretty)}.</p>
  <p class="sub daynav">{nav}</p></div>
  <div style="display:flex;flex-direction:column;gap:9px;align-items:flex-end">
    <a class="dl" href="/shadow.xlsx?day={d.day.isoformat()}" download>Download .xlsx (every tab)</a>
    <div class="tally">
      <div class="tal"><b>{len(d.rows)}</b><span>triggers</span></div>
      <div class="tal"><b>{stood}</b><span>RT-Y gate-B skips</span></div>
      <div class="tal"><b>{st.get("closed", 0)}</b><span>stop-rule trios</span></div>
      <div class="tal"><b>{wt.get("pairs", 0)}</b><span>wide-stop pairs</span></div>
      <div class="tal"><b>{ft.get("traded", 0)}</b><span>graded-F trades</span></div>
      <div class="tal"><b>{gt.get("fired", 0)}</b><span>gap fades</span></div>
      <div class="tal"><b>{mt.get("fades", 0)}</b><span>CT-M fades</span></div>
      <div class="tal"><b>{kt.get("signals", 0)}</b><span>FUKAA signals</span></div>
    </div>
  </div>
</header>
<nav class="tabbar" role="tablist">{tabbar}</nav>
{sections}
<footer>
  <p><b>Paper only.</b> Every figure is from the paper books' own fills and exits, net of charges; open positions join the totals
  when they close. A shadow book mirrors another book's entries with one rule changed, or trades what no trading book is
  offered, so its P&amp;L is never added to the others'.</p>
</footer>
</div><script>{_TAB_JS % ids}</script></body></html>"""
