# Pre-registration: the daily order block's exits on the fill session (2026-10-29, written before the run)

## What the live book showed

The positional book's first session (29 Oct 2026) closed TITAN short at "target" the same day it filled:
limit 4440 filled when the stock traded through it at 14:05, target 4371. But TITAN's low of the day (4341)
was printed at 09:15, five hours before the fill. The engine's `step` and the five-year harness's `walk`
both score the fill session on the whole day's range: stop if the range touched it, else target if the range
touched it. On the fill session that is a look-ahead whenever the target touch came before the fill.
The published +24.8 net (conservative, five years, members only) may include such phantom wins.

## The rules compared

* **range** (as published): fill-day stop if touched, else fill-day target if touched.
* **close** (provable): fill-day stop if touched (pessimistic: the stop lies beyond Z on the fill side, so
  reaching it means price passed Z first, except a session that opened beyond the stop, which is charged as a
  stop anyway); fill-day target only when the session **closes** beyond the target: a continuous path from the
  fill at Z to a close beyond the target must cross the target after the fill. Otherwise the position carries
  to the next session. Later sessions unchanged.
* **never**: no fill-day target at all (the lower bound).

## Test

The five-year conservative case exactly as published (2021-01-01 → 2025-09-30, members only, limit filled
10 bps through Z, stop slip 5 bps, 15 bps cost + 10 bps when the hold crosses a month end, target 2R, hold 20
sessions), re-walked under each rule. Reported: n, gross, net, win %, t by day, net by year, by side; and the
share of trades under **range** that exited on the fill session, by how, with their net.

## Decision, fixed before the run

* The engine moves to **close** regardless of the result (it is the provable rule).
* The positional book stays in shadow only if under **close**: net > 0, t ≥ 2, every year ≥ 0 — the same bar
  the strategy passed before. If it fails, `positional_enabled` goes to false and the strategy is marked
  failed in HANDOFF with this document as the reason.
* Nothing is tuned on the result; the sensitivity table is reported, not chosen from.

## Run log (run ≈ 22:00 IST, committed 22:11 in 882e9c5; `scripts/2026-10-29-all-timeframes/ob5y_fillday.py`, output `ob5y_fillday.log`)

Members only, n = 3,786, 2021-01 → 2025-09, conservative stack:

| fill-session rule | gross | net | win | t | by year (net) | long | short |
|---|---|---|---|---|---|---|---|
| range (published) | +41.5 | **+24.8** | 45 % | 2.42 | 34.4 / 24.6 / 32.8 / 28.7 / 5.4 | +57.2 (t 4.4) | −6.9 |
| close (provable) | +1.4 | **−15.5** | 36 % | −1.57 | 3.0 / −30.5 / −6.1 / −2.7 / −34.6 | +17.4 (t 1.5) | −47.6 (t −3.8) |
| never | +1.0 | −15.9 | 36 % | −1.60 | 3.0 / −31.2 / −7.0 / −2.8 / −34.8 | +16.9 | −47.9 |

Under **range**, 1,054 trades (28 %) exited on the fill session: 415 stops at −197 net (−21.6 bps a trade of the
total) and 639 targets at +301 net (**+50.7 bps a trade of the total** — twice the published edge). The sensitivity
table under **close** is negative in every cell (target 1.5–3R × hold 10–40: −12.7 to −17.0, t −0.9 to −1.7).

**Decision applied:** the engine's `daily_ob.step` now credits a fill-session target only on a close beyond it
(tests: TITAN's shape carries, a close beyond the target closes); `KN_POSITIONAL_ENABLED=false`; the strategy is
marked failed in HANDOFF (§2 row, §3.4 row 20, §6 item 14) and the lesson in LEARNINGS. The one live session's
record stays in `data-live/strategy/positional.json` (TITAN's "target" there is the phantom; its true state at the
close was open, 4434 against 4440).

## The fill session ordered exactly on 5m bars (run ≈ 22:05 IST, same commit; `ob5y_exact.py`, `ob5y_exact.log`; informational)

For the 2024-01 → 2025-09 member trades whose fill day is in the 5m cache (n = 1,139; 333 had no 5m day, 45 the daily
bar said "through" but no 5m bar did): the fill is the first 5m bar through Z, then stop-before-target bar by bar.

| rule | net | win | t | 2024 / 2025 | long | short |
|---|---|---|---|---|---|---|
| range | +14.0 | 43 % | 1.12 | 24.4 / 5.3 | +47.4 | −16.0 |
| close | −21.8 | 35 % | −1.04 | −3.6 / −37.0 | +7.6 | −48.3 |
| **exact (5m)** | **−23.7** | 35 % | −1.24 | −5.6 / −38.8 | +13.0 | −56.6 |

Of the 178 fill-day "targets" under **range**, the 5m bars say 98 were targets, 72 were stops and 8 gap-stops; the
304 fill-day exits net +95.5 under range, −25.7 in truth. The provable close rule (−21.8) tracks the truth (−23.7)
closely; the published rule does not. The verdict stands on real ordering, not only on the pessimistic bound.
