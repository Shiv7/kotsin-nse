"""The intraday option records the daily bhavcopy cannot test, kept live from the engine's own quotes beside the 10:00
straddle (strategy/straddle.py) — the two transcripts the owner pasted on 6 Nov 2026 that trade inside the day:

* ``sleep`` — the "sleep strategy": on the day before and the day of the nearest weekly expiry (DT1, DT0), at 09:22
  sell the call and the put whose premiums are nearest ₹75, one lot each; each leg has a stop at 150 % of its sale;
  when one leg is stopped the other's stop moves to its own sale price; everything still open is bought back at 15:15.
* ``expiry_otm`` — "The Options Seller": on expiry day only (DT0), at 09:30 sell the out-of-the-money call and put
  whose premiums are nearest ₹6, stop at 150 % of the sale (his 50 paise over the third candle on a ₹6 sale), bought
  back at 15:15.

Both are recorded at the bid when sold and the ask when bought back, with the cost model's charges; nothing is ordered.
The record is the only history these rules will ever have here — the fact base's expiry-day straddle lost, and one of
the interviewed traders says expiry-day spikes now turn ₹1 options into ₹100 — so the first ten sessions are a read,
not a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain import Instrument, InstrumentKind, OrderSide, Segment
from ..market.session import ist_hm
from . import straddle as st


@dataclass(frozen=True, slots=True)
class Spec:
    name: str
    open_hm: str
    close_hm: str
    premium: float  # the premium each sold leg is picked nearest to
    stop_mult: float = 1.5  # a leg is bought back when its ask reaches this multiple of its sale
    to_cost: bool = True  # after one leg's stop the other's stop moves to its own sale price
    dte: tuple[int, ...] = (0, 1)  # the days-to-expiry on which the record runs


SPECS: dict[str, Spec] = {
    "sleep": Spec("sleep", "09:22", "15:15", 75.0, 1.5, True, (0, 1)),
    "expiry_otm": Spec("expiry_otm", "09:30", "15:15", 6.0, 1.5, False, (0,)),
}


def _nearest(chain: dict[float, dict[str, Any]], kind: str, spot: float, premium: float) -> tuple[float, dict[str, Any] | None, float | None]:
    best, bk, bq, bp = 1e18, None, None, None
    for k, legs in chain.items():
        if (kind == "CE" and k <= spot) or (kind == "PE" and k >= spot):
            continue
        q = (legs.get(kind) or {}).get("quote")
        p = st.px(q, "bid")
        if p is None:
            continue
        if abs(p - premium) < best:
            best, bk, bq, bp = abs(p - premium), k, q, p
    return bk, bq, bp  # type: ignore[return-value]


def open_record(spec: Spec, chain: dict[float, dict[str, Any]], *, now: float, day: str, spot: float, expiry: str, dte: int, lot: int) -> dict[str, Any] | None:
    legs: dict[str, dict[str, Any]] = {}
    for kind in ("CE", "PE"):
        k, q, p = _nearest(chain, kind, spot, spec.premium)
        meta = st.leg_meta(chain, k, kind) if k is not None else None
        if meta is None or p is None:
            return None
        legs[kind] = {**meta, "entry": p, "entry_quote": q, "last": p, "stop": round(p * spec.stop_mult, 2), "status": "OPEN", "exit": None, "exit_quote": None,
                      "how": None, "closed_hm": None}
    return {"name": spec.name, "status": "OPEN", "day": day, "expiry": expiry, "dte": dte, "opened_ts": int(now), "opened_hm": ist_hm(now), "spot": spot, "lot": lot,
            "legs": legs, "credit": round(sum(x["entry"] for x in legs.values()), 2), "path": [], "pnl_pts": None, "gross_inr": None, "charges_inr": None,
            "net_inr": None, "closed_hm": None, "stale_exit": False}


def _close_leg(rec: dict[str, Any], kind: str, chain: dict[float, dict[str, Any]], costs: Any, how: str, now: float) -> None:
    leg = rec["legs"][kind]
    q = st.leg_quote(chain, leg["strike"], leg["kind"])
    p = st.px(q, "ask", leg.get("last") or leg["entry"])
    lot = int(rec["lot"])
    inst = Instrument(scrip_code=str(leg["scrip_code"]), symbol=leg.get("name") or "NIFTY", segment=Segment.NSE_FO, kind=InstrumentKind.OPTION, lot_size=lot,
                      strike=float(leg["strike"]), underlying="NIFTY")
    charges = costs.round_trip(inst, leg["entry"], p, lot, entry_side=OrderSide.SELL).total
    leg.update({"status": "CLOSED", "exit": p, "exit_quote": q, "last": p, "how": how, "closed_hm": ist_hm(now), "gross_inr": round((leg["entry"] - p) * lot, 2),
                "charges_inr": round(charges, 2), "stale": st.stale(q)})


def manage(rec: dict[str, Any], spec: Spec, chain: dict[float, dict[str, Any]], costs: Any, *, now: float) -> list[str]:
    """Mark the legs, apply the stops (and the move-to-cost), close at ``close_hm``. Returns the events."""
    if rec.get("status") != "OPEN":
        return []
    hm = ist_hm(now)
    events: list[str] = []
    for kind, leg in rec["legs"].items():
        if leg["status"] != "OPEN":
            continue
        q = st.leg_quote(chain, leg["strike"], leg["kind"])
        p = st.px(q, "ask")
        if p is not None:
            leg["last"] = p
        if p is not None and p >= leg["stop"]:
            _close_leg(rec, kind, chain, costs, "stop", now)
            events.append(f"{hm} {spec.name}: {kind} {leg['strike']:.0f} stopped at {p} (sold {leg['entry']})")
            if spec.to_cost:
                other = rec["legs"]["PE" if kind == "CE" else "CE"]
                if other["status"] == "OPEN" and other["stop"] > other["entry"]:
                    other["stop"] = other["entry"]
                    events.append(f"{hm} {spec.name}: the other leg's stop moved to cost {other['entry']}")
    open_legs = [x for x in rec["legs"].values() if x["status"] == "OPEN"]
    pnl = sum(x["entry"] - x["last"] for x in open_legs) + sum(x["entry"] - x["exit"] for x in rec["legs"].values() if x["status"] == "CLOSED")
    if not rec["path"] or rec["path"][-1][0] != hm:
        rec["path"].append([hm, round(pnl, 2)])
    if hm >= spec.close_hm and open_legs:
        for kind, leg in rec["legs"].items():
            if leg["status"] == "OPEN":
                _close_leg(rec, kind, chain, costs, "close", now)
        events.append(f"{hm} {spec.name}: closed at the bell")
    if all(x["status"] == "CLOSED" for x in rec["legs"].values()):
        gross = sum(x["gross_inr"] for x in rec["legs"].values())
        charges = sum(x["charges_inr"] for x in rec["legs"].values())
        rec.update({"status": "CLOSED", "closed_hm": hm, "pnl_pts": round(sum(x["entry"] - x["exit"] for x in rec["legs"].values()), 2), "gross_inr": round(gross, 2),
                    "charges_inr": round(charges, 2), "net_inr": round(gross - charges, 2), "stale_exit": any(x.get("stale") for x in rec["legs"].values())})
    return events


__all__ = ["SPECS", "Spec", "manage", "open_record"]
