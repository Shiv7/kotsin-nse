# Pivots — the frozen contract

Frozen 2026-09-23 after auditing the levels against NSE bhavcopy and the old stack. Everything
below is pinned by `tests/test_pivot_freeze.py` (formula and period selection, on RELIANCE's real
September sessions) and `tests/test_daily.py` (the data plane). Change any of it and a test fails
first.

## 1. Formula

Classic (Kite) convention — `bars/pivots.py::classic_pivots`:

```
P  = (H + L + C) / 3          R1 = 2P − L        S1 = 2P − H
R2 = P + (H − L)              S2 = P − (H − L)
R3 = P + 2(H − L)             S3 = P − 2(H − L)
R4 = P + 3(H − L)             S4 = P − 3(H − L)
BC = (H + L) / 2              TC = 2P − BC       (sorted: TC is always the upper edge)
```

**Classic only.** Fibonacci and Camarilla levels are still computed (`classic_pivots`) for display,
but neither enters the confluence engine: `pivot_points` emits only the eleven classic levels, and
every stop, target, wall, option-ladder rung and ratchet step is downstream of it (Camarilla off
since 2026-04-13, Fibonacci off since 2026-09-23).

`P, R1/S1, R2/S2, CPR` are identical across every platform. Only the outer
rungs differ from the floor-trader form (`R3 = H + 2(P − L)`); the dashboard, Kite and this engine
all use the Classic form. `kotsin-nse pivots SYMBOL --for DATE` prints both so a number from anywhere
can be matched.

## 2. Inputs — the only accepted source

**The broker's own daily candle** (`V2/historical/…/1d`, `BarSource.REST`). It equals NSE bhavcopy
to the paisa. Never a bar rolled up from intraday candles (5paisa's stop at 15:15, so the close and
any late-session high/low are missing) and never a bar the aggregator built from ticks (its close is
the last print, not the exchange's closing price). `bars/daily.py::is_official` is the gate.

| timeframe | source period | selection |
|---|---|---|
| daily | the previous **trading** session | last REST bar dated strictly before today (`previous_session`) |
| weekly | the previous completed Mon–Fri | `periods.weekly` (ISO week), running week excluded by calendar |
| monthly | the previous completed calendar month | `periods.monthly`, running month excluded by calendar |
| future | daily | same rule, front contract |
| options | daily (+ weekly in the report) | same rule, 8 OTM strikes, thin-bar and zero-range guards (`instrument/legs.py`) |

Levels are fixed for the whole session. Nothing recomputes them from ticks. Holidays: `data/holidays.txt`
via `TradingCalendar`; when the file is incomplete the market is the calendar (see §4).

## 3. When they are fetched

| moment | what happens |
|---|---|
| boot, any time of day | daily cache (`data/daily/*.json`) seeds the store instantly → `_backfill` refetches every name's `1d` from REST (REST wins over cache) → zones compute lazily → leg ladders load in the background |
| day roll (IST date changes) | daily series refetched, zone cache cleared, leg ladders reloaded for the new day |
| `daily_refresh_hm` (08:30, 15:45 IST) | daily series refetched: after the NSE close so the official close lands the same evening, before the MCX open so an overnight-persistent process starts on official candles |
| every `pivot_repair_interval_s` (120 s) | `bars/daily.py::audit` runs; anything **missing / unofficial / stale / short** is refetched (bounded per pass); legs that failed are retried; a stale set is refetched once per expected session, then left alone |

Every successful REST refresh rewrites the cache atomically, so the file on disk is always the
last known official series.

## 4. Corner cases, and what the engine does

| case | behaviour |
|---|---|
| mid-session restart | REST refetch → identical levels (idempotent; verified 2026-09-23 09:33). The live-built partial bar of the running day is excluded by `previous_session` and can never overwrite a REST bar (`store.py` PARTIAL guard). |
| broker historical API down at boot | cache seeds the previous session's official candles; zones and ladders are real immediately; repair loop replaces them when REST answers |
| one name's backfill fails | recorded in `_daily_failed`; the repair loop retries it every interval; `zones_for` returns no zones for it rather than stale ones, and **does not cache the failure** |
| process stays up overnight | day roll + 08:30 refresh replace yesterday's tick-built daily bar with the official candle before any pivot is computed |
| leg fetch fails (rate limit, blip) | 3 attempts with backoff inside `LegPivotLoader._one`; still-missing legs retried by the repair loop; guard refusals (thin / zero-range) are counted separately and not retried |
| holiday not in `data/holidays.txt` | every name's latest session predates the calendar's expectation → `holiday_suspected`; reported in health, no refetch storm |
| holiday in the file, market actually open | `expected_prev` is wrong-early; names look "fresh" — the audit cannot detect this. Keep the file right. |
| machine clock not IST | all "today" decisions use `market.session.ist_today()`; a UTC box gets the same answers |
| 15:15 cutoff on intraday REST bars | affects backfilled 30m/1m bars only, never the daily series; live ticks cover 15:15–15:30 |

## 5. Observability

Health check `pivots_ready`: `ok` when every underlying that has any candles at the broker holds an
official previous-session bar with ≥ 25 daily bars, no daily fetch is in the failed state, and no leg
is in the failed state after retries. A listed contract with no candles at all (COTTON, KAPAS, the
MCX indices) is `dormant`: reported in `detail`, asked once per session, never a fault. `detail` carries the audit summary
(`N/M names on the official previous session; k missing; …`) and the leg counts. `/api/leg-pivots`
reports `loaded / failed / refused`.

## 6. How FUDKII-RT-X exits use the ladders (operator's design, 2026-09-23)

`RT_X_LIMITS.own_ladder = True`, `reproject_stop_s = 10`, `peak_giveback_pct = 3`, `sustain_s = 75`:

- **Ladder:** the contract's **own** daily + weekly classic pivots (`LegPivotLoader`, weekly from the
  same candles, ≥ 3 sessions), merged (`mtf_rungs`, 2 % tolerance) and sorted; **T1–T4 are the rungs
  above the entry premium**. A rung the contract gapped over is not a target. Nothing is
  delta-projected onto the option.
- **Equity trigger:** if the underlying reaches its own T1 first, the option's price at that instant
  *is* T1 and the higher own rungs follow it.
- **Touch** of Tn → one lot out (the last rung takes the rest); the hard SL steps to T(n−1)
  (breakeven for T1). **Sustained** — 75 s continuously at/above Tn *and* a 1-minute close at/above it
  since the touch — → the hard SL steps to Tn; for T1 that arms the give-back.
- **One rising stop:** `ratchet_sl = max(stepped rung SL, peak − 3 % [spread-floored])`, never lowered;
  trading through it ends the trade at once. Before the T1 touch the option-side stop is the equity
  stop through **live** delta (re-projected every 10 s), with the 75 s sustain and the 9 % hard floor.
- No bar time-stop; NSE positions flatten at 15:20 IST (the graded-F shadow at 15:24, out by 15:25), MCX at 23:20.

**Three RT books off the same fill** (`risk/limits.py`, since 2026-09-23 evening). Every FUDKII
NSE fill is mirrored into RT-X, RT-N and RT-Y, each on its own wallet; only the exit differs.
The give-back band is read live (`ExitEngine._band_level`), never folded into the stepped SL, so
the rung SL and the band keep their own exit rules:

| | RT-X | RT-N | RT-Y |
|---|---|---|---|
| ladder | own daily+weekly rungs above entry | own daily R1–R4 above entry | own daily+weekly rungs ≥ 0.5 × expected daily move above entry |
| arming | T1 touch pays a lot; T1 sustained (75 s + 1m close) arms | equity T1 touch, or a 1m close over own R1, pays a lot and arms at once | as X, on the higher first rung |
| stepped SL | breakeven at T1 touch → T1 on sustain → T2 … | breakeven at arming → rung on sustain | one rung behind (`sl_lag`): breakeven until T2 touches, then T1 … |
| band | peak − 3 % | peak − 2 % | peak − max(10 %, 0.25 × expected daily move) |
| band exit | one read through | three consecutive reads | 75 s continuous breach |
| rung SL exit | one read through | one read through | 75 s continuous breach (`post_arm_sustain`) |

Expected daily move (`market/iv.py::expected_move_frac`) = spot × IV(name) × √(1/252) × δ / premium:
the option's own volatility unit, 60–200 % of premium on 2026-09-23 — which is why a 2–3 % band
scratched every runner in the replays.

**Entry gate — RT-X and RT-Y only** (`RiskLimits.dried_volume_v = 0.85`): the mirror is skipped
when the trigger 30m bar *and* the one before it are both under 0.85 × the T-2…T-7 volume baseline
(floor 1000, `bars/indicators.py::dried_volume` — the reference RT router's R1) on the underlying
**or** on its front future (`Engine._volume_surges`; the future's candles come from the broker at
twin time, a leg without data is absent, never dried). RT-N takes every fill as the control. The skip
is written on the ENTRY alert card (`card.skipped`).

**No premium floor for the FUDKII family** (operator, 2026-09-23 evening): the parent, the RT
twins and the CT fades select without the shared ₹5 floor (`Engine.selection_policy_for`); FUKAA
keeps it. On the cheap contracts this admits the δ-projected option stop is a tick or two, so the
stop is floored at `MIN_STOP_TICKS` = 8 ticks below the premium at entry (`floored_option_stop`)
and in the RT engines' re-projection (`RiskLimits.min_stop_ticks`) — the setting the replay of the
23-Sep sub-₹5 rejections was run with (RT-X −31.6k, RT-N −23.3k, RT-Y +13.0k gross on ten
contracts; the operator chose to trade them).

**Every book is independent** (operator, 2026-09-23 evening): the position counts and the money a
book's entry is checked against are its own (`risk/exposure.py`, `total_capital` = the book's
wallet). A parent fill spawns RT-X/RT-N/RT-Y (and CT-X/CT-Y on a COUNTER); none of them count
against the parent's caps, nor the parent against theirs. Before this the parent's
`max_positions_all_books=6` counted the twins, so the parent stopped entering after two fills and
a 09:45 burst was a lottery over which two names got the slots. `GradePolicy.min_stop_atr_filter`
(a filter, off by default; the evidence is on the field) can grade a signal whose stop is inside one
bar's noise as F.

**Counter-trend books — CT-X and CT-Y** (`strategy/counter.py`, 2026-09-23 evening; the reference
stack's wall-strength COUNTER, live there since 2026-09-11). On every FUDKII trigger the wall ahead
of the close is scored: every daily/weekly/monthly classic level inside the trigger candle on the
signal's side or within 0.5 × ATR30m past its extreme, weighted 1d 4.0 / 1wk 3.2 / 1mo 2.0 × nearness
rank (1.0 / 0.8 / 0.6 / 0.4), clustered within 0.25 × ATR30m. A genuine ST flip into a wall ≥ 5.2 is
**COUNTER**: the fade is entered by CT-X — the opposite OTM from the same selector, stop and targets
from `compute_confluence` for the flipped direction, sized under RT-X's limits, exits under RT-X's
policy — and mirrored into CT-Y (RT-Y's exits). Anything else is IN_TREND and CT-X/CT-Y stay flat.
The route and the wall are stamped on the ENTRY card (`card.route`); a COUNTER with no wall on the
flipped side is recorded as `COUNTER_NO_PLAN`. The in-trend mirrors (RT-X/N/Y) trade regardless of
the route — the fade is beside them, not instead of them.

**Which strike to buy, decided on its own terms** (operator, 2026-09-24 afternoon). The chooser
used to borrow the confluence target: the strike nearest T1. Those are not the same question. T1 is
the level the trade **exits** on, and on a distant one it put the strike where nothing trades —
KAYNES that morning anchored on 3800 against a 3523 spot, every candidate one-sided, trigger lost.

Two candidates now, and **only for choosing the strike**:

| | what it is |
|---|---|
| **A** | the strike one ATR30 beyond spot — where the move the trigger predicts actually gets to |
| **B** | the strike at the confluence target — where the move is expected to stop |

Decided on the combined **volume and open interest** (`strike_candidates`), because open interest
is lumpy rather than decaying with distance: RELIANCE that day held 12.5 m lots at the 1300 call
against 3.5 m one strike from spot, and KAYNES eight times more at 3600 than at either neighbour.
So which of the two is the tradeable contract is a real question. Two guards:

- **A delta floor** (`SelectionPolicy.min_delta` = 0.20). A strike that cannot respond to the move
  is not bought however much is parked on it. ETERNAL held *more* open interest at its target
  strike than one ATR out — 8.64 m against 7.65 m — on 0.15 delta.
- **A margin** (`oi_margin` = 1.5) before the further, less responsive strike is taken, on *every*
  liquidity measure both have. HAVELLS: 10 % more open interest for a quarter less delta is a bad
  trade; ADANIENT's 2.4x and INDIANB's 3.5x are not.

Validated on all sixteen of that day's signals: two candidates offered in 5, liquidity reversed the
order in 2, the delta floor blocked the target strike in 2, one strike served both in 7, and no
signal was left without a candidate. Neither candidate being tradeable falls through to the rest of
the chain, most-traded first, and the substitution is logged (`strike.fell_back`).

**The exit ladder is untouched.** `Signal.stop` and `Signal.targets` are still the confluence
engine's, for the parent and every twin; a test asserts the strike path writes neither.

**A strike is chosen on a price that is known** (operator, 2026-10-01). The broker's REST snapshot
(V1/MarketFeed) carries a last price and **never** a bid or ask (47,448 of 47,448 replies by 28 Sep).
A strike subscribed at the trigger holds only that until the feed's first frame, about a second
later — and the walk used to read its 0 / 0 as "one-sided" and refuse it. With the near strikes
often too dear for 4 lots under ₹75,000, the walk lands on exactly those strikes: SONACOMS 09:45
(770 PE 10.15 / 10.70 on the feed the same second, the stock −2.5 % by 12:26), PAYTM 13:15 and 12
more, 28 Sep – 1 Oct. Three pieces had each covered part of it — quote the whole span (24 Sep),
never let a snapshot wipe a two-sided quote (28 Sep), wait for the two preferred strikes only
(28 Sep) — and none for the fallback. Now one rule replaces the preferred-only wait
(`Engine._choose_option`):

- a snapshot quote is labelled (`Quote.src`); without a bid and ask it is **unpriced**, never
  "one-sided" — only the feed can say a book is one-sided;
- the choice re-walks every 0.1 s while a strike **ahead of** what it would take can still be
  priced: on the live subscription, the feed speaking, each strike at most 2 s
  (`STRIKE_GRACE_S`), the choice at most 5 s (`QUOTE_WAIT_S`). A strike behind the choice, one off
  the feed, or a silent feed costs nothing; a priced first choice is taken at once;
- in the session's first minute (`OPEN_SETTLE_S`) a one-sided feed book is still filling — the
  09:15 carry (TATASTEEL 180 PE: 0 / 0 at 09:15:03, 2.42 / 2.53 at 09:15:27) waits up to 09:16;
- a card preview never waits; the replay (`quote_wait_s = 0`) never waits; every refused strike is
  counted in the reason (`(+N more)`), and `quotes.awaited` logs what was waited for and why it stopped.

**The delta floor for the fallback strikes — in SHADOW mode** (operator, 2026-10-01: "keep the floor in shadow mode").
Live, the walk still takes a fallback strike under 0.20 (`SelectionPolicy.enforce_fallback_delta = False`); the choice
carries its delta (`Selection.delta_shadow`) and the event `strike.delta_shadow` records what the enforced floor would
have bought instead (or that it would have refused the trigger), for the comparison on outcomes. The two candidates
are floored as they always were. The case for enforcing it: it used to guard only the two
candidates; the fallback walk had none, and with the near strikes often too dear for 4 lots it walked far out: its
trades under 0.20 (24 Aug – 1 Oct, 43 trades, 16 strikes on 14 names — TECHM 1620 PE, CHOLAFIN 1700, SOLARINDS 19000…)
lost ₹1,14,096, 26 % won, −6.2 % a trade against −3.3 % above it. The floor reads the moneyness estimate
(`estimate_delta`: 0.20 is about 3.75 % out of the money) — the option's own implied delta flagged only 12 of the 43,
and the far strikes lost as badly where a high IV lifted it. Enforced, it is read before any price, so a strike it
refuses is never waited for; the OTM strike nearest spot is exempt (a coarse grid — IDEA's ₹1 strikes); the card preview
applies the same policy; and a name whose every strike within ~3.75 % costs ₹75,000 or more for 4 lots is refused by
rule (SONACOMS 1 Oct 09:45: 790 PE ₹82.6k, 780 / 770 PE under the floor).

**Depth where it is used** (operator, 2026-09-24 midday). The 09:45 staleness was never the
exchange's: `recv_ts` was stamped when the engine got round to a frame, so "book age" measured our
own backlog. Depth was subscribed for 2,504 instruments — every underlying and every shortlisted
strike — delivering ~1,000 frames a second, each parsed and pushed through the microstructure
accumulator **inside the socket reader**. No strategy reads those metrics; they are archived for a
backtest that has never run. At a 30m boundary the reader fell 15 s behind, every book in the
engine went stale at once, three orders were refused and the breaker halted every book. Four
changes, in this order:

1. **Frames are stamped on arrival**, and `feed.dispatch_lag_ms` reports how far behind the reader
   is. Book age now means the age of the data, not the age of our last turn.
2. **Depth follows what is about to be priced** — open positions, live cards, the strikes the
   selector is weighing and their legs — reconciled once a second (`Engine._sync_depth`, capped by
   `KN_DEPTH_MAX_SUBSCRIPTIONS`). `KN_DEPTH_ARCHIVE_SYMBOLS` keeps a small, declared sample of
   underlyings on depth permanently, for the archive and nothing else.
3. **A cold contract fills on the quote the selector already fetched** (`book_from_quote`): one
   level, truncating at what the touch can absorb, instead of the degraded last-price path. It
   never replaces a live ladder — only a book that would have been refused anyway.
4. **Measured, then tightened.** Across the 11:15 and 11:45 boundaries of 2026-09-24 the reader's
   worst dispatch lag was **25 ms**, with no frame more than a second behind and no stale
   rejection — against 13,000–17,000 ms book ages at 09:45 that morning. The opening allowance was
   cut 25 s → 10 s on that evidence. It is provisional at 10 s until one post-fix 09:15 open has
   been measured; what it still covers is genuine per-contract sparsity, not our own backlog.

**What the 2026-09-24 open forced** (operator, same morning). Three NSE names came back with
depth 13–15 s stale at the 09:45 decision, each order was refused, and three consecutive rejects
tripped the gateway breaker — which reports the **engine** halted, not just the gateway, so every
open position in every book was force-flattened at market seconds after entry. Three changes:

- **The reject fuse is eleven, not two.** `LiveCaps.breaker_consecutive_rejects` = 12
  (`KN_LIVE_BREAKER_CONSECUTIVE_REJECTS`): eleven consecutive rejects are tolerated, the twelfth
  trips it.
- **The depth window widens across the opens.** A paper fill may be priced on depth up to
  25 s old between 09:00 and 09:55 IST, and 6 s the rest of the day
  (`PaperMatcher.age_limit_ms`, `KN_PAPER_OPEN_MAX_BOOK_AGE_MS` / `KN_PAPER_MAX_BOOK_AGE_MS`).
  A session's first minutes deliver depth in bursts; the rest of the day has no such excuse.
- **One exchange per book.** `Engine.SEGMENT_BOOKS` reserves MCX_FO for FUDKII-RT-MCX, and
  `book_trades()` gates the twin mirror, every direct entry and the trigger-card page: the
  commodity book never sees an NSE trigger and the NSE books never see a commodity one. (There is
  no currency segment in this engine at all — `Segment` is NSE_EQ / NSE_FO / NSE_IDX / MCX_FO.)

**One session on the page, all of it** (operator, 2026-09-24). Three rules, one for each place a
signal could go missing:

- **Nothing is capped for display.** `/api/alerts` returns every alert of the session (`limit` is a
  caller's choice, not a policy) and the page asks for all of them. The per-book ring is a ceiling
  a session cannot reach (5000), not a page size. PIVOTBOSS's *global* daily cap — 30 firings a day,
  spent in arrival order across 216 underlyings that close the same 30m bar, so the survivors were
  the earliest rather than the strongest — is off; its per-scrip cadence (2 a day, 60-minute
  cooldown) stays, because one symbol not repeating itself is not a signal lost.
- **The page is emptied at 00:30 IST** (`KN_ALERTS_RESET_IST`, `alerts/engine.py::reset_day`), for
  every book and twin together: rings, counts, the detectors' caps and cooldowns, the living book,
  and the engine's `_signals_today`. Yesterday's triggers stay on the ledger and on the trigger-card
  tabs, which are read per day. The slot is half an hour past the date roll on purpose — 5paisa
  refuses every login for ~20 minutes past midnight IST.
- **Thirty concurrent positions per book** (`RiskLimits.max_positions_per_strategy`), parent and
  twin alike, each against its own wallet and its own count. It was 3 for the parent while its own
  twins ran to 30, so on a busy morning the same trigger filled in four books and the parent was
  the one that ran out of slots. One position per name per book is unchanged.

**The tick tape** (`ops/tape.py`, 2026-09-23 evening). Every rule in the table above is a
wall-clock rule, and until now the only record of the prices they were evaluated against was the
broker's 1-minute candle. The tape writes the top of book of every held contract (and its equity and
front-future legs, and the strikes the selector passed over) once a second while it matters, so a
stop that fired can be re-run second by second through the same `ExitEngine`
(`research/tape_replay.py`, `kotsin-nse tape --day … --code …`). It rolls at 15 sessions; the rows of
contracts that were actually traded are set aside and kept a year.

**Per-name implied vol (the stock's own VIX).** The option ladder's merge tolerance is
`k(name) × ATR30(parent) × δ / premium` (`market/iv.py`), where `k(name)` bands today's ATM implied
vol of the front expiry against the name's **own** median IV (≥ 10 sessions; India VIX until then).
Not IV/realised — a single stock's IV always sits above its realised. History is seeded from the
option candles the legs fetch and replaced by live once-a-minute points, persisted under
`data/iv/<SYMBOL>.json`. The equity zones stay on the India VIX band. `/api/chain/{symbol}.stockIv`
shows the IV, the median, the band and the `k` in force.

## Zones: one builder for live and the backtest (2026-10-03)

`bars/zones.py build_zones` is the only place zones are built — `Engine.zones_for` and
`BacktestContext.zones` both call it (the backtest used to cluster at a flat 0.25 % while live used
`k × ATR30 / price`; 14.5 % of publish decisions differed). It also:

* fixes the width **before the session**: ATR(14) over the 60 decision bars before today, over the
  previous close — a restart at 13:15 gives the same zones as the 09:00 boot (it used to take the
  ATR and LTP at the first call, which changed the clustering on ~30 % of symbol-days);
* refuses an NSE previous session that is still 5paisa's **provisional** daily candle (stamped
  09:15) until the end-of-day one (00:00) lands; MCX stamps its daily candle at the first trade and
  is not tested;
* refuses a session whose official close and its own last 30m close are more than 8 % apart — a
  corporate action adjusted the daily series and not the 30m one (VEDL ×0.374).

A refusal is listed in `Engine.zone_refusals` and is never cached.

`kotsin-nse zones SYMBOL --for DATE` prints one day's zones; `kotsin-nse zones ALL --k K --wall W`
prints the distribution over the cache. Measured 2026-10-03 (40 sessions × 212 names):

| | k 0.30 (live NEUTRAL) | k 0.45 |
|---|---|---|
| zones that are a single level | 88.8 % | 84.5 % |
| walls per symbol-day | 3.2 | 4.1 |
| a wall within 3 ATR above / below the close | 62 % / 52 % | 71 % / 62 % |
| no wall above at all | 13.5 % | 6.8 % |
| median width | 0.138 % | 0.207 % |

`k` and `WALL_MIN_STRENGTH` are the operator's to choose; this is the measurement.
