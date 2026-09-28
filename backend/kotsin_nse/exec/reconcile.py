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
from collections.abc import Callable
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
        #: the broker's net quantity per contract at the last read — None when there is none, or the last
        #: read FAILED (never an older one standing in for it) — and when that read was taken (review14c)
        self.last_venue: dict[str, int] | None = None
        self.last_venue_ts = 0.0

    async def run(
        self, positions: list[Position] | Callable[[], list[Position]], *, at_venue: bool = True,
        working: dict[str, tuple[int, str]] | Callable[[], dict[str, tuple[int, str]]] | None = None,
    ) -> ReconcileReport:
        """Compare the local book against the broker's.

        ``at_venue`` is False in SHADOW and PAPER, where the engine's positions deliberately do not
        exist at the broker. Comparing them anyway makes every open paper position a PHANTOM, which
        freezes entries — so a paper session would run clean until its first fill and then halt
        itself for the rest of the day. Found 2026-09-22 with one paper position open.

        The venue side is still read in those modes, because an ORPHAN means something regardless:
        a real position sitting at the broker that this engine knows nothing about is dangerous
        whether or not the engine is paper-trading.

        ``working``: per contract, what the broker has already filled of the engine's live orders
        that are still working and not yet booked on a position — ``(signed qty, symbol)``, a BUY's
        fill up, a SELL's down. The broker's net already shows it; the engine books it when the order
        is over, and without it every partly filled resting target froze the engine (review14 A9).

        ``positions`` and ``working`` may be callables: they are then read at the instant the broker
        has answered, so a fill booked while its call was out is counted once (review14b N7).
        """
        report = ReconcileReport()
        asked = time.time()  # a read is as new as the moment it was ASKED for, not answered
        try:
            venue_rows = await self.rest.net_positions()
        except VenueError as exc:
            report.error = str(exc)
            self.last = report
            self.last_venue, self.last_venue_ts = None, 0.0
            # A failed reconcile is not a clean one. Fail closed.
            self._freeze(f"reconcile failed: {exc}")
            return report

        venue = {r["scrip_code"]: r for r in venue_rows if r["net_qty"] != 0}
        self.last_venue = {code: int(r["net_qty"]) for code, r in venue.items()}
        self.last_venue_ts = asked
        if callable(positions):
            positions = positions()
        if callable(working):
            working = working()
        # One broker account, several books: two books long the same strike are ONE net position at
        # the broker, so the engine's side is the sum per contract (keyed by scrip, the last book's
        # position used to win, and a second book on the same strike read as a QTY_MISMATCH).
        local: dict[str, Any] = {}
        held: dict[str, int] = {}
        names: dict[str, str] = {}
        if at_venue:
            for p in positions:
                if p.status == "OPEN" and p.qty_remaining:
                    code = p.instrument.scrip_code
                    local.setdefault(code, p)
                    names.setdefault(code, p.instrument.symbol)
                    held[code] = held.get(code, 0) + int(p.qty_remaining)
            for code, (qty, symbol) in (working or {}).items():
                if qty:
                    held[code] = held.get(code, 0) + int(qty)
                    names.setdefault(code, symbol)
            for code in [c for c, q in held.items() if q == 0 and c not in local]:
                held.pop(code)
        report.checked = len(venue) + len(held)

        for code, row in venue.items():
            p = local.get(code)
            vq = abs(int(row["net_qty"]))
            if code not in held:
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
            elif held[code] != vq:
                report.mismatches.append(
                    Mismatch(
                        MismatchKind.QTY_MISMATCH,
                        code,
                        p.instrument.symbol if p is not None else names.get(code, ""),
                        held[code],
                        vq,
                    )
                )
        for code, qty in held.items():
            if code not in venue and qty != 0:
                report.mismatches.append(
                    Mismatch(
                        MismatchKind.PHANTOM,
                        code,
                        names.get(code, ""),
                        qty,
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
            "last": self.last.to_json(),
        }
