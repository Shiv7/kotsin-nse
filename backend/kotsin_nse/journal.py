"""The owner's call journal: a discretionary call, logged the moment it is seen, scored afterwards
from the bars that follow — the only way to measure a method that lives in the trader's judgement.

Every mechanical version of the owner's method (double bottom, CPR retest, the full sequence) is
zero before costs (HANDOFF §3.4 rows 10–12). What those tests cannot see is *which* setups he takes
and which he skips. The journal records the choice at the time, with the price on the screen, and the
engine scores it from the 1-minute bars with no hand in it: filled or not, stop or target first,
best and worst excursion, the R at 15 / 30 / 60 minutes and at the close.

Rules of the scoring, so the number cannot be argued with later:

* **Fill.** An ``entry`` is filled by the first 1m bar (the one forming when the call was logged counts)
  whose range covers the entry level; a call whose level is never reached by the close is *not filled*
  and scores nothing. An ``alert`` is the owner saying "watch this" — it is scored the same way, so an
  alert that would have worked and an entry that did are both counted, but kept apart.
* **Risk.** One R = |entry − stop|. A call without a stop gets a default risk of ``DEFAULT_RISK_PCT`` of
  the entry and is flagged, so it is never confused with a planned trade.
* **Stop before target.** A bar that touches both counts as the stop — the pessimistic reading.
* **The close.** A call still open at ``EOD_HM`` IST is closed at that bar's close; nothing is carried
  overnight, which is how the owner trades.
* **Time marks** ``r_15m / r_30m / r_60m`` are the R at the close of the last bar within that many
  minutes of the call (not of the fill): the question is "what happened after he said it".

Nothing here reads the engine: the scorer takes bars and returns a dict, so it is testable and the
engine is a thin caller (``Engine._score_journal``).
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Any

from .market.session import ist_at, ist_day

SIDES = ("LONG", "SHORT")
KINDS = ("entry", "alert")
STATUSES = ("OPEN", "CLOSED", "CANCELLED")
#: the owner's confirmation stack, as tags; free text is allowed too
TAGS = ("1H reversal", "15m Supertrend", "5m double bottom", "CPR retest", "pivot reject", "weekly pivot",
        "OI confirms", "flow confirms", "gap / auction", "news", "trend continuation")
DEFAULT_RISK_PCT = 0.5
EOD_HM = "15:25"
MARKS_MIN = (15, 30, 60)


def new_call(symbol: str, side: str, kind: str, *, price: float | None, entry: float | None, stop: float | None,
             target: float | None, tags: list[str], note: str, ts: float | None = None) -> dict[str, Any]:
    """A fresh journal row. ``price`` is what the screen showed; ``entry`` defaults to it."""
    side, kind = side.upper(), kind.lower()
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    ts = time.time() if ts is None else ts
    entry = entry if entry is not None else price
    if entry is None or entry <= 0:
        raise ValueError("no entry price: the engine has no last price for this symbol and none was given")
    if stop is not None and ((side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry)):
        raise ValueError("the stop is on the wrong side of the entry")
    if target is not None and ((side == "LONG" and target <= entry) or (side == "SHORT" and target >= entry)):
        raise ValueError("the target is on the wrong side of the entry")
    risk = abs(entry - stop) if stop is not None else entry * DEFAULT_RISK_PCT / 100
    return {"id": uuid.uuid4().hex[:10], "ts": ts, "day": ist_day(ts).isoformat(), "symbol": symbol.upper(), "side": side,
            "kind": kind, "price_at_call": price, "entry": float(entry), "stop": stop, "target": target,
            "risk": float(risk), "risk_is_default": stop is None, "tags": [t for t in tags if t][:12], "note": note[:500],
            "status": "OPEN", "outcome": None, "closed_ts": None, "close_reason": None}


def score(call: dict[str, Any], bars: list[tuple[float, float, float, float, float]], now: float) -> dict[str, Any]:
    """Score one call from 1m bars ``(ts, o, h, l, c)`` (any order, any range; the bar forming when
    the call was logged counts). Returns the outcome; ``final`` says whether the call is over."""
    side, entry, risk = call["side"], float(call["entry"]), float(call["risk"])
    sign = 1.0 if side == "LONG" else -1.0
    stop, target = call.get("stop"), call.get("target")
    t0 = call["ts"] - (call["ts"] % 60)  # the start of the minute the call was logged in
    day = date.fromisoformat(call["day"])
    eod = ist_at(day, EOD_HM)
    rows = sorted((b for b in bars if t0 <= b[0] <= eod), key=lambda b: b[0])
    out: dict[str, Any] = {"bars": len(rows), "last_ts": rows[-1][0] if rows else None, "filled_ts": None, "hit": None, "hit_ts": None,
                           "r_now": None, "mfe_r": None, "mae_r": None, "r_close": None, "final": False, "eod_ts": eod}
    for m in MARKS_MIN:
        out[f"r_{m}m"] = None
    filled = False
    hi = lo = None
    last_c = None
    for ts, _o, h, lw, c in rows:
        if not filled:
            if lw <= entry <= h:
                filled = True
                out["filled_ts"] = ts
                hi, lo = max(h, entry), min(lw, entry)
            else:
                continue
        else:
            hi, lo = max(hi, h), min(lo, lw)  # type: ignore[arg-type]
        last_c = c
        r_c = sign * (c - entry) / risk
        for m in MARKS_MIN:
            if ts + 60 <= call["ts"] + m * 60 or (ts <= call["ts"] + m * 60 < ts + 60):
                out[f"r_{m}m"] = round(r_c, 3)
        if out["hit"] is None:
            stopped = stop is not None and ((side == "LONG" and lw <= stop) or (side == "SHORT" and h >= stop))
            targeted = target is not None and ((side == "LONG" and h >= target) or (side == "SHORT" and lw <= target))
            if stopped:
                out["hit"], out["hit_ts"] = "stop", ts
            elif targeted:
                out["hit"], out["hit_ts"] = "target", ts
    if filled:
        out["mfe_r"] = round(sign * ((hi if side == "LONG" else lo) - entry) / risk, 3)  # type: ignore[operator]
        out["mae_r"] = round(sign * ((lo if side == "LONG" else hi) - entry) / risk, 3)  # type: ignore[operator]
        if out["hit"] == "stop":
            out["r_now"] = -1.0
        elif out["hit"] == "target":
            out["r_now"] = round(sign * (target - entry) / risk, 3)  # type: ignore[operator]
        elif last_c is not None:
            out["r_now"] = round(sign * (last_c - entry) / risk, 3)
    past_eod = now >= eod + 60 or (rows and rows[-1][0] >= eod)
    if out["hit"] is not None:
        out["final"] = True
    elif past_eod:
        out["final"] = True
        if filled and last_c is not None:
            out["r_close"] = out["r_now"]
    return out


def close_reason(outcome: dict[str, Any]) -> str:
    if outcome.get("hit") == "stop":
        return "stop"
    if outcome.get("hit") == "target":
        return "target"
    if outcome.get("filled_ts") is None:
        return "not filled"
    return "close"


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What the journal says so far: by kind, and by tag — counts, fills, stop/target, average R."""

    def block(rs: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [r for r in rs if r.get("outcome") and r["outcome"].get("filled_ts") is not None]
        done = [r for r in scored if r["status"] == "CLOSED"]
        rr = [r["outcome"]["r_now"] for r in done if r["outcome"].get("r_now") is not None]
        return {"calls": len(rs), "filled": len(scored), "closed": len(done),
                "targets": sum(1 for r in done if r["outcome"].get("hit") == "target"),
                "stops": sum(1 for r in done if r["outcome"].get("hit") == "stop"),
                "avg_r": round(sum(rr) / len(rr), 3) if rr else None, "sum_r": round(sum(rr), 2) if rr else None,
                "win_rate": round(sum(1 for x in rr if x > 0) / len(rr) * 100, 1) if rr else None,
                "avg_mfe_r": round(sum(r["outcome"]["mfe_r"] for r in scored) / len(scored), 3) if scored else None,
                "avg_mae_r": round(sum(r["outcome"]["mae_r"] for r in scored) / len(scored), 3) if scored else None}

    live = [r for r in rows if r["status"] != "CANCELLED"]
    tags = sorted({t for r in live for t in r.get("tags", [])})
    return {"all": block(live), "by_kind": {k: block([r for r in live if r["kind"] == k]) for k in KINDS},
            "by_side": {s: block([r for r in live if r["side"] == s]) for s in SIDES},
            "by_tag": {t: block([r for r in live if t in r.get("tags", [])]) for t in tags}}


__all__ = ["DEFAULT_RISK_PCT", "EOD_HM", "KINDS", "SIDES", "TAGS", "close_reason", "new_call", "score", "summary"]
