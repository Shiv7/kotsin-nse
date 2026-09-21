# Program evolution against the backtester (ShinkaEvolve)

An exploratory tool, deliberately fenced. LLM mutations edit the `policy()` dict in `initial.py`
(dotted `BacktestParams` paths, ≤ 8 entries); `evaluate.py` scores each candidate with the
walk-forward fitness on the **train range only** — the mean over four consecutive time blocks of
the day-clustered average R net of costs, penalised below 60 trades. The holdout (the last
`KN_COMMITTEE_HOLDOUT_FRAC` of the cache, 30 %) is never scored during evolution.

```bash
cd backend
uv run python evolve/evaluate.py --program_path evolve/initial.py --results_dir evolve/results/manual   # standalone fitness, no LLM
uv pip install git+https://github.com/SakanaAI/ShinkaEvolve                                              # the framework (Apache-2)
cd evolve && ANTHROPIC_API_KEY=… OPENAI_API_KEY=… python run_evo.py --config_path shinka_kotsin.yaml    # ≤ $10 by config
uv run python evolve/evaluate.py --program_path evolve/results/kotsin_fudkii/<best>.py --holdout      # the winner, once
```

What to expect: MadEvolve (BTC minute bars, fixed harness, parameter caps, impact model) kept 39–54 %
of validation PnL on its held-out test; AlgoEvolve's champion Sharpe was 4× its population mean.
Read the holdout number, not the evolution's, and record it in `docs/strategies/FUDKII.md` §8 with
the number of candidates evaluated — that count is the multiple-testing divisor.
