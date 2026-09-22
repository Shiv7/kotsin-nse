"""HotStocks, assembled: exchange data + the engine's own bars → a ranked book.

Two books, as the old dashboard rendered them side by side:

* **CAN1** — positional, ranked once on the exchange's published data. The bhavcopy and the deal
  disclosures only change after the close, so ranking more often than that would burn NSE calls to
  recompute an identical answer. The response carries ``dataAsOf`` so the page can show the age of
  the *ranking*, not the age of the HTTP call — the distinction the dashboard's own comment says it
  got wrong once already.
* **CAN2** — the live book. The old CAN2 was a separate momentum consumer with its own wallet;
  this repo's equivalent is what the engine is actually holding and has actually closed, so that
  is what is served, under its own name rather than pretending to be the old consumer.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import structlog

from . import metrics
from .nsepublic import NsePublicClient, NsePublicData

log = structlog.get_logger(__name__)

#: Re-fetch the exchange data at most this often. Its inputs are end-of-day.
REFRESH_S = 3600.0


class HotStocksService:
    def __init__(self, engine: Any, sectors_path: Path) -> None:
        self.engine = engine
        self.sectors = metrics.load_sectors(sectors_path)
        self._nse: NsePublicData | None = None
        self._nse_ts = 0.0
        self._cards: list[dict[str, Any]] = []
        self._ranked_ts = 0.0
        self._lock = asyncio.Lock()

    # -- exchange data ----------------------------------------------------------------------------

    async def _ensure_nse(self, *, force: bool = False) -> NsePublicData:
        if self._nse is not None and not force and time.time() - self._nse_ts < REFRESH_S:
            return self._nse
        client = NsePublicClient()
        try:
            data = await client.fetch_all(metrics.today_ist())
        finally:
            await client.aclose()
        # A fetch that returned nothing at all must not replace a good earlier one.
        if data.ok or self._nse is None:
            self._nse, self._nse_ts = data, time.time()
        return self._nse

    # -- CAN1 -------------------------------------------------------------------------------------

    def _oi_5d_pct(self, symbol: str) -> float | None:
        """Five-session OI change on the front future.

        OI arrives on the socket, never on the historical endpoint, so this is only answerable once
        the live archive holds five sessions. Until then it is ``None`` — which zeroes the OI
        bucket rather than scoring the name as if its open interest had not moved.
        """
        g = self.engine.groups.get(symbol)
        if not g or not getattr(g, "futures", None):
            return None
        bars = self.engine.store.bars(symbol, "1d", 6)
        ois = [b.oi for b in bars if getattr(b, "oi", None)]
        if len(ois) < 6 or not ois[0]:
            return None
        return round((ois[-1] - ois[0]) / ois[0] * 100, 2)

    async def rank(self, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            if self._cards and not force and time.time() - self._ranked_ts < REFRESH_S:
                return self._response()

            nse = await self._ensure_nse(force=force)
            fno, non_fno = [], []
            for sym, inst in sorted(self.engine.underlyings.items()):
                daily = self.engine.store.bars(sym, "1d", 300)
                if len(daily) < 21:
                    continue
                g = self.engine.groups.get(sym)
                zones = self.engine.zones_for(sym)
                entry = self._entry_zone(zones, self.engine.ltps.get(inst.scrip_code))
                card = metrics.build(
                    symbol=sym,
                    scrip_code=inst.scrip_code,
                    daily=daily,
                    ltp=self.engine.ltps.get(inst.scrip_code),
                    sector=self.sectors.get(sym.upper(), ""),
                    nse=nse,
                    oi_5d_pct=self._oi_5d_pct(sym),
                    fno_eligible=bool(g and getattr(g, "options", None)),
                    zones_entry=entry,
                    suggested_sl=self._suggested_sl(zones, self.engine.ltps.get(inst.scrip_code)),
                )
                if card is None:
                    continue
                (fno if card["fnoEligible"] else non_fno).append(card)

            self._cards = metrics.rank(fno) + metrics.rank(non_fno)
            self._ranked_ts = time.time()
            log.info(
                "hotstocks.ranked",
                fno=len(fno),
                non_fno=len(non_fno),
                delivery_day=nse.delivery_day,
                deals=len(nse.deals),
                errors=sorted(nse.errors),
            )
            return self._response()

    @staticmethod
    def _entry_zone(zones: list[Any], ltp: float | None) -> tuple[float, float] | None:
        """Nearest support zone below price up to price — where a pullback would be bought."""
        if not ltp:
            return None
        below = [z.price for z in zones if z.price < ltp]
        return (round(max(below), 2), round(ltp, 2)) if below else None

    @staticmethod
    def _suggested_sl(zones: list[Any], ltp: float | None) -> float | None:
        """The second support down — the one a stop sits under, not the one price is resting on."""
        if not ltp:
            return None
        below = sorted((z.price for z in zones if z.price < ltp), reverse=True)
        return round(below[1], 2) if len(below) > 1 else (round(below[0], 2) if below else None)

    def _response(self) -> dict[str, Any]:
        nse = self._nse
        as_of = nse.fetched_ts if nse else None
        fno = [c for c in self._cards if c["fnoEligible"]]
        non_fno = [c for c in self._cards if not c["fnoEligible"]]
        return {
            "fno": fno,
            "nonFno": non_fno,
            "generatedAt": time.time() * 1000,
            "fetchedAt": (as_of or 0) * 1000,
            "dataAsOf": (as_of or 0) * 1000 if as_of else None,
            "dataAgeMinutes": int((time.time() - as_of) / 60) if as_of else None,
            "deliveryDay": nse.delivery_day if nse else None,
            "unavailable": sorted(nse.errors) if nse else ["not fetched"],
            "sources": {
                "delivery": "NSE bhavcopy sec_bhavdata_full",
                "deals": "NSE historicalOR/bulk-block-short-deals (7d)",
                "indices": "NSE allIndices",
                "bars": "5paisa daily history",
                "oi": "5paisa socket (GetScripInfoForFuture), archived from 2026-09-22",
            },
        }

    # -- CAN2 -------------------------------------------------------------------------------------

    def live_book(self, open_positions: list[dict[str, Any]]) -> dict[str, Any]:
        """The engine's own open positions, in the CAN2 column's shape.

        Takes already-serialised positions rather than ``Position`` objects. Reaching into the
        model here duplicated a mapping that ``_position_json`` already owns, and got it wrong:
        ``Position`` has no ``symbol`` — the symbol is on ``underlying`` — so this raised as soon
        as there was an open position to render, having passed every test while the book was empty.
        """
        positions = [
            {
                "symbol": p["symbol"],
                "scripCode": p["scrip_code"],
                "strategy": p["strategy"],
                "contract": p["instrument"]["name"],
                "entryPrice": p["entry"],
                "sl": p["option_sl"],
                "initialSl": p["initial_option_sl"],
                "slPct": (
                    round((p["option_sl"] - p["entry"]) / p["entry"] * 100, 2)
                    if p["entry"] and p["option_sl"] is not None
                    else None
                ),
                "qty": p["qty_remaining"],
                "ltp": p.get("ltp"),
                "unrealizedPct": (
                    round((p["ltp"] - p["entry"]) / p["entry"] * 100, 2)
                    if p.get("ltp") and p["entry"]
                    else None
                ),
                "rNow": p.get("r_now"),
                "grade": p.get("grade"),
                "direction": p["direction"],
                "openedIst": p.get("opened_ist"),
                "targets": p.get("option_targets") or [],
                "equityEntry": p["equity_entry"],
                "equitySl": p["equity_sl"],
            }
            for p in open_positions
        ]
        wallets = [
            {
                "strategy": w.strategy,
                "balance": w.balance,
                "deployed": w.deployed,
                "realizedPnl": w.realized_pnl,
                "trades": w.trades,
                "wins": w.wins,
                "losses": w.losses,
            }
            for w in self.engine.wallets.values()
        ]
        return {
            "positions": positions,
            "wallets": wallets,
            "mode": self.engine.mode().value,
            "generatedAt": time.time() * 1000,
        }
