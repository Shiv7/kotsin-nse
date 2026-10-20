# record-5read: the 5-read and the strike walls, recorded live — RECORD-ONLY (operator, 19 Oct 2026)

Branch `record-5read` from `ctb-b1` (084fed9). Nothing pushed or deployed. No decision, gate, book or order changes:
CT-B1's conviction keeps `read5: None`. Write-up: `staging/next_record5/BUILD_record5.md`; design: `DESIGN_record5.md`.

| File | Change |
|---|---|
| `backend/kotsin_nse/research/read5.py` (new) | the research arithmetic, ported unchanged: legs, `opt_px`, Black-76 `iv76`, the five reads and votes, `decide`, `first_n`, the label, the VOL baseline, the walls' state / reading / overall / oiChangePct |
| `backend/kotsin_nse/ops/optrec.py` (new) | the recorder: as-of quotes (40-min ring, futures' minute states), OI of the day, per-minute option volume from `TotalQty`, coverage and gaps, 30m bars; minute part files `archive/option_volume/<day>/` |
| `backend/kotsin_nse/ops/read5rec.py` (new) | the trigger read, each follow-up minute, the first 2/3/4 of 5, the walls, the archive row, the card text |
| `backend/kotsin_nse/instrument/recband.py` (new) | the record band (KN_RECORD_BAND, default OFF): both types within K strikes of the day's prices, the cap |
| `backend/kotsin_nse/venue/fivepaisa/ws.py` | the record set and the `on_record` tap; a record-only row never reaches the engine's handlers; a band-held code is held at the broker like a twin-held one |
| `backend/kotsin_nse/ops/archive.py` | streams `option_volume_30m`, `read5` |
| `backend/kotsin_nse/engine.py` | recorder wiring, `_read5_note` in `_decide` (no await), the read task, the band sync, health `record5`, the card's `read5` |
| `backend/kotsin_nse/config.py` | `record5_enabled` (on), `record_band` (KN_RECORD_BAND, off), band K 3, caps 2000 / 1000 |
| `backend/pyproject.toml` | import-linter: strategy / risk / exec / venue / feed may not import the recorder, the reads or the band |
| `frontend/src/types.ts`, `components/BookCards.tsx` | the read-only "5-read · strike walls (recorded only)" text on every card |

---

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
| `engine.py:6281` `_eod_alarm()` | Raised once per position, kind and IST day (`_eod_alarmed`, `:846`; per day since the review fixes, §7 R2). Each alarm does four things:<br>- writes a structured `log.error(kind, …)`;<br>- writes a ledger event;<br>- sends a Telegram message;<br>- adds a row read by the health check.<br>Kinds:<br>- `eod.forced_fill`;<br>- `eod.still_open`, for an NSE position open at or after 15:20:00 (`:5974`);<br>- `eod.exit_after_deadline`, for an NSE exit filled at or after 15:20:00 of any reason, raised in `_book_exit` (`:6543-6548`);<br>- `eod.not_forced`. |
| `engine.py:6302` `_eod_check()`, added to `health_snapshot` at `:7071` | New `nse_out_by` health check. It is red for the rest of the IST day after any alarm, also across a restart that day (§7 R3), and while any NSE position is open after 15:20. Its detail gives the counts, the last four alarms and the rule. |
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
| `engine.py:2870` `_record_after_cutoff()` | No strike is chosen and nothing reaches the order path (since §7 R5 the level reader's futures read is the one broker read). The steps:<br>- A published trigger: its row is written as `RECORDED_ONLY` with the reason, and its ENTRY alert is adopted as for any trigger.<br>- An unpublished (graded F) trigger keeps its `NOT_PUBLISHED` row, the cutoff added to its reason (§7 R6).<br>- Every book that would have been offered the trigger records an `rt_twin.skipped` with `gate="entry_cutoff"`. That is the same gate a book's own refusal after the minute uses, so the Shadow page and day-book summaries count it the same way. For a published trigger those books are RT-X, RT-N, RT-Y, CT-X and CT-Y; for an unpublished one, RT-Y-F (RT-X, RT-Y without the gap switch, CT-Y, CT-X and CT-M record their graded-F rule instead, §7 R6).<br>- A `cutoff.recorded` event is written.<br>- Nothing is queued for the carry. |
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
| Tests | `PYTHONPATH=. pytest -q` (backend) | **1080 passed** at `c15aebf`; **1088 passed** after the review fixes (§7). Baseline at `0af8c0f`: 1063 passed. |
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
5. **Alarms fire once per position, kind and IST day** (per day since §7 R2). The health check stays red until the IST day ends, across a restart (§7 R3). The health monitor's usual three consecutive reads apply.
6. **A trigger is "recorded only" based on its bar, not the wall clock.** The 14:45 bar is decided at 15:15. An earlier bar decided late, after 15:10, is still refused by each book's own `_past_last_entry` and recorded as `PAST_ENTRY_CUTOFF` with its reason.
7. **Recorded-only triggers short-circuit dispatch.** They skip strike selection and the counter route (no fade is planned): no subscription, no order. Since §7 R5 the level reader still reads them, record-only. Graded-F (unpublished) triggers keep `NOT_PUBLISHED` on their row, with the cutoff in its reason (§7 R6).
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
- **Overnight positions:** fixed by §7 R2. An NSE position opened on an earlier IST day is past the 15:19 forced fill and the 15:20 out-by from the first pass. The 15:14 flatten (`past_force_flat`) still reads the time of day only; see §7.
- **Out of scope, as instructed:**
  - the option-pricing expiry close at 15:30;
  - index-future bars, still 15:29;
  - the MCX-holiday watchdog note.
- **Research tools follow the new clock:** the backtest, RL env and tape replay. The backtest closes EOD trades at the 14:45 bar's close. Results from before this change are not comparable without a re-run.
- **Graded-F triggers on `/api/signals`:** recorded-only graded-F triggers stay off the default `/api/signals` list, by its existing design. They appear on the cards and with `?unpublished=true`.

---

## 7. Review findings of 17 Oct and their outcome

A review of `close-1514` raised six findings, all minor. Each one was checked against the code: the reviewer's
probes (`review_tmp/test_adv_exit.py`, `review_tmp/rt/test_review_cutoff.py`) were run against this copy and
reproduced every one. All six are fixed, in A and B the same way. Each has a test that fails on `c15aebf` and passes
after the fix: the tests were run against `c15aebf`'s `kotsin_nse/` with the new tests in place, and all failed on
their behaviour assertions.

| # | Finding | Confirmed? | Outcome |
|---|---|---|---|
| R1 | A backoff from before 15:14 delays the 15:14 flatten by up to 30 s | Yes. Probe: a stop refused from 15:13:05 has `_exit_retry_at` = 15:14:05, and the position was still OPEN at 15:14:00 with a bid | Fixed |
| R2 | Alarms are deduplicated per position, not per day, and the deadlines read the time of day only | Yes. Probe in SHADOW: day 2 at 15:20 gave no new `eod.still_open` and the health line was green | Fixed |
| R3 | A restart turns the `nse_out_by` line green | Yes, from the code. `_eod_alarms` and `_eod_alarmed` lived in memory only | Fixed |
| R4 | The forced fill is stamped with the pass's start, so a fill made after 15:20 raises no alarm | Yes. Probe: wall clock at 15:20:10, both fills stamped 15:19:00, 0 alarms | Fixed |
| R5 | A recorded-only 14:45 trigger gets no level reading | Yes. Probe: `route_events == []` | Fixed |
| R6 | A graded-F 14:45 trigger is not labelled "recorded only" on the trigger lists, and RT-X's card gives the cutoff as its reason | Yes. Probe: `NOT_PUBLISHED` with FUDKII's reason only; RT-X card RECORDED_ONLY | Fixed |

### R1: the 15:14 flatten's first try never waits on an earlier backoff

| File:line | Change |
|---|---|
| `backend/kotsin_nse/engine.py:855` | New `_exit_failed_at`: when each position's exit was last refused. |
| `engine.py:6516`, `:6565` | Set beside `_exit_retry_at` in `_exit_cross` and `_exit_market`. |
| `engine.py:6592`, `:6598` | Cleared with it in `_book_exit`. |
| `engine.py:6243` `_backoff_before_flatten()` | New. True when the exit is an NSE flatten (`_nse_flatten`) and the last refusal came before the position's 15:14 force-flat. |
| `engine.py:6406` (`_exit_now`) | A wait from `_exit_retry_at` is skipped when `_backoff_before_flatten` holds. |

- The flatten's own refusals still wait `EOD_RETRY_S` (2 s), through `_exit_retry_s` as before.
- Before 15:14, the stop keeps its 2-4-8-16-30 s backoff.
- **Decision:** the reviewer suggested `min(retry_at, failed_at + 2 s)`. With that, a refusal at 15:13:59 would
  still hold the flatten to 15:14:01. Instead, a refusal from before the flatten is passed over entirely, so the
  first try is at 15:14:00, as the operator's "flattened at once" reads. A refusal at 15:13:59 followed by a try at
  15:14:00 is harmless, because exit rejects never count toward the breaker.

### R2: alarms once a day, and a position from an earlier day is past both deadlines

| File:line | Change |
|---|---|
| `backend/kotsin_nse/market/session.py:183` `held_overnight()` | New. True for a position opened on an earlier IST day. |
| `market/session.py:190`, `:196` | `past_nse_forced_fill()` and `past_nse_out_by()` take an optional `opened_ts`. Either is also true when `held_overnight` holds. MCX is still never past them. Without `opened_ts`, they read the time of day, as before. |
| `engine.py:852` | `_eod_alarmed` is keyed by `(kind, position, IST day)`. |
| `engine.py:6357` `_eod_alarm()` | The dedupe is per day. Earlier days' keys are dropped as today's are added. |
| `engine.py:6380` `_eod_check()` | The `late` filter reads today's key, and passes `opened_ts`. |
| `engine.py:6027-6030` (`_manage_positions`), `:6318` (`_force_eod_fill`), `:6627` (`_book_exit`) | Pass `pos.opened_ts`. |
| `engine.py:6351` `_eod_still_open()` | New. The `eod.still_open` alarm, saying "held overnight" when the position is. |
| `engine.py:6323-6324`, `:6629` | The forced fill's cause and the late-exit detail say "held from <day>" / "held overnight". |

- In PAPER, yesterday's position is force-filled at its last known bid at the first pass. That includes a pass after
  midnight, when an engine that was down over the close comes back. It raises `eod.forced_fill`, `eod.still_open` and
  `eod.exit_after_deadline`.
- In SHADOW and LIVE, it alarms (`eod.not_forced`, `eod.still_open`) at the first pass of every day it is held. The
  health line stays red.
- The conftest autouse `_nse_deadline_off` still pins both helpers to False (its lambdas take `*args`), so suite
  positions with old `opened_ts` are not affected.
- **Not changed:** the 15:14 flatten itself (`past_force_flat` / `_past_force_flat`) still reads the time of day. In
  PAPER the forced fill already takes the position out at once. A LIVE book's overnight lots alarm, and are closed at
  the broker.

### R3: the `nse_out_by` line survives a restart

| File:line | Change |
|---|---|
| `engine.py:372` | New `EOD_ALARM_KINDS`: the four alarm kinds. |
| `engine.py:7077` `_load_eod_alarms()` | New. Reads today's events of those kinds back from the ledger. It uses `ledger.recent(events, …)` with the kind and today's start, so it never reads the whole day's events. Each event's `day` must be today. It rebuilds `_eod_alarms` (oldest first) and `_eod_alarmed`. |
| `engine.py:881` (`start_core`) | Calls `_load_eod_alarms()` next to `_load_positions()`. |

- A position already alarmed today is not alarmed again after the restart.

### R4: a forced fill carries the clock of when it was made

| File:line | Change |
|---|---|
| `engine.py:6026` (`_manage_positions`) | The 15:19 and 15:20 checks read `time.time()` as each position is reached, not the pass's start. |
| `engine.py:6317` (`_force_eod_fill`) | `now` is read again after `_refresh_exit_book`. The fill, its order, the audit, `eodForced`, the trade and the alarms use it. When it is at or after 15:20, `eod.still_open` is raised there. `_book_exit` then raises `eod.exit_after_deadline` from the fill's own time. |

### R5: the level reader reads a recorded-only trigger

| File:line | Change |
|---|---|
| `engine.py:2890` (`_record_after_cutoff`) | After the record, `_read_route(sig, bar, und)` runs, with the same `except Exception` guard as `_handle_counter_unpublished`. The conditions mirror the routes it replaces: a FUDKII trigger; a published one off MCX; an unpublished one on NSE cash only. It runs last, so a slow futures read never holds the record. It places no order: no fade is planned, and nothing is subscribed. |
| `engine.py:4434` (`book_cards`) | **Found while fixing R5.** With a COUNTER route read, an in-trend card offered a counter-trend button (`ctaCounter`). An older line in that block also reuses `state` for the button's note, so the card lost RECORDED_ONLY. That reuse is unchanged; it predates this branch. A recorded-only trigger (`cut_q`) now gets no counter-trend button, and the card keeps RECORDED_ONLY. |

### R6: the trigger lists say "recorded only" for a graded-F trigger, and RT-X keeps its graded-F reason

| File:line | Change |
|---|---|
| `engine.py:2294-2298` (`_decide`) | When `_after_entry_cutoff(sig)` holds, the `NOT_PUBLISHED` row's reason gets " · after the 15:10 NSE entry cutoff — recorded only: …". The decision stays `NOT_PUBLISHED`, so `?unpublished=true` and the counts on `/api/fudkii/today` work as before. |
| `engine.py:2416` `_graded_f_rule()` | New. The `GRADED_F_RULES` text for a book that never trades a graded-F trigger, else None. RT-Y passes only through the gap switch. `_handle_unpublished` now uses it too, with the same behaviour. |
| `engine.py:2890` (`_record_after_cutoff`) | On an unpublished trigger:<br>- RT-X, RT-Y without the gap switch, CT-Y, CT-X and CT-M record their `graded_f` skip with that rule, as `_handle_unpublished` and `_handle_counter_unpublished` have them do;<br>- RT-Y-F records the cutoff;<br>- the event has a new `gradedF` list. |
| `engine.py:4405` (`book_cards`) | On such a card the TAKE reason stays the graded-F rule, not the cutoff. The TAKE was already disabled above for an unpublished trigger. |

- **Decision:** the finding named RT-X and RT-Y. CT-Y, CT-X and CT-M had the same mislabel, since before this branch
  `_handle_counter_unpublished` gave them a graded-F skip. They were fixed the same way.
- CT-Y-F, whose gap fade never takes a 14:45 bar, and CT-M on a published trigger still have no skip row, and their
  cards read RECORDED ONLY. This is unchanged and noted in B's §6.

### Tests

| File | Test | Covers |
|---|---|---|
| `backend/tests/test_nse_close_1514.py:100` | `test_a_position_opened_on_an_earlier_day_is_past_both_nse_deadlines_at_once` | R2, pure |
| `:378` | `test_a_backoff_from_before_1514_never_holds_the_flatten` | R1 |
| `:410` | `test_a_backoff_from_a_refusal_after_1514_still_waits_two_seconds` | R1 guard |
| `:432` | `test_an_nse_position_still_open_on_a_later_day_alarms_again_and_the_line_is_red` | R2, SHADOW |
| `:463` | `test_a_paper_position_from_an_earlier_day_is_forced_out_at_the_first_pass` | R2, PAPER at 00:45 |
| `:486` | `test_the_out_by_line_stays_red_after_a_restart_the_same_day` | R3 |
| `:519` | `test_a_forced_fill_made_after_1520_is_stamped_when_made_and_alarms` | R4 |
| `backend/tests/test_entry_cutoff_and_carry.py:168` | `test_a_1445_trigger_decided_at_1515_is_recorded_and_listed_never_traded_nor_carried` (extended) | R6: the reason, `books` / `gradedF`, the skips, five cards and their TAKE reason, `?unpublished=true`, `/api/fudkii/today`. R5: `counter.route` for both triggers, the route on the alert. |
| `:267` | `test_a_recorded_only_trigger_routed_counter_trend_offers_no_counter_trend_button` | R5, the card side |

`docs/BOOKS.md:36-41`, `:45-53`: the clock section describes the fixes.

### Results after the fixes

| Check | Result |
|---|---|
| Copy check | `python -c "import kotsin_nse; print(kotsin_nse.__file__)"` → `…/close1514/engA_work/backend/kotsin_nse/__init__.py` |
| `PYTHONPATH=. pytest -q` | **1088 passed** (1080 + 8 new) |
| `ruff check kotsin_nse tests` | All checks passed |
| `lint-imports` | 5 kept, 0 broken |
| Time-of-day sweep (`fakeclock`) | 1088 passed at each of 00:30, 10:00, 15:16 and 15:21 IST |
| Reviewer's probes | All three exit probes now fail their "expected the defect" assertion. The cutoff probe shows `route_events` = two `counter.route` and the cutoff in the F row's reason. |
