"""The scrip master, parsed once a day into typed :class:`Instrument` records.

This is the **single** place a symbol becomes a scrip code, and the single place a lot size,
tick size or contract multiplier comes from. Everything downstream takes an ``Instrument``.

Three defects in the old stack all reduce to not having this file:

* ``getAllOptionableUnderlyings()`` returned symbols into ``getPivotLevels(scripCode)`` → PIVOTBOSS
  never fired, for its entire life, with ``written=0 skipped=243`` printed every morning;
* the CAN2 OI gate queried a *stored* future scrip code that expires monthly → ``NO_DATA`` for 39
  of 40 scrips; here :meth:`front_future` resolves it live from the current master;
* MCX sizing ignored ``Multiplier`` → a 286-lot ALUMINI entry logged ₹99,943 against a real
  notional of ₹99.9 million.

The master is refreshed on a calendar day change, not on a timer, because that is when it changes:
new weekly expiries appear and expired contracts disappear overnight.
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from ..config import Segment, Settings
from ..domain import Instrument, InstrumentKind, OptionType

log = structlog.get_logger(__name__)


def _f(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def _i(row: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(row.get(key) or default))
    except (TypeError, ValueError):
        return default


def _expiry_date(raw: str) -> str:
    """The master ships ``2026-09-25 00:00:00`` or ``/Date(…)/`` depending on segment."""
    text = (raw or "").strip()
    if not text or text.startswith("1980"):
        return ""
    if text.startswith("/Date("):
        digits = "".join(ch for ch in text[6:] if ch.isdigit())
        if digits:
            return date.fromtimestamp(int(digits) / 1000).isoformat()
        return ""
    return text[:10]


@dataclass(slots=True)
class Catalogue:
    """Indexed view over one day's scrip master."""

    by_code: dict[str, Instrument] = field(default_factory=dict)
    equity_by_symbol: dict[str, Instrument] = field(default_factory=dict)
    futures_by_symbol: dict[str, list[Instrument]] = field(default_factory=lambda: defaultdict(list))
    options_by_symbol: dict[str, list[Instrument]] = field(default_factory=lambda: defaultdict(list))
    loaded_day: date | None = None
    rows_parsed: int = 0
    rows_skipped: int = 0

    # -- lookups ---------------------------------------------------------------------------------

    def get(self, scrip_code: str) -> Instrument | None:
        return self.by_code.get(str(scrip_code))

    def equity(self, symbol: str) -> Instrument | None:
        return self.equity_by_symbol.get(symbol.upper())

    def front_future(self, symbol: str, *, on: date | None = None) -> Instrument | None:
        """Nearest unexpired future. Resolved live, never cached across days — stored future codes
        going stale is what silently disabled the CAN2 OI gate."""
        today = (on or date.today()).isoformat()
        live = [f for f in self.futures_by_symbol.get(symbol.upper(), []) if f.expiry >= today]
        return min(live, key=lambda f: f.expiry) if live else None

    def expiries(self, symbol: str, *, on: date | None = None) -> list[str]:
        today = (on or date.today()).isoformat()
        return sorted({o.expiry for o in self.options_by_symbol.get(symbol.upper(), []) if o.expiry >= today})

    def chain(self, symbol: str, expiry: str, option_type: OptionType) -> list[Instrument]:
        rows = [
            o
            for o in self.options_by_symbol.get(symbol.upper(), [])
            if o.expiry == expiry and o.option_type is option_type and o.strike > 0
        ]
        return sorted(rows, key=lambda o: o.strike)

    def optionable_symbols(self) -> list[str]:
        return sorted(self.options_by_symbol)

    def stats(self) -> dict[str, Any]:
        return {
            "loaded_day": self.loaded_day.isoformat() if self.loaded_day else None,
            "instruments": len(self.by_code),
            "equities": len(self.equity_by_symbol),
            "underlyings_with_futures": len(self.futures_by_symbol),
            "underlyings_with_options": len(self.options_by_symbol),
            "rows_parsed": self.rows_parsed,
            "rows_skipped": self.rows_skipped,
        }


def parse_master(text: str, segment: Segment) -> list[tuple[Instrument, bool]]:
    """CSV → instruments. Returns ``(instrument, ok)`` so the caller can count skips instead of
    discovering later that a third of the file vanished silently."""
    out: list[tuple[Instrument, bool]] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        code = (row.get("Scripcode") or row.get("ScripCode") or "").strip()
        if not code:
            continue
        scrip_type = (row.get("ScripType") or "").strip().upper()
        strike = _f(row, "StrikeRate")
        name = (row.get("Name") or "").strip().upper()
        root = (row.get("SymbolRoot") or "").strip().upper() or name
        expiry = _expiry_date(row.get("Expiry") or "")
        if segment is Segment.NSE_EQ:
            kind = InstrumentKind.EQUITY
            otype = OptionType.NONE
            underlying = name
            series = (row.get("Series") or "").strip().upper()
            if series and series not in ("EQ", "BE"):
                out.append((Instrument(code, name, segment, kind), False))
                continue
        elif scrip_type in ("CE", "PE") and strike > 0:
            kind = InstrumentKind.OPTION
            otype = OptionType.CE if scrip_type == "CE" else OptionType.PE
            underlying = root
        else:
            kind = InstrumentKind.FUTURE
            otype = OptionType.FUT
            underlying = root
        inst = Instrument(
            scrip_code=code,
            symbol=name if kind is InstrumentKind.EQUITY else root,
            segment=segment,
            kind=kind,
            name=(row.get("FullName") or row.get("Name") or "").strip(),
            lot_size=max(1, _i(row, "LotSize", 1)),
            tick_size=_f(row, "TickSize", 0.05) or 0.05,
            multiplier=max(1, _i(row, "Multiplier", 1)),
            expiry=expiry,
            strike=strike,
            option_type=otype,
            underlying=underlying,
        )
        out.append((inst, True))
    return out


class CatalogueLoader:
    """Fetches, caches and refreshes the master. The cache file makes a restart cheap and lets the
    engine boot (degraded) when the broker's master endpoint is down."""

    def __init__(self, settings: Settings, fetch: Any) -> None:
        self.s = settings
        self._fetch = fetch  # FivePaisaREST.scrip_master_csv
        self.catalogue = Catalogue()
        self._cache_dir = settings.data_dir / "scripmaster"
        self._lock = asyncio.Lock()

    def _cache_path(self, segment: Segment, day: date) -> Path:
        return self._cache_dir / f"{segment.scripmaster_key}-{day.isoformat()}.csv"

    async def ensure(self, day: date | None = None, *, force: bool = False) -> Catalogue:
        """``force`` refetches today's master even if cached — scripFinder's 09:20 IST rebuild,
        which exists because strikes listed between 09:00 and 09:15 are missing from a master
        pulled overnight."""
        day = day or date.today()
        async with self._lock:
            if self.catalogue.loaded_day == day and not force:
                return self.catalogue
            if force:
                for seg in self.s.segment_list:
                    self._cache_path(seg, day).unlink(missing_ok=True)
            await self._load(day)
            return self.catalogue

    async def _load(self, day: date) -> None:
        started = time.time()
        cat = Catalogue(loaded_day=day)
        wanted = {s.scripmaster_key: s for s in self.s.segment_list}
        for key, segment in wanted.items():
            text = await self._text_for(segment, day)
            if not text:
                log.error("catalogue.segment_unavailable", segment=key)
                continue
            for inst, ok in parse_master(text, segment):
                if not ok:
                    cat.rows_skipped += 1
                    continue
                cat.rows_parsed += 1
                cat.by_code[inst.scrip_code] = inst
                if inst.kind is InstrumentKind.EQUITY:
                    cat.equity_by_symbol.setdefault(inst.symbol, inst)
                elif inst.kind is InstrumentKind.FUTURE:
                    cat.futures_by_symbol[inst.underlying].append(inst)
                elif inst.kind is InstrumentKind.OPTION:
                    cat.options_by_symbol[inst.underlying].append(inst)
        self.catalogue = cat
        log.info("catalogue.loaded", took_s=round(time.time() - started, 1), **cat.stats())

    async def _text_for(self, segment: Segment, day: date) -> str:
        path = self._cache_path(segment, day)
        if path.exists():
            return path.read_text()
        try:
            text = await self._fetch(segment)
        except Exception as exc:  # noqa: BLE001
            log.warning("catalogue.fetch_failed", segment=segment.value, error=str(exc))
            return self._newest_cached(segment)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        for old in sorted(self._cache_dir.glob(f"{segment.scripmaster_key}-*.csv"))[:-5]:
            old.unlink(missing_ok=True)
        return text

    def _newest_cached(self, segment: Segment) -> str:
        files = sorted(self._cache_dir.glob(f"{segment.scripmaster_key}-*.csv"))
        if not files:
            return ""
        log.warning("catalogue.using_stale_cache", segment=segment.value, file=files[-1].name)
        return files[-1].read_text()
