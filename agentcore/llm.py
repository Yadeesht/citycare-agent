"""The model interface. Anything with `async create(**kwargs)` returning an SDK-shaped message works."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from .context import RunContext
from .cost import Pricing
from .types import ModelResponse, ToolCall, Usage


class LLM(Protocol):
    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        **extra: Any,
    ) -> Any: ...


class AnthropicLLM:
    """Real client. Marks the system prompt and the tool list as cacheable."""

    def __init__(self, client: Any = None, *, cache: bool = True, timeout: float = 60.0, max_retries: int = 2):
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(timeout=timeout, max_retries=max_retries)
        self.client = client
        self.cache = cache

    async def create(self, *, model, system, messages, tools, max_tokens, **extra):
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages, **extra}
        if system:
            block: dict[str, Any] = {"type": "text", "text": system}
            if self.cache:
                block["cache_control"] = {"type": "ephemeral"}
            kwargs["system"] = [block]
        if tools:
            tools = [dict(t) for t in tools]
            if self.cache:
                tools[-1]["cache_control"] = {"type": "ephemeral"}
            kwargs["tools"] = tools
        return await self.client.messages.create(**kwargs)


class SyncAdapter:
    """Lets a client with a synchronous `messages.create` (such as tests/fake_claude.FakeClaude) act as an LLM."""

    def __init__(self, client: Any):
        self.client = client

    async def create(self, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.client.messages.create, **kwargs)


def _block_to_dict(block: Any) -> dict[str, Any]:
    """SDK block -> plain dict that can be sent back as history (keeps thinking signatures)."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(vars(block))


async def complete(
    llm: LLM,
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 1024,
    pricing: Pricing | None = None,
    ctx: RunContext | None = None,
    label: str = "",
    **extra: Any,
) -> ModelResponse:
    """One model call. Returns a normalised ModelResponse and records a trace event on `ctx`."""
    started = time.perf_counter()
    raw = await llm.create(
        model=model, system=system, messages=messages, tools=tools or [], max_tokens=max_tokens, **extra
    )
    duration = time.perf_counter() - started

    content = [_block_to_dict(b) for b in (getattr(raw, "content", None) or [])]
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    calls = [
        ToolCall(b["id"], b["name"], dict(b.get("input") or {})) for b in content if b.get("type") == "tool_use"
    ]
    usage = Usage.from_response(getattr(raw, "usage", None))
    cost = pricing.cost(model, usage) if pricing else 0.0
    stop_reason = getattr(raw, "stop_reason", None)

    if ctx is not None:
        ctx.trace(
            "model", label or model, duration_s=duration, usage=usage, cost_usd=cost,
            model=model, stop_reason=stop_reason, tool_calls=[c.name for c in calls],
        )
    return ModelResponse(model, content, text, calls, stop_reason, usage, cost, duration)
