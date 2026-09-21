"""Structured outputs for the review committee.

Field descriptions are the model's instructions (the TradingAgents pattern, as in kotsin-crypto):
every claim must cite evidence keys from the pack, and a review the pack cannot ground says
``INCONCLUSIVE`` and names the missing key — it never invents a cause.

This committee does not forecast. It reviews **the algo's own decisions after the fact** — one
signal and what the underlying did next, or a whole cohort of trades — and names the failure mode
from a fixed taxonomy so verdicts can be counted across reviews, not just read one at a time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class FailureMode(StrEnum):
    NOISE_STOP = "NOISE_STOP"  # stop inside ordinary bar range; taken out, direction then right
    WRONG_DIRECTION = "WRONG_DIRECTION"  # the move never came
    LATE_ENTRY = "LATE_ENTRY"  # the move was largely done at the trigger and reverted
    TARGET_UNREACHABLE = "TARGET_UNREACHABLE"  # T1 beyond what the hold window or room allowed
    GAVE_BACK = "GAVE_BACK"  # MFE ≥ 1 R, exit ≤ 0: the exit rule, not the entry
    COST_DOMINATED = "COST_DOMINATED"  # charges ate a move that was there
    SESSION_TIMING = "SESSION_TIMING"  # force-flat, entry cut-off or open volatility decided it
    INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"  # the underlying worked, the option leg did not
    DATA_QUALITY = "DATA_QUALITY"  # partial bar, stale quote, phantom print
    VALID_LOSS = "VALID_LOSS"  # sound construction, right process, lost anyway
    GOOD_TRADE = "GOOD_TRADE"
    INCONCLUSIVE = "INCONCLUSIVE"


FAILURE_MODES = ", ".join(m.value for m in FailureMode)

CaseRole = Literal["entry", "levels", "execution"]


class CaseAnalystReport(BaseModel):
    role: CaseRole = Field(description="Which analyst produced this report.")
    summary: str = Field(
        description="Two or three sentences on what the evidence for this role says about the outcome."
    )
    findings: list[str] = Field(
        description="Specific observations, each ending with the evidence keys it rests on in square brackets, e.g. [lvl.stop_dist_atr, path.bars_to_stop]."
    )
    evidence_keys: list[str] = Field(
        description="Every evidence key referenced above, exactly as written in the pack."
    )
    severity: float = Field(
        ge=0,
        le=1,
        description="How much of the outcome this role's factors explain: 0 = none, 1 = entirely.",
    )


class CaseDebate(BaseModel):
    construction_case: str = Field(
        description="The strongest case that the trade's CONSTRUCTION failed (stop, target, instrument, exit rule, costs), from cited evidence only."
    )
    thesis_case: str = Field(
        description="The strongest case that the THESIS failed (direction or timing) — or that the loss was a valid outcome of a sound trade."
    )
    unrebutted_construction_point: str = Field(
        description="The construction point the other side could not rebut, with its keys."
    )
    unrebutted_thesis_point: str = Field(
        description="The thesis point the other side could not rebut, with its keys."
    )
    unresolved: list[str] = Field(
        description="Questions the pack cannot answer, each naming the key that would answer it."
    )


class ParamChange(BaseModel):
    path: str = Field(
        description="Dotted path into BacktestParams exactly as the fields are named: fudkii.<field>, fudkii.grade_policy.<field>, fukaa.<field>, limits.<field>, slippage_bps, position_budget_inr. Never invent a field."
    )
    value: float = Field(
        description="The new value. Booleans as 1 or 0; integers as whole numbers."
    )


class Hypothesis(BaseModel):
    title: str = Field(description="One line naming the change and the effect expected.")
    rationale: str = Field(description="Why this change follows from the evidence, citing keys.")
    changes: list[ParamChange] = Field(
        description="One to three parameter changes that together form ONE testable hypothesis."
    )
    expected: str = Field(
        description="What a backtest of the change must show for the hypothesis to hold, as a number: e.g. 'avg R of the <0.25% stop bucket rises above −1 and overall avg R improves'."
    )


class PostMortem(BaseModel):
    failure_mode: FailureMode = Field(
        description=f"The primary failure mode, one of: {FAILURE_MODES}."
    )
    secondary: list[FailureMode] = Field(
        description="Contributing modes, if any, most important first."
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="How decisively the evidence supports the primary mode; below 0.5 prefer INCONCLUSIVE.",
    )
    what_happened: str = Field(
        description="The sequence, with numbers: trigger, levels, path, exit."
    )
    why: str = Field(description="The causal reading, citing keys.")
    counterfactual: str = Field(
        description="What the same path would have paid with a different construction, using path.* numbers (e.g. 'a stop at 0.5 ATR survives bar 1; T1 was touched on bar 6 for +2.1 R')."
    )
    evidence_keys: list[str] = Field(description="Every key the verdict rests on.")
    hypothesis: Hypothesis | None = Field(
        description="ONE testable parameter change this case argues for, or null when the case argues for none."
    )
    lesson: str = Field(
        description="One sentence, stored verbatim and re-read by future reviews."
    )


class CohortFinding(BaseModel):
    title: str = Field(description="One line naming the pattern.")
    failure_mode: FailureMode = Field(
        description=f"The mode this pattern belongs to, one of: {FAILURE_MODES}."
    )
    magnitude: str = Field(
        description="The numbers: n, avg R with its t, share of the cohort's losses, citing the by_*.<bucket>.* keys."
    )
    evidence_keys: list[str]
    confidence: float = Field(
        ge=0,
        le=1,
        description="Low when the bucket is small (too_small=true) or the t-statistic is weak.",
    )


class CohortReport(BaseModel):
    verdict: str = Field(
        description="Two or three sentences: does this cohort have an edge, and what dominates its result."
    )
    findings: list[CohortFinding] = Field(
        description="Ranked by how much of the result they explain."
    )
    hypotheses: list[Hypothesis] = Field(
        description="At most three, ranked, each testable by ONE backtest with the listed parameter changes."
    )
    not_explained: list[str] = Field(
        description="Parts of the result the tables cannot attribute, naming what would be needed."
    )
    evidence_keys: list[str]


class Reflection(BaseModel):
    lesson: str = Field(
        description="Two to four plain sentences: what the experiment says about the hypothesis, which reading it supports or undercuts, and one concrete lesson for the next review."
    )
    hypothesis_supported: bool = Field(
        description="Whether the experiment supported the hypothesis."
    )
