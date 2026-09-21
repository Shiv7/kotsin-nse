"""Fitness for ShinkaEvolve: the walk-forward score of a candidate policy on the TRAIN range only.

The score is the mean over consecutive time blocks of the day-clustered average R net of costs
(``committee.experiments.walk_forward_score``), penalised when the policy stops trading. The
holdout — the last ``KN_COMMITTEE_HOLDOUT_FRAC`` of the cache — is never scored here; grade the
final winner once with ``python evaluate.py --program_path <winner> --holdout`` and believe
that number, not the evolution's. MadEvolve (2026) kept 39–54 % of validation PnL on its test
set with every safeguard on; expect the same shape.

Runs with or without the ``shinka`` package: standalone it imports the program and scores it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)  # .env and the relative KN_DATA_DIR resolve from the backend directory

from kotsin_nse.committee.experiments import (  # noqa: E402
    PATCHABLE_ROOTS,
    apply_changes,
    holdout_split,
    walk_forward_score,
)
from kotsin_nse.committee.schemas import ParamChange  # noqa: E402
from kotsin_nse.config import Segment, Settings  # noqa: E402
from kotsin_nse.research.backtest import BacktestParams  # noqa: E402
from kotsin_nse.research.history import HistoryStore  # noqa: E402

#: the parameter budget — MadEvolve's pilot runs without one "fit validation strongly but failed
#: to generalize out-of-sample"
MAX_PARAMS = 8
N_BLOCKS = 4


def validate(run_output: Any, atol: float = 0.0) -> tuple[bool, str | None]:
    if not isinstance(run_output, dict):
        return False, "policy() must return a dict of dotted path → number"
    if len(run_output) > MAX_PARAMS:
        return False, f"{len(run_output)} parameters > budget {MAX_PARAMS}"
    for k, v in run_output.items():
        if not isinstance(k, str) or k.split(".")[0] not in PATCHABLE_ROOTS:
            return False, f"unknown parameter path {k!r}; roots: {', '.join(PATCHABLE_ROOTS)}"
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False, f"{k} must be a number"
    try:
        apply_changes(BacktestParams(), [ParamChange(path=k, value=float(v)) for k, v in run_output.items()])
    except ValueError as exc:
        return False, str(exc)
    return True, "policy is well-formed"


def score_policy(policy: dict[str, float], *, holdout: bool = False) -> dict[str, Any]:
    settings = Settings(_env_file=str(BACKEND / ".env"))
    changes = [ParamChange(path=k, value=float(v)) for k, v in policy.items()]
    params = apply_changes(BacktestParams(segment=Segment.NSE_EQ), changes)
    store = HistoryStore(settings.data_dir / "history")
    symbols = store.symbols("30m")
    split = holdout_split(store, symbols, frac=settings.committee_holdout_frac)
    if holdout:
        return walk_forward_score(settings, params, store, symbols, start=split.test_start, end=split.test_end, n_blocks=1)
    return walk_forward_score(settings, params, store, symbols, start=split.train_start, end=split.train_end, n_blocks=N_BLOCKS)


def aggregate(results: list[dict[str, float]], results_dir: str, *, holdout: bool = False) -> dict[str, Any]:
    policy = results[0]
    wf = score_policy(policy, holdout=holdout)
    metrics = {
        "combined_score": float(wf["score"]),
        "public": {
            "trades": wf["trades"],
            "blocks_positive": wf["blocks_positive"],
            "blocks_total": wf["blocks_total"],
            "policy": policy,
        },
        "private": {"blocks": wf["blocks"]},
        "text_feedback": (
            f"walk-forward score {wf['score']:+.3f} R over {wf['blocks_total']} blocks "
            f"({wf['blocks_positive']} positive), {wf['trades']} trades. "
            "A higher score with fewer than 60 trades is penalised; a block that never trades scores nothing."
        ),
    }
    Path(results_dir).mkdir(parents=True, exist_ok=True)
    (Path(results_dir) / "metrics.json").write_text(json.dumps(metrics, indent=1))
    return metrics


def _load_program(program_path: str) -> Any:
    spec = importlib.util.spec_from_file_location("candidate", program_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(program_path: str, results_dir: str, *, holdout: bool = False) -> None:
    try:
        from shinka.core import run_shinka_eval  # type: ignore[import-not-found]
    except ImportError:
        run_shinka_eval = None
    if run_shinka_eval is None or holdout:
        out = _load_program(program_path).run_experiment()
        ok, msg = validate(out)
        if not ok:
            print(f"invalid: {msg}")
            raise SystemExit(1)
        metrics = aggregate([out], results_dir, holdout=holdout)
    else:
        metrics, correct, error_msg = run_shinka_eval(
            program_path=program_path,
            results_dir=results_dir,
            experiment_fn_name="run_experiment",
            num_runs=1,
            get_experiment_kwargs=lambda _i: {},
            validate_fn=validate,
            aggregate_metrics_fn=lambda r: aggregate(r, results_dir),
        )
        if not correct:
            print(f"evaluation failed: {error_msg}")
    print(json.dumps(metrics, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FUDKII policy fitness (walk-forward, train range)")
    ap.add_argument("--program_path", default="initial.py")
    ap.add_argument("--results_dir", default="results/manual")
    ap.add_argument("--holdout", action="store_true", help="score on the holdout instead (the winner, once)")
    a = ap.parse_args()
    main(a.program_path, a.results_dir, holdout=a.holdout)
