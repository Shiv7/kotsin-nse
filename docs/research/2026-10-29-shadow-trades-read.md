# The fifteen shadow trades, read one by one (27 Oct live, 28 Oct replayed)

Owner's ask (29 Oct 02:10 IST): "each trade see holistic view, analyze each trade what we missed … different hour,
different pickup, nifty, each trade you personally read". Dossiers from `scripts/day-replay/`-style data: the stock's 5m
bars (27 Oct from the feed archive, 28 Oct the exchange's), the sequence points from the snapshot, NIFTY's 5m, the daily
and 30m caches. Script: `scripts/2026-10-29-trade-read/dossier.py`.

## The market on the two days
- **27 Oct:** NIFTY gapped −0.41 %, ran +0.6 % in the first hour to 22,804 (above R2), faded all afternoon, closed flat.
  Stocks: a morning impulse, then drift.
- **28 Oct:** NIFTY gapped −0.27 %, sold to 22,528 by 10:15 (S2 22,546 held), V-recovered to close −0.22 %. The bounce
  day after a selloff. Three of the day's shorts were taken within 35 minutes of that low.

## 27 Oct (live, feed bars)
1. **CROMPTON long 10:10 @ 197.36, stop 112 bps, wall PDH 0.8R → −56 (close).** Downtrend stock (−12 % in 20 days,
   1H SuperTrend against). The "retest" was one bar, five minutes after the through bar; the entry was the top of the
   first hour's 1.4 % impulse. 10:30 high 198.90 came within 0.15 % of PDH (+78 MFE, 0.7R), then the stock faded all
   day; the stop never hit, the close exit lost 56. Missed: a counter-trend long bought on the impulse bar with the wall
   nearer than the stop; no better long existed later.
2. **NIFTY long 10:35 @ 22,760, stop 43 bps, wall ROUND 22,800 at 0.4R → +11 (trail).** Index, recorded only now. Open
   under BC, through at 10:00, retest 10:20 to 22,684 inside the CPR, entry 60 points higher. The move from the retest
   to the 12:40 high was 130 points; the confirmation ate half of it. Trail to R3 − tol gave the rest back.
3. **POLICYBZR short 13:30 @ 1,015.9, stop 43 bps, wall S1 1.8R → +66 (trail).** The clean one: downtrend (−15 % in 20
   days, 1H with), opening spike to 1,041.9 rejected under R1/PDH, three hours of lower highs into a thin CPR, through BC
   at 13:20, entry at 13:30. S1 hit at 14:15, trailed out at S1 − tol. We caught the last 1 % of a 3.7 % range; the
   through bar at 13:20 would have given +90.
4. **BANKNIFTY short 14:10 @ 53,997, stop 22 bps, wall ROUND 53,900 → −22 (stop).** Index. Shorted at the end of a 700
   point slide, 30 points above S1 (53,965). S1 sat inside the tolerance so the system skipped it as a wall and named
   the round number below it instead; S1 held to the point (14:20 low 53,951) and the bounce stopped us. Missed: a
   level inside the tolerance under a short is the wall that stops you, not one to ignore.
5. **AXISBANK short 14:00 @ 1,217.5, stop 30 bps, wall ROUND 1,215 at 0.7R → +2 (trail).** A 0.05 %-wide CPR, R1/PDH
   rejected in the first hour, a 1 % grind down, entry 30 minutes after the retest. 14:15 low 1,211.9 missed S1 by 1.2
   points; ROUND 1,215 was "hit", the stop moved to 1,217.27 (the entry), bounce, out at +2. The stock closed at
   1,205.7: a 96 bps move given away by a trail set at a round number. (The point-5 "bullshit" of the day-1 review.)
6. **PIIND long 12:20 @ 2,045.3, stop 62 bps, wall R1 2,052 at 0.5R → +5.5 (trail).** Oversold bounce day (−16 % in
   20 days): PDL tested at the open, BOS 10:30, through TC 11:55, retest 12:00 held, entry 12:20. R1 at 0.5R hit five
   minutes later, the stop moved to R1 − tol = one point above the entry, out at 12:30. The stock closed +2.3 % above
   the entry (R2 was 3R away). The clearest cost of the first-wall-then-trail rule in two days: a 232 bps trend day
   banked at +5.5.

## 28 Oct (replay on the exchange's bars; NIFTY's low at 10:15)
7. **HINDUNILVR short 10:45 @ 1,773.5, stop 88 bps, wall ROUND 1,770 at 0.2R → +2 (trail).** An event day: +3 %
   spike to 1,859 in the first hour, then a 5 % collapse to 1,765.5. The entry was 0.3 % above that low, the stock
   the single most extended name in the universe at that minute (`xs_pct` 0.98, `range_pos` 0.09), the "wall" 20 bps
   away. Hit at 10:50, stop to the wall, out at 10:55; the stock closed 2.4 % higher. Missed: the whole move had
   happened; every tag said so.
8. **DABUR short 10:50 @ 357.2, stop 48 bps, wall ROUND 356 → −48 (stop).** Previous close: the 15:28 auction printed
   357.8 against the last trade 361.35 (−1 %), so the "last" CPR sat at 357.9–359.3. Both sides of the CPR traded in
   the first hour, through BC 10:25, a one-bar retest, entry 10:50 at the minute NIFTY was putting in its low. Never
   went down; stopped 11:25; the stock closed +0.8 % above the entry.
9. **NBCC short 10:40 @ 69.10, stop 63 bps, wall S1 0.8R → −63 (stop).** PDH 70.5 was the auction print (last trade
   69.87). Entry at 10:40, the day's low came five minutes later (69.03), then a two-hour grind up to the stop. NIFTY
   at its low of the day, the stock at its low of the day (`range_pos` 0.05).
10. **BHARATFORG short 10:15 @ 1,748.6, stop 58 bps, wall ROUND 1,740 → −58 (stop).** Previous day +1.9 %, 1H
    SuperTrend up. Wide CPR, both sides traded by 10:00, through BC at 10:00, one-bar retest, entry at 10:15, nine
    points above the first hour's low, at NIFTY's capitulation minute. Two hours of chop, stopped at 12:30, then the
    stock rallied to 1,770.
11. **RECLTD long 12:45 @ 291.8, stop 62 bps, wall R1 1.1R → −61 (stop).** The day after a −3.9 % results drop. A
    slow four-hour climb: through TC 12:35, "retest" 12:40, entry 12:45 — three consecutive green bars, the stock
    already +2.3 % from the open and a full ADR travelled (`move_adr` 0.96, `range_pos` 0.93, `xs_pct` 0.9). 13:00 high
    missed R1 by 0.5, faded to the stop at 14:20. Missed: buying the fourth green candle of the leg.
12. **INDUSTOWER short 13:00 @ 380.8, stop 97 bps, wall S2 0.6R → +96 (trail).** Gap down below the CPR, a first-hour
    spike over R1/PDH rejected, BOS at 10:05, then **three hours of base** between 379 and 383 under BC, entry on the
    base's breakdown at 13:00. Steady decline to 374; trailed out at 14:30 at S3 + tol; the stock closed at the low
    373 (−2 %). The best trade of two days; the trail still gave back 100 of a possible 205.
13. **TATACONSUM long 13:05 @ 993.5, stop 36 bps, wall PDH 1,000 at 1.8R → +47 (trail).** The day after +6.8 %
    results; a 3.5 % first-hour collapse to 962, a V-recovery, through TC at 12:45, entry 13:05, 1H with. PDH 1,000
    was the auction print (and the round number); hit at 15:10, trailed out at 998.2; closed 1,000.2.
14. **IDEA short 13:15 @ 11.95, stop 116 bps, wall S2 1.3R → +75 (close).** A one-paise CPR; through BC at 10:00, then
    **three hours of range 11.95–12.04**, entry on its breakdown at 13:15. Down to 11.82, closed at 11.86. S1 and PDL
    sat inside the tolerance and were skipped as walls; they broke anyway.
15. **PREMIERENE long 13:05 @ 849.25, stop 114 bps, wall R1 0.9R → −48 (close).** The 27 Oct auction printed 862.00
    against a last trade of 835–842 (+3.2 %): that one print became the official close and PDH, and R1 858.35 came out
    of it. Entry 25 minutes and +0.5 % after the retest at the top of a 2.3 % day move (`range_pos` 0.97). 13:10 high
    854.8 (R1 not reached), faded to the close.

## What the fifteen say together
- **Where the entry lands in the day's move decides it.** Nine entries came after the stock had travelled 0.7–1.0 ADR
  from the open (`range_pos` ≥ 0.9 or ≤ 0.1): the ones that paid were the days that kept trending (POLICYBZR, PIIND's
  underlying move, INDUSTOWER), the rest reversed. The 3,101-instance tests say nothing known at the bar tells them apart.
- **Three 28 Oct shorts were taken within 35 minutes of NIFTY's low of the day** (BHARATFORG 10:15, NBCC 10:40, DABUR
  10:50), all stopped, all closed above the entry. A hypothesis from three trades, not a rule: the day-group study
  found no NIFTY first-hour context that picks the algo.
- **The first wall is the problem, as the owner said on day 1.** Eight of fifteen first walls were ≤ 0.8R away, five
  of them round numbers; levels inside the tolerance (S1 under BANKNIFTY and AXISBANK, PDL under INDUSTOWER, S1/PDL
  under IDEA) were skipped though they were the nearest real levels. Trail-to-wall put the stop at the entry within
  minutes on PIIND (gave away +232), AXISBANK (+96), HINDUNILVR. On the history: first wall 1–2R away is the best band
  (+9.7 gross, −5.3 net, n 704); ROUND as the first wall 755 times at +3.2 gross; PDH/PDL +5.1; S1/R1 +0.4.
- **Hours of base, then the break — the trades that paid** (INDUSTOWER, IDEA, POLICYBZR) had the retest 2.5–3 hours
  after the through bar; the one-bar "retests" five minutes after it (CROMPTON, BHARATFORG, NBCC, DABUR, RECLTD) all
  lost. **On the history this does not hold:** retest 6–11 bars after the through +15.8 gross on 80 instances (noise),
  12–23 bars −2.3, 24–35 bars −13.9, ≥ 36 bars +2.1; 2,629 of 3,101 retests are the one-bar kind at +3.4. The eye sees a
  base; the arithmetic "bars between through and retest" is not it.
- **1H SuperTrend with the trade: 4 trades, +23 avg; against: 11 trades, −13 avg.** The 33-filter test on 3,101
  instances found the 1H filter worth nothing; fifteen trades do not overturn that.
- **The closing auction wrote four of the day-2 levels.** 27 Oct auction prints: PREMIERENE 862 (+3.2 % over the
  last trade), NBCC 70.5 (+0.9 %), TATACONSUM 1,000 (+0.5 %), HINDUNILVR 1,818 (+0.4 %), DABUR 357.8 (−1 %). The CPR
  used the last trade (`pdc=last`), but PDH/PDL and the floor pivots come from the daily candle's high/low, which
  include the print. Open item: compute PDH/PDL from continuous trades, or accept that Kite shows the same candle.
- **Nothing here changes the research verdict.** The two days read like the 3,101 instances: a trend-day subset pays,
  the rest pays the costs, and the trail chooses which trades hurt.
