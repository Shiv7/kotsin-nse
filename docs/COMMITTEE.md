# The review committee

**What it is.** A post-mortem on the algo's own decisions, run by Claude in structured roles, with
an experiment loop that grades every proposal by running the backtester. It answers the question
the Signals drill-down only lets a human ask one row at a time: *why does this lose, and what one
change would we test next?*

**What it is not.** It does not forecast, does not size, does not block. Nothing it produces is
read by the decision path (`engine._handle_signal` never consults it). Its outputs are a JSON review
log (`data/committee/reviews.json`) and backtest results.

Ported from `kotsin-crypto/committee` — the TradingAgents role scaffold, structured outputs whose
field descriptions are the instructions, a self-grading memory — and re-aimed. The crypto committee
reviews a *market*; this one reviews *trades*. `kotsin-crypto/research/rl` (offline exit-policy RL,
the parameter bandit) is a different thing and is not here: it needs episodes, and the only
episodes this book has are the backtester's.

## The loop

```
forensics (code)  →  committee (Claude)  →  hypothesis  →  experiment (backtester)  →  grade  →  memory
   tables, keys         cites keys            param patch     baseline vs patched       p ≤ 0.10    injected into
                                                              same cache, same range                 the next run
```

1. **Forensics** — `committee/forensics.py`. Deterministic, tested, free. Every trade (the
   backtester's `BtTrade` or the ledger's `Trade`) is normalised and bucketed nine ways — stop
   distance in % of entry, reward:risk, grade, exit reason, bars held, entry hour, weekday, month,
   symbol — each bucket with n, the **day-clustered** average R and its t, win rate, net and its
   share of the cohort's losses. Plus the headline: first-bar stop rate, give-back rate (MFE ≥ 1 R
   yet exit ≤ 0), median R / MFE captured, T1 hit rate, charges share of gross. Keys are citable:
   `by_stop_pct.<0.25.avg_r`, `cohort.first_bar_stop_rate`.
2. **Case pack** — `committee/evidence.py`. For one signal: everything the strategy stored when it
   decided (`trig.*` indicators, `lvl.*` stop/target geometry with the zone each came from, the
   zones table, `gate.*`), the outcome (`out.*`) and — computed here, nowhere else — the **path**:
   what the underlying did over the next 16 decision bars in R (`path.first_touch`,
   `path.bars_to_stop`, `path.max_adverse_before_t1_r` = the stop that would have survived).
3. **Committee** — `committee/pipeline.py`. Case: entry / levels / execution analysts in parallel
   → a construction-vs-thesis debate → a `PostMortem` with a failure mode from a fixed taxonomy
   (`NOISE_STOP`, `WRONG_DIRECTION`, `LATE_ENTRY`, `TARGET_UNREACHABLE`, `GAVE_BACK`,
   `COST_DOMINATED`, `SESSION_TIMING`, `INSTRUMENT_MISMATCH`, `DATA_QUALITY`, `VALID_LOSS`,
   `GOOD_TRADE`, `INCONCLUSIVE`), a counterfactual in `path.*` numbers, and at most one hypothesis.
   Five calls. Cohort: the same analysts read the forensic tables → a `CohortReport` with ranked
   findings and at most three hypotheses. Four calls. Every claim must cite a key; a review the
   pack cannot ground is `INCONCLUSIVE`, never a story.
4. **Experiment** — `committee/experiments.py`. A hypothesis is a list of `path=value` changes on
   `BacktestParams` (`fudkii.grade_policy.min_stop_atr`, `fukaa.volume_multiplier_nse`,
   `limits.entry_cutoff_buffer_min`, …; an unknown path is an error, never a no-op). The baseline
   (current defaults) and the patched parameters are both run over the same cached history and
   compared with the repo's within-day permutation test: **confirmed** if the patched average R is
   higher with p ≤ 0.10 on a sample the repo trusts (≥ 30 trades, ≥ 10 days), **refuted** if it is
   not higher with the same confidence, **inconclusive** otherwise. Runs in a worker thread.
5. **Memory** — `committee/memory.py`. Resolved hypotheses (with their measured Δ and p) and recent
   same-symbol lessons are injected into the next run's prompt. Pending ones never are.

## Using it

| Where | What |
|---|---|
| **Committee** page | forensics for the ledger or any backtest run, by book; "ask the committee"; hypotheses with *run experiment*; every review with its debate and evidence pack |
| **Signals** page → a signal → *ask the committee why* | a case review of that signal, with its trade if it closed |
| `GET /api/committee/forensics?source=backtest:<id>&strategy=FUDKII` | the tables, no key needed |
| `POST /api/committee/review/{signal,trade,cohort}` · `POST /api/committee/experiments/run` | the calls above |

Enable with `KN_ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY`) in `backend/.env`. `KN_COMMITTEE_MODEL`
(default `claude-opus-5`), `KN_COMMITTEE_MAX_RUNS_PER_DAY` (30), `KN_COMMITTEE_AUTO_REVIEW`
(false — when true every closed trade is reviewed as it closes), `KN_COMMITTEE_PATH_BARS` (16),
`KN_COMMITTEE_EXPERIMENT_SYMBOLS` (blank = every cached symbol).

**Cost.** A case is five calls on a ~4k-token pack; a cohort is four on ~6k tokens. At Opus list
prices that is roughly $0.15–0.30 per review; the daily cap bounds it. Forensics and experiments
cost nothing. The Committee page shows the running estimate.

## What to expect it to find first

The forensic tables on the 481-trade FUDKII backtest already say most of it without a model: the
modal loss is a stop inside 0.25% of entry taken out on the first bar, and grade A — the highest
reward:risk — is the worst grade because its stops are the closest. The committee's job is to turn
that into *one* parameter change, run it, and record whether it held. `docs/strategies/FUDKII.md`
§8 is where the confirmed ones go.
