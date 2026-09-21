"""The committee, TradingAgents-shaped and cost-bounded.

Case review: three analysts in parallel (entry / levels / execution) → one adversarial debate
(construction failed vs thesis failed) → one post-mortem. Five structured calls.
Cohort review: the same three analysts read the forensic tables → one report with ranked findings
and at most three testable hypotheses. Four calls.

Every role sees the same evidence and must cite its keys. The system prompt carries what is
already known about the strategies so the model does not rediscover it at ₹ per token.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .llm import LLM
from .schemas import (
    CaseAnalystReport,
    CaseDebate,
    CohortReport,
    PostMortem,
    Reflection,
)

SYSTEM_CORE = """You are one role on a post-trade review committee for a personal account trading NSE stock options and MCX futures through 5paisa. Positions open on 30-minute bars and close within the session (force-flat 15:20 IST on NSE, 23:20 on MCX). A round trip costs about 0.3% at ₹33,000; the flat ₹40 per order dominates.

The strategies under review:
- FUDKII: on a 30m bar, SuperTrend(7,3) flips direction AND the close is outside Bollinger(20,2). Entry at that close on the underlying; stop = the nearest pivot-confluence zone behind the price (classic pivots on 1d/1wk/1mo, clustered ±0.25%); targets = the walls ahead; grade A/B/C from reward:risk (A ≥ 2.5, B ≥ 1.8, C ≥ 1.2; below 1.0 is F and never published). Expressed as an OTM option with an estimated delta, or the MCX front future.
- FUKAA: the same trigger, admitted only when the bar's volume is a multiple of its baseline and a conviction composite is ≥ 60.
Already known from a one-year backtest (481 trades): average −1.40 R, t = −7.96, 17% wins; grade A is the worst grade; the modal loss is a stop hit within one bar.

Rules for every role:
- Reason ONLY from the evidence pack. It is complete for this review; there is no news and no other data. If something you need is missing, say so and name the key — never invent it.
- Cite the evidence keys you rely on, exactly as written (e.g. lvl.stop_dist_atr, path.bars_to_stop, by_stop_pct.<0.25.avg_r). A claim without a key is worth nothing.
- Quantify. "stop 0.09% from entry = 0.18 ATR, hit on bar 1 while the 16-bar MFE was +6.1 R" beats "the stop was tight".
- Separate the THESIS (direction, timing) from the CONSTRUCTION (stop, target, instrument, costs, exit rule). A losing trade can be a right thesis with a broken construction, or a sound construction with a wrong thesis.
- When the evidence cannot decide, the answer is INCONCLUSIVE with the missing key named — not a plausible story.
- Keep every text field short; the reader is another role or an engineer, not a client.
"""

CASE_ROLES = {
    "entry": "You are the ENTRY analyst: was the trigger timely? Read trig.* (how far the close sat outside the band, bars in trend, ATR%), path.* (what the underlying did over the following bars) and case.session_phase. Distinguish a LATE entry (the move was extended at the trigger and reverted) from a WRONG direction (it never went) from a good entry.",
    "levels": "You are the LEVELS analyst: was the stop a structural level or noise, and were the targets reachable? Read lvl.* (stop distance in % and ATR, the zone it came from and its strength, T1 distance, RR, fortress, room), the zones table and path.* (bars to stop / to T1, MFE and MAE in R, max adverse before T1). State the stop distance in R and ATR that would have survived the path, and whether T1 was ever within reach.",
    "execution": "You are the EXECUTION analyst: the instrument and the exit rule. Read out.* (premium entry/exit, charges share, exit reason, bars held, realised R against MFE), path.close_r_* and book.mode. Say whether the result came from the instrument (delta, spread, theta over the hold), from costs, or from the exit rule giving back what the path offered.",
}

COHORT_ROLES = {
    "entry": "You are the ENTRY analyst for a cohort: which entry conditions lose? Read by_hour_ist, by_dow, by_direction, by_strategy, by_grade, by_month and the cohort.* headline. Name the buckets that carry the losses and say whether they are large enough to trust (too_small).",
    "levels": "You are the LEVELS analyst for a cohort: is the stop placement the problem? Read by_stop_pct, by_rr, cohort.first_bar_stop_rate, cohort.stop_hit_rate, cohort.avg_mfe_r / avg_mae_r, cohort.median_stop_pct and by_exit_reason. Say what the numbers imply about stop distance and target reachability.",
    "execution": "You are the EXECUTION analyst for a cohort: exits and costs. Read by_bars_held, cohort.give_back_rate, cohort.mfe_capture_median, cohort.t1_hit_rate, cohort.eod_rate, cohort.charges_share_of_gross and by_symbol. Say whether the exit rule or the cost structure explains a material share of the result.",
}

CASE_DEBATE_PROMPT = "You run the debate. First argue that the CONSTRUCTION failed (stop, target, instrument, exit rule, costs); then argue that the THESIS failed (direction or timing) or that the loss was a valid outcome of a sound trade. Use only the analysts' cited evidence. Name the point each side could not rebut."

CASE_VERDICT_PROMPT = "You are the REVIEWER. Synthesise the analysts and the debate into one post-mortem. Pick the primary failure mode from the taxonomy, state the counterfactual with numbers from path.* (what a different stop or exit would have paid on this exact path), and — only if this case supports one — propose ONE hypothesis as parameter changes on paths that exist: fudkii.grade_policy.min_stop_atr, fudkii.grade_policy.stop_requires_wall (1/0), fudkii.grade_policy.rr_hard_floor, fudkii.grade_policy.room_min_atr, fudkii.flip_max_bars_ago, fudkii.eod_min_fortress, fukaa.volume_multiplier_nse, fukaa.volume_multiplier_mcx, fukaa.composite_min, limits.entry_cutoff_buffer_min, limits.risk_per_trade_pct, slippage_bps. Write a one-sentence lesson that will be re-read by future reviews."

COHORT_REPORT_PROMPT = "You are the REVIEWER. Synthesise the three analysts into one cohort report: a verdict on whether this cohort has an edge, findings ranked by how much of the result they explain (each with its failure mode and the buckets that show it), and at most three hypotheses, each ONE backtest away from an answer, as parameter changes on paths that exist: fudkii.grade_policy.min_stop_atr, fudkii.grade_policy.stop_requires_wall (1/0), fudkii.grade_policy.rr_hard_floor, fudkii.grade_policy.room_min_atr, fudkii.flip_max_bars_ago, fudkii.eod_min_fortress, fukaa.volume_multiplier_nse, fukaa.volume_multiplier_mcx, fukaa.composite_min, limits.entry_cutoff_buffer_min, limits.risk_per_trade_pct, slippage_bps. Prefer the hypothesis that addresses the largest loss_share. Say plainly what the tables cannot explain."


@dataclass(slots=True)
class CaseRun:
    ref: str
    started_ts: float
    analysts: list[CaseAnalystReport] = field(default_factory=list)
    debate: CaseDebate | None = None
    verdict: PostMortem | None = None
    calls: int = 0
    seconds: float = 0.0
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "started_ts": self.started_ts,
            "analysts": [a.model_dump() for a in self.analysts],
            "debate": self.debate.model_dump() if self.debate else None,
            "verdict": self.verdict.model_dump() if self.verdict else None,
            "calls": self.calls,
            "seconds": round(self.seconds, 1),
            "error": self.error,
        }


@dataclass(slots=True)
class CohortRun:
    ref: str
    started_ts: float
    analysts: list[CaseAnalystReport] = field(default_factory=list)
    report: CohortReport | None = None
    calls: int = 0
    seconds: float = 0.0
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "started_ts": self.started_ts,
            "analysts": [a.model_dump() for a in self.analysts],
            "report": self.report.model_dump() if self.report else None,
            "calls": self.calls,
            "seconds": round(self.seconds, 1),
            "error": self.error,
        }


def _render_reports(reports: list[CaseAnalystReport]) -> str:
    out = []
    for r in reports:
        out.append(f"[{r.role} analyst, severity {r.severity:.2f}] {r.summary}")
        out.extend(f"  - {f}" for f in r.findings)
    return "\n".join(out)


async def _analysts(llm: LLM, roles: dict[str, str], common: str) -> list[CaseAnalystReport]:
    return list(
        await asyncio.gather(
            *[
                llm.structured(
                    system=SYSTEM_CORE + prompt,
                    user=common + f"\nWrite the {role} analyst report. Set role to '{role}'.",
                    schema=CaseAnalystReport,
                    effort="medium",
                )
                for role, prompt in roles.items()
            ]
        )
    )


async def run_case_committee(
    llm: LLM, *, ref: str, evidence_text: str, past_context: str = ""
) -> CaseRun:
    run = CaseRun(ref=ref, started_ts=time.time())
    common = f"EVIDENCE PACK (one decision and what followed)\n{evidence_text}\n"
    try:
        run.analysts = await _analysts(llm, CASE_ROLES, common)
        run.calls += 3
        reports = _render_reports(run.analysts)
        run.debate = await llm.structured(
            system=SYSTEM_CORE + CASE_DEBATE_PROMPT,
            user=common + f"\nANALYST REPORTS\n{reports}\n"
            + (f"\nPAST LESSONS\n{past_context}\n" if past_context else "")
            + "\nProduce the debate.",
            schema=CaseDebate,
            effort="medium",
        )
        run.calls += 1
        d = run.debate
        debate_text = (
            f"CONSTRUCTION: {d.construction_case}\nTHESIS: {d.thesis_case}\n"
            f"Unrebutted construction point: {d.unrebutted_construction_point}\n"
            f"Unrebutted thesis point: {d.unrebutted_thesis_point}\n"
            f"Unresolved: {'; '.join(d.unresolved)}"
        )
        run.verdict = await llm.structured(
            system=SYSTEM_CORE + CASE_VERDICT_PROMPT,
            user=common + f"\nANALYST REPORTS\n{reports}\n\nDEBATE\n{debate_text}\n"
            + (f"\nPAST LESSONS\n{past_context}\n" if past_context else "")
            + "\nDeliver the post-mortem.",
            schema=PostMortem,
            effort="high",
            max_tokens=6000,
        )
        run.calls += 1
    except Exception as exc:  # noqa: BLE001 - a failed run is recorded, never raised into the engine
        run.error = f"{type(exc).__name__}: {exc}"
    run.seconds = time.time() - run.started_ts
    return run


async def run_cohort_committee(
    llm: LLM, *, ref: str, forensics_text: str, past_context: str = ""
) -> CohortRun:
    run = CohortRun(ref=ref, started_ts=time.time())
    common = f"EVIDENCE PACK (forensic tables over a cohort of trades)\n{forensics_text}\n"
    try:
        run.analysts = await _analysts(llm, COHORT_ROLES, common)
        run.calls += 3
        reports = _render_reports(run.analysts)
        run.report = await llm.structured(
            system=SYSTEM_CORE + COHORT_REPORT_PROMPT,
            user=common + f"\nANALYST REPORTS\n{reports}\n"
            + (f"\nPAST LESSONS AND EXPERIMENTS\n{past_context}\n" if past_context else "")
            + "\nDeliver the cohort report.",
            schema=CohortReport,
            effort="high",
            max_tokens=8000,
        )
        run.calls += 1
    except Exception as exc:  # noqa: BLE001 - see run_case_committee
        run.error = f"{type(exc).__name__}: {exc}"
    run.seconds = time.time() - run.started_ts
    return run


async def reflect(llm: LLM, *, hypothesis_text: str, result_text: str) -> Reflection:
    return await llm.structured(
        system="You are a trading analyst reviewing a hypothesis your own committee proposed, now that the backtest of it is in. Be specific and terse; your text is stored verbatim and re-read by future runs. A hypothesis that did not improve the day-clustered average R is refuted even if a sub-bucket improved.",
        user=f"HYPOTHESIS\n{hypothesis_text}\n\nEXPERIMENT RESULT\n{result_text}\n\nWrite the reflection.",
        schema=Reflection,
        effort="low",
        max_tokens=1000,
    )


__all__ = [
    "CaseRun",
    "CohortRun",
    "reflect",
    "run_case_committee",
    "run_cohort_committee",
]
