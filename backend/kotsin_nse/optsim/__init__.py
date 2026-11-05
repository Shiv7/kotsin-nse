"""Option structures on NSE's daily F&O bhavcopies: the chain as settlement closes, structures built by delta, a
simulator with the calendar, the loss budget and the management rules the option-selling playbooks use, the paper
books that run the pre-registered variants forward one bhavcopy at a time, the risk layer (allocation, margin,
slippage, drawdown distribution) and the nightly volatility regime. Built 6 Nov 2026 on the owner's "lets build all we
need proper system for us", after five option-selling transcripts were read against our data
(docs/research/2026-11-06-option-selling-playbooks.md). Nothing here orders anything."""
