# Pre-registration: the held retest of *any* wall, with the day's move (owner, 3 Nov 2026 ~02:00 IST)

**Owner's correction (verbatim, 3 Nov 01:58–02:03 IST):** "cpr is just one reference same concept we can apply
at R1 pivot confluence round numbers where things goes in our side we are price action trader not cpr trader";
"retest concept was to not buy thing at high price and try to figure out bottom not cpr"; "short covering going
to long build best confirmation what we need more seller are trap best time to make money in option buying".

The live rule (`strategy/cpr_retest.sequence`) and rows 15–17 of HANDOFF §3.4 tested the sequence **only around
the CPR**: open test → BOS → close through TC/BC → retest of the band → entry. RELIANCE on Mon 2 Nov opened 3.5 %
above R3 and never entered that sequence; under the owner's concept the wall was the opening-range high
(1237.3, broken 09:35, retested 10:00–10:05, confirmed 10:10 at 1239.5) and the day ran to 1274.6.

## Hypothesis
H1 (owner): after price closes through a wall in the direction of the day's move, the first pullback to that
wall that holds is a positive-expectancy long/short at the confirming close, stop beyond the retest bar, target the
next wall then trail. Expectation per the record (HANDOFF §318: every written-down entry concept 0 to +7 bps gross,
negative net): the generalized retest lands in the same place. The test decides.

H2 (owner, the board): the future's state turning short covering → long build-up on the same move is the
confirmation ("sellers trapped"). **Not testable on the development window** (no intraday OI before the live
archive, 23 Oct 2026). Stays under the ten-session rule (`2026-10-30-prereg-week.md`): the first read is at ten
live sessions (~9 Nov). Only a description on Mon 2 Nov is given here, labelled as such.

## Rule (price only, 5m closed bars)
- Walls per stock-day: CPR band (BC–TC), P, R1–R3, S1–S3, PDH, PDL (previous session's continuous H/L/last),
  previous week's WP/WR1/WR2/WS1/WS2, the opening range high/low (first 15 min, known from bar 3), and the round
  numbers at `cpr_retest.round_step` within the session's range. Tolerance `tol` = 0.1 × ADR(20, guarded), break
  margin `brk` = 0.5 × tol (the live constants). A wall is a **confluence** when another wall sits within `tol`.
- Long side at wall ℓ (short mirrored): (1) a close above ℓ + brk from a state not above it (a gap-open above
  the wall is not a break; the first bar can only set the state); (2) the first later bar k within 36 bars whose
  low ≤ ℓ + tol; **held** if its close ≥ ℓ − brk (CPR: close ≥ BC, the live rule); (3) entry at the first close
  above the retest bar's high within 6 bars (`CONFIRM_BARS`), entry bar ≤ 14:30 (the live cutoff); (4) stop =
  retest bar's low − tol; walls beyond the entry from `cpr_retest.walls` **with** round numbers (the owner named
  them); exit `wall_then_trail` (`ExitPlan`), the session's last decision bar = the 15:10 bar (bars from 15:15 are
  the closing auction and are dropped, as live since 1 Nov).
- **With the move:** the break bar's close is above (below) the day's open. Primary = with the move; the
  against-the-move set is reported beside it.
- One trade per stock-day-side: the earliest confirmed retest (the first wall that flips and holds). All flips are
  kept in the table for the by-wall split.
- Control: the same breaks entered **at the break bar's close** with the stop at that bar's low − tol (no retest):
  does waiting for the pullback add anything over buying the break?

## Data and bar
Development window 2024-01-01 → 2025-09-30, the 5m history cache, 219 F&O names less the index roots; the holdout
(2025-10 → 2026-10) is spent and is not read. Net = gross − 15 bps (the record's cost line; 13 at ₹1L).
Reported: n, mean gross, mean net, win %, median risk, day-clustered t (daily means), by year, by side, by wall
type (CPR / pivot / PDH-PDL / weekly / opening range / round / confluence), by entry clock, and the gap-and-go
subset (gap ≥ +2 % longs, the RELIANCE shape).
**Ships to SHADOW only if** the primary set's net > 0 with day-t ≥ 2.5 and both years positive net. Otherwise the
generalized retest is recorded beside the CPR one and nothing changes in the engine.
Then the same rule on the live archive days (27, 29, 30 Oct, 2 Nov; 28 Oct lost) as a demonstration — not evidence.

Script: `docs/research/scripts/2026-11-03-level-retest/level_retest.py`. Written before the first run.

## Addendum (3 Nov 03:45 IST, before running): can a ranker pick two a day? (owner: "can we do this anyway from algo", on the hindsight best-two row)
H3: a model trained on what is known at the entry — side, wall type, confluence, stop distance, first-wall distance and
room in R, entry clock, gap at the open, the stock's move from the open at entry — ranks the day's confirmed flips well
enough that its top two per day beat two picked at random. Data: the row-23 dev-window trades (first flips with the
move). Fit on 2024, pick on 2025, and the reverse; a gradient-boosted classifier of "net > 0" (or logistic regression if
sklearn is absent), the top two per day by predicted probability. Reported: the top-two's mean net per day against 200
random two-a-day draws, the all-trades mean, the top-decile lift, and the day-t of (top-two − random). **Bar: the top
two's mean net per day > 0 with day-t ≥ 2.5 in BOTH directions and above the random mean by ≥ 10 bps a day.** Below that,
"the algo cannot choose" stands and the live book keeps first-two with the evening scoreboard. The four live days are
only described (what the hindsight best-two had in common), not fitted — four days cannot train anything.

## Addendum 2 (3 Nov 03:55 IST, before running): H4 — the price action at the entry (owner: "why we missing any signal use data and find real insight")
H3's ranker saw only the setup's geometry. H4 adds what the owner reads on the chart and across the tape at the entry bar:
the stock's move from the open (in ADR and in %), its rank against every other stock's move at that bar (signed the
trade's way), the breadth at that bar (share of the universe moving the trade's way), NIFTY's move from its open, the
day's range so far in ADR, where the close sits in the day's range, the break bar's volume against the day's earlier bars
and its range in tolerances, the retest bar's volume and range, the bars from break to retest and from retest to
confirmation. Same data (98,259 first flips with the move), same bar as H3 (top two a day > 0 with day-t ≥ 2.5 in both
directions and ≥ 10 bps/day over random); one-way splits of every new feature are reported too (pooled, both years), with
the caution that a split that holds in one year only is noise.
