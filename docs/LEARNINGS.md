# Learnings — the rules this codebase enforces

Distilled from a year of operating a multi-strategy paper-trading stack on Indian markets, and
specifically from the 23 failure patterns catalogued in `kotsin-box/strategy-docs/_PATTERNS.md`.

Each rule names the failure it prevents and **how this repo enforces it**. A rule that lives only
in a document is not a rule — most of these were already understood in the old stack and still
happened.

| # | Rule | The failure | Enforced by |
|---|---|---|---|
| R1 | Config is typed and closed | a key nothing reads, masked by a default that happens to match. `fudkii.trigger.bb.period` was bound into a field that appeared in exactly one place — a log line — while the calculator used a hardcoded 20 | `config.py`: `extra="forbid"` for `.env`, `assert_no_unknown_env()` for the process env; strategy parameters are frozen dataclasses, and a test changes one and asserts the output changes |
| R2 | No sentinel caps | `selection.top.n=999` disabling a stage named "Top-N selection" | caps are `int \| None`; `None` means OFF and the boot banner prints `OFF` |
| R3 | Every gate declares what a missing input means | CAN2 traded on absent OI while FUDKOI never fired, on the same missing input, and neither alerted. During a 23-day OI outage one book was dark and the other unconfirmed | `strategy/gates.py`: `Gate(on_missing=FAIL_OPEN \| FAIL_CLOSED)`; missing inputs are counted separately from rejections |
| R4 | Any conjunction of gates counts which one is binding | `NSE_BB_30` had six mandatory gates and **two** lifetime signals; nothing recorded which gate did the killing, so a strangled strategy was indistinguishable from a selective one | `GateStats` + the `rejections` table + the binding-gate histogram on the Strategies page |
| R5 | Record every candidate, not only the winners | "what did the filter reject, and would it have won?" was unanswerable. The one book that kept a pass-and-fail audit is the only one whose config bug was provable in a single query | rejections are persisted with the same weight as signals |
| R6 | Strategy keys are an enum, never a string | `"HOTSTOCKS".equals(strategy)` silently excluding `HOTSTOCKS_MOMENTUM`, and substring matching collapsing the two. Both directions caused live bugs | `strategy/keys.py`; removing a key breaks every reference at import time |
| R7 | Delete the consumer with the producer | a wallet funded, and a strategy advertised, for six weeks after its code was deleted | wallets are created from `ALL_KEYS`, so there is nowhere for an orphan to live |
| R8 | Mode is state, not an environment variable | a restart without `CAN2_LIVE=true` left a book paper-trading for eight weeks; the only tell was one line in a boot banner | control-table row, `armed_until` with expiry, mode on every UI screen, Telegram echo on change |
| R9 | The broker is the source of truth for positions | positions held in an in-memory dict, orphaned by every restart while the log looked normal | `exec/reconcile.py` on boot and on a timer; any mismatch freezes entries; positions are persisted on every change and re-hydrated at boot |
| R10 | One owner per rule | a dashboard with `TIME_STOP_DAYS=3` next to an executor with `maxhold.days=5`, and neither code nor document naming an authority | `risk/exits.py` is the only module that may decide an exit or move a stop |
| R11 | Never pass a symbol where a scrip code is expected | a strategy that never fired in its entire life because a universe function returned symbols into a numeric-code lookup, printing `written=0 skipped=243` every morning | `Instrument` carries both and is the only thing passed around; nothing takes a bare string |
| R12 | A placeholder constant is a bug with a due date | `avg20d = 0.0 (TODO)` making a regime detector return `UNKNOWN` forever, and two named setups permanently dead | there are no `TODO`-shaped constants in this repo; an unknown contract size **declines the trade** rather than defaulting to 1 |
| R13 | No calibration on n < 30 | thresholds tuned to preserve two remembered trades | strategy docs carry the sample size; the PR template asks for it; parameters without one are marked as inherited, not validated |
| R14 | Model costs before believing an edge | the best exit rule found returned +0.118%/trade gross against a 0.299% round trip. Every stop, trail and target variant tested made it worse; entries were the binding problem | `risk/costs.py` is used by paper fills, live accounting and research alike, and sizing declines a trade whose charges exceed 35% of the move to T1 |
| R15 | Exposure is aggregated by underlying, across books | one trigger opening four funded positions, invisible in any single strategy's sizing | `risk/exposure.py` buckets by underlying symbol across every wallet |
| R16 | Paper fills walk the real book | stop exits booked at exactly the stop price, so the entire live ledger was optimistic by the gap | `exec/paper.py` walks the 20-level ladder with a 10% ceiling and records slippage and book age on every fill |
| R17 | Alert on transitions, not on conditions | one diagnostic line printed 3,296 times over 25 days; another 7,946 times in a day. Coverage was never a logging problem — it was a reading problem | Telegram sends are keyed and rate-limited; health needs three consecutive failures before it counts |
| R18 | A health probe must not treat slow as dead | a 10 s database probe timing out on a thrashing core, read as "dead", restarting the database every 3 minutes, emptying its cache, making the next probe slower | `ops/health.py` requires `consecutive_required` observations, and the count is on the System page |

## Economics — the number that decides everything

On NSE cash at **₹33,000 a position**:

* round trip **0.299%**, of which **81% was flat brokerage** (₹40/order × 2);
* break-even needed roughly **₹1.32 lakh** of position;
* the best exit found was "flat at the close, no stop management", at **+0.118%/trade gross** —
  every stop, trail and target rule tested made it worse;
* OTM calls on the same signals lost **−5.47%/trade**.

Two things follow, and both are encoded rather than remembered:

1. **Fewer, larger positions.** A fixed cost does not scale down. `risk/limits.py` defaults to a
   ₹1 lakh position budget and five concurrent positions, not twenty at ₹33,000.
2. **Entries, not exits.** Time spent on exit rules was measurably wasted. The gate counters exist
   so the next round of work is aimed at the entry funnel, where the evidence says the problem is.

## Operations

* Five JVMs with heaps sized for a 30 GB box, relaunched by a watchdog onto a 2 GB box inside
  30 seconds. Hence: one process, a memory limit in the unit file, and no restart on one failed probe.
* `pkill -f name` kills the shell that runs it. Kill by PID; wait on an artefact.
* `ps | grep -c pattern` over-counts, because a shell snapshot carries old command text. Verify by
  which PID holds the port.
* A 5-minute candle label is the bar's **start**. The decision happens on the forming bar, so the
  fill lands minutes later at a different price. Record both the bar label and the decision instant.
* Never reimplement maths the engine already has for a replay — hand-rolled replays erred between
  −80% and +185%. The backtester must import the live code.
* Trade statistics cluster by day. Always run a within-day permutation test before believing a
  split, and say plainly when n is too small.
