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
* The session before a monthly expiry, the tracked option expiry rolls to the next month while
  only the nearest future is on the quote channel: on 2026-10-26 every stock's calls/puts legs read
  as missing from 09:15 to 15:30 (`fno_snapshot` skipped the expiry because "its" future had no
  price). The strikes are now priced off the front future when the expiry's own has no quote, and
  the snapshot says so in `missing`. Check the F&O page the day before every expiry.
* **A sleeping laptop is a frozen engine that reports "ok".** On 2026-10-27 the laptop hibernated at 05:06
  (battery 1 %) and woke at 09:19:35; health said ok, the feed reconnected, and every stock's first 5m bar
  was two ticks with the first four minutes' volume in it. The engine re-backfills only when a previous
  close is missing, and the 1m sweep repairs only buckets that exist. Before every session check
  `pmset -g log | grep -E "Entering Sleep|Wake from"` and the first 1m bar's time; charger in overnight.
  The fix to build: re-run today's 1m backfill when the housekeeping loop finds a wall-clock gap.

## Research
- **A conditioning variable must be known at the time of the trade, not at the end of the day.** The day-read sweep (2026-10-26) first showed S1/S2 holds worth +30 to +57 bps "when few stocks tested S1/S2 that day" (t 9). The day's count is known only at the close — few tests all day means the market did not fall later. With the count known at the time it was −3 bps raw, −18 after costs. Same trap as "never returned to the CPR" (+100 bps, t 9–14): a day that never came back is a day that trended. Check every filter for hindsight before reading its number.
- **A level touch that "held" is a 30-minute fact.** Across 90,452 stock-days a held pivot gave way within the hour a third of the time and by the close half (the CPR band: 19 % / 39 %); 35–47 % of breaks were back through within 30 minutes. 5m legs are retraced 89 % (median) by the next one, 30m legs 66 %. Read structure on 30m or above; treat a 5m hold as an alert.
* **"Find the trending stocks and trade the pattern there" is a filter, and filters are tested like everything else.** Thirty-three definitions of trending known at the entry (daily averages, EMA stack, returns, relative strength, ADX, 15m/1H SuperTrend, the move so far, the cross-section rank) left the held-CPR-retest trade negative net in every subset, and the stocks that had moved the most by the entry were the worst subset: what has already moved has the least left. A pattern's good days are remembered; the question is always whether anything known at the time picks them (2026-10-27, note §11).
* **A live strategy must be the research's function on the bars so far, and replayed to prove it.** `strategy/cpr_retest.sequence` is the sweep's sequence code called on each prefix of the day; replaying twelve stocks' history through it reproduced the sweep's rows on 10,304 symbol-day-sides with zero differences and found every confirmation bar at its own prefix. Without that check a shadow result cannot be read against the backtest (2026-10-27).
* **The live strategy decides on the feed's bars, the research was on the exchange's, and the sequence is a chain of thresholds.** Shadow day 1 (2026-10-27): the feed-built 5m bars differed from the exchange's resampled 1m on 57 % of 15,849 bars, by 1.7 bps on average — and replaying `sequence` on the exchange's bars changed 13 of 434 symbol-sides, including 2 of the 6 confirmations (not fired there) and 1 new one. A pattern within noise on exchange bars is a different coin on feed bars; replay the day on the exchange's bars every evening before reading a shadow number.
* **At ₹25,000 a trade the flat brokerage is the strategy.** The measured 5paisa model charges ₹103 a round trip at ₹25,000 (41 bps; ₹80 of it the ₹40-an-order brokerage), ₹112 at ₹50,000 (22 bps), ₹130 at ₹1,00,000 (13 bps). A pattern worth +1 to +25 bps gross cannot be read from a ₹25,000 book's net: read gross bps, and size by what the costs allow.
* **The index roots carry an NSE_EQ leg and will walk into any "equity" book.** NIFTY and BANKNIFTY (scrip codes 9999…) are kind EQUITY in the catalogue; shadow day 1 booked both as ₹25,000 of stock, qty 1. Filter on `INDEX_ROOTS`, not on kind.
