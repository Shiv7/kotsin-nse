"""Depth check: a paper fill against the book recorded beside it. RECORD-ONLY.

Operator, 2026-10-09: "go ahead build it n tell me where to view it". The paper matcher fills a resting
order WHOLE at its limit — no queue, no sharing of the depth (exec/resting.py, "Fill rule (paper)") — so a
fill is optimistic whenever fewer lots were on offer at the price than we traded: 108 of the first 206
depth-logged fills. Every order since 5 Oct carries the five levels a side it met (``exec.depthAtFill``,
``exec.depthAtCross`` for a deadline cross, ``exec.depthAtPlace``); this module reads them back and says
what the same fill would have cost against the book as it stood.

* **The book** — the one at the fill (``depthAtFill``, or ``depthAtCross``: the book a cross walked, read
  just before it walked it); failing that, the one at placement (``depthAtPlace``). ``source`` says which
  (``"fill"`` | ``"place"`` | None) and ``ageMs`` how old it was when it was read.
* **At one price** (a resting limit, a touch taken whole): the quantity on the trading side at or better
  than the fill price — a BUY the asks at or under it, a SELL the bids at or over it — trades AT the fill
  price; the rest walks the next levels the same side (a BUY up the asks, a SELL down the bids). A resting
  BUY that filled because the ask came DOWN to its limit is judged on the book at the fill, as recorded.
* **Walked** (``limit`` None: a market order or a deadline cross) — the paper matcher already priced it
  level by level, so the realistic price walks the recorded book from its best level, each level at its
  own price, and is rounded to the paisa as the matcher rounds a walk.
* **Beyond the book** — what the five levels cannot hold is priced at the last visible level (never kinder
  than the fill price) and flagged ``beyondBook``.
* ``optimismRs`` — what the paper fill gained over that realistic price on the whole quantity, in rupees:
  positive = paper was kinder than the book. ``verdict``: ``full`` (the book held every lot at the price),
  ``partial`` (at least one lot, not all), ``thin`` (not one whole lot), ``no book`` (nothing recorded on
  the side that trades).

Pure and read-only: stored rows in, a verdict out. Nothing in the order path imports this module — it can
never move an order, a fill, a stop, a wallet or a decision (tests/test_depth_check.py holds that).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from typing import Any

#: the verdicts, best first — a summary carries the worst of its fills
VERDICTS: tuple[str, ...] = ("full", "partial", "thin", "no book")
#: the stored books in the order they are trusted: the book at the fill, the book a cross walked, the book at placement
BOOK_FIELDS: tuple[tuple[str, str], ...] = (("depthAtFill", "fill"), ("depthAtCross", "fill"), ("depthAtPlace", "place"))
#: a price within this of the fill counts as AT it (float noise on a 0.05 tick)
_EPS = 1e-6
#: ``HINDUNILVR-1960CE-L4``: the position's lots at the end of every readable order id (engine ``_contract_tag``)
_LOTS_TAG = re.compile(r"-L(\d+)$")


def _num(v: Any) -> float | None:
    """A finite number, or None — a stored row is never trusted to be well formed."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _levels(raw: Any) -> list[tuple[float, float]]:
    """``[[price, qty], ...]`` as price/quantity pairs, both positive; anything else is skipped."""
    if not isinstance(raw, list | tuple):
        return []
    out: list[tuple[float, float]] = []
    for lv in raw:
        if not isinstance(lv, list | tuple) or len(lv) < 2:
            continue
        px, q = _num(lv[0]), _num(lv[1])
        if px is not None and q is not None and px > 0 and q > 0:
            out.append((px, q))
    return out


def _lots(units: float, lot: int) -> float:
    return round(units / lot, 2) if lot > 1 else units


def _whole_lots(units: float, lot: int) -> int:
    """The whole lots in ``units``: a part lot on a level cannot fill a lot of ours."""
    return math.floor(units / lot + _EPS)


def depth_check(
    side: str,
    qty: float,
    fill_price: float | None,
    limit: float | None,
    depth: Any,
    lot_size: int | None = 1,
    source: str | None = None,
    *,
    multiplier: float = 1.0,
) -> dict[str, Any]:
    """One fill against one recorded book. ``side`` BUY | SELL; ``qty`` in units (shares), as the order
    carries it; ``limit`` the price the order rested at, None when it walked the book (a market order or a
    cross); ``depth`` ``{bids, asks, ageMs}`` as the engine stores it; ``source`` "fill" | "place" | None.
    Never raises: a malformed or empty book is a ``no book`` verdict."""
    side_ = str(side or "").upper()
    buy = side_ == "BUY"
    lot_n = _num(lot_size)
    lot = int(lot_n) if lot_n is not None and lot_n >= 1 else 1
    mult = _num(multiplier) or 1.0
    q = _num(qty) or 0.0
    px = _num(fill_price)
    walked = limit is None
    book = depth if isinstance(depth, dict) else {}
    age = _num(book.get("ageMs"))
    out: dict[str, Any] = {
        "side": side_ or None, "qty": q, "lots": _lots(q, lot), "lotSize": lot, "paperPrice": px,
        "limit": _num(limit), "walked": walked, "source": source if book else None,
        "ageMs": round(age) if age is not None else None, "visibleQty": None, "visibleLots": None, "covered": None,
        "shortfallLots": None, "realisticPrice": None, "beyondBook": False, "beyondQty": 0.0, "optimismRs": 0.0,
        "verdict": "no book", "note": "",
    }
    if q <= 0 or px is None or px <= 0 or side_ not in ("BUY", "SELL"):
        out["note"] = "no fill to check"
        return out
    levels = _levels(book.get("asks" if buy else "bids"))
    if not levels:
        out["note"] = "no book recorded" if not book else f"the book had no {'asks' if buy else 'bids'}"
        return out
    # best first: a BUY takes the lowest ask, a SELL the highest bid
    levels.sort(key=lambda lv: lv[0], reverse=not buy)
    if walked:
        at, worse, at_px = [], levels, None  # every level at its own price
    else:
        at = [lv for lv in levels if (lv[0] <= px + _EPS if buy else lv[0] >= px - _EPS)]
        worse = [lv for lv in levels if (lv[0] > px + _EPS if buy else lv[0] < px - _EPS)]
        at_px = px
    visible = sum(v for _, v in (levels if walked else at))
    covered = min(visible, q)
    left = q - (0.0 if walked else covered)
    cost = 0.0 if walked else covered * px
    for lpx, v in worse:
        if left <= 0:
            break
        take = min(v, left)
        cost += take * lpx
        left -= take
    beyond = max(left, 0.0)
    if beyond > 0:
        last = levels[-1][0]
        rest_px = last if walked else (max(last, px) if buy else min(last, px))
        cost += beyond * rest_px
    realistic = cost / q
    if walked:
        realistic = round(realistic, 2)  # the matcher prices a walk to the paisa (exec/paper.py)
    gain = (realistic - px) if buy else (px - realistic)
    verdict = "full" if covered >= q - _EPS else ("thin" if _whole_lots(covered, lot) == 0 else "partial")
    out.update(
        visibleQty=visible, visibleLots=_whole_lots(visible, lot), covered=round(covered / q, 3),
        shortfallLots=max(0, math.ceil((q - covered) / lot - _EPS)), realisticPrice=round(realistic, 4),
        beyondBook=beyond > 0, beyondQty=beyond, optimismRs=round(gain * q * mult, 2), verdict=verdict,
    )
    out["note"] = _note(out, at_px, levels[-1][0], buy)
    return out


def _note(c: dict[str, Any], at_px: float | None, last: float, buy: bool) -> str:
    """The check in plain words, for a row's detail."""
    n, lot = c["lots"], c["lotSize"]
    unit = "lots" if lot > 1 else "units"
    side = "asks" if buy else "bids"
    if at_px is None:
        head = f"walked the {side}: the book held {c['visibleLots']:g} {unit} for {n:g}"
    else:
        head = f"{c['visibleLots']:g} {unit} on offer at {at_px:g} or better for {n:g}"
    tail = ""
    if c["beyondBook"]:
        tail = f"; {math.ceil(c['beyondQty'] / lot - _EPS):g} {unit} beyond the five levels, priced at {last:g}"
    if c["verdict"] == "full" and at_px is not None:
        return f"{head} — the book held the whole order"
    return f"{head}{tail} — realistic {c['realisticPrice']:g} against paper {c['paperPrice']:g}"


def lot_size_of(trade_qty: Any, orders: Iterable[dict[str, Any]]) -> int | None:
    """The contract's lot from a readable order id: every tag carries the POSITION's lots (``-L4``), so the
    lot is the trade's quantity over it. None when no id says."""
    q = _num(trade_qty)
    if not q or q <= 0:
        return None
    for o in orders:
        m = _LOTS_TAG.search(str((o or {}).get("client_order_id") or ""))
        if m and int(m.group(1)) > 0 and q % int(m.group(1)) == 0:
            return int(q) // int(m.group(1))
    return None


def _filled(o: dict[str, Any]) -> bool:
    return (_num(o.get("filled")) or 0) > 0 and (_num(o.get("avg_price")) or 0) > 0


def depth_check_for_order(order: Any, *, lot_size: int | None = None, multiplier: float = 1.0) -> dict[str, Any] | None:
    """One stored order (the ledger's ``orders.json``) against the book it recorded, with its identity. None for
    an order that did not fill. A BUY entry's lot is read off its id when none is given (its tag is its own
    quantity); an exit's tag is the position's, so a trade passes its lot in."""
    if not isinstance(order, dict) or not _filled(order):
        return None
    ex = order.get("exec") if isinstance(order.get("exec"), dict) else {}
    depth, source, field = None, None, None
    for f, src in BOOK_FIELDS:
        if isinstance(ex.get(f), dict) and ex.get(f):
            depth, source, field = ex[f], src, f
            break
    # a cross (or a market order) walked the book; anything else filled at one price — its limit or the touch it took
    walked = bool(ex.get("crossedTs") or ex.get("depthAtCross")) or (bool(ex) and ex.get("limit") is None)
    qty = _num(order.get("filled")) or _num(order.get("qty")) or 0.0
    if lot_size is None and str(order.get("purpose")) == "ENTRY":
        lot_size = lot_size_of(qty, [order])
    fill = _num(order.get("avg_price")) or _num(ex.get("fillPrice"))
    limit = None if walked else (_num(ex.get("limit")) or fill)
    c = depth_check(str(order.get("side") or ""), qty, fill, limit, depth, lot_size or 1, source, multiplier=multiplier)
    return {
        "orderId": order.get("id"), "clientOrderId": order.get("client_order_id"), "strategy": order.get("strategy"),
        "purpose": order.get("purpose"), "kind": ex.get("kind"), "reason": order.get("reason") or "",
        "ts": _num(ex.get("filledTs")) or _num(order.get("ts")), "bookField": field, **c,
    }


def _worst(verdicts: Iterable[str]) -> str:
    seen = [v for v in verdicts if v != "no book"]
    return max(seen, key=VERDICTS.index) if seen else "no book"


def _summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Several fills of one leg (the entry; the exits, targets and stops) as one line."""
    booked = [c for c in checks if c["verdict"] != "no book"]
    qty = sum(c["qty"] for c in checks)
    bq = sum(c["qty"] for c in booked)
    return {
        "fills": len(checks), "checked": len(booked), "qty": qty,
        "byVerdict": {v: sum(1 for c in checks if c["verdict"] == v) for v in VERDICTS},
        "verdict": _worst(c["verdict"] for c in checks),
        "optimismRs": round(sum(c["optimismRs"] for c in booked), 2),
        "shortfallLots": sum(c["shortfallLots"] or 0 for c in booked),
        "paperPrice": round(sum(c["paperPrice"] * c["qty"] for c in checks) / qty, 4) if qty else None,
        "realisticPrice": round(sum(c["realisticPrice"] * c["qty"] for c in booked) / bq, 4) if bq else None,
        "orders": checks,
    }


def depth_check_for_trade(trade: dict[str, Any], orders: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """A closed trade and its stored orders: the entry's fills and the exits' (every exit, target and stop
    sell) each against its book, summed per leg, and the trade's total ``optimismRs``. Orders that did not
    fill are ignored; the lot is read off the order ids against the trade's quantity."""
    os_ = [o for o in orders if isinstance(o, dict)]
    lot = lot_size_of(trade.get("qty"), os_)
    mult = _num(trade.get("multiplier")) or 1.0
    legs: dict[str, list[dict[str, Any]]] = {"ENTRY": [], "EXIT": []}
    for o in sorted(os_, key=lambda o: _num(o.get("ts")) or 0.0):
        c = depth_check_for_order(o, lot_size=lot, multiplier=mult)
        if c is not None and str(o.get("purpose")) in legs:
            legs[str(o.get("purpose"))].append(c)
    entry, exit_ = _summary(legs["ENTRY"]), _summary(legs["EXIT"])
    every = legs["ENTRY"] + legs["EXIT"]
    return {
        "verdict": _worst(c["verdict"] for c in every), "optimismRs": round(entry["optimismRs"] + exit_["optimismRs"], 2),
        "fills": len(every), "checked": entry["checked"] + exit_["checked"], "lotSize": lot,
        "entry": entry, "exit": exit_,
    }


def orders_for_trades(
    trades: Iterable[dict[str, Any]], orders: Iterable[dict[str, Any]], entry_books: Callable[[str], list[str]] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Each trade's own orders out of a pile of stored ones: the exits by its position, the entry by its signal
    and book. A book that opens on another's fill (a stop-rule mirror, the wide-stop shadow) has no entry order
    of its own — ``entry_books(book)`` lists the books whose entry it took, its own first."""
    exits: dict[str, list[dict[str, Any]]] = {}
    entries: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for o in orders:
        if not isinstance(o, dict):
            continue
        if str(o.get("purpose")) == "EXIT" and o.get("position_id"):
            exits.setdefault(str(o["position_id"]), []).append(o)
        elif str(o.get("purpose")) == "ENTRY" and o.get("signal_id"):
            entries.setdefault((str(o.get("strategy")), str(o["signal_id"])), []).append(o)
    out: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        book, sig = str(t.get("strategy") or ""), str(t.get("signal_id") or "")
        opened = _num(t.get("opened_ts")) or 0.0
        entry: list[dict[str, Any]] = []
        for b in (entry_books(book) if entry_books else [book]):
            cands = [o for o in entries.get((b, sig), []) if _filled(o)]
            if cands:  # the fill nearest the open, not after it
                cands.sort(key=lambda o: (abs((_num(o.get("ts")) or 0.0) - opened), _num(o.get("ts")) or 0.0))
                entry = [cands[0]]
                break
        out[str(t.get("id"))] = entry + exits.get(str(t.get("position_id") or ""), [])
    return out


def book_summary(
    trades: Iterable[dict[str, Any]], checks: dict[str, dict[str, Any]], *, worst: int = 10
) -> dict[str, Any]:
    """Many trades of one book (or a set of books): fills checked, by verdict, the rupees paper was kinder in
    total, and the ``worst`` trades by that figure."""
    rows = [(t, checks.get(str(t.get("id")))) for t in trades]
    fills = [c for _, d in rows if d for leg in ("entry", "exit") for c in d[leg]["orders"]]
    booked = [c for c in fills if c["verdict"] != "no book"]
    ranked = sorted(((t, d) for t, d in rows if d and d["checked"]), key=lambda td: -td[1]["optimismRs"])
    return {
        "trades": len(rows), "fills": len(fills), "checked": len(booked),
        "byVerdict": {v: sum(1 for c in fills if c["verdict"] == v) for v in VERDICTS},
        "optimismRs": round(sum(c["optimismRs"] for c in booked), 2),
        "worst": [
            {"id": t.get("id"), "strategy": t.get("strategy"), "underlying": t.get("underlying"), "symbol": t.get("symbol"),
             "closedTs": t.get("closed_ts"), "net": t.get("net"), "qty": t.get("qty"), "verdict": d["verdict"],
             "optimismRs": d["optimismRs"], "entryRs": d["entry"]["optimismRs"], "exitRs": d["exit"]["optimismRs"]}
            for t, d in ranked[:worst]
        ],
    }
