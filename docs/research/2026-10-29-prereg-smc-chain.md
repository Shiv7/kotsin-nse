# Pre-registration: the intraday SMC chain on 5m with the 1H bias, the discount filter and MTF pivot confluence

Written 2026-10-29 01:22 IST, before the run. Owner (02:40): "any learning pattern we found add MTF pivot proper SMC
concept we should get something na bro". HANDOFF §3.4 rows 1–5 and the power study scored the parts (order-block
playbook on 1m −0.41 R; 5m sweep reversals −0.21 R; 1h/4h sweep → 5m BOS −0.03 R; Fib + pivot + 1h OB −0.08 R; an OB's
first retest races 49.7 % against 50.8 % for an ordinary candle). The intraday chain as SMC teaches it — liquidity sweep
→ displacement break of structure → the order block → entry on its first return, with the higher-timeframe bias, the
discount/premium half and pivot confluence as gates — was launched once (§3.6 "final numbers not recorded"). This records it.

## Fixed before the run

Instances: every order block the engine's own `structure.analyze` builds on a stock-day's 5m bars (2024-01 → 2025-09,
214 stocks): a close beyond a confirmed swing (or the opening range) with displacement (a fair value gap in the move or
clearance of an average candle), the last opposite candle before it as the zone. **Entry:** a limit at the zone's body
edge Z, filled by the first later bar that trades to it, only while the zone is intact (no close through its far side).
**Stop:** the zone's far side less a tenth of a candle (the engine's S1). **Targets:** the fixed walls known at the open —
daily R1/R2/R3 (S1/S2/S3), PDH/PDL, the weekly pivot and R1/R2 (S1/S2), the next round number — `wall_then_trail`, and
`fixed_1to2` beside it. Net = gross − 15 bps (the limit fills at Z; charges only).

Gates, each reported on its own and stacked: (a) all zones; (b) **sweep before** — a sweep of a swing low / equal lows /
PDL (highs for a short) in the six bars before the zone; (c) **1H bias** — the last 1H break of structure (close beyond
the last confirmed 1H swing high / low, 1H bars on the session grid from the 5m cache, the last 60 hours) points the
trade's way at the entry bar; (d) **discount / premium** — Z in the lower half (long) / upper half (short) of the 1H
dealing range (last 1H swing low to last 1H swing high); (e) **MTF pivot confluence** — a daily or weekly pivot level
within a tolerance of the zone; (f) the move's impulse is a gap (FVG); (g) entry at or before 14:30. Sides separately.

**Decision rule:** a gate set ships to SHADOW only if net > 0 with t by day ≥ 2.5 on ≥ 300 trades, both halves (2024,
2025) positive. Otherwise the SMC chain joins rows 1–5 as scored, and the line closes. Expected: fail.

## Run log
- 01:33 IST, one run (`scripts/2026-10-29-smc-chain/smc.py`, 2 min): 183,261 order-block first-return fills on 433 days
  (94,422 short / 88,839 long), median risk 17.9 bps, median room to wall 1 3.6R, median 4 bars from the break to the
  return.

  | gate | n | gross (trail) | net | t by day | gross (1:2) | net |
  |---|---|---|---|---|---|---|
  | (a) all zones | 183,261 | +4.9 | −10.1 | −44.7 | +6.7 | −8.3 |
  | (b) sweep before | 43,148 | +4.3 | −10.7 | −32.5 | +6.2 | −8.8 |
  | (c) 1H bias agrees | 98,318 | +4.7 | −10.3 | −31.1 | +6.6 | −8.4 |
  | (d) discount / premium | 81,064 | +4.6 | −10.4 | −30.6 | +6.7 | −8.3 |
  | (e) MTF pivot confluence | 144,585 | +4.7 | −10.3 | −40.6 | +6.5 | −8.5 |
  | (f) FVG impulse | 169,967 | +5.0 | −10.0 | −44.1 | +6.5 | −8.5 |
  | (b)+(c)+(d) | 6,730 | +2.4 | −12.6 | −15.2 | +5.2 | −9.8 |
  | (b)+(c)+(d)+(e)+(f)+(g) | 3,987 | +2.5 | −12.5 | −9.5 | +4.9 | −10.1 |

  Both halves within 1 bps of each other on every row; long / short the same. **The gates do not add: every one leaves
  the gross where it was, and stacking them lowers it** (the full chain +2.5 against +4.9 for any zone). The 1:2 exit
  wins 50 % — exactly a coin at two-to-one — for +6.7 gross on an 18 bps risk, and 15 bps of costs is 0.8 R.
- **Verdict: fail.** The intraday SMC chain scores like rows 1–5 and like the CPR sequence: a few bps gross a trade,
  negative net, no gate separating anything. The line closes.
