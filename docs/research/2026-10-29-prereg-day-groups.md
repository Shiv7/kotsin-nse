# Pre-registration: group the days by NIFTY's previous day and first hour against fixed levels, then pick the algo per group

Written 2026-10-29 02:05 IST, before the run. Owner's ask (29 Oct 01:50–02:00): "each day is not unique, club each nifty
day from his previous day then based on opening first 1 hour watch along and then try to put in group and those history
data best algo we will apply"; "optimisation around fibo pivot known stable data points and price based action";
"split data 80 20 learn 80% data pattern in remaining 20% test with RL so our learning also improved".

## What is fixed before the run

**Context (known at 10:15 IST, NIFTY only, all relative to fixed levels):** previous day's return and range in ADR, where
it closed in its range, its close against its own CPR; today's levels from yesterday (P, TC, BC, R1, S1, PDH, PDL, the
38.2/50/61.8 fibs of yesterday's range, the weekly pivot); the gap in ADR; where the open sits (above TC / inside the
CPR / below BC; above PDH / below PDL); the first hour (09:15–10:15, 12 bars): its range in ADR, the move from the open
in ADR, where 10:15 sits in that range, whether it broke PDH or R1, PDL or S1, whether it filled the gap, the 10:15 close
against the CPR and its position in yesterday's fib grid, the number of tests / rejections / breaks of the levels; and the
market at 10:15: the share of the 214 stocks above their previous close.

**Groups:** k-means, k = 5, on the standardised context, fitted on the training days only; and, readable, open location
(above / inside / below the CPR) × first-hour direction (up ≥ +0.25 ADR / flat / down ≤ −0.25 ADR).

**Actions (one per day per group):** flat; the live CPR retest entry long / short; the through-bar entry long / short;
the IB15 break long / short; the 10:15 movers basket (top decile long / bottom decile short to the close); NIFTY first-hour
continuation / fade to the close (futures, 5 bps). Stock trades net = gross − 15 bps. A day's reward for a stock action is
the mean net bps per trade that day (0 when it has no trade).

**Split:** the development window's days in time order; the first 80 % train, the last 20 % test (and the reverse as a
check). The holdout (2025-10 → 2026-10) stays untouched.

**Learning (the bandit):** per group, the action with the highest mean reward on training days, provided it traded on
≥ 20 of them with t ≥ 2; otherwise flat. The policy is then applied, frozen, to the test days.

**Decision rule:** the policy earns a place in SHADOW only if on the test days its mean reward per day is > 0 with
t ≥ 2, and it beats the best single action applied everywhere on the same test days. Otherwise: day grouping on these
contexts does not choose the algo, and the line closes. Expected: fail (the 2026-10-23 day-type study predicted the type
right 44 % of the time and traded it at −3.6 net; nothing on NIFTY's path has yet predicted the stocks' afternoon).

## Run log
- 02:20 IST, one run (`scripts/2026-10-29-label-variants/daygroups.py`, results beside it): 433 NIFTY days with the
  stocks' instances, train 346 (2024-01-01 → 2025-05-28), test 87 (2025-05-29 → 2025-09-30); the reverse split as a check.
  Every single action loses on its own across all days (net per traded day: retest −15 / −17, through −14 / −15,
  IB15 −22 / −17, movers basket −13 / −17, NIFTY continuation −3.2, fade −6.8).
  **k-means groups (fitted on train):** (0) the bounce after a big down day — yesterday −1.1 ADR, open under the CPR,
  first hour up, breadth 61 %; (1) gap-down and down through PDL/S1, breadth 35 %; (2) open above the CPR, first hour
  down back inside it; (3) gap-up above the CPR, through PDH/R1, breadth 68 %; (4) the same after a big up day.
  **The bandit learned flat in 4 of 5 groups** (no action reached t ≥ 2 on ≥ 20 training days) and in group 0 the
  movers basket long (+36.6 train, t 2.2). **On the test days the frozen policy made +3.6 bps a day, t 1.15, trading 14
  of 87 days (+22.6 on those)** — below the bar. The reverse split learned flat everywhere; the readable open × first-hour
  groups learned flat everywhere in both splits.
- **Verdict (by the rule written above): fail.** Grouping the day by NIFTY's previous day and first hour against the
  fixed levels does not pick an algo that pays; nothing ships. The one lead — buying the 10:15 top movers on the bounce
  day after a selloff — rests on 47 training and 14 test days and is recorded here, not acted on.
