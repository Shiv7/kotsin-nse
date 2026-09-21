"""Signed 5paisa REST client.

Every route and request shape here was read off the running NSE stack or py5paisa, not guessed:

* ``V2/historical/{exch}/{exchType}/{scrip}/{tf}?from=&end=`` on ``openapi.5paisa.com`` with the
  vendor APIM key — the only source of OHLCV history; 1m, 5m, 15m, 30m, 60m and 1d.
* ``V1/MarketFeed`` for a batched snapshot quote.
* ``V1/PlaceOrderRequest`` / ``V1/ModifyOrderRequest`` / ``V1/CancelOrderRequest`` /
  ``V2/OrderStatus`` / ``V2/NetPositionNetWise`` / ``SquareOffAll``.
* ``V2/GetExpiryForSymbolOptions`` + ``GetOptionsForSymbol`` for the option chain.

Two response envelopes exist and both must be checked: ``head.status`` (transport) and
``body.Status`` (RMS). The old client only checked one for a while, and an RMS rejection came back
looking like a successful placement.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from ...config import Segment, Settings
from ...domain import Instrument, InstrumentKind, OptionType, OrderSide
from ..base import VenueError
from .auth import APIM_KEY, Authenticator

log = structlog.get_logger(__name__)

HISTORICAL_BASE = "https://openapi.5paisa.com/V2/historical/"
VALID_INTERVALS = frozenset({"1m", "3m", "5m", "10m", "15m", "30m", "60m", "1d"})

#: Messages the broker returns with ``head.status=1`` that mean "nothing to return", not "your
#: session is dead". Observed live on 2026-09-21: a flat account answers ``V2/NetPositionNetWise``
#: with exactly ``No record found.`` — the same status code a dead session uses.
NO_RECORDS: tuple[str, ...] = ("no record found", "no data found", "no position")


class FivePaisaREST:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, auth: Authenticator) -> None:
        self.s = settings
        self.http = client
        self.auth = auth
        self.calls = 0
        self.failures = 0
        self.last_error: str = ""

    # -- plumbing --------------------------------------------------------------------------------

    def _head(self) -> dict[str, Any]:
        return {"key": self.s.fp_app_key.get_secret_value()}  # type: ignore[union-attr]

    async def _bearer(self) -> dict[str, str]:
        sess = await self.auth.token()
        return {"Authorization": f"Bearer {sess.access_token}"}

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        retry: bool = True,
        empty_ok: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        self.calls += 1
        headers = await self._bearer()
        url = self.s.endpoints.rest + path
        try:
            r = await self.http.post(
                url, json={"head": self._head(), "body": body}, headers=headers, timeout=30
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{path}: {exc}"
            raise VenueError(f"{path} failed: {exc}") from exc
        head, resp = data.get("head") or {}, data.get("body") or {}
        status = str(head.get("status", head.get("Status", "0")))
        if status not in ("0", "None"):
            # The broker overloads these codes: the same status=1 means "your session is dead" and
            # "there is nothing to return". Log what it actually said — without it the only symptom
            # is `head status=1` and there is no way to tell a flat book from a dead session.
            message = str(head.get("statusDescription") or resp.get("Message") or "")
            log.warning(
                "fivepaisa.head_status", path=path, status=status, message=message[:200]
            )
            # Checked before the re-auth: "No record found." is a *successful* answer meaning the
            # book is empty, and treating it as a failure froze a flat account out of trading while
            # re-logging in every 60 seconds to ask the same question.
            if any(m in message.strip().lower() for m in empty_ok):
                return resp
            if retry and status in ("1", "9"):  # session states the broker signals this way
                if self.auth.invalidate():
                    return await self._post(path, body, retry=False, empty_ok=empty_ok)
            self.failures += 1
            raise VenueError(f"{path}: head status={status}", raw=head)
        rms = resp.get("Status")
        if rms is not None and int(rms) != 0:
            self.failures += 1
            raise VenueError(f"{path}: {resp.get('Message', 'RMS rejected')}", raw=resp)
        return resp

    # -- market data -----------------------------------------------------------------------------

    async def candles(
        self, instrument: Instrument, interval: str, start: str, end: str
    ) -> list[dict[str, Any]]:
        """OHLCV rows, oldest first. ``start``/``end`` are ``YYYY-MM-DD`` IST calendar dates.

        The broker returns naive IST timestamps; conversion to epoch is the caller's job (and there
        is exactly one place that does it — ``market.session.ist_naive_to_ts``).
        """
        if interval not in VALID_INTERVALS:
            raise ValueError(f"interval {interval!r} not in {sorted(VALID_INTERVALS)}")
        self.calls += 1
        url = (
            f"{HISTORICAL_BASE}{instrument.exch}/{instrument.exch_type}/"
            f"{instrument.scrip_code}/{interval}?from={start}&end={end}"
        )
        headers = {"Ocp-Apim-Subscription-Key": APIM_KEY, **await self._bearer()}
        try:
            r = await self.http.get(url, headers=headers, timeout=60)
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            self.failures += 1
            self.last_error = f"historical {instrument.symbol}: {exc}"
            raise VenueError(f"historical {instrument.symbol} failed: {exc}") from exc
        rows = ((payload or {}).get("data") or {}).get("candles") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            if len(row) < 6:
                continue
            out.append(
                {
                    "dt": row[0],
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                }
            )
        return out

    async def market_feed(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        """Snapshot quotes keyed by scrip code. Batched — one call for the whole list."""
        if not instruments:
            return {}
        body = {
            "ClientCode": self.s.fp_client_code,
            "Count": len(instruments),
            "MarketFeedData": [
                {"Exch": i.exch, "ExchType": i.exch_type, "ScripCode": i.scrip_code}
                for i in instruments
            ],
        }
        resp = await self._post("V1/MarketFeed", body)
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        for row in resp.get("Data") or []:
            token = str(row.get("Token"))
            out[token] = {
                "scrip_code": token,
                "ltp": float(row.get("LastRate") or 0),
                "prev_close": float(row.get("PClose") or 0),
                "open": float(row.get("OpenRate") or 0),
                "high": float(row.get("High") or 0),
                "low": float(row.get("Low") or 0),
                "bid": float(row.get("BidRate") or 0),
                "ask": float(row.get("OfferRate") or 0),
                "bid_qty": int(row.get("BidQty") or 0),
                "ask_qty": int(row.get("OfferQty") or 0),
                "volume": int(row.get("Volume") or row.get("TotalQty") or 0),
                "ts": now,
            }
        return out

    async def expiries(self, exch: str, symbol: str) -> list[int]:
        """Option expiries for an underlying, as epoch **milliseconds** (the broker's own unit)."""
        resp = await self._post("V2/GetExpiryForSymbolOptions", {"Exch": exch, "Symbol": symbol})
        out: list[int] = []
        for row in resp.get("Expiry") or []:
            raw = str(row.get("ExpiryDate", ""))
            digits = "".join(ch for ch in raw if ch.isdigit() or ch == "-")
            if digits:
                try:
                    out.append(int(digits.split("-")[0] if "-" in digits[1:] else digits))
                except ValueError:
                    continue
        return sorted(out)

    async def option_chain(self, exch: str, symbol: str, expiry_ms: int) -> list[dict[str, Any]]:
        resp = await self._post(
            "GetOptionsForSymbol",
            {"Exch": exch, "Symbol": symbol, "ExpiryDate": f"/Date({expiry_ms})/"},
        )
        return list(resp.get("Options") or [])

    # -- account ---------------------------------------------------------------------------------

    async def net_positions(self) -> list[dict[str, Any]]:
        resp = await self._post(
            "V2/NetPositionNetWise",
            {"ClientCode": self.s.fp_client_code},
            empty_ok=NO_RECORDS,
        )
        out: list[dict[str, Any]] = []
        for row in resp.get("NetPositionDetail") or []:
            out.append(
                {
                    "scrip_code": str(row.get("ScripCode")),
                    "exch": str(row.get("Exch")),
                    "exch_type": str(row.get("ExchType")),
                    "net_qty": int(row.get("NetQty") or 0),
                    "buy_avg": float(row.get("BuyAvgRate") or 0),
                    "sell_avg": float(row.get("SellAvgRate") or 0),
                    "mtm": float(row.get("MTM") or 0),
                    "symbol": str(row.get("ScripName") or row.get("Symbol") or ""),
                }
            )
        return out

    async def margin(self) -> dict[str, Any]:
        resp = await self._post("V4/Margin", {"ClientCode": self.s.fp_client_code})
        rows = resp.get("EquityMargin") or []
        return dict(rows[0]) if rows else {}

    async def order_book(self) -> list[dict[str, Any]]:
        resp = await self._post("V4/OrderBook", {"ClientCode": self.s.fp_client_code})
        return list(resp.get("OrderBookDetail") or [])

    # -- orders -----------------------------------------------------------------------------------

    async def place_order(
        self,
        instrument: Instrument,
        side: OrderSide,
        qty: int,
        *,
        price: float = 0.0,
        intraday: bool = True,
        remote_order_id: str,
        stop_trigger: float = 0.0,
    ) -> str:
        """Place an order and return the broker's ``RemoteOrderID`` echo.

        ``price=0`` is a market order. ``remote_order_id`` is our idempotency key and the broker
        echoes it back, which is what makes reconciliation possible after a crash.
        """
        body: dict[str, Any] = {
            "ClientCode": self.s.fp_client_code,
            "Exchange": instrument.exch,
            "ExchangeType": instrument.exch_type,
            "ScripCode": instrument.scrip_code,
            "OrderType": "Buy" if side is OrderSide.BUY else "Sell",
            "Qty": qty,
            "DisQty": 0,
            "IsIntraday": intraday,
            "AHPlaced": "N",
            "RemoteOrderID": remote_order_id[:38],
            "AppSource": self.s.fp_app_source,
            "iOrderValidity": 0,
        }
        if price > 0:
            body["Price"] = instrument.round_price(price)
        if stop_trigger > 0:
            body["StopLossPrice"] = instrument.round_price(stop_trigger)
            body["IsStopLossOrder"] = True
        resp = await self._post("V1/PlaceOrderRequest", body)
        return str(resp.get("RemoteOrderID") or remote_order_id)

    async def modify_order(self, exch_order_id: str, *, price: float, qty: int) -> None:
        await self._post(
            "V1/ModifyOrderRequest",
            {
                "ExchOrderID": exch_order_id,
                "Price": price,
                "Qty": qty,
                "DisQty": qty,
                "AppSource": self.s.fp_app_source,
            },
        )

    async def cancel_order(self, exch_order_id: str) -> None:
        await self._post(
            "V1/CancelOrderRequest",
            {"ExchOrderID": exch_order_id, "AppSource": self.s.fp_app_source},
        )

    async def order_status(self, exch: str, remote_order_id: str) -> dict[str, Any]:
        resp = await self._post(
            "V2/OrderStatus",
            {
                "ClientCode": self.s.fp_client_code,
                "OrdStatusReqList": [{"Exch": exch, "RemoteOrderID": remote_order_id}],
            },
        )
        rows = resp.get("OrdStatusResLst") or []
        return dict(rows[0]) if rows else {}

    async def square_off_all(self) -> None:
        await self._post("SquareOffAll", {"ClientCode": self.s.fp_client_code})

    # -- scrip master ------------------------------------------------------------------------------

    async def scrip_master_csv(self, segment: Segment) -> str:
        url = self.s.endpoints.scripmaster + segment.scripmaster_key
        r = await self.http.get(url, timeout=180)
        r.raise_for_status()
        return r.text

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "last_error": self.last_error,
            "session": bool(self.auth.session and self.auth.session.valid),
        }


def kind_for(segment: Segment, scrip_type: str, strike: float) -> InstrumentKind:
    if segment is Segment.NSE_EQ:
        return InstrumentKind.EQUITY
    if segment is Segment.NSE_IDX:
        return InstrumentKind.INDEX
    if scrip_type in ("CE", "PE") and strike > 0:
        return InstrumentKind.OPTION
    return InstrumentKind.FUTURE


def option_type_for(scrip_type: str) -> OptionType:
    return {"CE": OptionType.CE, "PE": OptionType.PE}.get(scrip_type.upper(), OptionType.FUT)
