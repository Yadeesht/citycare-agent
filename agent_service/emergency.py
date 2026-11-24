"""The safety reply (context.md rule 4).

Only the router model decides whether a message describes an emergency (it sets `emergency=true`);
there are no regexes or word lists. The orchestrator then sends the turn to this worker, which
answers in code (108, the 24-hour ER) with no model call and raises the emergency + handoff flags.
"""

from __future__ import annotations

from typing import Any

from agentcore import RunContext, RunResult, StopReason

from .config import pick

SAFETY_REPLY = {
    "en": ("This sounds like an emergency. Please call 108 right now or go to the nearest emergency room. "
           "Our Emergency department at City Care Hospital, 12 MG Road, Camp, is open 24 hours. "
           "I've alerted our staff. Please don't wait for an appointment."),
    "hinglish": ("Yeh emergency lag rahi hai. Please abhi 108 par call kijiye ya sabse paas ke emergency room jaaiye. "
                 "City Care Hospital, 12 MG Road, Camp ka Emergency department 24 ghante khula rehta hai. "
                 "Maine hamare staff ko bata diya hai. Appointment ka intezaar mat kijiye."),
    "hi": ("यह आपातकाल लग रहा है। कृपया अभी 108 पर कॉल करें या नज़दीकी इमरजेंसी रूम जाएँ। "
           "सिटी केयर हॉस्पिटल, 12 एमजी रोड, कैंप का इमरजेंसी विभाग 24 घंटे खुला रहता है। "
           "मैंने हमारे स्टाफ को सूचित कर दिया है। कृपया अपॉइंटमेंट का इंतज़ार न करें।"),
}

SAFETY_FLAGS: dict[str, Any] = {"emergency": True, "handoff": True, "handoff_reason": "emergency"}


class SafetyWorker:
    """Used when the router reports an emergency."""

    name = "safety"

    async def run(self, ctx: RunContext, messages: list[dict[str, Any]]) -> RunResult:
        ctx.flags.update(SAFETY_FLAGS)
        ctx.state["handed_off"] = True
        text = pick(SAFETY_REPLY, ctx.state.get("language"))
        return RunResult(self.name, text, list(messages), StopReason.END_TURN, 0)
