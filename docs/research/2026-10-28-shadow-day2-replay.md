# Shadow day 2 (2026-10-28): the session the engine slept through, replayed on the exchange's bars

The laptop sat on battery with the lid closed and woke for seconds at a time; the engine closed two bars a
symbol all day and the live shadow file holds 0 signals (`docs/HANDOFF.md` §6 item 13, `LEARNINGS.md`). After
the 21:55 restart the boot backfill seeded today's 1m/5m from REST, so the day was replayed through the engine's
own `ShadowRunner` (`scripts/day-replay/simulate_day.py`: same store, runner, gateway and settings as live,
`KN_STRATEGY_PDC=last`, bars fed bucket by bucket across 219 symbols). The same bars were copied into the
research cache (`scripts/day-replay/fill_cache.py`: 1m +79,020 rows, 5m +16,227, 30m +8,640, 1d +652 for 216
symbols). Three symbols were not in the engine's universe (BAJAJHLDNG, NIFTYFPI, NIFTYNXT50).

## Funnel

216 symbols with levels → 137 open tests → 11 BOS → 2 through → 7 held retests → 9 confirmed (1 retest failed).

## The nine trades (bar = the entry bar's open-time label; live rule `wall_then_trail`)

| bar | symbol | side | entry | stop | risk bps | first wall | exit | how | bps | fixed_wall | fixed_1to2 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 10:15 | BHARATFORG | S | 1748.6 | 1758.79 | 58 | ROUND 1740 | 1758.79 | stop | −58.3 | −58.3 | −58.3 |
| 10:40 | NBCC | S | 69.1 | 69.54 | 63 | S1 68.77 | 69.54 | stop | −63.0 | −63.0 | −63.0 |
| 10:45 | HINDUNILVR | S | 1773.5 | 1789.06 | 88 | ROUND 1770 | 1773.16 | trail | +1.9 | +19.7 | −87.8 |
| 10:50 | DABUR | S | 357.2 | 358.93 | 48 | ROUND 356 | 358.93 | stop | −48.3 | −48.3 | −48.3 |
| 12:45 | RECLTD | L | 291.8 | 290.01 | 62 | R1 293.78 | 290.01 | stop | −61.5 | −61.5 | −61.5 |
| 13:00 | INDUSTOWER | S | 380.8 | 384.51 | 97 | S2 378.42 | 377.14 | trail | +96.1 | +62.5 | +194.8 |
| 13:05 | TATACONSUM | L | 993.5 | 989.97 | 36 | PDH 1000.0 | 998.17 | trail | +47.0 | +65.4 | +71.1 |
| 13:05 | PREMIERENE | L | 849.25 | 839.57 | 114 | R1 858.35 | 845.2 | close | −47.7 | −47.7 | −47.7 |
| 13:15 | IDEA | S | 11.95 | 12.09 | 116 | S2 11.77 | 11.86 | close | +75.3 | +75.3 | +75.3 |

Averages: `wall_then_trail` −6.5 bps (4 wins / 5 losses), `fixed_wall` −6.2, `fixed_1to2` −2.8. Book at ₹25,000
a trade: gross −₹141, charges ₹928, net −₹1,069; sum R −1.44. Five of nine hit the stop within the first hour of
the trade; the two that paid (INDUSTOWER, IDEA) had the first wall 0.4R and 0.6R away and ran far past it.

This is a replay, not a live record: the live engine would have decided on the same exchange bars (every closed
5m bar is reconciled before the sequence runs since 27 Oct), so the trades are what it would have booked, give or
take the EOD timing. The live file for the day stays at 0 signals; the replay is `data-live/strategy/2026-10-28-replay.json`.
