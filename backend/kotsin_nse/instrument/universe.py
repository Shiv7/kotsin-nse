"""The trading universe, built the way the old ``scripFinder`` built it — ported, not reinvented.

``scripFinder`` derived the F&O universe from the derivatives master itself: every ``SymbolRoot``
that has a future or an option **is** the universe (216 roots on 2026-09-21: 210 stocks + 6
indices), and the cash equity is joined to it by root. Then a ``ScripGroup`` per root — equity,
the current-month future(s), and a shortlist of strikes — was what the WebSocket subscribed to.
The strike shortlist was the important design choice: strikes within **±12% of the previous
close** (widened from ±8% on 2026-04-20 to catch intraday drift), the **5 closest to ATM on each
side**, so downstream selection ranks a real candidate pool by liquidity instead of being forced
onto one pre-chosen OTM strike. It was rebuilt nightly and again at **09:20 IST**, twenty minutes
after 09:00, to catch strikes listed between 09:00 and 09:15.

All of that is kept. What changed: the join is by the typed :class:`Instrument`, not by string;
a root with derivatives but no cash equity (an index) uses its front future as the underlying —
the same rule MCX already follows — instead of vanishing; and the build is **two passes**, because
strike selection needs the previous close and the close comes from the backfill of the universe
the first pass produces.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog

from ..config import Segment
from ..domain import Instrument, InstrumentKind, OptionType
from .catalogue import Catalogue

log = structlog.get_logger(__name__)

INDEX_ROOTS: frozenset[str] = frozenset(
    {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"}
)


@dataclass(frozen=True, slots=True)
class UniversePolicy:
    #: strikes admitted within ±band of the previous close (scripFinder: 0.12)
    band_pct: float = 12.0
    #: per side, closest to ATM (scripFinder: scripgroup.strikes.per.side=5)
    strikes_per_side: int = 5
    #: an expiry closer than this is all theta; skip to the next one
    min_days_to_expiry: int = 2
    #: front + next month, so a rollover never leaves the OI source empty
    futures_count: int = 2
    include_indices: bool = True
    #: MCX bands are wider (scripFinder passed caller-supplied factors for commodities)
    mcx_band_pct: float = 20.0


@dataclass(slots=True)
class ScripGroup:
    """One underlying and everything the engine subscribes to on its account."""

    root: str
    segment: Segment
    underlying: Instrument  # cash equity, or the front future when there is no cash leg
    equity: Instrument | None
    futures: list[Instrument] = field(default_factory=list)
    options: list[Instrument] = field(default_factory=list)
    close: float | None = None
    option_expiry: str | None = None
    note: str = ""

    @property
    def front_future(self) -> Instrument | None:
        return self.futures[0] if self.futures else None

    def to_json(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "segment": self.segment.value,
            "underlying": self.underlying.scrip_code,
            "underlying_kind": self.underlying.kind.value,
            "equity": self.equity.scrip_code if self.equity else None,
            "futures": [f.scrip_code for f in self.futures],
            "options": len(self.options),
            "close": self.close,
            "option_expiry": self.option_expiry,
            "note": self.note,
        }


class UniverseBuilder:
    def __init__(self, catalogue: Catalogue, policy: UniversePolicy | None = None) -> None:
        self.cat = catalogue
        self.policy = policy or UniversePolicy()

    # -- pass 1: who is in the universe -------------------------------------------------------

    def fno_roots(self, segment: Segment) -> set[str]:
        """Every root with a future or option in ``segment``'s derivatives master —
        ``FNOUniverseService.extractFNOUniverse`` verbatim."""
        roots: set[str] = set()
        for f in self.cat.futures_by_symbol:
            if any(x.segment is segment for x in self.cat.futures_by_symbol[f]):
                roots.add(f)
        for o in self.cat.options_by_symbol:
            if any(x.segment is segment for x in self.cat.options_by_symbol[o]):
                roots.add(o)
        return roots

    def _unexpired_futures(self, root: str, segment: Segment, today: date) -> list[Instrument]:
        t = today.isoformat()
        rows = [
            f
            for f in self.cat.futures_by_symbol.get(root, [])
            if f.segment is segment and f.expiry >= t
        ]
        return sorted(rows, key=lambda f: f.expiry)[: self.policy.futures_count]

    def build_underlyings(self, segments: Iterable[Segment], today: date) -> dict[str, ScripGroup]:
        groups: dict[str, ScripGroup] = {}
        for segment in segments:
            if segment is Segment.NSE_EQ:
                continue  # cash is reached through NSE_FO's roots; on its own it has no derivatives
            for root in sorted(self.fno_roots(segment)):
                futures = self._unexpired_futures(root, segment, today)
                equity = self.cat.equity(root) if segment is Segment.NSE_FO else None
                if equity is not None:
                    underlying = equity
                elif futures:
                    if root in INDEX_ROOTS and not self.policy.include_indices:
                        continue
                    underlying = futures[0]
                else:
                    log.debug("universe.skip", root=root, reason="no cash leg and no unexpired future")
                    continue
                groups[root] = ScripGroup(
                    root=root,
                    segment=segment,
                    underlying=underlying,
                    equity=equity,
                    futures=futures,
                    note="index" if root in INDEX_ROOTS else "",
                )
        return groups

    # -- pass 2: which strikes -----------------------------------------------------------------

    def select_strikes(self, group: ScripGroup, close: float | None, today: date) -> None:
        """``ScripGroupPopulator.selectStrikes``: strikes within the band around the previous
        close, the N closest to ATM per side, nearest tradeable expiry."""
        group.close = close
        group.options = []
        group.option_expiry = None
        if not close or close <= 0:
            group.note = (group.note + "; " if group.note else "") + "no close — strikes not selected"
            return
        expiries = [
            e
            for e in self.cat.expiries(group.root, on=today)
            if (date.fromisoformat(e) - today).days >= self.policy.min_days_to_expiry
        ]
        if not expiries:
            return
        expiry = expiries[0]
        band = self.policy.mcx_band_pct if group.segment is Segment.MCX_FO else self.policy.band_pct
        lo, hi = close * (1 - band / 100), close * (1 + band / 100)
        chosen: list[Instrument] = []
        for otype in (OptionType.CE, OptionType.PE):
            rows = [i for i in self.cat.chain(group.root, expiry, otype) if lo <= i.strike <= hi]
            rows.sort(key=lambda i: abs(i.strike - close))
            chosen += rows[: self.policy.strikes_per_side]
        group.options = sorted(chosen, key=lambda i: (i.option_type.value, i.strike))
        group.option_expiry = expiry

    def select_all(self, groups: dict[str, ScripGroup], close_for: Callable[[str], float | None], today: date) -> int:
        n = 0
        for g in groups.values():
            self.select_strikes(g, close_for(g.underlying.symbol), today)
            n += len(g.options)
        return n

    # -- what to subscribe -------------------------------------------------------------------------

    @staticmethod
    def subscriptions(groups: Iterable[ScripGroup]) -> dict[str, list[Instrument]]:
        """``getDesiredWebSocket``: ticks + depth on what we decide and trade, OI on what has it.

        Cash equity has no open interest, so it is never put on the ``oi`` channel — reading OI off
        the cash segment is the defect that pinned MicroAlpha's OI term at zero for its whole life.
        """
        mf: dict[str, Instrument] = {}
        md: dict[str, Instrument] = {}
        oi: dict[str, Instrument] = {}
        for g in groups:
            mf[g.underlying.scrip_code] = g.underlying
            md[g.underlying.scrip_code] = g.underlying
            for f in g.futures:
                mf[f.scrip_code] = f
                oi[f.scrip_code] = f
            for o in g.options:
                mf[o.scrip_code] = o
                md[o.scrip_code] = o
                oi[o.scrip_code] = o
        return {"mf": list(mf.values()), "md": list(md.values()), "oi": list(oi.values())}

    @staticmethod
    def summary(groups: dict[str, ScripGroup]) -> dict[str, Any]:
        subs = UniverseBuilder.subscriptions(groups.values())
        return {
            "underlyings": len(groups),
            "with_equity": sum(1 for g in groups.values() if g.equity is not None),
            "indices": sum(1 for g in groups.values() if g.note.startswith("index")),
            "with_options": sum(1 for g in groups.values() if g.options),
            "futures": sum(len(g.futures) for g in groups.values()),
            "options": sum(len(g.options) for g in groups.values()),
            "subscriptions": {k: len(v) for k, v in subs.items()},
            "by_segment": {
                s.value: sum(1 for g in groups.values() if g.segment is s) for s in Segment
            },
        }


def kind_label(i: Instrument) -> str:
    return "index-future" if i.kind is InstrumentKind.FUTURE and i.underlying in INDEX_ROOTS else i.kind.value


__all__ = ["INDEX_ROOTS", "ScripGroup", "UniverseBuilder", "UniversePolicy", "kind_label"]
