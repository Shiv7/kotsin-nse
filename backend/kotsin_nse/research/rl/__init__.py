"""Offline RL for exits, on the backtester's own trades.

Ported from kotsin-crypto/research/rl and re-based on this book: episodes are the trades the
backtester actually opened (same signals, same fills, same CostModel), the environment enforces the
session's force-flat as a terminal state the policy cannot avoid, and the baseline is the
backtester's own exit rule set (breakeven after T1, the proportional trail, targets, time stop).
Everything is evaluated walk-forward, out of sample, against that baseline, with a day-blocked
paired permutation test and cost stress. One symbol at a time; nothing here needs a GPU or a
second core.
"""
