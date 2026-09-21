"""Microstructure from the real 20-level order book — and an honest account of what that feed
cannot give.

What 5paisa publishes: ``MarketDepthService``, a 20-level book per instrument, refreshed at
roughly 2–5 Hz on active names. What it does **not** publish: a trade tape. ``MarketFeedV3`` is a
snapshot of last price and cumulative volume, so there is no trade-by-trade record and, crucially,
**no aggressor side**. That rules out the two metrics people ask for first:

* **Kyle's λ** needs signed order flow — which side hit — regressed on price impact. Not computable.
* **VPIN** needs volume bucketed by taker side. Not computable. Bulk-classifying from the tick rule
  on a snapshot feed would be inventing a tape, and a number invented is worse than a number absent.

What the book *does* support, exactly, and what this module computes:

* **L1 order-flow imbalance** (Cont, Kukanov & Stoikov 2014): the signed change in resting size at
  the best bid and ask between two book states. The closest thing to flow that a depth feed gives.
* **depth imbalance** over the top *k* levels — ``(Σbid − Σask) / (Σbid + Σask)``.
* **microprice** — the size-weighted mid, ``(Pa·Qb + Pb·Qa) / (Qb + Qa)``. Leads the mid.
* **spread** in basis points, and **book updates per bar** as a liquidity/attention proxy.

These are accumulated per bar and stamped into ``UnifiedBar.extra["micro"]`` at close, so a
strategy that wants them reads them like any other bar field — and a backtest that lacks them
sees ``None``, which its gates must declare a policy for (LEARNINGS R3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

Level = tuple[float, int]


@dataclass(slots=True)
class BookState:
    bids: list[Level]  # best first
    asks: list[Level]  # best first
    ts: float

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None


def l1_ofi(prev: BookState, cur: BookState) -> float | None:
    """Cont–Kukanov–Stoikov event-level OFI between two consecutive book states.

    ``e = 1{Pb ≥ Pb'}·Qb − 1{Pb ≤ Pb'}·Qb' − 1{Pa ≤ Pa'}·Qa + 1{Pa ≥ Pa'}·Qa'`` where primes are the
    previous state. Positive = net buying pressure arriving at the touch.
    """
    if not (prev.best_bid and prev.best_ask and cur.best_bid and cur.best_ask):
        return None
    pb0, qb0 = prev.best_bid
    pa0, qa0 = prev.best_ask
    pb1, qb1 = cur.best_bid
    pa1, qa1 = cur.best_ask
    e = 0.0
    if pb1 >= pb0:
        e += qb1
    if pb1 <= pb0:
        e -= qb0
    if pa1 <= pa0:
        e -= qa1
    if pa1 >= pa0:
        e += qa0
    return float(e)


def depth_imbalance(book: BookState, levels: int = 5) -> float | None:
    b = sum(q for _, q in book.bids[:levels])
    a = sum(q for _, q in book.asks[:levels])
    return None if b + a == 0 else (b - a) / (b + a)


def microprice(book: BookState) -> float | None:
    if not (book.best_bid and book.best_ask):
        return None
    pb, qb = book.best_bid
    pa, qa = book.best_ask
    return None if qb + qa == 0 else (pa * qb + pb * qa) / (qb + qa)


def spread_bps(book: BookState) -> float | None:
    if not (book.best_bid and book.best_ask):
        return None
    pb, pa = book.best_bid[0], book.best_ask[0]
    mid = (pa + pb) / 2
    return None if mid <= 0 else (pa - pb) / mid * 1e4


@dataclass(slots=True)
class MicroAccumulator:
    """Per-instrument running totals for the bar in progress."""

    bucket_ts: int
    updates: int = 0
    ofi_sum: float = 0.0
    ofi_pos: float = 0.0
    ofi_neg: float = 0.0
    imbalance_sum: float = 0.0
    imbalance_n: int = 0
    spread_sum: float = 0.0
    spread_n: int = 0
    micro_last: float | None = None
    mid_last: float | None = None
    depth_bid_last: int = 0
    depth_ask_last: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "updates": self.updates,
            "ofi": round(self.ofi_sum, 1),
            "ofi_buy": round(self.ofi_pos, 1),
            "ofi_sell": round(self.ofi_neg, 1),
            "imbalance": round(self.imbalance_sum / self.imbalance_n, 4) if self.imbalance_n else None,
            "spread_bps": round(self.spread_sum / self.spread_n, 2) if self.spread_n else None,
            "microprice": round(self.micro_last, 4) if self.micro_last is not None else None,
            "mid": round(self.mid_last, 4) if self.mid_last is not None else None,
            "micro_minus_mid": (
                round(self.micro_last - self.mid_last, 4)
                if self.micro_last is not None and self.mid_last is not None
                else None
            ),
            "depth5_bid": self.depth_bid_last,
            "depth5_ask": self.depth_ask_last,
        }


@dataclass(slots=True)
class MicroAggregator:
    """Consumes depth frames; produces per-bar microstructure for the decision timeframe."""

    tf_seconds: int
    bucket_of: Any  # Callable[[str, float], int] -> bucket start for (scrip_code, ts)
    levels: int = 5
    _last: dict[str, BookState] = field(default_factory=dict)
    _acc: dict[str, MicroAccumulator] = field(default_factory=dict)
    _closed: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)
    frames: int = 0
    tape_available: bool = False  # a fact about the venue, surfaced so nobody assumes otherwise

    def on_depth(self, scrip_code: str, bids: list[Level], asks: list[Level], ts: float) -> None:
        self.frames += 1
        cur = BookState(bids, asks, ts)
        bucket = int(self.bucket_of(scrip_code, ts))
        acc = self._acc.get(scrip_code)
        if acc is None or acc.bucket_ts != bucket:
            if acc is not None:
                self._closed.setdefault(scrip_code, {})[acc.bucket_ts] = acc.to_json()
                # keep only a short history per instrument; the bar carries the durable copy
                hist = self._closed[scrip_code]
                for old in sorted(hist)[:-8]:
                    hist.pop(old, None)
            acc = MicroAccumulator(bucket_ts=bucket)
            self._acc[scrip_code] = acc
        prev = self._last.get(scrip_code)
        if prev is not None:
            e = l1_ofi(prev, cur)
            if e is not None:
                acc.ofi_sum += e
                if e > 0:
                    acc.ofi_pos += e
                else:
                    acc.ofi_neg += e
        imb = depth_imbalance(cur, self.levels)
        if imb is not None:
            acc.imbalance_sum += imb
            acc.imbalance_n += 1
        sp = spread_bps(cur)
        if sp is not None:
            acc.spread_sum += sp
            acc.spread_n += 1
        mp = microprice(cur)
        if mp is not None:
            acc.micro_last = mp
        if cur.best_bid and cur.best_ask:
            acc.mid_last = (cur.best_bid[0] + cur.best_ask[0]) / 2
        acc.depth_bid_last = sum(q for _, q in cur.bids[: self.levels])
        acc.depth_ask_last = sum(q for _, q in cur.asks[: self.levels])
        acc.updates += 1
        self._last[scrip_code] = cur

    def for_bar(self, scrip_code: str, bucket_ts: int) -> dict[str, Any] | None:
        """Metrics for a bucket that has closed (or the one in progress)."""
        acc = self._acc.get(scrip_code)
        if acc is not None and acc.bucket_ts == bucket_ts:
            return acc.to_json()
        return self._closed.get(scrip_code, {}).get(bucket_ts)

    def live(self, scrip_code: str) -> dict[str, Any] | None:
        acc = self._acc.get(scrip_code)
        book = self._last.get(scrip_code)
        if acc is None or book is None:
            return None
        return {
            **acc.to_json(),
            "bucket_ts": acc.bucket_ts,
            "book_age_s": round(time.time() - book.ts, 2),
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "levels": {"bids": book.bids[: self.levels], "asks": book.asks[: self.levels]},
        }

    def stats(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "instruments": len(self._last),
            "tape_available": self.tape_available,
            "not_computable_without_tape": ["kyle_lambda", "vpin", "taker_side"],
        }


__all__ = ["BookState", "MicroAggregator", "depth_imbalance", "l1_ofi", "microprice", "spread_bps"]
