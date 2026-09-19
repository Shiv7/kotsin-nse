# 5paisa — verified facts

Everything here was read off the running NSE stack or the `py5paisa` client on 2026-09-20, not
inferred. Where something is unverified it says so.

## Hosts

| Purpose | URL |
|---|---|
| REST | `https://Openapi.5paisa.com/VendorsAPI/Service1.svc/` |
| Historical OHLCV | `https://openapi.5paisa.com/V2/historical/` |
| Market-data WebSocket | `wss://openfeed.5paisa.com/Feeds/api/chat?Value1={jwt}|{clientCode}` |
| Scrip master | `https://openapi.5paisa.com/VendorsAPI/Service1.svc/ScripMaster/segment/{segment}` |

## Authentication

```
POST TOTPLogin      {head:{Key}, body:{Email_ID, TOTP, PIN, PublicIP, LocalIP}} → body.RequestToken
POST GetAccessToken {head:{Key}, body:{RequestToken, EncryKey, UserId, PublicIP, LocalIP}} → body.AccessToken
```

* The access token is a **JWT**; its `exp` claim is the real expiry. Refresh 30 minutes early.
* The TOTP code is **single-use within its 30-second window**. A second login inside the same
  window is rejected. One process needs no distributed lock — it needs to remember the window.
* `PublicIP` must be the box's real outbound address or the broker's RMS rejects orders. Auto-detect
  it once and log it; never fall back to `127.0.0.1`.
* Two response envelopes must **both** be checked: `head.status` (transport) and `body.Status`
  (RMS). An RMS rejection otherwise reads as a successful placement.

## Historical candles

```
GET V2/historical/{Exch}/{ExchType}/{ScripCode}/{interval}?from=YYYY-MM-DD&end=YYYY-MM-DD
headers: Ocp-Apim-Subscription-Key: <vendor APIM key>, Authorization: Bearer <access token>
→ {"data": {"candles": [[Datetime, O, H, L, C, V], …]}}
```

Intervals: `1m 3m 5m 10m 15m 30m 60m 1d`. Timestamps are **naive IST**. The APIM subscription key
ships inside `py5paisa`; it identifies the API product, not the user, and the host rejects the
request without it.

## Market-data WebSocket

The control frame is byte-pedantic. A malformed frame is **not** rejected — the broker simply never
sends data for that scrip, which presents downstream as "an instrument that never ticks" and is
close to impossible to attribute.

```json
{"ClientCode":"50000001","MarketFeedData":[{"ExchType":"C","Exch":"N","ScripCode":"1660"}],
 "Method":"MarketFeedV3","Operation":"Subscribe"}
```

Outer key order `ClientCode, MarketFeedData, Method, Operation`; entry order `ExchType, Exch,
ScripCode`. That ordering is what the old `json-simple` implementation happened to emit (a
`JSONObject` is a `HashMap`, so bucket order) and what the broker has accepted for two years.

| `Method` | Channel |
|---|---|
| `MarketFeedV3` | ticks: `LastRate`, `LastQty`, `TotalQty`, `High`, `Low`, `OpenRate`, `PClose`, `BidRate`, `OffRate`, `TickDt` |
| `MarketDepthService` | 20-level book |
| `GetScripInfoForFuture` | `OpenInterest`, `OIChange`, `OIChangePercent` |
| `Indices` | index feed |

* `TickDt` is `/Date(1758271500000)/`, epoch **milliseconds**.
* `TotalQty` is the cumulative day volume and is monotonic within a session. Bar volume is its
  **delta**; summing `LastQty` loses any dropped tick permanently.
* **Cash equity has no open interest.** OI exists on futures and options only. Reading OI off the
  cash segment pinned 15% of one strategy's conviction score at exactly zero for its entire life.

## Orders

| Route | Purpose |
|---|---|
| `V1/PlaceOrderRequest` | place. `Price: 0` (or omitted) is a market order. `RemoteOrderID` is echoed back — that is what makes reconciliation after a crash possible |
| `V1/ModifyOrderRequest` · `V1/CancelOrderRequest` | modify / cancel by `ExchOrderID` |
| `V2/OrderStatus` | `{ClientCode, OrdStatusReqList:[{Exch, RemoteOrderID}]}` |
| `V2/NetPositionNetWise` | `NetPositionDetail[]` with `ScripCode`, `NetQty`, `BuyAvgRate`, `SellAvgRate`, `MTM` |
| `V4/Margin` · `V4/OrderBook` | account state |
| `SquareOffAll` | bulk flatten — the kill switch's last resort |

A 200 from `PlaceOrderRequest` means *accepted*, not *filled*. Poll `OrderStatus` until terminal.

## Scrip master

CSV per segment (`nse_eq`, `nse_fo`, `mcx_fo`, …) with columns `Exch, ExchType, Scripcode, Name,
Expiry, ScripType, StrikeRate, FullName, TickSize, LotSize, QtyLimit, Multiplier, SymbolRoot,
ISIN, Series`.

* `ScripType` is `CE` / `PE` for options and `XX` for futures.
* **`Multiplier` is load-bearing on MCX.** ALUMINI is quoted per kg on a 1,000 kg contract, so
  `price × qty` is not the notional: a 286-quantity entry logged ₹99,943 against a real ₹99.9
  million. Sizing that ignores it is wrong by exactly that factor.
* `LotSize` is 1 for cash and the contract lot for everything else.
* Refresh daily: new weekly expiries appear and expired contracts disappear overnight. A stored
  future scrip code goes stale every month — resolve the front month from today's master, never
  from a value saved at calibration time.

## Segments and wire identity

| Segment | `Exch` | `ExchType` |
|---|---|---|
| NSE cash | `N` | `C` |
| NSE F&O | `N` | `D` |
| MCX | `M` | `D` |
| Currency | `N` | `U` (unverified — no currency book here) |

Index instruments carry scrip codes beginning `999920`.

## Sessions

| Segment | Open | Close | Notes |
|---|---|---|---|
| NSE cash / F&O | 09:15 | 15:30 | 30m boundaries at :15 and :45 |
| MCX | 09:00 | 23:30 winter / 23:55 summer | 23:30 is the conservative assumption here |

The engine anchors its bar grid on the session open, so the opening minute is **09:15 and it is
included**. The old `tick_candles_1m` collection started at 09:16 and lost the minute that often
holds the day's extreme.

## Holidays

There is no exchange-holiday endpoint in this API. `data/holidays.txt` holds one `YYYY-MM-DD` per
line and the engine warns at boot when it is empty. Never guess one: a wrong entry skips a real
trading day, and a missing one produces a session of empty bars that looks like a feed outage.
