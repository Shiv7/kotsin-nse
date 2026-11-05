"""The shadow straddle and the per-minute option menu — recorded, never ordered.

The one thing measured positive on the live days (the fact base, 4 Nov 2026): the NIFTY at-the-money straddle
sold at 10:00 and bought back at 15:29 made +56 / +19 / +52 / +25 / −35 / +47 / −30 points on 27 Oct → 5 Nov
(five of seven; the −35 was the expiry day's own 0-DTE chain). The rule as it runs here, the owner's (6 Nov 2026):

* at ``OPEN_HM`` sell the call and the put of the strike nearest the SPOT (the index, rounded to the strike step),
  one lot each, at the bid;
* the expiry is the nearest tracked one strictly after today — on expiry day the following week's ("skip expiry
  day or use the following week": yes);
* buy both back at ``CLOSE_HM`` at the ask. No stop, no target: the measured version. The iron-fly variant (the
  wings ``wing_pts`` away bought at the open, sold at the close) is scored beside it, with its capped loss.

Every minute from the open to the close a *menu* row is kept beside the record: what the ATM straddle, the one-
and two-strike strangles, the iron fly and the skew trade would collect now, each with its breakevens, its capped
loss where one exists, the market's own probability of expiring inside the breakevens (lognormal under the ATM IV
— the price's probability, so a zero expected value before costs; the edge, if any, is realised < implied), the
ATM IV against the open's and the 20-session realised, and the put-call IV gap one strike out. The record answers
the owner's "sell at the best time / change the strike / sell the inflated side" questions from data, in the
evening, instead of from a hunch; a rule is tested only after a pre-registration.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from typing import Any

from ..domain import Instrument, InstrumentKind, OrderSide, Segment
from ..fno.chain import YEAR_S, greeks76, implied_vol
from ..market.session import ist_hm

OPEN_HM = "10:00"
CLOSE_HM = "15:29"
#: a record that could not open by here (a restart) is not opened at all: the measured rule is the morning sale
LAST_OPEN_HM = "15:00"
#: an option quote older than this is no price (the broker re-sends the previous close as a pre-open snapshot)
QUOTE_MAX_AGE_S = 900.0
#: a quote this fresh is traded at its bid / ask; older, at the last trade
FRESH_S = 120.0
STRIKE_STEP = 50.0
IST = timezone(timedelta(hours=5, minutes=30))


# -- the expiry and the clock -------------------------------------------------------------------------------


def pick_expiry(expiries: list[str], today: str) -> tuple[str | None, bool]:
    """The nearest expiry strictly after ``today`` (ISO dates), and whether today's own expiry was skipped."""
    xs = sorted(set(expiries))
    later = [e for e in xs if e > today]
    return (later[0] if later else None), bool(xs) and xs[0] == today


def expiry_t(expiry: str, now: float) -> float:
    """Years from ``now`` to the expiry's 15:30 IST (never under a minute)."""
    d = date.fromisoformat(expiry)
    end = datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp()
    return max(end - now, 60.0) / YEAR_S


def atm_strike(spot: float, step: float = STRIKE_STEP) -> float:
    return round(spot / step) * step


# -- quotes ------------------------------------------------------------------------------------------------


def px(q: dict[str, Any] | None, side: str, fallback: float | None = None) -> float | None:
    """A sale gets the bid and a purchase pays the ask when the quote is fresh; else the last trade; else
    ``fallback``. A quote older than ``QUOTE_MAX_AGE_S`` is no price at all."""
    if not q or age_of(q) > QUOTE_MAX_AGE_S:
        return fallback
    v = q.get(side) if age_of(q) <= FRESH_S else None
    return v or q.get("ltp") or fallback


def age_of(q: dict[str, Any]) -> float:
    """A quote's age in seconds; a missing age is infinite. (An age of exactly 0.0 — a frame this instant — is a fresh
    quote, not a missing one: ``q.get("age_s") or 1e9`` read it as missing, 6 Nov 2026, on the 5 Nov replay.)"""
    a = q.get("age_s")
    return 1e9 if a is None else float(a)


def stale(q: dict[str, Any] | None) -> bool:
    return not q or age_of(q) > QUOTE_MAX_AGE_S


def leg_quote(chain: dict[float, dict[str, Any]], k: float, kind: str) -> dict[str, Any] | None:
    return ((chain.get(k) or {}).get(kind) or {}).get("quote")


def leg_meta(chain: dict[float, dict[str, Any]], k: float, kind: str) -> dict[str, Any] | None:
    leg = (chain.get(k) or {}).get(kind)
    if not leg:
        return None
    return {"scrip_code": leg.get("scrip_code"), "name": leg.get("name"), "strike": k, "kind": kind}


# -- the model ---------------------------------------------------------------------------------------------


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_between(f: float, lo: float | None, hi: float | None, sigma: float | None, t: float) -> float | None:
    """The lognormal probability (drift-free, the option price's own measure) that the future ends between
    ``lo`` and ``hi`` at expiry; None without a volatility."""
    if not sigma or sigma <= 0 or t <= 0 or f <= 0:
        return None
    v = sigma * math.sqrt(t)

    def cdf(x: float | None, default: float) -> float:
        if x is None or x <= 0:
            return default
        return norm_cdf((math.log(x / f) + 0.5 * v * v) / v)

    return round(max(0.0, cdf(hi, 1.0) - cdf(lo, 0.0)), 3)


def iv_at(chain: dict[float, dict[str, Any]], k: float, kind: str, f: float, t: float) -> float | None:
    """One strike's implied volatility (a fraction) from its last trade, or None."""
    q = leg_quote(chain, k, kind)
    if stale(q):
        return None
    return implied_vol(q.get("ltp"), f, k, t, kind == "CE")


def realised_vol(closes: list[float], n: int = 20) -> float | None:
    """Annualised close-to-close volatility over the last ``n`` sessions, in vol points (the IV's unit)."""
    xs = [c for c in closes if c and c > 0][-(n + 1):]
    if len(xs) < 6:
        return None
    rets = [math.log(b / a) for a, b in pairwise(xs)]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var * 252) * 100, 2)


# -- the menu ----------------------------------------------------------------------------------------------


def _credit(chain: dict[float, dict[str, Any]], legs: list[tuple[float, str, str]]) -> float | None:
    """Sum of the legs' prices now: a ``sell`` leg at its bid, a ``buy`` leg at its ask (negative). None when
    any leg has no usable quote."""
    total = 0.0
    for k, kind, side in legs:
        p = px(leg_quote(chain, k, kind), "bid" if side == "sell" else "ask")
        if p is None:
            return None
        total += p if side == "sell" else -p
    return round(total, 2)


def menu_row(
    chain: dict[float, dict[str, Any]],
    *,
    now: float,
    spot: float,
    f: float,
    t: float,
    lot: int,
    wing_pts: float,
    iv_open: float | None,
    rv20: float | None,
    step: float = STRIKE_STEP,
) -> dict[str, Any]:
    """What each structure collects now and what the model says about it (see the module docstring)."""
    k = atm_strike(spot, step)
    ivs = [v for v in (iv_at(chain, k, "CE", f, t), iv_at(chain, k, "PE", f, t)) if v is not None]
    sigma = sum(ivs) / len(ivs) if ivs else None
    iv = round(sigma * 100, 2) if sigma is not None else None
    row: dict[str, Any] = {"hm": ist_hm(now), "spot": spot, "fut": f, "atm": k, "iv": iv,
                           "d_iv": round(iv - iv_open, 2) if iv is not None and iv_open is not None else None, "rv20": rv20,
                           "iv_over_rv": round(iv / rv20, 2) if iv and rv20 else None}

    def structure(name: str, legs: list[tuple[float, str, str]], lo_k: float, hi_k: float, capped: float | None) -> None:
        credit = _credit(chain, legs)
        if credit is None:
            row[name] = None
            return
        be = (round(lo_k - credit, 1), round(hi_k + credit, 1))
        ks = [k2 for k2, _, _ in legs]
        out = {"k": ks[0] if len(set(ks)) == 1 else ks, "credit": credit, "credit_inr": round(credit * lot),
               "be": list(be), "p": p_between(f, be[0], be[1], sigma, t)}
        if capped is not None:
            out["max_loss_inr"] = round(max(capped - credit, 0.0) * lot)
        row[name] = out

    structure("straddle", [(k, "CE", "sell"), (k, "PE", "sell")], k, k, None)
    structure("strangle1", [(k - step, "PE", "sell"), (k + step, "CE", "sell")], k - step, k + step, None)
    structure("strangle2", [(k - 2 * step, "PE", "sell"), (k + 2 * step, "CE", "sell")], k - 2 * step, k + 2 * step, None)
    wing = round(wing_pts / step) * step
    structure("fly", [(k, "CE", "sell"), (k, "PE", "sell"), (k + wing, "CE", "buy"), (k - wing, "PE", "buy")], k, k, wing)
    # the skew one strike out: the put's IV below against the call's IV above, and the trade that sells the richer
    # side and buys the cheaper one — a risk reversal, i.e. a position in the index, which is what its P&L will be
    ce_iv, pe_iv = iv_at(chain, k + step, "CE", f, t), iv_at(chain, k - step, "PE", f, t)
    if ce_iv is not None and pe_iv is not None:
        row["skew"] = round((pe_iv - ce_iv) * 100, 2)
        rich, cheap = (("PE", k - step, pe_iv), ("CE", k + step, ce_iv)) if pe_iv > ce_iv else (("CE", k + step, ce_iv), ("PE", k - step, pe_iv))
        credit = _credit(chain, [(rich[1], rich[0], "sell"), (cheap[1], cheap[0], "buy")])
        g_rich = greeks76(f, rich[1], t, rich[2], rich[0] == "CE")
        g_cheap = greeks76(f, cheap[1], t, cheap[2], cheap[0] == "CE")
        delta = round(-(g_rich["delta"] if g_rich else 0) + (g_cheap["delta"] if g_cheap else 0), 3)
        row["skew_trade"] = {"sell": f"{rich[0]} {rich[1]:.0f}", "buy": f"{cheap[0]} {cheap[1]:.0f}", "credit": credit,
                             "iv_gap": round((rich[2] - cheap[2]) * 100, 2), "delta": delta}
    else:
        row["skew"], row["skew_trade"] = None, None
    return row


# -- the record --------------------------------------------------------------------------------------------


def open_record(
    chain: dict[float, dict[str, Any]],
    *,
    now: float,
    day: str,
    spot: float,
    f: float,
    expiry: str,
    expiry_day_skipped: bool,
    lot: int,
    wing_pts: float,
    cond: dict[str, Any],
    step: float = STRIKE_STEP,
    target_pts: float = 0.0,
    stop_pts: float = 0.0,
) -> dict[str, Any] | None:
    """Sell the ATM straddle now (the bids), and buy the fly's wings beside it. None when a leg has no usable
    quote — the record is not opened on a snapshot price. With a ``target_pts`` or ``stop_pts`` a *managed* variant
    is scored beside the hold-to-the-close baseline: the same sale, bought back the minute the running P&L reaches
    the target or the stop (the owner, 6 Nov 2026: "if we are getting 76 or 90 points let's book it and lose only
    19"); the baseline is untouched, so the evening read has both, and the minute ``path`` lets it score any pair."""
    k = atm_strike(spot, step)
    legs: dict[str, dict[str, Any]] = {}
    for kind in ("CE", "PE"):
        meta, q = leg_meta(chain, k, kind), leg_quote(chain, k, kind)
        p = px(q, "bid")
        if meta is None or p is None:
            return None
        legs[kind] = {**meta, "entry": p, "entry_quote": q, "last": p, "exit": None, "exit_quote": None}
    wing = round(wing_pts / step) * step
    wings: dict[str, dict[str, Any]] | None = {}
    for kind, kw in (("CE", k + wing), ("PE", k - wing)):
        meta, q = leg_meta(chain, kw, kind), leg_quote(chain, kw, kind)
        p = px(q, "ask")
        if meta is None or p is None:
            wings = None
            break
        wings[kind] = {**meta, "entry": p, "entry_quote": q, "last": p, "exit": None, "exit_quote": None}
    credit = round(legs["CE"]["entry"] + legs["PE"]["entry"], 2)
    hm = ist_hm(now)
    rec: dict[str, Any] = {
        "status": "OPEN", "day": day, "opened_ts": int(now), "opened_hm": hm, "late": hm > OPEN_HM,
        "expiry": expiry, "days": (date.fromisoformat(expiry) - date.fromisoformat(day)).days, "expiry_day_skipped": expiry_day_skipped,
        "strike": k, "spot": spot, "future": f, "lot": lot, "legs": legs, "credit": credit, "credit_inr": round(credit * lot),
        "wings": wings, "wing_pts": wing if wings else None,
        "fly_credit": round(credit - wings["CE"]["entry"] - wings["PE"]["entry"], 2) if wings else None,
        "cond": cond, "menu": [],
        "closed_ts": None, "closed_hm": None, "debit": None, "pnl_pts": None, "gross_inr": None, "charges_inr": None, "net_inr": None,
        "stale_exit": None, "fly": None,
    }
    rec["fly_max_loss_inr"] = round(max(wing - rec["fly_credit"], 0.0) * lot) if wings else None
    rec["path"], rec["mfe"], rec["mae"] = [], None, None
    rec["managed"] = ({"target_pts": target_pts, "stop_pts": stop_pts, "status": "OPEN", "how": None, "closed_hm": None, "debit": None,
                       "pnl_pts": None, "gross_inr": None, "charges_inr": None, "net_inr": None, "stale_exit": None}
                      if (target_pts > 0 or stop_pts > 0) else None)
    return rec


def mark_record(rec: dict[str, Any], chain: dict[float, dict[str, Any]], now: float | None = None) -> None:
    """The legs' last prices now (a fresh quote only), the straddle's running P&L in points, its path and its best
    and worst point so far."""
    if rec.get("status") != "OPEN":
        return
    for group, side in (("legs", "ask"), ("wings", "bid")):
        for leg in (rec.get(group) or {}).values():
            q = leg_quote(chain, leg["strike"], leg["kind"])
            p = px(q, side)
            if p is not None:
                leg["last"], leg["last_quote"] = p, q
    rec["last_debit"] = round(sum(x["last"] for x in rec["legs"].values()), 2)
    rec["last_pnl_pts"] = round(rec["credit"] - rec["last_debit"], 2)
    if now is not None:
        hm = ist_hm(now)
        path = rec.setdefault("path", [])
        if not path or path[-1][0] != hm:
            path.append([hm, rec["last_pnl_pts"]])
        if rec.get("mfe") is None or rec["last_pnl_pts"] > rec["mfe"][1]:
            rec["mfe"] = [hm, rec["last_pnl_pts"]]
        if rec.get("mae") is None or rec["last_pnl_pts"] < rec["mae"][1]:
            rec["mae"] = [hm, rec["last_pnl_pts"]]


def _instrument(leg: dict[str, Any], lot: int, insts: dict[str, Instrument] | None) -> Instrument:
    inst = (insts or {}).get(str(leg["scrip_code"]))
    return inst or Instrument(scrip_code=str(leg["scrip_code"]), symbol=leg.get("name") or "NIFTY", segment=Segment.NSE_FO, kind=InstrumentKind.OPTION,
                              lot_size=lot, strike=float(leg["strike"]), underlying="NIFTY")


def _buy_back(rec: dict[str, Any], chain: dict[float, dict[str, Any]], costs: Any, instruments: dict[str, Instrument] | None) -> dict[str, Any]:
    """What buying the straddle's two legs back now costs: each at its ask (a stale leg at its last fresh mark), the
    gross against the sale, the charges of each leg's round trip entered on the sell side."""
    lot = int(rec["lot"])
    exits: dict[str, tuple[float, dict[str, Any] | None]] = {}
    gross, charges, any_stale = 0.0, 0.0, False
    for kind, leg in rec["legs"].items():
        q = leg_quote(chain, leg["strike"], leg["kind"])
        p = px(q, "ask", leg.get("last") or leg["entry"])
        any_stale |= stale(q)
        exits[kind] = (p, q)
        gross += (leg["entry"] - p) * lot
        charges += costs.round_trip(_instrument(leg, lot, instruments), leg["entry"], p, lot, entry_side=OrderSide.SELL).total
    debit = round(sum(p for p, _ in exits.values()), 2)
    return {"exits": exits, "debit": debit, "pnl_pts": round(rec["credit"] - debit, 2), "gross_inr": round(gross, 2), "charges_inr": round(charges, 2),
            "net_inr": round(gross - charges, 2), "stale_exit": any_stale}


def manage_record(rec: dict[str, Any], chain: dict[float, dict[str, Any]], costs: Any, *, now: float,
                  instruments: dict[str, Instrument] | None = None) -> str | None:
    """The managed variant: bought back the minute the running P&L is at or past its target or its stop. Returns
    "target" / "stop" when it closes this minute, else None. The baseline record is not touched."""
    m = rec.get("managed")
    if rec.get("status") != "OPEN" or not m or m.get("status") != "OPEN" or rec.get("last_pnl_pts") is None:
        return None
    pnl = rec["last_pnl_pts"]
    how = "target" if m["target_pts"] > 0 and pnl >= m["target_pts"] else ("stop" if m["stop_pts"] > 0 and pnl <= -m["stop_pts"] else None)
    if how is None:
        return None
    bb = _buy_back(rec, chain, costs, instruments)
    m.update({"status": "CLOSED", "how": how, "closed_hm": ist_hm(now), "closed_ts": int(now), **{k: bb[k] for k in ("debit", "pnl_pts", "gross_inr", "charges_inr", "net_inr", "stale_exit")}})
    return how


def close_record(rec: dict[str, Any], chain: dict[float, dict[str, Any]], costs: Any, *, now: float, how: str = "close",
                 instruments: dict[str, Instrument] | None = None) -> None:
    """Buy the straddle back (the asks), sell the wings (the bids), and settle: points, rupees, charges — the
    charges from the cost model, each leg a round trip entered on the sell side. A leg without a usable quote
    closes at its last fresh mark and the record says ``stale_exit``. A managed variant still open closes here too."""
    if rec.get("status") != "OPEN":
        return
    lot = int(rec["lot"])
    bb = _buy_back(rec, chain, costs, instruments)
    for kind, (p, q) in bb["exits"].items():
        rec["legs"][kind].update({"exit": p, "exit_quote": q, "last": p})
    wg, wc = 0.0, 0.0
    for leg in (rec.get("wings") or {}).values():
        q = leg_quote(chain, leg["strike"], leg["kind"])
        p = px(q, "bid", leg.get("last") or leg["entry"])
        leg["exit"], leg["exit_quote"], leg["last"] = p, q, p
        leg["gross_inr"] = round((p - leg["entry"]) * lot, 2)
        leg["charges_inr"] = round(costs.round_trip(_instrument(leg, lot, instruments), leg["entry"], p, lot, entry_side=OrderSide.BUY).total, 2)
        wg += leg["gross_inr"]
        wc += leg["charges_inr"]
    rec.update({"status": "CLOSED", "how": how, "closed_ts": int(now), "closed_hm": ist_hm(now), "debit": bb["debit"], "pnl_pts": bb["pnl_pts"],
                "gross_inr": bb["gross_inr"], "charges_inr": bb["charges_inr"], "net_inr": bb["net_inr"], "stale_exit": bb["stale_exit"],
                "last_debit": bb["debit"], "last_pnl_pts": bb["pnl_pts"]})
    if rec.get("wings"):
        w = rec["wings"]
        rec["fly"] = {"debit": round(w["CE"]["exit"] + w["PE"]["exit"], 2), "pnl_pts": round(rec["fly_credit"] - (bb["debit"] - w["CE"]["exit"] - w["PE"]["exit"]), 2),
                      "gross_inr": round(bb["gross_inr"] + wg, 2), "charges_inr": round(bb["charges_inr"] + wc, 2), "net_inr": round(bb["gross_inr"] + wg - bb["charges_inr"] - wc, 2)}
    m = rec.get("managed")
    if m and m.get("status") == "OPEN":
        m.update({"status": "CLOSED", "how": how, "closed_hm": ist_hm(now), "closed_ts": int(now),
                  **{k: bb[k] for k in ("debit", "pnl_pts", "gross_inr", "charges_inr", "net_inr", "stale_exit")}})


def summary(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    """The day json's short form: the record without its minute rows."""
    if not rec:
        return None
    return {k: v for k, v in rec.items() if k not in ("menu", "path")} | {"menu_rows": len(rec.get("menu") or []), "path_rows": len(rec.get("path") or [])}


__all__ = ["CLOSE_HM", "LAST_OPEN_HM", "OPEN_HM", "age_of", "atm_strike", "close_record", "expiry_t", "manage_record", "mark_record", "menu_row",
           "open_record", "p_between", "pick_expiry", "px", "realised_vol", "summary"]
