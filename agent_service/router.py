"""The router (LLMRouter on the cheap model) plus one service rule around it.

The router model alone decides the language, whether there is an emergency, and whether the patient wants a
person (no regexes or word lists). It runs on every message. Emergency -> safety worker; router failure -> `unrouted`
(resend / call 108 / front desk, no agent); a chat already handed to staff -> human worker. Everything else is the
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
        if route.meta.get("emergency") is True:   # first: an emergency beats a handoff, a staff chat and any request
            return Route("safety", {**route.meta, "routed_from": route.worker})
        if "router_error" in route.meta:          # the router call failed or gave no usable answer: no agent runs unrouted
            return Route("unrouted", route.meta)
        if ctx.state.get("handed_off"):          # the chat belongs to staff now; the agents stay out of it
            return Route("human", {**route.meta, "routed_from": route.worker})
        return route
