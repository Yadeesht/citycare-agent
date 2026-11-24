"""The router (LLMRouter on the cheap model) plus one service rule around it.

The router model alone decides the language and whether there is an emergency (no regexes or
word lists). If it reports `emergency`, the message goes to the safety worker. Everything else is the
router's call: it sees the recent transcript, so a bare "yes" after a read-back goes back to the
agent that asked.
"""

from __future__ import annotations

from typing import Any

from agentcore import LLM, LLMRouter, Route, RunContext

from . import config
from .agents import prompts
from .config import LANGUAGES

ROUTES = {
    "booking": "appointments, doctors, slots",
    "billing": "bills and payments",
    "info": "general hospital questions and anything else",
    "human": "asks for a person or is upset",
}
META_SCHEMA = {
    "language": {"type": "string", "enum": list(LANGUAGES)},
    "emergency": {"type": "boolean", "description": "true if an emergency is described"},
}


def build_llm_router(llm: LLM) -> LLMRouter:
    return LLMRouter(
        llm=llm, model=config.ROUTER_MODEL, system=prompts.ROUTER, routes=ROUTES, default="info",
        meta_schema=META_SCHEMA, pricing=config.PRICING, max_tokens=config.ROUTER_MAX_TOKENS,
        request_options=config.effort_options(config.ROUTER_EFFORT),
    )


class ServiceRouter:
    def __init__(self, inner: Any):
        self.inner = inner

    async def route(self, ctx: RunContext, message: str, history: list[dict[str, Any]]) -> Route:
        route = await self.inner.route(ctx, message, history)
        language = route.meta.get("language")
        if language in LANGUAGES:
            ctx.state["language"] = language
        if route.meta.get("emergency") is True:
            return Route("safety", {**route.meta, "routed_from": route.worker})
        return route
