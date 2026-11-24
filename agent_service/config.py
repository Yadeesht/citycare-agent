"""Models, prices and limits. Everything here can be overridden from the environment."""

from __future__ import annotations

import os
from pathlib import Path

from agentcore import ModelPrice, Pricing

ROOT = Path(__file__).resolve().parent.parent
HOSPITAL_INFO_PATH = ROOT / "hospital_info.md"

ROUTER_MODEL = os.getenv("ROUTER_MODEL", "claude-haiku-5-5")
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-haiku-5-5")      # try claude-sonnet-5-5 only if Haiku measures too weak

# Haiku 5.5 / Sonnet 5.5 think by default. Effort is the cost lever; the router needs almost none.
ROUTER_EFFORT = os.getenv("ROUTER_EFFORT", "low")
AGENT_EFFORT = os.getenv("AGENT_EFFORT", "medium")

ROUTER_MAX_TOKENS = 400
AGENT_MAX_TOKENS = 1500           # includes thinking tokens
AGENT_MAX_ITERATIONS = 8
AGENT_BUDGET_USD = 0.05           # hard stop for one agent run

PRICING = Pricing({
    "claude-haiku-5-5": ModelPrice(0.10, 0.50),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0, cache_read_mult=0.05),
    "claude-opus-5-5": ModelPrice(4.0, 20.0, cache_read_mult=0.05),
})

FRONT_DESK_PHONE = "020-4000-1234"   # from hospital_info.md


def effort_options(effort: str) -> dict:
    return {"output_config": {"effort": effort}} if effort else {}


# The router model reports the patient's language (en, hi, hinglish); canned replies are picked by it.
LANGUAGES = ("en", "hi", "hinglish")


def pick(replies: dict[str, str], language: str | None) -> str:
    return replies.get(language or "en") or replies["en"]
