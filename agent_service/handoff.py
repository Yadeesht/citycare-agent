"""Human handoff (context.md rule 8): the patient asks for a person, is upset, or the agent keeps failing.

- `wants_human` is the code floor for an explicit request; the router catches upset patients.
- The reply language is the one the router last reported for this session (English until then).
- Other code (hooks) raises the `handoff` flag; once raised the chat is staff's, so later
  messages get a short canned reply instead of the agent.
"""

from __future__ import annotations

import re
from typing import Any

from agentcore import Intercept, RunContext, RunResult, StopReason

from .config import FRONT_DESK_PHONE, pick

_ASK = re.compile(
    r"\b(?:speak|talk|chat|connect|transfer|put|call)\w*\s+(?:me\s+)?(?:through\s+)?(?:to|with)\s+(?:a\s+|an\s+|the\s+|your\s+|some\s+)?"
    r"(?:real\s+|live\s+|human\s+)?(?:human|person|people|staff|receptionist|representative|manager|operator|agent|someone|somebody|executive|front\s+desk)\b"
    r"|\b(?:real|live)\s+(?:person|human)\b|\bhuman\s+(?:agent|being)\b|\bi\s+want\s+(?:a\s+)?(?:human|person|staff)\b"
    r"|\b(?:insaan|insan|kisi\s+(?:insaan|insan|person|staff)|staff|manager|receptionist)\s+(?:se|ko)\s+(?:\w+\s+){0,2}(?:baat|bat|baatcheet|connect)\b"
    r"|(?:इंसान|स्टाफ|मैनेजर|रिसेप्शनिस्ट|किसी\s*व्यक्ति)\s*से\s*(?:\S+\s+){0,2}बात",
    re.IGNORECASE,
)


def wants_human(message: str) -> bool:
    return bool(_ASK.search(message.replace("’", "'")))


HANDOFF_REPLY = {
    "en": f"Of course. I'm passing this chat to our staff and they will help you shortly. For anything urgent, call the front desk on {FRONT_DESK_PHONE} (Monday to Saturday, 9am to 8pm).",
    "hinglish": f"Zaroor. Main yeh chat hamare staff ko de raha hoon, woh jaldi aapki madad karenge. Zaroori ho to front desk ko {FRONT_DESK_PHONE} par call kijiye (Monday se Saturday, subah 9 se shaam 8).",
    "hi": f"ज़रूर। मैं यह चैट हमारे स्टाफ को दे रहा हूँ, वे जल्दी आपकी मदद करेंगे। ज़रूरी हो तो फ्रंट डेस्क को {FRONT_DESK_PHONE} पर कॉल करें (सोमवार से शनिवार, सुबह 9 से शाम 8)।",
}
ALREADY_HANDED_OFF_REPLY = {
    "en": f"Our staff have this chat and will get back to you shortly. For anything urgent, please call the front desk on {FRONT_DESK_PHONE}.",
    "hinglish": f"Hamare staff ke paas yeh chat hai, woh jaldi aapse baat karenge. Zaroori ho to front desk ko {FRONT_DESK_PHONE} par call kijiye.",
    "hi": f"हमारे स्टाफ के पास यह चैट है, वे जल्दी आपसे संपर्क करेंगे। ज़रूरी हो तो फ्रंट डेस्क को {FRONT_DESK_PHONE} पर कॉल करें।",
}
TROUBLE_REPLY = {
    "en": f"Sorry, I'm having trouble right now. I'm passing this to our staff, who will help you shortly. You can also call the front desk on {FRONT_DESK_PHONE}.",
    "hinglish": f"Maaf kijiye, abhi mujhe dikkat aa rahi hai. Main yeh hamare staff ko de raha hoon, woh jaldi madad karenge. Aap front desk ko {FRONT_DESK_PHONE} par bhi call kar sakte hain.",
    "hi": f"माफ़ कीजिए, अभी मुझे दिक्कत आ रही है। मैं यह हमारे स्टाफ को दे रहा हूँ, वे जल्दी मदद करेंगे। आप फ्रंट डेस्क को {FRONT_DESK_PHONE} पर भी कॉल कर सकते हैं।",
}


def handoff_flags(reason: str) -> dict[str, Any]:
    return {"handoff": True, "handoff_reason": reason}


class HandoffGate:
    """After a handoff the chat belongs to staff; before one, an explicit request for a person triggers it."""

    async def check(self, ctx: RunContext, message: str, history: list[dict[str, Any]]) -> Intercept | None:
        language = ctx.state.get("language")
        if ctx.state.get("handed_off"):
            return Intercept(pick(ALREADY_HANDED_OFF_REPLY, language), "human", handoff_flags("already_handed_off"))
        if wants_human(message):
            return Intercept(pick(HANDOFF_REPLY, language), "human", handoff_flags("patient_request"), {"handed_off": True})
        return None


class HumanWorker:
    """The router chose `human` (upset patient, complaint). No model call."""

    name = "human"

    async def run(self, ctx: RunContext, messages: list[dict[str, Any]]) -> RunResult:
        ctx.flags.update(handoff_flags("router"))
        ctx.state["handed_off"] = True
        text = pick(HANDOFF_REPLY, ctx.state.get("language"))
        return RunResult(self.name, text, list(messages), StopReason.END_TURN, 0)
