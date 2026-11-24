"""Human handoff (context.md rule 8): the patient asks for a person, is upset, or the agent keeps failing.

- The router model decides when a patient asks for a person or is upset (route `human`); no regex.
- Other code (hooks) raises the `handoff` flag. Once raised the chat is staff's: ServiceRouter sends every
  later message to `human` (unless the router reports an emergency), which gives a short canned reply.
- The reply language is the one the router last reported for this session (English until then).
"""

from __future__ import annotations

from typing import Any

from agentcore import RunContext, RunResult, StopReason

from .config import FRONT_DESK_PHONE, pick

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

ROUTER_FAILED_REPLY = {
    "en": f"Sorry, something went wrong on our side and I couldn't process that. Please send your message again. If it is urgent or an emergency, call 108 or our front desk on {FRONT_DESK_PHONE}.",
    "hinglish": f"Maaf kijiye, hamari taraf se kuch gadbad ho gayi aur main yeh process nahi kar paya. Please apna message dobara bhejiye. Zaroori ya emergency ho to 108 par ya front desk ko {FRONT_DESK_PHONE} par call kijiye.",
    "hi": f"माफ़ कीजिए, हमारी तरफ़ से कुछ गड़बड़ हो गई और मैं इसे प्रोसेस नहीं कर पाया। कृपया अपना संदेश दोबारा भेजें। ज़रूरी या आपातकाल हो तो 108 पर या फ्रंट डेस्क को {FRONT_DESK_PHONE} पर कॉल करें।",
}


def handoff_flags(reason: str) -> dict[str, Any]:
    return {"handoff": True, "handoff_reason": reason}


class HumanWorker:
    """The router chose `human` (asks for a person, upset, complaint) or the chat was already handed to staff. No model call."""

    name = "human"

    async def run(self, ctx: RunContext, messages: list[dict[str, Any]]) -> RunResult:
        language = ctx.state.get("language")
        if ctx.state.get("handed_off"):   # staff already have this chat
            ctx.flags.update(handoff_flags("already_handed_off"))
            return RunResult(self.name, pick(ALREADY_HANDED_OFF_REPLY, language), list(messages), StopReason.END_TURN, 0)
        ctx.flags.update(handoff_flags("router"))
        ctx.state["handed_off"] = True
        text = pick(HANDOFF_REPLY, language)
        return RunResult(self.name, text, list(messages), StopReason.END_TURN, 0)


class RouterFailureWorker:
    """The router could not classify the message. No agent runs on an unrouted message: ask the patient to
    resend and point to 108 / the front desk. No handoff: the chat is not staff's, the system just hiccuped."""

    name = "unrouted"

    async def run(self, ctx: RunContext, messages: list[dict[str, Any]]) -> RunResult:
        return RunResult(self.name, pick(ROUTER_FAILED_REPLY, ctx.state.get("language")), list(messages), StopReason.END_TURN, 0)
