"""Download the 13 option contracts of FUDKII_CT_Buckets_14-19Oct2026 from 5paisa before they expire (27 Oct 2026).

Per contract: daily candles from 1 Aug (for the option's own daily / weekly / monthly pivots) and every 1-minute candle
of October (the trade days and the days around them). Saved as Parquet under data/, one file per contract and interval.
Run from kotsin-nse/backend so the broker settings in .env are found:

    cd ~/Downloads/kotsin/kotsin-nse/backend && uv run python ~/Downloads/kotsin/ctb-backtest/fetch_options.py
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd

from kotsin_nse.config import Segment, Settings
from kotsin_nse.domain import Instrument, InstrumentKind, OptionType
from kotsin_nse.venue.fivepaisa.auth import Authenticator
from kotsin_nse.venue.fivepaisa.rest import FivePaisaREST

OUT = Path(__file__).resolve().parent / "data"
EXPIRY = "2026-10-27"
#: (symbol, strike, type, scrip code, lot) — scrip codes from the 20 Oct NSE F&O scrip master
CONTRACTS = [
    ("JSWENERGY", 490, "CE", "108306", 1075),
    ("ENRIN", 3000, "PE", "105815", 175),
    ("GVT&D", 4100, "CE", "95302", 125),
    ("AXISBANK", 1300, "CE", "73979", 625),
    ("GODREJCP", 850, "CE", "93731", 500),
    ("SWIGGY", 230, "PE", "111212", 1825),
    ("VOLTAS", 1020, "PE", "132410", 375),
    ("PNB", 122, "PE", "103068", 8000),
    ("AXISBANK", 1240, "CE", "56394", 625),
    ("PREMIERENE", 920, "PE", "105344", 650),
    ("SBICARD", 600, "CE", "107450", 800),
    ("UNIONBANK", 180, "CE", "116330", 4425),
    ("UNITDSPR", 1400, "CE", "116646", 400),
]


def instrument(sym: str, strike: float, ot: str, code: str, lot: int) -> Instrument:
    return Instrument(
        scrip_code=code, symbol=sym, segment=Segment.NSE_FO, kind=InstrumentKind.OPTION,
        name=f"{sym} {EXPIRY} {ot} {strike}", lot_size=lot, expiry=EXPIRY, strike=float(strike),
        option_type=OptionType(ot), underlying=sym,
    )


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    s = Settings()
    async with httpx.AsyncClient() as http:
        rest = FivePaisaREST(s, http, Authenticator(s, http))
        for sym, k, ot, code, lot in CONTRACTS:
            inst = instrument(sym, k, ot, code, lot)
            tag = f"{sym.replace('&', 'and')}_{k}{ot}_{code}"
            # the historical endpoint drops the latest session unless the end runs past it: pad by two days
            daily = await rest.candles(inst, "1d", "2026-08-01", "2026-10-23")
            pd.DataFrame(daily).to_parquet(OUT / f"{tag}_1d.parquet", index=False)
            minute: list[dict] = []
            d = date(2026, 10, 1)
            while d <= date(2026, 10, 21):
                end = d + timedelta(days=4)
                minute += await rest.candles(inst, "1m", d.isoformat(), (end + timedelta(days=2)).isoformat())
                d = end + timedelta(days=1)
                await asyncio.sleep(0.3)
            m = pd.DataFrame(minute).drop_duplicates("dt").sort_values("dt") if minute else pd.DataFrame()
            m.to_parquet(OUT / f"{tag}_1m.parquet", index=False)
            days = sorted({str(x)[:10] for x in m["dt"]}) if len(m) else []
            print(f"{tag:28s} daily {len(daily):3d}  1m {len(m):5d} rows over {len(days)} days ({days[0] if days else '-'} .. {days[-1] if days else '-'})")
            await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
