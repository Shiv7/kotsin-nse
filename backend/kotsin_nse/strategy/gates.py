"""Gates with an explicit policy for missing data, and a per-gate rejection counter.

Two rules, each paid for in production:

* **A gate must declare what a missing input means.** The old family had no house rule: CAN2 traded
  anyway when OI was unavailable while FUDKOI never fired, on the same missing input, and neither
  raised anything. During a 23-day OI outage one book was silently dark and the other was taking
  unconfirmed signals. Here every gate names ``FAIL_OPEN`` or ``FAIL_CLOSED`` at construction and
  flags ``missing=True`` so it can be counted.
* **Any conjunction of gates must count which one is binding.** ``NSE_BB_30`` had six mandatory
  gates and produced **two** signals in its lifetime; nothing recorded which gate did the killing,
  so a strangled strategy was indistinguishable from a selective one. :class:`GateStats` counts
  every rejection by name and the System page shows it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class OnMissing(StrEnum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    required: bool
    value: float | None
    threshold: float | None
    missing: bool
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "required": self.required,
            "value": self.value,
            "threshold": self.threshold,
            "missing": self.missing,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    on_missing: OnMissing
    required: bool = True

    def evaluate(
        self,
        value: float | None,
        predicate: Callable[[float], bool],
        threshold: float | None = None,
        note: str = "",
    ) -> GateResult:
        if value is None:
            return GateResult(
                name=self.name,
                passed=self.on_missing is OnMissing.FAIL_OPEN,
                required=self.required,
                value=None,
                threshold=threshold,
                missing=True,
                note=f"input missing → {self.on_missing.value}",
            )
        return GateResult(
            name=self.name,
            passed=bool(predicate(value)),
            required=self.required,
            value=value,
            threshold=threshold,
            missing=False,
            note=note,
        )

    def verdict(self, passed: bool, *, value: float | None = None, note: str = "") -> GateResult:
        """For a gate whose input is not a single number (a time window, a state machine)."""
        return GateResult(self.name, passed, self.required, value, None, False, note)


def chain_passed(results: Iterable[GateResult]) -> bool:
    return all(r.passed or not r.required for r in results)


def failed_gates(results: Iterable[GateResult]) -> list[str]:
    return [r.name for r in results if r.required and not r.passed]


def binding_gate(results: Iterable[GateResult]) -> str | None:
    """The first required gate that failed — the answer to "why didn't this fire?"."""
    failures = failed_gates(results)
    return failures[0] if failures else None


@dataclass(slots=True)
class GateStats:
    """Per-gate rejection counts, per strategy. Cheap, and it turns "the book is quiet" into a
    number you can act on."""

    evaluated: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    missing: Counter[str] = field(default_factory=Counter)
    binding: Counter[str] = field(default_factory=Counter)
    candidates: int = 0
    passed: int = 0

    def record(self, results: Iterable[GateResult]) -> None:
        rows = list(results)
        self.candidates += 1
        for r in rows:
            self.evaluated[r.name] += 1
            if r.missing:
                self.missing[r.name] += 1
            if r.required and not r.passed:
                self.rejected[r.name] += 1
        who = binding_gate(rows)
        if who is None:
            self.passed += 1
        else:
            self.binding[who] += 1

    def to_json(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "passed": self.passed,
            "by_gate": {
                name: {
                    "evaluated": self.evaluated[name],
                    "rejected": self.rejected[name],
                    "missing": self.missing[name],
                    "binding": self.binding[name],
                }
                for name in sorted(self.evaluated)
            },
        }
