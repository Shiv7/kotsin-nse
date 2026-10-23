"""The Analysis page's written summary: the stock's bundle (``analysis/bundle.py``) sent to Claude,
which reads it the way the page is laid out and answers in a fixed set of short sections.

* Model ``claude-opus-5-5`` with adaptive thinking at ``medium`` effort, streamed (the bundle is
  ~10–20k tokens) and collected with ``get_final_message()``.
* Server-side fallbacks on (``fallbacks="default"``, beta ``server-side-fallback-2026-07-01``):
  if a safety classifier declines, the API reruns on a fallback model inside the same call.
* The system prompt is fixed and cached; only the data changes per call.
* **No call is ever made on its own.** A summary is made only when the owner presses "generate" (the
  route's ``refresh``); a page view, a poll or a reload only reads the last one, however old. At most
  ``DAILY_CAP`` paid calls per day (IST), whatever asks. (2026-10-23: an Analysis tab left open re-read
  the summary every 10 minutes against a 10-minute cache — 20 Opus calls for BEL, about $6, unasked.)

The key comes from ``KN_ANTHROPIC_API_KEY`` in ``backend/.env`` (or ``ANTHROPIC_API_KEY`` in the
environment). Without one the endpoint says so instead of failing.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic
import structlog

log = structlog.get_logger(__name__)

MODEL = "claude-opus-5-5"
DAILY_CAP = 20
MAX_TOKENS = 64000

SYSTEM = """You are a senior Indian equity-derivatives analyst writing for a discretionary trader who
trades NSE stock futures and options. You receive one JSON document describing a single stock,
assembled by the trader's own engine from 5paisa's feed and NSE's daily F&O files. Read all of it.

Write in this order, with these exact headings, plain English, short sentences, numbers from the
data (prices to two decimals, percentages to one or two):

## Verdict
One or two lines: the bias (bullish / bearish / range), its strength, and the single condition
that would prove it wrong.

## Big picture
Daily trend, where price sits in its 1-month / 3-month / 1-year ranges, the moving averages,
volatility, and the Fibonacci retracement of the last major swing (how much is already retraced).

## Positioning
What NSE's futures OI and the option walls say over the last sessions (build-up labels, PCR trend,
the call and put walls for the nearest expiry), and the build-up across timeframes.

## Levels
The pivot zones that matter (daily / weekly / monthly), the market-profile value area and POC, any
smart-money-concept structure (order block, break of structure, sweeps), and where they agree.

## Today
How price reacted to those levels today, what the order flow (estimated buy/sell, OFI) and the
microstructure (queue imbalance, book skew, walls pulled/filled, absorption, vacuums, VPIN, Kyle's
lambda) say, and what the options say (64-state reading, IV, straddle, PCR).

## Option-selling plan
Assess the two hedged setups in `hedge` (credit, max loss, breakevens, probability, theta): which
fits the evidence better and why, the adjustment or exit trigger for each, and position sizing in
lots against max loss. Never recommend a naked short option.

## Risks and data caveats
What could invalidate the read (events, expiry pinning, gap risk) and the data's limits stated in
it (estimated order-flow sides, 5-level book, recording gaps, missing fields).

Rules: use only the data given — do not invent news, earnings dates or prices. If a field is null
or missing, say it is unavailable rather than guessing. This is analysis for an experienced trader,
not financial advice; keep that to one short line at the end. No tables wider than five columns."""


class BudgetExceeded(RuntimeError):
    """The day's cap of paid summary calls is used up."""


class Summarizer:
    def __init__(self, api_key: str | None) -> None:
        # An explicit key from settings, else the SDK's own resolution (ANTHROPIC_API_KEY, profiles)
        self._client: anthropic.AsyncAnthropic | None
        try:
            self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        except anthropic.AnthropicError:
            self._client = None
        self._configured = self._client is not None and bool(api_key or self._client.api_key or self._client.auth_token)
        self._cache: dict[str, dict[str, Any]] = {}
        self._day = ""
        self._calls = 0

    @property
    def configured(self) -> bool:
        return self._configured

    def cached(self, symbol: str) -> dict[str, Any] | None:
        """The last summary made for ``symbol`` — reused until the owner asks for a new one."""
        return self._cache.get(symbol)

    def calls_left(self) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 19_800))
        if day != self._day:
            self._day, self._calls = day, 0
        return DAILY_CAP - self._calls

    async def summarize(self, symbol: str, bundle: dict[str, Any], *, refresh: bool = False) -> dict[str, Any]:
        """A paid call — only when the owner pressed generate (``refresh``) or no summary exists and
        the route was asked to make one. Raises ``BudgetExceeded`` past ``DAILY_CAP`` calls today."""
        if not refresh and (hit := self.cached(symbol)):
            return {**hit, "cached": True}
        if self._client is None:
            raise RuntimeError("no Anthropic credentials")
        if self.calls_left() <= 0:
            raise BudgetExceeded(f"{DAILY_CAP} summaries already made today — the cap stops further paid calls until tomorrow")
        self._calls += 1
        data = json.dumps(bundle, default=float, separators=(",", ":"), sort_keys=True)
        t0 = time.time()
        async with self._client.beta.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"Stock data for {symbol} (JSON):\n{data}"}],
        ) as stream:
            msg = await stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise RuntimeError("the model declined to analyse this data")
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        out = {
            "symbol": symbol, "text": text, "model": msg.model, "created_ts": time.time(), "seconds": round(time.time() - t0, 1),
            "stop_reason": msg.stop_reason,
            "usage": {"input": msg.usage.input_tokens, "output": msg.usage.output_tokens,
                      "cache_read": getattr(msg.usage, "cache_read_input_tokens", 0) or 0},
            "request_id": getattr(msg, "_request_id", None),
        }
        log.info("analysis.summary", symbol=symbol, model=msg.model, seconds=out["seconds"], **out["usage"])
        self._cache[symbol] = out
        return {**out, "cached": False}


__all__ = ["DAILY_CAP", "MODEL", "BudgetExceeded", "Summarizer"]
