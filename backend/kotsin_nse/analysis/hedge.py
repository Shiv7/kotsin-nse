"""Two hedged option-selling setups from the live chain — illustrations of how the walls, the
Greeks and the bias fit together, NOT recommendations: the engine runs SHADOW and no setup here has
been backtested.

1. **Directional credit spread** — with the bias: bearish → bear call spread (sell a call at or
   above the nearest resistance / call wall, buy the one ``WING`` strikes above, or one strike above
   when the subscribed chain ends sooner); bullish → bull
   put spread (mirror); neutral → the side whose short strike sits further out in σ.
2. **Iron condor** — sell the call and the put nearest ``SHORT_DELTA`` in |Δ| (no nearer the money
   than the walls when the walls are in the chain), buy each wing ``WING`` strikes out.

Per lot: credit, maximum loss (width − credit), breakevens, and a rough chance of expiring with the
whole credit (1 − |Δ| of the short strikes, a risk-neutral approximation). Prices are the last
traded premiums — a fill sits somewhere in the spread.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

SHORT_DELTA = 0.25
WING = 2


def _leg(rows: Sequence[dict[str, Any]], i: int, kind: str) -> dict[str, Any] | None:
    if not 0 <= i < len(rows):
        return None
    q = rows[i].get(kind) or {}
    if q.get("ltp") in (None, 0) or q.get("delta") is None:
        return None
    return {"strike": rows[i]["strike"], "ltp": q["ltp"], "delta": q["delta"], "theta": q.get("theta"), "vega": q.get("vega")}


def _spread(rows: Sequence[dict[str, Any]], short_i: int, kind: str, lot: int) -> dict[str, Any] | None:
    up = kind == "CE"
    s = _leg(rows, short_i, kind)
    # the wing WING strikes out, or one strike out when the subscribed chain ends sooner (ATM ± 4)
    b = next((x for w in (WING, 1) if (x := _leg(rows, short_i + w if up else short_i - w, kind)) is not None), None)
    if s is None or b is None:
        return None
    credit = s["ltp"] - b["ltp"]
    width = abs(b["strike"] - s["strike"])
    if credit <= 0 or width <= 0:
        return None
    be = s["strike"] + credit if up else s["strike"] - credit
    return {
        "legs": [{"action": "SELL", "type": kind, **s}, {"action": "BUY", "type": kind, **b}],
        "credit": round(credit, 2), "width": width, "max_loss": round(width - credit, 2),
        "credit_lot": round(credit * lot), "max_loss_lot": round((width - credit) * lot),
        "breakeven": round(be, 2), "pop_pct": round((1 - abs(s["delta"])) * 100),
        "theta_lot": round(((s["theta"] or 0) - (b["theta"] or 0)) * -lot, 1),  # ₹ a day earned by decay
        "reward_risk": round(credit / (width - credit), 2) if width > credit else None,
    }


def setups(rows: Sequence[dict[str, Any]], future: float, bias: str, lot: int,
           call_wall: float | None, put_wall: float | None, resistance: float | None, support: float | None) -> dict[str, Any]:
    rows = sorted([r for r in rows if r.get("CE") or r.get("PE")], key=lambda r: r["strike"])
    if not rows:
        return {"setups": [], "note": "no option chain"}
    strikes = [r["strike"] for r in rows]

    def nearest_delta(kind: str, floor: float | None) -> int | None:
        cand = [
            i for i, r in enumerate(rows)
            if (r.get(kind) or {}).get("delta") is not None
            and ((r["strike"] > future) if kind == "CE" else (r["strike"] < future))
            and (floor is None or (r["strike"] >= floor if kind == "CE" else r["strike"] <= floor))
        ]
        if not cand:
            return None
        return min(cand, key=lambda i: abs(abs(rows[i][kind]["delta"]) - SHORT_DELTA))

    out: list[dict[str, Any]] = []
    b = bias.lower()
    side = "CE" if "bear" in b else "PE" if "bull" in b else None
    if side is None:  # neutral: the side whose 0.25-delta strike is further from the price
        ci, pi = nearest_delta("CE", None), nearest_delta("PE", None)
        side = "CE" if ci is not None and (pi is None or abs(strikes[ci] - future) >= abs(future - strikes[pi])) else "PE"
    floor = max(x for x in (call_wall, resistance) if x is not None) if side == "CE" and (call_wall or resistance) else \
        min(x for x in (put_wall, support) if x is not None) if side == "PE" and (put_wall or support) else None
    i = nearest_delta(side, floor if floor is not None and (min(strikes) <= floor <= max(strikes)) else None)
    sp = _spread(rows, i, side, lot) if i is not None else None
    if sp:
        out.append({
            "name": "Bear call spread" if side == "CE" else "Bull put spread",
            "why": (f"bias {bias}: sell the call at/above resistance {floor:,.0f}" if side == "CE" else f"bias {bias}: sell the put at/below support {floor:,.0f}")
            if floor else f"bias {bias}: sell the ~{SHORT_DELTA:.2f}-delta {'call' if side == 'CE' else 'put'}",
            **sp,
        })
    ci = nearest_delta("CE", call_wall if call_wall and min(strikes) <= call_wall <= max(strikes) else None)
    pi = nearest_delta("PE", put_wall if put_wall and min(strikes) <= put_wall <= max(strikes) else None)
    cs = _spread(rows, ci, "CE", lot) if ci is not None else None
    ps = _spread(rows, pi, "PE", lot) if pi is not None else None
    if cs and ps:
        credit = cs["credit"] + ps["credit"]
        width = max(cs["width"], ps["width"])
        out.append({
            "name": "Iron condor",
            "why": "range-bound into expiry: short strikes at the walls / ~0.25 delta, wings two strikes out",
            "legs": cs["legs"] + ps["legs"],
            "credit": round(credit, 2), "width": width, "max_loss": round(width - credit, 2),
            "credit_lot": round(credit * lot), "max_loss_lot": round((width - credit) * lot),
            "breakeven": [round(cs["legs"][0]["strike"] + credit, 2), round(ps["legs"][0]["strike"] - credit, 2)],
            "pop_pct": round((1 - abs(cs["legs"][0]["delta"]) - abs(ps["legs"][0]["delta"])) * 100),
            "theta_lot": round(cs["theta_lot"] + ps["theta_lot"], 1),
            "reward_risk": round(credit / (width - credit), 2) if width > credit else None,
        })
    return {"setups": out, "lot": lot, "note": "illustrative only — SHADOW, untested; last-traded premiums, fills differ"}


__all__ = ["SHORT_DELTA", "WING", "setups"]
