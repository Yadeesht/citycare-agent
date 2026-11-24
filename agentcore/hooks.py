"""Hook interfaces. Hooks are where a host enforces its rules in code.

A before-tool hook sees the call the model wants to make and can allow it, change its
input, or deny it with a reason that is sent back to the model as a tool error.
Hooks may be plain functions or coroutines. The runner (added with the loop) must treat
a hook that raises as a denial, so a broken check never lets a call through.
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union

from .context import RunContext
from .tools import Tool
from .types import ModelResponse, ToolCall, ToolResult


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str                      # shown to the model
    code: str = "denied"
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class Rewrite:
    input: dict[str, Any]            # replaces the tool input


Decision = Union[Allow, Deny, Rewrite]

BeforeToolHook = Callable[[RunContext, Tool, ToolCall], Union[Decision, None, Awaitable[Union[Decision, None]]]]
AfterToolHook = Callable[[RunContext, Tool, ToolCall, ToolResult], Union[ToolResult, None, Awaitable[Union[ToolResult, None]]]]
BeforeModelHook = Callable[[RunContext, list[dict[str, Any]]], Union[list[dict[str, Any]], None, Awaitable[Any]]]
AfterModelHook = Callable[[RunContext, ModelResponse], Union[None, Awaitable[None]]]


@dataclass
class Hooks:
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)
    before_model: list[BeforeModelHook] = field(default_factory=list)
    after_model: list[AfterModelHook] = field(default_factory=list)


    async def run_before_tool(self, ctx: RunContext, tool: Tool, call: ToolCall) -> tuple[Decision, ToolCall]:
        """First Deny wins; Rewrites chain. A hook that raises denies the call (fail closed)."""
        for hook in self.before_tool:
            try:
                decision = await _call(hook, ctx, tool, call)
            except Exception as exc:  # noqa: BLE001
                return Deny(f"A safety check failed to run ({type(exc).__name__}); action not taken.", "hook_error"), call
            if isinstance(decision, Deny):
                return decision, call
            if isinstance(decision, Rewrite):
                call = dataclasses.replace(call, input=decision.input)
        return Allow(), call

    async def run_after_tool(self, ctx: RunContext, tool: Tool, call: ToolCall, result: ToolResult) -> ToolResult:
        for hook in self.after_tool:
            try:
                changed = await _call(hook, ctx, tool, call, result)
            except Exception as exc:  # noqa: BLE001
                ctx.trace("error", f"after_tool:{tool.name}", error=repr(exc))
                continue
            if isinstance(changed, ToolResult):
                result = changed
        return result

    async def run_before_model(self, ctx: RunContext, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for hook in self.before_model:
            changed = await _call(hook, ctx, messages)
            if changed is not None:
                messages = changed
        return messages

    async def run_after_model(self, ctx: RunContext, response: ModelResponse) -> None:
        for hook in self.after_model:
            try:
                await _call(hook, ctx, response)
            except Exception as exc:  # noqa: BLE001
                ctx.trace("error", "after_model", error=repr(exc))


async def _call(hook: Any, *args: Any) -> Any:
    out = hook(*args)
    if inspect.isawaitable(out):
        out = await out
    return out
