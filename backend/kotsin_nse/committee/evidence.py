"""The evidence pack for one case: a compact, point-in-time, numeric view of one decision and what
the underlying did next, with stable keys the committee can cite.

Nothing here is re-derived from the market. ``trig.*``, ``lvl.*``, ``zones`` and ``gate.*`` are
the numbers the strategy stored on the signal at the moment it decided (``Signal.context``); the
outcome is the trade the ledger or the backtester wrote; only ``path.*`` is computed here, from the
decision-frame bars after the signal, because "what would a wider stop have paid" is the question
every post-mortem turns on and no stored record answers it.

A backtest trade has no ``Signal.context`` (the backtester keeps trades, not signals), so a case
built from one carries the levels and the path and says which trigger keys are absent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from ..market.session import ist_hm, to_ist

PATH_CHECKPOINTS = (1, 2, 4, 8, 16)


@dataclass(slots=True)
class BarRow:
    ts: int
    o: float
    h: float
    l: float  # noqa: E741 - o/h/l/c is the vocabulary
    c: float
    v: float


@dataclass(slots=True)
class Case:
    ref: str  # signal_id, or "bt-<run>#<index>"
    kind: str  # "ledger" | "backtest"
    strategy: str
    symbol: str
    direction: str  # BULLISH | BEARISH
    ts: int
    entry: float
    stop: float
    targets: tuple[float, ...] = ()
    grade: str = ""
    rr: float = 0.0
    exchange: str = "N"
    session_phase: str = "MID"
    mode: str = ""
    signal: Mapping[str, Any] | None = None  # the ledger's signal row (context, gates, evidence)
    trade: Mapping[str, Any] | None = None  # ledger Trade JSON or BtTrade JSON
    bars_before: Sequence[BarRow] = field(default_factory=tuple)
    bars_after: Sequence[BarRow] = field(default_factory=tuple)


def _r(v: Any, d: int = 4) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, d) if f == f else None  # NaN → None


def atr_proxy(bars: Sequence[BarRow], period: int = 14) -> float | None:
    """Mean true range over the last ``period`` bars. A proxy for a case that has no stored ATR
    (a backtest trade); the signal's own ATR is used whenever it exists."""
    if len(bars) < 2:
        return None
    trs = []
    for prev, b in pairwise(bars):
        trs.append(max(b.h - b.l, abs(b.h - prev.c), abs(b.l - prev.c)))
    tail = trs[-period:]
    return sum(tail) / len(tail) if tail else None


def path_metrics(
    *, bullish: bool, entry: float, stop: float, t1: float | None, bars: Sequence[BarRow]
) -> dict[str, Any]:
    """What the underlying did after the decision, in R (risk = |entry − stop|).

    Same-bar ties go to the stop — the backtester's convention, and the pessimistic one.
    ``max_adverse_before_t1_r`` is the number a post-mortem needs most: the stop distance (in R)
    that would have survived until T1 was touched.
    """
    out: dict[str, Any] = {"path.window_bars": len(bars)}
    risk = abs(entry - stop)
    if risk <= 0 or not bars:
        out["path.note"] = "no risk distance or no bars after the decision"
        return out
    sign = 1.0 if bullish else -1.0

    def fav(b: BarRow) -> float:
        return (b.h - entry) if bullish else (entry - b.l)

    def adv(b: BarRow) -> float:
        return (entry - b.l) if bullish else (b.h - entry)

    t1_dist = abs(t1 - entry) if t1 is not None else None
    bars_to_stop = next((i + 1 for i, b in enumerate(bars) if adv(b) >= risk), None)
    bars_to_t1 = (
        next((i + 1 for i, b in enumerate(bars) if fav(b) >= t1_dist), None)
        if t1_dist is not None
        else None
    )
    if bars_to_stop is None and bars_to_t1 is None:
        first = "NEITHER"
    elif bars_to_t1 is None or (bars_to_stop is not None and bars_to_stop <= bars_to_t1):
        first = "STOP"
    else:
        first = "T1"
    favs = [fav(b) / risk for b in bars]
    advs = [adv(b) / risk for b in bars]
    mfe_i = max(range(len(bars)), key=lambda i: favs[i])
    out.update(
        {
            "path.first_touch": first,
            "path.bars_to_stop": bars_to_stop,
            "path.bars_to_t1": bars_to_t1,
            "path.mfe_r": round(max(favs), 3),
            "path.mfe_bar": mfe_i + 1,
            "path.mae_r": round(-max(advs), 3),
            "path.max_adverse_before_mfe_r": round(-max(advs[: mfe_i + 1]), 3),
            "path.max_adverse_before_t1_r": round(-max(advs[:bars_to_t1]), 3)
            if bars_to_t1
            else None,
        }
    )
    for n in PATH_CHECKPOINTS:
        if len(bars) >= n:
            out[f"path.close_r_{n}b"] = round((bars[n - 1].c - entry) * sign / risk, 3)
    last = bars[-1]
    out["path.direction_right_at_end"] = (last.c - entry) * sign > 0
    return out


def case_pack(case: Case) -> dict[str, Any]:
    ctx = dict((case.signal or {}).get("context") or {})
    ev = dict((case.signal or {}).get("evidence") or {})
    indi = dict(ctx.get("indicators") or {})
    conf = dict(ctx.get("confluence") or {})
    zones = list(ctx.get("zones") or [])
    bullish = case.direction == "BULLISH"
    t1 = case.targets[0] if case.targets else None
    risk = abs(case.entry - case.stop)

    pack: dict[str, Any] = {
        "case.ref": case.ref,
        "case.kind": case.kind,
        "case.strategy": case.strategy,
        "case.symbol": case.symbol,
        "case.direction": case.direction,
        "case.ts_ist": to_ist(case.ts).strftime("%Y-%m-%d %H:%M"),
        "case.session_phase": case.session_phase,
        "case.exchange": case.exchange,
        "case.entry": _r(case.entry, 2),
        "case.stop": _r(case.stop, 2),
        "case.targets": [_r(t, 2) for t in case.targets],
        "case.grade": case.grade,
        "case.rr": _r(case.rr, 2),
    }
    if case.signal is not None:
        pack["case.decision"] = case.signal.get("decision")
        pack["case.decision_reason"] = case.signal.get("decision_reason")

    # -- trigger (stored by the strategy) --------------------------------------------------------
    atr = _r(indi.get("atr") or ev.get("atr"))
    atr_source = "signal"
    if atr is None:
        atr = _r(atr_proxy(list(case.bars_before)))
        atr_source = "proxy_from_bars" if atr is not None else "missing"
    if indi:
        band = indi.get("bb_upper") if bullish else indi.get("bb_lower")
        pack.update(
            {
                "trig.close": _r(case.entry, 2),
                "trig.bb_upper": _r(indi.get("bb_upper"), 2),
                "trig.bb_middle": _r(indi.get("bb_middle"), 2),
                "trig.bb_lower": _r(indi.get("bb_lower"), 2),
                "trig.close_vs_band_pct": _r((case.entry / band - 1) * 100, 3) if band else None,
                "trig.bb_width_pct": _r(
                    (indi["bb_upper"] - indi["bb_lower"]) / indi["bb_middle"] * 100, 3
                )
                if indi.get("bb_middle")
                else None,
                "trig.st_value": _r(indi.get("st_value"), 2),
                "trig.st_trend": indi.get("st_trend"),
                "trig.bars_in_trend": indi.get("bars_in_trend"),
                "trig.bars_since_flip": ev.get("bars_since_flip"),
                "trig.score": ev.get("score"),
                "trig.volume": ev.get("volume"),
                "trig.oi": ev.get("oi"),
                "trig.oi_change_pct": _r(ev.get("oi_change_pct"), 3),
                "trig.params": indi.get("params"),
            }
        )
    else:
        pack["trig.note"] = "no Signal.context on this case (backtest trade or pre-telemetry row)"
    pack["trig.atr"] = atr
    pack["trig.atr_source"] = atr_source
    pack["trig.atr_pct"] = _r(atr / case.entry * 100, 3) if atr and case.entry else None

    # -- levels ---------------------------------------------------------------------------------
    def zone_strength(members: str) -> float | None:
        for z in zones:
            if ",".join(z.get("members", [])) == members:
                return _r(z.get("strength"), 2)
        return None

    ahead = [z for z in zones if (z["price"] > case.entry) == bullish]
    behind = [z for z in zones if (z["price"] > case.entry) != bullish]
    pack.update(
        {
            "lvl.stop_dist_pct": _r(risk / case.entry * 100, 3) if case.entry else None,
            "lvl.stop_dist_atr": _r(risk / atr, 3) if atr else None,
            "lvl.stop_zone": conf.get("stop_zone") or None,
            "lvl.stop_zone_strength": zone_strength(conf["stop_zone"]) if conf.get("stop_zone") else None,
            "lvl.t1": _r(t1, 2),
            "lvl.t1_dist_pct": _r(abs(t1 - case.entry) / case.entry * 100, 3) if t1 and case.entry else None,
            "lvl.t1_dist_atr": _r(abs(t1 - case.entry) / atr, 3) if t1 and atr else None,
            "lvl.t1_zone": (conf.get("target_zones") or [None])[0],
            "lvl.rr": _r(conf.get("rr", case.rr), 2),
            "lvl.grade": conf.get("grade", case.grade),
            "lvl.fortress": _r(conf.get("fortress"), 2),
            "lvl.room_ratio": _r(conf.get("room_ratio"), 2),
            "lvl.n_zones": len(zones) if zones else None,
            "lvl.walls_ahead": sum(1 for z in ahead if z.get("wall")) if zones else None,
            "lvl.walls_behind": sum(1 for z in behind if z.get("wall")) if zones else None,
            "lvl.note": conf.get("note") or None,
            "lvl.policy": conf.get("policy"),
        }
    )
    pack["zones"] = [
        {
            "price": z["price"],
            "vs_entry_pct": _r((z["price"] / case.entry - 1) * 100, 2),
            "strength": z["strength"],
            "wall": bool(z.get("wall")),
            "members": ",".join(z.get("members", [])),
        }
        for z in zones
    ]

    # -- gates and the derived book's own numbers -------------------------------------------
    for g in (case.signal or {}).get("gates") or []:
        name = g.get("name")
        state = "missing" if g.get("missing") else ("pass" if g.get("passed") else "FAIL")
        pack[f"gate.{name}"] = f"{state} (value {g.get('value')} vs {g.get('threshold')})"
    for k, v in (ctx.get("conviction") or {}).items():
        pack[f"conv.{k}"] = v
    for k, v in (ctx.get("volume") or {}).items():
        pack[f"vol.{k}"] = v

    # -- outcome -----------------------------------------------------------------------------
    t = case.trade
    if t is None:
        pack["out.filled"] = False
    else:
        gross = float(t.get("gross") or 0.0)
        charges = float(t.get("charges") or 0.0)
        pack.update(
            {
                "out.filled": True,
                "out.instrument": t.get("symbol") if case.kind == "ledger" else "underlying (backtest)",
                "out.qty": t.get("qty"),
                "out.entry": _r(t.get("entry"), 2),
                "out.exit": _r(t.get("exit"), 2),
                "out.gross": _r(gross, 2),
                "out.charges": _r(charges, 2),
                "out.charges_share_of_gross_pct": _r(charges / abs(gross) * 100, 1) if gross else None,
                "out.net": _r(t.get("net"), 2),
                "out.r": _r(t.get("r_multiple"), 3),
                "out.mfe_r": _r(t.get("mfe_r"), 3),
                "out.mae_r": _r(t.get("mae_r"), 3),
                "out.exit_reason": t.get("exit_reason"),
                "out.bars_held": t.get("bars_held")
                if t.get("bars_held") is not None
                else (max(1, round(float(t.get("duration_s") or 0) / 1800)) if t.get("duration_s") else None),
                "out.duration_min": _r(float(t["duration_s"]) / 60, 0) if t.get("duration_s") else None,
                "out.opt_r_modelled": _r(t.get("opt_r_modelled"), 3),
                "out.opt_net_modelled": _r(t.get("opt_net_modelled"), 2),
            }
        )

    pack.update(path_metrics(bullish=bullish, entry=case.entry, stop=case.stop, t1=t1, bars=case.bars_after))
    pack["book.mode"] = case.mode or None
    pack["bars_before"] = [_bar_row(b) for b in case.bars_before]
    pack["bars_after"] = [_bar_row(b) for b in case.bars_after]
    return pack


def _bar_row(b: BarRow) -> dict[str, Any]:
    return {"t": ist_hm(b.ts), "ts": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v}


def render_case(pack: Mapping[str, Any]) -> str:
    lines = []
    for k, v in pack.items():
        if k in ("zones", "bars_before", "bars_after"):
            continue
        lines.append(f"{k}: {v if v is not None else 'missing'}")
    zones = pack.get("zones") or []
    if zones:
        lines += ["", "zones (price, vs entry %, strength, wall, members):"]
        lines += [
            f"  {z['price']}  {z['vs_entry_pct']:+.2f}%  {z['strength']}  {'WALL' if z['wall'] else '-'}  {z['members']}"
            for z in zones
        ]
    for label, key in (("bars before the decision", "bars_before"), ("bars after the decision (decision frame)", "bars_after")):
        rows = pack.get(key) or []
        if rows:
            lines += ["", f"{label} (IST, o/h/l/c/v):"]
            lines += [f"  {b['t']} o={b['o']} h={b['h']} l={b['l']} c={b['c']} v={b['v']}" for b in rows]
    return "\n".join(lines)


__all__ = ["BarRow", "Case", "atr_proxy", "case_pack", "path_metrics", "render_case"]
