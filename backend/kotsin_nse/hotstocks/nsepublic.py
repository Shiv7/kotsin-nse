"""NSE's own public data: delivery, disclosed deals, index levels.

The broker serves price, volume, OI, depth and the option chain — and nothing else. Delivery
percentage, bulk/block deal disclosures and sector index levels are **exchange** data, published
by NSE itself, and they are what three of the five HotStocks score buckets are made of. The old
stack reached them through ``nsepython`` inside fastAnalytics; this goes at the same URLs directly,
because the dependency bought nothing but a CSV parse and a cookie jar.

Three sources, each verified live on 2026-09-22:

* ``nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`` — the full bhavcopy,
  one row per scrip, carrying ``DELIV_QTY`` and ``DELIV_PER``. A plain CSV; needs a browser
  ``User-Agent`` and nothing else. Published after the close, so the caller walks back day by day.
* ``www.nseindia.com/api/historicalOR/bulk-block-short-deals`` — disclosed deals with the client
  name, side, quantity and weighted price. Needs the cookie NSE's front page sets.
* ``www.nseindia.com/api/allIndices`` — every index with its percent change, which is how a stock
  is judged against *its own sector* rather than against NIFTY alone.

**None of this is authenticated and none of it is the broker.** It therefore cannot fail in a way
that costs a trade: every fetch returns empty on error and says so in :class:`NsePublicData.errors`,
and the score buckets that depend on it degrade to zero rather than guessing. A HotStocks card that
cannot prove institutional flow must say it cannot, not imply there was none.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

BHAVCOPY = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
DEALS = "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals"
ALL_INDICES = "https://www.nseindia.com/api/allIndices"
HOME = "https://www.nseindia.com/"
DEALS_REFERER = "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"

#: NSE serves a 403 and an HTML challenge to anything that does not look like a browser.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

#: How many calendar days back to look for the most recent published bhavcopy.
BHAV_LOOKBACK_DAYS = 6
#: Window of disclosed deals the flow buckets read. The old engine's smart-money view is 7 days.
DEALS_WINDOW_DAYS = 7


@dataclass(slots=True)
class DealRow:
    day: str
    symbol: str
    client: str
    side: str  # BUY | SELL
    qty: int
    price: float
    kind: str  # bulk | block

    @property
    def value_cr(self) -> float:
        return self.qty * self.price / 1e7


@dataclass(slots=True)
class DeliveryRow:
    symbol: str
    close: float
    prev_close: float
    traded_qty: float
    turnover_lacs: float
    deliv_qty: float
    deliv_pct: float


@dataclass(slots=True)
class NsePublicData:
    """One snapshot. Empty collections mean *not known*, never *zero*."""

    delivery: dict[str, DeliveryRow] = field(default_factory=dict)
    delivery_by_day: dict[str, dict[str, DeliveryRow]] = field(default_factory=dict)
    deals: list[DealRow] = field(default_factory=list)
    indices: dict[str, float] = field(default_factory=dict)
    delivery_day: str = ""
    fetched_ts: float = 0.0
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.delivery or self.deals or self.indices)


def _f(row: dict[str, str], key: str) -> float:
    try:
        return float((row.get(key) or "").strip())
    except (TypeError, ValueError):
        return 0.0


def parse_bhavcopy(text: str) -> dict[str, DeliveryRow]:
    """EQ-series rows of a ``sec_bhavdata_full`` CSV, keyed by symbol.

    The header is space-padded (``" SERIES"``) and so are the values (``" EQ"``), which is why
    every key and cell is stripped before use — reading it raw silently yields an empty series
    filter and a card with no delivery on it.
    """
    out: dict[str, DeliveryRow] = {}
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("SERIES") != "EQ":
            continue
        sym = row.get("SYMBOL", "")
        if not sym:
            continue
        out[sym] = DeliveryRow(
            symbol=sym,
            close=_f(row, "CLOSE_PRICE"),
            prev_close=_f(row, "PREV_CLOSE"),
            traded_qty=_f(row, "TTL_TRD_QNTY"),
            turnover_lacs=_f(row, "TURNOVER_LACS"),
            deliv_qty=_f(row, "DELIV_QTY"),
            deliv_pct=_f(row, "DELIV_PER"),
        )
    return out


def parse_deals(payload: Any, kind: str) -> list[DealRow]:
    rows: list[DealRow] = []
    for r in (payload or {}).get("data") or []:
        sym = str(r.get("BD_SYMBOL") or "").strip()
        if not sym:
            continue
        try:
            qty = int(float(r.get("BD_QTY_TRD") or 0))
            price = float(r.get("BD_TP_WATP") or 0)
        except (TypeError, ValueError):
            continue
        rows.append(
            DealRow(
                day=str(r.get("BD_DT_DATE") or ""),
                symbol=sym,
                client=str(r.get("BD_CLIENT_NAME") or "").strip(),
                side=str(r.get("BD_BUY_SELL") or "").strip().upper(),
                qty=qty,
                price=price,
                kind=kind,
            )
        )
    return rows


def parse_indices(payload: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in (payload or {}).get("data") or []:
        name = str(r.get("indexSymbol") or r.get("index") or "").strip()
        if not name:
            continue
        try:
            out[name.upper()] = float(r.get("percentChange") or 0.0)
        except (TypeError, ValueError):
            continue
    return out


class NsePublicClient:
    """Fetches the three public sources. Never raises — a failure is recorded, not thrown."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._own = client is None
        self.http = client or httpx.AsyncClient(follow_redirects=True, headers=HEADERS)

    async def aclose(self) -> None:
        if self._own:
            await self.http.aclose()

    async def _warm(self) -> None:
        """NSE's ``/api/*`` routes need the cookie its front page sets.

        The warm-up itself answers 403 to a datacentre IP and still hands back the cookie, so its
        status is deliberately ignored — what matters is the jar, not the page.
        """
        try:
            await self.http.get(HOME, headers=HEADERS, timeout=15)
        except Exception as exc:  # noqa: BLE001 - the cookie may already be held
            log.debug("nse.warmup_failed", error=str(exc))

    async def delivery(self, on: date, *, lookback: int = BHAV_LOOKBACK_DAYS) -> tuple[str, dict[str, DeliveryRow]]:
        """Most recent published bhavcopy at or before ``on``. Returns (day, rows)."""
        for back in range(lookback + 1):
            d = on - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            url = BHAVCOPY.format(ddmmyyyy=d.strftime("%d%m%Y"))
            try:
                r = await self.http.get(url, headers=HEADERS, timeout=45)
                if r.status_code != 200 or "SYMBOL" not in r.text[:200]:
                    continue
                rows = parse_bhavcopy(r.text)
                if rows:
                    return d.isoformat(), rows
            except Exception as exc:  # noqa: BLE001 - try the previous day
                log.debug("nse.bhavcopy_failed", day=d.isoformat(), error=str(exc))
        return "", {}

    async def deals(self, start: date, end: date) -> list[DealRow]:
        await self._warm()
        out: list[DealRow] = []
        for opt, kind in (("bulk_deals", "bulk"), ("block_deals", "block")):
            params = {
                "optionType": opt,
                "from": start.strftime("%d-%m-%Y"),
                "to": end.strftime("%d-%m-%Y"),
            }
            try:
                r = await self.http.get(
                    DEALS,
                    params=params,
                    headers={**HEADERS, "Referer": DEALS_REFERER},
                    timeout=45,
                )
                r.raise_for_status()
                out.extend(parse_deals(r.json(), kind))
            except Exception as exc:  # noqa: BLE001 - one kind failing must not lose the other
                log.warning("nse.deals_failed", kind=kind, error=str(exc))
        return out

    async def indices(self) -> dict[str, float]:
        await self._warm()
        try:
            r = await self.http.get(
                ALL_INDICES, headers={**HEADERS, "Referer": HOME}, timeout=30
            )
            r.raise_for_status()
            return parse_indices(r.json())
        except Exception as exc:  # noqa: BLE001
            log.warning("nse.indices_failed", error=str(exc))
            return {}

    async def fetch_all(self, today: date, *, delivery_days: int = 5) -> NsePublicData:
        """Everything the score needs, in one pass. Partial results are returned, not discarded."""
        import time

        data = NsePublicData(fetched_ts=time.time())

        day, rows = await self.delivery(today)
        if rows:
            data.delivery_day, data.delivery = day, rows
            data.delivery_by_day[day] = rows
        else:
            data.errors["delivery"] = "no bhavcopy published in the lookback window"

        # Earlier sessions, for the 5-day delivery average and its trend.
        if day:
            cursor = date.fromisoformat(day)
            for _ in range(delivery_days - 1):
                cursor -= timedelta(days=1)
                prev_day, prev_rows = await self.delivery(cursor, lookback=4)
                if not prev_rows:
                    break
                data.delivery_by_day[prev_day] = prev_rows
                cursor = date.fromisoformat(prev_day)

        deals = await self.deals(today - timedelta(days=DEALS_WINDOW_DAYS), today)
        if deals:
            data.deals = deals
        else:
            data.errors.setdefault("deals", "no disclosed deals returned")

        idx = await self.indices()
        if idx:
            data.indices = idx
        else:
            data.errors["indices"] = "allIndices unavailable"

        log.info(
            "hotstocks.nse_public",
            delivery_day=data.delivery_day,
            delivery_rows=len(data.delivery),
            delivery_days=len(data.delivery_by_day),
            deals=len(data.deals),
            indices=len(data.indices),
            errors=sorted(data.errors),
        )
        return data
