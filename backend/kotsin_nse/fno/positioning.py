"""The operator's 64-state table (2026-10-18): futures, the near-the-money calls and the puts each
get their OWN build-up state from their own price move and OI change.

* state — price up + OI up = LB (long build-up), up + down = SC (short covering), down + up = SB
  (short build-up), down + down = LU (long unwinding);
* score — futures LB +2 / SC +1 / SB −2 / LU −1; calls LB +1 / SC +0.5 / SB −1 / LU −0.5 (calls are
  bullish when bought); puts the reverse (SB +1 / LU +0.5 / LB −1 / SC −0.5);
* bias — from the score; premium signal — both premiums up = IV↑ (a big move priced in), both down
  = IV↓ (range), else Normal when the calls move with the future and Divergence when they do not.

The research study (``research/oi64_study.py``) found no next-day edge in the bias on its own: this
is a reading of positioning, not a signal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

FUT_SCORE = {"LB": 2.0, "SC": 1.0, "SB": -2.0, "LU": -1.0}
CALL_SCORE = {"LB": 1.0, "SC": 0.5, "SB": -1.0, "LU": -0.5}
PUT_SCORE = {"LB": -1.0, "SC": -0.5, "SB": 1.0, "LU": 0.5}
LONG = {"LB": "LONG BUILD-UP", "SC": "SHORT COVERING", "SB": "SHORT BUILD-UP", "LU": "LONG UNWINDING"}
SHORT = {v: k for k, v in LONG.items()}

#: the table as the operator numbered it: four futures blocks (LB, SC, SB, LU) of sixteen, the
#: call/put pairs in the same order inside every block — row 39 = SB futures, LB calls, LB puts
_PAIRS = "LB SB,LB LU,SC SB,SC LU,LB SC,LU SB,LB LB,SB SB,SC SC,LU LU,SB LU,SC LB,LU SC,SB SC,LU LB,SB LB"
ROWS: tuple[tuple[str, str, str], ...] = tuple(
    (f, *pair.split()) for f in ("LB", "SC", "SB", "LU") for pair in _PAIRS.split(",")  # type: ignore[misc]
)
ROW_OF = {k: i + 1 for i, k in enumerate(ROWS)}

#: an OI change smaller than this (%) is too small to call a build-up — the leg is flagged weak
WEAK_OI_PCT = 1.0


def leg_state(price_up: bool, oi_up: bool) -> str:
    return ("LB" if oi_up else "SC") if price_up else ("SB" if oi_up else "LU")


def bias(score: float) -> str:
    if score >= 3:
        return "Strong Bull"
    if score >= 1.5:
        return "Bull"
    if score > 0:
        return "Mild Bull"
    if score == 0:
        return "Neutral"
    if score > -1.5:
        return "Mild Bear"
    if score > -3:
        return "Bear"
    return "Strong Bear"


def premium_signal(fut_up: bool, call_up: bool, put_up: bool) -> str:
    if call_up and put_up:
        return "IV↑ big move"
    if not call_up and not put_up:
        return "IV↓ range"
    return "Normal" if call_up == fut_up else "Divergence"


@dataclass(frozen=True, slots=True)
class Leg:
    """One leg's move since its reference: premium (or futures price) % and OI change."""

    price_chg_pct: float
    oi_chg: float  # contracts (or units) added since the reference
    oi_chg_pct: float | None  # vs the reference OI; None when there is none
    #: an OI change smaller than this many contracts is no build-up either, whatever its % — on
    #: 2026-10-20 GOLD's calls read "short build-up" on +7 contracts against 16,739 in the future
    min_contracts: float = 0.0

    @property
    def state(self) -> str:
        return leg_state(self.price_chg_pct > 0, self.oi_chg > 0)

    @property
    def weak(self) -> bool:
        return (
            self.oi_chg_pct is None
            or abs(self.oi_chg_pct) < WEAK_OI_PCT
            or abs(self.oi_chg) < self.min_contracts
        )

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state, "weak": self.weak}


@dataclass(frozen=True, slots=True)
class Reading:
    fut: str
    call: str
    put: str
    score: float
    bias: str
    premium: str
    row: int
    weak: tuple[str, ...]  # legs whose OI moved less than WEAK_OI_PCT — read them as unconfirmed

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "weak": list(self.weak)}


def read(fut: Leg, calls: Leg, puts: Leg) -> Reading:
    f, c, p = fut.state, calls.state, puts.state
    score = FUT_SCORE[f] + CALL_SCORE[c] + PUT_SCORE[p]
    weak = tuple(name for name, leg in (("futures", fut), ("calls", calls), ("puts", puts)) if leg.weak)
    return Reading(
        fut=f,
        call=c,
        put=p,
        score=score,
        bias=bias(score),
        premium=premium_signal(fut.price_chg_pct > 0, calls.price_chg_pct > 0, puts.price_chg_pct > 0),
        row=ROW_OF[(f, c, p)],
        weak=weak,
    )
