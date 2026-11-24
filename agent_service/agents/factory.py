"""Builds the workers: booking, billing, info (models on agentcore), human and safety (no model)."""

from __future__ import annotations

from agentcore import Agent, LLM, Tool

from .. import config
from ..emergency import SafetyWorker
from ..handoff import HumanWorker
from ..hospital_api import HospitalAPI
from ..hospital_hooks import build_hooks
from ..hospital_tools import build_tools
from . import prompts

BOOKING_TOOLS = {"current_time", "find_doctors", "free_slots", "list_appointments", "book", "reschedule", "cancel"}
BILLING_TOOLS = {"current_time", "list_bills", "payment_link"}


def _subset(tools: list[Tool], names: set[str]) -> list[Tool]:
    return [t for t in tools if t.name in names]


def build_workers(api: HospitalAPI, llm: LLM) -> dict[str, object]:
    tools = build_tools(api)
    hooks = build_hooks(api)
    common = dict(
        model=config.AGENT_MODEL, llm=llm, pricing=config.PRICING, max_iterations=config.AGENT_MAX_ITERATIONS,
        max_tokens=config.AGENT_MAX_TOKENS, budget_usd=config.AGENT_BUDGET_USD,
        request_options=config.effort_options(config.AGENT_EFFORT),
    )
    return {
        "booking": Agent(name="booking", system=prompts.booking_prompt, tools=_subset(tools, BOOKING_TOOLS), hooks=hooks, **common),
        "billing": Agent(name="billing", system=prompts.billing_prompt, tools=_subset(tools, BILLING_TOOLS), hooks=hooks, **common),
        "info": Agent(name="info", system=prompts.info_prompt(prompts.read_facts()), tools=[], **common),
        "human": HumanWorker(),
        "safety": SafetyWorker(),
    }
