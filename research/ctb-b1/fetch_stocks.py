"""Extend the history cache's 5m and 1d stock bars through 20 Oct 2026 for every NSE option root (read-only login)."""
from __future__ import annotations
import asyncio, csv
from pathlib import Path
import httpx
from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind
from kotsin_nse.research.history import HistoryStore
from kotsin_nse.venue.fivepaisa.auth import Authenticator
from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST

MASTER = Path("data-live/scripmaster/nse_fo-2026-10-20.csv")
EQ = Path("data-live/scripmaster/nse_eq-2026-10-20.csv")

async def main() -> None:
    roots = sorted({r["SymbolRoot"] for r in csv.DictReader(open(MASTER)) if r["ScripType"] in ("CE", "PE")})
    code = {r["Name"]: r["ScripCode"] for r in csv.DictReader(open(EQ))}
    s = Settings(); store = HistoryStore(s.data_dir / "history")
    async with httpx.AsyncClient() as http:
        rest = FivePaisaREST(s, http, Authenticator(s, http))
        ok = miss = 0
        for sym in roots:
            c = code.get(sym)
            if c is None:
                miss += 1; print("no cash code", sym); continue
            inst = Instrument(scrip_code=c, symbol=sym, segment=Segment.NSE_EQ, kind=InstrumentKind.EQUITY, underlying=sym)
            for tf, start in (("30m", "2026-09-15"), ("5m", "2026-10-14"), ("1d", "2026-10-01")):
                rows = await rest.candles(inst, tf, start, "2026-10-22")
                store.merge(sym, tf, rows)
                await asyncio.sleep(0.15)
            ok += 1
        print("done", ok, "names;", miss, "without cash code")

asyncio.run(main())
