"""Index membership the engine measures a stock against — read, never traded.

``NIFTY50`` is NSE's own list (nsearchives.nseindia.com/content/indices/ind_nifty50list.csv, fetched
2026-10-01). NSE reconstitutes the index twice a year (end of March and September, effective the
following month); refresh this list then. A stale member only drops out of an average — it never
blocks a decision — and every reading built on it says how many members it used.
"""

from __future__ import annotations

#: as of 2026-10-01
NIFTY50: frozenset[str] = frozenset({
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BSE",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA",
    "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH",
    "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
    "INFY", "INDIGO", "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA",
    "TCS", "TATACONSUM", "TMPV", "TATASTEEL", "TECHM", "TITAN",
    "TRENT", "ULTRACEMCO",
})
