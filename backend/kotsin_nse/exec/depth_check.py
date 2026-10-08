"""Paper fills against the book that was there: a RECORD-ONLY check (operator, 2026-10-09: "go ahead build it
n tell me where to view it").

The paper matcher fills a resting limit WHOLE at its price, with no queue and no share of the depth
(exec/resting.py ``fills``: "Optimistic — no queue"). A real order at that price gets at most what the book
shows there; the rest walks to the next levels. Every entry fill since 3 Oct 17:51 keeps the five levels a side
it filled against (``exec.depthAtFill``), and so do about 60 % of exit fills. A crossed exit keeps the book it
walked (``depthAtCross``), and most orders keep the book from when they were placed (``depthAtPlace``). On 108 of
206 depth-logged fills the book showed fewer lots at the price than we traded.

This module reads those stored books. For each fill it reports how much the book showed at or better than the
paper price, the price the whole quantity would really have averaged, and how many rupees kinder the paper fill
was than that. The API calls it when a page asks (``/api/trades?depth=1``'s ``depthCheck``, ``/api/depth-check``),
and so does phase 35's deploy/compare_ab.py, for both engines. Nothing in the order path imports it (tests/test_depth_check.py checks this), so
it can never move an order, a fill, a stop, a wallet or a decision. Nothing here raises on a malformed or empty
book: such a fill is "no book".

A resting limit also fills when a trade prints THROUGH its price while the touch never reaches it (exec/resting.py
``fills``). Our order would then have stood first at its price, so the other side of the book is not what it would
have cost: the honest alternative is no fill, or part of one. Such a fill gets its own verdict, ``print``, and
stays out of the rupees (review, 9 Oct 2026: on 8 Oct these were half the "kinder" total on both engines). What
taking the book instead would have cost is kept beside it as ``crossRs``, to read, never summed.

One module, the same on both engines (review, 9 Oct 2026: the two copies had drifted, and "thin" meant under one
lot on A but under half the lots on B). It uses only the standard library and no run-time syntax newer than
Python 3.9, so compare_ab.py can load it under the system interpreter.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Covered below this share of the quantity, the fill is "thin"; from here up to all of it, "partial".
THIN_BELOW = 0.5
#: A book older than this at the fill is flagged stale. It is the paper matcher's own age limit outside
#: the opening window (exec/paper.py ``max_book_age_ms``). The verdict still stands, but the page says how old it was.
STALE_MS = 6_000.0
#: A resting order judged only on its book at placement: allowed when it filled within this long of
#: being placed (a marketable limit). An order that rested for longer filled against a book nobody recorded.
PLACE_GOOD_FOR_S = 2.0
#: How many trades the summary's "worst" lists keep.
WORST_N = 10

#: Every verdict. full / partial / thin are judged on the book and priced; ``print`` filled on a trade through
#: the price and is not priced; ``no book`` had nothing recorded to judge on.
VERDICTS = ("full", "partial", "thin", "print", "no book")
#: The verdicts judged on the book, best first: a leg or a trade carries the worst of its fills.
_RANK = {"full": 0, "partial": 1, "thin": 2}
_TAG_LOTS = re.compile(r"-L(\d+)$")


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


def _levels(raw: Any, *, buy: bool) -> list[tuple[float, float]]:
    """One side of a stored book as ``(price, quantity)``, best first: the asks rising for a buy, the bids
    falling for a sell. A level that is not a positive price and quantity is dropped, never raised on."""
    out: list[tuple[float, float]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for lv in raw:
        if isinstance(lv, Mapping):
            px, q = _pos(lv.get("price", lv.get("px"))), _pos(lv.get("qty", lv.get("quantity")))
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            px, q = _pos(lv[0]), _pos(lv[1])
        else:
            continue
        if px is not None and q is not None:
            out.append((px, q))
    out.sort(key=lambda x: x[0], reverse=not buy)
    return out


def _lots(units: float, lot: int) -> float:
    """Units as lots: a whole number when they divide, else to two places."""
    n = units / lot
    return round(n) if abs(n - round(n)) < 1e-9 else round(n, 2)


def _inr(x: float) -> str:
    """Rupees the Indian way, "₹1,23,456"; paise kept under ₹100."""
    whole, _, frac = (f"{abs(x):.2f}" if abs(x) < 100 else f"{abs(x):.0f}").partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        head, groups = head[:-2], [head[-2:], *groups]
    grouped = ",".join([g for g in (head, *groups) if g] + [tail])
    return f"{'-' if x < 0 else ''}₹{grouped}{f'.{frac}' if frac else ''}"


def _count(n: float, lot: int) -> str:
    """``n`` lots (or units, for a contract without lots) in words: "1 lot", "4 lots", "250 units"."""
    unit = "lot" if lot > 1 else "unit"
    return f"{n:g} {unit}{'' if n == 1 else 's'}"


def depth_check(
    side: Any,
    qty: Any,
    fill_price: Any,
    limit: Any,
    depth: Any,
    lot_size: Any,
    source: str | None,
    *,
    multiplier: Any = 1,
    reach: Any = None,
) -> dict[str, Any]:
    """One fill against the book recorded with it.

    ``side`` is ``"BUY"`` or ``"SELL"``. ``qty`` is the units filled and ``fill_price`` the paper price.
    ``limit`` is the order's limit. None means a market order (or a crossed exit), which the matcher walked
    through the book. ``depth`` is the stored book ``{bids: [[px, qty]..], asks: [[px, qty]..], ageMs}``.
    ``source`` says which book it is (``"fill"``, ``"cross"`` or ``"place"``). ``reach`` is the touch on the side
    that trades as it was recorded with the fill (``bookAtFill``: the ask for a buy, the bid for a sell).

    * **visible**: what the book showed at or better than the paper price, as units and whole lots. A buy
      counts asks at or below it, a sell bids at or above it. A walked order is judged at the worst level its
      walk reached, because that is the last price it paid.
    * **realistic price**: a limit fill gets the visible part at the paper price. The rest walks on through
      the levels beyond it, up the asks for a buy and down the bids for a sell. A walked order gets each level
      at its own price, rounded to the paisa as the matcher rounds its walk (exec/paper.py). Whatever the five
      levels cannot hold is priced at the last level and flagged ``beyondBook``. For a limit fill that price is
      never better than the paper price, because a limit fills at its own price or not at all.
    * **optimismRs**: how many rupees kinder the paper fill was than that price, over the whole quantity.
      Positive means the paper was kinder.
    * **verdict**: ``full`` when the book showed every lot, ``partial`` when it showed at least
      ``THIN_BELOW`` of them, ``thin`` below that, and ``no book`` when there was nothing to judge on. A limit
      whose touch at the fill never reached its price (``reach``, else the book's own best level when the book is
      the one at the fill) was filled by a trade printing through it: ``print``, with no ``optimismRs``. What
      taking the book would have cost instead is ``crossRs``, to read only.
    """
    buy = str(getattr(side, "value", side) or "").upper() == "BUY"
    q, px = _pos(qty), _pos(fill_price)
    lot = max(1, int(_pos(lot_size) or 1))
    mult = _pos(multiplier) or 1.0
    market = limit is None
    book = depth if isinstance(depth, Mapping) else None
    age = _num(book.get("ageMs")) if book is not None else None
    out: dict[str, Any] = {
        "side": "BUY" if buy else "SELL", "qty": int(q) if q else 0, "lots": _lots(q, lot) if q else 0, "lotSize": lot,
        "paperPrice": px, "limit": _num(limit), "market": market, "source": None, "ageMs": None, "stale": False,
        "visibleQty": None, "visibleLots": None, "covered": None, "shortfallLots": None, "touch": None, "touchQty": None,
        "realisticPrice": None, "beyondBook": False, "beyondQty": 0, "optimismRs": None, "verdict": "no book",
        "onPrint": False, "reach": None, "crossRs": None,
    }
    if q is None or px is None:
        return {**out, "words": "no fill price or quantity to judge"}
    levels = _levels(book.get("asks" if buy else "bids"), buy=buy) if book is not None else []
    if not levels:
        why = "no book recorded" if book is None else f"the recorded book had no {'asks' if buy else 'bids'}"
        return {**out, "words": why}
    tol = 1e-6 * max(1.0, px)

    def better(p: float, than: float) -> bool:
        return p <= than + tol if buy else p >= than - tol

    cost, rem, beyond = 0.0, q, 0.0
    if market:
        # walked: each level at its own price, as the matcher took them
        mark = levels[0][0]
        for p, avail in levels:
            take = min(avail, rem)
            cost, rem, mark = cost + take * p, rem - take, p
            if rem <= 0:
                break
        visible = sum(a for p, a in levels if better(p, mark))
    else:
        # a limit fill: the visible part at its own price, the rest walked beyond it
        visible = sum(a for p, a in levels if better(p, px))
        take = min(visible, q)
        cost, rem = take * px, q - take
        for p, avail in levels:
            if rem <= 0:
                break
            if better(p, px):
                continue
            take = min(avail, rem)
            cost, rem = cost + take * p, rem - take
    if rem > 1e-9:
        last = levels[-1][0]
        if not market:  # a limit fills at its own price or not at all: past the book is never better than the paper
            last = max(last, px) if buy else min(last, px)
        cost, beyond = cost + rem * last, rem
    realistic = cost / q
    if market:
        realistic = round(realistic, 2)  # the matcher prices its walk to the paisa (exec/paper.py)
    optimism = ((realistic - px) if buy else (px - realistic)) * q * mult
    covered = min(visible, q) / q
    verdict = "full" if covered >= 1 - 1e-9 else "partial" if covered >= THIN_BELOW else "thin"
    vis_lots = math.floor(visible / lot + 1e-9)
    want_lots = math.ceil(q / lot - 1e-9)
    out.update(
        source=source, ageMs=round(age) if age is not None else None, stale=bool(age is not None and age > STALE_MS),
        visibleQty=int(visible), visibleLots=vis_lots, covered=round(covered, 3), shortfallLots=max(0, want_lots - vis_lots),
        touch=levels[0][0], touchQty=int(levels[0][1]), realisticPrice=round(realistic, 4), beyondBook=beyond > 0,
        beyondQty=round(beyond), optimismRs=round(optimism, 2), verdict=verdict,
    )
    if not market:
        # a limit fills on the touch reaching it OR on a trade printing through it (exec/resting.py ``fills``)
        at = _pos(reach)
        if at is None and source == "fill":
            at = levels[0][0]  # no quote kept with the fill: the book at the fill says where the touch was
        if at is not None and ((at > px + tol) if buy else (at < px - tol)):
            out.update(verdict="print", onPrint=True, reach=at, crossRs=out["optimismRs"], optimismRs=None)
    out["words"] = _aged(_words(out, buy=buy, lot=lot), out)
    return out


def _words(c: dict[str, Any], *, buy: bool, lot: int) -> str:
    """The check in a line, for a person: the lots the book showed at the price, where the rest would
    have gone, and the rupees."""
    want = c["lots"] if lot > 1 else c["qty"]
    vis = c["visibleLots"] if lot > 1 else c["visibleQty"]
    if c["verdict"] == "print":
        cross = c["crossRs"] or 0.0
        return (f"filled on a trade printing through {c['paperPrice']:.2f} while the {'ask' if buy else 'bid'} was "
                f"{c['reach']:.2f}: our order would have stood first at its price, so the book does not price it "
                f"(taking the {'asks' if buy else 'bids'} instead: {_inr(abs(cross))} {'worse' if cross >= 0 else 'better'})")
    if c["market"]:
        head = f"walked the {'asks' if buy else 'bids'}: the book held {_count(vis, lot)} up to the last level taken"
    else:
        head = f"the book showed {_count(vis, lot)} at {c['paperPrice']:.2f} or {'lower' if buy else 'higher'}"
    if c["verdict"] == "full":
        return f"{head}, enough for all {_count(want, lot)} traded"
    rest = f"{head} of the {_count(want, lot)} traded; walking the rest, all of it averages {c['realisticPrice']:.2f}"
    if c["beyondBook"]:
        beyond = _lots(c["beyondQty"], lot) if lot > 1 else c["beyondQty"]
        rest += f" ({_count(beyond, lot)} past the levels shown)"
    rs = c["optimismRs"]
    return f"{rest}: the paper fill was {_inr(abs(rs))} {'kinder' if rs >= 0 else 'harsher'}"


def _aged(words: str, c: Mapping[str, Any]) -> str:
    return f"{words} (the book was {c['ageMs'] / 1000:.1f} s old)" if c.get("stale") and c.get("ageMs") is not None else words


def _book_of(ex: Mapping[str, Any], *, market: bool) -> tuple[Any, str | None, str]:
    """The stored book an order is judged on, which book it is, and why when there is none. A fill is judged
    on the book at the fill, and a crossed exit on the book it walked. The book at placement is used only for a
    market order or a limit that filled within ``PLACE_GOOD_FOR_S`` of being placed."""
    if ex.get("depthAtFill"):
        return ex["depthAtFill"], "fill", ""
    if ex.get("depthAtCross"):
        return ex["depthAtCross"], "cross", ""
    if ex.get("depthAtPlace"):
        wait = _num(ex.get("waitS"))
        if market or (wait is not None and wait <= PLACE_GOOD_FOR_S):
            return ex["depthAtPlace"], "place", ""
        return None, None, f"only the book at placement, {wait:.0f} s before the fill" if wait is not None else "only the book at placement"
    return None, None, "no book recorded"


def depth_check_for_order(order: Any, *, lot_size: Any = None, multiplier: Any = 1) -> dict[str, Any] | None:
    """One stored order row (orders.json) checked against its own recorded book; None for an order that did
    not fill. The order's own ``exec`` trail says how it filled. A trail with ``crossedTs`` (an exit crossed
    at its deadline) or no limit (a market order) was walked, and anything else filled at its limit."""
    if not isinstance(order, Mapping):
        return None
    filled = _pos(order.get("filled"))
    if order.get("status") != "FILLED" and not filled:
        return None
    raw = order.get("exec")
    ex: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    crossed = ex.get("crossedTs") is not None or bool(ex.get("depthAtCross"))
    market = crossed or ex.get("limit") is None
    depth, source, why = _book_of(ex, market=market)
    price = _pos(order.get("avg_price")) or _pos(ex.get("fillPrice"))
    buy = str(order.get("side") or "").upper() == "BUY"
    at_fill = ex.get("bookAtFill")
    reach = at_fill.get("ask" if buy else "bid") if isinstance(at_fill, Mapping) else None
    c = depth_check(order.get("side"), filled or order.get("qty"), price, None if market else ex.get("limit"), depth,
                    lot_size, source, multiplier=multiplier, reach=reach)
    if why and c["verdict"] == "no book":
        c["words"] = why
    return {
        **c, "orderId": order.get("id"), "clientOrderId": order.get("client_order_id"), "purpose": str(order.get("purpose") or ""),
        "kind": str(ex.get("kind") or ("market" if market else "")), "crossed": crossed,
        "filledTs": _num(ex.get("filledTs")) or _num(order.get("ts")), "reason": order.get("reason") or "",
    }


def lot_size_from_tags(trade: Mapping[str, Any], orders: Iterable[Mapping[str, Any]]) -> int | None:
    """A trade's lot size from its orders' ids when nothing else gives it. ``…-HINDUNILVR-1960CE-L4`` names
    the position's lots (engine.py ``_contract_tag``), and the trade's ``qty`` is the position's units."""
    qty = _pos(trade.get("qty"))
    if qty is None:
        return None
    for o in orders:
        m = _TAG_LOTS.search(str(o.get("client_order_id") or ""))
        if m and int(m.group(1)) > 0 and qty % int(m.group(1)) == 0:
            return int(qty // int(m.group(1)))
    return None


def _worst(verdicts: Iterable[str]) -> str:
    """The worst book-judged verdict; failing one, ``print`` when a fill was on a print, else ``no book``."""
    vs = list(verdicts)
    judged = [v for v in vs if v in _RANK]
    if judged:
        return max(judged, key=lambda v: _RANK[v])
    return "print" if "print" in vs else "no book"


def _side(checks: list[dict[str, Any]]) -> dict[str, Any]:
    judged = [c for c in checks if c["verdict"] in _RANK]
    return {
        "fills": len(checks), "checked": len(judged), "prints": sum(1 for c in checks if c["verdict"] == "print"),
        "verdict": _worst(c["verdict"] for c in checks),
        "optimismRs": round(sum(c["optimismRs"] for c in judged), 2) if judged else None, "orders": checks,
    }


def depth_check_for_trade(
    trade: Mapping[str, Any],
    orders: Iterable[Mapping[str, Any]],
    *,
    lot_size: Any = None,
    entry_from: str | None = None,
) -> dict[str, Any]:
    """A closed trade's fills against their books: the entry, the exits summed (stop, target rungs and
    crosses alike), and the total rupees the paper fills were kinder. ``orders`` are the trade's own filled
    orders (``orders_for_trade``). ``entry_from`` names the book whose entry fill a mirror copied. A fill on a
    print counts in ``prints``, never in the rupees."""
    orders = [o for o in orders if isinstance(o, Mapping)]
    lot = _pos(lot_size) or lot_size_from_tags(trade, orders) or 1
    mult = _pos(trade.get("multiplier")) or 1
    checks = [c for c in (depth_check_for_order(o, lot_size=lot, multiplier=mult) for o in orders) if c is not None]
    checks.sort(key=lambda c: c["filledTs"] or 0.0)
    entry = _side([c for c in checks if c["purpose"] == "ENTRY"])
    exits = _side([c for c in checks if c["purpose"] == "EXIT"])
    judged = [c for c in checks if c["verdict"] in _RANK]
    return {
        "verdict": _worst(c["verdict"] for c in checks), "optimismRs": round(sum(c["optimismRs"] for c in judged), 2) if judged else None,
        "fills": len(checks), "checked": len(judged), "prints": sum(1 for c in checks if c["verdict"] == "print"),
        "stale": sum(1 for c in judged if c["stale"]), "lotSize": int(lot), "entryFrom": entry_from, "entry": entry, "exit": exits,
    }


def index_orders(orders: Iterable[Mapping[str, Any]]) -> dict[str, dict[Any, list[Mapping[str, Any]]]]:
    """Filled orders, entries by ``(book, signal id)`` and exits by position. An entry is placed before its
    position exists, so it carries no position id."""
    entries: dict[Any, list[Mapping[str, Any]]] = {}
    exits: dict[Any, list[Mapping[str, Any]]] = {}
    for o in orders:
        if not isinstance(o, Mapping) or (o.get("status") != "FILLED" and not _pos(o.get("filled"))):
            continue
        if o.get("purpose") == "ENTRY":
            entries.setdefault((o.get("strategy"), o.get("signal_id")), []).append(o)
        elif o.get("purpose") == "EXIT" and o.get("position_id"):
            exits.setdefault(o.get("position_id"), []).append(o)
    return {"entries": entries, "exits": exits}


def orders_for_trade(
    trade: Mapping[str, Any], index: Mapping[str, Mapping[Any, list[Mapping[str, Any]]]], *, sources: Mapping[str, str] | None = None
) -> tuple[list[Mapping[str, Any]], str | None]:
    """A trade's filled orders, and the book whose entry it copied. A stop-rule mirror and the wide-stop shadow
    place no entry of their own. They copy their source book's fill, so the entry is looked for along
    ``sources`` (book → the book it copies), up to the book that placed it."""
    exits = list(index.get("exits", {}).get(trade.get("position_id"), []))
    sid, book, seen = trade.get("signal_id"), str(trade.get("strategy") or ""), set()
    opened = _num(trade.get("opened_ts"))
    hop = 0
    while book and book not in seen and hop < 6:
        seen.add(book)
        found = index.get("entries", {}).get((book, sid))
        if found:
            pick = found[0]
            if len(found) > 1 and opened is not None:
                # more than one fill on the trigger: the one nearest the trade's opening
                pick = min(found, key=lambda o: abs((_num((o.get("exec") or {}).get("filledTs")) or _num(o.get("ts")) or 0.0) - opened))
            return [pick, *exits], (book if hop else None)
        book, hop = (sources or {}).get(book, ""), hop + 1
    return exits, None


def lot_sizes(positions: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Position id → its contract's lot size, from the stored position rows."""
    out: dict[str, int] = {}
    for p in positions:
        inst = p.get("instrument") if isinstance(p, Mapping) else None
        lot = _pos(inst.get("lot_size")) if isinstance(inst, Mapping) else None
        if lot and p.get("id"):
            out[str(p["id"])] = int(lot)
    return out


def check_trades(
    trades: Iterable[Mapping[str, Any]],
    orders: Iterable[Mapping[str, Any]],
    *,
    positions: Iterable[Mapping[str, Any]] = (),
    sources: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Trade id → its ``depth_check_for_trade``, the orders and lot sizes gathered from the stored rows."""
    index = index_orders(orders)
    lots = lot_sizes(positions)
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        mine, copied = orders_for_trade(t, index, sources=sources)
        out[str(t.get("id"))] = depth_check_for_trade(t, mine, lot_size=lots.get(str(t.get("position_id"))), entry_from=copied)
    return out


def _blank() -> dict[str, Any]:
    return {"trades": 0, "fills": 0, "checked": 0, "byVerdict": {v: 0 for v in VERDICTS}, "optimismRs": 0.0, "stale": 0, "copied": 0}


def _worst_row(t: Mapping[str, Any], dc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tradeId": t.get("id"), "book": t.get("strategy"), "underlying": t.get("underlying"), "symbol": t.get("symbol"),
        "closedTs": _num(t.get("closed_ts")), "net": _num(t.get("net")), "optimismRs": dc["optimismRs"], "verdict": dc["verdict"],
        "entryRs": dc["entry"]["optimismRs"], "exitRs": dc["exit"]["optimismRs"], "prints": dc.get("prints", 0),
    }


def summarize(
    trades: Sequence[Mapping[str, Any]],
    checks: Mapping[str, Mapping[str, Any]],
    *,
    order: Sequence[str] = (),
    worst_n: int = WORST_N,
) -> dict[str, Any]:
    """Per book: the trades, the fills, how many were judged on a book and each verdict, the total rupees the
    paper fills were kinder, how many books were stale, and the ``worst_n`` trades the paper flattered most.
    The same again over every trade given (``total``, ``worst``).

    A fill is counted once however many trades carry it. A stop-rule mirror and the wide-stop shadow copy their
    book's entry fill: it shows in each of their rows (``copied`` says how many), but the total counts it once
    (review, 9 Oct 2026: with the mirrors shown, one RT-Y entry was summed up to six times). Fills on a print
    count under ``byVerdict.print`` and never in the rupees. ``order`` lists the books in the order to show them;
    any other book follows."""
    books: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[Any]] = {}
    seen_total: set[Any] = set()
    total = _blank()
    worst_all: list[dict[str, Any]] = []
    for t in trades:
        dc = checks.get(str(t.get("id")))
        if dc is None:
            continue
        name = str(t.get("strategy") or "")
        b = books.setdefault(name, {**_blank(), "worst": []})
        fills = [*dc["entry"]["orders"], *dc["exit"]["orders"]]
        for agg, counted in ((b, seen.setdefault(name, set())), (total, seen_total)):
            agg["trades"] += 1
            for i, c in enumerate(fills):
                oid = c.get("orderId") or f"{t.get('id')}#{i}"
                if oid in counted:
                    continue
                counted.add(oid)
                agg["fills"] += 1
                agg["byVerdict"][c["verdict"] if c["verdict"] in agg["byVerdict"] else "no book"] += 1
                if c["verdict"] in _RANK:
                    agg["checked"] += 1
                    agg["optimismRs"] += c["optimismRs"] or 0.0
                    agg["stale"] += 1 if c["stale"] else 0
        if dc.get("entryFrom"):
            b["copied"] += 1
        if dc["optimismRs"] is not None and dc["optimismRs"] > 0:
            row = _worst_row(t, dc)
            b["worst"].append(row)
            worst_all.append(row)
    rank = {k: i for i, k in enumerate(order)}
    rows = []
    for k in sorted(books, key=lambda k: (rank.get(k, len(rank)), k)):
        b = books[k]
        b["optimismRs"] = round(b["optimismRs"], 2)
        b["worst"] = sorted(b["worst"], key=lambda r: -r["optimismRs"])[:worst_n]
        rows.append({"book": k, **b})
    total["optimismRs"] = round(total["optimismRs"], 2)
    return {"thinBelow": THIN_BELOW, "staleMs": STALE_MS, "books": rows, "total": total,
            "worst": sorted(worst_all, key=lambda r: -r["optimismRs"])[:worst_n]}
