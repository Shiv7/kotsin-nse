"""The strategy contract.

A strategy is a **pure function of bars and its own state**: no I/O, no wall clock, no broker, no
orders. ``import-linter`` enforces it (``strategy`` may not import ``venue``, ``exec``, ``ledger``,
``api``, ``ops``, ``feed`` or ``instrument``), which is what makes the backtester able to replay the
*live* code rather than a hand-rolled copy — the old hand-rolled replays erred between −80% and
+185%.

There are two shapes, because the FUDKII family has two:

* :class:`Strategy` — reads bars, emits signals. FUDKII.
* :class:`DerivedStrategy` — reads a *base* signal and either re-emits it under its own key or
  drops it. FUKAA. The old code published FUKAA from inside the FUDKII trigger; modelling that
  relationship explicitly is what stops the two from drifting apart, and it means the BB/SuperTrend
  maths exists once rather than twice.

A signal is emitted on the **underlying**. Which option is bought is a separate concern
(``instrument.select``), because it depends on a live chain the strategy must not see.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..bars.pivots import Zone
from ..bars.unified import UnifiedBar
from ..domain import Direction
from .gates import GateResult
from .keys import StrategyKey


@dataclass(frozen=True, slots=True)
class Signal:
    """A decision on the underlying, with everything needed to audit it later.

    ``evidence`` is not decoration: every key here must be read by a gate, a sizer or the strategy
    doc. The old family computed several scores on the hot path, stamped them into the payload and
    then never used them for anything (``effectiveKoi`` shipped "shadow mode: NOT used for ranking
    yet" and stayed that way).
    """

    strategy: StrategyKey
    symbol: str
    direction: Direction
    ts: int  # decision bar start, epoch seconds
    entry: float  # underlying price at the decision
    stop: float  # on the underlying
    targets: tuple[float, ...] = ()
    grade: str = ""
    rr: float = 0.0
    score: float = 0.0
    confidence: float = 1.0
    reason: str = ""
    gates: tuple[GateResult, ...] = ()
    evidence: Mapping[str, float] = field(default_factory=dict)
    source_signal_id: str = ""  # set on a derived signal

    @property
    def signal_id(self) -> str:
        return f"{self.strategy.value}-{self.symbol}-{self.ts}-{self.direction.value[0]}"[:38]

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    def to_json(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "strategy": self.strategy.value,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "ts": self.ts,
            "entry": self.entry,
            "stop": self.stop,
            "targets": list(self.targets),
            "grade": self.grade,
            "rr": self.rr,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
            "gates": [g.to_json() for g in self.gates],
            "evidence": dict(self.evidence),
            "source_signal_id": self.source_signal_id,
        }


@dataclass(frozen=True, slots=True)
class Rejection:
    """A candidate that did not become a signal.

    Recorded with the same weight as a signal. "What did the filter reject, and would it have won?"
    was unanswerable for most of the old stack — only FUKAA kept an audit of passes *and* fails, and
    that is the only reason its MCX multiplier bug was provable in one query.
    """

    strategy: StrategyKey
    symbol: str
    ts: int
    direction: Direction | None
    binding_gate: str
    gates: tuple[GateResult, ...]
    evidence: Mapping[str, float] = field(default_factory=dict)
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "symbol": self.symbol,
            "ts": self.ts,
            "direction": self.direction.value if self.direction else None,
            "binding_gate": self.binding_gate,
            "gates": [g.to_json() for g in self.gates],
            "evidence": dict(self.evidence),
            "note": self.note,
        }


@dataclass(slots=True)
class Outcome:
    """What one evaluation produced. Both halves are persisted."""

    signals: list[Signal] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    def extend(self, other: Outcome) -> None:
        self.signals.extend(other.signals)
        self.rejections.extend(other.rejections)


class Context(Protocol):
    """Everything a strategy may read. Deliberately small."""

    def bars(self, symbol: str, tf: str, n: int) -> Sequence[UnifiedBar]: ...

    def zones(self, symbol: str) -> list[Zone]:
        """Multi-timeframe pivot confluence zones for the underlying."""
        ...

    def session_phase(self, symbol: str, ts: int) -> str:
        """``OPEN`` | ``MID`` | ``EOD`` for the bar starting at ``ts``.

        The strategy needs to know it is on the last bar of the session without knowing that the
        session is IST, or that MCX closes at 23:30 while NSE closes at 15:30. ``market.session``
        is the only module that knows either.
        """
        ...

    def exchange(self, symbol: str) -> str:
        """``N`` (NSE), ``M`` (MCX) or ``C`` (currency). Thresholds differ per exchange and the
        strategy must be able to pick the right one without knowing what a segment is."""
        ...

    @property
    def state(self) -> MutableMapping[str, Any]:
        """Per-strategy scratch that survives across bars — and, unlike the old in-memory position
        dicts, is snapshotted by the engine so a restart does not lose it."""
        ...


class Strategy(Protocol):
    key: StrategyKey
    timeframes: tuple[str, ...]

    def on_bar(self, ctx: Context, bar: UnifiedBar) -> Outcome: ...


class DerivedStrategy(Protocol):
    key: StrategyKey
    source: StrategyKey

    def on_signal(self, ctx: Context, bar: UnifiedBar, base: Signal) -> Outcome: ...
