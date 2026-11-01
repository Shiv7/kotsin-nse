"""The broker is the source of truth for positions.

Runs on boot and on a timer. Compares ``NetPositionNetWise`` against the local book and reports
three classes of disagreement:

* **ORPHAN** — the broker holds a position we do not know about. Something placed an order we lost
  track of, or a previous process died between placement and persistence.
* **PHANTOM** — we think we hold something the broker does not. Usually an exit that filled while
  we were down.
* **QTY_MISMATCH** — same instrument, different size. A partial exit that we recorded as full, or
  vice versa.

Any mismatch **freezes new entries** until it is resolved or acknowledged. That is the rule the old
stack did not have: CAN2 kept its open positions in an in-memory dict with no persistence and no
re-hydration, so every restart silently orphaned whatever was live, and the consumer's ``open=0``
sat next to an executor that still held the trade.

Reconciliation never places an order by itself. It reports; a human or an explicit kill decides.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from ..domain import Position
from ..venue.base import VenueError
from ..venue.fivepaisa.rest import FivePaisaREST

log = structlog.get_logger(__name__)


class MismatchKind(StrEnum):
    ORPHAN = "ORPHAN"
    PHANTOM = "PHANTOM"
    QTY_MISMATCH = "QTY_MISMATCH"


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: MismatchKind
    scrip_code: str
    symbol: str
    local_qty: int
    venue_qty: int
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "scrip_code": self.scrip_code,
            "symbol": self.symbol,
            "local_qty": self.local_qty,
            "venue_qty": self.venue_qty,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class LocalPosition:
    """What the engine believes it holds, in the only terms the broker's book can be compared with."""

    scrip_code: str
    symbol: str
    qty: int


def _as_local(p: Any) -> LocalPosition | None:
    if isinstance(p, LocalPosition):
        return p if p.qty else None
    inst = getattr(p, "instrument", None)
    if inst is not None:  # domain.Position
        if getattr(p, "status", "OPEN") != "OPEN" or not getattr(p, "qty_remaining", 0):
            return None
        return LocalPosition(str(inst.scrip_code), str(inst.symbol), int(p.qty_remaining))
    return None


@dataclass(slots=True)
class ReconcileReport:
    ts: float = field(default_factory=time.time)
    checked: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    error: str = ""

    @property
    def clean(self) -> bool:
        return not self.mismatches and not self.error

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "checked": self.checked,
            "clean": self.clean,
            "error": self.error,
            "mismatches": [m.to_json() for m in self.mismatches],
        }


class Reconciler:
    def __init__(self, rest: FivePaisaREST) -> None:
        self.rest = rest
        self.last: ReconcileReport = ReconcileReport()
        self.frozen = False
        self.freeze_reason = ""
        self.runs = 0

    async def run(self, positions: list[Position] | list[LocalPosition] | list[Any]) -> ReconcileReport:
        report = ReconcileReport()
        self.runs += 1
        try:
            venue_rows = await self.rest.net_positions()
        except VenueError as exc:
            report.error = str(exc)
            self.last = report
            # A failed reconcile is not a clean one. Fail closed.
            self._freeze(f"reconcile failed: {exc}")
            return report

        venue = {r["scrip_code"]: r for r in venue_rows if r["net_qty"] != 0}
        local: dict[str, LocalPosition] = {}
        for raw in positions:
            lp = _as_local(raw)
            if lp is not None:
                local[lp.scrip_code] = lp
        report.checked = len(venue) + len(local)

        for code, row in venue.items():
            p = local.get(code)
            vq = abs(int(row["net_qty"]))
            if p is None:
                report.mismatches.append(
                    Mismatch(
                        MismatchKind.ORPHAN,
                        code,
                        row.get("symbol", ""),
                        0,
                        vq,
                        "broker holds a position the engine does not know about",
                    )
                )
            elif p.qty != vq:
                report.mismatches.append(
                    Mismatch(
                        MismatchKind.QTY_MISMATCH,
                        code,
                        p.symbol,
                        p.qty,
                        vq,
                    )
                )
        for code, p in local.items():
            if code not in venue:
                report.mismatches.append(
                    Mismatch(
                        MismatchKind.PHANTOM,
                        code,
                        p.symbol,
                        p.qty,
                        0,
                        "engine holds a position the broker does not show",
                    )
                )

        self.last = report
        if report.mismatches:
            self._freeze(f"{len(report.mismatches)} position mismatch(es)")
            log.error("reconcile.mismatch", count=len(report.mismatches),
                      kinds=[m.kind.value for m in report.mismatches])
        else:
            self._thaw()
        return report

    def _freeze(self, reason: str) -> None:
        if not self.frozen:
            log.error("reconcile.freeze", reason=reason)
        self.frozen, self.freeze_reason = True, reason

    def _thaw(self) -> None:
        if self.frozen:
            log.info("reconcile.thaw")
        self.frozen, self.freeze_reason = False, ""

    def acknowledge(self) -> None:
        """Operator override: accept the current state and let entries resume."""
        log.warning("reconcile.acknowledged", previous=self.freeze_reason)
        self._thaw()

    def stats(self) -> dict[str, Any]:
        return {
            "frozen": self.frozen,
            "freeze_reason": self.freeze_reason,
            "runs": self.runs,
            "last": self.last.to_json(),
        }
