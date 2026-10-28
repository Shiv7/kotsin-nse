# Pre-registration: does a visual "proper setup" judgment predict the held CPR retest's outcome?

Written 2026-10-28 00:50 IST, before any label was produced.

## Question

The owner's method is visual: a "proper double bottom", a "mature retest", a "rejection at TC". Every rule
tested so far is arithmetic on bars (HANDOFF §3.4 rows 10–16) and none has an edge. This test asks whether
the visual judgment, made blind at the entry bar, separates the net outcome of the 3,101 held retests of the
development window (2024-01-01 → 2025-09-30; `retest_trail.parquet`, entry at the first close beyond the
retest bar within 6 bars). The holdout (2025-10-01 → 2026-10-16) is spent and is not touched.

## The label

One image per instance: the day's 5m candles from 09:15 to the entry bar inclusive — no later bar — prices
as % of the previous close, no symbol, no date, the CPR band, R1, S1, PDH and PDL drawn, the retest bar
marked, the side stated. A fixed prompt (the owner's method in plain words) and a fixed JSON form:

| field | values |
|---|---|
| `double_bottom` | none / weak / proper (for a short: double top) |
| `retest` | none / weak / clean |
| `rejection_at_edge` | no / yes (a wick through TC or BC that closed back) |
| `impulse` | weak / moderate / strong (the move before the retest) |
| `take` | no / yes (would the method take this entry) |
| `reason` | one line |

## Calibration gate (before the full run)

50 instances, seeded (`20261028`), 25 long / 25 short, blind ids c01–c50. The owner labels them on a private
page; Claude Opus 5.5 labels the same images (effort medium, structured output). Agreement is measured as
exact agreement and Cohen's kappa per field. The full run proceeds only if kappa ≥ 0.4 on `take` and on at
least one of `double_bottom` / `retest`; otherwise the prompt gets the owner's five worked examples and the
gate is re-run once. Two failures end the experiment: a label that is not the owner's judgment tests nothing.

## The test (full run, 3,101 instances, Batch API)

Primary: `take = yes` against `take = no`, mean net bps per trade under `wall_then_trail` after 15 bps costs,
t-statistic over daily means (the convention of every test in HANDOFF §3.4). Pass: t ≥ 2.0 and the `take`
group's net > 0. Secondary (Bonferroni over 4): `double_bottom = proper`, `retest = clean`,
`rejection_at_edge = yes`, `impulse = strong`, each against its complement. Proxy check: the `take` label's
rank correlation with first-wall distance in R and retest depth in tolerances; a label explained by either
adds nothing.

## What each outcome means

Pass → encode what the model's reasons describe as a number, re-test the numeric version on the same window,
then shadow. Fail → on the 5m chart alone the visual judgment adds nothing to the sequence; close the line.
Expected: fail.

## Cost

Calibration: 50 Opus 5.5 calls, under $1. Full run: Haiku 5.5 under $1 or Opus 5.5 about $17, Batch API.

## Run log

- 2026-10-28 00:16 IST: 50 charts rendered (`scripts/2026-10-28-chart-labels/render.py`, seed 20261028; 25 long,
  25 short; blind ids c01–c50). The key (symbol, day, outcome per id) stays off the repo until the comparison.
- 00:30 IST: the owner's labelling page published (private artifact `HGy22HLxfLXBLf83PrTfN5`, labels in its
  `labels` collection). Owner's labels: pending.
- 00:35 IST: Claude Opus 5.5 labelled all 50 (`label_opus.py`, effort medium, structured output, one fixed
  prompt): $0.85 (30.8k input, 52.8k cached, 35.8k output tokens — the output is mostly thinking), 9.4 s a
  call. Distribution before any comparison: `take` yes 1 / no 49; `retest` clean 3, weak 45, none 2;
  `double_bottom` proper 0, weak 29, none 21; `rejection_at_edge` yes 14; `impulse` strong 24, moderate 18,
  weak 8. The model is strict: on its own reading almost no mechanical held retest is a setup the method
  would take. Whether that is the owner's reading is exactly what the gate measures; nothing is tuned until
  his 50 labels are in.
- 2026-10-29 00:48 IST (interim, not the gate): the owner printed the sheet to PDF. The sheet's database
  (`scripts/2026-10-28-chart-labels/labels-2026-10-29.json`) holds 20 labels by the owner (c01–c08, c39–c50) and
  30 by a separate Claude.ai chat he opened on the same page (c09–c38, labeller `claude`, each reason headed "AS
  SHIV WOULD GRADE IT"; that chat also republished the page with a third tab). The 30 are a model rater, not the
  owner, so they cannot stand in for his labels in the gate. On his 20 against Opus: `take` 10/20 (kappa −0.10;
  owner yes 9, Opus yes 0), `double_bottom` 4/20 (0.01; owner "proper" 14, Opus never), `retest` 8/20 (0.07),
  `rejection_at_edge` 5/20 (−0.27; owner yes 16, Opus 6), `impulse` 11/20 (0.31). The disagreement is systematic,
  not noise: the owner reads most charts as proper double bottoms with a rejection, and in 8 of his 11 "no"
  answers his reason names an earlier bar he would have entered on. The gate still waits for his remaining 30.
