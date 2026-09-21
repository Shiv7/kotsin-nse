#!/usr/bin/env python3
"""Launch ShinkaEvolve on the FUDKII policy. Needs `pip install git+https://github.com/SakanaAI/ShinkaEvolve`
and an LLM key for the models named in the YAML; see README.md."""

from __future__ import annotations

import argparse

import yaml
from shinka.core import EvolutionConfig, ShinkaEvolveRunner  # type: ignore[import-not-found]
from shinka.database import DatabaseConfig  # type: ignore[import-not-found]
from shinka.launch import LocalJobConfig  # type: ignore[import-not-found]

TASK = """You are tuning the parameters of FUDKII, a 30-minute-bar breakout strategy on NSE stock underlyings
(SuperTrend(7,3) flip coinciding with a Bollinger(20,2) close outside the band; stop = nearest pivot-confluence
zone behind, targets = walls ahead; grade from reward:risk). Positions are intraday, force-flat at 15:20 IST,
and a round trip costs about 0.3% — costs decide the sign of most results.

Edit only the dict returned by policy(). Allowed keys are dotted paths on BacktestParams:
fudkii.grade_policy.{min_stop_atr, stop_requires_wall (1/0), rr_hard_floor, rr_a, rr_b, rr_c, room_min_atr,
fortress_block, max_targets}, fudkii.{flip_max_bars_ago, eod_min_fortress, bb_period, bb_mult, st_atr_period,
st_mult}, fukaa.{volume_multiplier_nse, composite_min, rr_floor}, limits.{time_stop_bars, trail_arm_pct,
trail_giveback_pct, breakeven_after_t1 (1/0), entry_cutoff_buffer_min}, slippage_bps. At most 8 keys.

Known from the diagnosis on the train range: the modal loss is a stop inside 0.25% of entry taken out on the
first bar; a 1 ATR stop floor halved the loss but did not make it positive; trades held 5–12 bars are the only
positive bucket. The fitness is the mean over four consecutive time blocks of the day-clustered average R net
of costs, with a penalty below 60 trades. A change that only works in one block scores poorly by design.
Prefer few, large, explainable changes over many small ones; the holdout charges for every free parameter."""


def main(config_path: str) -> None:
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["evo_config"]["task_sys_msg"] = TASK
    runner = ShinkaEvolveRunner(
        evo_config=EvolutionConfig(**config["evo_config"]),
        job_config=LocalJobConfig(eval_program_path="evaluate.py", time="00:20:00"),
        db_config=DatabaseConfig(**config["db_config"]),
        max_evaluation_jobs=config.get("max_evaluation_jobs"),
        max_proposal_jobs=config.get("max_proposal_jobs"),
        max_db_workers=config.get("max_db_workers"),
        debug=False,
        verbose=True,
    )
    runner.run()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config_path", default="shinka_kotsin.yaml")
    main(ap.parse_args().config_path)
