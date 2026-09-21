"""The review committee: a post-mortem on the algo's own decisions, with an experiment loop.

Ported from kotsin-crypto's ``committee`` (TradingAgents-shaped roles, structured outputs,
self-grading memory) and re-aimed: the crypto committee forecasts a market; this one reviews
**what FUDKII/FUKAA did and why it lost**, names the failure mode from a fixed taxonomy, proposes
one parameter change at a time, and grades each proposal by running the backtester. The learning
loop is propose → test → grade → remember; the reward is the day-clustered change in average R.

Nothing here can place, block or resize a trade.
"""
