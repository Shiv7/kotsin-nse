"""The single registry of strategy identities.

Keys are an enum, never a string literal. ``"HOTSTOCKS".equals(strategy)`` silently excluding
``HOTSTOCKS_MOMENTUM`` — and the reverse, a substring match collapsing the two — caused live bugs in
both directions: an unreachable time stop, a book left exposed to a sweep it was meant to be exempt
from, and a dashboard that labelled one strategy as the other. An enum makes each of those a
ruff/mypy error instead of a silent mismatch.

Removing a key must break every reference at import time (R6). The old executor kept funding a
wallet, and advertising a strategy, for six weeks after its producer was deleted.
"""

from __future__ import annotations

from enum import StrEnum


class StrategyKey(StrEnum):
    #: 30m SuperTrend flip coinciding with a Bollinger break, expressed as an OTM option.
    FUDKII = "FUDKII"
    #: The same trigger, admitted only when volume confirms participation.
    FUKAA = "FUKAA"

    @property
    def display_name(self) -> str:
        return {StrategyKey.FUDKII: "FUDKII", StrategyKey.FUKAA: "FUKAA"}[self]

    @property
    def wallet_id(self) -> str:
        return f"strategy-wallet-{self.value}"


ALL_KEYS: tuple[StrategyKey, ...] = tuple(StrategyKey)
