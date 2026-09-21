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

* The access token is a **JWT** and its `exp` is **23:59:59 IST, fixed, whatever time it was
  minted** — decoded live 2026-09-21: a 23:40:40 login had `exp` 23:59:59 (19 minutes). So there is
  no "refresh early": a login at 23:45 returns a token with the same expiry. Use a token until it
  actually expires; re-login after midnight; and **reconnect the WebSocket** after midnight, because
  the socket URL carries the JWT and a socket left open on a dead token looks connected and
  delivers nothing at the open (`Engine._housekeeping` does both).
* The TOTP code is **single-use within its 30-second window**. A second login inside the same
  window is rejected. One process needs no distributed lock — it needs to remember the window.
* **Do not send a code in the first seconds of its window.** Measured 2026-09-22: the post-midnight
  re-login fired at 00:01:00.2 IST, 0.2 s into a fresh window, and `TOTPLogin` answered Status 0
  with **no `RequestToken`**; the retry at 00:01:02, same window, succeeded. `Authenticator` now
  waits 3 s past a window edge and never sends in a window's last 2 s.
* **Midnight rollover, measured 2026-09-22.** `exp` passed at 23:59:59; the engine asked for a
  reconnect at 00:00:59, re-logged at 00:01:02 with a token good until 23:59:59 the next day
  (`expires_in_h=23.98`), and the socket came back with every subscription re-sent
  (`mf` 2,962 / `md` 2,502 / `oi` 2,748). Health stayed `ok`; `reconnects=1`.
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

**It is a snapshot feed, not a tape.** Measured on the first live MCX session (2026-09-21): ~6.5
`MarketFeedV3` frames per minute per symbol, each a snapshot of `LastRate` and the cumulative
`TotalQty` at send time. Trades between two frames are invisible. Consequences, measured:

| | |
|---|---|
| 1m bars built live vs the exchange's own candle | **122 / 139 exact (87.8%)** across 11 symbols |
| every miss | a boundary attribution — a print between frames near a minute edge lands in the adjacent bucket (COPPER 23:14 close 1412.75 vs 1412.45, the difference reappearing in 23:15's open) |
| volume | never lost (`TotalQty` is cumulative), only shifted a bucket |
| depth (`MarketDepthService`) | 20 levels, ~2–5 Hz on active names; ~4,900 frames in 9 min for 11 symbols |
| after the close | frames keep arriving (12 phantom forming bars observed 150 s after MCX 23:30) — the bar builder must ignore ticks outside the session, and the same guard keeps NSE's 09:00–09:15 pre-open prints out of the 09:15 bar |

**So a forming candle cannot be made tick-perfect from this feed**, and the closed bars are made
exact a different way: the historical endpoint returns the exchange's own OHLCV per bucket **and
serves a bucket while it is still forming**, so a just-closed bucket is final within seconds.
`bars/verify.py` fetches it and installs it over the live build — the 30m decision waits for it
(bounded) — and reports the running fidelity on the System page.

There is **no trade tape and no aggressor side**, so Kyle's λ and VPIN are not computable on this
venue. `bars/micro.py` computes what the book does support (L1 OFI, depth imbalance, microprice,
spread) and says so.

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

## The F&O universe (from the master, the way scripFinder did it)

Measured 2026-09-21: `nse_fo` carries **80,883** instruments — 647 futures and 80,236 options —
over **216 roots**; **214** join to a cash equity by `SymbolRoot`, the other two (`NIFTYFPI`,
`NIFTYNXT50`) are indices with no cash leg. Stock options are monthly (RELIANCE: 536 strikes over
3 expiries); index options are weekly. `instrument/universe.py` derives the universe from the
roots, joins the equity, keeps front + next future, and shortlists strikes within ±12% of the
previous close, 5 per side, nearest tradeable expiry — the shortlist the socket subscribes to.

## Scrip master

CSV per segment (`nse_eq`, `nse_fo`, `mcx_fo`, …) with columns `Exch, ExchType, Scripcode, Name,
Expiry, ScripType, StrikeRate, FullName, TickSize, LotSize, QtyLimit, Multiplier, SymbolRoot,
ISIN, Series`.

* `ScripType` is `CE` / `PE` for options and `XX` for futures.
* **`Multiplier` is load-bearing on MCX.** ALUMINI is quoted per kg on a 1,000 kg contract, so
  `price × qty` is not the notional: a 286-quantity entry logged ₹99,943 against a real ₹99.9
  million. Sizing that ignores it is wrong by exactly that factor.
* `LotSize` is 1 for cash and the contract lot for everything else.
* **Unavailable overnight.** `ScripMaster/segment/{seg}` answers **404** with the body
  `Cache not available for segment 'nse_eq'. Retry after some time.` — measured 2026-09-22 from
  00:00 to at least 00:03 IST for all three segments, on the same URL that served them at 23:50.
  The loader falls back to the newest cached day (`catalogue.using_stale_cache`) and the 09:20
  rebuild fetches again with `force=True`.
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
