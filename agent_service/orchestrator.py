"""Wires gates, router and workers into agentcore's Orchestrator and maps the result to /chat.

    message -> HandoffGate -> ClockGate -> router -> booking | billing | info | human | safety
"""

from __future__ import annotations

import logging
from typing import Any

from agentcore import InMemorySessionStore, LLM, Orchestrator, Outcome, RunContext, StopReason, Tracer, to_jsonable

from .agents.factory import build_workers
from .config import pick
from .handoff import HandoffGate, TROUBLE_REPLY, handoff_flags
from .hospital_api import HospitalAPI
from .router import ServiceRouter, build_llm_router
from .schemas import ChatRequest, ChatResponse

log = logging.getLogger("citycare")


class ClockGate:
    """Not a real gate: it never answers. It puts the hospital's clock in the session state each turn."""

    def __init__(self, api: HospitalAPI):
        self.api = api

    async def check(self, ctx: RunContext, message: str, history: list[dict[str, Any]]):
        res = await self.api.now()
        if res.ok:
            ctx.state["now"] = {"now": res.data.now, "weekday": res.data.weekday}
        return None


class CityCareService:
    def __init__(self, api: HospitalAPI, llm: LLM, *, tracer: Tracer | None = None):
        self.api = api
        self.tracer = tracer or Tracer()
        self.core = Orchestrator(
            router=ServiceRouter(build_llm_router(llm)),
            workers=build_workers(api, llm),
            gates=[HandoffGate(), ClockGate(api)],
            store=InMemorySessionStore(),
            tracer=self.tracer,
            default_worker="info",
        )

    async def handle(self, req: ChatRequest) -> ChatResponse:
        try:
            outcome = await self.core.handle(req.session_id, {"phone": req.phone}, req.message)
        except Exception:  # noqa: BLE001 - /chat must never 500 on a patient
            log.exception("chat failed for session %s", req.session_id)
            self.core.store.get(req.session_id).state["handed_off"] = True
            return ChatResponse(
                reply=pick(TROUBLE_REPLY, self.core.store.get(req.session_id).state.get("language")), agent="human", handoff=True,
                trace_id=self.tracer.begin(req.session_id, "failed turn"),
            )
        return self._respond(req, outcome)

    def _respond(self, req: ChatRequest, outcome: Outcome) -> ChatResponse:
        session = self.core.store.get(req.session_id)
        flags = dict(outcome.flags)

        text = outcome.text.strip()
        run = outcome.run
        if run is not None and (run.stop_reason not in (StopReason.END_TURN, StopReason.MAX_TOKENS) or not text):
            # the agent did not finish (error, budget, loop limit, refusal): never leave the patient hanging
            log.warning("agent %s ended with %s: %s", outcome.agent, run.stop_reason, run.error)
            text = pick(TROUBLE_REPLY, session.state.get("language"))
            flags.update(handoff_flags("agent_failed"))
        if flags.get("handoff"):
            session.state["handed_off"] = True   # the chat now belongs to staff

        return ChatResponse(
            reply=text, agent=outcome.agent if outcome.agent in ("booking", "billing", "info", "human", "safety") else "info",
            handoff=bool(flags.get("handoff")), emergency=bool(flags.get("emergency")), trace_id=outcome.trace_id or "",
        )

    def trace(self, session_id: str) -> dict[str, Any]:
        traces = self.tracer.for_session(session_id)
        return {
            "session_id": session_id,
            "total_cost_usd": round(sum(t.cost_usd for t in traces), 6),
            "traces": [
                {
                    "trace_id": t.trace_id, "label": t.label, "started_at": t.started_at,
                    "cost_usd": round(t.cost_usd, 6), "usage": to_jsonable(t.usage),
                    "events": [
                        {"kind": e.kind, "name": e.name, "started_at": e.started_at, "duration_s": round(e.duration_s, 4),
                         "usage": to_jsonable(e.usage) if e.usage else None, "cost_usd": round(e.cost_usd, 6),
                         "data": to_jsonable(e.data)}
                        for e in t.events
                    ],
                }
                for t in traces
            ],
        }
