# CT-B1 gap fade — the backtest of `FUDKII_CT_Buckets_14-19Oct2026.xlsx`

Research only. Nothing here is imported by the engine; the scripts import engine A's own code
(`strategy/fudkii.py`, `strategy/b1.py`, `bars/pivots.py`) so the rules are the engine's, not a copy.
Run everything from `backend/` with the project venv:

    cd backend && uv run --with openpyxl python ../research/ctb-b1/<script>.py

Branch cut from `record-5read-engine-a` (c1d8048) on 21 Oct 2026; Shiv7 + Claude.

## Verdict, in one paragraph

The sheet's 13 trades are worth **₹14,787 (3.3 %)** on real option candles under the sheet's own rules, not the
₹2,08,498 (31.5 %) the sheet computes — the sheet values every trade at its day's peak −3 % as if the trail never
fired on the way up, and has the wrong lot size for ENRIN (1,076 vs 175: 60 % of its profit). Every FUDKII 09:15
trigger of October judged by engine A's B1 rule gives 33 fades worth **₹35,221 (1.9 %)** with the 3 %/5 % trail, up to
₹1,13,610 (6.1 %) with a 15 %/20 % trail. On the **stock**, over the exploration window Apr 2024 – Sep 2025,
**the B1 fade loses: −0.175 ATR30 (−0.15 % of price) a trade, 48.2 % win, n = 1,042, t = −2.6**, negative in every gap
bucket, in two of four half-years, unchanged by the pin rule, the conviction points, the grade, the opening volume or
an 11:15 time stop. October's +0.28 ATR on 33 trades is inside noise (standard error ≈ 0.37). The apparent opposite
edge (follow the trigger, +0.15 %, t = 2.5) is market beta: +0.04 %, t = 0.7 after subtracting the Nifty's move. The
one large number found (big gaps ≥ 1.5 dATR whose first bar reverses: +0.59 %, 63 %, t = 4.8) is 198 of 360 cases on
7 Apr 2025 and 3 Jun 2024; without them +0.16 %, t = 0.8. **No edge demonstrated; the direction, not the exit, is the
problem.** Per-trade noise is 1.75 % of price, so confirming a +0.15 % edge needs ~540 trades, +0.30 % ~140 — a monthly
sheet of 13–33 rows cannot show anything under ~0.7 %/trade.

## What is here

| File | What |
|---|---|
| `FUDKII_CT_Buckets_14-19Oct2026.xlsx` | the operator's sheet (the input), one tab `B1_GAP-FADE`, 14 rows, 229 formulas |
| `sheet.py` | the 13 traded rows as the sheet states them; the option's raw classic pivots (P/TC/BC/R1-4/S1-4 of its previous day, week, month) — every target in the sheet matched one of these to the tick (ENRIN 71.38 = 1d.R3, 95.08 = 1wk.R1; VOLTAS 18 / 18.1 = 1wk.BC / Pivot) |
| `ladder.py` | the sheet's exit ladder on 1m candles: −25 % stop before arming, arm at +5 %, one lot per pivot target (3), then the trail from the peak (5m close −3 %, or −5 % at once), everything out by 15:19. `lenient=True` reads the −5 % on the minute's close and exempts the arming minute (the order of ticks inside a minute is unknowable) |
| `run.py` | replays the 13 sheet rows → `CTB_B1_replay.xlsx` (strict) / `CTB_B1_replay_lenient.xlsx` (`LENIENT=1`) |
| `triggers.py` | every FUDKII 09:15 trigger of October regenerated with engine A's `Fudkii` (graded F included), the official gap from the daily row's open (verified against the sheet: JSWENERGY 0.053, GODREJCP 0.613 vs 0.629), engine A's `b1_read`, the fade strike as `b1_plan` anchors it (close ∓ 1 ATR30, nearest listed OTM) → `triggers_oct.csv` (194 triggers, 33 fades; 13 of the sheet's 14 rows reproduce — SWIGGY 16 Oct's 09:15 close does not break the band on the broker's bars) |
| `run_oct.py` | the 33 October fades through the ladder, strict / lenient / trail grid → `CTB_B1_october.xlsx` |
| `explore.py` | the exploration window on the stock: 78,399 symbol-days, 5,273 triggers, 1,042 B1 fades → `explore_2024_2025.parquet` (gap, pins, outcomes at 10:15 / 11:15 / 13:15 / close, MFE / MAE, opening relative volume; a control row for every name-day) |
| `fetch_options.py`, `fetch_fades.py`, `fetch_stocks.py` | the downloads (read-only 5paisa login): the 13 sheet contracts, the 30 other fade contracts, and the stocks' 30m / 5m / 1d bars through 20 Oct into `data-live/history` |
| `data/` | the 43 option contracts' October 1m and daily candles, fetched 21 Oct 00:34–01:15 IST — **irreplaceable after the 27 Oct expiry**, which is why they are committed |

## The sheet, as read

- The targets are the option's own raw pivots in ascending order, rounded to the tick (Devina, 21 Oct 00:23).
- Stop −25 % of entry before arming; out by 15:19 at the latest (00:25). Halfway columns M/O/Q ignored (00:29).
- Trail: in a running 5-minute candle a dip past −3 % does not exit; −5 % inside the candle exits all lots at once;
  a recovery to within 3 % cancels (00:29). The trail starts at arming (answer 6).
- Defaults taken where the sheet is silent (flagged in the outputs): three targets then the trail; the −25 % stop on a
  touch; a 5m close between −3 % and −5 % exits at that close; a target below +5 % is still taken on touch; the
  exchange's lot sizes; entry at the 09:45 one-minute open; ₹20 an order; GST on brokerage + exchange + SEBI only
  (the sheet's formula also taxes stamp duty and STT — shown as a separate column).
- Formula notes: Y values the last lots at peak −3 % and never books a loss; AF taxes stamp + STT; Q changes formula at
  row 5; M5 / M11 are typed zeros; row 6 (BANKINDIA, not traded) carries formulas two columns off; VOLTAS I10 says
  Bullish for a PE.

## Results

**Sheet rows on real candles (exchange lots, ₹4,47,933 invested):** strict ₹14,787 (3.3 %), lenient ₹20,008 (4.5 %);
sheet ₹2,08,498. All 13 positive but tiny: JSWENERGY out at 8.88 in the arming minute (its minute's low was −10 %),
ENRIN out at 62.05 on a 5m close −3 % (then 97.8), VOLTAS out at 16.15 after T1 (then 17.65), PNB out at 1.32 (then 1.9).

**October, 33 fades, ₹18.55 L:**

| trail (close / hard) | wins | net | return | PF | biggest trade |
|---|---|---|---|---|---|
| 3 % / 5 % strict (the sheet) | 29 / 33 | ₹35,221 | 1.9 % | 1.73 | 33 % |
| 3 % / 5 % lenient | 29 / 33 | ₹71,340 | 3.8 % | 2.48 | 29 % |
| 5 / 8 | 25 / 33 | ₹56,320 | 3.0 % | 2.07 | 36 % |
| 8 / 12 | 19 / 33 | ₹82,747 | 4.5 % | 2.35 | 70 % |
| 10 / 15 | 19 / 33 | ₹1,02,783 | 5.5 % | 2.56 | 53 % |
| 15 / 20 | 19 / 33 | ₹1,13,610 | 6.1 % | 2.37 | 43 % |

The four losers are the −25 % stop (NYKAA −₹24,413, HINDALCO, TCS, BAJFINANCE). 29 of 33 "armed" is not evidence: an
OTM option whose minutes swing 5–10 % touches +5 % most days either way. Conviction ≥ 3: ₹7,886 on 16; < 3: ₹27,335 on 17.

**Exploration window on the stock (move from the 09:45 close, signed the fade's way, ATR30):**

| group | n | win | to close | t |
|---|---|---|---|---|
| B1 fade (gap ≥ 0.25, no pin) | 1,042 | 48.2 % | −0.175 | −2.6 |
| by half: 24H1 / 24H2 / 25H1 / 25Q3 | 194 / 317 / 383 / 148 | | +0.06 / −0.22 / −0.37 / +0.12 | 0.3 / −1.8 / −3.4 / 0.7 |
| gap ≥ 0.25 pinned | 727 | 48.7 % | −0.148 | −2.0 |
| every 09:15 trigger faded | 5,273 | 49.0 % | −0.128 | −4.4 |
| gap against the trigger, faded | 1,095 | 45.7 % | −0.231 | −3.8 |
| bullish triggers faded / bearish faded | 646 / 396 | 45 % / 53 % | −0.334 / +0.085 | −3.8 / 0.9 |
| follow the trigger on the B1 set, market-adjusted | 1,030 | 47.8 % | +0.04 % of price | 0.7 |
| all triggers followed, market-adjusted | 5,246 | 49.9 % | +0.045 % | 2.1 |

The fade's loss builds after 11:15 (−0.05 at 11:15 → −0.175 at the close). No gap size, grade, opening volume band
or pin test turns it positive. The control (every name-day, |gap| ≥ 0.25, faded): ≈ 0 to slightly negative in every
bucket below 1.5 dATR.

## What would change the verdict

A rule that, on the exploration window, market-adjusted, is positive in ≥ 3 of 4 half-years, not dominated by its
top two days, t ≥ 3 after costs, ≥ 500 trades, and ≥ 0.3 % of price a trade if traded through options (≥ 0.1 %
through futures). Then one holdout run (2025-10-01 → 2026-10-16, untouched here except that October 2026's 12
sessions were replayed for the sheet). Engine A's CT-B1 paper book records every 09:15 trigger with its gap: the free
forward test, already running.
