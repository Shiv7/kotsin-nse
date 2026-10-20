# record-5read: the 5-read and the strike walls, recorded live — RECORD-ONLY (operator, 19 Oct 2026)

Branch `record-5read` from `ctb-b1` (561dc9d). The same change as engine A's (7cecb25); B's archive writes the new streams as part files like its others. Nothing pushed or deployed. No decision, gate, book or order changes:
CT-B1's conviction keeps `read5: None`. Write-up: `staging/next_record5/BUILD_record5.md`; design: `DESIGN_record5.md`.

| File | Change |
|---|---|
| `backend/kotsin_nse/research/read5.py` (new) | the research arithmetic, ported unchanged: legs, `opt_px`, Black-76 `iv76`, the five reads and votes, `decide`, `first_n`, the label, the VOL baseline, the walls' state / reading / overall / oiChangePct |
| `backend/kotsin_nse/ops/optrec.py` (new) | the recorder: as-of quotes (40-min ring, futures' minute states), OI of the day, per-minute option volume from `TotalQty`, coverage and gaps, 30m bars; minute part files `archive/option_volume/<day>/` |
| `backend/kotsin_nse/ops/read5rec.py` (new) | the trigger read, each follow-up minute, the first 2/3/4 of 5, the walls, the archive row, the card text |
| `backend/kotsin_nse/instrument/recband.py` (new) | the record band (KN_RECORD_BAND, OFF until the operator decides): both types within K strikes of the day's prices, the cap |
| `backend/kotsin_nse/venue/fivepaisa/ws.py` | the record set and the `on_record` tap; a record-only row never reaches the engine's handlers; a band-held code is held at the broker like a twin-held one |
| `backend/kotsin_nse/ops/archive.py` | streams `option_volume_30m`, `read5` |
| `backend/kotsin_nse/engine.py` | recorder wiring, `_read5_note` in `_decide` (no await), the read task, the band sync, health `record5`, the card's `read5` |
| `backend/kotsin_nse/config.py` | `record5_enabled` (on), `record_band` (KN_RECORD_BAND, off), band K 3, caps 2000 / 1000 |
| `backend/pyproject.toml` | import-linter: strategy / risk / exec / venue / feed may not import the recorder, the reads or the band |
| `frontend/src/types.ts`, `components/BookCards.tsx` | the read-only "5-read · strike walls (recorded only)" text on every card |


**Review fixes (20 Oct, third commit, the same as engine A's).** With KN_RECORD_BAND on, an engine subscribe of a code
only a record set streams (this engine's band or the twin's) is answered as the broker answers a fresh subscribe — the
code's current state, a plain frame now, live from now — never primed and "live since the band"; the twin's band is
pinned at the hub apart from its engine's codes (frame marker `Record`; deploy A and B together), so neither engine's
band changes the other's reading; reconnects send the engines' codes before the bands'. The 09:15 open is a first print
only for a code streaming by 09:15; the time-matched IV takes the future's first quote up to 5 s after the option's
price time when it has none before it; an OI that is only the previous close is not read; a moment before a restart
says so. `stats()` keeps its counts; the boot / midnight history is built in the worker thread; the ledger's `read5.*`
events are compact (the whole record in `archive/read5_detail/<day>.jsonl`); the chain is read after the read's wait;
the recorder's tap runs after the engine's dispatch; wall candidates are picked a minute before each bar; minute
volumes are written by the socket's owner alone (engine A: this engine reads the hub and writes none), kept 30 days,
never below 2 GB free; the hub's #down/#up is a volume gap whatever KN_HUB_RECONNECT_REPAIR says.

**Re-check fixes (20 Oct, fourth commit, the same as engine A's).** Band on, reading the hub (this engine): a book's
subscribe of a strike its own band holds and engine A's books stream is primed from its own pool when the hub answers (it
got no first frame before); the stand-in snapshot never re-sends a frame the stream delivered meanwhile; a subscribe
builds no band-wide set (0.3 µs an all-held call, was 68 µs band off / 257 µs band on); a broker reconnect sends every
channel's engine codes before any band's. Record: each minute's first future/stock state is kept beside its last (the IV
stand-in survives the ring's compaction); a name read through its front future (NIFTYNXT50, NIFTYFPI) has no EQ and is
not scored; reading the hub, an OI that is only the previous close says the hub replays no OI (after a restart of this
engine alone, a code A already streams prints OI only on a change); the volume tape's owner flag is set at the hub wiring.

---

# close-1514: engine B changes (operator decisions of 17 Oct 2026)

This branch is `close-1514`. It was cut from `71036d2` (`phase35-engine-b`) in a scratch clone of
`kotsin-nse-p35`. Nothing has been pushed or deployed. NSE only: MCX is untouched.

It ports engine A's `close-1514` (A: `0af8c0f..c15aebf`) to engine B. **B behaves as A does for the same
inputs**: the same times, the same forced-fill rule and flags, the same alarms and the same trigger-list
labels. A's patch was applied hunk by hunk, then every hunk was read again in B's code. Where B's code differs,
the port was adapted (section 7 lists every intentional A/B difference).

Line numbers are for the branch head. Paths are relative to the repo root.

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
| `backend/kotsin_nse/ops/fulltape.py:44` | `WINDOW` changed from `("09:14", "15:31")` to `("09:14", "15:41")`. The module docstring (lines 1-4) and the comment (lines 40-43) say so. | B's full tape is the only cut on its raw NSE recording, as in A. The check is a minute compare, so recording runs to 15:41:59. |
| none | `SESSIONS[...].close` stays 15:30. The aggregator, bars, B's OI candles, the 15:15 closing-auction bar, the carry and all signal logic are unchanged. | As decided by the operator. |

**What B records after 15:30, and where it comes from.**
- B has no broker socket of its own. In `connect` mode it reads engine A's feed hub (`venue/fivepaisa/ws.py`).
- A's `HubServer.tap` forwards every raw socket message before any parsing, with no clock and no segment filter.
- So B already received the 15:30-15:40 frames. B's own OI archive holds 32,650 rows on 15 Oct and 44,537 rows
  on 16 Oct between 15:30 and 15:45 (engineB_map §2.2). Only the full tape's `WINDOW` threw the F&O quotes away.
- None of these has a time window, so no change was needed:
  - the quote table (`Engine._on_tick`, two clocks);
  - the OI archive (`Engine._on_oi` → `archive.oi`);
  - the tick tape (`ops/tape.py`);
  - FeedRate (`ops/feed_rate.py`, a per-contract probe, not a recorder);
  - the clock loop that calls `_record_tape` (`engine.py` `_clock`, every second, unconditional).
- **A's hub needs no change.** But B records only while A's process, broker socket and HubServer are up. B has
  no fallback socket.

**Jobs that run just after recording ends: none moved.**
- `BAR_AUDIT_HM = "15:40"` (`engine.py:279`) is unchanged. `_audit_bars` → `BarReconciler.audit_day`
  (`bars/verify.py`) reads only two things:
  - B's bar store, built by the aggregator, which still closes at 15:30;
  - the broker's REST candles.
- It never reads anything the recorder writes (`archive/tape_full`), so 15:40 is still after its inputs.
- `daily_refresh_hm` (15:45) is a REST refetch. B's OI candles end at the 15:30 close.
- Nothing in B's engine, `deploy/` or `scripts/` reads `tape_full` on a clock.

## 2. R2: flatten from 15:14, out before 15:20

All of this is A's code, applied unchanged unless a row says otherwise. B's exit path at `71036d2` was the same
as A's, line for line, apart from the offsets: session.py, `_manage_positions` .. `_place_exit_limit`,
`exec/resting.py`, `exec/paper.py`, `exec/gateway.py` and `risk/limits.py`.

### 2.1 The clock

| File:line | Change |
|---|---|
| `backend/kotsin_nse/market/session.py:62-64` | NSE_EQ, NSE_FO and NSE_IDX `force_flat` changed from 15:20 to 15:14. MCX stays 23:20. The comment (lines 52-60) and the field docstring (line 42) explain why. |
| `market/session.py:70`, `:73` | New `NSE_FORCED_FILL = time(15, 19)` and `NSE_OUT_BY = time(15, 20)`. |
| `market/session.py:183`, `:188` | New `past_nse_forced_fill()` and `past_nse_out_by()`. Both are always False for MCX. |
| `backend/kotsin_nse/engine.py:368` | `NSE_LAST_ENTRY_HM` changed from `"15:15"` to `"15:10"`. Inclusive minute, so 15:10:59 is allowed. It matches `settings.live_entry_cutoff_ist` and the gateway's 15:10. |
| `engine.py:370`, `:373` | `LAST_ENTRY_HM = {}` and `FORCE_FLAT_HM = {}`. RT-Y-F's own times are removed: last entry 15:22 and flatten 15:24. The `FORCE_FLAT_HM.update(...)` that copied 15:24 onto its `_SE`/`_SA` mirrors is deleted. |
| `engine.py:376-383` | New constants: `NSE_FORCED_FILL_HM`, `NSE_OUT_BY_HM`, `EOD_RETRY_S = 2.0` and `NSE_EOD_AT_ONCE`. |
| `engine.py:390` | `EOD_PLAN_HM` changed from `"15:15"` to `"15:13"`. It is still record-only, and its verdict now reads "flatten at 15:14 as now". |
| `engine.py:3067`, `:3076` | Docstrings of `_force_flat_hm` and `_past_last_entry`. Text only. |

### 2.2 "First avenue": sell into the bid at once and retry every 2 s

| File:line | Change |
|---|---|
| `engine.py:6434` `_nse_flatten()` | True for an NSE position's exit when either: the reason is EOD; or the time is at or after its 15:14 force-flat, whatever rule fired first that second. MCX always returns false. |
| `engine.py:6613` (`_place_exit_limit`) | When `_nse_flatten` holds, the exit takes the at-once path (`_exit_at_once` → `_exit_cross`). It sells into the bid through the depth for every lot, with no 10 s walk from the mid. With no bid, it falls back to `_exit_market`. |
| `engine.py:6406` (`_exit`) | From the flatten onwards, an exit resting since before 15:14 (a trail, a halt walking from the mid) is replaced by the at-once sale. |
| `engine.py:6443` `_exit_retry_s()`, used at `:6703` (`_exit_cross`) and `:6751` (`_exit_market`) | A refused NSE flatten retries every 2 s. Every other exit, and MCX, keeps the `min(2**n, 30)` backoff. |
| `engine.py:6255-6261` (`_manage_positions`, no-LTP branch) | From 15:14, a contract with no last price now tries an EOD exit with the bid from `_touch`, if any. It used to be skipped. |
| `exec/resting.py:28-30` | Docstring only. |

### 2.3 The 15:19 forced paper fill

| File:line | Change |
|---|---|
| `engine.py:6237` (top of the `_manage_positions` loop) | Runs before the no-quote skip: `if past_nse_forced_fill(...) and await self._force_eod_fill(pos, now): continue`. |
| `engine.py:6489` `_force_eod_fill()` | Same steps as A, in this order:<br>1. Skip LIVE books' positions.<br>2. Skip if an exit is in flight this tick.<br>3. Take off the resting exit and the resting target sells.<br>4. Take one REST snapshot through `_refresh_exit_book`.<br>5. Read the price from `_last_known_exit_price`.<br>6. Fill on paper through `Gateway.force_paper_exit`.<br>7. Book it through `_book_exit`.<br>8. Raise the alarm. |
| `engine.py:6533` (in `_force_eod_fill`) | **B only (typing):** the audit's `fillPrice` is read as `result.fill.price if result.fill is not None else round(price, 2)`. A reads `result.fill.price` directly. `force_paper_exit` always fills, so the value is the same; B's mypy gate flags the bare Optional read. |
| `engine.py:6459` `_last_known_exit_price()` | **Adapted to B's two clocks.** The order is the same as A's: the last known bid, which is the newer of the depth book's and the quote's bid by `ts` (when we saw it), of any age; else the last known LTP; else the last mark; else the entry. A short position reads the ask. The difference is how B ages the last-price fallback. B has no `_ltp_ts` (A's arrival time of the LTP), so B reads it from one of two places:<br>- the quote's `ts`, when that quote still holds that price. A REST snapshot keeps its 0/0 bid and ask but stamps when it was read.<br>- otherwise, with no observation of that price held, the broker's trade time `_ltp_traded_ts`.<br>For the same inputs (a price seen N s ago with no bid), A and B give the same price, source and age. |
| `exec/gateway.py:297` `force_paper_exit()` | Identical to A. |
| Flag, not a new exit reason | Identical to A. The reason stays `ExitReason.EOD`. The forced fill is marked in five places:<br>- the order `note`, "forced fill 15:19, stale book — … paper-filled at the last known bid X, N s old";<br>- the `F` suffix on the order id (`exit_client_order_id(..., forced=True)`, `engine.py:7618`);<br>- `exec.eodForced` in the order audit;<br>- `exec_log["eodForced"]` on the position;<br>- `eod_forced` on the trade row (`engine.py:6820`). |
| LIVE and SHADOW | Identical to A. These positions are never paper-filled. `_fills_on_paper` (`:6451`) decides, and such a position raises `eod.not_forced`. |

### 2.4 Alarms at 15:20 and the health check

| File:line | Change |
|---|---|
| `engine.py:6548` `_eod_alarm()` | Identical to A. Raised once per position, kind and IST day (per day since the review fixes, §8 R2). Each alarm writes a structured `log.error(kind, …)` and a ledger event, sends a Telegram message, and adds a row for the health line. The kinds are:<br>- `eod.forced_fill`;<br>- `eod.still_open` (`:6234`);<br>- `eod.exit_after_deadline` (`_book_exit`, `:6810`);<br>- `eod.not_forced`. |
| `engine.py:6569` `_eod_check()`, added to `health_snapshot` at `:7461` | Identical check (`nse_out_by`), red across a restart the same day since §8 R3. In B it sits after `bar_audit` and before B's own `duty_errors`, `zones` and `latency` lines. The rejected hunk was placed by hand there. |
| `engine.py:6704`, `:6752`, `:6783` | `_exit_fail_note` keeps the last refusal for each position, as the forced fill's cause. It is cleared on a full fill. |
| `engine.py:7342-7346` (`_roll_day_state`) | **B only.** B resets per-day state in one place (its 3 Oct review). At the day roll:<br>- yesterday's alarm rows go;<br>- the raised kinds and the last refusal of positions no longer held go;<br>- a position still held keeps its raised kinds.<br>Behaviour is the same as A: A's health line already reads today's rows only, and a held position never alarms twice for one kind in either engine. This only stops the maps growing.<br>**Changed by §8 R2:** the raised kinds are keyed by day, and the roll keeps today's only, so a held position alarms again on the new day, as in A. |

### 2.5 Entries never fill after the cutoff

| File:line | Change |
|---|---|
| `engine.py:3671-3674` (`_advance_one`, entry branch) | An NSE entry still resting after the last entry minute is cancelled through `_entry_missed`, with "cancelled at the entry cutoff — …". Its hold is released. |
| `engine.py:2857-2860` (`_enter_book_once`) | For a gated book, the minute is checked again after `_book_gates`, which can wait on the broker (the volume read). |
| unchanged | `exec/gateway.py:71` and `config.py:246` (15:10). Operator TAKE goes through `_enter_book_once`. |

### 2.6 Text updates (no change to behaviour)

- `api/review_peers.py:180-182`: "day-end 15:20" becomes "day-end flatten".
- `api/shadow.py:982` and `:1017`: these now say 15:14.
- `api/review.py:42` comment.
- `committee/pipeline.py:28`: the committee prompt.
- `risk/exits.py:140` and `research/backtest.py:587-591` comments.
- `docs/BOOKS.md:28-47` (the clock), `:149-150` (the exit table) and `:194` (the 15:13 plan).
- **B only:** `docs/BOOKS.md:210-212`, in B's "two clocks" section. "the 15:20 flatten's last-price fill" now
  describes the 15:14 flatten and the 15:19 forced fill.
- `docs/PIVOTS.md:99-100`.

## 3. 14:45-bar triggers: recorded, never traded

Identical to A. B's `_decide`, `_handle_unpublished`, `_handle_counter_unpublished` and `book_cards` were the same
code as A's at the base, so the hunks landed unchanged.

| File:line | Change |
|---|---|
| `engine.py:2529-2532` (`_decide` dispatch) | `elif (late := self._after_entry_cutoff(sig)) is not None:` → `_record_after_cutoff`. It comes after the carry and before the published and unpublished routes. |
| `engine.py:3089` `_after_entry_cutoff()` | Read off the trigger's bar, not the wall clock. The bar must be decided after 15:10 and no later than the close, and must not be the close's own bar. That is the 14:45 bar. MCX is excluded. |
| `engine.py:3105` `_record_after_cutoff()` | No strike is chosen and nothing reaches the order path (since §8 R5 the level reader's futures read is the one broker read). The steps:<br>- A published trigger: its row is written as `RECORDED_ONLY` with the reason, and its ENTRY alert is adopted.<br>- An unpublished one keeps its `NOT_PUBLISHED` row, the cutoff added to its reason (§8 R6).<br>- Every book offered it records an `rt_twin.skipped` with `gate="entry_cutoff"`. On an unpublished trigger, RT-X, RT-Y without the gap switch, CT-Y, CT-X and CT-M record their graded-F rule instead (§8 R6).<br>- A `cutoff.recorded` event is written.<br>- Nothing is queued for the carry. |
| `engine.py:4431`, `:4441`, `:4594`, `:4648` (`book_cards`) | New card state `RECORDED_ONLY`. The TAKE button is disabled with the reason. New field `recordedOnly`. |
| `api/daybook.py:398-399` | Day-book labels for `RECORDED_ONLY` and `PAST_ENTRY_CUTOFF`. |
| `frontend/src/components/BookCards.tsx:55`, `:144`, `:624` | New type field, the `RECORDED ONLY · after the entry cutoff` chip, and the card's headline. |
| `frontend/src/pages/Signals.tsx:205-208`, `:265` | The decision badge reads `RECORDED ONLY · after cutoff`, in blue, with the reason as its tooltip. As in A, `CARRIED` also turns blue. |

B's trigger-list APIs are the same code as A's: `/api/signals`, `?unpublished=true`, `/api/fudkii/today`,
`/api/alerts` and `/api/books/{book}/cards`. B's extra routes (`/oi`, `/volume`, `/feed/rate`) list no triggers.
B's frontend sources were byte-identical to A's at the base, and are identical after the change.

## 4. Tests

### Changed (they pinned the old times or behaviour)

A's rewrites, applied unchanged. B's copies of these files took A's hunks cleanly:
- `backend/tests/conftest.py:36-63`: `_entry_window_open` docstring, the autouse `_nse_deadline_off` and the `nse_deadline` fixture.
- `backend/tests/test_entry_cutoff_and_carry.py:64`, `:77`, `:97`.
- `backend/tests/test_urgent_stops_depth_eod_plan.py`: the plan at 15:13, its verdict "flatten at 15:14 as now", and nothing recorded before 15:13.
- `backend/tests/test_mae_and_stop_execution.py`: the NSE EOD exit sells at the bid at once.
- `backend/tests/test_tape.py:344-355`: the replay is forced from 15:14.
- `backend/tests/test_rl.py:76-82`: the 14:45 bar contains 15:14.
- `backend/tests/test_depth_check.py`: two engine tests pin `past_force_flat` False. They run at 15:30 and are about the depth check, not the close.

### Added

| File | Tests |
|---|---|
| `backend/tests/test_nse_close_1514.py` (new) | A's 12 tests: the clock; the forced id; every book sold at the bid at 15:14; MCX keeps its walk and backoff; a halt or trail resting from before sells at once; 2 s retries; a no-LTP bid; the 15:19 forced fill (price, age, flag, note, `F` id, trade flag, alarm, health line red, wallet released); the fallbacks; LIVE and SHADOW; the 15:20 alarms; a stop at 15:19:59 in time and MCX. **Adapted:** `test_the_forced_fill_falls_back_to_the_last_price_then_the_entry` (`:264`) sets B's `_ltp_traded_ts` instead of A's `_ltp_ts`. It also pins B's two clocks: a REST snapshot holding the price gives the sighting age (20 s), and a quote of another price does not count as a sighting (50 s, from the trade time). **B only:** `test_the_day_roll_drops_the_deadline_state_of_closed_positions_only` (`:375`). |
| `backend/tests/test_entry_cutoff_and_carry.py` | A's four: `:124` (gates past the cutoff), `:151` (only the 14:45 bar is recorded only), `:167` (the 14:45 trigger end to end: rows, events, skips, no carry, 6 book cards plus the RT-Y-F shadow, `/api/signals`, `?unpublished=true`, `/api/fudkii/today`, the alert row, TAKE refused) and `:244` (a resting entry cancelled at the cutoff). |
| `backend/tests/test_full_tape.py:39` | A's `test_fo_ticks_are_recorded_to_1540_after_the_1530_close`. |
| `backend/tests/test_full_tape.py:55` | **B only:** `test_engine_b_records_a_post_close_hub_frame_and_its_oi_and_builds_no_bar_from_it`. It runs through B's own tick path:<br>- a 15:39:30 option frame (seen-time and traded-time kept apart) is on the full tape;<br>- its OI frame is in the OI archive;<br>- a 15:30, 15:35 and 15:39:59 future frame builds no bar after the 15:29 bar and leaves nothing in B's `late_qty`.<br>With the old window the first check fails. |

### Results, on the branch head, running against the copy

`python -c "import kotsin_nse; print(kotsin_nse.__file__)"` (from `backend/`, `PYTHONPATH=.`) →
`…/scratchpad/close1514/engB_work/backend/kotsin_nse/__init__.py`

| Check | Command | Result |
|---|---|---|
| Tests | `PYTHONPATH=. pytest -q` (backend) | **1156 passed** at `3d42b52`; **1164 passed** after the review fixes (§8). Baseline at `71036d2`: 1137 passed. |
| Ruff | `ruff check kotsin_nse tests` | **All checks passed!** |
| Import contracts | `lint-imports` | **5 kept, 0 broken** |
| mypy gate | `python ../scripts/mypy_gate.py` | **Red at the base, and unchanged by this branch.** At `71036d2` the gate already reports "22 NEW mypy error(s)", in `api/live_trades.py`, `api/review.py`, `engine.py` (2), `ledger/db.py` and `strategy/regime_gates.py`. They come from commits after the baseline file was written (3 Oct). On this branch the gate's output is byte-identical to the base run: 0 new errors from this change. See §6. |
| Frontend build | `npm run build` (`tsc --noEmit && vite build`) | Built. `frontend/dist` is gitignored. |
| Frontend tests | `npm test` | **7 pass, 0 fail** |
| Time-of-day sweep | the full suite with `time.time()` shifted (scratch pytest plugin `fakeclock`) | **1156 passed** at each of 10:00, 14:50, 15:12, 15:16 and 15:21 IST. |
| R1 commit alone | `pytest -q` at `038e238`, in a scratch worktree (since removed) | **1139 passed** (1137 baseline + 2 R1 tests). |

**Tool notes.**
- mypy is not in engine A's venv. It ran as mypy 2.4.0, the version `pyproject.toml` pins. It was installed
  offline from the local uv cache into a scratch venv, with A's venv site-packages added through a `.pth` file
  for the third-party types. Nothing was installed into A's venv.
- Node is `/opt/homebrew/bin/node` v26.10.0. `node_modules` in the copy is a real directory of per-entry
  symlinks into the live A `frontend/node_modules` (`.vite` and `.DS_Store` not linked), so any vite cache lands
  in the copy. The live `node_modules` and `.vite` mtimes were checked unchanged after the build.

## 5. Decisions I had to take

1. **Same behaviour as A, decision for decision.** A's §5 decisions 1-11 hold for B unchanged:
   - "first avenue" covers any NSE exit from 15:14;
   - a flag, not a new exit reason;
   - the forced-fill price order;
   - LIVE and SHADOW are never paper-filled;
   - alarms fire once per position, kind and IST day (since §8 R2);
   - "recorded only" is decided off the bar;
   - recorded-only triggers short-circuit dispatch, but the level reader still reads them (since §8 R5);
   - the autouse deadline fixture in tests;
   - the plan moves to 15:13;
   - `BAR_AUDIT_HM` stays 15:40;
   - the dicts are kept but empty.
2. **The last-price age in B's two clocks** (§2.3). B keeps the observation time on the quote and the trade time
   in `_ltp_traded_ts`. The age is the observation, when B holds one for that price, as every B age guard reads
   it. Otherwise it is the trade time, which overstates the age rather than understating it.
3. **mypy-safe `fillPrice`** (§2.3). The value is unchanged.
4. **B's day roll prunes the deadline state** (§2.4). This follows B's own rule for per-day maps. Since §8 R2
   it keeps today's raised kinds only, so alarms are the same as A's: once a position, kind and day.
5. **The mypy baseline was not updated.** The gate fails at the base for reasons unrelated to this work. Running
   `--update` would silently accept 22 errors other commits introduced, and fixing them would touch unrelated
   live-engine code. Both are the operator's call.
6. **No trading-day gate on the full tape**, though engineB_map recommends one (B wrote 5,519 rows on Saturday
   17 Oct from weekend frames). It is not in the operator's decisions, and A does not have one.
7. **B's OI candles stay on the 15:30 close** (engineB_map §2.4, optional). The raw OI frames to 15:40 are
   archived. Extending the display candles needs its own yes.

## 6. Left undone, and notes for the operator

- **mypy gate is red at the base** (`71036d2`), with 22 errors not in `mypy-baseline.txt`. This branch adds none.
  The options are to fix them, or to run `uv run python ../scripts/mypy_gate.py --update` on purpose.
- **Possible gap shared with A (partly closed by §8 R6, in both engines):** `_record_after_cutoff` records
  `entry_cutoff` skips for these books only:
  - a published trigger: RT-X, RT-N, RT-Y, CT-X and CT-Y;
  - an unpublished one: RT-Y-F, RT-Y and RT-X.

  The routes it short-circuits also reach other books:
  - CT-M (`_market_fade`) on a published trigger;
  - on an unpublished trigger, CT-Y-F (`_gap_fade`), plus the `graded_f` skips that CT-Y, CT-X and CT-M write.

  Those books' cards still read RECORDED ONLY, because `cutoff.recorded` is found and they have no skip. But the
  books get no `rt_twin.skipped` row and are missing from the event's `books`. If wanted, fix it in both engines
  at once.

  **Since §8 R6:** on an unpublished trigger, CT-Y, CT-X and CT-M write their `graded_f` skips again, as do RT-X and
  RT-Y without the gap switch. Two books still have no skip row: CT-Y-F on an unpublished trigger, and CT-M on a
  published one. Their cards read RECORDED ONLY.
- **Shared with A:** the 15:19 fill takes one REST snapshot first. For a held contract the feed cannot vouch for,
  B's `_apply_snapshot` replaces the quote with the broker's last price and a 0/0 bid and ask. A's does the same.
  The forced fill then reads the depth book's last bid, or the last price, rather than the quote's older bid. The
  flatten's own 2 s retries refresh the same way from 15:14.
- **Fixed by §8 R2 (both engines):** an NSE position held overnight (for example, a mode switched to SHADOW with
  lots open) used to keep the alarm kinds it raised, and was silent the next day. It now alarms again each day it is
  held, from the first pass, and the health line is red.
- **Alarm channel:** B's `.env` is a symlink to A's, so B sends to the same Telegram chat. Each deadline alarm
  will arrive twice, once per engine, and the text does not say which engine sent it. This matches B's other
  messages today. Decide whether B should alarm, or label its messages.
- **R1 operations (B-specific):** B records the 15:30-15:41 tape only while A's process, broker socket and
  HubServer are up.
  - Restart windows for both engines should move to 15:42 or later. An A restart between 15:14 and 15:42 blinds B.
  - B's feed-silence watchdog reconnects only while a group's segment `is_open` (15:30 for NSE), as in A. It was
    not extended.
- **`.env` was not read.** A `KN_LIVE_ENTRY_CUTOFF_IST` or `KN_PAPER_LIMIT_*` override there would still apply.
- **Deploy:** nothing was restarted or deployed. It needs the operator's explicit yes, and the operator runs it.
  - Deploy A and B together, so A-vs-B stays comparable.
  - Restart after 15:42 and well before the next open.
  - B's UI dist is gitignored: build it and copy it on deploy.
  - Any NSE position open when an engine restarts after 15:19 is force-filled at boot, with the alarms.
- **Out of scope, as instructed:**
  - the option-pricing expiry close at 15:30;
  - index-future bars, still 15:29;
  - B's OI candles and `zones.py` futures basis, both still on the 15:30 close.
- **Research tools follow the new clock** (the backtest, RL env and tape replay), as in A.

## 7. Every intentional A/B difference

| # | Where | A | B | Why |
|---|---|---|---|---|
| 1 | `engine._last_known_exit_price`, last-price age | `_ltp_ts` (the LTP's arrival time) | the quote's `ts` when the quote holds that price; else `_ltp_traded_ts` (the trade time) | B has two clocks and no `_ltp_ts`. Same price, source and age for the same inputs. |
| 2 | `engine._force_eod_fill`, audit `fillPrice` | `result.fill.price` | `result.fill.price if result.fill is not None else round(price, 2)` | B's mypy gate. Same value. |
| 3 | `engine.health_snapshot` | `_eod_check()` after `bar_audit` | after `bar_audit`, before B's `duty_errors`, `zones` and `latency` lines | B has more health lines. Same check. |
| 4 | `engine._roll_day_state` | no such hook. `_eod_alarms` and the day-keyed `_eod_alarmed` are pruned by day lazily in `_eod_alarm`; `_exit_fail_note` and `_exit_failed_at` are popped on a full fill | also pruned at the IST day roll: today's alarm rows and kinds kept; the refusal maps of positions no longer held dropped | B's per-day-state rule. Alarms are the same as A's. |
| 5 | `docs/BOOKS.md:210-212` | (no such paragraph) | the two-clocks section's "15:20 flatten's last-price fill" now describes 15:14 and 15:19 | B-only doc text |
| 6 | `tests/test_nse_close_1514.py` | `_ltp_ts` in the fallback test; 12 tests | `_ltp_traded_ts` plus two-clock assertions; plus the day-roll test (13 tests; since §8 it is `test_the_day_roll_drops_yesterdays_deadline_state`, and both files gained A's seven review tests) | Follows 1 and 4 |
| 7 | `tests/test_full_tape.py` | the window test | the window test plus B's end-to-end post-close hub frame test (tape, OI archive, no bar, no `late_qty`) | Pins R1 on B's own tick path |
| 8 | `CHANGES.md` | A's | this file | |

---

## 8. Review findings of 17 Oct and their outcome

The same six findings as engine A's `CHANGES.md` §7 (R1-R6). The reviewer's probes reproduced every one in B, with
the same numbers as in A. All six are fixed in B with the same code as A: the patch was applied hunk by hunk from A,
and the changed functions are byte-identical in the two engines. The exceptions are B's own `_roll_day_state` and the
earlier intentional differences in §7.

Each test fails on `3d42b52` and passes after the fix. The tests were run against `3d42b52`'s `kotsin_nse/` with the
new tests in place, and all failed on their behaviour assertions.

| # | Finding | Confirmed? | Outcome |
|---|---|---|---|
| R1 | A backoff from before 15:14 delays the 15:14 flatten | Yes. Probe: `_exit_retry_at` = 15:14:05, OPEN at 15:14:00 | Fixed |
| R2 | Alarms are deduplicated per position, not per day, and the deadlines read the time of day only | Yes. Probe: day-2 count stayed 1, line green. Here `_roll_day_state` kept held positions' keys on purpose | Fixed |
| R3 | A restart turns the `nse_out_by` line green | Yes, from the code | Fixed |
| R4 | The forced fill is stamped with the pass's start | Yes. Probe: both fills at 15:19:00 with the wall clock at 15:20:10, 0 alarms | Fixed |
| R5 | A recorded-only trigger gets no level reading | Yes. Probe: `route_events == []` | Fixed |
| R6 | A graded-F 14:45 trigger is not labelled on the lists, and RT-X's card shows the cutoff reason | Yes. Probe: the same F row and cards as A | Fixed |

### Changes (B line numbers; the rationale and decisions are A's §7)

| Finding | File:line |
|---|---|
| R1 | `backend/kotsin_nse/engine.py:924` `_exit_failed_at`; `:6783`, `:6832` set; `:6859`, `:6865` cleared; `:6503` `_backoff_before_flatten()`; `:6673` `_exit_now` |
| R2 | `backend/kotsin_nse/market/session.py:183` `held_overnight()`, `:190` and `:196` take `opened_ts`; `engine.py:921` day-keyed `_eod_alarmed`; `:6624` `_eod_alarm()`; `:6618` `_eod_still_open()`; `:6647` `_eod_check()`; `:6287`, `:6583`, `:6894` pass `opened_ts`; `:6588`, `:6896` "held overnight" text |
| R2, B only | `engine.py:7444-7455` (`_roll_day_state`): keeps today's alarm rows and raised kinds only, rather than the held positions' kinds. `_exit_failed_at` is pruned with `_exit_fail_note`. |
| R3 | `engine.py:387` `EOD_ALARM_KINDS`; `:7392` `_load_eod_alarms()`; `:950` called in `start_core` |
| R4 | `engine.py:6286` (`_manage_positions`): the clock is read per position; `:6582` (`_force_eod_fill`): the clock is read again after the snapshot, and `eod.still_open` is raised there |
| R5 | `engine.py:3125` (`_record_after_cutoff`): `_read_route` runs last, guarded; `:4669` (`book_cards`): no counter-trend button on a recorded-only trigger |
| R6 | `engine.py:2526-2530` (`_decide`): the cutoff is added to the `NOT_PUBLISHED` reason; `:2648` `_graded_f_rule()`, also used by `_handle_unpublished`; `:3125` the graded-F skips and the `gradedF` list; `:4640` (`book_cards`): the TAKE reason stays the graded-F rule |
| Docs | `docs/BOOKS.md:36-41`, `:45-53` |

### Tests

- `backend/tests/test_nse_close_1514.py`: A's seven new tests (pure deadline test at `:104`; `:415`, `:447`, `:469`,
  `:500`, `:523`, `:556`). They are unchanged from A, because none of them reads B's two clocks.
- **B only, changed:** `test_the_day_roll_drops_the_deadline_state_of_closed_positions_only` became
  `test_the_day_roll_drops_yesterdays_deadline_state` (`:389`). Yesterday's raised kinds go, even for a held
  position. Today's stay. `_exit_failed_at` is pruned with `_exit_fail_note`. It pinned the old keys, and fails on
  `3d42b52`.
- `backend/tests/test_entry_cutoff_and_carry.py:168` (extended) and `:267` (new): identical to A.

### Results after the fixes

| Check | Result |
|---|---|
| Copy check | `python -c "import kotsin_nse; print(kotsin_nse.__file__)"` → `…/close1514/engB_work/backend/kotsin_nse/__init__.py` |
| `PYTHONPATH=. pytest -q` | **1164 passed** (1156 + 8 new; the day-roll test was rewritten in place) |
| `ruff check kotsin_nse tests` | All checks passed |
| `lint-imports` | 5 kept, 0 broken |
| mypy gate (`../scripts/mypy_gate.py`, scratch venv as in §4) | Exit 1, with output byte-identical to the base `71036d2` and to `3d42b52`: the same 22 errors that predate this work, and 0 new |
| Time-of-day sweep (`fakeclock`) | 1164 passed at each of 00:30, 10:00, 15:16 and 15:21 IST |
| Reviewer's probes | All three exit probes now fail their "expected the defect" assertion. The cutoff probe shows two `counter.route` events and the cutoff in the F row's reason. |
