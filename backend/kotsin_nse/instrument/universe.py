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
    #: MCX: more strikes — "per side" is per option type, so 20 calls and 20 puts nearest the
    #: money ≈ ±10 strikes; ±5 (crude: ±250) was too narrow to see a wall — and, when the nearest
    #: expiry is this close, the next one too: NATURALGAS's options expire 3 days out on 2026-10-20,
    #: all expiry noise. Cheap there: ten roots, against NSE's two hundred.
    mcx_strikes_per_side: int = 20
    mcx_next_expiry_days: int = 7


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
    #: the price the strikes were last centred on (the previous close at boot, the live future after)
    center: float | None = None
    option_expiry: str | None = None  # the nearest selected
    option_expiries: list[str] = field(default_factory=list)  # every selected, nearest first
    note: str = ""

    @property
    def front_future(self) -> Instrument | None:
        return self.futures[0] if self.futures else None

    def future_for(self, expiry: str) -> Instrument | None:
        """The future an option of ``expiry`` is priced on: the nearest one expiring on or after
        it. MCX GOLD's 31 Dec options sit on the February future (December's expires 4 Dec);
        NATURALGAS's 23 Oct options on the October future (27 Oct)."""
        later = [f for f in self.futures if f.expiry >= expiry]
        return min(later, key=lambda f: f.expiry) if later else None

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

    def select_strikes(
        self, group: ScripGroup, close: float | None, today: date, *, around: float | None = None, each_side: int | None = None
    ) -> None:
        """``ScripGroupPopulator.selectStrikes``: strikes within the band around the previous
        close, the N closest to ATM per side, nearest tradeable expiry.

        ``around`` centres the choice on another price than the close — the live future, once the
        day has moved (COFORGE 2026-10-23 gapped +3.5 %: five strikes picked around yesterday's
        1,905 all sat below a 1,973 future). ``each_side`` takes exactly that many strikes below
        and above the one nearest the centre, calls and puts alike (ATM ± N), band or no band."""
        group.close = close
        group.options = []
        group.option_expiry = None
        group.option_expiries = []
        ref = around if around and around > 0 else close
        group.center = ref
        if not ref or ref <= 0:
            group.note = (group.note + "; " if group.note else "") + "no close — strikes not selected"
            return
        expiries = [
            e
            for e in self.cat.expiries(group.root, on=today)
            if (date.fromisoformat(e) - today).days >= self.policy.min_days_to_expiry
        ]
        if not expiries:
            return
        mcx = group.segment is Segment.MCX_FO
        chosen_expiries = expiries[:1]
        if mcx and len(expiries) > 1 and (date.fromisoformat(expiries[0]) - today).days <= self.policy.mcx_next_expiry_days:
            chosen_expiries = expiries[:2]
        band = self.policy.mcx_band_pct if mcx else self.policy.band_pct
        per_side = self.policy.mcx_strikes_per_side if mcx else self.policy.strikes_per_side
        lo, hi = ref * (1 - band / 100), ref * (1 + band / 100)
        chosen: list[Instrument] = []
        for expiry in chosen_expiries:
            for otype in (OptionType.CE, OptionType.PE):
                if each_side is not None:
                    chain = sorted(self.cat.chain(group.root, expiry, otype), key=lambda i: i.strike)
                    if chain:
                        k = min(range(len(chain)), key=lambda j: abs(chain[j].strike - ref))
                        chosen += chain[max(0, k - each_side) : k + each_side + 1]
                    continue
                rows = [i for i in self.cat.chain(group.root, expiry, otype) if lo <= i.strike <= hi]
                rows.sort(key=lambda i: abs(i.strike - ref))
                chosen += rows[:per_side]
        group.options = sorted(chosen, key=lambda i: (i.expiry, i.option_type.value, i.strike))
        group.option_expiry = chosen_expiries[0]
        group.option_expiries = chosen_expiries

    def select_all(self, groups: dict[str, ScripGroup], close_for: Callable[[str], float | None], today: date) -> int:
        n = 0
        for g in groups.values():
            self.select_strikes(g, close_for(g.underlying.symbol), today)
            n += len(g.options)
        return n

    # -- what to subscribe -------------------------------------------------------------------------

    @staticmethod
    def subscriptions(
        groups: Iterable[ScripGroup], *, futures_months: int | None = None, option_depth: bool = True
    ) -> dict[str, list[Instrument]]:
        """``getDesiredWebSocket``: quotes + depth on what we decide and trade, OI on what has it.
        Keys are the generic channel names of ``feed/base.py`` (``quotes``, ``depth``, ``oi``).

        Cash equity has no open interest, so it is never put on the ``oi`` channel — reading OI off
        the cash segment is the defect that pinned MicroAlpha's OI term at zero for its whole life.

        ``futures_months`` keeps only the nearest N futures and ``option_depth=False`` drops the
        options' order books: on 2026-10-19 the 2,751 books were 88 % of all 5paisa messages while
        near futures got ~2 quote frames a minute (``docs/FIVEPAISA_FACTS.md``).
        """
        quotes: dict[str, Instrument] = {}
        depth: dict[str, Instrument] = {}
        oi: dict[str, Instrument] = {}
        for g in groups:
            quotes[g.underlying.scrip_code] = g.underlying
            depth[g.underlying.scrip_code] = g.underlying
            # MCX keeps every listed month (two): its options can sit on the second (GOLD Dec → Feb)
            for f in g.futures[: None if g.segment is Segment.MCX_FO else futures_months]:
                quotes[f.scrip_code] = f
                oi[f.scrip_code] = f
            for o in g.options:
                quotes[o.scrip_code] = o
                if option_depth:
                    depth[o.scrip_code] = o
                oi[o.scrip_code] = o
        return {"quotes": list(quotes.values()), "depth": list(depth.values()), "oi": list(oi.values())}

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
