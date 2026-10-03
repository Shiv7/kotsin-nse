# The books — who takes what, and what stops whom

Operator, 2026-09-26: "a fudkii signal goes to all variants at the same time at once. each variant and
twin assess it at the same time in parallel to decide if it fullfill teh specific logic of that
particular strategy, ifyes, then it gets qualified for trade, if not, stat teh reason and record it in
teh dahsvoard on teh card." · "two variants or twins can trade the same trade but 1 trade cannot be
taken twice by the same strategy while the previous one is active/live."

Code: `engine.py` — `_handle_signal` (the trigger, once) → `_enter_book` (every book, concurrently).

## 1. The books

| Book | What it is | Takes | Its own entry rules | Its own exits | Purse |
|---|---|---|---|---|---|
| **FUDKII** | the parent strategy: ST flip + close outside the Bollinger band on 30m, graded on its pivot confluence; its stop is the nearest pivot WALL (strength ≥ 5.2) behind the close — walls only, NSE and MCX alike | its own trigger | its sizing incl. the costs-against-T1 test | the equity levels projected through delta; 40 % / 30 % / 20 % / 10 % of the position at T1–T4; the stop to breakeven after T1; from +3 % on the premium a trail that gives back 40 % of the peak's gain; a time stop after 8 bars (4 h) | ₹10 L |
| **RT-X** | in-trend variant | the FUDKII trigger | dried volume (equity + future) | the option's own multi-timeframe ladder; a lot per rung; a 3 % give-back line off the peak | ₹10 L |
| **RT-N** | in-trend variant, the ungated control | the FUDKII trigger | none | the option's own daily R1–R4; arms when the underlying reaches its T1 or its resting R1 sell is touched (a 1-minute close over R1 with limit orders off); 2 % give-back. Its option stop is the option PRICED with the stock at its stop (the option's own implied volatility and time left, not the straight-line delta) and never more than 35 % below the premium paid (operator, 27 Sep; 1–25 Sep: −₹2,05,273 → −₹1,80,291) | ₹10 L |
| **RT-Y** | in-trend variant, gated (the paper A/B) | the FUDKII trigger | dried volume + gate B: breadth > 50 %, no key pivot within 0.5 ATR ahead, no 09:45 gap ≥ 0.3 daily ATR its own way | T1 = max(the option's own T1, entry +5 %) on every route — the option touching it, its resting sell, or the stock reaching its own T1 (which sells nothing below an own T1 over +5 %); with no own ladder T1 is entry +5 % itself. T1 sells one lot, the stop goes to breakeven and a 3 % give-back line trails the peak for the rest; T2–T4 one lot each; any stop exits everything at once. Its option stop is never more than 25 % below the premium paid (operator, 28 Sep; 1–28 Sep replay: ₹16,973 → ₹19,732 on 28 trades). A stop floor is built and tested but **off** (`min_equity_stop_atr`, validated at `RT_Y_STOP_FLOOR_ATR` = 0.5, operator decision pending, 1 Oct): when on, a planned stock stop nearer than 0.5 ATR30 to the trigger's close moves out to it at the fill, the option stop re-projected under the 25 % cap, and the entry checks read the same stop (HDFCLIFE 1 Oct: 0.18-ATR stop hit by a print at it, then the call ran 14.60 → 18.20; Aug replay +₹2,830, live 28 Sep–1 Oct +₹7,535 vs −₹2,200) | ₹10 L |
| **RT-Y wide (shadow)** | RT-Y with one number changed | RT-Y's own fill: same contract, price, size and instant | none of its own — it opens when RT-Y fills, unless its own purse is halted, short of money or at its exposure cap (each recorded) | RT-Y's, with the equity stop 1 % further than the plan's — never nearer than RT-Y's own stop (should RT-Y's floor be switched on), no floor of its own — and without RT-Y's 25 % cap (1 Oct: 0.5 ATR30 is at most ~1 % of price, so the plan's stop 1 % further is already the wider; inheriting the floor only deepened its stop-outs) | ₹10 L |
| **RT-Y graded F (shadow)** | RT-Y's rules on the triggers FUDKII does not publish (operator, 28 Sep) | a trigger FUDKII grades F (NOT_PUBLISHED), NSE only; where no pivot cluster ahead makes a target, the raw pivot zones ahead, nearest first, are its ladder | RT-Y's gates, run BEFORE the strike choice (a trigger they refuse costs no broker call) | RT-Y's, incl. the 25 % cap — not its 0.5 ATR stop floor (mixed in the 1 Oct study) | ₹10 L — its wallet and its cards (the unpublished triggers only) on its own Alerts tab, FUDKII-RT-Y-F (29 Sep) |
| **CT-X** | counter-trend fade | the fade plan of a FUDKII trigger routed COUNTER; a plan whose stop is nearer than 0.5 ATR30 to the close is graded F (every counter-trend plan) | none | own ladder, RT-X's policy | ₹10 L |
| **CT-Y** | counter-trend fade + the 09:45 gap fade | (a) its own 09:45 gap fade of a trigger that gapped ≥ 0.3 daily ATR its own way; (b) the same fade plan as CT-X | stands aside from (b) on a trigger it already gap-faded | own ladder, RT-Y's policy (incl. the +5 % T1 floor) | ₹10 L |
| **CT-M (shadow)** | the market-against fade (operator, 3 Oct: "fade when the market is clearly against" as a shadow, named FUDKII-CT-M) | every published NSE trigger at most 45 % of the market agrees with — breadth, the share of NSE names past today's open the trigger's way (`CT_M_MARKET_AGAINST_MAX`); a trigger above that is a skip on its card with the share | none of its own: CT-Y's fade plan (`Engine.fade_plan`) — the opposite OTM, the stock stop 1 ATR30 past the close, the walls on the fade's side (else one target 1 ATR30 away); beside CT-Y's gap fade, never instead of it | CT-Y's: RT-Y's policy incl. the +5 % T1 floor, no 25 % premium cap | ₹10 L — paper, on the Shadow page; never in the day's totals. 25 Sep–1 Oct actual replay: 3 fades, all won, +₹19,127; option model 24 Aug–1 Oct: 73 fades, −₹80,934 (−₹76,238 to 11 Sep, −₹4,696 after), against −₹2,17,547 for the same triggers in-trend. Every fade and skip: the Shadow page's *Market fade · CT-M* tab; a CT-M chip on every card |
| **RT-MCX** | the commodity book | every FUDKII trigger on MCX (FUDKII's purse never trades MCX) | none | own ladder, RT-X's policy | ₹30 L |
| **FUKAA (shadow)** | a separate strategy derived from FUDKII's context — FUDKII's trigger admitted only when volume confirms it; in SHADOW since 2 Oct (inputs fixed) | its own signals | its own, incl. the costs-against-T1 test: 4x volume on T or T-1 (else watched one bar), composite ≥ 60 | never trades: each signal is a SHADOW row and a `fukaa.shadow` event with its inputs and alignment (market with / against, OI quadrant); the Shadow page's *FUKAA · shadow* tab | ₹10 L, untouched |

## 1b. The clock (operator, 29 Sep)

- **Last NSE entry: 15:15** for every book — a trigger decided at 15:15 (the 14:45 bar) may still be entered
  in that minute, nothing later (`PAST_ENTRY_CUTOFF`, recorded on the book's card). **The graded-F shadow:
  before 15:23.** MCX keeps its own session. Paper and live alike (a live order still stops at 15:10 first).
- **Every NSE position is out by 15:25:** the books keep the 15:20 flatten (moving it to 15:24 cost −₹29,932 over
  24 Aug–28 Sep in the replay — 89 trades held to the close); the graded-F shadow, whose entries run to 15:22,
  flattens from 15:24.
- **A trigger decided at the close** — the 15:15 bar, complete at 15:30 — is not traded that day. It is kept
  (`CARRIED`, the `carry.queued` event, safe across the overnight restart) and at the next session's open it
  enters on its stock's **first print after 09:15**: through the trigger's stop → dropped (`carry.dropped`);
  otherwise — in favour (the open on the trade's side of the close) or in the zone (between the stop and the
  close) — re-issued at the open (its card reads "fired 09:15", entry = the open, targets already passed
  dropped) and routed as it would have been: published → the in-trend books, not published → the graded-F
  shadow. No fade. No print within 5 minutes of the open → `carry.expired`. The 24 Aug–28 Sep stock-level
  backtest of the idea lost (56 entries, −0.13 % to −0.26 % a trade before option costs): the operator's
  call, to be judged on its own trades.

- **The MCX roll (operator, 30 Sep):** a commodity future 5 calendar days or fewer from expiry is not the one
  read or traded — the next month is. The chart, the trigger, RT-MCX's entry and the levels all use that
  contract; a rolled commodity never takes the expiring month's cached daily candles (no levels rather than wrong
  ones until the broker answers). On 29 Sep ALUMINIUM's expiring contract traded 53 lots against 1,438 in the next
  month. NSE is not rolled here.

## 2. The funnel — decided ONCE per trigger (a reason here stops every book)

1. The trigger itself — FUDKII's strategy logic and grade. A trigger FUDKII grades F (a SuperTrend flip with
   the close through the band, but no publishable grade) is recorded as `NOT_PUBLISHED` on FUDKII's card and
   reaches ONE book — the graded-F shadow; every other book sees FUDKII's published signals only, until each
   book's own grade rule is proven (phase19). Every path runs side by side, not one after another.
2. The underlying is in the universe (`NO_UNDERLYING`).
3. The segment: an MCX trigger goes to RT-MCX alone (`ROUTED`).
4. The contract: the selector's strike for the trigger's direction — the fade's opposite strike for the
   fade books — whose 4 lots cost under ₹75,000: a strike whose 4 lots cost more is passed over for the
   selector's next choice, further OTM (operator, 22 and 27 Sep) (`NO_INSTRUMENT` when none fits).
5. The δ-projected option stop and targets (each book floors the stop for itself) and, for the books
   that gate on it (RT-X, RT-Y), the equity and future volume reading — read once per trigger (the
   future's two REST calls), waited on only by those books: FUDKII places its order at once. A reading
   that fails is no reading and blocks no book.
6. The same trigger for the same books a second time (a re-run) is never entered twice. An operator
   TAKE is exempt: it asks again on purpose, and the per-book checks decide it — including a book's
   entry still being placed (`ALREADY_ENTERING`), so a TAKE never doubles a live order at the broker.

## 3. Then EACH book, all at once — a reason here stops that book alone, recorded on its card

In this order. The same checks apply when a book takes a signal on its own — FUKAA, RT-MCX's routed
trigger, CT-Y's gap fade, an operator TAKE — except the gates, which run only when a trigger is handed
to several books (FUDKII's four in-trend books; CT-X and CT-Y on a fade).

| Check | Refusal | Notes |
|---|---|---|
| an entry of this book already working on this name | `ALREADY_RESTING` | one trade per book per name while it is live (a position open on the name is refused by EXPOSURE) |
| an entry of this book being placed on this name (at the broker, not yet filled) | `ALREADY_ENTERING` | every entry counts as live from its first check — a LIVE order waits on the broker for up to ~30 s. The card shows the entry being placed; the refused attempt is an event (`book.already_entering`) |
| the engine is halted (operator halt, broker reconcile freeze) | `ENGINE_HALTED` | refused before any order — no order is sent, so nothing counts toward a breaker |
| this book's order breaker is tripped | `BREAKER` | see §6 |
| the book trades the segment | `WRONG_SEGMENT` | |
| its wallet is halted — daily loss ≥ 10 % (clears each morning) or drawdown ≥ 15 % (stays until that wallet is reset) | `WALLET_HALTED` | a halt stops only its own book |
| its own gates (table above) | `SKIPPED` + the gate | dried volume, breadth, pivot ahead, open gap, gap-faded |
| the underlying already through the signal's stop | `STOP_BREACHED` | SBILIFE 2026-09-24: stopped out 67 ms after the fill |
| its own size: exactly 4 lots, costing under ₹75,000, paid from its own purse — never fewer lots (a purse that cannot pay for 4 refuses the trade). RT-MCX alone keeps its old sizing: up to 4 lots within 1 % of its purse at risk and ₹1 lakh ("₹75,000 cap does not apply to any MCX trade") | `NOT_SIZED` | the costs-against-T1 test applies to the book that OWNS the signal (FUDKII, FUKAA, CT-X on its fade, CT-Y on its gap fade, RT-MCX on its routed trigger, a TAKE); a book taking another book's signal (RT-X/N/Y, CT-Y on CT-X's fade) does not have it |
| its own exposure: ≤ 1 position per name, its own ceilings | `EXPOSURE` | another book's positions never count |
| its money: the outlay held before the order | `WALLET` | the hold is counted while the order is at the broker, released on any failure |
| **its own entry order** (§4) | `LIMIT_UNFILLED` / filled | |

No book waits on another's fill, sizes from another's purse, or is stopped by another's halt.

## 4. The entry order (every book, PAPER with limit orders on)

| Time | Rule |
|---|---|
| 0 s | BUY LIMIT at the option's signal price if the book still contains it, else at the mid — never above the signal price + 3 % |
| 0–30 s | the limit does not move |
| 30–60 s | a signal price that left the book is followed to the mid, never above the cap. Nothing is chased: a 30-s race and "take the ask within the cap after 30 s" were both tested and are OFF (the entry-rule test, Sep 1–25 replay, all books net, on the stop rules of that day: this rule −₹7.82 L; + race −₹7.87 L; + taking the ask −₹9.00 L) |
| any time | the book halts, the engine halts, or the stock goes through the stop → cancelled, never filled |
| 60 s | not filled → missed, with how far the option ran |

## 5. Exits

Each position exits through its own book's exit engine. Target sells rest in advance for every book
(FUDKII: its share ladder; the own-ladder books: a lot per rung; RT-Y/CT-Y/shadow: T1 at max(own T1,
entry +5 %), entry +5 % itself when the contract has no own ladder; RT-N: its own R1 from the fill).
Each book's targets are its own — RT-X, RT-N, CT-X and RT-MCX have no +5 % floor. Any other exit
cancels the resting sell first — never two sells for the same lots. A stop exits every remaining lot at
once.

How an exit sells (PAPER with limit orders on; LIVE sells at once):

| Exit | Order |
|---|---|
| target | a SELL resting at the rung from the moment the contract is held — the next rung only, placed the instant the one below fills; fills on a touch |
| **urgent stop** — the stock through its stop (SL-EQ), the option's mid down 2 % or more in 30 s, or a book 2 ticks wide or less | **sold into the bid at once, through the depth for every lot** (operator, 3 Oct: "if the momentum is very high, then stoploss if not executed quickly and waited will attract high losses"; live 30 Sep – 1 Oct, 43 stops: +₹23,224 after the depth, +₹5,370 without DMART's collapse; the candle-built replay −₹5,209) |
| calm option stop on a wide book | the mid, walked to the bid, sold at the bid after 15 s |
| trail (the give-back line) | the mid, walked to the bid, 45 s — the walk beat the bid on 13 of 18 |
| 15:20 flatten | the mid, walked to the bid, 10 s |

Every order's trail keeps the book at placing and at the fill (or cross, or cancel) — the best bid and
ask, and five levels a side, price and quantity (`depthAtPlace`, `depthAtFill`, `depthAtCross`,
`depthAtCancel`).

**15:15 plan (recorded, not acted on).** At 15:15 each open NSE position's distance to its stop line
and its next target is written to the ledger as an `eod.plan` event with what the operator's rule
would do: stop line within 3 % → exit now; else target within 3 % → wait for it; else flatten at the
book's time as now (operator, 3 Oct: "if at 15:15 we are waiting for SL which is very near, we should
exit asap, in case the SL is away, then we wait if the target is close by"). 30 Sep – 1 Oct had one
such position in nine, so the rule is measured before it is used.

**Quote freshness — two clocks (review, 3 Oct).** Every quote and book carries when the engine SAW
it (`ts`: the feed frame's arrival, the broker call) and, separately, when the broker last TRADED
(`traded_ts`, 5paisa's TickDt). Age guards — the exit loop's 60 s staleness, the matcher's book age,
the background re-quote of held contracts — read only the first. The trade time decides only whether
a price is a newer print: a broker snapshot replaces a held quote only when it has a newer trade, so
5paisa's 5 s cache can never move a position's mid backwards, and an older cached print never counts
as a trade through a resting order. A held contract the feed cannot vouch for is marked fresh from the
newest trade known, without a bid or ask the feed cannot vouch for. Known gap (before this change too):
with the feed silent, a stale depth book and a snapshot carrying no bid, no fresh book can be built —
an exit then waits for the feed, or for the 15:20 flatten's last-price fill.

## 5b. Data-pipeline changes accepted 3 Oct (Shiv7's Stage 0–5, reviewed and fixed in phase35)

The operator accepted these four behaviour changes on 3 Oct; every book sees them alike, so the A/B
stays like for like.

| Change | What it does | Evidence |
|---|---|---|
| **Option stop held at the fill** | The delta-line books (RT-X, RT-Y, CT-X, CT-Y, the parent) keep the option stop stamped at the fill (entry premium − stock risk × the delta at the ENTRY spot), capped and floored as before. It used to be re-derived with today's delta: on an adverse move delta falls, so the line rose toward the entry and stopped the option out before the stock reached its stop. The stock stop still runs on every position and fires first when delta falls against the trade; RT-N keeps its priced stop. | MOTHERSON 1 Oct (RT-Y-F replay): −₹12,962 → +₹2,605 |
| **One zone builder** | Pivot zones cluster at k × ATR(14)/price as before, but the width is taken from the sessions BEFORE today (a restart no longer changes it), live and the backtest share the builder, and a provisional daily candle or a corporate-action basis sets no levels (re-asked every 10 min; the zones health line alarms from 09:20). | Replay 25 Sep–1 Oct: RT+CT +₹1,25,776 → +₹98,839 — mostly LAURUSLABS 25 Sep, whose grade-A wall was two levels 0.12 % apart merging at exactly the wall minimum |
| **SuperTrend over 120 bars** | FUDKII, the cards and the chart read the same converged window (55 bars put 16.7 % of flips on another bar; 120 bars 0 of 1,633). The boot backfill reaches back 12 trading sessions so a restart holds it. | Stage 3 measurement |
| **MCX volume floor in lots** | MCX volume is in LOTS (GOLD's 30m median ~214); a floor of 1,000 made 93–95 % of GOLD/COPPER readings read "dried", so RT-MCX skipped nearly every trigger there. The floor is 10 lots now: RT-MCX takes most of them. | Stage 2 measurement |

## 6. Order breaker, per book

A book's breaker trips after 12 CONSECUTIVE rejected entry orders of that book (operator, 2026-09-26:
"each fudkii variant, parent or twins required 12 consecutive rejection to trigger a halt. not total
12"). Only a real failure counts — no depth to fill against, the broker refusing. An exit, a duplicate, a
halt, a cap refusal or the engine not set up to send (`REJECTED_CONFIG`) never counts, and only an ENTRY
that goes through resets the book's count (an exit or target fill does not). A tripped book places no
entry, and its resting entries are cancelled, until it is reset — per book on the Risk page, or `POST
/api/control/reset-breaker?book=…`; its open positions keep their exits, and no other book is affected.
A trip survives a restart (trips and resets are on the ledger); a run of rejects short of 12 starts
again at 0 after one.

## 7. Order ids

`FII-RTX-260926-192510-007-EN-HINDUNILVR-1960CE-L4` — the book's code (`FII-P` parent, `FII-RTX`,
`FII-RTN`, `FII-RTY`, `FII-CTX`, `FII-CTY`, `FII-RTM` RT-MCX, `FII-RYW` RT-Y wide, `FII-RYF` RT-Y graded F,
`FKA` FUKAA), the IST
date and time, the book's order number that day, then what the order is and on what. The first part is
unique and fits inside the 38 characters of an id the broker keeps. What it is: `EN` entry, `TK` an
operator TAKE, `T1V1` a resting target sell (rung 1, first placing), and an exit's reason and rung —
`SLE0` stock stop at rung 0, `SLO` option stop, `TG` target, `TR` trail, `EOD`, `HLT`, `DL` daily loss,
`MAN` operator, `TS` time stop — with `R1` for a retry after a rejection and `X` for the cross at the bid.
Every exit, target and retry of a position carries that position's own entry id, so an order always
names the book and the trade it belongs to. Two exceptions: the wide-stop shadow opens on RT-Y's fill,
so its own ref (`FII-RYW-…`) first appears on its exits; a position opened before readable ids keeps
the old ones. A restart restores each book's order number from the day's orders and the positions'
refs, so a number is never reused.

## 8. LIVE mode (not armed)

Only FUDKII's orders go to the broker until the live limit-order manager is built (the §4 rules exist
only in PAPER; a live entry today is a market order). Every other book fills on paper against the same
live book — entries AND exits. When all FUDKII books go live:

* each book keeps its own orders and ids (§7) — its own order book — on the one broker account;
* the broker reconcile compares the SUM of the live books per contract with the broker's net position;
* the LIVE_CAPPED caps are each book's own: its positions, the live orders it SENT today (entries and
  exits, refused by the broker or filled; restored after a restart; an exit is never refused by it), its
  daily loss, ₹ per order;
* a live entry is refused unless the broker account's free margin covers it (fails closed: no answer, or
  no recognised margin field, refuses the entry — the field is confirmed on the first armed session).
  Today the check is per entry against a 15-s snapshot, not net of other live entries sent in the same
  second — the live limit-order manager (next phase) reserves margin per order;
* the broker keeps 38 characters of an order id; the engine polls a live order by the id as sent.
