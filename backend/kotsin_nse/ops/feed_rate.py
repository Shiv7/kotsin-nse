"""How fast 5paisa's price feed really is, per contract, and whether it delivers every trade.

The only measurement on record (2026-09-21) was MCX: ~6.5 ``MarketFeedV3`` frames a minute per
symbol, about one every 9 s. Nobody had measured NSE (operator, 2026-10-03: "seriously 9 seconds? …
I see the real-time websocket"). Two questions, answered from the frames themselves:

* **How often does a contract's frame arrive?** frames per minute and the median gap between them.
* **Does every trade arrive as its own frame?** Each frame carries ``LastQty`` (the size of the
  last trade) and ``TotalQty`` (the day's volume so far). If every trade came through, the sizes
  summed frame by frame equal the growth of the day's volume: ``Σ LastQty == ΔTotalQty``. A feed
  that sends the latest state instead of every trade (conflation) shows less — the share it shows is
  ``volume_seen_pct``, and 100 % means a tick-by-tick feed.

Pure counters fed from the tick path; reset each IST day by the caller.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(slots=True)
class _Code:
    segment: str
    frames: int = 0
    first_recv: float = 0.0
    last_recv: float = 0.0
    first_total: int | None = None
    last_total: int = 0
    #: Σ LastQty over the frames AFTER the first (the first one's trade happened before we looked)
    qty_seen: int = 0
    #: frames whose TotalQty grew — a frame that repeats the same state carries no new trade
    trade_frames: int = 0
    gaps: list[float] | None = None


class FeedRate:
    def __init__(self, keep_gaps: int = 400) -> None:
        self.keep_gaps = keep_gaps
        self._codes: dict[str, _Code] = {}

    def reset(self) -> None:
        self._codes.clear()

    def on_tick(self, code: str, segment: str, recv_ts: float, last_qty: int, total_qty: int) -> None:
        c = self._codes.get(code)
        if c is None:
            c = self._codes[code] = _Code(segment=segment, gaps=[])
        if c.frames:
            gap = recv_ts - c.last_recv
            if c.gaps is not None and len(c.gaps) < self.keep_gaps and gap >= 0:
                c.gaps.append(gap)
        else:
            c.first_recv = recv_ts
        c.frames += 1
        c.last_recv = recv_ts
        if total_qty > 0:
            if c.first_total is None:
                c.first_total = total_qty
            elif total_qty > c.last_total:
                c.trade_frames += 1
                c.qty_seen += max(0, int(last_qty))
            c.last_total = max(c.last_total, total_qty)

    def snapshot(self, *, min_frames: int = 20) -> dict[str, object]:
        """Per segment: how many contracts, their median frames a minute and gap, and the share of
        the traded volume the frames' own trade sizes account for."""
        by: dict[str, list[_Code]] = {}
        for c in self._codes.values():
            if c.frames >= min_frames and c.last_recv > c.first_recv:
                by.setdefault(c.segment, []).append(c)
        out: dict[str, object] = {}
        for seg, codes in sorted(by.items()):
            per_min = [c.frames / ((c.last_recv - c.first_recv) / 60.0) for c in codes]
            gaps = [statistics.median(c.gaps) for c in codes if c.gaps]
            grew = [(c.qty_seen, c.last_total - (c.first_total or c.last_total)) for c in codes]
            seen, traded = sum(s for s, _ in grew), sum(t for _, t in grew if t > 0)
            per_code_pct = [s / t * 100.0 for s, t in grew if t > 0]
            out[seg] = {
                "contracts": len(codes),
                "frames_per_min_median": round(statistics.median(per_min), 1),
                "frames_per_min_p10": round(_pct(per_min, 10), 1),
                "frames_per_min_p90": round(_pct(per_min, 90), 1),
                "gap_s_median": round(statistics.median(gaps), 2) if gaps else None,
                "volume_seen_pct": round(seen / traded * 100.0, 1) if traded else None,
                "volume_seen_pct_median": round(statistics.median(per_code_pct), 1) if per_code_pct else None,
                "verdict": _verdict(seen, traded),
            }
        return out

    def code(self, code: str) -> dict[str, object] | None:
        c = self._codes.get(code)
        if c is None:
            return None
        span = c.last_recv - c.first_recv
        traded = c.last_total - (c.first_total or c.last_total)
        return {"segment": c.segment, "frames": c.frames,
                "frames_per_min": round(c.frames / (span / 60.0), 1) if span > 0 else None,
                "gap_s_median": round(statistics.median(c.gaps), 2) if c.gaps else None,
                "volume_traded": traded, "volume_seen_by_last_qty": c.qty_seen,
                "volume_seen_pct": round(c.qty_seen / traded * 100.0, 1) if traded > 0 else None}


def _pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    k = max(0, min(len(s) - 1, round(p / 100.0 * (len(s) - 1))))
    return s[k]


def _verdict(seen: int, traded: int) -> str:
    if traded <= 0:
        return "no trading seen yet"
    pct = seen / traded * 100.0
    if pct >= 97.0:
        return "tick-by-tick: every trade arrives as its own frame"
    return f"conflated: the frames show {pct:.0f}% of the traded volume as individual trades"


__all__ = ["FeedRate"]
