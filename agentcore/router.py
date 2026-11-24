"""LLMRouter: picks a worker with one cheap model call.

The host supplies every word the model sees (system prompt, route descriptions, extra fields).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .context import RunContext
from .cost import Pricing
from .llm import LLM, complete
from .orchestrator import Route, transcript_window


class LLMRouter:
    """
    routes       worker name -> one-line description, shown to the model
    meta_schema  extra JSON-schema properties the model should report alongside its choice
    default      worker used when the model call fails or the answer is unusable
    """

    def __init__(
        self,
        *,
        llm: LLM,
        model: str,
        system: str | Callable[[RunContext], str],
        routes: Mapping[str, str],
        default: str,
        meta_schema: dict[str, Any] | None = None,
        pricing: Pricing | None = None,
        max_tokens: int = 200,
        history_window: int = 6,
        tool_name: str = "route",
        request_options: dict[str, Any] | None = None,
    ):
        if default not in routes:
            raise ValueError("default must be one of the routes")
        self.llm, self.model, self.system = llm, model, system
        self.routes = dict(routes)
        self.default = default
        self.meta_schema = dict(meta_schema or {})
        self.pricing = pricing
        self.max_tokens = max_tokens
        self.history_window = history_window
        self.tool_name = tool_name
        self.request_options = dict(request_options or {})

    def _tool(self) -> dict[str, Any]:
        worker = {
            "type": "string",
            "enum": list(self.routes),
            "description": "; ".join(f"{name}: {desc}" for name, desc in self.routes.items()),
        }
        return {
            "name": self.tool_name,
            "description": "Choose which worker handles the latest message.",
            "input_schema": {
                "type": "object",
                "properties": {"worker": worker, **self.meta_schema},
                "required": ["worker"],
            },
        }

    async def route(self, ctx: RunContext, message: str, history: list[dict[str, Any]]) -> Route:
        system = self.system(ctx) if callable(self.system) else self.system
        messages = transcript_window(history, self.history_window) + [{"role": "user", "content": message}]
        try:
            response = await complete(
                self.llm, model=self.model, system=system, messages=messages, tools=[self._tool()],
                max_tokens=self.max_tokens, pricing=self.pricing, ctx=ctx, label="router",
                **self.request_options,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.trace("error", "router", error=repr(exc))
            return Route(self.default, {"router_error": repr(exc)})

        call = next((c for c in response.tool_calls if c.name == self.tool_name), None)
        if call is None:
            return Route(self.default, {"router_error": "no_route_call"})
        meta = dict(call.input)
        worker = meta.pop("worker", None)
        if worker not in self.routes:
            return Route(self.default, {**meta, "router_error": f"unknown worker {worker!r}"})
        ctx.trace("route", worker, **meta)
        return Route(worker, meta)
