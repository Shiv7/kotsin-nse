# Five option-selling playbooks, what our data says, and what we are missing

6 Nov 2026 (02:00–03:15). The owner pasted five transcripts: the "Batman" double ratio spread (16/20-delta, 2:1 each side,
40–50 days out, stop = the centre profit, no adjustment); Yogesh Nanda's weekly NIFTY strangle (Tuesday entry for the next
week, strikes outside the 3 % weekly range, premium balancing to 70 %, 2.5–3 % of margin stop, exit two days before expiry);
Theta Gainers' BANKNIFTY monthly iron condor → iron fly (25-delta shorts, 17-delta hedges at the breakevens, adjust only at
09:45 / 15:20 when a short strike is hit, ₹20k a month loss budget on ₹5 lakh, half the margin used); Vaibhav Shinde's
"sleep" strategy (intraday DT0/DT1 fixed-premium straddle on NIFTY and SENSEX, 50 % per-leg stop then the other leg to
cost, exit 15:15, pledged funds for margin, 25 uncorrelated automated strategies, 0.8 % slippage in the backtest, Monte
Carlo on drawdowns); Mitesh Patel's interview (support/resistance/breakout, fixed risk per trade, RR > 1, large capital so
2 % a month satisfies, no revenge trading, expiry-day spikes make expiry trading a loser now); "The Options Seller"
(expiry-day far-OTM selling after the third candle with a 50-paise stop, 4 % a month on ₹1 crore).

## What they share

Every one is a short-volatility book: the return is the variance premium, the risk is the tail. Our own numbers say the
premium is real — the NIFTY 10:00 straddle sold to the close made +134 points over seven live days, the Batman structure
+1,864 points with its stop and +3,138 without over 30 monthly cycles (2024-02 → 2026-10, bhavcopy closes), and buying
the ATM stock straddle for five sessions loses on average (−0.4 % of cost, positive 35 %) except in the stock's own
lowest IV decile (+3.4 %). The craft in all five is not the entry; it is sizing, hedging, the clock and the loss budget.

The common rules: never a naked leg overnight (far-OTM hedges as a standing cost); a monthly loss budget in rupees, not a
tight mark-to-market stop; management only at fixed clock times; strikes from the measured range (NIFTY close-to-close
under 3 % in 91 % of weeks, BANKNIFTY 84 % — but the week's extreme goes beyond 3 % in 20 % and 35 % of weeks, which is
what a strangle actually meets); days-to-expiry windows (40–50 for ratio spreads, the first days of the month for
condors, DT0/DT1 for the intraday straddle, exit two days before expiry for positional); half the margin used; the capital
sized so that 2–4 % a month is a satisfying absolute number; backtest with slippage, then automate with error alerts.

## What our data adds

* The Batman stop (close the structure when the mark-to-market loss equals the credit) fired in 9 of 30 cycles; six of
  those cycles would have ended profitable at expiry — the stop was hit by volatility spikes that reverted — and it cut
  the two real disasters (Sep 2024, Sep 2026) in half. Same pattern as the owner's 19-point straddle stop (+127 → +4 over
  seven days): a mark-to-market stop on a short-vol book costs more than it saves except in the rare crash. The
  practitioners' answer is the budgeted loss plus wings, not the tight stop.
* The credit of the Batman at entry averaged 141 points, 4.1 % of the video's margin — its "4 % in the centre" is right.
  The sold strikes sat 4.4 % from the future on average; the future crossed one in 11 of 30 cycles.
* Intraday option strategies (the sleep straddle, the expiry-day OTM sale) cannot be tested on our data: the bhavcopies
  are closes, the tick archive is eight days old. Everything we know about the 10:00 straddle rests on eight sessions.

## What we are missing

1. **A portfolio risk layer.** No capital allocation across books, no per-strategy monthly loss budget, no margin model
   (the covered book's margin is a setting), no slippage model for options (0.5–0.8 % of premium is what the
   practitioners assume), no drawdown distribution (Monte Carlo over the recorded P&L). Every shadow book reports gross
   and charges; none reports what it would do to a ₹20 lakh account over a bad quarter.
2. **A standing tail hedge.** The straddle record is naked; the fly variant is the only defined-risk structure; the
   covered book's downside beyond the 5 % stop is a gap the stop cannot catch. Nothing we run buys the far wing as a
   cost of doing business.
3. **Expiry-calendar discipline.** No days-to-expiry rules (no new short premium inside two days of expiry, no
   expiry-day sales except through the following week's chain, DT0/DT1 flags on every record), no monthly-cycle entries.
4. **Position management for option structures.** No premium balancing, no condor-to-fly conversion, no roll of a
   hedge inward, no fixed management clock (09:45 / 15:20). The covered book manages every minute, which the
   practitioners would call over-trading.
5. **Intraday option history.** Eight days of ticks. The only way to test the intraday selling rules is to buy or
   accumulate a 1-minute NIFTY option history; the engine's archive accumulates from 23 Oct, so this is months away
   unless bought.
6. **Volatility regime in the engine.** No India VIX series, no per-stock IV percentile (now computable from the
   bhavcopy store), no weekly-versus-monthly term structure, no skew history. The go / no-go the practitioners use
   ("weekly got hard in 2022", "VIX up 30 % in May") has no field in our board.
7. **A reusable option-structure simulator.** The Batman test is a one-off script. A simulator over the bhavcopy
   closes (strikes by delta, daily mark-to-market, stops, rolls, loss budget) would test the condor, the fly, the
   strangle and the ratio variants in minutes, with the same pre-registration discipline as the equity book.
8. **Diversification.** One equity rule, one straddle record, one book. The practitioners run several uncorrelated
   systems so that one bad month is a fraction of the capital.

What we do that the videos do not: controls (the random pick, the shifted walls), pre-registration, archive-pinned
numbers, and honesty about N. The Batman homework the video assigns — "backtest two years yourselves" — is done above.

Outputs: `docs/research/scripts/2026-11-06-batman/` (batman.py, batman-dte40/45/50.txt, the trade lists);
`2026-11-06-swing-screen/iv_cheap.py`, iv-cheap.txt, iv-cheap-forward.txt.
