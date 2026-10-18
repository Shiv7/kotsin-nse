"""Per-minute order flow, estimated from quote snapshots.

5paisa (and every retail NSE source) sends snapshots, not one message per trade, and never says
which side initiated a trade. So each snapshot's NEW volume (the rise in the cumulative
``total_qty``) is classified as a whole:

* at or above the previous snapshot's ask → buyer-initiated;
* at or below the previous snapshot's bid → seller-initiated;
* in between → by the tick rule (price up = buy, down = sell, unchanged = as the last one).

This is the quote rule applied to batches. On a ~6-frames-a-minute feed many trades share one
frame, so it is an ESTIMATE of delta, labelled as one everywhere it is shown; a 1-second vendor feed
through the same code would sharpen it.

Per minute and instrument: buy volume, sell volume, delta, the day's cumulative delta, frames,
the average top-5 book imbalance (bid qty − ask qty) / (bid + ask) from depth snapshots, and the
last OI seen (futures). Pure state; the engine feeds it, the archive keeps it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any


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

    @property
    def delta(self) -> float:
        return self.buy - self.sell

    @property
    def imbalance(self) -> float | None:
        return self.imb_sum / self.imb_n if self.imb_n else None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("imb_sum")
        d.pop("imb_n")
        d["delta"] = self.delta
        d["imbalance"] = round(self.imbalance, 4) if self.imbalance is not None else None
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
            st.day, st.cum, st.minutes, st.last_total = day, 0.0, [], 0
        if not st.minutes or st.minutes[-1].ts != m:
            if st.minutes:
                self.closed.append(st.minutes[-1])
            st.minutes.append(FlowMinute(code, m, cum_delta=st.cum))
            if len(st.minutes) > self.keep:
                del st.minutes[0]
        return st.minutes[-1]

    def on_tick(self, code: str, ts: float, ltp: float, total_qty: int, bid: float, ask: float) -> None:
        if ltp <= 0:
            return
        st = self._st.setdefault(code, _State())
        fm = self._minute(code, ts)
        fm.frames += 1
        new = total_qty - st.last_total if st.last_total else 0
        if new > 0:
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
        imb = imbalance(bids, asks)
        if imb is None:
            return
        fm = self._minute(code, ts)
        fm.imb_sum += imb
        fm.imb_n += 1

    def on_oi(self, code: str, ts: float, oi: float) -> None:
        self._minute(code, ts).oi = oi

    def minutes(self, code: str) -> list[FlowMinute]:
        st = self._st.get(code)
        return list(st.minutes) if st else []

    def drain(self) -> list[FlowMinute]:
        out, self.closed = self.closed, []
        return out

    def stats(self) -> dict[str, Any]:
        return {"instruments": len(self._st), "pending_closed": len(self.closed)}
