# Shadow week 1 end to end (Tue 27 → Fri 30 Oct 2026), with the recorded data — written 2026-11-01

The strategy went into SHADOW on the night of Mon 26 Oct; the week is four sessions. 31 Oct and 1 Nov are the
weekend (the laptop slept on battery from Sat 02:47 — no session was lost by that, see §6).

## 0. What exists for each session

| session | live shadow file | replay (current rules) | 1m bars archive | flow (OFI/book) | F&O board | official OI (bhavcopy) |
|---|---|---|---|---|---|---|
| Tue 27 | yes — loop-0 rules (round walls, ₹25k, index legs) | `2026-10-27-replay-loop1.json` | full (from 09:19) | full | full | yes |
| Wed 28 | **0 signals — laptop asleep** | `2026-10-28-replay-loop1.json` (exchange bars) | 2 bars a symbol | fragments | fragments | yes (the roll) |
| Thu 29 | yes — loop-1 rules, ₹25k | = live | full | full | full | yes |
| Fri 30 | yes — loop-2 rules (14:30 cutoff, ₹1L); 11:29 restart, morning file kept | = live | full | full | full | yes |

"Current rules" = `wall_then_trail`, pdc `last`, no round walls, 0.6 ADR move cap, continuous H/L, 14:30 cutoff,
₹1,00,000 a trade. The 27/28 replays run loop 1 without the cutoff; no entry that week was after 14:20, so they are
the current rules. Charges at ₹1L: ₹127 a round trip (the two 30 Oct live trades: ₹124, ₹130), i.e. 12.7 bps.

**Calendar fact that matters for the OI data:** the October stock-futures series expired **Tue 27 Oct** (bhavcopy
futures OI by expiry: the 27 Oct contract 7.6 bn on 23 Oct → 1.1 bn on 27 Oct → gone on 28 Oct). So (a) the
engine's board on the afternoon of 27 Oct shows the expiring contract running off — `futures_state` LU with OI
−23 to −36 % is the expiry, not a read; (b) the 28 Oct official labels (143 SC, 59 LU, total OI −5.6 %) are the
roll. Open item: the board should follow the next contract on expiry day.

## 1. The market, session by session (NIFTY from the engine's REST daily store; breadth from the archives)

| | Tue 27 (expiry) | Wed 28 | Thu 29 | Fri 30 |
|---|---|---|---|---|
| NIFTY o / h / l / c | 22709 / 22830 / 22637 / 22750 | 22678 / 22778 / 22498 / 22699 | 22691 / 22962 / 22657 / 22937 | 22965 / 23118 / 22905 / 23053 |
| open → close | +18 bps, closed at 0.59 of range | +9 bps, 0.72 | **+108 bps, 0.92** | +39 bps, 0.69 |
| shape | range; 14:30 dip to 22673 bought back to 22762 by 15:00 | first bar to 22575, low 22498 at 10:20, climb to 22737 by 13:00 | trend day: 10:30 leg 22761→22871, afternoon 22831→22946 | low 22905 at 10:30 after the opening sell-off, then +170 to 23098 by 11:30; index up, market down |
| stocks up (open→close) / median | 72 % / +77 bps | 65 % / +56 | 72 % / +95 (49 % closed in the top fifth of their range) | **36 % / −54** (34 % closed in the bottom fifth) |
| official futures: % up, median px, total OI | 64 %, +0.42 %, +0.7 % | 69 %, +0.75 %, −5.6 % (roll) | 81 %, +1.00 %, +0.3 % | 26 %, −1.09 %, +0.2 % |
| official labels (LB/SB/SC/LU/flat) | 46/33/50/19/65 | roll: 3/1/143/59/6 | 45/21/57/5/90 | 17/49/27/45/80 |
| median stock PCR (OI) | 0.74 | 0.81 | 0.83 | 0.78 |
| engine board 10:15: futures LB/SB/SC/LU | 61/15/97/45 — "Strong Bull" 125 of 218 | lost | **114**/61/34/15 — Strong Bull 103 | 33/**142**/7/42 — Strong Bear 155 |
| market-wide equity buy/sell by half hour (all codes) | 09:00 1.10 · 10:30 1.30 · 13:00 0.75 · 14:00 0.85 · 14:30 1.12 · 15:00 2.88 | lost | 09:00 0.93 · 09:30 0.85 · 10:00 1.03 · **10:30 2.54** · 11:00–12:00 ≈ 0.95 · 12:30–15:00 1.15–1.75 | **09:00 0.47** · 09:30 0.82 · 10:00 0.80 · 10:30 1.22 · 11:00 1.20 · 11:30 1.34 · 15:00 1.56 |

The board's 10:15 composition described each day's breadth (SC-heavy → broad short-covering day; LB-heavy → the
trend day; SB-heavy → the broad down day). That is a market-level description of the same morning, not a forecast
(T2 in `2026-10-30-week-ofi-options.md` found nothing cross-sectional).

## 2. The strategy under the current rules, session by session

### Funnel (symbol-sides)

| session | with levels | open tests | BOS | through TC/BC | held retest | confirmed | taken | skipped (why) |
|---|---|---|---|---|---|---|---|---|
| 27 replay | 155 | 155 | 43 | 17 | 15 | 8 | 4 | GRASIM, ADANIENT, PIIND moved > 0.6 ADR; BANKNIFTY index |
| 28 replay | 170 | 170 | 32 | 20 | 18 | 9 | 7 | RECLTD (0.96 ADR), HINDUNILVR (0.63 ADR) |
| 29 live | 161 | 161 | 33 | 19 | 16 | 6 (7 signals) | 6 | AMBUJACEM 14:40 (0.80 ADR; it closed −155 bps from there) |
| 30 live | 168 | 158 (full day) | 34 | 14 | 13 | 2 | 2 | — (nothing after 11:33) |

About one symbol-side in six that passes the open test breaks structure; one in two of those goes through the
CPR edge; nearly all of those hold the retest; a quarter to a half of the held retests confirm. The cap on the move
from the open skipped 7 of 26 confirmations; the skipped ones were −30 to −155 bps from their entry at the close
(RECLTD/HINDUNILVR on 28 Oct and AMBUJACEM are the record; GRASIM/ADANIENT/PIIND were small).

### The 19 trades, with the readings at the entry

bps = `wall_then_trail` gross. net = at ₹1L after ₹127. NIFTY pos = NIFTY's position in its range so far at the
entry (0 = at the low). "rest" = NIFTY from the entry to the close. Board = futures / calls / puts state and bias
at the entry minute; mkt b/s = market-wide equity buy/sell in the previous 60 minutes; prev = the previous
session's official build-up (bhavcopy). 28 Oct has no board or flow (archives lost).

| session | entry | symbol | side | risk bps | first wall | exit | bps | net ₹1L | NIFTY pos → rest | board (fut / calls / puts, bias) | flow | prev official |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 27 r | 10:45 | ONGC | S | 39 | S1 39 bps | stop 12:30 | −38.7 | −514 | 0.77 → −9 | SB +7 % / SB / LB, Strong Bear | delta15 −114k; mkt 1.01 | **LB** (+7.0 % OI, +0.8 %) |
| 27 r | 14:00 | AXISBANK | S | 30 | S1 28 | trail 14:40 | +9.5 | −32 | 0.04 → +37 | LU (expiry) / SB / LB, Strong Bear | −56k; 0.89 | **LB** (+2.1 %, +0.9 %) |
| 27 r | 14:10 | NESTLEIND | S | 51 | PDL 37 | stop 14:35 | −50.5 | −632 | 0.01 → +41 | LU (expiry) / SB / LB, Strong Bear | −15k; 0.88 | flat |
| 27 r | 14:10 | COALINDIA | S | 43 | S1 67 | stop 14:35 | −42.8 | −555 | 0.01 → +41 | LU (expiry) / SB / LB, Strong Bear | −7k; 0.88 | SC |
| 28 r | 10:15 | BHARATFORG | S | 58 | S1 98 | stop 12:30 | −58.3 | −710 | 0.05 → +80 | — | — | SC |
| 28 r | 10:30 | NBCC | S | 46 | S1 41 | stop 11:00 | −45.7 | −584 | 0.07 → +83 | — | — | flat |
| 28 r | 10:50 | DABUR | S | 48 | S1 41 | stop 11:25 | −48.3 | −610 | 0.33 → +61 | — | — | **LB** (+6.7 %, +0.8 %) |
| 28 r | 12:35 | PREMIERENE | L | 55 | R1 141 | close | +13.6 | +9 | 0.93 → +10 | — | — | SC |
| 28 r | 13:00 | INDUSTOWER | S | 97 | S2 63 | trail 14:30 | +96.1 | +834 | 0.86 → −17 | — | — | flat |
| 28 r | 13:05 | TATACONSUM | L | 36 | PDH 60 | trail 15:10 | +42.0 | +293 | 0.83 → −14 | — | — | SC |
| 28 r | 13:15 | IDEA | S | 116 | S2 151 | close | +75.3 | +626 | 0.76 → −6 | — | — | **LB** (+1.0 %, +1.1 %) |
| 29 | 10:25 | TATAPOWER | L | 51 | PDH 46 | trail 13:05 | +104.3 | +916 | 1.00 → +40 | LB / LB / SB, Strong Bull | ofi15 +13k; mkt 0.92 | LU |
| 29 | 10:25 | SHREECEM | L | 48 | R1 28 | stop 11:35 | −48.4 | −611 | 1.00 → +40 | LB / **SB** / LU, Bull | mkt 0.92 | LU (−27 %) |
| 29 | 10:45 | JSWSTEEL | L | 52 | PDH 52 | close | +45.6 | +329 | 0.90 → +37 | LB / LB / SB, Strong Bull | mkt15 **2.57** | LU |
| 29 | 10:50 | ETERNAL | L | 100 | R2 144 | close | +205.4 | +1,927 | 0.85 → +41 | LB / LB / SB, Strong Bull | mkt15 **2.55** | LU |
| 29 | 14:10 | TIINDIA | L | 105 | R1 77 | close | +168.6 | +1,559 | 0.94 → +20 | LB / LB / SB, Strong Bull | delta15 +14k; mkt 1.36 | SC |
| 29 | 14:50 | LT | L | 32 | R1 98 | close | −11.7 | −244 | 1.00 → −8 | SC / SC / LU, Bull | mkt 1.49 | SC |
| 30 | 10:15 | LUPIN | S | 44 | S1 53 | stop 10:35 | −43.9 | −566 | 0.18 → +55 | SB / SB / SC, Strong Bear | delta15 −5.7k; mkt15 0.79 | SC |
| 30 | 10:30 | ADANIPOWER | S | 70 | S1 90 | stop 10:35 | −69.9 | −826 | 0.09 → +60 | SB / SB / SC, Strong Bear | **delta15 +184k (b/s 2.2)**; mkt15 1.31 | **LB** (+1.2 %, +4.2 %) |

### Totals

| | n | mean gross bps | win | net at ₹1L |
|---|---|---|---|---|
| **week, current rules** | 19 | **+15.9** | 47 % | **+₹609** |
| without Thu 29 | 13 | −12.5 | 31 % | −₹3,267 |
| Tue 27 / Wed 28 / Thu 29 / Fri 30 | 4 / 7 / 6 / 2 | −30.6 / +10.7 / +77.3 / −56.9 | | −1,733 / −142 / +3,876 / −1,392 |
| longs / shorts | 8 / 11 | +64.9 / −19.8 | 75 % / 27 % | |
| with NIFTY's eventual day direction / against | 11 / 8 | +43.9 / −22.6 | 55 % / 38 % | |
| exits: stop / trail / close | 9 / 4 / 6 | −49.6 / +63.0 / +82.8 | | |
| entry 10:xx / 12–13:xx / 14:xx | 10 / 4 / 5 | +0.2 / +56.7 / +14.6 | | |
| alternative rules on the same 19 | `fixed_wall` +4.9 · `fixed_1to2` +30.0 | | | |

Five of the nine stops hit within six bars; the stopped trades' best excursion was 2–34 bps, none reached its wall.

### The live book as it actually ran (what the Shadow page showed)

| session | rules | trades | mean gross | booked net | net at ₹1L |
|---|---|---|---|---|---|
| Tue 27 | loop 0 (round walls, index legs, ₹25k) | 6 (incl. NIFTY, BANKNIFTY) | +1.1 | −₹683 | −₹696 |
| Wed 28 | — asleep | 0 | | | |
| Thu 29 | loop 1, ₹25k | 6 | +77.3 | +₹522 | +₹3,876 |
| Fri 30 | loop 2, ₹1L | 2 | −56.9 | −₹1,391 | −₹1,392 |
| **week** | | 14 | +36.7 | −₹1,552 | **+₹1,788** |

## 3. Against the record (dev window 2024-01 → 2025-09, base rule, 3,101 instances, net = gross − 15)

| question | dev window | this week |
|---|---|---|
| mean net a trade | −11.8 | +3.2 (19, current rules) / −12.5 without Thu |
| a day averaging ≥ +77 gross on ≥ 5 trades | 1 of 312 such days (0.3 %); day-mean median −15, p95 +33 | Thu 29: +77.3 on 6 |
| a 4-session stretch at this week's level (≥ +1 net) | 20 % of the 414 windows; median window −14 | this week |
| 9+ wins of 19 at the 40 % base rate | p = 0.33 | 9 of 19 |
| longs vs shorts | −11.6 vs −12.0 — no side | +65 vs −20 |
| trades with NIFTY's eventual day direction (needs the close) | −4.3 vs −26.9 against (n 2,075 / 1,026) | +44 vs −23 |
| real-time proxy: skip shorts with NIFTY ≤ 0.2 of its range so far, longs ≥ 0.8 (T4) | kept −11.5 vs dropped −12.2: **+0.3 bps, fails** | would have skipped 7 losers and 5 winners |
| 10:xx shorts / 13:xx shorts | −10.5 / −19.8 | −51 (6) / +86 (2) |
| stops within 6 bars | 30 % of stops | 5 of 9 |
| months averaging > +15 gross | 0 of 21; 5 % of months positive | |

Read: the week is one trend day plus the dev-window result. The pattern that jumps off the trade table — every
loser on Tue, Wed morning and Fri was sold at NIFTY's low minutes before the index turned (27 Oct 14:00–14:10 at
0.00–0.04 before the 14:30 bounce; 28 Oct 10:15–10:50 at 0.05–0.33 before the +80 bps climb; 30 Oct 10:15–10:30 at
0.09–0.18 before the +170-point reversal), and every Wed afternoon winner was taken with NIFTY at 0.76–0.93 — is the
same shape the dev window shows with the close in hand (+22 bps "with the day"), and no real-time proxy captures
it: NIFTY's position so far is worth +0.3 bps on 3,100 instances. It stays a description, not a rule.

## 4. What the integrated readings said at the entries

- **Thu 29 longs:** five of six had the future and the calls in long build-up on a "Strong Bull" board, with
  market-wide buying ≥ 1.36 in the previous hour (2.5 at 10:45–10:50). The one stop (SHREECEM) had the calls in
  short build-up and a wall 28 bps away. The one close-loser (LT, 14:50) had a short-covering future and 35
  minutes left. Consistent with T3 in the 30 Oct note; six trades decide nothing.
- **Shorts after an official long build-up day** (the September daily gate's case): ONGC −39, DABUR −48,
  ADANIPOWER −70, IDEA +75, AXISBANK +10 — 2 of 5 won, mean −14. The gate is still the one OI rule with evidence
  (daily study, 2024–25); the week neither confirms nor refutes it.
- **Into the tape:** ADANIPOWER was sold into +184k of 15-minute buying delta (buy/sell 2.2) and stopped in one
  bar; LUPIN's tape was neutral and it stopped in four. T1 says the tape's aggression reverts over 5–30 minutes by
  ~2 bps — a timing aid, not a veto.
- **Expiry day (Tue 27):** the three afternoon shorts carried `futures_state` LU with −23 to −36 % OI — the
  October contract expiring, not positioning. The board must not be read on expiry afternoons until it follows the
  next contract.
- **The market-level board at 10:15 matched the day's breadth on all three recorded days.** Whether it predicts
  the close is T2's question (nothing cross-sectional); as a regime label for the strategy's side it is the thing
  to test once ten sessions exist — this week's longs were all on the LB-heavy day and all but two shorts on the
  SC-heavy expiry day and the SB-heavy divergence day.

## 5. Verdict for the week

1. The rule is unchanged and still has no edge on the record. The week's +₹609 at ₹1L under the current rules is
   Thursday; the other three sessions sum to −₹3,267, the dev window's number.
2. Nothing seen this week passes a pre-registered bar on the dev window: not the NIFTY-position filter (T4), not
   the morning board (T2), not a stop or wall change (loop 2). The leans stay leans: the noon long build-up
   (+15 bps to the close, t 2.1), the daily gate against yesterday's build-up, the 10:15 board as a regime label.
3. The readings now ride on every signal (`context`, `bhav`), so the per-trade table writes itself; keep it to
   ten sessions before testing anything on it.
4. The data caveats found this week: the board's futures state is the expiring contract on expiry day; the
   official labels the day after expiry are the roll; the 27 Oct archive starts at 09:19 (hibernate).

## 6. Infrastructure: the week's largest measured loss

| | what happened | effect |
|---|---|---|
| Tue 27 | hibernate at 05:06 (battery 1 %), woke 09:19 | first bar of every stock missed; restarted 09:25 |
| Wed 28 | asleep on battery all session, dark wakes of seconds | **session lost**; replayed from exchange bars |
| Thu 29 | **not clean** (found 1 Nov evening from `pmset -g log` and the bars archive): lid closed at 10:29 on battery → "Clamshell Sleep" until 10:47 (192 live 1m bars at 10:29, 3 at 10:47, 216 at 10:48) | **18 minutes lost mid-session**; the 10:30–10:45 5m buckets came from the REST sweep, which under the old code shifted the sequence's stage windows (REVIEW S3); SHREECEM's 11:35 stop and TATAPOWER's trail were after the gap |
| Fri 30 | 11:29 restart on a stale approval | feed down 4 minutes, REST healed the bars, nothing lost by the recompute |
| Sat 31 → Sun 1 | clamshell sleep on battery from 02:47 Sat (`caffeinate -dis` does not stop lid-closed sleep); lid opened 13:28; charger in at 16:01 at 5 %; the charger flapped AC/battery every few minutes Saturday evening; **1 % at 01:50 Sunday**; 36 % and discharging at 06:40 Sunday | no session on a weekend; the engine process survived (re-logged into 5paisa at 00:01 Sunday) but is one flat battery from dying before Monday |

One of four sessions lost, one opening missed, one 18-minute mid-session sleep first read as a clean day, and one
restart gap: above 30 % data loss on the only live evidence the project collects. Every cause was the laptop
being used as a laptop (lid closed, battery, a flapping charger), not the software. Monday needs the charger in and the lid open from 08:45, or the VPS.

Scripts: `scripts/2026-11-01-week/` (week_table.py, readings.py, market_flow.py) read only the archives, the
strategy files, the research cache and the dev-window instances; nothing touched the broker.
