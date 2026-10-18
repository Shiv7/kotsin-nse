"""Sector of each stock, from NSE's own Nifty 500 list (its ``Industry`` column).

``sectors.csv`` is that list as published at
https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv (fetched 2026-10-18). An F&O
stock outside the Nifty 500 is ``Other`` until the file is refreshed.
"""

from __future__ import annotations

import csv
from functools import cache
from pathlib import Path

PATH = Path(__file__).with_name("sectors.csv")


@cache
def sectors() -> dict[str, str]:
    with PATH.open() as f:
        return {r["symbol"].upper(): r["sector"] for r in csv.DictReader(f)}
