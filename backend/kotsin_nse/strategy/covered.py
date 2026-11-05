"""The ₹20 lakh covered-call book, in shadow — the owner (6 Nov 2026): "create cover call or position management
based on real-time market output and assume 20 lakh capital".

The position: long one lot of the front NIFTY future (the index exposure — about ₹16 lakh of notional at 24,500,
so one lot is the size a ₹20 lakh book carries) and short one call of the nearest tracked expiry strictly after
today: the lowest strike at or above the money whose delta is at most ``call_delta`` (the market's own ~30 % chance
of finishing in the money), sold at the bid. The call's premium is the book's income; the future's move its risk.

Management, every minute, from what the engine reads now (the chain's quotes, the IV and the deltas it implies):

* **R1 roll up** — the short call's delta reaches ``roll_delta`` (the index ran through it): buy it back at the ask,
  sell the next ``call_delta`` call of the same expiry.
* **R2 take profit** — the call trades at or under ``take_profit`` of what it was sold for: buy it back, sell a fresh
  ``call_delta`` call (the decay is banked, the next one sold).
* **R3 expiry** — on the call's own expiry day at ``settle_hm``: buy it back at its last price (intrinsic by then)
  and sell the following expiry's call; the chain the engine hands over on expiry day is already the next week's.
* **R4 the future rolls** — when the engine's front contract changes, the leg is re-based at the new front's price
  and the roll's cost is realised.
* **R5 the drawdown stop** — equity ``max_dd`` of the capital below its peak: both legs closed, the book HALTED for
  the day, reopened the next session.

Capital ₹20 lakh; the pair's margin an estimate from settings (check 5paisa's calculator before believing it), the
rest idle. Everything is marked every minute and persisted (``data-live/strategy/book.json``); nothing is ordered.
The P&L of a covered call is the index's move plus the premium minus the calls that went in the money: on a
trend day the book earns less than the index, on a flat day more. It is recorded so the ten-session read can say
which, with its costs, against holding the future alone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..domain import Instrument, InstrumentKind, OrderSide, Segment
from ..fno.chain import greeks76
from ..market.session import ist_hm
from . import straddle as st

#: the book opens at the straddle's hour, after the opening range; it never opens a leg at the open's prices
OPEN_HM = st.OPEN_HM
QuoteOf = Callable[[str], dict[str, Any] | None]


def _fut_instrument(future: dict[str, Any], insts: dict[str, Instrument] | None) -> Instrument:
    inst = (insts or {}).get(str(future["scrip_code"]))
    return inst or Instrument(scrip_code=str(future["scrip_code"]), symbol=future.get("name") or "NIFTY", segment=Segment.NSE_FO,
                              kind=InstrumentKind.FUTURE, lot_size=int(future.get("lot") or 1), expiry=future.get("expiry") or "", underlying="NIFTY")


def _call_instrument(call: dict[str, Any], lot: int, insts: dict[str, Instrument] | None) -> Instrument:
    inst = (insts or {}).get(str(call["scrip_code"]))
    return inst or Instrument(scrip_code=str(call["scrip_code"]), symbol=call.get("name") or "NIFTY", segment=Segment.NSE_FO, kind=InstrumentKind.OPTION,
                              lot_size=lot, expiry=call.get("expiry") or "", strike=float(call["strike"]), underlying="NIFTY")


def choose_call(chain: dict[float, dict[str, Any]], *, spot: float, f: float, t: float, max_delta: float) -> dict[str, Any] | None:
    """The lowest strike at or above the money whose call's delta (from its own IV) is at most ``max_delta``, with
    a usable bid now. None when no strike qualifies (an IV the premium cannot imply, or no fresh quote)."""
    atm = st.atm_strike(spot)
    for k in sorted(k for k in chain if k >= atm):
        iv = st.iv_at(chain, k, "CE", f, t)
        if iv is None:
            continue
        g = greeks76(f, k, t, iv, True)
        if g is None or g["delta"] > max_delta:
            continue
        q = st.leg_quote(chain, k, "CE")
        p = st.px(q, "bid")
        meta = st.leg_meta(chain, k, "CE")
        if p is None or meta is None:
            continue
        return {**meta, "entry": p, "entry_quote": q, "last": p, "delta": round(g["delta"], 3), "iv": round(iv * 100, 2), "theta": round(g["theta"], 2)}
    return None


def new_book(capital: float, margin: float) -> dict[str, Any]:
    return {"status": "FLAT", "capital": capital, "margin_inr": margin, "future": None, "call": None, "calls_closed": [], "events": [],
            "realised_inr": 0.0, "charges_inr": 0.0, "unrealised_inr": 0.0, "equity": capital, "peak": capital, "dd_inr": 0.0,
            "halted_day": None, "opened_day": None, "opened_ts": None, "marks": {}}


def _event(book: dict[str, Any], now: float, day: str, what: str, **detail: Any) -> str:
    book["events"].append({"ts": int(now), "day": day, "hm": ist_hm(now), "what": what, **detail})
    return what


def _sell_call(book: dict[str, Any], chain: dict[float, dict[str, Any]], *, now: float, day: str, spot: float, f: float, t: float, expiry: str,
               max_delta: float) -> str | None:
    c = choose_call(chain, spot=spot, f=f, t=t, max_delta=max_delta)
    if c is None:
        return None
    book["call"] = {**c, "expiry": expiry, "sold_ts": int(now), "sold_hm": ist_hm(now), "sold_day": day, "spot_at_sale": spot, "future_at_sale": f}
    return _event(book, now, day, "sell_call", strike=c["strike"], premium=c["entry"], delta=c["delta"], iv=c["iv"], expiry=expiry)


def _close_call(book: dict[str, Any], *, now: float, day: str, how: str, quote: dict[str, Any] | None, costs: Any, lot: int,
                insts: dict[str, Instrument] | None) -> None:
    call = book["call"]
    if not call:
        return
    p = st.px(quote, "ask", call.get("last") or call["entry"])
    gross = (call["entry"] - p) * lot
    charges = costs.round_trip(_call_instrument(call, lot, insts), call["entry"], p, lot, entry_side=OrderSide.SELL).total
    call.update({"exit": p, "exit_quote": quote, "closed_ts": int(now), "closed_hm": ist_hm(now), "closed_day": day, "how": how,
                 "gross_inr": round(gross, 2), "charges_inr": round(charges, 2), "net_inr": round(gross - charges, 2), "stale_exit": st.stale(quote)})
    book["calls_closed"].append(call)
    book["realised_inr"] = round(book["realised_inr"] + gross - charges, 2)
    book["charges_inr"] = round(book["charges_inr"] + charges, 2)
    book["call"] = None
    _event(book, now, day, "close_call", how=how, strike=call["strike"], premium=call["entry"], exit=p, net_inr=round(gross - charges, 2))


def _close_future(book: dict[str, Any], *, now: float, day: str, how: str, price: float, costs: Any, insts: dict[str, Instrument] | None) -> None:
    fut = book["future"]
    if not fut:
        return
    lot = int(fut["lot"])
    gross = (price - fut["entry"]) * lot
    charges = costs.round_trip(_fut_instrument(fut, insts), fut["entry"], price, lot, entry_side=OrderSide.BUY).total
    book["realised_inr"] = round(book["realised_inr"] + gross - charges, 2)
    book["charges_inr"] = round(book["charges_inr"] + charges, 2)
    _event(book, now, day, "close_future", how=how, entry=fut["entry"], exit=price, net_inr=round(gross - charges, 2))
    book["future"] = None


def mark(book: dict[str, Any], *, day: str, chain: dict[float, dict[str, Any]], fut_ltp: float | None, f: float, t: float, quote_of: QuoteOf | None) -> None:
    """Every leg at its last price, the call's delta from its IV, the equity and the drawdown."""
    fut, call = book.get("future"), book.get("call")
    unreal = 0.0
    if fut and fut_ltp:
        fut["last"] = fut_ltp
        unreal += (fut_ltp - fut["entry"]) * int(fut["lot"])
    if call:
        q = st.leg_quote(chain, call["strike"], "CE") if call.get("expiry") == book.get("chain_expiry") else None
        if q is None and quote_of is not None:
            try:
                q = quote_of(str(call["scrip_code"]))
            except Exception:  # noqa: BLE001
                q = None
        p = st.px(q, "ask")
        if p is not None:
            call["last"], call["last_quote"] = p, q
            if call.get("expiry") == book.get("chain_expiry"):
                iv = st.iv_at(chain, call["strike"], "CE", f, t)
                g = greeks76(f, call["strike"], t, iv, True) if iv is not None else None
                if g is not None:
                    call["delta"], call["iv"] = round(g["delta"], 3), round(iv * 100, 2)
        unreal += (call["entry"] - call["last"]) * int(fut["lot"] if fut else book.get("lot") or 1)
    book["unrealised_inr"] = round(unreal, 2)
    book["equity"] = round(book["capital"] + book["realised_inr"] + unreal, 2)
    book["peak"] = max(book.get("peak") or book["capital"], book["equity"])
    book["dd_inr"] = round(book["equity"] - book["peak"], 2)
    book["marks"][day] = {"equity": book["equity"], "fut": fut and fut.get("last"), "call": call and call.get("last"), "call_strike": call and call.get("strike")}


def step(
    book: dict[str, Any] | None,
    *,
    now: float,
    day: str,
    hm: str,
    spot: float,
    chain: dict[float, dict[str, Any]],
    future: dict[str, Any] | None,
    f: float,
    t: float,
    expiry: str,
    quote_of: QuoteOf | None,
    costs: Any,
    capital: float,
    margin: float,
    call_delta: float,
    roll_delta: float,
    take_profit: float,
    max_dd: float,
    settle_hm: str,
    instruments: dict[str, Instrument] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """One minute of the book: open it when flat (from ``OPEN_HM``), mark it, apply R1–R5. Returns the book and
    the events this minute (empty when only the mark changed)."""
    book = book or new_book(capital, margin)
    book["chain_expiry"] = expiry
    events: list[str] = []
    fut_ltp = (future or {}).get("ltp") or f
    if book["status"] == "HALTED" and book.get("halted_day") != day:
        book["status"] = "FLAT"
    if book["status"] == "FLAT":
        if hm < OPEN_HM or hm > st.LAST_OPEN_HM or not future or not fut_ltp:
            return book, events
        fq = st.px((future or {}).get("quote"), "ask", fut_ltp)
        book["future"] = {"scrip_code": future["scrip_code"], "name": future.get("name"), "expiry": future.get("expiry"), "lot": int(future.get("lot") or 1),
                          "entry": fq, "last": fq, "opened_ts": int(now), "opened_day": day}
        book["lot"] = int(future.get("lot") or 1)
        # the drawdown stop measures from the peak since this opening: a book halted yesterday is not halted again today
        book["peak"] = round(book["capital"] + book["realised_inr"], 2)
        book.update({"status": "OPEN", "opened_day": book.get("opened_day") or day, "opened_ts": book.get("opened_ts") or int(now)})
        events.append(_event(book, now, day, "buy_future", price=fq, lot=book["lot"], notional_inr=round(fq * book["lot"]),
                             exposure_pct=round(fq * book["lot"] / capital * 100, 1)))
        e = _sell_call(book, chain, now=now, day=day, spot=spot, f=f, t=t, expiry=expiry, max_delta=call_delta)
        if e:
            events.append(e)
    if book["status"] != "OPEN":
        return book, events
    lot = int(book["lot"])
    # R4: the engine's front contract changed — re-base the leg, the roll realised
    fut = book["future"]
    if future and fut and future["scrip_code"] != fut["scrip_code"] and fut_ltp:
        _close_future(book, now=now, day=day, how="roll", price=fut.get("last") or fut["entry"], costs=costs, insts=instruments)
        fq = st.px(future.get("quote"), "ask", fut_ltp)
        book["future"] = {"scrip_code": future["scrip_code"], "name": future.get("name"), "expiry": future.get("expiry"), "lot": lot,
                          "entry": fq, "last": fq, "opened_ts": int(now), "opened_day": day}
        events.append(_event(book, now, day, "roll_future", price=fq))
    mark(book, day=day, chain=chain, fut_ltp=fut_ltp, f=f, t=t, quote_of=quote_of)
    call = book["call"]
    if call:
        own_expiry_day = call.get("expiry") == day
        q = call.get("last_quote")
        if own_expiry_day and hm >= settle_hm:
            _close_call(book, now=now, day=day, how="expiry", quote=q, costs=costs, lot=lot, insts=instruments)
            events.append("close_call")
        elif (call.get("delta") or 0) >= roll_delta:
            _close_call(book, now=now, day=day, how="roll_up", quote=q, costs=costs, lot=lot, insts=instruments)
            events.append("close_call")
        elif call.get("last") is not None and call["last"] <= take_profit * call["entry"]:
            _close_call(book, now=now, day=day, how="take_profit", quote=q, costs=costs, lot=lot, insts=instruments)
            events.append("close_call")
    if book["call"] is None and hm <= st.LAST_OPEN_HM:
        e = _sell_call(book, chain, now=now, day=day, spot=spot, f=f, t=t, expiry=expiry, max_delta=call_delta)
        if e:
            events.append(e)
    mark(book, day=day, chain=chain, fut_ltp=fut_ltp, f=f, t=t, quote_of=quote_of)
    # R5: the drawdown stop
    if book["dd_inr"] <= -max_dd * capital:
        if book["call"]:
            _close_call(book, now=now, day=day, how="drawdown", quote=book["call"].get("last_quote"), costs=costs, lot=lot, insts=instruments)
        _close_future(book, now=now, day=day, how="drawdown", price=fut_ltp or book["future"]["entry"], costs=costs, insts=instruments)
        book.update({"status": "HALTED", "halted_day": day, "unrealised_inr": 0.0})
        book["equity"] = round(book["capital"] + book["realised_inr"], 2)
        book["dd_inr"] = round(book["equity"] - book["peak"], 2)
        events.append(_event(book, now, day, "halt", equity=book["equity"], dd_inr=book["dd_inr"]))
    return book, events


def view(book: dict[str, Any] | None, day: str | None = None) -> dict[str, Any] | None:
    """The day json's short form: the state, the legs, today's events, the running tallies."""
    if not book:
        return None
    today = [e for e in book.get("events") or [] if day is None or e.get("day") == day]
    return {k: book.get(k) for k in ("status", "capital", "margin_inr", "future", "call", "realised_inr", "charges_inr", "unrealised_inr", "equity",
                                      "peak", "dd_inr", "opened_day", "halted_day", "chain_expiry")} | {
        "calls_closed": len(book.get("calls_closed") or []), "premium_collected_inr": round(sum(c.get("gross_inr") or 0 for c in book.get("calls_closed") or []), 2),
        "events_today": today, "events": len(book.get("events") or []), "days": len(book.get("marks") or {})}


__all__ = ["choose_call", "mark", "new_book", "step", "view"]
