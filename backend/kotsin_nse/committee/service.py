"""The committee as a service: reviews on demand, optional auto-review of every closed trade,
experiments in a worker thread, and a status the UI reads.

Structurally unable to touch a trade: it runs on its own tasks, the engine hands it nothing but a
closed trade's signal id, and nothing it produces is read by the decision path. Its only outputs
are a JSON review log and backtest results.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ..config import Segment, Settings
from ..ledger.db import trades
from ..market.session import ist_hm, session_phase
from ..research.backtest import BacktestParams
from ..research.history import HistoryStore
from .evidence import BarRow, Case, case_pack, render_case
from .experiments import apply_changes, changes_key, run_experiment
from .forensics import TradeRec, blind_view, forensics, from_backtest, from_ledger, render
from .llm import LLM, AnthropicLLM
from .memory import ReviewLog
from .pipeline import reflect, run_case_committee, run_cohort_committee
from .schemas import ParamChange

if TYPE_CHECKING:
    from ..engine import Engine

log = structlog.get_logger("committee")

#: decision-frame bars before the signal shown in the pack (context for the trigger, ATR proxy)
BARS_BEFORE = 24
PUBLIC_DROP = ("run", "pack")


def public(entry: dict[str, Any]) -> dict[str, Any]:
    """A review without its bulk (the full run and the evidence pack), for lists."""
    return {k: v for k, v in entry.items() if k not in PUBLIC_DROP}


class CommitteeService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        llm: LLM | None = None,
        *,
        decision_tf: str = "30m",
    ) -> None:
        self.engine = engine
        self.s = settings
        self.decision_tf = decision_tf
        key = (
            settings.anthropic_api_key.get_secret_value()
            if settings.anthropic_api_key
            else os.environ.get("ANTHROPIC_API_KEY") or None
        )
        self.llm: LLM | None = llm
        if self.llm is None and key:
            try:
                self.llm = AnthropicLLM(key, settings.committee_model)
            except ImportError:
                log.warning("committee.sdk_missing", note="the anthropic package is not installed")
        self.available = self.llm is not None
        self.auto = self.available and settings.committee_auto_review
        self.log = ReviewLog(settings.data_dir / "committee" / "reviews.json")
        self.running: set[str] = set()
        self.runs_today = 0
        self._day = self._today()
        self.errors = 0
        self.last_error = ""
        self.last_run_ts: float | None = None
        self.last_autopilot_ts: float | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    @staticmethod
    def _today() -> str:
        return datetime.now(tz=UTC).strftime("%Y-%m-%d")

    def _budget(self) -> None:
        if self._today() != self._day:
            self._day, self.runs_today = self._today(), 0
        if self.llm is None:
            raise RuntimeError(
                "committee unavailable: set KN_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) in backend/.env"
            )
        if self.runs_today >= self.s.committee_max_runs_per_day:
            raise RuntimeError(
                f"committee budget: {self.runs_today} runs today ≥ cap {self.s.committee_max_runs_per_day}"
            )

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass

    # -- sources --------------------------------------------------------------------------------

    def backtest_run(self, run_id: str) -> dict[str, Any]:
        path = self.s.data_dir / "backtests" / f"{run_id}.json"
        if not path.exists() or not run_id.startswith("bt-"):
            raise KeyError(f"unknown backtest run {run_id!r}")
        return json.loads(path.read_text())

    async def cohort_rows(
        self, source: str, strategy: str | None = None
    ) -> tuple[list[TradeRec], dict[str, Any]]:
        if source == "ledger":
            rows = await self.engine.ledger.recent(trades, 5000)
            recs = [from_ledger(r) for r in rows]
            meta: dict[str, Any] = {"source": "ledger", "segment": None}
        elif source.startswith("backtest:"):
            run = self.backtest_run(source.split(":", 1)[1])
            summary = run.get("summary") or {}
            recs = [from_backtest(t) for t in run.get("trades") or []]
            meta = {
                "source": source,
                "segment": (summary.get("params") or {}).get("segment"),
                "run": {
                    k: summary.get(k)
                    for k in ("id", "created_ts", "trades", "avg_r", "avg_r_t", "net", "symbols", "n_days")
                },
            }
        else:
            raise KeyError(f"unknown source {source!r}; use 'ledger' or 'backtest:<run id>'")
        if strategy:
            recs = [r for r in recs if r.strategy == strategy]
        return recs, meta

    async def forensics(self, source: str, strategy: str | None = None) -> dict[str, Any]:
        """Deterministic and free: no key needed, no call made."""
        recs, meta = await self.cohort_rows(source, strategy)
        return {"meta": {**meta, "strategy": strategy, "n": len(recs)}, **forensics(recs)}

    # -- cohort review ----------------------------------------------------------------------------

    async def review_cohort(self, source: str, strategy: str | None = None) -> dict[str, Any]:
        self._budget()
        assert self.llm is not None
        key = f"cohort:{source}:{strategy or 'ALL'}"
        if key in self.running:
            raise RuntimeError(f"a review of {key} is already in progress")
        self.running.add(key)
        try:
            f = await self.forensics(source, strategy)
            if f["cohort"].get("n", 0) == 0:
                raise KeyError(f"no trades in {source}" + (f" for {strategy}" if strategy else ""))
            blind_map: dict[str, str] = {}
            view: dict[str, Any] = f
            title = f"{source} strategy={strategy or 'ALL'}"
            if self.s.committee_blind:
                view, blind_map = blind_view(f)
                title = f"cohort strategy={strategy or 'ALL'}"
            text = render(view, title=title)
            run = await run_cohort_committee(
                self.llm,
                ref=key,
                forensics_text=text,
                past_context=self.log.past_context(strategy=strategy),
            )
            self.runs_today += 1
            self.last_run_ts = time.time()
            entry: dict[str, Any] = {
                "kind": "cohort",
                "subject": f["meta"],
                "strategy": strategy,
                "symbol": None,
                "run": run.to_json(),
                "pack": {"cohort": f["cohort"], "dims": f["dims"]},
                "blind": self.s.committee_blind,
                "blind_map": blind_map,
                "error": run.error,
            }
            if run.report is not None:
                r = run.report
                top = r.findings[0] if r.findings else None
                entry.update(
                    verdict=r.verdict,
                    findings=[x.model_dump() for x in r.findings],
                    not_explained=r.not_explained,
                    failure_mode=top.failure_mode.value if top else None,
                    confidence=top.confidence if top else None,
                    lesson=r.verdict,
                    hypotheses=[h.model_dump() for h in r.hypotheses],
                )
            else:
                self.errors += 1
                self.last_error = run.error or "no report"
            return self._record(entry)
        finally:
            self.running.discard(key)

    # -- case review -------------------------------------------------------------------------------

    async def review_signal(self, signal_id: str) -> dict[str, Any]:
        self._budget()
        row = await self.engine.ledger.signal(signal_id)
        if row is None:
            raise KeyError(f"unknown signal {signal_id!r}")
        trade = await self.engine.ledger.trade_for_signal(signal_id)
        symbol, ts = str(row["symbol"]), int(row["ts"])
        inst = self.engine.underlyings.get(symbol)
        segment = inst.segment if inst else Segment.NSE_EQ
        before, after = await self._bars_around(symbol, ts)
        case = Case(
            ref=signal_id,
            kind="ledger",
            strategy=str(row["strategy"]),
            symbol=symbol,
            direction=str(row["direction"]),
            ts=ts,
            entry=float(row["entry"]),
            stop=float(row["stop"]),
            targets=tuple(float(t) for t in row.get("targets") or ()),
            grade=str(row.get("grade") or ""),
            rr=float(row.get("rr") or 0.0),
            exchange=segment.exch,
            session_phase=session_phase(segment, ts),
            mode=self.engine.mode().value,
            signal=row,
            trade=trade,
            bars_before=before,
            bars_after=after,
        )
        return await self._review_case(case)

    async def review_backtest_trade(self, run_id: str, index: int) -> dict[str, Any]:
        self._budget()
        run = self.backtest_run(run_id)
        trades_ = run.get("trades") or []
        if not 0 <= index < len(trades_):
            raise KeyError(f"{run_id} has {len(trades_)} trades; no index {index}")
        t = trades_[index]
        seg_name = (run.get("summary") or {}).get("params", {}).get("segment", "NSE_EQ")
        segment = Segment[seg_name] if seg_name in Segment.__members__ else Segment.NSE_EQ
        symbol, ts = str(t["symbol"]), int(t["entry_ts"])
        before, after = await self._bars_around(symbol, ts)
        entry, stop = float(t["entry"]), float(t["stop"])
        t1 = float(t["target1"]) if t.get("target1") is not None else None
        risk = abs(entry - stop)
        case = Case(
            ref=f"{run_id}#{index}",
            kind="backtest",
            strategy=str(t["strategy"]),
            symbol=symbol,
            direction=str(t["direction"]),
            ts=ts,
            entry=entry,
            stop=stop,
            targets=(t1,) if t1 is not None else (),
            grade=str(t.get("grade") or ""),
            rr=round(abs(t1 - entry) / risk, 2) if t1 is not None and risk > 0 else 0.0,
            exchange=segment.exch,
            session_phase=session_phase(segment, ts),
            mode="backtest",
            signal=None,
            trade=t,
            bars_before=before,
            bars_after=after,
        )
        return await self._review_case(case)

    async def _review_case(self, case: Case) -> dict[str, Any]:
        assert self.llm is not None
        if case.ref in self.running:
            raise RuntimeError(f"a review of {case.ref} is already in progress")
        self.running.add(case.ref)
        try:
            blind = self.s.committee_blind
            pack = case_pack(case, blind=blind)
            run = await run_case_committee(
                self.llm,
                ref=case.ref,
                evidence_text=render_case(pack),
                past_context=self.log.past_context(symbol=case.symbol, strategy=case.strategy),
            )
            self.runs_today += 1
            self.last_run_ts = time.time()
            entry: dict[str, Any] = {
                "kind": "case",
                "subject": {
                    "ref": case.ref,
                    "kind": case.kind,
                    "ts": case.ts,
                    "direction": case.direction,
                    "entry": case.entry,
                    "stop": case.stop,
                    "grade": case.grade,
                    "segment": None,
                },
                "strategy": case.strategy,
                "symbol": case.symbol,
                "run": run.to_json(),
                "pack": pack,
                "blind": blind,
                "blind_map": {"SYM": case.symbol, "case": case.ref} if blind else {},
                "error": run.error,
            }
            if run.verdict is not None:
                v = run.verdict
                entry.update(
                    failure_mode=v.failure_mode.value,
                    secondary=[m.value for m in v.secondary],
                    confidence=v.confidence,
                    what_happened=v.what_happened,
                    why=v.why,
                    counterfactual=v.counterfactual,
                    lesson=v.lesson,
                    hypotheses=[v.hypothesis.model_dump()] if v.hypothesis else [],
                )
            else:
                self.errors += 1
                self.last_error = run.error or "no verdict"
            return self._record(entry)
        finally:
            self.running.discard(case.ref)

    def _record(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.log.append(entry)
        run = entry.get("run") or {}
        if entry.get("error"):
            log.error(
                "committee.review_failed", kind=entry["kind"], ref=run.get("ref"), error=entry["error"]
            )
        else:
            log.info(
                "committee.review",
                kind=entry["kind"],
                ref=run.get("ref"),
                failure_mode=entry.get("failure_mode"),
                confidence=entry.get("confidence"),
                hypotheses=len(entry.get("hypotheses") or []),
                calls=run.get("calls"),
                seconds=run.get("seconds"),
            )
        return public(entry)

    async def _bars_around(self, symbol: str, ts: int) -> tuple[list[BarRow], list[BarRow]]:
        """Decision-frame bars either side of the signal: the live store first (it holds the
        backfilled month), the Parquet history cache for anything older."""
        n_after = self.s.committee_path_bars
        rows = [
            BarRow(b.ts, b.open, b.high, b.low, b.close, b.volume)
            for b in self.engine.store.bars(symbol, self.decision_tf, 5000)
        ]
        before = [b for b in rows if b.ts < ts][-BARS_BEFORE:]
        after = [b for b in rows if b.ts > ts][:n_after]
        if len(before) < BARS_BEFORE or len(after) < n_after:
            store = HistoryStore(self.s.data_dir / "history")
            df = await asyncio.to_thread(store.load, symbol, self.decision_tf)
            if not df.empty:
                hist = [
                    BarRow(int(r.ts), float(r.o), float(r.h), float(r.l), float(r.c), float(r.v))
                    for r in df.itertuples()
                ]
                if len(before) < BARS_BEFORE:
                    before = [b for b in hist if b.ts < ts][-BARS_BEFORE:]
                if len(after) < n_after:
                    after = [b for b in hist if b.ts > ts][:n_after]
        return before, after

    # -- manual proposals --------------------------------------------------------------------------

    def propose(
        self,
        *,
        title: str,
        changes: list[ParamChange],
        expected: str = "",
        rationale: str = "",
        segment: str = "NSE_EQ",
    ) -> dict[str, Any]:
        """A hypothesis from a person, graded exactly like the committee's. The loop must not need
        a key: the forensic tables already say enough to propose from, and the backtester — not
        the model — is what confirms or refutes."""
        if not changes:
            raise ValueError("a hypothesis needs at least one parameter change")
        apply_changes(BacktestParams(), changes)  # an unknown path fails here, not in the worker
        entry = self.log.append(
            {
                "kind": "manual",
                "subject": {"source": "manual", "segment": segment},
                "strategy": None,
                "symbol": None,
                "lesson": None,
                "hypotheses": [
                    {
                        "title": title,
                        "rationale": rationale,
                        "changes": [c.model_dump() for c in changes],
                        "expected": expected,
                    }
                ],
            }
        )
        log.info("committee.proposed", hypothesis=entry["hypotheses"][0]["id"], title=title)
        return entry["hypotheses"][0]

    # -- experiments -------------------------------------------------------------------------------

    def start_experiment(self, hyp_id: str) -> dict[str, Any]:
        found = self.log.find_hypothesis(hyp_id)
        if found is None:
            raise KeyError(f"unknown hypothesis {hyp_id!r}")
        entry, h = found
        if h.get("status") == "running":
            raise RuntimeError(f"experiment {hyp_id} is already running")
        self.log.update_hypothesis(hyp_id, status="running", started_ts=time.time(), error=None)
        self._spawn(self._run_experiment(entry, h))
        return {**h, "status": "running"}

    async def _run_experiment(self, entry: dict[str, Any], h: dict[str, Any]) -> None:
        try:
            changes = [ParamChange(**c) for c in h.get("changes") or []]
            if not changes:
                raise ValueError("the hypothesis lists no parameter changes")
            seg_name = (entry.get("subject") or {}).get("segment") or "NSE_EQ"
            segment = Segment[seg_name] if seg_name in Segment.__members__ else Segment.NSE_EQ
            result = await asyncio.to_thread(
                run_experiment,
                self.s,
                changes=changes,
                segment=segment,
                max_symbols=self.s.committee_experiment_symbols,
                holdout_frac=self.s.committee_holdout_frac,
                n_tested=self.n_tested(),
            )
            reflection: str | None = None
            if self.llm is not None:
                try:
                    r = await reflect(
                        self.llm,
                        hypothesis_text=(
                            f"{h.get('title')}\nrationale: {h.get('rationale')}\n"
                            f"changes: {h.get('changes')}\nexpected: {h.get('expected')}"
                        ),
                        result_text=json.dumps(
                            {
                                k: result.get(k)
                                for k in (
                                    "verdict", "note", "out_of_sample", "in_sample", "cost_stress",
                                    "survives_cost_stress", "monthly", "split", "n_tested",
                                )
                            },
                            default=str,
                        ),
                    )
                    reflection = r.lesson
                except Exception as exc:  # noqa: BLE001 - the grade stands without the prose
                    self.errors += 1
                    self.last_error = f"reflection: {exc}"
            self.log.update_hypothesis(
                h["id"],
                status=result["verdict"],
                result=result,
                reflection=reflection,
                finished_ts=time.time(),
            )
            log.info(
                "committee.experiment",
                hypothesis=h["id"],
                verdict=result["verdict"],
                delta_avg_r=result.get("delta_avg_r"),
                p_value=result.get("p_value"),
                seconds=result.get("seconds"),
            )
        except Exception as exc:
            self.log.update_hypothesis(
                h["id"], status="error", error=str(exc)[:300], finished_ts=time.time()
            )
            log.exception("committee.experiment_failed", hypothesis=h.get("id"), error=str(exc))

    def n_tested(self) -> int:
        """How many hypotheses have been graded so far, plus this one — the Bonferroni divisor."""
        graded = sum(
            1 for h in self.log.hypotheses() if h.get("status") in ("confirmed", "refuted", "inconclusive")
        )
        return graded + 1

    def vetoed(self, changes: list[Any]) -> str | None:
        """The status of an already-graded hypothesis with the same changes, if any. AlphaMemo's
        veto: a search that re-proposes a known failure has not learned, and a search that
        re-runs a known success has not moved."""
        key = changes_key(changes)
        for h in self.log.hypotheses():
            if h.get("status") in ("confirmed", "refuted", "inconclusive") and changes_key(
                h.get("changes") or []
            ) == key:
                return str(h["status"])
        return None

    # -- autopilot ----------------------------------------------------------------------------------

    async def autopilot_source(self) -> str:
        """The ledger once it can carry a verdict; the newest backtest run until then."""
        rows = await self.engine.ledger.recent(trades, 5000)
        if len(rows) >= 30:
            return "ledger"
        root = self.s.data_dir / "backtests"
        runs = sorted(root.glob("bt-*.json"), key=lambda x: x.stat().st_mtime, reverse=True) if root.exists() else []
        if not runs:
            raise KeyError("nothing to review: no ledger trades and no backtest runs")
        return f"backtest:{runs[0].stem}"

    async def autopilot_once(self, source: str | None = None) -> dict[str, Any]:
        """One night's loop: cohort review → veto known results → up to N experiments → memory.
        Sequential and bounded; every step lands in the log even when a later one fails."""
        self._budget()
        source = source or await self.autopilot_source()
        review = await self.review_cohort(source, None)
        if review.get("error"):
            raise RuntimeError(f"cohort review failed: {review['error']}")
        full = self.log.get(review["id"]) or {}
        ran: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for h in list(full.get("hypotheses") or []):
            if len(ran) >= self.s.committee_autopilot_experiments:
                break
            prior = self.vetoed(h.get("changes") or [])
            if prior:
                self.log.update_hypothesis(h["id"], status="vetoed", veto=f"already {prior}")
                skipped.append({"id": h["id"], "title": h.get("title"), "prior": prior})
                continue
            self.log.update_hypothesis(h["id"], status="running", started_ts=time.time(), error=None)
            found = self.log.find_hypothesis(h["id"])
            if found is None:
                continue
            entry, hh = found
            await self._run_experiment(entry, hh)
            graded = self.log.find_hypothesis(h["id"])
            status = graded[1].get("status") if graded else "error"
            ran.append({"id": h["id"], "title": h.get("title"), "status": status})
        summary = self.log.append(
            {
                "kind": "autopilot",
                "subject": {"source": source, "review_id": review["id"]},
                "strategy": None,
                "symbol": None,
                "ran": ran,
                "skipped": skipped,
                "lesson": (
                    f"autopilot on {source}: {len(ran)} experiment(s) "
                    f"[{', '.join(r['status'] for r in ran) or 'none'}], {len(skipped)} vetoed"
                ),
            }
        )
        self.last_autopilot_ts = time.time()
        log.info("committee.autopilot", source=source, ran=len(ran), vetoed=len(skipped))
        return public(summary)

    def autopilot_due(self, now: float, day: str, last_day: str) -> bool:
        return (
            self.s.committee_autopilot
            and self.available
            and day != last_day
            and ist_hm(now) >= self.s.committee_autopilot_ist
        )

    # -- auto review ---------------------------------------------------------------------------------

    def on_trade_closed(self, trade: dict[str, Any]) -> None:
        """Called by the engine after a trade is written. Fire-and-forget; never awaited on the
        trade path."""
        if not self.auto:
            return
        sid = trade.get("signal_id")
        if not sid:
            return
        self._spawn(self._auto_review(str(sid)))

    async def _auto_review(self, signal_id: str) -> None:
        try:
            await self.review_signal(signal_id)
        except Exception as exc:  # noqa: BLE001 - an advisory failure is logged, not raised
            self.errors += 1
            self.last_error = str(exc)[:200]
            log.warning("committee.auto_review_failed", signal_id=signal_id, error=str(exc))

    # -- status ------------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        hyps = self.log.hypotheses()
        return {
            "available": self.available,
            "auto": self.auto,
            "model": self.llm.model if self.llm else None,
            "runs_today": self.runs_today,
            "max_runs_per_day": self.s.committee_max_runs_per_day,
            "running": sorted(self.running),
            "experiments_running": sum(1 for h in hyps if h.get("status") == "running"),
            "errors": self.errors,
            "last_error": self.last_error,
            "last_run_ts": self.last_run_ts,
            "llm": self.llm.stats() if self.llm else None,
            "log": self.log.stats(),
            "path_bars": self.s.committee_path_bars,
            "blind": self.s.committee_blind,
            "holdout_frac": self.s.committee_holdout_frac,
            "n_tested": self.n_tested(),
            "autopilot": self.s.committee_autopilot,
            "autopilot_ist": self.s.committee_autopilot_ist,
            "last_autopilot_ts": self.last_autopilot_ts,
        }


__all__ = ["CommitteeService", "public"]
