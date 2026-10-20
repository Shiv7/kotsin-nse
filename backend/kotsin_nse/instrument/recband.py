"""The record band: the option strikes the 5-read recorder keeps streaming around each name's LIVE spot.

Record-only (operator, 2026-10-19: record the 5-read and the strike walls live, "for research only"). Every standing
option subscription was anchored on the previous close — 5 strikes a side on prices, 12 on OI — and nothing followed
spot until a book priced a trigger, which is after the trigger: on 19 Oct PNB's quotes covered 114-118 with the
trigger's close at 123.88, and its legs never printed. The band follows spot instead: both types of every listed
strike within K strikes of any price the name has shown today (sticky for the day), on the legs' own expiry — the
nearest at least 2 calendar days out, from today's date.

Both types, because a CE leg at a bearish close is a call that was in the money at the candle start, and a PE leg at
a bullish close the mirror (measured 12-19 Oct: an OTM-only band misses CGPOWER, NIFTY and others at any K).

Pure: instruments in, instruments out. ``Engine._rec_sync`` subscribes them on the feed's record set.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from ..domain import Instrument, OptionType
from .catalogue import Catalogue

#: strikes each side of the price range (K = 3: all four legs streaming before the candle start on 97 % of the 144
#: research-scored triggers of 14-19 Oct, against 43 % on the boot band; DESIGN_record5.md §4)
DEFAULT_BAND_STRIKES = 3
MIN_DAYS_TO_EXPIRY = 2


def legs_expiry(cat: Catalogue, root: str, today: date, min_days: int = MIN_DAYS_TO_EXPIRY) -> str | None:
    """The legs' expiry for ``root`` on ``today``: the nearest listed option expiry at least ``min_days`` calendar
    days after today (inputs14; ``UniversePolicy.min_days_to_expiry``). Read from today's date at every call — a
    ``ScripGroup.option_expiry`` set at boot or at 09:20 is not consulted."""
    ok = [e for e in cat.expiries(root, on=today) if e and (date.fromisoformat(e[:10]) - today).days >= min_days]
    return min(ok) if ok else None


def chain_pair(cat: Catalogue, root: str, expiry: str) -> tuple[list[Instrument], list[Instrument]]:
    """The listed calls and puts of ``root`` on ``expiry``, each sorted by strike."""
    return cat.chain(root, expiry, OptionType.CE), cat.chain(root, expiry, OptionType.PE)


def strikes_near(chain: Sequence[Instrument], lo: float, hi: float, k: int) -> list[Instrument]:
    """Every listed strike within ``k`` strikes of the price range [lo, hi]: the strikes inside it (and equal to
    either end), plus ``k`` below ``lo`` and ``k`` above ``hi``. ``chain`` sorted by strike."""
    ks = [i.strike for i in chain]
    i0 = bisect_left(ks, lo)
    i1 = bisect_right(ks, hi)
    return list(chain[max(0, i0 - k):min(len(chain), i1 + k)])


def band(ce: Sequence[Instrument], pe: Sequence[Instrument], lo: float, hi: float, k: int) -> list[Instrument]:
    """Both types within ``k`` strikes of [lo, hi]."""
    return [*strikes_near(ce, lo, hi, k), *strikes_near(pe, lo, hi, k)]


@dataclass(slots=True)
class RootBand:
    """One name's band for the day: the expiry, the price range seen so far, the codes it holds."""

    root: str
    expiry: str
    lo: float
    hi: float
    codes: dict[str, Instrument] = field(default_factory=dict)

    def widen(self, price: float) -> bool:
        """Take in a new price; True when the range grew (the band may grow with it)."""
        if price <= 0:
            return False
        grew = price < self.lo or price > self.hi
        self.lo, self.hi = min(self.lo, price), max(self.hi, price)
        return grew


def nearest_first(instruments: Iterable[Instrument], anchor: dict[str, float]) -> list[Instrument]:
    """Additions in the order the cap admits them: the strike nearest its name's current price first."""
    return sorted(instruments, key=lambda i: (abs(i.strike - anchor.get(i.underlying or i.symbol, i.strike)), i.scrip_code))


def capped(new: Sequence[Instrument], already_extra: int, cap: int, engine_has: set[str]) -> tuple[list[Instrument], bool]:
    """``new`` cut so the codes the engine does not itself want (the EXTRA subscriptions) stay within ``cap``: codes
    the engine already holds cost nothing and always pass. Returns (admitted, capped)."""
    room = max(0, cap - already_extra)
    out: list[Instrument] = []
    hit = False
    for i in new:
        if i.scrip_code in engine_has:
            out.append(i)
        elif room > 0:
            out.append(i)
            room -= 1
        else:
            hit = True
    return out, hit


__all__ = ["DEFAULT_BAND_STRIKES", "MIN_DAYS_TO_EXPIRY", "RootBand", "band", "capped", "chain_pair", "legs_expiry",
           "nearest_first", "strikes_near"]
