"""Market microstructure from the feed we have: price/volume snapshots (~1 s on a lightly loaded
connection) and a 5-level book. What market makers do shows in the book before it shows in the
price; this module measures the parts of that the feed lets us see.

Per instrument, rolling over the last ``WINDOW_S`` (snapshots, not a tape — every figure that
needs a trade's side uses the quote rule on a snapshot's new volume, and says it is an estimate):

**From the book (each depth frame)**

* *Microprice* (a₁·B₁ + b₁·A₁)/(B₁ + A₁) — the mid tilted toward the thinner side; above the mid,
  the next tick is more likely up. *Queue imbalance* (B₁ − A₁)/(B₁ + A₁).
* *Spread* in bps. Glosten–Milgrom: the spread is the market maker's insurance against informed
  flow, so a spread widening with no news is them smelling it (``spread_z``: now vs the window).
* *Liquidity vacuum*: one side's visible size falls by ``VACUUM_DROP`` within ``VACUUM_S`` —
  makers pulling quotes, and price tends to slide through the gap.
* *Walls*: a level ≥ ``WALL_MULT`` × the median level size. Tracked while visible; when it goes,
  it was **filled** if the price traded to it, else **pulled**. A high pull rate is spoofing-like
  (a wall shown to move others, not to trade).
* *Absorption*: the best bid unchanged for ``ABSORB_S`` while the estimated selling into it adds
  up to ``ABSORB_MULT`` × its displayed size — a hidden buyer refilling (an iceberg), the live form
  of an order block. Mirrored for the ask.
* *Book skew*: (bid depth − ask depth)/(total) — Avellaneda–Stoikov makers shade both quotes
  against their inventory (long makers lean the book to the ask). Their inventory itself is not
  observable; the skew is the visible trace.

**From prices and volume (each quote frame)**

* *Kyle's λ*: the OLS slope of Δmid on estimated signed volume across frames, in bps per 10,000
  shares — how far a given net flow moves the price (high λ: fragile, fast breakouts, stops hit).
* *VPIN* (flow toxicity): volume split into buckets of ``VPIN_BUCKET_MIN`` minutes' average volume;
  mean |buy − sell| / bucket over the last ``VPIN_BUCKETS``. High = one-sided, toxic flow; makers
  widen or pull.
* *Efficiency ratio* (Kaufman) of the mid over the window: |net move| / Σ|moves| — under ~0.3 is
  chop. *Realised volatility* (bps, per √minute) of 1-minute mid returns.
* *Roll's spread* 2·√(−cov(Δp_t, Δp_{t−1})) — the bid-ask bounce implied by trade prices (None
  when the covariance is positive: a trending window).
* *Amihud illiquidity*: mean |1-minute return| / ₹ crore traded.

Events (walls pulled/filled, absorption, vacuum) are kept, newest last, with plain-language text.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from .meter import classify

WINDOW_S = 1800.0
WALL_MULT = 3.0
VACUUM_DROP = 0.5
VACUUM_S = 5.0
ABSORB_S = 20.0
ABSORB_MULT = 2.0
VPIN_BUCKET_MIN = 1.0
VPIN_BUCKETS = 30
EVENTS_KEPT = 50

Book = Sequence[tuple[float, int]]


@dataclass(slots=True)
class _Wall:
    side: str  # BID | ASK
    price: float
    size: float
    first_ts: float
    last_ts: float
    peak: float


@dataclass(slots=True)
class _State:
    frames: deque[tuple[float, float, float, float]] = field(default_factory=deque)  # ts, mid, signed vol, ltp
    books: deque[tuple[float, float, float]] = field(default_factory=deque)  # ts, bid depth, ask depth
    spreads: deque[tuple[float, float]] = field(default_factory=deque)  # ts, spread bps
    last_total: int = 0
    bid: float = 0.0
    ask: float = 0.0
    ltp: float = 0.0
    last_side: int = 0
    book_bids: tuple[tuple[float, int], ...] = ()
    book_asks: tuple[tuple[float, int], ...] = ()
    walls: dict[tuple[str, float], _Wall] = field(default_factory=dict)
    pulled: int = 0
    filled: int = 0
    lo_since: float = math.inf  # traded range since the walls were last checked
    hi_since: float = -math.inf
    absorb_bid: tuple[float, float, float] | None = None  # price, since ts, sold into it
    absorb_ask: tuple[float, float, float] | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=EVENTS_KEPT))
    vacuum_at: dict[str, float] = field(default_factory=dict)


def _ols_slope(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) < 30:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / sxx


class MicroMeter:
    """Feed it the same ticks and books as the flow meter; read ``snapshot(code)``."""

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self.window = window_s
        self._st: dict[str, _State] = {}

    def _trim(self, st: _State, now: float) -> None:
        cut = now - self.window
        for dq in (st.frames, st.books, st.spreads):
            while dq and dq[0][0] < cut:
                dq.popleft()

    def _event(self, st: _State, ts: float, kind: str, text: str, **kw: Any) -> None:
        st.events.append({"ts": ts, "kind": kind, "text": text, **kw})

    # -- quotes -------------------------------------------------------------------------------------

    def on_tick(self, code: str, ts: float, ltp: float, total_qty: int, bid: float, ask: float, auction: bool = False) -> None:
        if ltp <= 0:
            return
        st = self._st.setdefault(code, _State())
        new = total_qty - st.last_total if st.last_total and total_qty >= st.last_total else 0
        crossed = bid > 0 and ask > 0 and bid >= ask
        side = 0 if (auction or crossed or new <= 0) else classify(ltp, st.bid, st.ask, st.ltp, st.last_side)
        mid = (bid + ask) / 2 if bid > 0 and ask > bid else ltp
        if not auction and not crossed:
            st.frames.append((ts, mid, side * new, ltp))
            st.lo_since, st.hi_since = min(st.lo_since, ltp), max(st.hi_since, ltp)
            self._absorption(st, ts, side, new)
        st.last_side = side or st.last_side
        if total_qty >= st.last_total:
            st.last_total = total_qty
        st.bid, st.ask, st.ltp = bid, ask, ltp
        self._trim(st, ts)

    def _absorption(self, st: _State, ts: float, side: int, new: int) -> None:
        """Selling into an unmoving best bid (or buying into an unmoving best ask) well beyond the
        size shown there: someone is refilling it."""
        for which, hit, book in (("BID", -1, st.book_bids), ("ASK", 1, st.book_asks)):
            if not book:
                continue
            px, shown = book[0]
            cur = st.absorb_bid if which == "BID" else st.absorb_ask
            if cur is None or cur[0] != px:
                cur = (px, ts, 0.0)
            if side == hit and new > 0:
                cur = (cur[0], cur[1], cur[2] + new)
            if ts - cur[1] >= ABSORB_S and shown > 0 and cur[2] >= ABSORB_MULT * shown:
                self._event(
                    st, ts, "ABSORPTION",
                    f"Absorption at {px:,.2f}: {cur[2]:,.0f} {'sold into' if which == 'BID' else 'bought from'} the "
                    f"{'bid' if which == 'BID' else 'ask'} over {ts - cur[1]:.0f}s, still {shown:,} shown — a hidden "
                    f"{'buyer' if which == 'BID' else 'seller'}",
                    side=which, price=px,
                )
                cur = (px, ts, 0.0)
            if which == "BID":
                st.absorb_bid = cur
            else:
                st.absorb_ask = cur

    # -- books --------------------------------------------------------------------------------------

    def on_depth(self, code: str, ts: float, bids: Book, asks: Book) -> None:
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return  # empty, or the auction's crossed book
        st = self._st.setdefault(code, _State())
        bd, ad = float(sum(q for _p, q in bids)), float(sum(q for _p, q in asks))
        mid = (bids[0][0] + asks[0][0]) / 2
        st.books.append((ts, bd, ad))
        st.spreads.append((ts, (asks[0][0] - bids[0][0]) / mid * 1e4))
        self._vacuum(st, ts, bd, ad)
        self._walls(st, ts, bids, asks)
        st.book_bids, st.book_asks = tuple(bids), tuple(asks)
        self._trim(st, ts)

    def _vacuum(self, st: _State, ts: float, bd: float, ad: float) -> None:
        recent = [b for b in st.books if ts - b[0] <= VACUUM_S]
        if len(recent) < 2:
            return
        for name, i, now_d in (("bid", 1, bd), ("ask", 2, ad)):
            peak = max(b[i] for b in recent)
            if peak > 0 and now_d <= (1 - VACUUM_DROP) * peak and ts - st.vacuum_at.get(name, 0.0) > 60:
                st.vacuum_at[name] = ts
                self._event(
                    st, ts, "VACUUM",
                    f"Liquidity vacuum on the {name}: visible size {peak:,.0f} → {now_d:,.0f} in {VACUUM_S:.0f}s — "
                    f"makers pulling, price can slide {'down' if name == 'bid' else 'up'} through it",
                    side=name.upper(),
                )

    def _walls(self, st: _State, ts: float, bids: Book, asks: Book) -> None:
        sizes = [q for _p, q in (*bids, *asks)]
        med = statistics.median(sizes) if sizes else 0
        seen: set[tuple[str, float]] = set()
        if med > 0:
            for side, book in (("BID", bids), ("ASK", asks)):
                for p, q in book:
                    if q >= WALL_MULT * med:
                        k = (side, p)
                        seen.add(k)
                        w = st.walls.get(k)
                        if w is None:
                            st.walls[k] = _Wall(side, p, q, ts, ts, q)
                        else:
                            w.size, w.last_ts, w.peak = q, ts, max(w.peak, q)
        lo, hi = st.lo_since, st.hi_since
        for k in [k for k in st.walls if k not in seen]:
            w = st.walls.pop(k)
            in_view = (bids[-1][0] <= w.price) if w.side == "BID" else (asks[-1][0] >= w.price)
            if not in_view:
                continue  # it scrolled out of the 5 visible levels — neither filled nor pulled
            traded = (lo <= w.price) if w.side == "BID" else (hi >= w.price)
            if traded:
                st.filled += 1
                self._event(st, ts, "WALL_FILLED", f"{w.side.lower()} wall {w.peak:,.0f} at {w.price:,.2f} traded through after {ts - w.first_ts:.0f}s", side=w.side, price=w.price)
            elif ts - w.first_ts >= 5:
                st.pulled += 1
                self._event(st, ts, "WALL_PULLED", f"{w.side.lower()} wall {w.peak:,.0f} at {w.price:,.2f} pulled before price reached it ({ts - w.first_ts:.0f}s shown)", side=w.side, price=w.price)
        st.lo_since, st.hi_since = (st.ltp or math.inf), (st.ltp or -math.inf)

    # -- read ---------------------------------------------------------------------------------------

    def snapshot(self, code: str) -> dict[str, Any] | None:
        st = self._st.get(code)
        if st is None:
            return None
        out: dict[str, Any] = {"frames": len(st.frames), "window_s": self.window}
        b, a = st.book_bids, st.book_asks
        if b and a:
            (b1, qb), (a1, qa) = b[0], a[0]
            mid = (a1 + b1) / 2
            micro = (a1 * qb + b1 * qa) / (qb + qa) if qb + qa else mid
            bd, ad = sum(q for _p, q in b), sum(q for _p, q in a)
            out.update(
                mid=round(mid, 4), microprice=round(micro, 4), micro_bps=round((micro - mid) / mid * 1e4, 3),
                queue_imbalance=round((qb - qa) / (qb + qa), 4) if qb + qa else None,
                spread_bps=round((a1 - b1) / mid * 1e4, 3), book_skew=round((bd - ad) / (bd + ad), 4) if bd + ad else None,
                bid_depth=bd, ask_depth=ad, levels=min(len(b), len(a)),
                walls=[{"side": w.side, "price": w.price, "size": w.size, "age_s": round(st.books[-1][0] - w.first_ts, 1)}
                       for w in st.walls.values()] if st.books else [],
            )
        sp = [s for _t, s in st.spreads]
        if len(sp) > 10 and statistics.pstdev(sp) > 0:
            out["spread_z"] = round((sp[-1] - statistics.fmean(sp)) / statistics.pstdev(sp), 2)
        f = list(st.frames)
        if len(f) > 31:
            dm = [(f[i][1] - f[i - 1][1]) / f[i - 1][1] * 1e4 for i in range(1, len(f))]
            sv = [f[i][2] / 1e4 for i in range(1, len(f))]
            lam = _ols_slope(sv, dm)
            out["kyle_lambda_bps_per_10k"] = round(lam, 4) if lam is not None else None
        mids = self._minute_mids(f)
        if len(mids) >= 3:
            moves = [abs(mids[i] - mids[i - 1]) for i in range(1, len(mids))]
            out["efficiency_ratio"] = round(abs(mids[-1] - mids[0]) / sum(moves), 3) if sum(moves) else None
            rets = [(mids[i] / mids[i - 1] - 1) * 1e4 for i in range(1, len(mids))]
            out["realised_vol_bps_min"] = round(statistics.pstdev(rets), 3) if len(rets) > 1 else None
        px = [x[3] for x in f]
        if len(px) > 20:
            d = [px[i] - px[i - 1] for i in range(1, len(px))]
            cov = statistics.covariance(d[1:], d[:-1]) if len(d) > 2 else 0.0
            out["roll_spread"] = round(2 * math.sqrt(-cov), 4) if cov < 0 else None
        out.update(self._vpin(f))
        out.update(self._amihud(f))
        out["walls_pulled"], out["walls_filled"] = st.pulled, st.filled
        out["events"] = list(st.events)[-15:]
        return out

    @staticmethod
    def _minute_mids(f: list[tuple[float, float, float, float]]) -> list[float]:
        by: dict[int, float] = {}
        for ts, mid, _v, _p in f:
            by[int(ts // 60)] = mid
        return [by[k] for k in sorted(by)]

    @staticmethod
    def _vpin(f: list[tuple[float, float, float, float]]) -> dict[str, Any]:
        """Volume-synchronised: buckets of a fixed volume (``VPIN_BUCKET_MIN`` minutes' average)."""
        if len(f) < 30:
            return {}
        span_min = max(1.0, (f[-1][0] - f[0][0]) / 60)
        total = sum(abs(v) for _t, _m, v, _p in f)
        bucket = total / span_min * VPIN_BUCKET_MIN
        if bucket <= 0:
            return {}
        imb: list[float] = []
        buy = sell = 0.0
        for _t, _m, v, _p in f:
            buy += max(v, 0.0)
            sell += max(-v, 0.0)
            if buy + sell >= bucket:
                imb.append(abs(buy - sell) / (buy + sell))
                buy = sell = 0.0
        tail = imb[-VPIN_BUCKETS:]
        return {"vpin": round(statistics.fmean(tail), 3), "vpin_buckets": len(tail)} if tail else {}

    @staticmethod
    def _amihud(f: list[tuple[float, float, float, float]]) -> dict[str, Any]:
        by: dict[int, list[tuple[float, float, float]]] = {}
        for ts, mid, v, p in f:
            by.setdefault(int(ts // 60), []).append((mid, abs(v), p))
        mins = sorted(by)
        vals = []
        for a, b in pairwise(mins):
            r = abs(by[b][-1][0] / by[a][-1][0] - 1)
            cr = sum(v * p for _m, v, p in by[b]) / 1e7
            if cr > 0:
                vals.append(r / cr)
        return {"amihud": round(statistics.fmean(vals) * 1e4, 4)} if vals else {}


__all__ = ["MicroMeter"]
