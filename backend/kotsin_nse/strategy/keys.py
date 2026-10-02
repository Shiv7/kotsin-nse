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
    #: The FUDKII trigger under the RT *exit* policy — sustain, hard floor, peak ratchet. Its
    #: entries are FUDKII's, taken on the same contract at the same price, so the only thing being
    #: compared is the exit. Its own wallet and its own slots, so the two equity curves stand apart
    #: and neither can starve the other of capital.
    FUDKII_RT_X = "FUDKII_RT_X"
    #: The same RT exit policy on MCX, in its own wallet. Commodities and equities do not share a
    #: purse here: one CRUDEOIL lot is a different size of bet from one BLUESTARCO lot, and a book
    #: holding both would have its equity curve driven by whichever happened to fire first. Sized at
    #: Rs 30,00,000 so its thirty slots are reachable — the NSE book's Rs 10,00,000 binds at ten.
    FUDKII_RT_MCX = "FUDKII_RT_MCX"
    #: The two other RT exit policies, twinned off the same FUDKII fills (docs/PIVOTS.md §6): N is
    #: the immediate-arming 2 % dwell book that ran on 2026-09-23, Y the third vertical.
    FUDKII_RT_N = "FUDKII_RT_N"
    FUDKII_RT_Y = "FUDKII_RT_Y"
    #: The counter-trend pair (strategy/counter.py): when a FUDKII trigger runs into a ≥ 5.2 wall the
    #: fade is traded with the opposite OTM — CT-X under RT-X's exits, CT-Y under RT-Y's.
    FUDKII_CT_X = "FUDKII_CT_X"
    FUDKII_CT_Y = "FUDKII_CT_Y"
    #: A SHADOW of RT-Y: its entries, its exits, one difference — the equity stop 1 % further from
    #: entry (the "1 % past" test, 2026-09-26). Its own wallet so the curve stands apart; kept off
    #: the trading tabs and shown on the Shadow page.
    FUDKII_RT_Y_W1 = "FUDKII_RT_Y_W1"
    #: A SHADOW with entries of its own: RT-Y's rules on the triggers FUDKII grades F and does not
    #: publish — which RT-Y never sees — with the raw pivots ahead as targets when no cluster makes
    #: one (2026-09-28). Its own wallet; on the Shadow page, off the trading tabs.
    FUDKII_RT_Y_F = "FUDKII_RT_Y_F"

    @property
    def display_name(self) -> str:
        return {
            StrategyKey.FUDKII: "FUDKII",
            StrategyKey.FUKAA: "FUKAA",
            StrategyKey.FUDKII_RT_X: "FUDKII-RT-X",
            StrategyKey.FUDKII_RT_MCX: "FUDKII-RT-MCX",
            StrategyKey.FUDKII_RT_N: "FUDKII-RT-N",
            StrategyKey.FUDKII_RT_Y: "FUDKII-RT-Y",
            StrategyKey.FUDKII_CT_X: "FUDKII-CT-X",
            StrategyKey.FUDKII_CT_Y: "FUDKII-CT-Y",
            StrategyKey.FUDKII_RT_Y_W1: "RT-Y · wide stop (shadow)",
            StrategyKey.FUDKII_RT_Y_F: "RT-Y · graded F (shadow)",
        }[self]

    @property
    def wallet_id(self) -> str:
        return f"strategy-wallet-{self.value}"


#: Opening capital per book. Anything not listed takes ``paper_initial_inr``.
INITIAL_INR: dict[StrategyKey, float] = {
    StrategyKey.FUDKII_RT_MCX: 3_000_000.0,
}

ALL_KEYS: tuple[StrategyKey, ...] = tuple(StrategyKey)

#: Books that exist to be compared, not traded on their own entries: they mirror another book's
#: fills with one rule changed, and live on the Shadow page rather than among the trading tabs.
SHADOW_OF: dict[StrategyKey, StrategyKey] = {
    StrategyKey.FUDKII_RT_Y_W1: StrategyKey.FUDKII_RT_Y,
}

#: Every book shown on the Shadow page instead of the trading tabs, and left out of the day's
#: totals: the mirrors above, and the graded-F shadow, which places entries of its own on triggers
#: no trading book takes.
SHADOW_BOOKS: frozenset[StrategyKey] = frozenset({*SHADOW_OF, StrategyKey.FUDKII_RT_Y_F})
