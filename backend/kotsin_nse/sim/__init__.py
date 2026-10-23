"""Replay of recorded sessions for option-buying research: a day's quotes, books and flow per stock
(``day.py``), an environment that buys an at-the-money call or put and sells it later at the
recorded bid with real charges (``env.py``), baseline policies (``policies.py``) and a runner
(``run.py``). Research only — nothing here places an order."""
