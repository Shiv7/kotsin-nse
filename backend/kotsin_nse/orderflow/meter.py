"""Per-minute order flow, estimated from quote snapshots.

5paisa (and every retail NSE source) sends snapshots, not one message per trade, and never says
which side initiated a trade. So each snapshot's NEW volume (the rise in the cumulative
``total_qty``) is classified as a whole:

* at or above the previous snapshot's ask → buyer-initiated;
* at or below the previous snapshot's bid → seller-initiated;
* in between → by the tick rule (price up = buy, down = sell, unchanged = as the last one).

Before the open (``auction``) and whenever the book is crossed (best bid ≥ best ask) the new volume
has no side: in NSE's pre-open the cumulative quantity grows with the auction's indicative matched
quantity while the book shows market orders at the price bands (RELIANCE 2026-10-23 09:05: bid
1302.9, ask 1066.1), and the quote rule had booked 10,364 of it as buying before a trade. It is
counted as ``unknown`` and never enters the cumulative delta.

This is the quote rule applied to batches. On a ~6-frames-a-minute feed many trades share one
frame, so it is an ESTIMATE of delta, labelled as one everywhere it is shown; a 1-second vendor feed
through the same code would sharpen it.

Per minute and instrument: buy volume, sell volume, delta, the day's cumulative delta, frames,
the average top-5 book imbalance (bid qty − ask qty) / (bid + ask) from depth snapshots, and the
last OI seen (futures). Pure state; the engine feeds it, the archive keeps it.

From every depth frame (5 levels a side on 5paisa), the book's own readings, kept
per minute so they can be tested against the next 15–30 minutes before anything trades on them
(``research/micro_ic.py``):

* ``ofi`` — Cont–Kukanov–Stoikov order-flow imbalance at the best bid/ask, summed over the minute:
  a bid that rises adds its new size, an unchanged bid adds its change in size, a bid that falls
  takes away its old size; the ask is the mirror. ``ofi_deep`` sums the same level by level down
  to ``OFI_LEVELS``. Both are in shares; divide by depth to compare stocks.
* ``qi`` — level-1 queue imbalance (B1 − A1) / (B1 + A1), the minute's mean.
* ``micro_bps`` — microprice (a1·B1 + b1·A1) / (B1 + A1) less the mid, in bps of the mid, mean.
* ``wimb`` — every level's imbalance, each weighted by exp(−distance from the mid in bps /
  ``DEPTH_DECAY_BPS``), mean.
* ``spread_bps``, ``bid_depth``, ``ask_depth`` (visible size, all levels), means; ``levels`` the most
  levels a frame had; ``mid`` the minute's last mid.

Snapshots, not an order-by-order feed: an OFI between two frames nets everything that happened in
between, and no order ID says whether a size was cancelled or filled. Estimates, labelled as such.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

#: the distance from the mid (bps) over which a level's weight in ``wimb`` falls to 1/e
DEPTH_DECAY_BPS = 5.0
#: ``ofi_deep`` sums the level-by-level OFI down to this level
OFI_LEVELS = 10

Book = Sequence[tuple[float, int]]


@dataclass(slots=True)
class FlowMinute:
    code: str
    ts: int  # minute start, epoch s
    buy: float = 0.0
    sell: float = 0.0
    unknown: float = 0.0
    frames: int = 0
    cum_delta: float = 0.0
    imb_sum: float = 0.0
    imb_n: int = 0
    oi: float | None = None
    ofi: float = 0.0
    ofi_deep: float = 0.0
    book_n: int = 0
    qi_sum: float = 0.0
    micro_sum: float = 0.0
    wimb_sum: float = 0.0
    spread_sum: float = 0.0
    bid_depth_sum: float = 0.0
    ask_depth_sum: float = 0.0
    levels: int = 0
    mid: float | None = None

    @property
    def delta(self) -> float:
        return self.buy - self.sell

    @property
    def imbalance(self) -> float | None:
        return self.imb_sum / self.imb_n if self.imb_n else None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("imb_sum", "imb_n", "qi_sum", "micro_sum", "wimb_sum", "spread_sum", "bid_depth_sum", "ask_depth_sum"):
            d.pop(k)
        d["delta"] = self.delta
        d["imbalance"] = round(self.imbalance, 4) if self.imbalance is not None else None
        n = self.book_n
        d["qi"] = round(self.qi_sum / n, 4) if n else None
        d["micro_bps"] = round(self.micro_sum / n, 3) if n else None
        d["wimb"] = round(self.wimb_sum / n, 4) if n else None
        d["spread_bps"] = round(self.spread_sum / n, 3) if n else None
        d["bid_depth"] = round(self.bid_depth_sum / n, 1) if n else None
        d["ask_depth"] = round(self.ask_depth_sum / n, 1) if n else None
        return d


@dataclass(slots=True)
class _State:
    last_total: int = 0
    bid: float = 0.0
    ask: float = 0.0
    ltp: float = 0.0
    last_side: int = 0
    cum: float = 0.0
    day: int = -1
    minutes: list[FlowMinute] = field(default_factory=list)
    bids: Book = ()
    asks: Book = ()


def classify(price: float, prev_bid: float, prev_ask: float, prev_ltp: float, last_side: int) -> int:
    """+1 buyer-initiated, −1 seller-initiated, 0 unknown."""
    if prev_ask > 0 and price >= prev_ask:
        return 1
    if prev_bid > 0 and price <= prev_bid:
        return -1
    if prev_ltp > 0 and price > prev_ltp:
        return 1
    if prev_ltp > 0 and price < prev_ltp:
        return -1
    return last_side


def imbalance(bids: Sequence[tuple[float, int]], asks: Sequence[tuple[float, int]], levels: int = 5) -> float | None:
    b = sum(q for _p, q in bids[:levels])
    a = sum(q for _p, q in asks[:levels])
    return (b - a) / (b + a) if b + a > 0 else None


def ofi(prev_bids: Book, prev_asks: Book, bids: Book, asks: Book, levels: int = 1) -> float:
    """Order-flow imbalance between two snapshots, level by level down to ``levels`` (level 1 alone
    is Cont–Kukanov–Stoikov; deeper is the multi-level version). Positive = net buying pressure."""
    e = 0.0
    for k in range(min(levels, len(prev_bids), len(prev_asks), len(bids), len(asks))):
        (pb, qb), (nb, nqb) = prev_bids[k], bids[k]
        e += nqb if nb > pb else (nqb - qb) if nb == pb else -qb
        (pa, qa), (na, nqa) = prev_asks[k], asks[k]
        e += -nqa if na < pa else (qa - nqa) if na == pa else qa
    return e


@dataclass(frozen=True, slots=True)
class BookRead:
    mid: float
    spread_bps: float
    qi: float
    micro_bps: float
    wimb: float
    bid_depth: float
    ask_depth: float
    levels: int


def read_book(bids: Book, asks: Book, decay_bps: float = DEPTH_DECAY_BPS) -> BookRead | None:
    """One snapshot's readings, or None for a one-sided, locked or crossed book."""
    if not bids or not asks:
        return None
    (b1, q_b), (a1, q_a) = bids[0], asks[0]
    if b1 <= 0 or a1 <= b1 or q_b + q_a <= 0:
        return None
    mid = (a1 + b1) / 2
    micro = (a1 * q_b + b1 * q_a) / (q_b + q_a)
    num = den = 0.0
    for sign, book in ((1, bids), (-1, asks)):
        for p, q in book:
            w = math.exp(-abs(p - mid) / mid * 1e4 / decay_bps)
            num += sign * w * q
            den += w * q
    return BookRead(
        mid=mid,
        spread_bps=(a1 - b1) / mid * 1e4,
        qi=(q_b - q_a) / (q_b + q_a),
        micro_bps=(micro - mid) / mid * 1e4,
        wimb=num / den if den > 0 else 0.0,
        bid_depth=float(sum(q for _p, q in bids)),
        ask_depth=float(sum(q for _p, q in asks)),
        levels=min(len(bids), len(asks)),
    )


class FlowMeter:
    """Feed it ticks, depth and OI for the codes it should watch; read minutes back per code."""

    def __init__(self, keep_minutes: int = 400) -> None:
        self._st: dict[str, _State] = {}
        self.keep = keep_minutes
        self.closed: list[FlowMinute] = []  # minutes closed since the last drain (for the archive)

    def _minute(self, code: str, ts: float) -> FlowMinute:
        st = self._st.setdefault(code, _State())
        m = int(ts // 60) * 60
        day = m // 86400
        if st.day != day:  # a new UTC day — NSE's session never spans one
            st.day, st.cum, st.minutes, st.last_total, st.bids, st.asks = day, 0.0, [], 0, (), ()
        if not st.minutes or st.minutes[-1].ts != m:
            if st.minutes:
                self.closed.append(st.minutes[-1])
            st.minutes.append(FlowMinute(code, m, cum_delta=st.cum))
            if len(st.minutes) > self.keep:
                del st.minutes[0]
        return st.minutes[-1]

    def on_tick(self, code: str, ts: float, ltp: float, total_qty: int, bid: float, ask: float, auction: bool = False) -> None:
        if ltp <= 0:
            return
        st = self._st.setdefault(code, _State())
        fm = self._minute(code, ts)
        fm.frames += 1
        new = total_qty - st.last_total if st.last_total else 0
        if new > 0 and (auction or (bid > 0 and ask > 0 and bid >= ask)):
            fm.unknown += new
        elif new > 0:
            side = classify(ltp, st.bid, st.ask, st.ltp, st.last_side)
            if side > 0:
                fm.buy += new
                st.cum += new
            elif side < 0:
                fm.sell += new
                st.cum -= new
            else:
                fm.unknown += new
            st.last_side = side or st.last_side
            fm.cum_delta = st.cum
        if total_qty >= st.last_total:
            st.last_total = total_qty
        st.bid, st.ask, st.ltp = bid, ask, ltp

    def on_depth(self, code: str, ts: float, bids: Sequence[tuple[float, int]], asks: Sequence[tuple[float, int]]) -> None:
        if bids and asks and bids[0][0] >= asks[0][0]:
            return  # a crossed book is the auction's, not a market anyone can trade against
        imb = imbalance(bids, asks)
        if imb is None:
            return
        fm = self._minute(code, ts)
        fm.imb_sum += imb
        fm.imb_n += 1
        r = read_book(bids, asks)
        if r is None:
            return
        st = self._st[code]
        if st.bids and st.asks:
            fm.ofi += ofi(st.bids, st.asks, bids, asks, 1)
            fm.ofi_deep += ofi(st.bids, st.asks, bids, asks, OFI_LEVELS)
        st.bids, st.asks = tuple(bids), tuple(asks)
        fm.book_n += 1
        fm.qi_sum += r.qi
        fm.micro_sum += r.micro_bps
        fm.wimb_sum += r.wimb
        fm.spread_sum += r.spread_bps
        fm.bid_depth_sum += r.bid_depth
        fm.ask_depth_sum += r.ask_depth
        fm.levels = max(fm.levels, r.levels)
        fm.mid = r.mid

    def on_oi(self, code: str, ts: float, oi: float) -> None:
        self._minute(code, ts).oi = oi

    def restore(self, rows: Sequence[dict[str, Any]], day: int) -> int:
        """Put back today's minutes from the archive (``FlowMinute.to_json`` rows, any order) after a
        restart, ahead of whatever the live feed has added since; the running cumulative delta
        continues from the last restored minute. Means come back as one-sample sums. Returns the
        minutes restored."""
        by: dict[str, dict[int, dict[str, Any]]] = {}
        for r in rows:
            cur = by.setdefault(str(r["code"]), {}).get(int(r["ts"]))
            if cur is None or (r.get("frames") or 0) > (cur.get("frames") or 0):  # two runs' halves of a minute: the fuller
                by[str(r["code"])][int(r["ts"])] = r
        n = 0
        for code, mins in by.items():
            st = self._st.setdefault(code, _State())
            live_from = st.minutes[0].ts if st.minutes else None
            old = []
            for ts in sorted(mins):
                if live_from is not None and ts >= live_from:
                    break
                r = mins[ts]
                g = lambda k, r=r: float(r.get(k) or 0.0)  # noqa: E731
                bn = int(r.get("book_n") or 0)
                fm = FlowMinute(code, ts, buy=g("buy"), sell=g("sell"), unknown=g("unknown"), frames=int(r.get("frames") or 0),
                                cum_delta=g("cum_delta"), oi=r.get("oi") if r.get("oi") == r.get("oi") else None,
                                ofi=g("ofi"), ofi_deep=g("ofi_deep"), book_n=bn, levels=int(r.get("levels") or 0),
                                mid=r.get("mid") if r.get("mid") == r.get("mid") else None)
                imb = r.get("imbalance")
                if imb is not None and imb == imb:
                    fm.imb_sum, fm.imb_n = float(imb), 1
                for k, attr in (("qi", "qi_sum"), ("micro_bps", "micro_sum"), ("wimb", "wimb_sum"), ("spread_bps", "spread_sum"),
                                ("bid_depth", "bid_depth_sum"), ("ask_depth", "ask_depth_sum")):
                    v = r.get(k)
                    if bn and v is not None and v == v:
                        setattr(fm, attr, float(v) * bn)
                old.append(fm)
            if not old:
                continue
            st.minutes = old + st.minutes
            if len(st.minutes) > self.keep:
                del st.minutes[: len(st.minutes) - self.keep]
            st.day = day
            if live_from is None:
                st.cum = old[-1].cum_delta
            n += len(old)
        return n

    def minutes(self, code: str) -> list[FlowMinute]:
        st = self._st.get(code)
        return list(st.minutes) if st else []

    def drain(self) -> list[FlowMinute]:
        out, self.closed = self.closed, []
        return out

    def stats(self) -> dict[str, Any]:
        return {"instruments": len(self._st), "pending_closed": len(self.closed)}
