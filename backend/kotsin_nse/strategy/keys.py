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
    #: A SHADOW with entries of its own: the fade of a published trigger the market is clearly
    #: against — at most CT_M_MARKET_AGAINST_MAX of the NSE names past today's open its way — under
    #: CT-Y's fade plan and exits (operator, 2026-10-03: "fade when the market is clearly against" as
    #: a shadow, named FUDKII-CT-M). Its own wallet; on the Shadow page, off the trading totals.
    FUDKII_CT_M = "FUDKII_CT_M"
    #: STOP-RULE MIRRORS (operator, 2026-10-04: "each startegy ... have all 3 [stop rules] ... and then we
    #: compare which ... works best with which startegy"): every book's fill copied into two shadows — the
    #: same contract, size, price, instant and plan — that differ from it in the stop alone. ``_SE``: Model
    #: E, the stock's stop with a fixed 60 s confirmation; ``_SA``: the stock's stop confirmed by magnitude
    #: × time (risk/exits.py ``_equity_stop``). They keep running on their own rules when the operator
    #: closes the real trade ("yes they should keep running as per their rules"). Their own wallets; on the
    #: Shadow page and on each card's stop-rule strip, never in the day's totals.
    FUDKII_SE = "FUDKII_SE"
    FUDKII_SA = "FUDKII_SA"
    FUDKII_RT_X_SE = "FUDKII_RT_X_SE"
    FUDKII_RT_X_SA = "FUDKII_RT_X_SA"
    FUDKII_RT_N_SE = "FUDKII_RT_N_SE"
    FUDKII_RT_N_SA = "FUDKII_RT_N_SA"
    FUDKII_RT_Y_SE = "FUDKII_RT_Y_SE"
    FUDKII_RT_Y_SA = "FUDKII_RT_Y_SA"
    FUDKII_CT_X_SE = "FUDKII_CT_X_SE"
    FUDKII_CT_X_SA = "FUDKII_CT_X_SA"
    FUDKII_CT_Y_SE = "FUDKII_CT_Y_SE"
    FUDKII_CT_Y_SA = "FUDKII_CT_Y_SA"
    FUDKII_RT_MCX_SE = "FUDKII_RT_MCX_SE"
    FUDKII_RT_MCX_SA = "FUDKII_RT_MCX_SA"
    FUDKII_RT_Y_F_SE = "FUDKII_RT_Y_F_SE"
    FUDKII_RT_Y_F_SA = "FUDKII_RT_Y_F_SA"
    FUDKII_RT_Y_W1_SE = "FUDKII_RT_Y_W1_SE"
    FUDKII_RT_Y_W1_SA = "FUDKII_RT_Y_W1_SA"
    FUDKII_CT_M_SE = "FUDKII_CT_M_SE"
    FUDKII_CT_M_SA = "FUDKII_CT_M_SA"

    @property
    def display_name(self) -> str:
        if (m := STOP_MIRRORS.get(self)) is not None:
            return f"{m[0].display_name} · {STOP_RULE_LABELS[m[1]]} (shadow)"
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
            StrategyKey.FUDKII_CT_M: "FUDKII-CT-M · market-against fade (shadow)",
        }[self]

    @property
    def wallet_id(self) -> str:
        return f"strategy-wallet-{self.value}"


#: The stop rules every book is measured under: "current" is the book's own stop as it trades today,
#: "E" and "A" its two mirrors' (``STOP_MIRRORS``).
STOP_RULE_LABELS: dict[str, str] = {"current": "current stop", "E": "stop E", "A": "stop adaptive"}

#: mirror → (the book whose fills it copies, its stop rule)
STOP_MIRRORS: dict[StrategyKey, tuple[StrategyKey, str]] = {
    k: (StrategyKey(k.value[:-3]), "E" if k.value.endswith("_SE") else "A")
    for k in StrategyKey
    if k.value.endswith(("_SE", "_SA"))
}


def stop_mirrors_of(book: str) -> dict[str, StrategyKey]:
    """``{rule: mirror}`` for a book — its two stop-rule mirrors — or {} for a book with none."""
    return {rule: k for k, (src, rule) in STOP_MIRRORS.items() if src.value == book}


def stop_rule_of(book: str) -> tuple[str, str]:
    """``(the book whose trade it is, the stop rule it runs)``: a mirror's source and its rule, any other
    book itself under its ``"current"`` stop."""
    try:
        m = STOP_MIRRORS.get(StrategyKey(book))
    except ValueError:
        m = None
    return (m[0].value, m[1]) if m else (book, "current")


#: Opening capital per book. Anything not listed takes ``paper_initial_inr``. A stop-rule mirror
#: takes its source's, so the purses compared stand on the same footing.
INITIAL_INR: dict[StrategyKey, float] = {
    StrategyKey.FUDKII_RT_MCX: 3_000_000.0,
    StrategyKey.FUDKII_RT_MCX_SE: 3_000_000.0,
    StrategyKey.FUDKII_RT_MCX_SA: 3_000_000.0,
}

ALL_KEYS: tuple[StrategyKey, ...] = tuple(StrategyKey)

#: Books that exist to be compared, not traded on their own entries: they mirror another book's
#: fills with one rule changed, and live on the Shadow page rather than among the trading tabs.
SHADOW_OF: dict[StrategyKey, StrategyKey] = {
    StrategyKey.FUDKII_RT_Y_W1: StrategyKey.FUDKII_RT_Y,
}

#: Every book shown on the Shadow page instead of the trading tabs, and left out of the day's
#: totals: the mirrors above, the graded-F shadow, which places entries of its own on triggers
#: no trading book takes, CT-M, and every stop-rule mirror.
SHADOW_BOOKS: frozenset[StrategyKey] = frozenset({*SHADOW_OF, StrategyKey.FUDKII_RT_Y_F, StrategyKey.FUDKII_CT_M, *STOP_MIRRORS})

#: The books that fade the trigger — they buy the opposite option to the one the SuperTrend flip asks
#: for (strategy/counter.py, CT-Y's gap fade, CT-M's market-against fade). Every other book trades the
#: trigger's own way: with the new trend.
COUNTER_TREND: frozenset[StrategyKey] = frozenset({StrategyKey.FUDKII_CT_X, StrategyKey.FUDKII_CT_Y, StrategyKey.FUDKII_CT_M})


def describe_book(strategy: str) -> dict[str, str]:
    """A ledger row's ``strategy`` as the trades page shows it (operator, 2026-10-04: "add the strategy
    name and counter-trend/trend"): the book's name and the side of the trigger it trades. A key no
    longer in the registry keeps its raw string and an unknown side."""
    try:
        key = StrategyKey(strategy)
    except ValueError:
        return {"strategy_label": strategy, "trend": ""}
    side = STOP_MIRRORS[key][0] if key in STOP_MIRRORS else key  # a stop-rule mirror trades its source's side
    return {"strategy_label": key.display_name, "trend": "counter-trend" if side in COUNTER_TREND else "trend"}
