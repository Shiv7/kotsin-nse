"""Every open trade, one card each (operator, 2026-10-09: "in the dashabpord have a dedicated page for all actove trades ...
in the form of cards ... each card with that OTM's orderbook refreshed last time ... if it is older than our threshold, flag
it on the card and also in reason for exit: that orderbook was stale, metion lots, mention strategyname, engine name, charges
so far , slipage cost if any, potental charges at exit and approx slipage at every refresh rate if we had to exit at LTP").

RECORD-ONLY. ``/api/positions/live`` reads the engine's open positions, its quotes and depth books, the paper matcher's own
book-age limit, the charge model and the stored orders, and returns them. Nothing here moves an order, a stop, a wallet or a
decision, and nothing in the order path imports this module (tests/test_positions_live.py checks both). No engine cache is
filled from here either: the front future is peeked at, never resolved into ``Engine._front_code`` (review, 9 Oct 2026: a
poll while the contract list was still loading saved "no front future" for the day, and the OI handler then dropped that
stock's front-month OI). ``/api/trades`` reads the closed side of the same question here: the exits refused for a stale book,
and how old the book was that priced the closing cross.

How each number is worked out:

* **the quote**: the last trade, bid, ask and mid all from the contract's quote, the one the exit loop judges the stop on
  and marks the excursions at (``Engine._manage_positions``: ``MarketView.option_mid`` and ``option_bid``). One feed for the
  four (review, 9 Oct 2026: a bid and ask from the depth beside a mid from the quote read 11.10 / 11.45 with a mid of 10.50).
* **unrealised**: the open lots marked as the engine marks its excursions (risk/exits.py ``_track``): at the quote's bid
  while the quote is fresh enough for the exit loop (``position_quote_max_age_s``), else at the last trade; a short at the
  ask. It is **neutral** while it is within the round trip's charges: the entry's and those an exit now would pay. The
  slices already sold (target rungs) are shown beside it, not in it.
* **the book**: the contract's depth book as the paper matcher reads it (``Engine.books``). Its age now is judged against
  the matcher's own limit (``PaperMatcher.age_limit_ms``, from the settings: 6 s, 10 s in the opening window). Older is
  STALE: an exit now would be refused, as the stored REJECTED exits show ("cross failed: book for X is 12545 ms old").
* **exit now**: on a fresh book, the open lots walked down its bids, measured against the last trade and that book's own
  touch and mid. What the five levels cannot hold is priced at the last level shown and flagged, and the part the matcher
  would sell in one cross (within its ceiling under the touch) is kept beside it. On a stale book its levels are not prices
  anyone is showing, so their SHAPE is moved to the quote's bid and walked, against the quote's mid: an approximation
  (ABCAPITAL, 9 Oct 10:03:13: the 9.5 s old book moved to the quote's 4.30 walks to 4.2625; the stop sold 0.25 s later at
  4.26). With no book, the quote's bid alone.
* **each target**: the rung's share walked down the bid ladder's shape, every level moved by the distance from the touch to
  the rung. An approximation of the queue there, not a forecast of it.
* **charges**: those paid so far (the entry, and any slice sold), and an exit now under the charge model (``CostModel.leg``,
  data/charges.toml): the open lots sold at the mark.
* **slippage so far**: the entry fill against the signal's premium and against the mid when the order was placed, from
  its ``exec`` trail (a mirror's is the fill it copied), and each slice sold against the mid when its order was placed.
* **market aligned**: the NIFTY 50 members past their day open the trade's way (``Engine.n50_alignment``, the card's
  strip), at the trigger bar's close and now. A larger share now is the market moving the trade's way, a smaller one
  against it, the same share unchanged. Unknown without both readings.
* **winning direction**: the stock now against the trigger close. Its reasons are measured readings that agree with it,
  each left out when the engine does not hold it: the stock, the front future against its close on the trigger bar, the
  NIFTY 50 members, the option's premium, and the future's OI risen since the previous close while the stock has gone the
  same way from that close (a build-up), on OI printed in today's session.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from ..config import Segment
from ..domain import Direction, InstrumentKind, OrderSide, PosSide
from ..exec.paper import walk_book
from ..market.session import ist_day, ist_hm, ist_today, session_open_ts
from ..strategy.keys import SHADOW_BOOKS, STOP_MIRRORS, StrategyKey, describe_book
from . import peer

if TYPE_CHECKING:
    from ..domain import Position
    from ..engine import Engine

log = structlog.get_logger(__name__)

#: an exit refused because the matcher would not price it on a book that old (exec/paper.py ``PaperMatcher.fill``), as the
#: order's trail keeps it: "cross failed: book for LTF is 33190 ms old (limit 6000)"
STALE_RE = re.compile(r"book for .+? is (\d+(?:\.\d+)?) ms old(?: \(limit (\d+(?:\.\d+)?)\))?")
#: the stock within this much of the trigger close (% of it) has moved neither way: the trade map's "entry · now"
FLAT_PCT = 0.05
#: a trigger whose route is not stored yet is looked for again after this long (it is written as the trigger is read)
STATIC_RETRY_S = 60.0
#: a trigger with no route this long after it fired will not get one
STATIC_GIVE_UP_S = 3_600.0
#: the signals kept worked out: a few dozen open at once, and a session's triggers are a few hundred
STATIC_CACHE = 2_000
#: rows built between two yields to the event loop the engine shares (60 rows took ~5 ms in one go)
ROWS_PER_YIELD = 10
SHADOW_VALUES = frozenset(k.value for k in SHADOW_BOOKS)
#: what "neutral" means, for the page's tooltip
NEUTRAL_RULE = ("neutral: the open lots' P&L (at the bid, as the engine marks them) is within the round trip's charges: "
                "the entry's plus those an exit now would pay. Profit above that band, loss below it. Slices already sold "
                "are shown beside it, not in it.")


def _num(v: Any) -> float | None:
    """A finite number, or None. Never raises."""
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v: Any) -> float | None:
    x = _num(v)
    return x if x is not None and x > 0 else None


def _r2(v: float | None) -> float | None:
    """Rounded to the paisa; never "-0.0" (a share sold wholly at its rung gives up nothing, not minus nothing)."""
    return None if v is None else round(v, 2) + 0.0


def _levels(raw: Any) -> list[tuple[float, int]]:
    """One side of a book as ``(price, quantity)``, best first as stored; a level without a positive price and size dropped."""
    out: list[tuple[float, int]] = []
    for lv in raw or ():
        if isinstance(lv, (list, tuple)) and len(lv) >= 2:
            p, q = _pos(lv[0]), _pos(lv[1])
            if p is not None and q is not None:
                out.append((p, int(q)))
    return out


def _moved(levels: Sequence[tuple[float, int]], to: float) -> list[tuple[float, int]]:
    """A ladder's shape with its best level moved to ``to``: every level shifted by the same distance. Not rounded, so a
    share that sits wholly at the new best gives up exactly nothing (an RT-N rung is 5.78333…)."""
    if not levels:
        return []
    shift = to - levels[0][0]
    return [(p + shift, q) for p, q in levels if p + shift > 0]


def _clean(v: Any) -> Any:
    """``v`` with every non-finite number made null: JSON has no NaN, and one would otherwise fail the whole answer."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, Mapping):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def encode(payload: Mapping[str, Any]) -> bytes:
    """The answer as JSON, as Starlette's ``JSONResponse`` writes it. A NaN or an infinity anywhere (a quote gone bad) is
    made null and named in the notes rather than failing every card (the encoder refuses them)."""
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except ValueError:
        fixed = _clean(payload)
        fixed["notes"] = [*(fixed.get("notes") or []), "a value that was not a number was left out (shown as DM)"]
        return json.dumps(fixed, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


# -- the pieces, pure ---------------------------------------------------------------------------------------------------


def refusal_age(text: str) -> tuple[float | None, float | None]:
    """``(age ms, limit ms)`` of a stale-book refusal in an order's outcome or note; ``(None, None)`` for any other."""
    m = STALE_RE.search(text or "")
    if not m:
        return None, None
    return float(m[1]), (float(m[2]) if m[2] else None)


def stale_refusals(orders: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Position id → the exits refused for a stale book: how many, the oldest book among them, the limit they were held to
    and when the last one was. Read from the stored REJECTED exit orders."""
    out: dict[str, dict[str, Any]] = {}
    for o in orders:
        if not isinstance(o, Mapping) or o.get("purpose") != "EXIT" or o.get("status") != "REJECTED":
            continue
        raw = o.get("exec")
        ex: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        age, limit = refusal_age(str(ex.get("outcome") or ""))
        if age is None:
            age, limit = refusal_age(str(o.get("note") or ""))
        pid = str(o.get("position_id") or "")
        if age is None or not pid:
            continue
        r = out.setdefault(pid, {"count": 0, "maxAgeMs": 0.0, "limitMs": None, "lastTs": None})
        r["count"] += 1
        r["maxAgeMs"] = max(r["maxAgeMs"], age)
        r["limitMs"] = limit if limit is not None else r["limitMs"]
        ts = _num(o.get("ts"))
        if ts is not None and (r["lastTs"] is None or ts > r["lastTs"]):
            r["lastTs"] = ts
    return out


def merge_refusals(into: dict[str, dict[str, Any]], more: Mapping[str, Mapping[str, Any]]) -> None:
    """``more`` (refusals stored since the last read) added to ``into``, position by position."""
    for pid, r in more.items():
        have = into.get(pid)
        if have is None:
            into[pid] = dict(r)
            continue
        have["count"] += r["count"]
        have["maxAgeMs"] = max(have["maxAgeMs"], r["maxAgeMs"])
        have["limitMs"] = r["limitMs"] if r["limitMs"] is not None else have["limitMs"]
        if r["lastTs"] is not None and (have["lastTs"] is None or r["lastTs"] > have["lastTs"]):
            have["lastTs"] = r["lastTs"]


def walk_all(levels: Sequence[tuple[float, int]], qty: int, *, sell: bool = True, ceiling_pct: float = 10.0) -> dict[str, Any] | None:
    """``qty`` units taken through one side of a book, best level first, for the whole quantity. What the levels cannot
    hold is priced at the last one shown (``beyondQty``). ``matcherQty`` is what the paper matcher would take in one cross:
    the levels within ``ceiling_pct`` of the touch (exec/paper.py ``walk_book``). None without a level or a quantity."""
    lv = [(p, q) for p, q in levels if p > 0 and q > 0]
    if not lv or qty <= 0:
        return None
    cost, rem, used = 0.0, qty, 0
    for p, q in lv:
        if rem <= 0:
            break
        take = min(q, rem)
        cost, rem, used = cost + take * p, rem - take, used + 1
    beyond = rem
    if rem > 0:
        cost += rem * lv[-1][0]
    capped = walk_book(lv, qty, touch=lv[0][0], ceiling_pct=ceiling_pct, buy=not sell)
    # ``value`` is kept to 6 places: the money is worked out from it, not from the 4-place average
    return {"avg": round(cost / qty, 4), "value": round(cost, 6), "qty": qty, "inBook": qty - beyond, "beyondQty": beyond,
            "levels": used, "touch": round(lv[0][0], 4), "touchQty": lv[0][1], "matcherQty": capped.filled,
            "matcherAvg": round(capped.avg_price, 4) if capped.filled else None}


def exit_now(levels: Sequence[tuple[float, int]], qty: int, *, sell: bool, ltp: float | None, mid: float | None,
             touch: float | None, mult: float = 1.0, ceiling_pct: float = 10.0, moved: bool = False) -> dict[str, Any] | None:
    """The open lots closed at market now, against the last trade, the mid and the touch in rupees (positive = what the
    exit gives up). ``basis``:

    * ``depth``: ``levels`` walked as they are (a fresh book; ``touch`` and ``mid`` must be that book's own);
    * ``shape`` (``moved``: a stale book): the levels' shape moved so the best sits at ``touch``, the quote's, and walked.
      The levels are not prices anyone is showing; their spacing and sizes are the best read held. An approximation;
    * ``touch``: no levels, the whole quantity at ``touch`` (the bid to sell, the ask to buy back);
    * ``none``: nothing to price on.
    """
    if qty <= 0:
        return None
    lv = [(p, q) for p, q in levels if p > 0 and q > 0]
    if moved:
        lv = _moved(lv, touch) if touch else []
    w = walk_all(lv, qty, sell=sell, ceiling_pct=ceiling_pct) if lv else None
    if w is not None:
        value, basis = w["value"], ("shape" if moved else "depth")
    elif touch:
        value, basis = touch * qty, "touch"
    else:
        return {"basis": "none", "avg": None, "value": None, "qty": qty, "vsLtpRs": None, "vsMidRs": None, "vsTouchRs": None,
                "mid": mid, "touch": None, "walk": None}
    sg = 1.0 if sell else -1.0

    def cost(ref: float | None) -> float | None:
        return _r2((ref * qty - value) * sg * mult) if ref else None

    return {"basis": basis, "avg": round(value / qty, 4), "value": round(value, 6), "qty": qty, "vsLtpRs": cost(ltp),
            "vsMidRs": cost(mid), "vsTouchRs": cost(touch), "mid": round(mid, 4) if mid else None, "touch": touch, "walk": w}


def at_rung(levels: Sequence[tuple[float, int]], rung: float, qty: int, *, sell: bool = True, mult: float = 1.0) -> dict[str, Any] | None:
    """``qty`` sold at a target rung on today's book shape: every level moved by the distance from the touch to the rung,
    then walked. The rupees are what the share would give up against the rung itself. An approximation: the book at the
    rung will not be today's book moved."""
    lv = [(p, q) for p, q in levels if p > 0 and q > 0]
    if not lv or qty <= 0 or rung <= 0:
        return None
    w = walk_all(_moved(lv, rung), qty, sell=sell)
    if w is None:
        return None
    sg = 1.0 if sell else -1.0
    return {"avg": w["avg"], "rs": _r2((rung * qty - w["value"]) * sg * mult), "touchQty": w["touchQty"],
            "beyondQty": w["beyondQty"], "levels": w["levels"]}


def entry_slippage(audit: Mapping[str, Any] | None, *, qty: int, mult: float = 1.0, buy: bool = True) -> dict[str, Any] | None:
    """The entry fill against the signal's premium (the ``ref`` the limit started from) and against the mid of the book
    when the order was placed, from its ``exec`` trail. Positive rupees = paid more than that price. None without a trail."""
    if not isinstance(audit, Mapping) or not audit:
        return None
    fill = _pos(audit.get("fillPrice"))
    ref = _pos(audit.get("ref"))
    bap = audit.get("bookAtPlace") if isinstance(audit.get("bookAtPlace"), Mapping) else {}
    bid, ask = _pos(bap.get("bid")), _pos(bap.get("ask"))
    mid = (bid + ask) / 2 if bid and ask else None
    sg = 1.0 if buy else -1.0

    def rs(at: float | None) -> float | None:
        return _r2((fill - at) * sg * qty * mult) if fill and at else None

    return {"fill": fill, "ref": ref, "midAtPlace": _r2(mid), "bidAtPlace": bid, "askAtPlace": ask, "limit": _num(audit.get("limit")),
            "vsRefRs": rs(ref), "vsMidRs": rs(mid), "vsRefPct": round((fill / ref - 1) * 100 * sg, 2) if fill and ref else None,
            "waitS": _num(audit.get("waitS")), "why": audit.get("why"), "outcome": audit.get("outcome")}


def exit_slices_slippage(exits: Iterable[Any], *, mult: float = 1.0, sell: bool = True) -> list[dict[str, Any]]:
    """Each slice already sold against the mid of the book when its order was placed (positive = sold under it)."""
    out: list[dict[str, Any]] = []
    sg = 1.0 if sell else -1.0
    for x in exits or ():
        if not isinstance(x, Mapping):
            continue
        fill, qty = _pos(x.get("fillPrice")), int(_pos(x.get("qty")) or 0)
        bap = x.get("bookAtPlace") if isinstance(x.get("bookAtPlace"), Mapping) else {}
        bid, ask = _pos(bap.get("bid")), _pos(bap.get("ask"))
        mid = (bid + ask) / 2 if bid and ask else None
        out.append({"reason": x.get("reason"), "qty": qty, "fill": fill, "midAtPlace": _r2(mid),
                    "vsMidRs": _r2((mid - fill) * sg * qty * mult) if (mid and fill and qty) else None})
    return out


def book_state(age_ms: float | None, limit_ms: float, has_book: bool) -> str:
    """``fresh`` while the paper matcher would trade on the book, ``stale`` past its limit (it refuses ``age > limit``, so a
    book exactly at the limit still trades), ``none`` without a level."""
    if not has_book:
        return "none"
    return "stale" if (age_ms or 0.0) > limit_ms else "fresh"


def pnl_state(open_pnl: float | None, band: float | None) -> str | None:
    """profit / loss / neutral: neutral while the open lots' P&L is within the round trip's charges (``NEUTRAL_RULE``); the
    band's edge itself is neutral."""
    if open_pnl is None:
        return None
    b = abs(band or 0.0)
    return "neutral" if abs(open_pnl) <= b else "profit" if open_pnl > 0 else "loss"


def share_with(a: Mapping[str, Any] | None) -> float | None:
    """The NIFTY 50 members past their open the reading's way, as a share of those read."""
    if not a or not a.get("names"):
        return None
    return float(a.get("with") or 0) / float(a["names"])


def flip(a: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The same reading for the other direction: those with become those against."""
    if a is None:
        return None
    return {"with": a.get("against", 0), "against": a.get("with", 0), "flat": a.get("flat", 0), "names": a.get("names", 0)}


def market_aligned(at_trigger: Mapping[str, Any] | None, now: Mapping[str, Any] | None) -> dict[str, Any]:
    """Has the market moved the trade's way since the trigger: a larger share of the NIFTY 50 members past their open the
    trade's way now than at the trigger bar's close (``yes``), a smaller one (``no``), the same (``unchanged``). ``unknown``
    without both readings, never a guess."""
    s0, s1 = share_with(at_trigger), share_with(now)
    if at_trigger is None or now is None or s0 is None or s1 is None:
        why = "the NIFTY 50 reading " + ("at the trigger" if s0 is None else "now") + " is not held"
        return {"aligned": "unknown", "atTrigger": at_trigger, "now": now, "why": why}
    s0r, s1r = round(s0, 3), round(s1, 3)
    aligned, moved = ("yes", "a larger share than") if s1r > s0r else ("no", "a smaller share than") if s1r < s0r else ("unchanged", "the same share as")
    why = (f"{now.get('with')} of {now.get('names')} NIFTY 50 names past their open the trade's way now, {moved} at the "
           f"trigger ({at_trigger.get('with')} of {at_trigger.get('names')})")
    return {"aligned": aligned, "atTrigger": at_trigger, "now": now, "shareAtTrigger": s0r, "shareNow": s1r, "why": why}


def fill_book_age(ex: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """How old the book was that priced an exit's fill, and how the fill was priced: ``cross`` (the matcher walked the depth,
    ``depthAtCross``), ``market`` (a market order, on the book kept as it was placed) or ``resting`` (a resting limit filled
    on a touch, priced on a fresh book or quote, or a fresh print: ``Engine._touch``). A resting fill's ``depthAtFill`` is
    only the depth snapshot of that moment, never what priced it, so its age is not judged against the matcher's limit
    (review, 9 Oct 2026: 45 trades on A, 39 on B read "filled on a 9.4 s old book" that way). A book stamped a hair after
    our clock reads 0, not -0.1 s. ``(None, None)`` without one."""
    for k, how in (("depthAtCross", "cross"), ("depthAtPlace", "market"), ("depthAtFill", "resting")):
        d = ex.get(k)
        if k == "depthAtPlace" and ex.get("limit") is not None:
            continue  # a limit's book at placement priced nothing
        age = _num(d.get("ageMs")) if isinstance(d, Mapping) else None
        if age is not None:
            return max(0.0, age), how
    return None, None


def stale_book_for_trades(trades: Iterable[Mapping[str, Any]], orders: Iterable[Mapping[str, Any]], *,
                          limit_ms_at: Callable[[float], float]) -> dict[str, dict[str, Any]]:
    """Trade id → "was the order book stale on the way out": the exits refused for a stale book (count, oldest) and, when
    the closing exit was priced on the depth (a cross or a market order), that book's age against the paper matcher's limit
    at that instant (``limit_ms_at``). Read from the stored orders at request time; nothing the engine wrote is changed."""
    orders = [o for o in orders if isinstance(o, Mapping) and o.get("purpose") == "EXIT"]
    refused = stale_refusals(orders)
    filled: dict[str, list[Mapping[str, Any]]] = {}
    for o in orders:
        if o.get("status") == "FILLED" and o.get("position_id"):
            filled.setdefault(str(o["position_id"]), []).append(o)
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        pid = str(t.get("position_id") or "")
        r = refused.get(pid) or {"count": 0, "maxAgeMs": None, "limitMs": None, "lastTs": None}
        fills = sorted(filled.get(pid, []), key=lambda o: _num((o.get("exec") or {}).get("filledTs")) or _num(o.get("ts")) or 0.0)
        age = limit = snap = None
        how: str | None = None
        stale_fill = False
        if fills:
            last = fills[-1]
            ex = last.get("exec") if isinstance(last.get("exec"), Mapping) else {}
            seen, how = fill_book_age(ex)
            if how == "resting":
                snap = seen
            elif seen is not None:
                age = seen
                at = _num(ex.get("crossedTs")) or _num(ex.get("filledTs")) or _num(last.get("ts")) or _num(t.get("closed_ts")) or 0.0
                limit = limit_ms_at(at)
                stale_fill = age > limit
        parts = []
        if r["count"]:
            parts.append(f"{r['count']} exit{'s' if r['count'] > 1 else ''} refused, {r['maxAgeMs'] / 1000:.1f} s")
        if stale_fill:
            parts.append(f"{'crossed' if how == 'cross' else 'sold at market'} on a {age / 1000:.1f} s old book, limit {limit / 1000:g} s")  # type: ignore[operator]
        out[str(t.get("id"))] = {
            "stale": bool(r["count"] or stale_fill), "refused": r["count"], "maxAgeMs": r["maxAgeMs"], "limitMs": r["limitMs"],
            "lastRefusedTs": r["lastTs"], "fillAgeMs": age, "fillLimitMs": limit, "fillStale": stale_fill, "fillHow": how,
            "depthAgeAtFillMs": snap,
            "words": f"order book stale ({'; '.join(parts)})" if parts else "",
        }
    return out


def _parse(rows: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            d = json.loads(r)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def front_future_peek(e: Engine, symbol: str) -> str | None:
    """The stock's front-month future, as ``Engine._front_future_code`` has it for today, or read from the contract list
    without being saved. The engine's own method saves what it finds, a None too, and its OI handler keeps to that for the
    day: a page polled while the list was still loading after a restart cut the stock's OI off (review, 9 Oct 2026)."""
    key = (symbol, ist_today())
    if key in e._front_code:
        return e._front_code[key]
    f = e.catalogue_loader.catalogue.front_future(symbol, on=key[1])
    return f.scrip_code if f is not None else None


def oi_in_session(e: Engine, contracts: Iterable[str]) -> bool:
    """Every contract behind an OI reading printed in today's session. 5paisa's OI frames carry no time, and at a subscribe
    it re-sends each contract's last print: before the open, or on a closed day, the level is an old one. Phase 35's
    reading refuses those itself; phase 34's does not, so the page checks on both and the two engines agree."""
    today = ist_today()
    codes = list(contracts)
    if not codes or not e.calendar.is_trading_day(today):
        return False
    since = session_open_ts(Segment.NSE_FO, today)
    return all((e._fut_oi.get(c) or (0.0, 0.0))[1] >= since for c in codes)


def prev_close(e: Engine, symbol: str) -> float | None:
    """The stock's last daily close before today (``Engine._prev_close``, read on the last three daily bars only)."""
    today = ist_today()
    prior = [b for b in e.store.bars(symbol, "1d", 3) if ist_day(b.ts) < today]
    return prior[-1].close if prior else None


# -- the closed trades' side (/api/trades) ------------------------------------------------------------------------------


#: closed trades whose stale-book reading is kept: a closed trade's orders are all stored before its row, so it never changes
STALE_CACHE_TRADES = 5_000


@dataclass(slots=True)
class StaleBookReads:
    """``staleBook`` on /api/trades, worked out once per closed trade from its stored exit orders."""

    ledger: Any
    limit_ms_at: Callable[[float], float]
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def for_trades(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        todo = [t for t in rows if str(t.get("id")) not in self.checks]
        if todo:
            opened = [float(t.get("opened_ts") or t.get("closed_ts") or 0.0) for t in todo]
            closed = [float(t.get("closed_ts") or 0.0) for t in todo]
            raw = await self.ledger.exit_order_json(min(opened) - 60, max(*closed, *opened) + 120, ("FILLED", "REJECTED"))
            made = await asyncio.to_thread(lambda: stale_book_for_trades(todo, _parse(raw), limit_ms_at=self.limit_ms_at))
            self.checks.update(made)
            while len(self.checks) > STALE_CACHE_TRADES:
                self.checks.pop(next(iter(self.checks)))
        return {str(t.get("id")): self.checks[str(t.get("id"))] for t in rows if str(t.get("id")) in self.checks}


# -- the open trades (/api/positions/live) ------------------------------------------------------------------------------


@dataclass(slots=True)
class LiveTrades:
    """The active-trades page's reads. ``position_view`` and ``rule_fields`` are the API's own helpers (api/routes.py), so a
    card names a book and marks a position exactly as the Overview and the Trades page do."""

    engine: Engine
    position_view: Callable[[Any, Any], dict[str, Any]]
    rule_fields: Callable[[str], dict[str, str]]
    entry_sources: Mapping[str, str]
    #: signal id → what does not change once it fired: the signal, its trigger, the future's levels from its route
    statics: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: positions whose row failed and was logged (once each)
    logged: set[str] = field(default_factory=set)
    #: open position id → its exits refused for a stale book, and the last orders row read for them. The orders table is
    #: only ever inserted into, a row final as it is stored, so each poll reads the rows stored since the last one, not
    #: the whole table again (it has no index on time: a read by time scanned all of it on every poll)
    refused: dict[str, dict[str, Any]] = field(default_factory=dict)
    refused_upto: int = 0
    refused_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def view(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        e = self.engine
        now = time.time()
        open_ = [p for p in e.positions.values() if p.status == "OPEN" and p.qty_remaining > 0]
        notes: list[str] = []
        try:
            await self._load_statics(open_, now)
        except Exception as exc:  # noqa: BLE001 - the cards stand without the trigger's levels
            notes.append(f"the triggers could not be read: {str(exc)[:120]}")
        refusals: Mapping[str, dict[str, Any]] = {}
        try:
            refusals = await self._refusals(open_)
        except Exception as exc:  # noqa: BLE001 - the refusals are a column, never the page
            notes.append(f"the refused exits could not be read: {str(exc)[:120]}")
        name = peer.read_identity(e.s.data_dir).name
        memo: dict[Any, Any] = {}
        rows: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for i, p in enumerate(sorted(open_, key=lambda x: -x.opened_ts)):
            if i and not i % ROWS_PER_YIELD:
                await asyncio.sleep(0)  # the engine's tick may run between rows: a single pause stays ~1 ms
                now = time.time()
            if p.status != "OPEN" or p.qty_remaining <= 0:
                continue  # closed while the page was being read
            try:
                rows.append(self._row(p, now, name, refusals, memo))
            except Exception as exc:  # noqa: BLE001 - one bad row never costs the page (it is named instead)
                skipped.append({"positionId": p.id, "book": p.strategy, "error": f"{type(exc).__name__}: {exc}"[:200]})
                if p.id not in self.logged:
                    self.logged.add(p.id)
                    log.warning("positions_live.row_failed", position=p.id, book=p.strategy, error=str(exc)[:160])
        m = e.matcher
        return {
            "engine": name, "nowTs": now, "count": len(rows), "rows": rows, "skipped": skipped, "notes": notes,
            "bookLimit": {"nowMs": m.age_limit_ms(now), "maxMs": m.max_book_age_ms, "openMaxMs": m.open_max_book_age_ms,
                          "openWindow": f"{m.open_window_ist[0]}-{m.open_window_ist[1]}", "ceilingPct": m.ceiling_pct},
            "quoteLimitS": e.s.position_quote_max_age_s, "neutralRule": NEUTRAL_RULE, "recordOnly": True,
            "tookMs": round((time.perf_counter() - t0) * 1000, 2),
        }

    async def _refusals(self, open_: list[Position]) -> Mapping[str, dict[str, Any]]:
        """The open positions' exits refused for a stale book: the orders rows stored since the last read, added in. One
        read at a time (both pages poll), so no row is counted twice."""
        if not open_:
            return {}
        async with self.refused_lock:
            # the first read goes back to the oldest open position; after it, only the rows stored since
            since = 0.0 if self.refused_upto else min(p.opened_ts for p in open_) - 60
            upto, raw = await self.engine.ledger.exit_orders_after(self.refused_upto, since, ("REJECTED",))
            if raw:
                merge_refusals(self.refused, await asyncio.to_thread(lambda: stale_refusals(_parse(raw))))
            self.refused_upto = upto
            keep = {p.id for p in open_}
            for pid in [k for k in self.refused if k not in keep]:
                del self.refused[pid]  # closed: a position is never opened again
            return dict(self.refused)

    async def _load_statics(self, open_: list[Position], now: float) -> None:
        """Each open position's signal, its trigger and the future's levels on its route, read once; the stored JSON is
        parsed off the event loop."""
        want = {p.signal_id for p in open_ if p.signal_id}
        due = {s for s in want if s not in self.statics or (not self.statics[s]["complete"] and now - self.statics[s]["at"] >= STATIC_RETRY_S)}
        if not due:
            return
        led = self.engine.ledger
        sigs = {d.get("signal_id"): d for d in await asyncio.to_thread(_parse, await led.signal_json(due))}
        parents = {str(d.get("source_signal_id")) for d in sigs.values() if d.get("source_signal_id")} - set(sigs)
        if parents:
            sigs.update({d.get("signal_id"): d for d in await asyncio.to_thread(_parse, await led.signal_json(parents))})
        trig_of = {s: (str((sigs.get(s) or {}).get("source_signal_id") or "") or s) for s in due}
        trig_ids = {t for t in trig_of.values() if t in sigs}
        routes: dict[str, dict[str, Any]] = {}
        if trig_ids:
            since = min(float(sigs[t].get("ts") or now) for t in trig_ids) - 3_600
            for ev in await asyncio.to_thread(_parse, await led.event_json("counter.route", trig_ids, since)):
                routes[str(ev.get("signal_id"))] = ev  # the last route read for a trigger stands
        for s in due:
            sig, trig = sigs.get(s), sigs.get(trig_of[s])
            route = routes.get(trig_of[s])
            if sig is None:
                self.statics[s] = {"sig": None, "trigger": None, "futLevels": None, "futClose": None, "complete": False, "at": now}
                continue
            bull = sig.get("direction") == Direction.BULLISH.value
            fut_leg = next((lg for lg in (route or {}).get("legs", []) if isinstance(lg, Mapping) and lg.get("name") == "future"), None)
            ctx = sig.get("context") if isinstance(sig.get("context"), Mapping) else {}
            conf = ctx.get("confluence") if isinstance(ctx.get("confluence"), Mapping) else {}
            t = trig or sig
            self.statics[s] = {
                "sig": {"id": s, "ts": _num(sig.get("ts")), "direction": sig.get("direction"), "entry": _num(sig.get("entry")),
                        "stop": _num(sig.get("stop")), "targets": [x for x in (_num(v) for v in sig.get("targets") or ()) if x is not None],
                        "rr": _num(sig.get("rr")), "grade": sig.get("grade"), "reason": sig.get("reason"),
                        "confluence": {"stop_zone": conf.get("stop_zone"), "target_zones": list(conf.get("target_zones") or [])}},
                "trigger": {"id": t.get("signal_id"), "direction": t.get("direction"), "ts": _num(t.get("ts")), "entry": _num(t.get("entry"))},
                "futLevels": self.engine._fut_levels(route, bull) if route else None,
                "futClose": _num(fut_leg.get("close")) if fut_leg else None,
                # a published trigger is routed as it is read; a trigger with no route an hour on will not get one
                "complete": route is not None or now - float(t.get("ts") or now) > STATIC_GIVE_UP_S, "at": now,
            }
        while len(self.statics) > STATIC_CACHE:
            self.statics.pop(next(iter(self.statics)))

    def _n50(self, direction: Direction, at_ts: int | None, memo: dict[Any, Any]) -> dict[str, Any] | None:
        key = ("n50", direction, at_ts)
        if key not in memo:
            memo[key] = self.engine.n50_alignment(direction, at_ts=at_ts)
        return memo[key]

    def _row(self, p: Position, now: float, engine_name: str, refusals: Mapping[str, dict[str, Any]], memo: dict[Any, Any]) -> dict[str, Any]:
        e = self.engine
        inst, und = p.instrument, p.underlying
        code = inst.scrip_code
        mult = float(inst.multiplier or 1)
        lot = max(1, int(inst.lot_size or 1))
        long = p.side is PosSide.LONG
        view = self.position_view(e, p)
        rules = self.rule_fields(p.strategy)
        try:
            key: StrategyKey | None = StrategyKey(p.strategy)
        except ValueError:
            key = None
        static = self.statics.get(p.signal_id) or {}
        sig = static.get("sig") or {}
        trig = static.get("trigger") or {}

        # -- the quote: the last trade, bid, ask and mid from one feed, the one the exit loop marks the position on
        q = e.quotes.get(code)
        q_fresh = q is not None and now - q.ts <= e.s.position_quote_max_age_s
        ltp = e.ltps.get(code) or (q.ltp if q else None) or None
        qb, qa = (_pos(q.bid), _pos(q.ask)) if q is not None else (None, None)
        qmid = (qb + qa) / 2 if qb and qa else None
        q_touch = (qb if long else qa) if q_fresh else None

        # -- the book, as the matcher reads it
        book = e.books.get(code)
        limit_ms = e.matcher.age_limit_ms(now)
        has_book = book is not None and bool(book.bids or book.asks)
        # a book stamped a hair ahead of our clock (seen in the exec trails) is 0 s old, not "-0 s"
        age_ms = max(0.0, book.age_ms(now)) if book is not None else None
        state = book_state(age_ms, limit_ms, has_book)
        # the depth on the side an exit trades
        side_levels = _levels(book.bids if long else book.asks) if has_book and book is not None else []
        bb = _pos(book.best_bid) if has_book and book is not None else None
        ba = _pos(book.best_ask) if has_book and book is not None else None

        # -- money: the open lots at the mark, as the engine marks its excursions (the quote's bid while it is fresh)
        qty = int(p.qty_remaining)
        mark_px = q_touch or ltp
        basis = ("bid" if long else "ask") if q_touch else ("last" if ltp else None)
        unreal = _r2((mark_px - p.entry) * p.dir_sign * qty * mult) if mark_px else None
        exit_side = OrderSide.SELL if long else OrderSide.BUY
        exit_ch = e.costs.leg(inst, exit_side, mark_px, qty) if mark_px and qty > 0 else None
        paid = round(float(p.entry_charges) + float(p.charges), 2)
        band = float(p.entry_charges) + (exit_ch.total if exit_ch else 0.0)
        # an exit now: a fresh book walked as it stands, against its own touch and mid (one feed); otherwise the quote's
        # touch, with a stale book's shape under it (its levels are not prices anyone is showing: LTF 270 CE, 9 Oct
        # 12:37:52, its 45 s old book bid 4.65 while the quote bid 4.50)
        if state == "fresh" and side_levels:
            walk = exit_now(side_levels, qty, sell=long, ltp=ltp, mid=(bb + ba) / 2 if bb and ba else None, touch=side_levels[0][0],
                            mult=mult, ceiling_pct=e.matcher.ceiling_pct)
            feed = "book"
        else:
            walk = exit_now(side_levels, qty, sell=long, ltp=ltp, mid=qmid if q_fresh else None, touch=q_touch, mult=mult,
                            ceiling_pct=e.matcher.ceiling_pct, moved=True)
            feed = "quote"
        if walk is not None:
            walk.update(bookState=state, feed=feed, quoteTouch=q_touch)
        net_now = None
        if walk and walk.get("value") is not None and qty > 0:
            sold = (walk["value"] - p.entry * qty) * p.dir_sign * mult
            ch = e.costs.leg(inst, exit_side, walk["value"] / qty, qty).total
            net_now = _r2(p.realised_gross + sold - paid - ch)
        entry_audit = (p.exec_log or {}).get("entry")
        entry_from = self.entry_sources.get(p.strategy)
        from_label = describe_book(entry_from)["strategy_label"] if entry_from else None
        slip_entry = entry_slippage(entry_audit if isinstance(entry_audit, Mapping) else None, qty=int(p.qty), mult=mult, buy=long)
        if slip_entry is not None:
            slip_entry["copiedFrom"] = entry_from
            slip_entry["copiedFromLabel"] = from_label
        slip_exits = exit_slices_slippage((p.exec_log or {}).get("exits") or (), mult=mult, sell=long)
        slip_total = sum(x for x in ([(slip_entry or {}).get("vsMidRs")] + [s["vsMidRs"] for s in slip_exits]) if x is not None)

        # -- targets and stops
        bull = p.direction is Direction.BULLISH
        sg = 1.0 if bull else -1.0
        und_ltp = e.ltps.get(und.scrip_code)
        eq_entry = float(p.equity_entry or 0.0)

        def fav(px: float, ref: float) -> float:
            return sg * (px - ref) / ref * 100 if ref else 0.0

        stop_now = max(p.option_sl, p.ratchet_sl)
        plan = None
        if key is not None:
            plan = e._exit_plan(key, inst, qty, tuple(p.option_targets), stop_now, float(p.equity_sl or 0), und.segment,
                                hit=p.targets_hit, rising=p.ratchet_sl > p.option_sl, entry=p.entry, initial_sl=p.initial_option_sl)
        shares = [r for r in (plan or {}).get("rows", []) if r.get("kind") == "target"]
        opt_targets = []
        for i, rung in enumerate(list(p.option_targets)[:4]):
            row = shares[i] if i < len(shares) else {}
            hit = i < p.targets_hit
            share_qty = int(row.get("qty") or 0) if not hit else 0
            opt_targets.append({
                "rung": i + 1, "price": rung, "hit": hit, "share": row.get("action"), "qty": share_qty or None,
                "lots": round(share_qty / lot, 2) if share_qty else None,
                "slippage": None if hit or not share_qty else at_rung(side_levels, rung, share_qty, sell=long, mult=mult),
            })
        eq_targets = [{"price": t, "passed": und_ltp is not None and eq_entry > 0 and fav(und_ltp, eq_entry) >= fav(t, eq_entry) > 0}
                      for t in p.equity_targets]
        fut_code = front_future_peek(e, und.symbol) if und.segment is Segment.NSE_EQ else None
        fut_ltp = e.ltps.get(fut_code) if fut_code else None
        fut_levels = static.get("futLevels")
        fut_close = static.get("futClose")
        fut_targets = [{"label": a.get("label"), "price": a.get("price"),
                        "passed": bool(fut_ltp and fut_close and fav(fut_ltp, fut_close) >= fav(float(a.get("price") or 0), fut_close) > 0)}
                       for a in (fut_levels or {}).get("ahead", [])]
        mirror_state = e._mirror_stop_state(p) if rules["stop_rule"] != "current" else None

        # -- the market and the winner
        trig_dir = trig.get("direction") or p.direction.value
        trig_ts = int(trig["ts"]) if trig.get("ts") else None
        market: dict[str, Any] = {"aligned": "unknown", "why": "NSE only", "atTrigger": None, "now": None}
        if und.segment is not Segment.MCX_FO:
            n50_now = self._n50(p.direction, None, memo)
            n50_trig = self._n50(p.direction, trig_ts, memo) if trig_ts else None
            market = market_aligned(n50_trig, n50_now)
            if trig_ts is None:
                market["why"] = "the trigger is not in the ledger"
        winning = self._winning(p, und_ltp, eq_entry, fut_ltp, fut_close, (qmid if q_fresh else None) or ltp, market, memo)

        refused = refusals.get(p.id)
        q_age = round(max(0.0, now - q.ts), 1) if q else None
        named = describe_book(p.strategy)
        row = {
            "id": p.id, "engine": engine_name, "book": p.strategy, "bookLabel": named["strategy_label"], "trend": named["trend"],
            **rules, "variant": rules["stop_rule"], "mirror": key in STOP_MIRRORS if key else False,
            "shadow": key in SHADOW_BOOKS if key else False, "sourceShadow": rules["source_book"] in SHADOW_VALUES,
            "entryFrom": entry_from, "entryFromLabel": from_label,
            "underlying": und.symbol, "segment": und.segment.value, "contract": inst.name or code, "scripCode": code,
            "kind": inst.kind.value, "optionType": inst.option_type.value or None, "side": p.side.value,
            "tradeDirection": p.direction.value, "triggerDirection": trig_dir,
            "qty": int(p.qty), "qtyRemaining": qty, "lotSize": lot, "lots": round(qty / lot, 2), "lotsAtEntry": round(p.qty / lot, 2),
            "multiplier": mult, "entry": p.entry, "openedTs": p.opened_ts, "openedHm": ist_hm(p.opened_ts),
            "openedDay": ist_day(p.opened_ts).isoformat(), "signalId": p.signal_id,
            "triggerId": trig.get("id"), "triggerTs": trig.get("ts"), "triggerClose": eq_entry or trig.get("entry"), "grade": p.grade or sig.get("grade"),
            "quote": {"ltp": ltp, "bid": qb, "ask": qa, "mid": round(qmid, 4) if qmid else None, "ageS": q_age, "fresh": q_fresh,
                      "limitS": e.s.position_quote_max_age_s, "spreadPct": round(q.spread_pct, 2) if q and q.spread_pct is not None else None},
            "orderBook": {"state": state, "ageS": round(age_ms / 1000, 1) if age_ms is not None else None, "limitS": round(limit_ms / 1000, 1),
                          "stale": state == "stale", "ts": book.ts if book is not None else None, "bid": bb, "ask": ba,
                          "depth": e._depth(code, now)},
            "pnl": {"unrealised": unreal, "basis": basis, "unrealisedLtp": view.get("unrealized"), "realisedGross": _r2(p.realised_gross),
                    "tradeGross": _r2(p.realised_gross + unreal) if unreal is not None else None, "band": _r2(band),
                    "state": pnl_state(unreal, band), "netIfExitNow": net_now, "rNow": view.get("r_now"), "maxDdInr": view.get("max_dd_inr")},
            "money": {
                "chargesSoFar": {"entry": round(float(p.entry_charges), 2), "exits": round(float(p.charges), 2), "total": paid},
                "exitCharges": {"price": mark_px, "qty": qty, **(exit_ch.to_json() if exit_ch else {"total": None})},
                "slippage": {"entry": slip_entry, "exits": slip_exits, "totalVsMidRs": _r2(slip_total)},
                "exitNow": walk,
            },
            "staleExits": refused,
            "targets": {"option": opt_targets, "equity": eq_targets, "future": fut_targets, "futureClose": fut_close, "futureLtp": fut_ltp,
                        "optionStop": p.option_sl, "initialOptionStop": p.initial_option_sl, "stopNow": stop_now, "line": p.ratchet_sl or None,
                        "equityStop": p.equity_sl, "peakMid": p.peak_mid or None, "armedBy": p.armed_by or None, "mirror": mirror_state,
                        "exitPlan": plan},
            "market": market, "winning": winning,
            # the trade map (frontend components/TradeMap.tsx), in the alert card's own field names
            "map": {"direction": p.direction.value, "entry": eq_entry or sig.get("entry"), "stop": p.equity_sl or sig.get("stop"),
                    "targets": list(p.equity_targets) or sig.get("targets") or [], "rr": sig.get("rr"),
                    "confluence": sig.get("confluence") or {}, "futLevels": fut_levels, "plan": None, "rtCard": None, "spark": [],
                    "position": {"entry": p.entry, "option_sl": p.option_sl, "initial_option_sl": p.initial_option_sl,
                                 "option_targets": list(p.option_targets), "targets_hit": p.targets_hit, "note": p.note},
                    "live": {"line": p.ratchet_sl, "optionSl": p.option_sl, "ladder": list(p.option_targets),
                             "targetsHit": p.targets_hit, "underlying": und_ltp}},
        }
        return row

    def _winning(self, p: Position, und_ltp: float | None, eq_entry: float, fut_ltp: float | None, fut_close: float | None,
                 opt_now: float | None, market: Mapping[str, Any], memo: dict[Any, Any]) -> dict[str, Any]:
        """Which way the stock has gone since the trigger close, and the measured readings that agree with it."""
        if not und_ltp or not eq_entry:
            return {"direction": None, "vsTrade": None, "stockMovePct": None, "reasons": [], "why": "no price for the stock"}
        move = (und_ltp - eq_entry) / eq_entry * 100
        if abs(move) < FLAT_PCT:
            return {"direction": "FLAT", "vsTrade": "flat", "stockMovePct": round(move, 3), "reasons": [],
                    "why": f"the stock is within {FLAT_PCT:g}% of the trigger close"}
        win = Direction.BULLISH if move > 0 else Direction.BEARISH
        up = move > 0
        word = "up" if up else "down"
        reasons = [f"stock {move:+.2f}% from the trigger close {eq_entry:,.2f} (now {und_ltp:,.2f})"]
        if fut_ltp and fut_close:
            fm = (fut_ltp - fut_close) / fut_close * 100
            if (fm > 0) == up and abs(fm) >= FLAT_PCT:
                reasons.append(f"future {fm:+.2f}% from its close on the trigger bar {fut_close:,.2f} (now {fut_ltp:,.2f})")
        if market.get("atTrigger") and market.get("now"):
            a0, a1 = market["atTrigger"], market["now"]
            if win is not p.direction:
                a0, a1 = flip(a0), flip(a1)
            s0, s1 = share_with(a0), share_with(a1)
            if s0 is not None and s1 is not None and s1 > s0:
                reasons.append(f"NIFTY 50: {a1['with']} of {a1['names']} names past their open {word}, from {a0['with']} at the trigger")  # type: ignore[index]
        if opt_now and p.entry:
            pm = (opt_now - p.entry) / p.entry * 100
            # the premium rises when the trade is winning: it agrees with the stock's way when that is the trade's
            agrees = (pm > 0) == (win is p.direction)
            if agrees and abs(pm) >= 0.5:
                what = "premium" if p.instrument.kind is InstrumentKind.OPTION else "price"
                reasons.append(f"the {p.instrument.option_type.value or 'contract'} {what} {pm:+.1f}% since the entry {p.entry:g} (now {opt_now:g})")
        sym = p.underlying.symbol
        if p.underlying.segment is not Segment.MCX_FO:
            if ("oi", sym) not in memo:
                memo[("oi", sym)] = self.engine.oi_reading(sym)
            oi = memo[("oi", sym)]
            # OI added says nothing of a direction alone: it backs the way the price went over the SAME window (since the
            # previous close), long build-up when up, short build-up when down (review, 9 Oct 2026)
            if oi.ok and (oi.change_pct or 0) > 0 and oi_in_session(self.engine, oi.contracts):
                if ("prev", sym) not in memo:
                    memo[("prev", sym)] = prev_close(self.engine, sym)
                prev = memo[("prev", sym)]
                day = (und_ltp - prev) / prev * 100 if prev else None
                if day is not None and (day > 0) == up and abs(day) >= FLAT_PCT:
                    reasons.append(f"future OI {oi.change_pct:+.1f}% since the previous close and the stock {day:+.2f}% from that close "
                                   f"{prev:,.2f}: {'long' if up else 'short'} build-up, positions added {word}, not unwound")
        return {"direction": win.value, "vsTrade": "with" if win is p.direction else "against", "stockMovePct": round(move, 3),
                "reasons": reasons, "why": ""}


__all__ = [
    "FLAT_PCT", "NEUTRAL_RULE", "STALE_RE", "LiveTrades", "StaleBookReads", "at_rung", "book_state", "encode", "entry_slippage", "exit_now",
    "exit_slices_slippage", "fill_book_age", "front_future_peek", "market_aligned", "merge_refusals", "oi_in_session",
    "pnl_state", "prev_close", "refusal_age", "stale_book_for_trades", "stale_refusals", "walk_all",
]
