"""Shared test helpers. Nothing here touches the network or an API key."""

from __future__ import annotations

import asyncio
from typing import Any

from agentcore import Agent, Hooks, RunContext, SyncAdapter, Tool
from tests.fake_claude import FakeClaude


def run(coro):
    return asyncio.run(coro)


def make_ctx(**kw: Any) -> RunContext:
    return RunContext(session_id=kw.pop("session_id", "s1"), **kw)


def make_agent(script: list, tools: list[Tool] | None = None, hooks: Hooks | None = None, **kw: Any):
    fake = FakeClaude(script)
    agent = Agent(
        name=kw.pop("name", "worker"),
        system=kw.pop("system", "You are a test agent."),
        model=kw.pop("model", "m"),
        llm=SyncAdapter(fake),
        tools=tools or [],
        hooks=hooks,
        **kw,
    )
    return agent, fake


def tool(name: str, fn, schema: dict | None = None, **kw: Any) -> Tool:
    schema = schema or {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}
    return Tool(name=name, description=f"{name} tool", input_schema=schema, fn=fn, **kw)


USER = [{"role": "user", "content": "hi"}]
