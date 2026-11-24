"""The agent loop: model -> tool calls -> hooks -> tools -> model, until the model answers.

An Agent has no built-in name, prompt or rules. The host passes them in, and enforces its
rules through hooks. Nothing here raises for model, tool or hook failures; they come back
as tool errors or as RunResult.error.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Callable, Iterable

from .context import RunContext
from .cost import Pricing
from .hooks import Deny, Hooks
from .llm import LLM, complete
from .results import ApiResult, to_jsonable
from .tools import Tool, ToolRegistry, validate_input
from .types import ModelResponse, RunResult, StopReason, ToolCall, ToolResult, Usage


class Agent:
    def __init__(
        self,
        *,
        name: str,
        system: str | Callable[[RunContext], str],
        model: str,
        llm: LLM,
        tools: ToolRegistry | Iterable[Tool] = (),
        hooks: Hooks | None = None,
        pricing: Pricing | None = None,
        max_iterations: int = 8,
        max_tokens: int = 1024,
        budget_usd: float | None = None,
        request_options: dict[str, Any] | None = None,
    ):
        self.name = name
        self.system = system
        self.model = model
        self.llm = llm
        self.tools = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self.hooks = hooks or Hooks()
        self.pricing = pricing
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.budget_usd = budget_usd
        self.request_options = dict(request_options or {})

    async def run(self, ctx: RunContext, messages: list[dict[str, Any]]) -> RunResult:
        msgs = list(messages)
        usage = Usage()
        cost = 0.0
        tool_results: list[ToolResult] = []
        system = self.system(ctx) if callable(self.system) else self.system
        specs = self.tools.specs()

        def finish(stop: str, text: str = "", iterations: int = 0, error: str | None = None) -> RunResult:
            return RunResult(
                agent=self.name, text=text, messages=msgs, stop_reason=stop, iterations=iterations,
                usage=usage, cost_usd=cost, tool_results=tool_results, error=error, trace_id=ctx.trace_id,
            )

        for iteration in range(1, self.max_iterations + 1):
            if self.budget_usd is not None and cost >= self.budget_usd:
                return finish(StopReason.BUDGET, iterations=iteration - 1)

            msgs = await self.hooks.run_before_model(ctx, msgs)
            try:
                response: ModelResponse = await complete(
                    self.llm, model=self.model, system=system, messages=msgs, tools=specs,
                    max_tokens=self.max_tokens, pricing=self.pricing, ctx=ctx, label=self.name,
                    **self.request_options,
                )
            except Exception as exc:  # noqa: BLE001 - the host decides what an LLM failure means
                ctx.trace("error", self.name, error=repr(exc))
                return finish(StopReason.ERROR, iterations=iteration, error=repr(exc))

            usage = usage + response.usage
            cost += response.cost_usd
            msgs.append({"role": "assistant", "content": response.content})
            await self.hooks.run_after_model(ctx, response)

            stop = response.stop_reason
            if stop == "pause_turn":
                continue
            if stop == "tool_use" and response.tool_calls:
                results = await asyncio.gather(*(self._run_tool(ctx, call) for call in response.tool_calls))
                tool_results.extend(results)
                msgs.append({"role": "user", "content": [r.to_block() for r in results]})
                continue
            if stop == "refusal":
                return finish(StopReason.REFUSAL, response.text, iteration)
            if stop == "max_tokens":
                return finish(StopReason.MAX_TOKENS, response.text, iteration)
            return finish(StopReason.END_TURN, response.text, iteration)

        return finish(StopReason.MAX_ITERATIONS, iterations=self.max_iterations)

    async def _run_tool(self, ctx: RunContext, call: ToolCall) -> ToolResult:
        started = time.perf_counter()

        def result(content: Any, *, is_error: bool = False, denied: bool = False) -> ToolResult:
            duration = time.perf_counter() - started
            ctx.trace("tool", call.name, duration_s=duration, is_error=is_error, denied=denied)
            return ToolResult(call.id, call.name, content, is_error, denied, duration)

        tool = self.tools.get(call.name)
        if tool is None:
            return result({"error": "unknown_tool", "message": f"No tool named {call.name!r}"}, is_error=True)

        problems = validate_input(tool.input_schema, call.input)
        if problems:
            return await self._after(
                ctx, tool, call,
                result({"error": "invalid_input", "message": "; ".join(problems)}, is_error=True),
            )

        decision, call = await self.hooks.run_before_tool(ctx, tool, call)
        if isinstance(decision, Deny):
            payload: dict[str, Any] = {"error": decision.code, "message": decision.reason}
            if decision.data:
                payload.update(decision.data)
            return await self._after(ctx, tool, call, result(payload, is_error=True, denied=True))

        problems = validate_input(tool.input_schema, call.input)  # a rewrite must still be valid
        if problems:
            return await self._after(
                ctx, tool, call,
                result({"error": "invalid_input", "message": "; ".join(problems)}, is_error=True),
            )

        try:
            out = tool.fn(ctx, call.input)
            if inspect.isawaitable(out):
                out = await asyncio.wait_for(out, tool.timeout_s)
        except asyncio.TimeoutError:
            return await self._after(
                ctx, tool, call,
                result({"error": "timeout", "message": f"{tool.name} timed out after {tool.timeout_s}s"}, is_error=True),
            )
        except Exception as exc:  # noqa: BLE001
            return await self._after(
                ctx, tool, call,
                result({"error": "tool_exception", "message": f"{type(exc).__name__}: {exc}"}, is_error=True),
            )

        if isinstance(out, ApiResult):
            res = result(out.data if out.ok else out.error_payload(), is_error=not out.ok)
        else:
            res = result(to_jsonable(out))
        return await self._after(ctx, tool, call, res)

    async def _after(self, ctx: RunContext, tool: Tool, call: ToolCall, res: ToolResult) -> ToolResult:
        return await self.hooks.run_after_tool(ctx, tool, call, res)
