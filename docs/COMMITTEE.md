# The review committee

**What it is.** A post-mortem on the algo's own decisions, run by Claude in structured roles, with
an experiment loop that grades every proposal by running the backtester **out of sample**. It
answers the question the Signals drill-down only lets a human ask one row at a time: *why does
this lose, and what one change do we test next?*

**What it is not.** It does not forecast, does not size, does not block. Nothing it produces is
read by the decision path (`engine._handle_signal` never consults it). Its outputs are a JSON review
log (`data/committee/reviews.json`) and backtest results.

Ported from `kotsin-crypto/committee` — the TradingAgents role scaffold, structured outputs whose
field descriptions are the instructions, a self-grading memory — and re-aimed. The crypto committee
reviews a *market*; this one reviews *trades*. `kotsin-crypto/research/rl` is a different thing and
is not here (see *RL: not now*).

## The loop

```
forensics (code)  →  committee (Claude)  →  hypothesis  →  experiment (backtester)  →  grade  →  memory
   blind tables        cites keys            param patch     holdout, cost stress,     p ≤ 0.10    injected into
                                                              months won, Bonferroni    adjusted    the next run
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
   `BacktestParams` (an unknown path is an error, never a no-op). The grade is read **only on the
   holdout** — the last `KN_COMMITTEE_HOLDOUT_FRAC` (30 %) of the cached history by time, which
   the committee never sees. Six backtests: both arms in sample (reported, labelled), both arms
   out of sample, both arms out of sample at ×1.5 brokerage and ×2 slippage. The out-of-sample
   within-day permutation p is Bonferroni-adjusted by the number of hypotheses graded so far.
   **confirmed** = patched average R higher with adjusted p ≤ 0.10 on ≥ 30 trades / ≥ 10 days *and*
   the delta survives cost stress; **refuted** = not higher with the same confidence; otherwise
   **inconclusive**. Per-month average R of both arms says how many months the change won.
5. **Memory** — `committee/memory.py`. Resolved hypotheses (with their measured Δ and p) and recent
   same-symbol lessons are injected into the next run's prompt. Pending ones never. A hypothesis
   whose changes were already graded is **vetoed**, not re-run (AlphaMemo's rule: re-proposing a
   known result is not learning).

## Blinding

`KN_COMMITTEE_BLIND=true` (default): the model reads `SYM`, `Mon 10:15`, `S01`, `M03` instead of
`ADANIENT`, `2025-10-29`, `SBIN`, `2025-12`. Time of day, weekday, every number and the zone
timeframes stay — they are the evidence. What goes is what lets a model recall what a named stock
did on a dated day (Look-Ahead-Bench, 2026: standard LLMs carry that). The log and the UI keep the
real names (`blind_map`); only the prompt is blind.

## Autopilot

`KN_COMMITTEE_AUTOPILOT=true` runs one loop a day after `KN_COMMITTEE_AUTOPILOT_IST` (02:00) with
every segment closed: cohort review of the ledger (once it has ≥ 30 trades; the newest backtest
until then) → veto hypotheses already graded → up to `KN_COMMITTEE_AUTOPILOT_EXPERIMENTS` (2)
experiments, sequentially, in a worker thread → an `autopilot` entry in the log. Needs a key;
≈ $1–3 a night at Opus prices. `POST /api/committee/autopilot/run` or the page button runs it now.

## Using it

| Where | What |
|---|---|
| **Committee** page | forensics for the ledger or any backtest run, by book; "ask the committee"; propose your own hypothesis; run experiments; autopilot; every review with its debate and (blind) evidence pack |
| **Signals** page → a signal → *ask the committee why* | a case review of that signal, with its trade if it closed |
| `kotsin-nse committee forensics --source backtest:<id> [--strategy FUDKII]` | the tables, no engine, no key |
| `kotsin-nse committee experiment --changes fudkii.grade_policy.min_stop_atr=1.0` | the six-backtest grade, printed, not logged |
| `kotsin-nse backtest --decision-tf 1d --holding delivery` | the same signal on daily bars, held overnight, long-only, delivery STT |
| `GET /api/committee/forensics`, `POST /api/committee/review/{signal,trade,cohort}`, `POST /api/committee/hypotheses`, `POST /api/committee/experiments/run`, `POST /api/committee/autopilot/run` | the API |

Enable the model with `KN_ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY`). `KN_COMMITTEE_MODEL`
(`claude-opus-5`), `KN_COMMITTEE_MAX_RUNS_PER_DAY` (30), `KN_COMMITTEE_AUTO_REVIEW` (false — every
closed trade reviewed as it closes), `KN_COMMITTEE_PATH_BARS` (16), `KN_COMMITTEE_HOLDOUT_FRAC`
(0.3), `KN_COMMITTEE_BLIND` (true), `KN_COMMITTEE_EXPERIMENT_SYMBOLS` (blank = every cached symbol).
A case is five calls on a ~4k-token pack, a cohort four on ~6k: roughly $0.15–0.30 per review.
Forensics and experiments cost nothing.

## Program evolution (`backend/evolve/`)

A ShinkaEvolve harness for the exploratory case: LLM mutations edit a `policy()` dict of
`BacktestParams` paths (≤ 8), the fitness is the walk-forward score on the **train range only**
(mean over four consecutive blocks of day-clustered average R net of costs, penalised below 60
trades), and the holdout is scored once, for the winner, by hand. `evolve/README.md` has the
commands. Standalone (no LLM) the evaluator scores the live defaults at **−1.44 R, 0 of 4 blocks
positive** — the number any candidate has to beat before the holdout is allowed to see it.

## RL: not now — and what would change it

Not built, on purpose. The exit is the profitable part of the trade (TIME_STOP exits average
+1.99 R, 82 % wins), so an exit policy is aimed at the wrong bottleneck; 481 episodes a year is too
few to learn a policy that generalises; and the two closest experiments — kotsin-crypto's own
R0/R1 (exit-policy FQI and a parameter bandit, walk-forward) and FinRL's public contests — both
came back negative out of sample once costs were modelled. What would change it: an entry with a
positive out-of-sample average R, a tape (the archive below), and several thousand episodes.

## The instrument question

No harness fixes this. SEBI: 91 % of individual F&O traders lost money in FY25 (₹1.05 lakh crore,
₹1.1 lakh each). This book: charges 209 % of gross on the 30m backtest, a 9.7 % bid-ask spread on
the closing quote of a stock option it would actually buy, and a ₹2,000 account at ₹40 per order.
Measured 2026-09-22 from the live catalogue: one ATM lot of NIFTY costs ≈ ₹10,900 (lot 65 ×
₹167.5), BANKNIFTY ≈ ₹14,100 (lot 30 × ₹471) — there is no index-option position this account can
open. Cash equity on daily bars is the remaining candidate; see the first results below.

## The archive (`ops/archive.py`)

`archive_enabled` was a config key nothing read until 2026-09-22 — a whole live session of option
OI, the one input FUKAA cannot be tested without, went unrecorded. Now, per IST day:
`data/archive/bars/<day>.parquet` (every closed 1m bar of every tracked underlying),
`oi/<day>.parquet` (every OI frame, futures and strikes), `micro/<day>.parquet` (the 30m
microstructure metrics), `option_quotes/<day>.parquet` (the carded contracts sampled with the spot
and delta that priced them) and `quotes/<day>.parquet` (the tick tape, `ops/tape.py`). Flushed every
`KN_ARCHIVE_FLUSH_S` (300 s) and at shutdown; the System page's health carries its counters. Months
of this are the only route to a FUKAA backtest — which is why the retention window is per stream
(`KN_ARCHIVE_KEEP_RESEARCH_SESSIONS` = 250 for these four; only the tape rolls at 15).

## First results (2026-09-22, 28 cached NSE symbols, 2025-09-22 → 2026-09-21)

| Experiment | In sample | **Out of sample** (2026-06-04 → 09-21) | Cost stress | Months won | Verdict |
|---|---|---|---|---|---|
| `fudkii.grade_policy.min_stop_atr = 1.0` | −1.38 → −0.18 R (336 → 218 trades) | **−1.32 → −0.57 R** (196 → 146), Δ +0.75, p 0.0002 | Δ +0.71 | 4 / 4 | confirmed |
| FUDKII on daily bars, delivery, long-only | 24 trades all year: −0.78 R (t −1.55, too small), 33 % wins, charges 148 % of gross; 15 shorts refused; `st_flip` binding 5,589× | | | | not a strategy |

Read the first row precisely: the stop floor is a **real, robust improvement** — and the strategy
with it is still −0.57 R out of sample with t = −5.1. The daily variant barely fires (the SuperTrend
flip and the Bollinger break rarely coincide on daily bars) and loses when it does. The entry has
no edge at either horizon; that is what the loop is for — to say so with numbers before money does.
