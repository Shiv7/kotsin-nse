# close-1514: engine A changes (operator decisions of 17 Oct 2026)

This branch is `close-1514`, cut from `0af8c0f` in a scratch clone. Nothing has been pushed or deployed. NSE only: MCX is untouched.
Line numbers below are for the branch head. Paths are relative to the repo root.

| Decision | Summary |
|---|---|
| R1 | Raw NSE ticks are recorded to 15:40 (to 15:41:59). The session close stays 15:30. |
| R2 | Every NSE position is flattened from 15:14, selling into the bid at once, with a retry every 2 s. Anything still open at 15:19 is force-filled on paper and alarmed. Anything open, or any exit filled, at or after 15:20 alarms. |
| Entry cutoff | The last NSE entry minute is 15:10, inclusive, for every book. A resting entry is cancelled at the cutoff. |
| 14:45 bar | Its triggers are decided at 15:15. They are recorded and shown on every trigger list, clearly labelled. They are never traded and never carried. |

---

## 1. R1: record ticks until 15:40

| File:line | Change | Why |
|---|---|---|
| `backend/kotsin_nse/ops/fulltape.py:44` | `WINDOW` changed from `("09:14", "15:31")` to `("09:14", "15:41")`. The module docstring (lines 1-4) and the comment (lines 40-43) say so. | The full tape is the only cut on raw NSE recording. OI (`Engine._on_oi`), the tick tape (`ops/tape.py`) and option-quote sampling have no time window. The check is a minute compare, so recording now runs to 15:41:59. |
| none | `SESSIONS[...].close` stays 15:30. The aggregator, bars, the 15:15 closing-auction bar, the carry and all signal logic are unchanged. | As decided by the operator. |

**Jobs that run just after recording ends: none moved.**
- `BAR_AUDIT_HM = "15:40"` (`engine.py:275`) is unchanged. `_audit_bars` → `BarReconciler.audit_day` (`bars/verify.py:309-333`) reads only two things:
  - the engine's bar store, built by the aggregator, which still closes at 15:30;
  - the broker's REST candles.
- It never reads anything the recorder writes (`archive/tape_full`). The bars it audits are final at 15:30, so 15:40 is still after its inputs.
- `daily_refresh_hm` (15:45) is a REST refetch of daily bars, so it also reads nothing the recorder writes.
- Nothing in the engine, `deploy/` or `scripts/` reads `tape_full` on a clock.

## 2. R2: flatten from 15:14, out before 15:20

### 2.1 The clock

| File:line | Change | Why |
|---|---|---|
| `backend/kotsin_nse/market/session.py:62-65` | NSE_EQ, NSE_FO and NSE_IDX `force_flat` changed from 15:20 to 15:14. MCX stays 23:20. The comment (lines 52-61) and the field docstring (line 42) explain why. | Single source of truth. The backtester, RL env and tape replay follow it, which the operator accepted. |
| `market/session.py:70`, `:73` | New `NSE_FORCED_FILL = time(15, 19)` and `NSE_OUT_BY = time(15, 20)`. | The 15:19 forced fill and the 15:20 deadline. |
| `market/session.py:183`, `:188` | New `past_nse_forced_fill()` and `past_nse_out_by()`. Both are always False for MCX. | The engine reads the deadline through these. Tests monkeypatch them; see §5. |
| `backend/kotsin_nse/engine.py:353` | `NSE_LAST_ENTRY_HM` changed from `"15:15"` to `"15:10"`. The comment is at lines 346-352. | The entry cutoff. Inclusive minute, so 15:10:59 is allowed. It matches `settings.live_entry_cutoff_ist` and the gateway's 15:10. |
| `engine.py:355`, `:358` | `LAST_ENTRY_HM = {}` and `FORCE_FLAT_HM = {}`. RT-Y-F's own times are removed: last entry 15:22 and flatten 15:24. The `FORCE_FLAT_HM.update(...)` that copied 15:24 onto its `_SE`/`_SA` mirrors is deleted. | Every NSE book uses 15:10 and 15:14. The dicts and resolvers stay, now empty, so `_past_force_flat` always goes through `past_force_flat`. |
| `engine.py:361-368` | New constants: `NSE_FORCED_FILL_HM`, `NSE_OUT_BY_HM`, `EOD_RETRY_S = 2.0` and `NSE_EOD_AT_ONCE`. | Used by the logic below. |
| `engine.py:375` | `EOD_PLAN_HM` changed from `"15:15"` to `"15:13"`. It is still record-only. Its verdict now reads "flatten at 15:14 as now", because it comes from `_force_flat_hm`. | So the plan still comes before the flatten. |
| `engine.py:2832-2846` | Docstrings of `_force_flat_hm` and `_past_last_entry`. | Text only. |

### 2.2 "First avenue": sell into the bid at once and retry every 2 s

| File:line | Change |
|---|---|
| `engine.py:6174` `_nse_flatten()` | Returns true for an NSE position's exit when either: the reason is EOD; or the time is at or after its 15:14 force-flat, whatever rule fired first that second (a halt, a stop, a target rung). MCX always returns false. |
| `engine.py:6345` (`_place_exit_limit`) | When `_nse_flatten` holds, the exit takes the existing at-once path (`_exit_at_once` → `_exit_cross`). That path sells into the bid through the depth for every lot, with no 10 s walk from the mid. With no bid, it falls through to `_exit_market` as before. |
| `engine.py:6146` (`_exit`) | From the flatten onwards, an exit resting since before 15:14 (a trail, a halt walking from the mid) is replaced by the at-once sale. |
| `engine.py:6183` `_exit_retry_s()`, used at `:6436` (`_exit_cross`) and `:6484` (`_exit_market`) | A refused NSE flatten retries every `EOD_RETRY_S` (2 s). Every other exit, and MCX, keeps the `min(2**n, 30)` backoff. Exit rejects never count towards the breaker (`gateway.py:193-204`), so faster retries cost nothing there. |
| `engine.py:5996-6001` (`_manage_positions`, no-LTP branch) | Before, a contract with no last price was skipped entirely, including the flatten. From 15:14 it now tries an EOD exit with the bid from `_touch`, if any. |
| `exec/resting.py:28-30` | Docstring only. The pure `urgent_stop` policy is unchanged; the NSE rule lives in the engine. |

### 2.3 The 15:19 forced paper fill

| File:line | Change |
|---|---|
| `engine.py:5977-5978` (top of the `_manage_positions` loop) | Runs before the no-quote skip, so a contract that never printed is also covered: `if past_nse_forced_fill(...) and await self._force_eod_fill(pos, now): continue`. |
| `engine.py:6224` `_force_eod_fill()` | Steps, in order:<br>1. Skip LIVE books' positions (see below).<br>2. Skip if an exit is in flight this tick; the next tick tries again.<br>3. Take off the resting exit and the resting target sells.<br>4. Take one REST snapshot through `_refresh_exit_book`. It is rate-limited to once per 2 s per contract and capped at 3 s.<br>5. Read the price from `_last_known_exit_price` (`:6199`), in this order: the last known bid, which is the newer of the depth book's and the quote's bid, of any age; else the last known LTP; else the last mark; else the entry. A short position reads the ask.<br>6. Fill on paper through `Gateway.force_paper_exit`.<br>7. Book it through the normal `_book_exit`, so the wallet, ledger and trade are all updated.<br>8. Raise the alarm. |
| `exec/gateway.py:297` `force_paper_exit()` | Fills an EXIT whole at the given price, with no matcher, no book walk and no age limit. The order is `FILLED` and carries the note. It raises if the order is not an exit. |
| Flag, not a new exit reason | The reason stays `ExitReason.EOD`. The forced fill is marked in five places:<br>- the order `note`: "forced fill 15:19, stale book — not out after the 15:14 flatten (`<the matcher's last refusal>`); paper-filled at the last known bid X, N s old";<br>- the order id gets an `F` suffix (`exit_client_order_id(..., forced=True)`, `engine.py:7220`);<br>- `exec.eodForced` in the order audit;<br>- `exec_log["eodForced"]` on the position;<br>- `eod_forced` on the trade row (`engine.py:6553`). |
| LIVE and SHADOW | These positions are never paper-filled. `_fills_on_paper` (`:6191`) returns true for PAPER in every book, and for LIVE in every book except `LIVE_BOOKS`. A position that is not paper-filled raises `eod.not_forced` instead. A real position must be closed at the broker. SHADOW places nothing. |

### 2.4 Alarms at 15:20 and the health check

| File:line | Change |
|---|---|
| `engine.py:6281` `_eod_alarm()` | Raised once per position and kind (`_eod_alarmed`, `:846`). Each alarm does four things:<br>- writes a structured `log.error(kind, …)`;<br>- writes a ledger event;<br>- sends a Telegram message;<br>- adds a row read by the health check.<br>Kinds:<br>- `eod.forced_fill`;<br>- `eod.still_open`, for an NSE position open at or after 15:20:00 (`:5974`);<br>- `eod.exit_after_deadline`, for an NSE exit filled at or after 15:20:00 of any reason, raised in `_book_exit` (`:6543-6548`);<br>- `eod.not_forced`. |
| `engine.py:6302` `_eod_check()`, added to `health_snapshot` at `:7071` | New `nse_out_by` health check. It is red for the rest of the IST day after any alarm, and while any NSE position is open after 15:20. Its detail gives the counts, the last four alarms and the rule. |
| `engine.py:6516` | `_exit_fail_note` keeps the last refusal for each position, as the forced fill's cause. It is cleared on a full fill. |

### 2.5 Entries never fill after the cutoff

| File:line | Change |
|---|---|
| `engine.py:3436-3439` (`_advance_one`, entry branch) | An NSE entry still resting after the last entry minute is cancelled through `_entry_missed`, with "cancelled at the entry cutoff — …". It is never filled afterwards, and its hold is released. |
| `engine.py:2622-2625` (`_enter_book_once`) | For a gated book, the minute is checked again after `_book_gates`. The volume read can wait on the broker, so a book decided at 15:10:58 could otherwise send its order at 15:11:02. |
| unchanged | `exec/gateway.py:71` and `config.py:234` (15:10). Operator TAKE goes through `_enter_book_once`, so it is refused after 15:10. |

### 2.6 Text updates (no change to behaviour)

- `api/review_peers.py:179-182`: "day-end 15:20" becomes "day-end flatten". It has no time in it, so past trades flattened at 15:20 or 15:24 are not relabelled 15:14.
- `api/shadow.py:982` and `:1017`: these now say 15:14.
- `api/review.py:42` comment.
- `committee/pipeline.py:28`: the prompt now says flatten from 15:14, out before 15:20, and the last entry is 15:10.
- `risk/exits.py:140`, `research/backtest.py:554-558` comments. The 15:14 flatten now falls inside the 14:45 bar.
- `docs/BOOKS.md:28-46` (the clock), `:149-150` (the exit table), `:194` (the 15:13 plan).
- `docs/PIVOTS.md:99-100`.

## 3. 14:45-bar triggers: recorded, never traded

| File:line | Change |
|---|---|
| `engine.py:2297-2300` (`_decide` dispatch) | A new branch after the carry: `elif (late := self._after_entry_cutoff(sig)) is not None:` → `_record_after_cutoff`. |
| `engine.py:2854` `_after_entry_cutoff()` | True when the trigger's bar is decided after `NSE_LAST_ENTRY_HM` and no later than the close, and it is not the close's own bar. In practice that is the 14:45 bar, decided at 15:15. It reads the bar time, not the wall clock. MCX is excluded. The reason text is "after the 15:10 NSE entry cutoff — recorded only: the 14:45 bar is decided at 15:15, and every NSE position is flattened from 15:14; not traded, not carried". |
| `engine.py:2870` `_record_after_cutoff()` | No strike is chosen and nothing reaches the broker or the order path. The steps:<br>- A published trigger: its row is written as `RECORDED_ONLY` with the reason, and its ENTRY alert is adopted as for any trigger.<br>- An unpublished (graded F) trigger keeps its `NOT_PUBLISHED` row.<br>- Every book that would have been offered the trigger records an `rt_twin.skipped` with `gate="entry_cutoff"`. That is the same gate a book's own refusal after the minute uses, so the Shadow page and day-book summaries count it the same way. For a published trigger those books are RT-X, RT-N, RT-Y, CT-X and CT-Y; for an unpublished one, RT-Y-F, RT-Y and RT-X.<br>- A `cutoff.recorded` event is written.<br>- Nothing is queued for the carry. |
| `engine.py:4196`, `:4206`, `:4359`, `:4413` (`book_cards`) | New card state `RECORDED_ONLY`, for every book except FUDKII and RT-N on an unpublished trigger, which stay `NOT_PUBLISHED`. The TAKE button is disabled with the reason, and there is a new `recordedOnly` field. |
| `api/daybook.py:398-399` | Day-book labels for `RECORDED_ONLY` and `PAST_ENTRY_CUTOFF`. |
| `frontend/src/components/BookCards.tsx:55`, `:143-144`, `:624` | New type field, a `RECORDED ONLY · after the entry cutoff` chip, and the card's headline "Recorded only, not traded — `<reason>`". |
| `frontend/src/pages/Signals.tsx:205-208`, `:265` | The decision badge reads `RECORDED ONLY · after cutoff`, in blue, with the reason as its tooltip. |

**Why the UI was changed, and what it already showed.**
- `/api/signals` (`api/routes.py:780-785`) returns every signal row except `NOT_PUBLISHED`. `/api/books/{book}/cards` (`engine.book_cards`) builds one card per FUDKII signal row of the day. So a refused trigger was already listed:
  - as `PAST_ENTRY_CUTOFF` on the FUDKII row;
  - as `SKIPPED`, with its reason, on the other books.
- Two things were wrong with that:
  - the label did not say "recorded only";
  - the trigger first went through strike selection, so a broker read could replace the cutoff row with `NO_INSTRUMENT`.
- The changes above give the trigger its own state and a clear label.
- The list shows it in four places:
  - `/api/signals`: the published row;
  - `?unpublished=true`: graded-F triggers, which stay off the default list, as before;
  - `/api/fudkii/today`;
  - `/api/alerts`.

## 4. Tests

### Changed (they pinned the old times or behaviour)

| File | Change |
|---|---|
| `backend/tests/test_entry_cutoff_and_carry.py` | Docstring. Old 15:15/15:22 tests are rewritten as `test_every_book_enters_until_1510_and_mcx_is_untouched` (`:64`), now including RT-Y-F and its mirrors. Old 15:20/15:24 tests become `test_every_nse_book_flattens_from_1514_out_before_1520_and_mcx_at_2320` (`:77`), which adds guard checks: cutoff < flatten < forced fill < out-by, cutoff ≤ the live gateway's, the empty dicts, and EOD_PLAN 15:13. `test_past_1510_every_book_refuses_and_the_graded_f_shadow_too` (`:97`). |
| `backend/tests/test_urgent_stops_depth_eod_plan.py` | Docstring. The plan now runs at 15:13:05 with verdict "flatten at 15:14 as now"; `EOD_PLAN_HM == "15:13"`; nothing is recorded at 15:12:59. |
| `backend/tests/test_mae_and_stop_execution.py` | The NSE EOD exit now sells at the bid at once, where it used to rest with a 10 s deadline. Docstring updated. |
| `backend/tests/test_tape.py:330-351` | The replay is forced from 15:14 (15:13 not forced, 15:14 forced). |
| `backend/tests/test_rl.py:76-82` | The 14:45 bar contains 15:14. |
| `backend/tests/test_depth_check.py` | In two engine tests, `past_force_flat` is pinned to False. Their clock reads 15:30, past the flatten, which now sells at once and would close the position in the same pass that fills it. These tests are about the depth check, not the close. |
| `backend/tests/conftest.py:36-62` | The docstring is updated. There is a new autouse fixture, `_nse_deadline_off`, which pins `past_nse_forced_fill` and `past_nse_out_by` to False. It follows the existing `_entry_window_open` pattern, because the suite runs at all hours. The new `nse_deadline` fixture restores the real rule for the tests of it. `past_force_flat` itself is left as before. |

### Added

| File | Tests |
|---|---|
| `backend/tests/test_nse_close_1514.py` (new) | 13 tests:<br>- the clock: 15:14, 15:19, 15:20, MCX untouched;<br>- the forced order id;<br>- at 15:14 every book is sold at the bid in the same second, including RT-Y-F, the RT-Y-F_SE mirror, CT-Y and RT-Y-W1_SA, with no alarm and the health check green;<br>- MCX keeps its walk and backoff, and only the NSE flatten retries every 2 s;<br>- from 15:14 a halt, or a trail resting from before, sells at once;<br>- a stale book is retried at +2 s and +2 s, not +4 s;<br>- a contract with no LTP sells its book bid at 15:14;<br>- the 15:19 forced fill at the last known bid: price, age, flag, note, `F` id, trade flag, alarm, health check red, wallet released;<br>- fallbacks to the last price, then the entry;<br>- LIVE books are not paper-filled, SHADOW places nothing, and `eod.not_forced` and `eod.still_open` each fire once;<br>- at 15:20, `still_open`, forced fill and `exit_after_deadline` all fire;<br>- a stop filled at 15:19:59 is in time, and MCX at 15:25 gets no alarm. |
| `backend/tests/test_entry_cutoff_and_carry.py` | Four new tests:<br>- `test_an_entry_whose_gates_ran_past_the_cutoff_is_not_sent` (`:124`);<br>- `test_only_a_trigger_decided_between_the_cutoff_and_the_close_is_recorded_only` (`:151`);<br>- `test_a_1445_trigger_decided_at_1515_is_recorded_and_listed_never_traded_nor_carried` (`:167`). It runs through the real `_decide` dispatch and checks: no strike chosen, no position, no resting order; the rows, events and skips; no carry; the cards on 6 books and the RT-Y-F shadow; `/api/signals`, `?unpublished=true` and `/api/fudkii/today`; the alert row; and that an operator TAKE is refused;<br>- `test_a_resting_nse_entry_is_cancelled_at_the_entry_cutoff_never_filled_after_it` (`:244`). |
| `backend/tests/test_full_tape.py:37` | `test_fo_ticks_are_recorded_to_1540_after_the_1530_close`: 15:31:30, 15:35:00, 15:40:59 and 15:41:59 are recorded; 15:42:00 is not. |

### Results, on the branch head, running against the copy

`python -c "import kotsin_nse; print(kotsin_nse.__file__)"` → `…/scratchpad/close1514/engA_work/backend/kotsin_nse/__init__.py`

| Check | Command | Result |
|---|---|---|
| Tests | `PYTHONPATH=. pytest -q` (backend) | **1080 passed**, 120 warnings. Baseline at `0af8c0f`: 1063 passed. |
| Ruff | `ruff check kotsin_nse tests` | **All checks passed!** |
| Import contracts | `lint-imports` | **5 kept, 0 broken** |
| Frontend build | `npm run build` (`tsc --noEmit && vite build`) | Built; `frontend/dist` is a real directory |
| Frontend tests | `npm test` | **7 pass, 0 fail** |

**Time-of-day sweep (scratch-only pytest plugin that shifts `time.time()`).**
- At simulated 10:00, 14:50 and 15:16 IST, both the baseline clone and this branch were fully green: 1063 and 1080 passed.

## 5. Decisions I had to take

1. **"First avenue" covers more than the EOD rule.** At or after 15:14, any exit of an NSE position sells into the bid at once and retries every 2 s. That includes an exit from a rule that beat EOD to the position, such as a halt or a rung. An exit resting from before 15:14 is replaced.
2. **The forced fill keeps reason `EOD` and adds a flag**, not a new `EOD_FORCED` reason. Existing analyses compare `exit_reason == "EOD"`: `committee/forensics.py:240`, `api/review_peers.py`, `api/review.py:453`. They keep counting these trades as day-end exits. The flag is on the order (note, `F` id, `eodForced`), the position and the trade.
3. **Forced-fill price.** The newer bid of the depth book and the quote, of any age; then the last LTP; then the last mark; then the entry. One REST snapshot is taken first. Taking the entry as a last resort gives zero gross P&L. It happens only when nothing ever priced the contract, and the alarm says so.
4. **LIVE and SHADOW are never paper-filled.** A LIVE book's lots are at the broker, and SHADOW places nothing. Both get `eod.not_forced`, and still `eod.still_open` at 15:20, to meet "any NSE position open at/after 15:20".
5. **Alarms fire once per position and kind.** The health check stays red until the IST day ends. The health monitor's usual three consecutive reads apply.
6. **A trigger is "recorded only" based on its bar, not the wall clock.** The 14:45 bar is decided at 15:15. An earlier bar decided late, after 15:10, is still refused by each book's own `_past_last_entry` and recorded as `PAST_ENTRY_CUTOFF` with its reason.
7. **Recorded-only triggers short-circuit dispatch.** They skip strike selection, the counter route and the level reader: no broker read, no subscription. Graded-F (unpublished) triggers keep `NOT_PUBLISHED` on their row, and their cards say RECORDED ONLY.
8. **Tests switch the wall-clock deadline off by default** with an autouse fixture, matching `_entry_window_open`. Their own tests turn the real rule back on with `nse_deadline`.
9. **`EOD_PLAN_HM` moved to 15:13** (option a) rather than being retired. The operator's text asked for 15:13.
10. **`BAR_AUDIT_HM` stays 15:40.** It does not read what the recorder writes (§1).
11. **`FORCE_FLAT_HM` and `LAST_ENTRY_HM` are kept, but empty.** The resolvers are untouched, and a guard test asserts the dicts are empty.
12. **`node_modules` is linked entry by entry, not as one symlink.** Each entry of the live `frontend/node_modules` (packages, `.bin`, `.package-lock.json`) is a symlink inside a real `node_modules` directory in the copy. If vite or tsc ever write a cache (`node_modules/.vite`), it lands in the copy and never in the live folder. The live `node_modules` and `.vite` mtimes were checked after the build and are unchanged. `node_modules/` and `frontend/dist/` are gitignored.

## 6. Left undone, and notes for the operator

- **Engine B (`kotsin-nse-p35`) is not changed.** If only A is deployed, A-vs-B comparisons diverge from the first day.
- **`.env` was not read.** A `KN_LIVE_ENTRY_CUTOFF_IST` or `KN_PAPER_LIMIT_*` override there would still apply.
- **Deploy:** nothing was restarted or deployed. Restart after 15:41, so the new recording window is not cut that day, and well before the next open. This changes the live engine's behaviour, so it needs the operator's explicit yes, and the operator runs it. Any NSE position open when it restarts after 15:19 will be force-filled on paper at boot, with the alarms.
- **LIVE mode:**
  - From 15:14 a LIVE book's exit for a contract with no LTP now retries every 2 s at the broker.
  - There is no forced fill for LIVE.
  - Engine A runs paper. Review this before any live use.
- **Overnight positions:** the deadline checks use time of day only, like `past_force_flat` always has. An NSE position somehow carried overnight is not flagged until 15:14/15:19/15:20 the next day, or immediately if it is already past 15:20 when seen.
- **Out of scope, as instructed:**
  - the option-pricing expiry close at 15:30;
  - index-future bars, still 15:29;
  - the MCX-holiday watchdog note.
- **Research tools follow the new clock:** the backtest, RL env and tape replay. The backtest closes EOD trades at the 14:45 bar's close. Results from before this change are not comparable without a re-run.
- **Graded-F triggers on `/api/signals`:** recorded-only graded-F triggers stay off the default `/api/signals` list, by its existing design. They appear on the cards and with `?unpublished=true`.
