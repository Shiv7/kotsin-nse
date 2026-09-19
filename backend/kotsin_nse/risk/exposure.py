"""Exposure aggregated by **underlying**, not by position.

FUDKII and FUKAA are separate books with separate wallets and they co-trade deliberately (the
cross-strategy scrip dedup was excised on 2026-06-24). That is fine — but one SuperTrend flip on
RELIANCE can therefore open a FUDKII position and a FUKAA position in the *same* option, and
neither book's own sizing can see the other. The old stack had exactly this and the aggregate was
not visible anywhere.

So exposure is measured here, across every strategy, bucketed by the underlying symbol, and the
gateway consults it before an entry.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..domain import Position
from .limits import RiskLimits


@dataclass(frozen=True, slots=True)
class ExposureVerdict:
    allowed: bool
    reason: str = ""
    underlying_pct: float = 0.0
    open_in_underlying: int = 0


class ExposureBook:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def by_underlying(self, positions: list[Position]) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for p in positions:
            if p.status != "OPEN":
                continue
            out[p.underlying.symbol] += p.entry * p.qty_remaining * p.instrument.multiplier
        return dict(out)

    def check(
        self,
        *,
        underlying: str,
        outlay: float,
        positions: list[Position],
        total_capital: float,
    ) -> ExposureVerdict:
        lim = self.limits
        live = [p for p in positions if p.status == "OPEN"]
        if len(live) >= lim.max_positions_total:
            return ExposureVerdict(False, f"{len(live)} open ≥ cap {lim.max_positions_total}")
        same = [p for p in live if p.underlying.symbol == underlying]
        if len(same) >= lim.max_positions_per_underlying:
            return ExposureVerdict(
                False,
                f"{len(same)} already open in {underlying} ≥ cap {lim.max_positions_per_underlying}",
                open_in_underlying=len(same),
            )
        current = self.by_underlying(live).get(underlying, 0.0)
        pct = (current + outlay) / total_capital * 100 if total_capital > 0 else 0.0
        if pct > lim.max_underlying_exposure_pct:
            return ExposureVerdict(
                False,
                f"{underlying} exposure would be {pct:.1f}% > cap {lim.max_underlying_exposure_pct}%",
                underlying_pct=pct,
                open_in_underlying=len(same),
            )
        return ExposureVerdict(True, "ok", underlying_pct=pct, open_in_underlying=len(same))

    def snapshot(self, positions: list[Position], total_capital: float) -> dict[str, Any]:
        buckets = self.by_underlying(positions)
        return {
            "total_capital": round(total_capital, 2),
            "gross": round(sum(buckets.values()), 2),
            "gross_pct": round(sum(buckets.values()) / total_capital * 100, 2)
            if total_capital
            else 0.0,
            "by_underlying": {
                k: {
                    "outlay": round(v, 2),
                    "pct": round(v / total_capital * 100, 2) if total_capital else 0.0,
                }
                for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
            },
        }
