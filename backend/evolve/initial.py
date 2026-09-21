"""The program ShinkaEvolve evolves: a parameter policy for FUDKII.

Only the block between the markers is editable. It must return a dict of dotted
``BacktestParams`` paths → numbers — the same vocabulary as a committee hypothesis
(``fudkii.<field>``, ``fudkii.grade_policy.<field>``, ``fukaa.<field>``, ``limits.<field>``,
``slippage_bps``); an unknown path makes the candidate invalid, and more than
``evaluate.MAX_PARAMS`` entries does too. The fitness lives in ``evaluate.py``, never here.
"""

# EVOLVE-BLOCK-START
def policy() -> dict[str, float]:
    """Overrides on the live defaults. Keep it small: every free number is a degree of freedom
    the holdout will charge for."""
    return {
        "fudkii.grade_policy.min_stop_atr": 0.0,
        "fudkii.grade_policy.rr_hard_floor": 1.0,
        "fudkii.grade_policy.room_min_atr": 0.5,
        "limits.time_stop_bars": 8,
    }
# EVOLVE-BLOCK-END


def run_experiment() -> dict[str, float]:
    return policy()
