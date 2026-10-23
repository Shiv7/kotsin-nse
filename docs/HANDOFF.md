# Kotsin — handoff for a new session (Claude / Codex)

Last updated **2026-10-23**, on branch `fresh-base`. Read this whole file
before changing anything. It records what has been tried across every Kotsin module, what the data
said, and where the work stands. Numbers are from backtests unless marked otherwise.

---

## 0. Ground rules (the owner's standing instructions)

- **Broker = 5paisa**, always. Never suggest Zerodha/other brokers. Data vendors (Accelpix, GFDL) are
  allowed as *data* sources only; orders always go to 5paisa.
- **Never print credentials.** `backend/.env` is git-ignored (chmod 600). The old repos committed 5paisa
  keys and the TOTP seed; they are known-compromised (account holds ~₹2,000).
- **Git identity: `Shiv7 <yshiv866@gmail.com>` only.** Repos: `github.com/Shiv7/kotsin-nse`,
  `github.com/Shiv7/kotsin-crypto`.
- **Protect the laptop:** 8 GB RAM. Run backtests one at a time, `nice -n 15`, never several heavy jobs
  at once. The disk was 97 % full on 2026-10-18 (cleaned to ~40 GB free); keep generated data in
  `backend/data-live/` and don't duplicate caches.
- **Ask before:** restarting the live engine, arming LIVE, pushing to a new remote/branch you didn't
  create, deleting the owner's files, or anything that spends money.
- **Honest research protocol** (§4) — do not tune on the holdout, do not call in-sample results an edge.
- Toolchain: Python via `uv` (arm64). Node via nvm: `export PATH="$HOME/.nvm/versions/node/v22.16.0/bin:$PATH"`
  (the old Intel Homebrew in `/usr/local` was removed; its node never ran on this Mac).
- **5paisa TOTP:** two logins in the same 30-second window fail ("OTP has been used in past"); wait for
  the next window between scripts that log in.

People: **Devina Kothari** = "the operator" quoted in `docs/BOOKS.md` on the phase34/35 branches; she
runs the live engines from her own Mac with Claude.

---

## 1. Repositories and branches

`~/Downloads/kotsin/`:

| Repo | What | State |
|---|---|---|
| **kotsin-nse** | NSE/MCX engine on 5paisa, Python asyncio + FastAPI + React, one process, port **8500** | `fresh-base` = current research branch (this file) |
| kotsin-crypto | Delta Exchange India app (perps/options), ports 8400/8402/8403 | `main` 6 commits ahead of origin (not pushed), CAN2 retired |
| kotsin-box | old stack docs — `strategy-docs/_PATTERNS.md` (23 failure patterns), `SESSION-PRIMER.md` | reference |
| streamingcandle, optionProducerJava, tradeExcutionModule, trading-dashboard, fastAnalayticsKotsin, scripFinder | the old Java/Python stack (5 JVMs, Kafka, Mongo, Redis) | reference only; reusable maths, not code |
| trade-records | old paper ledger (1,330 rows, Jun–Sep 2026) | evidence that every old book lost |

kotsin-nse branches:

| Branch | Meaning |
|---|---|
| `main` | the original lean rebuild (FUDKII + FUKAA), Sept 2026 |
| `phase10-13-consolidated` + `feature/oi-candles` (PR #1) | data-pipeline fixes (Stages 0–5, OI from our own prints) |
| `phase34-stop-rules` | **engine A — what Devina's live engine runs** (her books RT-X/RT-N/RT-Y/CT-*/…) |
| `phase35-engine-b` / `phase35-*` | engine B, feed hub, stop-rule mirrors |
| **`fresh-base`** | **clean restart, no legacy strategy; all current research (§3)** |

---

## 2. `fresh-base` — what exists

Built 2026-10-18 from `995a914` with every strategy removed, then rebuilt as a research platform.

| Module | Does |
|---|---|
| `venue/fivepaisa/` | TOTP auth, REST (candles, quotes, orders, positions, scrip master), the openfeed WebSocket |
| `feed/` | **market-data seam**: generic channels (quotes/depth/OI), messages stamped with `source`; `feed/fivepaisa.py`; `feed/rate.py` measures real frames/min per source. A vendor (Accelpix/GFDL) plugs in here (`docs/ARCHITECTURE.md` "Adding a market-data source") |
| `bars/` | ticks → 1m…30m + live 1d on the session grid; REST backfill; each closed 30m bar checked against the exchange candle |
| `scan/` | the minute **board**: strength = (last − open) ÷ 50-session avg range, zero unless one-sided (2×); top 20 a side, 3 picks, breadth, sectors (NSE Nifty-500 industry), VWAP, OI build-up; `DayReplay` rebuilds any cached day minute by minute |
| `structure/` | 5m swings, BOS (close only), order blocks + life cycle, FVGs, liquidity, sweeps, Z/S1/R1/R2 |
| `profile/` | volume profile (POC/VA/HVN/LVN) and market profile (TPO, IB, single prints, day type) |
| `orderflow/` | per-minute **estimated** delta (quote rule on each snapshot's new volume), CVD, top-5 book imbalance, futures OI — live only, recorded from 2026-10-19. From 2026-10-23 also the book's own readings from every depth frame (5 levels — 5paisa's 20-level gateway refuses this account): L1 and multi-level **OFI** (all 5 levels), queue imbalance, microprice − mid, distance-weighted imbalance, spread, visible depth; the archive keeps the 5 levels a side |
| `market/fo_universe.py` | **F&O membership by date** from NSE's bhavcopies + NSE's symbol-change list: a stock is tradable as a future only on sessions its FUTSTK rows appear. `swing.py` uses it by default (`fo_members=true`) |
| `market/fo_bhavcopy.py` | NSE daily F&O bhavcopy (official OI of every contract) |
| `research/` | backtesters: `obtest.py` (order block), `playbooks.py` (5m sweep, value area), `mtf.py` (1h/4h sweep), `fibob.py` (Fib+pivot+OB), `swing.py` (dropped 2026-10-23, §5), `oi_daily.py`, `oi16.py` (16 & 64 states), studies `oi16_study.py`, `oi64_study.py`; `micro_ic.py` — each recorded order-flow reading against the next 5/15/30 min (IC by day) |
| `exec/`, `risk/costs.py`, `ledger/` | order gateway (SHADOW/PAPER/LIVE, nothing submits), measured cost model (₹40/order flat, STT, GST…) |
| UI | Overview, **Board** (live/replay), **Backtest** (every run, every trade opens on its chart), Chart, Options, Universe, System (feed rate) |

Run it:
```bash
cd ~/Downloads/kotsin/kotsin-nse/backend
uv run kotsin-nse serve                         # full engine, needs 5paisa session (market hours)
KN_ENGINE_ENABLED=false uv run kotsin-nse serve  # UI + replays + backtests offline, no broker login
# UI: http://127.0.0.1:8500  (build: cd frontend && npx vite build)
uv run pytest -q && uv run ruff check kotsin_nse tests && uv run lint-imports
```

CLI (all read the cache in `backend/data-live/`, `KN_DATA_DIR` from `.env`):
```
fetch-history --all-fo --tf 1m,5m,1d --start --end   # 5paisa history into data-live/history/<tf>/
fetch-oi --start --end [--legacy]                    # NSE bhavcopies into data-live/oi_daily/ (--legacy: the older file, to 2024-07-05, into oi_daily_legacy/) + NSE's symbol-change list
micro-ic --start --end                               # does the recorded order flow predict (needs ≥ 15 recorded days)
ob-backtest | pb-backtest | mtf-backtest | fib-backtest | swing-backtest  --start --end --set k=v --label
```
Backtest results are JSON in `backend/data-live/obtests/` (git-ignored; visible on the Backtest page).

Data held locally (git-ignored): 1m for 217 F&O stocks 2026-08-03→10-16; **5m 216 stocks 2024-04→2026-10**;
**1d 217 stocks 2021→2026-10**; NSE bhavcopy 2024-01→ (`oi_daily/`) and **2021-01→2023-12** (`oi_daily_legacy/`,
740 sessions, older format: no contract token, volume in contracts) — futures + options OI and prices.

---

## 3. Every idea / strategy tried, and the verdict

R = multiples of the planned risk. "net" is after the measured cost model.

### 3.1 Old Kotsin stack (paper ledger Jun–Sep 2026)
| Strategy | Idea | Verdict |
|---|---|---|
| FUDKII | 30m SuperTrend flip + Bollinger break, graded by pivot confluence | paper −₹3.6 L |
| FUDKII-RT | FUDKII + geometry router | −₹8.4 L; thresholds fitted on 1–2 examples |
| FUKAA | FUDKII + volume/OI confirmation | −₹2.2 L |
| QUANT / FUDKOI | scoring book / OI-threshold book | −₹1.5 L / −₹1.7 L |
| CAN1 / CAN2 | equity breakouts | positive in-sample only; best exit +0.118 % gross vs 0.299 % cost |
| BB-squeeze family, PIVOTBOSS, PIVOT_CONFLUENCE, RETEST, MERE, MICROALPHA | assorted | dead (2–47 lifetime signals, or never fired because of bugs) |

### 3.2 kotsin-nse rebuild (`main`, Sept 2026)
| Idea | Verdict |
|---|---|
| FUDKII on 1y, 24 names | **−1.40 R, t −8** (481 trades): confluence stops sat inside one bar's noise |
| FUDKII full universe | −0.53 R, 3,354 trades, negative before costs; every slice negative |
| Stop policies (ATR floor, walls only) | best −0.27 R |
| FUKAA | untestable then (no OI history) |
| Daily FUDKII (delivery) | 24 trades/yr, −0.78 R |
| Review committee (forensics + Claude post-mortems + hypothesis→out-of-sample grade) | tool works; `min_stop_atr=1.0` +0.75 R out of sample, still losing |
| RL exit policy (FQI, walk-forward), ShinkaEvolve param search | no improvement; entries are the problem |

### 3.3 Devina's books (phase34/35, paper)
| Book | Idea | Verdict |
|---|---|---|
| RT-X / RT-N / RT-Y | in-trend option variants with option ladders | all negative in replay |
| RT-Y gate B | breadth > 50 %, no pivot ahead, no gap | positive only in-sample (Sept) |
| RT-Y wide, RT-Y graded F | wider stop / unpublished triggers | no edge shown |
| CT-X, CT-Y (09:45 gap fade, gap switch), CT-M | counter-trend fades | gap fade +3.7 % gross in-sample, never confirmed |
| Stop-rule mirrors (stop E, adaptive), equity stop mode | alternative stops | no edge |
| Her manual Excel re-trades (Weak+RT-Y, Strong hold 30/60, Scenario 2b, 3-of-5 votes) | discretionary | looked better because fired stops were ignored and exits used hindsight; her skip rules (engine vs 3-of-5 disagreement, no room to target) held in-sample |

### 3.4 `fresh-base` research (2026-10-18/19, 216 stocks)
| # | Strategy | Data | Best result | Run ids |
|---|---|---|---|---|
| 1 | order-block.com playbook (strength board → 5m OB → first retest) | 53 days 1m | −0.41 R; 8 variants negative (any top-20: −0.94 R, t −4.9) | `ob-5a35973994`, `ob-918c00f07a`…`ob-7576a187b8` |
| 2 | 5m liquidity sweep reversal (PDH/PDL, prior value edges, IB) | 36 days | −0.21 R; value-edge sweeps −0.50 R (t −4.0); mirror −0.28 R | `pb-99186464e0`, `pb-71a4c9228e`, `pb-51e4470d88`, `pb-d96418b57f`, `pb-0a7d8f4323` |
| 3 | value-area re-entry (Dalton 80 % rule) | 36 days | −0.13 R | `pb-1c7778965a`, `pb-2bff5600f1`, `pb-dd70199e6c` |
| 4 | 1h/4h sweep → 15m/5m BOS entry | 18 months 5m | −0.03 R (costs fell to 0.035 R/trade) | `mtf-29fe72e400` … `mtf-6fb19408ab` |
| 5 | Fibonacci (golden / OTE) + pivots/CPR + 1h OB, 15m entry | 18 months | −0.08 R; **Fibonacci no better than a 40–55 % control** | `fib-85ee299414` … `fib-d1d624d7fb` |
| 6 | OI build-up (4 states, daily) | 18 months | filter only: **don't buy after short build-up / short after long build-up** (−0.20 / −0.18 R) | — |
| 7 | 16 states (futures × call OI × put OI) | 63,576 stock-days | no prediction on its own | `research/oi16_study.py` |
| 8 | **Owner's 64-state table** (futures/call/put each LB/SC/SB/LU; score ±4; premium signal) | 63,508 stock-days | no prediction alone; as a gate, longs after Bear days −0.19 R, shorts after Bull days −0.15 R | `research/oi64_study.py` |
| 9 | Swing reversal at price-action levels + structure trail | 4.5 years daily | ~~+0.32 R, +51 R~~ — survivorship. **Only stocks with futures on the day: −0.32 R, −34.9 R, −₹1.73 L, 109 trades** — §5 | `swing-266ee719c5` → `swing-6f44d1bf43` |
| 10 | **Double bottom / top, entered on the first 5m close beyond the neckline**, measured-move target (`research/confirm.py`) | 2024-04 → 2025-09, 5m, point-in-time F&O | **0.009 R before slippage and charges, −0.205 R after**, 81,134 trades, every month negative. No filter changes it: lighter 2nd test, higher low vs undercut, at a daily pivot, 15m/1h Supertrend with or against, time of day — all −0.16…−0.24 R | `confirm-586c27cf99` |
| 11 | **Owner's CPR retest**: 5m close beyond TC (BC), a later 5m bar back to it that closes beyond, 15m Supertrend(10,3) agreeing; target R1 (S1) (`research/confirm.py`) | same | **+0.061 R before slippage and charges, −0.508 R after**, 39,297 trades; median stop 22 bps, so 10 bps of slippage alone ≈ 0.5 R. Narrow/wide CPR, open beyond/inside, 1h with/against — all ≤ −0.45 R. (The rule's 1-minute step can't be tested: the 1m cache starts inside the holdout.) | `confirm-6b0db9921c` |
| 11b | **Owner's CPR rejection, 1:2** (as he trades it): price above the CPR comes back into it, a 5m bar closes back above TC; stop under P, target 2 × risk; no Supertrend flip waited for; 1h agreement recorded two ways | same | **+0.016 R before slippage and charges, −0.366 R after**, 46,803 trades, 0/18 months. 1h structure agreeing: +0.010 / −0.360 R; 1h Supertrend: −0.002 / −0.372; both + a weekly level inside the CPR: −0.017 / −0.313 (4,123 trades). Wider risk loses less (costs), never wins | `confirm-8e1152abae` |
| 11c | **Owner's full CPR sequence** (1m, `research/dbcpr.py`): 5m double bottom under the CPR → 1m Supertrend turns → a 1m rejection at P/TC from above → the first close above that bar's high; 1:2 | 2024-04 → 2025-09, 1m, all F&O | **7,791 trades: +0.04 R before costs, −0.36 R after** (stop one CPR level beyond), −0.64 R with the stop under the rejection bar; 0/18 months. 1h agreeing, weekly level in the CPR, P vs TC, side, year — none turns it | `dbcpr-b31c1779f9` |
| 12 | Same double bottom/top on **recorded 1m bars** with the futures-flow ("selling eased") and option-writer ("puts written") checks (`research/confirm_live.py`) | 19 + 23 Oct 2026 | 1,078 signals, −0.29 R before costs; neither check separates winners. Two days — re-run when ≥ 15 are recorded | — |

Rows 10–11 came from a hand read of ETERNAL on 2026-10-23 (CPR retest at 12:05, R1 hit at 13:05; NIFTY's
double bottom 11:00–11:05 with put writing under it): one good day, picked afterwards. The owner's
point stands — **the first candle is an alert, the entry is the confirmation** — and the rules were
built that way; over 18 months neither confirmation beats a random walk on the price path.

**Pre-registered studies of 2026-10-23** (`docs/research/2026-10-23-prereg-unsupervised.md`; pass = t ≥ 3, ≥ 60 % months, both halves, beats costs):
- **Option signals → next 1 / 5 sessions** (`research/optsignals.py`, `optsig-1140012c2c`, 158,131 stock-days 2021-04 → 2025-09). None passes after costs, but the chain carries information beyond price: **steeper put skew → stock up next day** (IC +0.037, t 10.5; unchanged with the day's own move removed — opposite to the US literature), **OTM put OI build → up** (t 9.4; "put writers = support"), **abnormal option/stock volume → down** (t 7.5, 43/54 months; as Johnson & So), call−put IV spread → down (t −5.3). Best quintile spread 13 bps/day vs 29 bps to trade both legs. Isolation-forest anomalies move ~7 % more than the rest next day (passes, t 7.3) but still less than their IV priced — no help to option buyers. **Lead: a combined chain score, rebalanced weekly.**
- **Regimes** (`research/regimes.py`, `regime-b254ea133d`, GMM k = 4 fitted on the past only, 2022-04 → 2025-09): calm uptrend (346 sessions), quiet drift down (285), relief rally (158), fear (72). NIFTY's next-day move ÷ what its ATM IV priced (1.0 = fair): **0.88 overall; 0.77 in the calm uptrend (t −4.75, 33/39 months, both halves) — PASSES the pre-registered sellers'-regime test**; but 5.5 % of those days moved > 2× priced (worst 4.3×), so only hedged selling. No intraday rule is positive in any regime; the 1-day reversal is not regime-dependent. **Lead: hedged NIFTY weekly selling in the calm regime — backtest on bhavcopy prices, then the holdout.**
- **Day types** (`research/daytype.py`, `daytype-7265de4ef6`, 57,849 stock-days): k-means finds grind up (22 %), grind down (26 %), range/chop with both extremes in the first hour (33 %), trend up (11 %), trend down (8 %). At 11:00 the type is predicted right 44 % of the time (vs 31 % for the commonest). Trading 11:00 → close on it: **+11 bps gross, −3.6 bps after 14.6 bps costs**, t −2.2, 5/12 months — fails; barely better than simply following the morning move (−4.2). The morning move continues a little (+4.8 bps gross on all days), never enough to pay.

Tools built alongside: board/scanner, volume & market profile, order-flow recorder, vendor data seam,
NSE OI pipeline, Gautam Jha's published strategy (1-minute PDH/PDL breakout) reviewed — it is the
intraday scale already shown to lose.

### 3.5 kotsin-crypto (Delta Exchange India)
| Idea | Verdict |
|---|---|
| CAN2 (5m breakout + volume surge) | ~0 gross, −11 bps net, 2,845 trades — **retired by the owner** |
| Scalping battery | chasing loses everywhere; only a fade of a 5m 30-bar breakout without volume surge showed +3.8 bps gross (t 3.2), viable only with Delta's free-exit (Scalper Offer) |
| Cross-asset lead/lag | no edge; kept as a risk layer |
| KK (a real trader's 15m EMA 9/26 + 4h trend + confluence) | his live record +$371, 72 % wins; mechanised over 2.6 y every version negative (−0.32 R OOS); his edge is discretion |
| SMC price action (equal-low bases, BOS/CHoCH, swing trail) | research workflow launched; final numbers not recorded |
| RL exit policy + 24-arm bandit; LLM committee | don't beat baselines / built but off (no API key) |

### 3.6 Lessons that repeat across all of it
1. **Entries are the problem, not exits.** Tuning stops only changes how fast a bad entry loses.
2. **Tight intraday stops + ₹40/order make costs 0.3–0.6 R a trade.** Hour/daily stops bring it to ~0.04 R.
3. **Indicators and positioning (Fibonacci, OI states, 64-state) don't predict on their own;** as filters
   they remove bad trades, they don't create good ones.
4. **Shorts lost in every version until they got a fast exit and a weak-stock filter** (falls are fast).
5. ~~The only positive result is slow~~ — it was survivorship (§5). **No strategy is positive today.**
6. **Trade only what was tradable on the day.** A backtest over today's F&O list buys stocks NSE admitted
   later, often because they had already risen. Every backtester must use `market/fo_universe.py`.

---

## 4. Research protocol (keep it)

- Costs: `risk/costs.py` on a stock future; 5 bp slippage on market fills; statistics by day/week, not trade.
- **Exploration window:** intraday strategies 2024-04-01 → 2025-09-30; daily strategies 2021-04-01 → 2025-09-30.
- **Holdout: 2025-10-01 → 2026-10-16 — UNTOUCHED.** One run, for one finalised candidate, judged by
  criteria written down *before* the run.
- Pass rule used so far: t(by week) ≥ 2 and ≥ 60 % of months positive. For a low-win, big-winner
  strategy (§5) the proposed criteria are: holdout total R > 0, profit factor ≥ 1.2, still positive
  after removing its single best trade, ≥ 15 trades. **Not yet run — awaiting the owner's go.**
- Every backtester counts a **funnel** (how many setups survive each rule) — read it before changing rules.
- **Point-in-time universe:** a stock is traded only on days it had stock futures (`market/fo_universe.py`,
  from the bhavcopies). The other backtesters (`obtest`, `playbooks`, `mtf`, `fibob`) do not use it yet.
- Known 5paisa history facts (`docs/FIVEPAISA_FACTS.md`): the latest session is returned only if the
  request's end runs past it (pad +2 days); finished days' 1m stops at 15:14; the latest session is
  provisional after 15:14 (lumped bars); recent days' 5m stop at 15:10 (2025 days run to 15:30);
  `OIChangePercent` is always 0 (use the bhavcopy); OI frames carry no timestamp; the feed is
  snapshots (~6.5/min/symbol measured on MCX; NSE measured from 2026-10-19 on the System page).

---

## 5. The swing strategy (`research/swing.py`) — dropped 2026-10-23

**Verdict:** the profit below was survivorship. The backtest traded today's 217 F&O stocks on every day
since 2021. With only stocks that had stock futures on the decision day (`fo_members=true`, now the
default), the same rules make **109 trades, 22 % win, −0.32 R avg, −34.9 R, −₹1.73 L, t(by week) −1.78**
(`swing-6f44d1bf43`). The 51 trades in stocks not yet in F&O made +86.3 R (BDL +34 R in Nov 2023,
HINDZINC +24 R in Apr 2024, DELHIVERY, KAYNES, ANGELONE…). By year: 2021 +4.1, 2022 −21.3, 2023 −16.0,
2024 −2.7, 2025 +0.9 R. **Do not spend the holdout on it.** The original write-up is kept below.

**Plain English:** wait for a stock to reject an important level, confirm the turn, enter, and let the
market decide how far it runs.

1. **Level (daily):** the day's low touches (within 0.15 of a normal day) a **double bottom** (an intact
   prior daily swing low), a **retest** of a broken high, or the **previous week/month pivot / S1**.
2. **Rejection:** long lower tail (≥ 2× body, ≥ half the range), close in the top 40 %, volume ≥ 1.2× the
   20-day average.
3. **New momentum:** within 10 days a daily close above the last lower high; the rejection low holds.
4. **Room:** nearest resistance (untaken daily swing high, previous month high, monthly R1) ≥ **2× risk**.
5. **Entry:** next day's open. **Stop** just beyond the rejection low.
6. **Longs exit:** the stop moves under every newly confirmed daily higher low — no target, no time limit.
7. **Shorts:** only in weak stocks (close < 50-day avg, 20-day < 50-day); once +1 R, the stop follows the
   previous day's high (falls are fast).
8. Risk a fixed ₹ per trade (₹5,000 in tests), stock futures, rolled monthly, max 8 open.

Command:
```bash
uv run kotsin-nse swing-backtest --start 2021-04-01 --end 2025-09-30 --label "..." \
  --set use_bias=false --set room_r=2 --set fast_trail_after_r=1 --set fast_trail_sides=short \
  --set trend_filter=true --set trend_filter_sides=short
```

Exploration result `swing-266ee719c5`: **160 trades, 25 % win, +0.32 R avg, +51 R, net +₹2.56 L,
profit factor 1.44, 12 trades ≥ 5 R, best +34 R** (HINDZINC 2024: 346 → 699), worst drawdown 25 R,
longest losing streak 16. By year: 2021 +13.6, 2022 −6.1 (shorts +9.8 offset longs −15.9),
2023 +23.2, 2024 +22.0, 2025 −1.3 R.

**Caveat:** fragile — without its best 3 trades it is −19 R; bootstrap P(total ≤ 0) ≈ 15 %.

How we got there (all exploration, `swing-*` runs): room 5R → too few trades; longs-only room 5R
`swing-6f10a8b5de` +0.59 R (63); longs room 2R `swing-9402619295` +0.32 R (113); shorts base
`swing-2059d20125` −0.55 R → fast trail + weak only `swing-d7bdbd6c04` +0.32 R (47); a fast trail on
longs killed the big winners (`swing-1eb2cc291c`), a strong-stock filter on longs hurt (`swing-05cccb9428`).
The 64-state bias gate (`use_bias=true`) only has data from 2024, which left too few trades to judge.

---

## 6. Open work / next steps

1. **No candidate.** The holdout (2025-10-01 → 2026-10-16) is still untouched; keep it for a strategy that
   survives the point-in-time universe. Re-check the other backtesters with `fo_universe` too.
2. Robustness block for every backtest report: top-5 share of profit, total without the best 1/3/5,
   4-week block bootstrap, excess over drift / "just an up market" control, count of variants tried.
3. **Live recording:** the launchd jobs start the engine at 08:55 and stop it at 16:00 on weekdays. They
   never ran before 2026-10-23: macOS blocked launchd's zsh from ~/Downloads until /bin/zsh got Full
   Disk Access; the scripts exist only on `fresh-base`, so keep it checked out in the main folder.
   Recorded NSE days: 19 Oct, 23 Oct, then every session. From 23 Oct: the book's
   readings (OFI, queue, microprice, weighted imbalance). After ≥ 15 days: `kotsin-nse micro-ic` — keep
   a reading only with a stable IC (0.03–0.05) and a top-minus-bottom spread above costs.
4. **Parked (2026-10-23): a plain-language read per stock on the board** — "gap up and go / fading /
   holding / filled", opened above yesterday's high, side of VWAP since when, futures OI behind the
   move (fresh longs / shorts, covering, flat), CVD faded from its peak, rejected at yesterday's high,
   the book flipping at the day's extreme, with or against its sector, the next pivot zone. Draft
   rules in `docs/parked/story.py` (not wired, untested); meant for the stock popup ("What's
   happening") and a tag per stock in the Opening gaps card. From the 23 Oct open analysis (SWIGGY,
   INFY, COFORGE, TCS).
5. **Board "Align" (x/4) does not separate stocks on a one-sided day:** three of its four points are
   market-wide (sector lean, breadth) or nearly so, so on 23 Oct every bearish contender scored 4/4
   and no bullish one could pass 2/4. Replace with stock-specific checks (own VWAP side, futures OI
   in its direction, CVD/OFI its way, stronger than its sector) and show the market lean once.
6. **F&O panel: the last 15 minutes and the last hour** (`analysis/buildup.recent`, in `fno_snapshot` as
   `recent`): futures build-up, and calls / puts OI change summed over the strikes that have a point at
   both ends (so strike re-centring can't show as a jump — the since-open PCR flipped 0.61 ↔ 1.15 on
   NIFTY for that reason). OI history per contract is kept in the engine (`oi_hist`) and refilled from
   the archive at a restart. FLAT under 0.1 % (15 min) / 0.2 % (1 h) — display thresholds, untested.
7. The 64-state gate can now be judged from 2021 (`oi_daily_legacy/`; join on symbol + expiry, there is
   no contract token before 2024).
8. Owner's wishlist not yet done: one self-sufficient strategy with **sector-wise** layer and
   **PCR / option OI walls** (daily data exists in the bhavcopy); read the old repos
   (`fastAnalayticsKotsin`, `optionProducerJava`, kotsin-box) for PCR/OI logic.
9. Data vendors: Accelpix API registration in review (personal use only); GFDL being registered. Buy only
   if a backtest shows order flow adds edge. Selling a board to others would need a redistribution licence.
10. Devina's later feed fixes (Stage 0–5 on `phase10-13-consolidated`) are not on `fresh-base`; port one at
   a time if needed. Known defects on the old branches (inverted option stop, dead held-quote refresh,
   corporate-action mismatch between daily and intraday caches) were found in the 2026-10-03 review.
11. kotsin-crypto `main` has 6 unpushed commits (archive gzip, cross-asset); push only on the owner's word.
