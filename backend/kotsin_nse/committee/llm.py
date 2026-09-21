"""LLM access for the committee: one method, structured output, usage accounting. ``FakeLLM``
lets the whole pipeline run in tests without a network. Same shape as kotsin-crypto's."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import structlog
from pydantic import BaseModel

log = structlog.get_logger("committee.llm")
T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output) — Anthropic list prices, 2026-06.
PRICES_PER_M: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


class CommitteeLLMError(RuntimeError):
    pass


class LLM(Protocol):
    model: str

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        effort: str = "medium",
        max_tokens: int = 4000,
    ) -> T: ...

    def stats(self) -> dict[str, Any]: ...


class AnthropicLLM:
    def __init__(self, api_key: str, model: str = "claude-opus-5") -> None:
        import anthropic  # imported here so the engine boots without the SDK when no key is set

        self._anthropic = anthropic
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.calls = 0
        self.errors = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.last_call_ts: float | None = None
        self.last_error = ""

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        effort: str = "medium",
        max_tokens: int = 4000,
    ) -> T:
        a = self._anthropic
        try:
            resp = await self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": effort},
            )
        except a.RateLimitError as exc:
            self.errors += 1
            self.last_error = f"rate limited: {exc.message}"
            raise CommitteeLLMError(self.last_error) from exc
        except a.APIStatusError as exc:
            self.errors += 1
            self.last_error = f"api {exc.status_code}: {exc.message}"
            raise CommitteeLLMError(self.last_error) from exc
        except a.APIConnectionError as exc:
            self.errors += 1
            self.last_error = f"connection: {exc}"
            raise CommitteeLLMError(self.last_error) from exc
        self.calls += 1
        self.last_call_ts = time.time()
        u = resp.usage
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        if resp.stop_reason == "refusal":
            self.errors += 1
            self.last_error = "refusal"
            raise CommitteeLLMError("the model declined this request")
        parsed = resp.parsed_output
        if parsed is None:
            self.errors += 1
            self.last_error = f"unparsed ({resp.stop_reason})"
            raise CommitteeLLMError(f"no structured output (stop_reason={resp.stop_reason})")
        return parsed

    def stats(self) -> dict[str, Any]:
        pin, pout = PRICES_PER_M.get(self.model, (5.0, 25.0))
        return {
            "model": self.model,
            "calls": self.calls,
            "errors": self.errors,
            "last_error": self.last_error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "est_cost_usd": round(
                self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout, 4
            ),
            "last_call_ts": self.last_call_ts,
        }


class FakeLLM:
    """Returns canned instances per schema; records every call."""

    def __init__(
        self,
        responses: dict[type[BaseModel], Callable[[str, str], BaseModel] | BaseModel],
        model: str = "fake",
    ) -> None:
        self.model = model
        self._responses = responses
        self.calls: list[tuple[str, str, str]] = []

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        effort: str = "medium",
        max_tokens: int = 4000,
    ) -> T:
        self.calls.append((schema.__name__, system, user))
        r = self._responses[schema]
        out = r(system, user) if callable(r) else r
        return out  # type: ignore[return-value]

    def stats(self) -> dict[str, Any]:
        return {"model": self.model, "calls": len(self.calls), "errors": 0, "est_cost_usd": 0.0}


__all__ = ["LLM", "PRICES_PER_M", "AnthropicLLM", "CommitteeLLMError", "FakeLLM"]
