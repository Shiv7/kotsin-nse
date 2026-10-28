# The daily order block by side and by regime: which side to run when (2026-10-29, 03:50–04:10 IST)

Owner: "check short and long wise success then check why this work for short and which regime and mark this strategy
for that"; and: "the market is falling so our strategy works for both, it's just the market is on the short side, that's
why good short trades in October". Trades: the conservative five-year set (through fill, 15 + 5 + roll, members only,
`daily_ob_conservative.parquet`) and October 2026 (`ob_2026-10_through.csv`). Regimes read at the entry day from the
day before (no month-end hindsight): NIFTY vs its 50- and 200-DMA, its 20-day return and 20-day realised vol
(terciles cut on 2021–23), breadth (share of names above their 20-DMA), the stock's own 50-DMA and 20-day return
relative to the trade. Script `scripts/2026-10-29-all-timeframes/ob_regime.py`.

## By side

| side | 2021–23 | 2024–25 | Oct 2026 |
|---|---|---|---|
| long | 1,129 trades, +64.5 net, t 4.1 | 741, +45.9, t 2.1 | 48, −1.6 |
| short | 1,140, −4.2, t −1.4 | 776, −10.8 | 84, +101.9 |

The long side is the strategy. Over five years the shorts earn nothing after costs; October's shorts were the
best short month in the set.

## Why the shorts worked in October — the owner's reading, tested

By the month's own NIFTY return (hindsight): the shorts' monthly net correlates −0.50 with it. In the 8 months NIFTY
fell more than 3 % the shorts made +112 a trade (Feb 2025 +364, Oct 2024 +150, Dec 2022 +133, Feb 2022 +163, Nov 2021
+29; Jun 2022 −107); in the 16 months it rose more than 3 % they made −60. The longs: +72 in the rising months, −27
in the hard-falling ones, but positive in most regimes (correlation +0.10). **So yes: both sides work, and the market
picks the side.** October's NIFTY was +1.2 % on the month, yet the stocks fell with weak breadth and the shorts paid.

Known at the entry (NIFTY's 20-day return before the trade), the picture is weaker because the market's fall during
the hold is only weakly predicted by the fall before it:

| NIFTY 20-day return at entry | short n | short net | long n | long net |
|---|---|---|---|---|
| under −5 % | 144 | +9.3 | 59 | +142.9 |
| −5 to −2 % | 476 | 0.0 | 272 | +51.4 |
| −2 to 0 % | 436 | +28.6 | 403 | +62.6 |
| 0 to +2 % | 340 | +1.5 | 382 | +57.2 |
| +2 to +5 % | 380 | −13.2 | 565 | +40.3 |
| over +5 % | 224 | −62.2 | 237 | +61.4 |

Shorts lose clearly once the market has been rising; in flat-to-falling regimes they are break-even to +29. The
longs are positive in every bin.

## The marking (chosen on 2021–23 by the rule "side ON in a regime where net > 0, t ≥ 2.5, n ≥ 200", then applied unchanged)

Only long cells pass the rule in 2021–23 (above the 200-DMA +69, 20-day up +71, low/mid vol +73, strong breadth
+83, with the stock's trend +67); no short cell does. Policies known at the entry, per period (net bps, t by day):

| policy | 2021–23 | 2024–25 | Oct 2026 | every year 2021–25 |
|---|---|---|---|---|
| both sides, every regime | +30.0 (2.2) | +16.9 (1.3) | +64.2 | +34 / +25 / +33 / +29 / +5 |
| long only | +64.5 (4.1) | +45.9 (2.1) | −1.6 | +70 / +25 / +103 / +58 / +31 |
| **long always, short only when NIFTY's 20-day return ≤ 0** | +47.0 (4.4) | +28.2 (1.2) | +64.2 | +48 / +31 / +65 / +36 / +20 |
| long always, short when 20-day ≤ −2 % | +47.9 (3.9) | +33.0 (1.6) | +53.9 | +48 / +20 / +83 / +54 / +12 |
| long above the 200-DMA, short when 20-day ≤ 0 | +44.6 (3.9) | +29.5 (1.3) | +101.9 | +14 / +37 / +62 / +37 / +21 |

**Marked:** LONG on in every regime (the core; highest per trade). SHORT on only when NIFTY's 20-day return is at or
below zero — the owner's reading written as a rule the system can read at the entry; off when the market has been
rising, where the shorts lose −13 to −62. The combined policy earns less per trade than long-only (+47 / +28 against
+65 / +46) but is positive in every one of the five years and in October, and the shorts pay exactly in the months the
longs do not (2022, Oct 2024, Feb 2025, Oct 2026). That is the reason to carry them: diversification across the
regime, not a higher average.

## What this is and is not
- Two periods and one month; the marking is chosen on 2021–23 and holds on 2024–25 and October without change.
- The shorts' payoff is decided by the market falling *during* the hold; the trailing 20-day return is a weak proxy
  (break-even to +29 at the entry against +112 in the months that actually fell). A better regime read for the
  short side is the open question for the shadow book — not for the backtest, which has nothing left to test.
- The shadow book carries both sides with the switch and records the regime on every entry; the evening read splits
  by side and regime as above.

## Built (04:45 IST, the owner: "go ahead test what we believed, long short take both depend on situation")

`strategy/daily_ob.py` (the rule, pure) and `strategy/positional.py` (`PositionalRunner`, the book): at every session's
close — after the engine's post-close refresh of the daily series, and at boot for the sessions missed — the open
positions take the day's bar (gap-aware stop, stop before target, out after 20 sessions), the pending limits are
checked (filled only when the session traded 10 bps through Z; a touch that did not is recorded and the zone is not
retried; a short needs NIFTY's trailing 20-session return ≤ 0, `KN_POSITIONAL_SHORT_GATE`; a full book or a stock
already held is recorded as skipped), and every intact zone with no return yet becomes the next session's pending
limit. Sizing ₹500 of risk a trade (`KN_POSITIONAL_RISK_INR`); costs the measured model with delivery STT on the long
cash leg and futures STT on the short. State in `data-live/strategy/positional.json`, read at `/api/positional`, the
summary in `/api/health` under `positional`; every entry carries its zone, its regime and the stock's context for
the evening read. Tests `tests/test_positional.py`. First session on the book: the restart's next close.
