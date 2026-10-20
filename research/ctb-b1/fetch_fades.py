"""Download the option candles (1m for October, 1d from 1 Aug) of every B1 fade in triggers_oct.csv - before the 27 Oct
expiry takes them off the broker. Read-only login. Run from kotsin-nse/backend."""

from __future__ import annotations

import asyncio
import csv
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.venue.fivepaisa.auth import Authenticator
from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST

HERE = Path(__file__).resolve().parent
OUT = HERE / "data"
MASTER = Path("data-live/scripmaster/nse_fo-2026-10-20.csv")
EXPIRY = "2026-10-27"


def contracts() -> list[tuple[str, float, str, str, int]]:
    master = {}
    for r in csv.DictReader(open(MASTER)):
        if r["ScripType"] in ("CE", "PE") and r["Expiry"].startswith(EXPIRY):
            master[(r["SymbolRoot"], float(r["StrikeRate"]), r["ScripType"])] = (r["ScripCode"], int(r["LotSize"]))
    out = []
    for t in csv.DictReader(open(HERE / "triggers_oct.csv")):
        if t["b1_take"] != "True" or not t["fade_strike"]:
            continue
        k = float(t["fade_strike"])
        code, lot = master[(t["symbol"], k, t["fade_side"])]
        out.append((t["symbol"], k, t["fade_side"], code, lot))
    return sorted(set(out))


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    s = Settings()
    async with httpx.AsyncClient() as http:
        rest = FivePaisaREST(s, http, Authenticator(s, http))
        for sym, k, ot, code, lot in contracts():
            tag = f"{sym.replace('&', 'and')}_{k:g}{ot}_{code}"
            if (OUT / f"{tag}_1m.parquet").exists():
                continue
            inst = Instrument(scrip_code=code, symbol=sym, segment=Segment.NSE_FO, kind=InstrumentKind.OPTION,
                              name=f"{sym} {EXPIRY} {ot} {k}", lot_size=lot, expiry=EXPIRY, strike=k,
                              option_type=OptionType(ot), underlying=sym)
            daily = await rest.candles(inst, "1d", "2026-08-01", "2026-10-23")
            pd.DataFrame(daily).to_parquet(OUT / f"{tag}_1d.parquet", index=False)
            minute: list[dict] = []
            d = date(2026, 10, 1)
            while d <= date(2026, 10, 21):
                end = d + timedelta(days=4)
                minute += await rest.candles(inst, "1m", d.isoformat(), (end + timedelta(days=2)).isoformat())
                d = end + timedelta(days=1)
                await asyncio.sleep(0.2)
            m = pd.DataFrame(minute).drop_duplicates("dt").sort_values("dt") if minute else pd.DataFrame(columns=["dt", "o", "h", "l", "c", "v"])
            m.to_parquet(OUT / f"{tag}_1m.parquet", index=False)
            print(f"{tag:30s} daily {len(daily):3d}  1m {len(m):5d}")
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
